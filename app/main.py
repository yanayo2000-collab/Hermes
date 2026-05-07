from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.async_pipeline import CircuitBreaker, TokenBucketRateLimiter, fingerprint_payload
from app.crm_adapter import LiveCrmAdapter
from app.native_ocr import normalize_native_ocr_fields
from app.ocr_adapter import RapidOcrAdapter
from app.production_ops import build_success_notifications, fetch_json, format_lark_alert


PHONE_PREFIX_COUNTRY_MAP = {
    '1': 'United States',
    '7': 'Russia',
    '20': 'Egypt',
    '27': 'South Africa',
    '31': 'Netherlands',
    '32': 'Belgium',
    '33': 'France',
    '34': 'Spain',
    '39': 'Italy',
    '44': 'United Kingdom',
    '49': 'Germany',
    '52': 'Mexico',
    '55': 'Brazil',
    '60': 'Malaysia',
    '61': 'Australia',
    '62': 'Indonesia',
    '63': 'Philippines',
    '64': 'New Zealand',
    '65': 'Singapore',
    '66': 'Thailand',
    '81': 'Japan',
    '82': 'South Korea',
    '84': 'Vietnam',
    '86': 'China',
    '90': 'Turkey',
    '91': 'India',
    '92': 'Pakistan',
    '93': 'Afghanistan',
    '94': 'Sri Lanka',
    '95': 'Myanmar',
    '98': 'Iran',
    '212': 'Morocco',
    '213': 'Algeria',
    '216': 'Tunisia',
    '218': 'Libya',
    '220': 'Gambia',
    '221': 'Senegal',
    '233': 'Ghana',
    '234': 'Nigeria',
    '251': 'Ethiopia',
    '254': 'Kenya',
    '255': 'Tanzania',
    '256': 'Uganda',
    '351': 'Portugal',
    '352': 'Luxembourg',
    '353': 'Ireland',
    '354': 'Iceland',
    '355': 'Albania',
    '356': 'Malta',
    '357': 'Cyprus',
    '358': 'Finland',
    '380': 'Ukraine',
    '420': 'Czech Republic',
    '852': 'Hong Kong',
    '853': 'Macau',
    '855': 'Cambodia',
    '856': 'Laos',
    '880': 'Bangladesh',
    '886': 'Taiwan',
    '961': 'Lebanon',
    '962': 'Jordan',
    '963': 'Syria',
    '964': 'Iraq',
    '965': 'Kuwait',
    '966': 'Saudi Arabia',
    '971': 'United Arab Emirates',
    '972': 'Israel',
    '973': 'Bahrain',
    '974': 'Qatar',
    '975': 'Bhutan',
    '976': 'Mongolia',
    '977': 'Nepal',
    '998': 'Uzbekistan',
}

GLOBAL_PHONE_PATTERN = re.compile(r'^\+(\d{1,3})(?:[ \t\-()]|\d){6,}$')
PHONE_CANDIDATE_PATTERN = re.compile(r'(\+?\d[\d \t\-().]{8,}\d)')
GROUP_VALUE_PATTERN = re.compile(r'^[A-Za-z]+-\d+$', flags=re.IGNORECASE)
GROUP_CANDIDATE_WITHOUT_DASH_PATTERN = re.compile(r'^[A-Za-z]+\d+$', flags=re.IGNORECASE)
PURE_DIGIT_ID_PATTERN = re.compile(r'^\d{6,12}$')
BARE_INVITE_CODE_PATTERN = re.compile(r'^(?:[A-Z]{6}|(?=.*[A-Z])[A-Z0-9]{6})$')
INVITE_CODE_CAPTURE_PATTERN = re.compile(r'(?:^|\b)(?:invite\s*code|personal\s*invite\s*code|code|个人邀请码|邀请码|kode\s+gabung\s+agensi|codigo\s+da\s+pessoa)\s*[:：是]?\s*([^\s"\'{}]{4,16})', flags=re.IGNORECASE)
INVITE_CODE_HOMOGLYPH_MAP = {
    'А': 'A', 'Β': 'B', 'В': 'B', 'С': 'C', 'Ε': 'E', 'Е': 'E', 'Η': 'H', 'Н': 'H', 'Ι': 'I', 'І': 'I',
    'Ј': 'J', 'Κ': 'K', 'К': 'K', 'М': 'M', 'Ν': 'N', 'О': 'O', 'Ο': 'O', 'Р': 'P', 'Ρ': 'P', 'Ѕ': 'S',
    'Т': 'T', 'Τ': 'T', 'Х': 'X', 'Χ': 'X', 'Υ': 'Y', 'Ү': 'Y', 'Ζ': 'Z',
    'а': 'A', 'β': 'B', 'в': 'B', 'с': 'C', 'ε': 'E', 'е': 'E', 'η': 'H', 'н': 'H', 'ι': 'I', 'і': 'I',
    'ј': 'J', 'κ': 'K', 'к': 'K', 'м': 'M', 'ո': 'N', 'ο': 'O', 'о': 'O', 'ρ': 'P', 'р': 'P', 'ѕ': 'S',
    'τ': 'T', 'т': 'T', 'χ': 'X', 'х': 'X', 'у': 'Y', 'γ': 'Y', 'ζ': 'Z',
}


def normalize_invite_code_candidate(raw: Optional[str]) -> Dict[str, Any]:
    raw_text = str(raw or '').strip()
    if not raw_text:
        return {
            'raw_input': None,
            'normalized': None,
            'has_homoglyphs': False,
            'unsupported_chars': [],
            'is_valid': False,
        }
    normalized_chars = []
    unsupported_chars = []
    has_homoglyphs = False
    for char in raw_text:
        upper_char = char.upper()
        if re.fullmatch(r'[A-Z0-9]', upper_char):
            normalized_chars.append(upper_char)
            continue
        mapped = INVITE_CODE_HOMOGLYPH_MAP.get(char) or INVITE_CODE_HOMOGLYPH_MAP.get(upper_char)
        if mapped:
            normalized_chars.append(mapped)
            has_homoglyphs = True
            continue
        unsupported_chars.append(char)
    normalized = ''.join(normalized_chars).upper() if normalized_chars else None
    is_valid = bool(normalized and BARE_INVITE_CODE_PATTERN.fullmatch(normalized))
    return {
        'raw_input': raw_text,
        'normalized': normalized,
        'has_homoglyphs': has_homoglyphs,
        'unsupported_chars': unsupported_chars,
        'is_valid': is_valid,
    }


def validate_invite_code_field(invite_code: Optional[str], *, invite_code_meta: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, str]]:
    meta = invite_code_meta or normalize_invite_code_candidate(invite_code)
    normalized = str(meta.get('normalized') or '').strip().upper()
    if meta.get('unsupported_chars'):
        return {
            'reason': 'invalid_invite_code_format',
            'detail': 'invite_code contains unsupported non-Latin characters',
            'reply_text': 'Invalid Code. Use 6 English letters or letters+digits only.',
        }
    if normalized and not meta.get('is_valid'):
        return {
            'reason': 'invalid_invite_code_format',
            'detail': 'invite_code must be 6 English letters or letters+digits, not pure digits',
            'reply_text': 'Invalid Code. Use 6 English letters or letters+digits only.',
        }
    return None


def extract_bare_multiline_candidates(text: str) -> Dict[str, Optional[str]]:
    lines = [line.strip() for line in str(text or '').splitlines() if line.strip()]
    result: Dict[str, Optional[str]] = {
        'mobile_line': None,
        'registration_group_line': None,
        'account_id_line': None,
        'invite_code_line': None,
    }
    for line in lines:
        if result['mobile_line'] is None and GLOBAL_PHONE_PATTERN.fullmatch(line):
            result['mobile_line'] = line
            continue
        if result['registration_group_line'] is None and GROUP_VALUE_PATTERN.fullmatch(line):
            result['registration_group_line'] = line
            continue
        if result['account_id_line'] is None and PURE_DIGIT_ID_PATTERN.fullmatch(line):
            result['account_id_line'] = line
            continue
        if result['invite_code_line'] is None:
            invite_meta = normalize_invite_code_candidate(line)
            normalized = str(invite_meta.get('normalized') or '').strip().upper()
            if invite_meta.get('is_valid') and normalized.lower() not in {'linky', 'fumi'}:
                result['invite_code_line'] = normalized
                continue
    return result


def extract_invalid_group_candidate(text: str) -> Optional[str]:
    for line in [line.strip() for line in str(text or '').splitlines() if line.strip()]:
        if GROUP_CANDIDATE_WITHOUT_DASH_PATTERN.fullmatch(line):
            return line
    labeled_match = re.search(r'(?:注册群组|group)\s*[:：]?\s*([A-Za-z]+\d+)', str(text or ''), flags=re.IGNORECASE)
    if labeled_match:
        candidate = str(labeled_match.group(1) or '').strip()
        if GROUP_CANDIDATE_WITHOUT_DASH_PATTERN.fullmatch(candidate):
            return candidate
    return None


def normalize_phone_identity(*, mobile: str, area_code: int, country: str) -> tuple[str, int, str]:
    raw = str(mobile or '').strip()
    normalized_country = str(country or '').strip()
    normalized_area_code = int(area_code or 0)

    international_match = re.fullmatch(r'\+(\d{1,3})\s+(\d{6,15})', raw)
    if international_match:
        prefix = international_match.group(1)
        body = international_match.group(2)
        normalized_area_code = int(prefix)
        normalized_country = PHONE_PREFIX_COUNTRY_MAP.get(prefix, normalized_country)
        return body, normalized_area_code, normalized_country

    explicit_prefix_match = re.fullmatch(r'\+(\d{1,3})[ \t\-().]+([\d \t\-().]{4,})', raw)
    if explicit_prefix_match:
        prefix = explicit_prefix_match.group(1)
        body = ''.join(ch for ch in explicit_prefix_match.group(2) if ch.isdigit())
        if body:
            normalized_area_code = int(prefix)
            normalized_country = PHONE_PREFIX_COUNTRY_MAP.get(prefix, normalized_country)
            return body, normalized_area_code, normalized_country

    if raw.startswith('+'):
        digits = '+' + ''.join(ch for ch in raw[1:] if ch.isdigit())
        for prefix in sorted(PHONE_PREFIX_COUNTRY_MAP.keys(), key=len, reverse=True):
            if digits.startswith(f'+{prefix}'):
                normalized_area_code = int(prefix)
                normalized_country = PHONE_PREFIX_COUNTRY_MAP[prefix]
                body = digits[len(prefix) + 1:]
                return body, normalized_area_code, normalized_country
        raw = ''.join(ch for ch in raw if ch.isdigit())
    else:
        raw = ''.join(ch for ch in raw if ch.isdigit())

    if normalized_area_code and not normalized_country:
        normalized_country = PHONE_PREFIX_COUNTRY_MAP.get(str(normalized_area_code), normalized_country)

    return raw, normalized_area_code, normalized_country


def format_display_phone(phone: Optional[str], *, area_code: Optional[int] = None) -> str:
    raw = str(phone or '').strip()
    if not raw or raw == '-':
        return '-'
    if re.search(r'[^\d\s+\-().]', raw):
        return raw
    digits_only = ''.join(ch for ch in raw if ch.isdigit())
    if raw.startswith('+'):
        normalized_mobile, normalized_area_code, _ = normalize_phone_identity(mobile=raw, area_code=int(area_code or 0), country='')
        if normalized_mobile and normalized_area_code:
            return f'+{normalized_area_code} {normalized_mobile}'
        return raw
    normalized_area_code = int(area_code or 0)
    if not normalized_area_code and digits_only:
        for prefix in sorted(PHONE_PREFIX_COUNTRY_MAP.keys(), key=len, reverse=True):
            if digits_only.startswith(prefix) and len(digits_only) > len(prefix):
                normalized_area_code = int(prefix)
                digits_only = digits_only[len(prefix):]
                break
    if normalized_area_code and digits_only:
        return f'+{normalized_area_code} {digits_only}'
    return raw


def validate_fast_intake_fields(*, mobile: Optional[str], app_name: Optional[str], account_id: Optional[str]) -> Optional[Dict[str, str]]:
    phone_text = str(mobile or '').strip()
    app_text = str(app_name or '').strip()
    account_text = str(account_id or '').strip()

    normalized_phone_text = format_display_phone(phone_text) if phone_text else ''
    if phone_text and not re.fullmatch(r'\+\d{1,3}\s\d{6,15}', normalized_phone_text):
        return {
            'reason': 'invalid_phone_format',
            'detail': 'mobile must use format +<country code> <number>',
            'reply_text': 'Invalid phone format. Use +<country code> <number>.',
        }

    if account_text and not account_text.isdigit():
        return {
            'reason': 'invalid_account_id_format',
            'detail': 'account_id must contain digits only',
            'reply_text': 'Invalid ID. Digits only.',
        }

    if app_text.lower() in {'linky', 'fumi'} and account_text and not re.fullmatch(r'\d{8}', account_text):
        app_label = 'Linky' if app_text.lower() == 'linky' else 'FUMI'
        return {
            'reason': 'invalid_account_id_format',
            'detail': f'{app_label} account_id must be exactly 8 digits',
            'reply_text': f'Invalid ID. {app_label} requires exactly 8 digits.',
        }

    return None


def parse_manual_cs_message(*, text: str, image_ocr_text: Optional[str] = None) -> Dict[str, Any]:
    text = str(text or '')
    image_ocr_text = str(image_ocr_text or '')
    combined = "\n".join(part for part in [text, image_ocr_text] if part).strip()
    normalized = combined.replace('：', ':')
    text_normalized = text.replace('：', ':')
    bare_candidates = extract_bare_multiline_candidates(text)
    ocr_normalized = normalize_native_ocr_fields(image_ocr_text) if image_ocr_text.strip() else {}

    mobile = None
    phone_candidate = None
    if bare_candidates.get('mobile_line'):
        phone_candidate = str(bare_candidates['mobile_line']).strip()
    else:
        phone_match = PHONE_CANDIDATE_PATTERN.search(normalized)
        if phone_match:
            phone_candidate = phone_match.group(1).strip()
    if phone_candidate:
        mobile, area_code, country = normalize_phone_identity(mobile=phone_candidate, area_code=0, country='')
    else:
        area_code, country = 0, ''

    text_account_id = None
    ocr_account_id = None
    labeled_patterns = [
        r'(?:^|\b)(?:id|uid|ywid|用户id|用户ID)\s*[:：是]?\s*(\d{6,})',
    ]
    for pattern in labeled_patterns:
        match = re.search(pattern, text.replace('：', ':'), flags=re.IGNORECASE)
        if match:
            text_account_id = match.group(1)
            break
    if image_ocr_text:
        match = re.search(r'(?:uid|id)\s*[:：]?\s*(\d{6,})', image_ocr_text, flags=re.IGNORECASE)
        if match:
            ocr_account_id = match.group(1)
    if not ocr_account_id:
        ocr_account_id = str(ocr_normalized.get('account_id') or '').strip() or None
    text_invite_code = None
    invite_code_meta = {
        'raw_input': None,
        'normalized': None,
        'has_homoglyphs': False,
        'unsupported_chars': [],
        'is_valid': False,
    }
    match = INVITE_CODE_CAPTURE_PATTERN.search(text.replace('：', ':'))
    if match:
        invite_code_meta = normalize_invite_code_candidate(str(match.group(1) or '').strip())
        if invite_code_meta.get('is_valid'):
            text_invite_code = str(invite_code_meta.get('normalized') or '').strip().upper() or None
    ocr_invite_meta = normalize_invite_code_candidate(
        str(
            ocr_normalized.get('person_code')
            or ocr_normalized.get('invite_code')
            or ocr_normalized.get('guild_invite_code')
            or ''
        ).strip().upper() or None
    )
    ocr_invite_code = str(ocr_invite_meta.get('normalized') or '').strip().upper() if ocr_invite_meta.get('is_valid') else None
    bare_invite_meta = normalize_invite_code_candidate(str(bare_candidates.get('invite_code_line') or '').strip().upper() or None)
    bare_invite_code = str(bare_invite_meta.get('normalized') or '').strip().upper() if bare_invite_meta.get('is_valid') else None
    invite_code = ocr_invite_code or text_invite_code or bare_invite_code
    selected_invite_meta = ocr_invite_meta if ocr_invite_code else (invite_code_meta if text_invite_code else bare_invite_meta)
    inferred_text_account_id = str(bare_candidates.get('account_id_line') or '').strip() or None
    account_id = ocr_account_id or text_account_id or inferred_text_account_id
    if not account_id:
        digit_runs = re.findall(r'\b\d{6,}\b', normalized)
        if mobile:
            digit_runs = [run for run in digit_runs if run != mobile and run != f"62{mobile}"]
        if digit_runs:
            account_id = digit_runs[-1]

    registration_group = None
    group_patterns = [
        r'(?:注册群组|group)\s*[:：]?\s*([A-Za-z]+(?:-\d+)?)',
        r'([A-Za-z]+-\d+)',
        r'([A-Za-z]+)组',
    ]
    for pattern in group_patterns:
        match = re.search(pattern, text_normalized, flags=re.IGNORECASE)
        if match:
            registration_group = match.group(1)
            break
    if not registration_group and bare_candidates.get('registration_group_line'):
        registration_group = str(bare_candidates['registration_group_line']).strip()

    app_name = None
    for app in ['Linky', 'FUMI']:
        if re.search(rf'\b{re.escape(app)}\b', text_normalized, flags=re.IGNORECASE):
            app_name = app
            break

    dept_name = None
    explicit_dept_patterns = [
        r'(?:公会|agency|guild|dept)\s*[:：]?\s*([A-Za-z]+(?:-\d+)?)',
    ]
    inferred_dept_patterns = [
        r'\b(Piso|Permata|Sampanye|Carote)\b(?!-\d)',
    ]
    for pattern in explicit_dept_patterns:
        match = re.search(pattern, text_normalized, flags=re.IGNORECASE)
        if match:
            dept_name = match.group(1)
            break
    if not dept_name:
        dept_name = str(ocr_normalized.get('guild_name') or ocr_normalized.get('agency_name') or '').strip() or None
    if not dept_name:
        for pattern in inferred_dept_patterns:
            match = re.search(pattern, text_normalized, flags=re.IGNORECASE)
            if match:
                dept_name = match.group(1)
                break

    conflicts = []
    if text_account_id and ocr_account_id and text_account_id != ocr_account_id:
        conflicts.append('account_id_conflict')

    missing_fields = [
        name for name, value in {
            'mobile': mobile,
            'account_id': account_id,
            'registration_group': registration_group,
            'app_name': app_name,
            'dept_name': dept_name,
            'invite_code': invite_code,
        }.items() if not value
    ]

    score = 0.0
    weights = {
        'mobile': 0.2,
        'account_id': 0.2,
        'registration_group': 0.2,
        'app_name': 0.15,
        'dept_name': 0.15,
        'invite_code': 0.1,
    }
    values = {
        'mobile': mobile,
        'account_id': account_id,
        'registration_group': registration_group,
        'app_name': app_name,
        'dept_name': dept_name,
        'invite_code': invite_code,
    }
    for key, weight in weights.items():
        if values[key]:
            score += weight
    if conflicts:
        score -= 0.15
    confidence = max(0.0, min(round(score, 2), 1.0))

    return {
        'mobile': mobile,
        'area_code': area_code,
        'country': country,
        'account_id': account_id,
        'registration_group': registration_group,
        'app_name': app_name,
        'dept_name': dept_name,
        'invite_code': invite_code,
        'confidence': confidence,
        'missing_fields': missing_fields,
        'conflicts': conflicts,
        'evidence': {
            'text_used': bool(text.strip()),
            'image_ocr_used': bool(image_ocr_text.strip()),
            'text_account_id': text_account_id,
            'ocr_account_id': ocr_account_id,
            'text_invite_code': text_invite_code,
            'ocr_invite_code': ocr_invite_code,
            'invite_code_raw_input': selected_invite_meta.get('raw_input'),
            'invite_code_had_homoglyphs': bool(selected_invite_meta.get('has_homoglyphs')),
            'invite_code_unsupported_chars': list(selected_invite_meta.get('unsupported_chars') or []),
        },
        'invite_code_meta': selected_invite_meta,
        'raw_text': text,
        'raw_ocr_text': image_ocr_text,
    }


def extract_explicit_intake_fields(text: str) -> Dict[str, Optional[str]]:
    normalized = str(text or '').replace('：', ':')
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    patterns = {
        'mobile': [r'(?:^|\n|\b)(?:phone|mobile)\s*:\s*([^\n]+)'],
        'account_id': [r'(?:^|\n|\b)(?:id|uid|account_id)\s*:\s*(\d{6,})'],
        'registration_group': [r'(?:^|\n|\b)(?:group|registration_group)\s*:\s*([^\n]+)'],
        'app_name': [r'(?:^|\n|\b)(?:app)\s*:\s*([^\n]+)'],
        'dept_name': [r'(?:^|\n|\b)(?:agency|guild|dept|公会)\s*:\s*([^\n]+)'],
        'invite_code': [r'(?:^|\n|\b)(?:invite\s*code|personal\s*invite\s*code|code|个人邀请码|邀请码)\s*:\s*([^\n]+)'],
    }
    result: Dict[str, Optional[str]] = {
        'mobile': None,
        'account_id': None,
        'registration_group': None,
        'app_name': None,
        'dept_name': None,
        'invite_code': None,
    }
    for key, pats in patterns.items():
        for pattern in pats:
            match = re.search(pattern, normalized, flags=re.IGNORECASE)
            if match:
                result[key] = str(match.group(1) or '').strip()
                break
    if result['app_name'] is None:
        for line in lines:
            if re.fullmatch(r'(Linky|FUMI)', line, flags=re.IGNORECASE):
                result['app_name'] = line
                break
    if result['dept_name'] is None:
        for line in lines:
            if re.fullmatch(r'(Piso|Permata|Sampanye|Carote)', line, flags=re.IGNORECASE):
                result['dept_name'] = line
                break
    return result


DEFAULT_DB_PATH = str(Path(__file__).resolve().parents[1] / "data" / "automation.db")

INTAKE_BOT_PRESETS_PAGE_HTML = """
<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>收口机器人配置中心</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 24px; background: #f6f8fb; color: #1f2937; }
    h1 { margin: 0 0 8px 0; }
    h2 { margin-bottom: 10px; }
    .muted { color: #6b7280; font-size: 13px; }
    .page-grid { display: grid; grid-template-columns: 1fr; gap: 16px; }
    .card { background: white; border-radius: 12px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-top: 16px; }
    .card.tight { padding: 12px 16px; }
    .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
    .summary-item { border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px; background: #fafbff; }
    .summary-item .label { color: #6b7280; font-size: 12px; margin-bottom: 6px; }
    .summary-item .value { font-size: 18px; font-weight: 700; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 980px; }
    th, td { padding: 12px; border-bottom: 1px solid #e5e7eb; text-align: left; font-size: 14px; vertical-align: top; }
    th { background: #eef2ff; position: sticky; top: 0; z-index: 1; }
    input, select { width: 100%; box-sizing: border-box; padding: 8px 10px; font-size: 14px; min-width: 180px; border: 1px solid #d1d5db; border-radius: 8px; background: #fff; }
    input:focus, select:focus { outline: none; border-color: #2563eb; box-shadow: 0 0 0 3px rgba(37,99,235,.12); }
    .field-stack { display: flex; flex-direction: column; gap: 8px; }
    .executor-form-grid { display: grid; grid-template-columns: repeat(2, minmax(240px, 1fr)); gap: 12px; }
    .executor-card-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; margin-top: 12px; }
    .executor-card { border: 1px solid #e5e7eb; border-radius: 12px; padding: 12px; background: #fafbff; }
    .executor-card h3 { margin: 0 0 10px 0; font-size: 16px; }
    .executor-meta { display: grid; grid-template-columns: 92px 1fr; gap: 6px 10px; font-size: 13px; align-items: start; }
    .executor-meta .k { color: #6b7280; }
    .executor-actions { margin-top: 10px; display: flex; gap: 8px; }
    .field-hint { color: #6b7280; font-size: 12px; }
    button { padding: 8px 12px; border-radius: 8px; border: none; background: #2563eb; color: #fff; cursor: pointer; white-space: nowrap; }
    button.secondary { background: #374151; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .toast { position: fixed; right: 24px; bottom: 24px; min-width: 240px; background: #111827; color: #fff; padding: 12px 14px; border-radius: 10px; display: none; }
    .toast.success { background: #065f46; }
    .toast.error { background: #991b1b; }
    .page-shell { max-width: 1320px; margin: 0 auto; }
    .shell-nav { position: sticky; top: 0; z-index: 20; display:flex; gap:10px; flex-wrap:wrap; margin: 0 0 16px 0; padding: 10px 0 12px; background: rgba(246,248,251,.96); backdrop-filter: blur(8px); }
    .shell-nav a { color:#2563eb; text-decoration:none; font-size:13px; padding:6px 10px; border-radius:999px; background:#eef2ff; }
    .hero { background:#ffffff; border:1px solid #e5e7eb; border-radius:16px; padding:20px; box-shadow:0 1px 3px rgba(0,0,0,.06); }
    .hero .eyebrow { color:#6366f1; font-size:12px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; margin-bottom:8px; }
    .hero .subtitle { color:#4b5563; font-size:14px; margin-top:8px; }
    .config-workspace { display:grid; gap:16px; margin-top:16px; }
  </style>
</head>
<body>
  <div class="page-shell">
    <div class="shell-nav">
      <a href="/ops">运营工作台</a>
      <a href="/ops/intake-bot-presets">收口配置中心</a>
      <a href="/ops/production-ops">群审批控制台</a>
      <a href="/ops/official-group-bridge">官方群审批桥接台</a>
    </div>
    <div class="hero">
      <div class="eyebrow">Config Center</div>
      <h1>收口配置中心</h1>
      <div class="subtitle">收口机器人配置中心 · 实时修改默认 default_app / default_guild。仅允许使用 CRM 下拉选项；当选项不可用时禁止保存。</div>
      <div class="muted" style="margin-top:8px;">同页还会加载公会执行器配置：/api/ops/guild-executors</div>
      <div class="muted" style="margin-top:8px;"><a href="/ops">返回运营工作台</a></div>
    </div>

    <div class="config-workspace">
      <div class="card tight config-overview">
        <h2 style="margin-top:0;">配置概况</h2>
    <div class="summary-grid">
      <div class="summary-item"><div class="label">收口机器人</div><div class="value" id="presetCount">-</div></div>
      <div class="summary-item"><div class="label">公会执行器</div><div class="value" id="executorCount">-</div></div>
      <div class="summary-item"><div class="label">已配置代理</div><div class="value" id="executorProxyCount">-</div></div>
      <div class="summary-item"><div class="label">已配置密码引用</div><div class="value" id="executorSecretCount">-</div></div>
      <div class="summary-item"><div class="label">生产守护</div><div class="value" id="daemonEnabledState">-</div></div>
    </div>
  </div>

  </div>

  <div class="card">
    <h2 style="margin-top:0;">机器人配置列表</h2>
    <div class="table-wrap">
      <table>
      <thead>
        <tr>
          <th>profile</th>
          <th>robot_name</th>
          <th>app_id</th>
          <th>default_app</th>
          <th>default_guild</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody id=\"presetRows\"></tbody>
      </table>
    </div>
  </div>

  <div class=\"card\" style=\"margin-top:16px;\">
    <h2 style=\"margin-top:0;\">新增收口机器人</h2>
    <div class=\"field-stack\" style=\"max-width:520px; gap:12px;\">
      <label class=\"field-hint\">profile_name</label>
      <input id=\"new_profile_name\" placeholder=\"e.g. intake-a96f1cec\" />
      <label class=\"field-hint\">robot_name</label>
      <input id=\"new_robot_name\" placeholder=\"e.g. Permata Intake Bot\" />
      <label class=\"field-hint\">app_id</label>
      <input id=\"new_app_id\" placeholder=\"e.g. cli_xxx\" />
      <label class=\"field-hint\">default_app</label>
      <select id=\"new_default_app\"></select>
      <div id=\"new_default_app_hint\" class=\"field-hint\"></div>
      <label class=\"field-hint\">default_guild</label>
      <select id=\"new_default_guild\"></select>
      <div id=\"new_default_guild_hint\" class=\"field-hint\"></div>
      <div>
        <button id=\"createPresetButton\" onclick=\"createPreset()\">新增机器人</button>
      </div>
    </div>
  </div>

  <div class=\"card\" style=\"margin-top:16px;\">
    <h2 style=\"margin-top:0;\">群审批控制台</h2>
    <div class=\"muted\" style=\"margin-bottom:12px;\">注册群守护与官方群审批现已收敛到统一控制面。这里仅展示当前状态，点击后进入控制台操作。</div>
    <div class=\"summary-grid\">
      <div class=\"summary-item\"><div class=\"label\">当前状态</div><div class=\"value\" id=\"daemonEnabledState\">-</div></div>
      <div class=\"summary-item\"><div class=\"label\">入口</div><div class=\"value\"><a href=\"/ops/production-ops\">打开群审批控制台</a></div></div>
    </div>
    <div class=\"muted\" id=\"productionOpsRuntimeHint\" style=\"margin-top:12px;\">运行状态加载中…</div>
    <div style=\"margin-top:12px;\">
      <a href=\"/ops/production-ops\"><button type=\"button\">进入群审批控制台</button></a>
    </div>
  </div>

  <div class=\"card\" style=\"margin-top:16px;\">
    <h2 style=\"margin-top:0;\">执行器总览</h2>
    <div class=\"muted\" style=\"margin-bottom:8px;\">公会执行器配置</div>
    <div class=\"muted\">用于配置每个公会的后台地址、账号、密码引用、代理与浏览器执行参数。接口：/api/ops/guild-executors</div>
    <div id=\"guildExecutorRows\" class=\"executor-card-grid\"></div>
  </div>

  <div class=\"card\" style=\"margin-top:16px;\">
    <h2 style=\"margin-top:0;\">新增 / 更新公会执行器</h2>
    <div class=\"muted\" style=\"margin-bottom:12px;\">字段均提供中文说明；英文键名保留在括号中，便于后续技术对接。</div>
    <div class=\"executor-form-grid\">
      <div class=\"field-stack\">
        <label class=\"field-hint\">公会名（guild_name）</label>
        <input id=\"new_executor_guild_name\" placeholder=\"例如 Permata\" />
      </div>
      <div class=\"field-stack\">
        <label class=\"field-hint\">后台地址（backend_url）</label>
        <input id=\"new_executor_backend_url\" placeholder=\"https://guild.linke.ai/guild/addAnchor\" />
      </div>
      <div class=\"field-stack\">
        <label class=\"field-hint\">登录账号（login_username）</label>
        <input id=\"new_executor_login_username\" placeholder=\"后台登录账号\" />
      </div>
      <div class=\"field-stack\">
        <label class=\"field-hint\">密码引用（password_secret_ref）</label>
        <input id=\"new_executor_password_secret_ref\" placeholder=\"只填密码引用，不回显明文\" />
      </div>
      <div class=\"field-stack\">
        <label class=\"field-hint\">代理地址（proxy_url）</label>
        <input id=\"new_executor_proxy_url\" placeholder=\"http://user:pass@host:port\" />
      </div>
      <div class=\"field-stack\">
        <label class=\"field-hint\">代理地区（proxy_region）</label>
        <select id=\"new_executor_proxy_region\"></select>
        <div class=\"field-hint\">先按 15 个大型城市预置；后续扩城市时直接加到统一选项表。</div>
      </div>
      <div class=\"field-stack\">
        <label class=\"field-hint\">代理类型（proxy_type）</label>
        <input id=\"new_executor_proxy_type\" placeholder=\"http / socks5\" value=\"http\" />
      </div>
      <div class=\"field-stack\">
        <label class=\"field-hint\">浏览器配置键（browser_profile_key）</label>
        <input id=\"new_executor_browser_profile_key\" placeholder=\"例如 permata-profile\" />
      </div>
      <div class=\"field-stack\">
        <label class=\"field-hint\">并发数（bind_concurrency）</label>
        <input id=\"new_executor_bind_concurrency\" type=\"number\" min=\"1\" value=\"1\" />
      </div>
      <div class=\"field-stack\">
        <label class=\"field-hint\">超时秒数（request_timeout_seconds）</label>
        <input id=\"new_executor_request_timeout_seconds\" type=\"number\" min=\"5\" value=\"30\" />
      </div>
      <div class=\"field-stack\">
        <label class=\"field-hint\">是否启用（enabled）</label>
        <select id=\"new_executor_enabled\"><option value=\"true\" selected>启用</option><option value=\"false\">停用</option></select>
      </div>
      <div class=\"field-stack\">
        <label class=\"field-hint\">备注（notes）</label>
        <input id=\"new_executor_notes\" placeholder=\"可填城市、用途、风险备注\" />
      </div>
    </div>
    <div style=\"margin-top:12px; display:flex; gap:8px;\">
      <button id=\"createExecutorButton\" onclick=\"createOrUpdateExecutor()\">保存公会执行器</button>
      <button type=\"button\" class=\"secondary\" onclick=\"clearExecutorForm()\">清空表单</button>
    </div>
  </div>

  <div id=\"presetToast\" class=\"toast\"></div>

<script>
async function loadJson(url, options) {
  const res = await fetch(url, options || {});
  if (!res.ok) {
    let detail = '';
    try {
      const data = await res.json();
      detail = data.detail || JSON.stringify(data);
    } catch (_) {}
    throw new Error(detail || `Failed to load ${url}: ${res.status}`);
  }
  return await res.json();
}
function showToast(message, type='success') {
  const toast = document.getElementById('presetToast');
  toast.textContent = message;
  toast.className = `toast ${type}`;
  toast.style.display = 'block';
  clearTimeout(window.__presetToastTimer);
  window.__presetToastTimer = setTimeout(() => { toast.style.display = 'none'; }, 2400);
}
function renderSelectOptions(options, selectedValue, placeholder) {
  const normalized = String(selectedValue || '');
  const rows = Array.isArray(options) ? options : [];
  if (!rows.length) {
    const fallbackLabel = placeholder || '暂无可用选项';
    return `<option value="">${fallbackLabel}</option>`;
  }
  return rows.map(item => {
    const value = String(item.value || '');
    const label = String(item.label || item.value || '');
    const selected = value === normalized ? ' selected' : '';
    const disabled = item.disabled ? ' disabled' : '';
    return `<option value="${value}"${selected}${disabled}>${label}</option>`;
  }).join('');
}
function renderExecutorProxyRegionOptions(selectedValue) {
  const options = Array.isArray(window.__guildExecutorProxyRegionOptions) ? window.__guildExecutorProxyRegionOptions : [];
  const rows = Array.isArray(window.__guildExecutors) ? window.__guildExecutors : [];
  const currentGuildName = String(document.getElementById('new_executor_guild_name')?.value || '').trim();
  const assignedMap = new Map();
  rows.forEach(row => {
    const region = String(row.proxy_region || '').trim();
    const guildName = String(row.guild_name || '').trim();
    if (region && guildName) assignedMap.set(region, guildName);
  });
  const decorated = options.map(item => {
    const value = String(item.value || '').trim();
    const assignedGuild = assignedMap.get(value) || '';
    const assignedToOther = assignedGuild && assignedGuild !== currentGuildName;
    return {
      value,
      label: assignedToOther ? `${item.label}（已分配给 ${assignedGuild}）` : String(item.label || value),
      disabled: Boolean(assignedToOther),
    };
  });
  const matchesConfigured = decorated.some(item => item.value === String(selectedValue || '').trim());
  if (selectedValue && !matchesConfigured) {
    decorated.unshift({value: String(selectedValue), label: `历史值（待改）: ${selectedValue}`, disabled: false});
  }
  return renderSelectOptions([{value: '', label: '不指定 / 留空'}].concat(decorated), selectedValue || '', '不指定 / 留空');
}
function presetFieldHtml(kind, row, options, source, currentValue) {
  const fieldId = `${kind}_${row.profile_name}`;
  const rows = Array.isArray(options) ? options : [];
  const hasOptions = rows.length > 0;
  const unavailable = !hasOptions || source === 'unavailable';
  const placeholder = kind === 'default_app' ? 'No CRM app options available' : 'No CRM guild options available';
  const disabledAttr = unavailable ? ' disabled data-unavailable="true"' : '';
  const selectHtml = `<select id="${fieldId}"${disabledAttr}>${renderSelectOptions(rows, currentValue, placeholder)}</select>`;
  let hint = 'CRM dropdown options are currently unavailable. Saving is disabled.';
  if (source === 'live' && hasOptions) {
    hint = 'Using live CRM dropdown options only.';
  } else if (source === 'cache' && hasOptions) {
    hint = 'Using cached CRM dropdown options only.';
  }
  const currentText = currentValue ? `<div class="field-hint">Current saved value: ${currentValue}</div>` : '';
  return `<div class="field-stack">${selectHtml}<div class="field-hint">${hint}</div>${currentText}</div>`;
}
function robotNameFieldHtml(row) {
  const inputId = `robot_name_${row.profile_name}`;
  const buttonId = `edit_robot_name_${row.profile_name}`;
  const value = String(row.robot_name || row.profile_name || '');
  return `<div class="field-stack"><input id="${inputId}" value="${value}" disabled /><button type="button" id="${buttonId}" onclick="enableRobotNameEdit('${row.profile_name}')">编辑名称</button></div>`;
}
function presetRowHtml(row, appOptions, guildOptions, appSource, guildSource) {
  const saveDisabled = appSource === 'unavailable' || guildSource === 'unavailable' || !appOptions.length || !guildOptions.length;
  return `<tr>
    <td>${row.profile_name}</td>
    <td>${robotNameFieldHtml(row)}</td>
    <td>${row.app_id || ''}</td>
    <td>${presetFieldHtml('default_app', row, appOptions, appSource, row.default_app)}</td>
    <td>${presetFieldHtml('default_guild', row, guildOptions, guildSource, row.default_guild)}</td>
    <td><button onclick="savePreset('${row.profile_name}')" ${saveDisabled ? 'disabled' : ''}>保存</button></td>
  </tr>`;
}
function enableRobotNameEdit(profileName) {
  const input = document.getElementById(`robot_name_${profileName}`);
  if (input) {
    input.disabled = false;
    input.focus();
    input.select();
  }
}
function renderCreatePresetForm(data) {
  const appOptions = data.app_options || [];
  const guildOptions = data.guild_options || [];
  const appSource = data.app_options_source || 'unavailable';
  const guildSource = data.guild_options_source || 'unavailable';
  const appSelect = document.getElementById('new_default_app');
  const guildSelect = document.getElementById('new_default_guild');
  const createButton = document.getElementById('createPresetButton');
  appSelect.innerHTML = renderSelectOptions(appOptions, '', 'No CRM app options available');
  guildSelect.innerHTML = renderSelectOptions(guildOptions, '', 'No CRM guild options available');
  document.getElementById('new_default_app_hint').textContent = appSource === 'live'
    ? 'Using live CRM dropdown options only.'
    : appSource === 'cache'
      ? 'Using cached CRM dropdown options only.'
      : 'CRM dropdown options are currently unavailable. Saving is disabled.';
  document.getElementById('new_default_guild_hint').textContent = guildSource === 'live'
    ? 'Using live CRM dropdown options only.'
    : guildSource === 'cache'
      ? 'Using cached CRM dropdown options only.'
      : 'CRM dropdown options are currently unavailable. Saving is disabled.';
  const disabled = appSource === 'unavailable' || guildSource === 'unavailable' || !appOptions.length || !guildOptions.length;
  appSelect.disabled = disabled;
  guildSelect.disabled = disabled;
  createButton.disabled = disabled;
}
function refreshExecutorProxyRegionSelect(selectedValue) {
  const field = document.getElementById('new_executor_proxy_region');
  if (!field) return;
  field.innerHTML = renderExecutorProxyRegionOptions(selectedValue || '');
  field.value = selectedValue || '';
}
function guildExecutorRowHtml(row) {
  const passwordState = row.password_configured ? '已配置' : '未配置';
  const enabledText = row.enabled ? '启用' : '停用';
  return `<div class="executor-card">
    <h3>${row.guild_name || ''}</h3>
    <div class="executor-meta">
      <div class="k">后台地址</div><div>${row.backend_url || '-'}</div>
      <div class="k">登录账号</div><div>${row.login_username || '-'}</div>
      <div class="k">密码引用</div><div>${passwordState}</div>
      <div class="k">代理地区</div><div>${row.proxy_region || '-'}</div>
      <div class="k">代理地址</div><div>${row.proxy_url || '-'}</div>
      <div class="k">代理类型</div><div>${row.proxy_type || '-'}</div>
      <div class="k">浏览器配置</div><div>${row.browser_profile_key || '-'}</div>
      <div class="k">并发数</div><div>${row.bind_concurrency || '-'}</div>
      <div class="k">超时秒数</div><div>${row.request_timeout_seconds || '-'}</div>
      <div class="k">状态</div><div>${enabledText}</div>
      <div class="k">备注</div><div>${row.notes || '-'}</div>
    </div>
    <div class="executor-actions">
      <button class="secondary" onclick="fillExecutorForm('${String(row.guild_name || '').replace(/'/g, "&#39;")}')">回填编辑</button>
      <button class="secondary" onclick="deleteExecutor('${String(row.guild_name || '').replace(/'/g, "&#39;")}')">删除执行器</button>
    </div>
  </div>`;
}
async function reloadGuildExecutors() {
  const data = await loadJson('/api/ops/guild-executors');
  const rows = Array.isArray(data.rows) ? data.rows : [];
  window.__guildExecutorProxyRegionOptions = Array.isArray(data.proxy_region_options) ? data.proxy_region_options : [];
  window.__guildExecutors = rows;
  document.getElementById('guildExecutorRows').innerHTML = rows.map(guildExecutorRowHtml).join('');
  document.getElementById('executorCount').textContent = String(rows.length);
  document.getElementById('executorProxyCount').textContent = String(rows.filter(row => String(row.proxy_url || '').trim()).length);
  document.getElementById('executorSecretCount').textContent = String(rows.filter(row => row.password_configured).length);
  refreshExecutorProxyRegionSelect(document.getElementById('new_executor_proxy_region').value || '');
}
function applyProductionOpsDaemonConfig(data) {
  const config = data.config || {};
  const enabledField = document.getElementById('production_ops_enabled');
  const intervalField = document.getElementById('production_ops_interval_seconds');
  const notifyField = document.getElementById('production_ops_notify_chat_id');
  const apiBaseUrlField = document.getElementById('production_ops_api_base_url');
  const workerBaseUrlField = document.getElementById('production_ops_worker_base_url');
  const areaField = document.getElementById('production_ops_area');
  const remarkField = document.getElementById('production_ops_remark');
  const approvedCountField = document.getElementById('production_ops_approved_count');
  const autoRecoverField = document.getElementById('production_ops_auto_recover_worker');
  if (enabledField) enabledField.value = config.enabled ? 'true' : 'false';
  if (intervalField) intervalField.value = Number(config.interval_seconds || 20);
  if (notifyField) notifyField.value = config.notify_chat_id || '';
  if (apiBaseUrlField) apiBaseUrlField.value = config.api_base_url || '';
  if (workerBaseUrlField) workerBaseUrlField.value = config.worker_base_url || '';
  if (areaField) areaField.value = config.area || 'Indonesia';
  if (remarkField) remarkField.value = config.remark || '';
  if (approvedCountField) approvedCountField.value = Number(config.approved_count || 1);
  if (autoRecoverField) autoRecoverField.value = config.auto_recover_worker ? 'true' : 'false';
  const runtime = data.runtime || {};
  const checkedAt = runtime.status && runtime.status.checked_at ? runtime.status.checked_at : '暂无';
  const pendingIncidents = Array.isArray((runtime.status || {}).incidents) ? runtime.status.incidents.length : 0;
  const runtimeText = `launchd=${runtime.launch_agent_installed ? 'installed' : 'not_installed'} · 启用=${config.enabled ? 'on' : 'off'} · 最近检查=${checkedAt} · incidents=${pendingIncidents}`;
  const runtimeHint = document.getElementById('productionOpsRuntimeHint');
  const daemonEnabledState = document.getElementById('daemonEnabledState');
  if (runtimeHint) runtimeHint.textContent = runtimeText;
  if (daemonEnabledState) daemonEnabledState.textContent = config.enabled ? 'ON' : 'OFF';
}
async function reloadProductionOpsDaemonConfig() {
  const data = await loadJson('/api/ops/production-ops-daemon');
  applyProductionOpsDaemonConfig(data);
}
async function saveProductionOpsDaemonConfig() {
  const payload = {
    enabled: document.getElementById('production_ops_enabled').value === 'true',
    interval_seconds: Number(document.getElementById('production_ops_interval_seconds').value || 20),
    notify_chat_id: document.getElementById('production_ops_notify_chat_id').value.trim(),
    api_base_url: document.getElementById('production_ops_api_base_url').value.trim(),
    worker_base_url: document.getElementById('production_ops_worker_base_url').value.trim(),
    area: document.getElementById('production_ops_area').value.trim(),
    remark: document.getElementById('production_ops_remark').value.trim(),
    approved_count: Number(document.getElementById('production_ops_approved_count').value || 1),
    auto_recover_worker: document.getElementById('production_ops_auto_recover_worker').value === 'true',
  };
  await loadJson('/api/ops/production-ops-daemon', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  showToast(payload.enabled ? '生产守护已开启并应用' : '生产守护已关闭并应用', 'success');
  await reloadProductionOpsDaemonConfig();
}
function fillExecutorForm(guildName) {
  const rows = Array.isArray(window.__guildExecutors) ? window.__guildExecutors : [];
  const row = rows.find(item => String(item.guild_name || '') === String(guildName || ''));
  if (!row) return;
  document.getElementById('new_executor_guild_name').value = row.guild_name || '';
  document.getElementById('new_executor_backend_url').value = row.backend_url || '';
  document.getElementById('new_executor_login_username').value = row.login_username || '';
  document.getElementById('new_executor_password_secret_ref').value = '';
  document.getElementById('new_executor_proxy_url').value = row.proxy_url || '';
  refreshExecutorProxyRegionSelect(row.proxy_region || '');
  document.getElementById('new_executor_proxy_type').value = row.proxy_type || 'http';
  document.getElementById('new_executor_browser_profile_key').value = row.browser_profile_key || '';
  document.getElementById('new_executor_bind_concurrency').value = row.bind_concurrency || 1;
  document.getElementById('new_executor_request_timeout_seconds').value = row.request_timeout_seconds || 30;
  document.getElementById('new_executor_enabled').value = row.enabled ? 'true' : 'false';
  document.getElementById('new_executor_notes').value = row.notes || '';
  document.getElementById('new_executor_guild_name').scrollIntoView({behavior: 'smooth', block: 'center'});
}
function clearExecutorForm() {
  document.getElementById('new_executor_guild_name').value = '';
  document.getElementById('new_executor_backend_url').value = '';
  document.getElementById('new_executor_login_username').value = '';
  document.getElementById('new_executor_password_secret_ref').value = '';
  document.getElementById('new_executor_proxy_url').value = '';
  refreshExecutorProxyRegionSelect('');
  document.getElementById('new_executor_proxy_type').value = 'http';
  document.getElementById('new_executor_browser_profile_key').value = '';
  document.getElementById('new_executor_bind_concurrency').value = 1;
  document.getElementById('new_executor_request_timeout_seconds').value = 30;
  document.getElementById('new_executor_enabled').value = 'true';
  document.getElementById('new_executor_notes').value = '';
}
async function createOrUpdateExecutor() {
  const guildName = document.getElementById('new_executor_guild_name').value.trim();
  if (!guildName) throw new Error('guild_name is required.');
  const payload = {
    backend_url: document.getElementById('new_executor_backend_url').value.trim(),
    login_username: document.getElementById('new_executor_login_username').value.trim(),
    password_secret_ref: document.getElementById('new_executor_password_secret_ref').value.trim(),
    proxy_url: document.getElementById('new_executor_proxy_url').value.trim(),
    proxy_region: document.getElementById('new_executor_proxy_region').value.trim(),
    proxy_type: document.getElementById('new_executor_proxy_type').value.trim() || 'http',
    browser_profile_key: document.getElementById('new_executor_browser_profile_key').value.trim(),
    bind_concurrency: Number(document.getElementById('new_executor_bind_concurrency').value || 1),
    request_timeout_seconds: Number(document.getElementById('new_executor_request_timeout_seconds').value || 30),
    enabled: document.getElementById('new_executor_enabled').value === 'true',
    notes: document.getElementById('new_executor_notes').value.trim(),
  };
  await loadJson(`/api/ops/guild-executors/${encodeURIComponent(guildName)}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  showToast('保存公会执行器成功', 'success');
  await reloadGuildExecutors();
}
async function deleteExecutor(guildName) {
  const normalized = String(guildName || '').trim();
  if (!normalized) return;
  await loadJson(`/api/ops/guild-executors/${encodeURIComponent(normalized)}`, {
    method: 'DELETE',
  });
  if (document.getElementById('new_executor_guild_name').value.trim() === normalized) {
    clearExecutorForm();
  }
  showToast(`删除公会执行器成功：${normalized}`, 'success');
  await reloadGuildExecutors();
}
async function reloadPresets() {
  const data = await loadJson('/api/ops/intake-bot-presets');
  document.getElementById('presetRows').innerHTML = data.rows.map(
    row => presetRowHtml(row, data.app_options || [], data.guild_options || [], data.app_options_source, data.guild_options_source)
  ).join('');
  renderCreatePresetForm(data);
  document.getElementById('presetCount').textContent = String((data.rows || []).length);
}
async function savePreset(profileName) {
  const robotNameField = document.getElementById(`robot_name_${profileName}`);
  const appField = document.getElementById(`default_app_${profileName}`);
  const guildField = document.getElementById(`default_guild_${profileName}`);
  if (!appField || !guildField || appField.dataset.unavailable === 'true' || guildField.dataset.unavailable === 'true') {
    throw new Error('CRM dropdown options are unavailable. Saving is disabled.');
  }
  const payload = {
    robot_name: robotNameField ? robotNameField.value.trim() : '',
    default_app: appField.value.trim(),
    default_guild: guildField.value.trim(),
  };
  await loadJson(`/api/ops/intake-bot-presets/${profileName}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  showToast('保存成功', 'success');
  await reloadPresets();
}
async function createPreset() {
  const profileName = document.getElementById('new_profile_name').value.trim();
  const robotName = document.getElementById('new_robot_name').value.trim();
  const appId = document.getElementById('new_app_id').value.trim();
  const defaultApp = document.getElementById('new_default_app').value.trim();
  const defaultGuild = document.getElementById('new_default_guild').value.trim();
  if (!profileName) {
    throw new Error('profile_name is required.');
  }
  if (!appId) {
    throw new Error('app_id is required.');
  }
  if (!defaultApp || !defaultGuild) {
    throw new Error('CRM dropdown options are unavailable. Saving is disabled.');
  }
  await loadJson(`/api/ops/intake-bot-presets/${encodeURIComponent(profileName)}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({robot_name: robotName, app_id: appId, default_app: defaultApp, default_guild: defaultGuild}),
  });
  document.getElementById('new_profile_name').value = '';
  document.getElementById('new_robot_name').value = '';
  document.getElementById('new_app_id').value = '';
  showToast('新增机器人成功', 'success');
  await reloadPresets();
}
reloadPresets().catch(err => showToast(err.message, 'error'));
reloadGuildExecutors().catch(err => showToast(err.message, 'error'));
reloadProductionOpsDaemonConfig().catch(err => showToast(err.message, 'error'));
setInterval(() => {
  reloadPresets().catch(err => showToast(err.message, 'error'));
  reloadGuildExecutors().catch(err => showToast(err.message, 'error'));
  reloadProductionOpsDaemonConfig().catch(err => showToast(err.message, 'error'));
}, 15000);
</script>
  </div>
</body>
</html>
"""


PRODUCTION_OPS_PAGE_HTML = """
<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>群审批控制台</title>
  <style>
    :root {
      --bg: #f3f6fb;
      --panel: #ffffff;
      --panel-soft: #f8fbff;
      --line: #dbe4f0;
      --line-strong: #c7d4e3;
      --text: #142033;
      --muted: #5d6b82;
      --brand: #2563eb;
      --brand-soft: #e8f0ff;
      --success: #16a34a;
      --warning: #d97706;
      --danger: #dc2626;
      --shadow: 0 10px 30px rgba(15, 23, 42, 0.06);
      --radius: 18px;
    }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 24px; background: linear-gradient(180deg, #f7faff 0%, var(--bg) 100%); color: var(--text); }
    .page-shell { max-width: 1320px; margin: 0 auto; }
    .shell-nav { position: sticky; top: 0; z-index: 20; display:flex; gap:10px; flex-wrap:wrap; margin: 0 0 18px 0; padding: 12px 0 14px; background: rgba(243,246,251,.92); backdrop-filter: blur(10px); }
    .shell-nav a { color:var(--brand); text-decoration:none; font-size:13px; padding:8px 12px; border-radius:999px; background:#eef4ff; border:1px solid #d8e5ff; }
    .hero { background: linear-gradient(135deg, #ffffff 0%, #f5f9ff 58%, #eef4ff 100%); border:1px solid var(--line); border-radius:24px; padding:24px; box-shadow: var(--shadow); }
    .hero .eyebrow { color:#4f46e5; font-size:12px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; margin-bottom:8px; }
    .hero h1 { margin:0; font-size:30px; line-height:1.15; }
    .hero .subtitle { color:var(--muted); font-size:14px; margin-top:10px; max-width:920px; line-height:1.7; }
    .card { background: var(--panel); border-radius: var(--radius); padding: 18px; box-shadow: var(--shadow); border:1px solid rgba(219,228,240,.95); margin-top: 16px; }
    .card h2 { margin:0 0 14px 0; font-size:20px; }
    .top-overview-grid { display:grid; grid-template-columns: minmax(0, 1.2fr) minmax(360px, .8fr); gap:12px; margin-top:16px; align-items:stretch; }
    .top-overview-grid .card { margin-top:0; padding:16px; }
    .summary-grid { display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap:10px; }
    .summary-item { padding:12px 14px; border-radius:14px; background:linear-gradient(180deg, #ffffff 0%, #f8fbff 100%); border:1px solid var(--line); min-height:72px; }
    .summary-item .label { color:var(--muted); font-size:12px; margin-bottom:8px; }
    .summary-item .value { font-size:22px; font-weight:700; letter-spacing:-0.02em; }
    .status-grid { display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:12px; }
    .status-card { padding:14px; border-radius:16px; background:linear-gradient(180deg, #ffffff 0%, #f8fbff 100%); border:1px solid var(--line); }
    .status-card h3 { margin:0 0 10px 0; font-size:15px; }
    .status-meta { display:grid; grid-template-columns: 92px 1fr; gap:6px 10px; font-size:13px; }
    .status-meta .k { color:var(--muted); }
    .account-grid { display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:14px; }
    .account-card { padding:18px; border-radius:18px; background:linear-gradient(180deg, #ffffff 0%, #f7fbff 100%); border:1px solid var(--line); }
    .account-card h3 { margin:0 0 14px 0; font-size:16px; display:flex; justify-content:space-between; align-items:flex-start; gap:10px; }
    .account-head-left { display:flex; align-items:center; gap:10px; min-width:0; }
    .account-head-title { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .card-monitor-toggle { display:inline-flex; align-items:center; justify-content:center; min-width:88px; padding:6px 12px; border-radius:999px; font-size:12px; line-height:1; border:1px solid transparent; flex-shrink:0; }
    .card-monitor-toggle.enabled { background:#dcfce7; color:#166534; border-color:#86efac; }
    .card-monitor-toggle.disabled { background:#e5e7eb; color:#4b5563; border-color:#cbd5e1; }
    .card-monitor-toggle.pending { background:#dbeafe; color:#1d4ed8; border-color:#93c5fd; cursor:wait; opacity:.92; }
    .status-dot { display:inline-block; width:10px; height:10px; border-radius:999px; margin-right:6px; }
    .status-dot.green { background:var(--success); box-shadow:0 0 0 4px rgba(22,163,74,.12); }
    .status-dot.gray { background:#94a3b8; box-shadow:0 0 0 4px rgba(148,163,184,.15); }
    .status-dot.amber { background:var(--warning); box-shadow:0 0 0 4px rgba(217,119,6,.14); }
    .status-dot.blue { background:var(--brand); box-shadow:0 0 0 4px rgba(37,99,235,.14); }
    .status-dot.red { background:var(--danger); box-shadow:0 0 0 4px rgba(220,38,38,.14); }
    .account-meta { display:grid; grid-template-columns: 104px 1fr; gap:8px 12px; font-size:13px; }
    .account-alert { margin-top:12px; padding:12px 14px; border-radius:12px; border:1px solid #e5e7eb; font-size:13px; }
    .account-alert strong { display:block; margin-bottom:4px; }
    .account-alert.red { background:#fef2f2; border-color:#fecaca; color:#991b1b; }
    .account-alert.amber { background:#fffbeb; border-color:#fcd34d; color:#92400e; }
    .account-alert.blue { background:#eff6ff; border-color:#bfdbfe; color:#1d4ed8; }
    .account-meta .k { color:var(--muted); }
    .link-actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:14px; }
    .binding-list { display:grid; gap:12px; }
    .binding-card { border:1px solid var(--line); border-radius:16px; background:var(--panel-soft); padding:14px; }
    .binding-card.is-empty { background: #fafcff; border-style:dashed; }
    .binding-card-head { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:12px; }
    .binding-title { font-size:14px; font-weight:700; }
    .binding-badge { display:inline-flex; align-items:center; gap:6px; padding:5px 10px; border-radius:999px; background:var(--brand-soft); color:var(--brand); font-size:12px; font-weight:600; }
    .binding-config-grid { display:grid; grid-template-columns: minmax(0, 1.35fr) minmax(180px, .7fr) minmax(180px, .7fr); gap:10px; }
    .binding-meta-grid { display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap:10px; margin-top:10px; }
    .advanced-fields { margin-top:10px; border:1px solid var(--line); border-radius:12px; background:#fbfdff; }
    .advanced-fields summary { cursor:pointer; list-style:none; padding:10px 12px; font-size:12px; color:var(--muted); font-weight:600; }
    .advanced-fields summary::-webkit-details-marker { display:none; }
    .advanced-fields[open] summary { border-bottom:1px solid var(--line); }
    .advanced-fields-body { padding:10px 12px 12px; }
    .advanced-mapping-grid { display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:10px; }
    .binding-subgrid { display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:10px; }
    .schedule-inline-grid { display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap:8px; }
    .inline-list { display:flex; flex-direction:column; gap:10px; }
    .inline-row { display:grid; grid-template-columns: minmax(0,1fr) auto auto; gap:8px; align-items:center; }
    .inline-row button { min-width:88px; }
    .binding-row { grid-template-columns: minmax(0, 1.35fr) minmax(160px, .6fr) auto auto; }
    .toggle-switch { display:inline-flex; align-items:center; gap:10px; padding:6px; border-radius:999px; background:#e5ecf6; border:1px solid var(--line); }
    .toggle-switch button { min-width:72px; border-radius:999px; background:transparent; color:#374151; }
    .toggle-switch button.active { background:var(--brand); color:#fff; box-shadow:0 1px 2px rgba(0,0,0,.12); }
    .toggle-state-label { font-size:12px; color:var(--muted); }
    .executor-form-grid { display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:16px 18px; }
    .field-stack { display:flex; flex-direction:column; gap:6px; }
    .field-hint { color:var(--muted); font-size:12px; }
    .section-split { display:grid; grid-template-columns: minmax(0, 1.45fr) minmax(320px, .75fr); gap:16px; align-items:start; }
    input, select, textarea { padding: 10px 12px; border: 1px solid var(--line-strong); border-radius: 10px; font-size: 14px; width: 100%; box-sizing: border-box; background:#fff; color:var(--text); }
    input:focus, select:focus, textarea:focus { outline:none; border-color:#8db3ff; box-shadow:0 0 0 4px rgba(37,99,235,.12); }
    button { padding: 9px 14px; border-radius: 10px; border: none; background: var(--brand); color: #fff; cursor: pointer; white-space: nowrap; font-weight:600; }
    button.secondary { background: #334155; }
    .muted { color: var(--muted); font-size: 13px; line-height:1.65; }
    .mini-note { margin-top:8px; padding:10px 12px; border-radius:12px; background:#f8fbff; border:1px solid var(--line); }
    .toast { position: fixed; right: 24px; bottom: 24px; min-width: 240px; background: #111827; color: #fff; padding: 12px 14px; border-radius: 12px; display: none; box-shadow:0 12px 28px rgba(15,23,42,.26); }
    .toast.success { background: #065f46; }
    .toast.error { background: #991b1b; }
    .qr-modal { position: fixed; inset: 0; background: rgba(15, 23, 42, .52); display: none; align-items: center; justify-content: center; padding: 24px; z-index: 60; }
    .qr-modal.is-open { display: flex; }
    .qr-modal-card { width: min(760px, 100%); max-height: calc(100vh - 48px); overflow: auto; background: #fff; border-radius: 20px; box-shadow: 0 24px 64px rgba(15,23,42,.24); border: 1px solid rgba(219,228,240,.95); }
    .qr-modal-head { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; padding:18px 20px 14px; border-bottom:1px solid var(--line); }
    .qr-modal-head h3 { margin:0; font-size:18px; }
    .qr-modal-head .muted { margin-top:6px; }
    .qr-modal-body { padding:18px 20px 20px; }
    .qr-modal-status { margin-bottom:14px; }
    .qr-modal-actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:14px; }
    .qr-shell { margin-top:12px; border-radius:16px; background:#0f172a; color:#e2e8f0; padding:16px; overflow:auto; }
    .qr-shell pre { margin:0; font-size:10px; line-height:1.05; }
    .qr-loading { display:flex; align-items:center; gap:10px; color:var(--muted); font-size:13px; }
    .qr-loading::before { content:''; width:16px; height:16px; border-radius:999px; border:2px solid #c7d2fe; border-top-color: var(--brand); animation: qr-spin .9s linear infinite; }
    .button-loading { opacity:.8; cursor:wait; }
    @keyframes qr-spin { to { transform: rotate(360deg); } }
    @media (max-width: 1080px) {
      .top-overview-grid, .status-grid, .account-grid, .section-split, .binding-config-grid, .binding-meta-grid, .schedule-inline-grid, .executor-form-grid { grid-template-columns: 1fr; }
      .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .binding-row { grid-template-columns: 1fr; }
      .account-meta, .status-meta { grid-template-columns: 96px 1fr; }
    }
  </style>
</head>
<body>
  <div class=\"page-shell\">
    <div class=\"shell-nav\">
      <a href=\"/ops\">运营工作台</a>
      <a href=\"/ops/intake-bot-presets\">收口配置中心</a>
      <a href=\"/ops/production-ops\">群审批控制台</a>
      <a href=\"/ops/official-group-bridge\">官方群审批桥接台</a>
    </div>
    <div class=\"hero\">
      <div class=\"eyebrow\">Group Approval Console</div>
      <h1>群审批控制台</h1>

    </div>

    <div class=\"top-overview-grid\">
      <div class=\"card\">
        <h2 style=\"margin-top:0;\">当前状态</h2>
        <div class=\"summary-grid\">
          <div class=\"summary-item\"><div class=\"label\">守护状态</div><div class=\"value\" id=\"daemonEnabledState\">-</div></div>
          <div class=\"summary-item\"><div class=\"label\">审批账号</div><div class=\"value\" id=\"waAccountCount\">-</div></div>
          <div class=\"summary-item\"><div class=\"label\">运行中</div><div class=\"value\" id=\"waEnabledAccountCount\">-</div></div>
          <div class=\"summary-item\"><div class=\"label\">官方群待处理</div><div class=\"value\" id=\"officialPendingCount\">-</div></div>
        </div>
        <div class=\"muted\" id=\"productionOpsRuntimeHint\" style=\"margin-top:10px;\"></div>
      </div>

      <div class=\"card\">
        <h2 style=\"margin-top:0;\">官方群总览</h2>
        <div class=\"status-card\">
          <div class=\"status-meta\" id=\"officialBridgeSummaryMeta\"></div>
        </div>
      </div>
    </div>

    <div class=\"card\">
      <h2 style=\"margin-top:0;\">WhatsApp 审批账号</h2>
      <div id=\"approvalAccountRows\" class=\"account-grid\"></div>
    </div>

    <div class="card">
      <h2 style="margin-top:0;">新增 / 更新 WhatsApp 审批账号</h2>
      <div class="section-split">
        <div class="field-stack">
          <div class="executor-form-grid">
            <input type="hidden" id="wa_account_key" />
            <div class="field-stack">
              <label class="field-hint">账号名称（account_name）</label>
              <input id="wa_account_name" placeholder="例如 WA Admin 1" />
            </div>
            <div class="field-stack">
              <label class="field-hint">负责类型（responsible_type）</label>
              <select id="wa_responsible_type"><option value="registration_group">注册群</option><option value="official_group">官方群</option></select>
            </div>
            <input type="hidden" id="wa_enabled" value="true" />
            <div class="field-stack">
              <label class="field-hint">备注（notes）</label>
              <input id="wa_notes" placeholder="例如负责印尼注册群审批" />
            </div>
          </div>

          <div class="field-stack" style="margin-top:14px;">
            <label class="field-hint">逐群绑定配置（最多3组）</label>
            <div class="binding-list">
              <div class="binding-card" id="wa_binding_card_1">
                <div class="binding-card-head">
                  <div class="binding-title">第 1 组群绑定</div>
                  <span class="binding-badge">逐群独立配置</span>
                </div>
                <div class="binding-config-grid">
                  <div class="field-stack">
                    <label class="field-hint">群链接</label>
                    <input id="wa_group_link_1" placeholder="https://chat.whatsapp.com/xxx" />
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">群名称（选填）</label>
                    <input id="wa_group_name_1" placeholder="例如 印尼注册群 A" />
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">地区</label>
                    <select id="wa_group_area_1"></select>
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">通知机器人</label>
                    <select id="wa_group_notify_profile_name_1"></select>
                  </div>
                </div>
                <div class="binding-meta-grid">
                  <div class="field-stack">
                    <label class="field-hint">本群监控</label>
                    <select id="wa_group_enabled_1"><option value="true">监控中</option><option value="false">不监控</option></select>
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">审批人数阈值</label>
                    <input id="wa_group_approval_count_threshold_1" type="number" min="1" step="1" value="30" placeholder="例如 25 / 28 / 100" />
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">审批超时分钟</label>
                    <input id="wa_group_approval_timeout_minutes_1" type="number" min="1" step="1" value="30" placeholder="例如 30 / 45 / 90" />
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">自动恢复 worker</label>
                    <select id="wa_group_auto_recover_worker_1"><option value="true">开启</option><option value="false">关闭</option></select>
                  </div>
                </div>
                <details class="advanced-fields">
                  <summary>高级项</summary>
                  <div class="advanced-fields-body">
                    <div class="advanced-mapping-grid">
                      <div class="field-stack">
                        <label class="field-hint">系统映射 registration_group（可留空）</label>
                        <input id="wa_group_registration_group_1" placeholder="可留空；系统默认按实时探针推断" />
                      </div>
                      <div class="field-stack">
                        <label class="field-hint">系统映射 group_id（可留空）</label>
                        <input id="wa_group_group_id_1" placeholder="可留空；拿不到时不要手填" />
                      </div>
                    </div>
                  </div>
                </details>
                <div class="field-stack" style="margin-top:10px;">
                  <label class="field-hint">监控时间段（最多3个）</label>
                  <div class="schedule-inline-grid">
                    <input id="wa_group_schedule_window_1_1" placeholder="09:00-12:00" />
                    <input id="wa_group_schedule_window_1_2" placeholder="14:00-18:00" />
                    <input id="wa_group_schedule_window_1_3" placeholder="20:00-22:00" />
                  </div>
                </div>
              </div>

              <div class="binding-card" id="wa_binding_card_2">
                <div class="binding-card-head">
                  <div class="binding-title">第 2 组群绑定</div>
                  <span class="binding-badge">逐群独立配置</span>
                </div>
                <div class="binding-config-grid">
                  <div class="field-stack">
                    <label class="field-hint">群链接</label>
                    <input id="wa_group_link_2" placeholder="https://chat.whatsapp.com/yyy" />
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">群名称（选填）</label>
                    <input id="wa_group_name_2" placeholder="例如 印尼注册群 B" />
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">地区</label>
                    <select id="wa_group_area_2"></select>
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">通知机器人</label>
                    <select id="wa_group_notify_profile_name_2"></select>
                  </div>
                </div>
                <div class="binding-meta-grid">
                  <div class="field-stack">
                    <label class="field-hint">本群监控</label>
                    <select id="wa_group_enabled_2"><option value="true">监控中</option><option value="false">不监控</option></select>
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">审批人数阈值</label>
                    <input id="wa_group_approval_count_threshold_2" type="number" min="1" step="1" value="30" placeholder="例如 25 / 28 / 100" />
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">审批超时分钟</label>
                    <input id="wa_group_approval_timeout_minutes_2" type="number" min="1" step="1" value="30" placeholder="例如 30 / 45 / 90" />
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">自动恢复 worker</label>
                    <select id="wa_group_auto_recover_worker_2"><option value="true">开启</option><option value="false">关闭</option></select>
                  </div>
                </div>
                <details class="advanced-fields">
                  <summary>高级项</summary>
                  <div class="advanced-fields-body">
                    <div class="advanced-mapping-grid">
                      <div class="field-stack">
                        <label class="field-hint">系统映射 registration_group（可留空）</label>
                        <input id="wa_group_registration_group_2" placeholder="可留空；系统默认按实时探针推断" />
                      </div>
                      <div class="field-stack">
                        <label class="field-hint">系统映射 group_id（可留空）</label>
                        <input id="wa_group_group_id_2" placeholder="可留空；拿不到时不要手填" />
                      </div>
                    </div>
                  </div>
                </details>
                <div class="field-stack" style="margin-top:10px;">
                  <label class="field-hint">监控时间段（最多3个）</label>
                  <div class="schedule-inline-grid">
                    <input id="wa_group_schedule_window_2_1" placeholder="09:00-12:00" />
                    <input id="wa_group_schedule_window_2_2" placeholder="14:00-18:00" />
                    <input id="wa_group_schedule_window_2_3" placeholder="20:00-22:00" />
                  </div>
                </div>
              </div>

              <div class="binding-card" id="wa_binding_card_3">
                <div class="binding-card-head">
                  <div class="binding-title">第 3 组群绑定</div>
                  <span class="binding-badge">逐群独立配置</span>
                </div>
                <div class="binding-config-grid">
                  <div class="field-stack">
                    <label class="field-hint">群链接</label>
                    <input id="wa_group_link_3" placeholder="https://chat.whatsapp.com/zzz" />
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">群名称（选填）</label>
                    <input id="wa_group_name_3" placeholder="例如 印尼注册群 C" />
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">地区</label>
                    <select id="wa_group_area_3"></select>
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">通知机器人</label>
                    <select id="wa_group_notify_profile_name_3"></select>
                  </div>
                </div>
                <div class="binding-meta-grid">
                  <div class="field-stack">
                    <label class="field-hint">本群监控</label>
                    <select id="wa_group_enabled_3"><option value="true">监控中</option><option value="false">不监控</option></select>
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">审批人数阈值</label>
                    <input id="wa_group_approval_count_threshold_3" type="number" min="1" step="1" value="30" placeholder="例如 25 / 28 / 100" />
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">审批超时分钟</label>
                    <input id="wa_group_approval_timeout_minutes_3" type="number" min="1" step="1" value="30" placeholder="例如 30 / 45 / 90" />
                  </div>
                  <div class="field-stack">
                    <label class="field-hint">自动恢复 worker</label>
                    <select id="wa_group_auto_recover_worker_3"><option value="true">开启</option><option value="false">关闭</option></select>
                  </div>
                </div>
                <details class="advanced-fields">
                  <summary>高级项</summary>
                  <div class="advanced-fields-body">
                    <div class="advanced-mapping-grid">
                      <div class="field-stack">
                        <label class="field-hint">系统映射 registration_group（可留空）</label>
                        <input id="wa_group_registration_group_3" placeholder="可留空；系统默认按实时探针推断" />
                      </div>
                      <div class="field-stack">
                        <label class="field-hint">系统映射 group_id（可留空）</label>
                        <input id="wa_group_group_id_3" placeholder="可留空；拿不到时不要手填" />
                      </div>
                    </div>
                  </div>
                </details>
                <div class="field-stack" style="margin-top:10px;">
                  <label class="field-hint">监控时间段（最多3个）</label>
                  <div class="schedule-inline-grid">
                    <input id="wa_group_schedule_window_3_1" placeholder="09:00-12:00" />
                    <input id="wa_group_schedule_window_3_2" placeholder="14:00-18:00" />
                    <input id="wa_group_schedule_window_3_3" placeholder="20:00-22:00" />
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div style="margin-top:14px; display:flex; gap:8px;">
            <button type="button" onclick="saveApprovalAccount()">保存审批账号</button>
            <button type="button" class="secondary" onclick="clearApprovalAccountForm()">清空表单</button>
          </div>
        </div>

      </div>
    </div>

    <div class=\"card\">
      <h2 style=\"margin-top:0;\">地区选项源</h2>
      <div class=\"field-stack\" style=\"margin-top:12px;\">
        <label class=\"field-hint\">地区选项（每行一个）</label>
        <textarea id=\"area_options_text\" rows=\"5\" placeholder=\"Indonesia\\nBrazil\\nMexico\"></textarea>
      </div>
      <div style=\"margin-top:12px; display:flex; gap:8px;\">
        <button type=\"button\" onclick=\"saveAreaOptions()\">保存地区选项</button>
      </div>
    </div>

    <div class=\"card\">
      <h2 style=\"margin-top:0;\">日志与状态入口</h2>
      <div class=\"muted\" id=\"productionOpsPathsHint\" style=\"margin-top:12px;\">状态文件路径加载中…</div>
    </div>



    <div id=\"productionOpsToast\" class=\"toast\"></div>
    <div id=\"approvalQrModal\" class=\"qr-modal\" onclick=\"dismissApprovalQrModal(event)\">
      <div class=\"qr-modal-card\" role=\"dialog\" aria-modal=\"true\" aria-labelledby=\"approvalQrModalTitle\" onclick=\"event.stopPropagation()\">
        <div class=\"qr-modal-head\">
          <div>
            <h3 id=\"approvalQrModalTitle\">账号激活二维码</h3>
            <div class=\"muted\" id=\"approvalQrModalSubtitle\">请使用对应 WhatsApp 账号扫码。</div>
          </div>
          <button type=\"button\" class=\"secondary\" onclick=\"closeApprovalQrModal()\">关闭</button>
        </div>
        <div class=\"qr-modal-body\">
          <div id=\"approvalQrModalStatus\" class=\"qr-modal-status muted\">正在准备二维码…</div>
          <div id=\"approvalQrModalContent\"></div>
          <div class=\"qr-modal-actions\">
            <button type=\"button\" onclick=\"retryApprovalQrModal()\">重新生成二维码</button>
            <button type=\"button\" class=\"secondary\" onclick=\"refreshApprovalQrModal()\">刷新状态</button>
          </div>
        </div>
      </div>
    </div>
  </div>
<script>
async function loadJson(url, options) {
  const res = await fetch(url, options || {});
  if (!res.ok) {
    let detail = '';
    try {
      const data = await res.json();
      detail = data.detail || JSON.stringify(data);
    } catch (_) {}
    throw new Error(detail || `Failed to load ${url}: ${res.status}`);
  }
  return await res.json();
}
function showToast(message, type='success') {
  const toast = document.getElementById('productionOpsToast');
  toast.textContent = message;
  toast.className = `toast ${type}`;
  toast.style.display = 'block';
  clearTimeout(window.__productionOpsToastTimer);
  window.__productionOpsToastTimer = setTimeout(() => { toast.style.display = 'none'; }, 2400);
}
function accountDisplayName(accountKey) {
  const normalized = String(accountKey || '').trim();
  const rows = Array.isArray(window.__approvalAccounts) ? window.__approvalAccounts : [];
  const row = rows.find(item => String(item.account_key || '').trim() === normalized);
  return row?.account_name || normalized || '该账号';
}
function mergeApprovalSessionState(previousState, nextState) {
  const previous = previousState && typeof previousState === 'object' ? previousState : {};
  const next = nextState && typeof nextState === 'object' ? nextState : {};
  const merged = {...previous, ...next};
  if (!next.qr_ascii && previous.qr_ascii) merged.qr_ascii = previous.qr_ascii;
  if (!next.qr_text && previous.qr_text) merged.qr_text = previous.qr_text;
  if (!next.last_qr_at && previous.last_qr_at) merged.last_qr_at = previous.last_qr_at;
  if (!next.auth_path && previous.auth_path) merged.auth_path = previous.auth_path;
  if (!next.client_id && previous.client_id) merged.client_id = previous.client_id;
  return merged;
}
function closeApprovalQrModal() {
  clearTimeout(window.__approvalQrModalRefreshTimer);
  window.__approvalQrModalRefreshTimer = null;
  const closingAccountKey = String(window.__approvalQrModalState?.accountKey || '').trim();
  if (closingAccountKey && window.__approvalSessionStateByAccount && window.__approvalSessionStateByAccount[closingAccountKey]) {
    window.__approvalSessionStateByAccount[closingAccountKey] = {
      ...window.__approvalSessionStateByAccount[closingAccountKey],
      qr_ascii: null,
      qr_text: null,
      qr_available: false,
    };
    renderApprovalAccountRows();
  }
  window.__approvalQrModalState = {
    ...(window.__approvalQrModalState || {}),
    open: false,
    loading: false,
  };
  const modal = document.getElementById('approvalQrModal');
  if (modal) modal.classList.remove('is-open');
}
function dismissApprovalQrModal(event) {
  if (event && event.target && event.target.id === 'approvalQrModal') closeApprovalQrModal();
}
function scheduleApprovalQrModalRefresh() {
  clearTimeout(window.__approvalQrModalRefreshTimer);
  window.__approvalQrModalRefreshTimer = null;
  const state = window.__approvalQrModalState || {};
  const sessionState = state.sessionState && typeof state.sessionState === 'object' ? state.sessionState : {};
  if (!state.open || state.loading || state.error || sessionState.login_verified) return;
  window.__approvalQrModalRefreshTimer = setTimeout(() => {
    refreshApprovalQrModal().catch(err => {
      openApprovalQrModal(state.accountKey, {loading: false, error: err.message || '刷新状态失败'});
    });
  }, 3000);
}
function renderApprovalQrModal() {
  const modal = document.getElementById('approvalQrModal');
  const statusEl = document.getElementById('approvalQrModalStatus');
  const contentEl = document.getElementById('approvalQrModalContent');
  const titleEl = document.getElementById('approvalQrModalTitle');
  const subtitleEl = document.getElementById('approvalQrModalSubtitle');
  if (!modal || !statusEl || !contentEl || !titleEl || !subtitleEl) return;
  const state = window.__approvalQrModalState || {};
  if (!state.open || !state.accountKey) {
    clearTimeout(window.__approvalQrModalRefreshTimer);
    window.__approvalQrModalRefreshTimer = null;
    modal.classList.remove('is-open');
    return;
  }
  const accountName = accountDisplayName(state.accountKey);
  const sessionState = state.sessionState && typeof state.sessionState === 'object' ? state.sessionState : {};
  const loading = Boolean(state.loading);
  const error = String(state.error || '').trim();
  titleEl.textContent = `${accountName} · 激活二维码`;
  subtitleEl.textContent = '请使用这个 WhatsApp 账号，在“关联设备”里扫码。';
  if (loading) {
    statusEl.innerHTML = '<div class="qr-loading">正在启动扫码服务并生成二维码，请保持这个弹窗打开。</div>';
  } else if (error) {
    statusEl.innerHTML = `<span style="color:#b91c1c;">${error}</span>`;
  } else if (sessionState.login_verified) {
    statusEl.innerHTML = '<span style="color:#166534;">已完成登录检测：账号已登录，可以正常使用。</span>';
    if (!state.successAnnounced) {
      window.__approvalQrModalState = {...state, successAnnounced: true};
      showToast('账号已登录，可以正常使用', 'success');
    }
  } else if (sessionState.qr_available) {
    const suffix = sessionState.last_qr_at ? ` · 最近出码：${sessionState.last_qr_at}` : '';
    statusEl.innerHTML = `<span style="color:#1d4ed8;">二维码已准备好，请尽快扫码${suffix}</span>`;
  } else {
    statusEl.textContent = '正在等待二维码返回…';
  }
  if (sessionState.qr_ascii) {
    const safeQr = String(sessionState.qr_ascii || '').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    contentEl.innerHTML = `<div class="field-hint">绑定二维码</div><div class="qr-shell"><pre>${safeQr}</pre></div><div class="muted" style="margin-top:10px;">扫码后系统会自动做一次登录检测，通过后会提示“账号已登录，可以正常使用”。</div>`;
  } else if (loading) {
    contentEl.innerHTML = '<div class="mini-note">正在生成二维码，通常几秒内返回；若首次拉起 runtime 稍慢，也会继续在这个弹窗内刷新结果。</div>';
  } else if (sessionState.login_verified) {
    contentEl.innerHTML = `<div class="mini-note">${sessionState.login_check_message || '账号已登录，可以正常使用。'}</div>`;
  } else {
    contentEl.innerHTML = '<div class="mini-note">暂未拿到二维码。你可以点下方“刷新状态”继续等待，或“重新生成二维码”重新拉起。</div>';
  }
  modal.classList.add('is-open');
  scheduleApprovalQrModalRefresh();
}
function openApprovalQrModal(accountKey, options = {}) {
  const normalized = String(accountKey || '').trim();
  if (!normalized) return;
  const currentState = window.__approvalQrModalState || {};
  const sessionSource = options.sessionState || (window.__approvalSessionStateByAccount && window.__approvalSessionStateByAccount[normalized]) || {};
  const mergedSessionState = mergeApprovalSessionState(currentState.sessionState, sessionSource);
  const successAnnounced = options.resetSuccessAnnounced ? false : Boolean(options.successAnnounced ?? (currentState.accountKey === normalized ? currentState.successAnnounced : false));
  window.__approvalQrModalState = {
    open: true,
    accountKey: normalized,
    sessionState: mergedSessionState,
    loading: Boolean(options.loading),
    error: options.error || '',
    successAnnounced,
  };
  renderApprovalQrModal();
}
async function refreshApprovalQrModal() {
  const accountKey = String(window.__approvalQrModalState?.accountKey || '').trim();
  if (!accountKey) return;
  await refreshApprovalAccountSession(accountKey, {silent: true, keepModal: true});
}
async function retryApprovalQrModal() {
  const accountKey = String(window.__approvalQrModalState?.accountKey || '').trim();
  if (!accountKey) return;
  await startApprovalAccountSession(accountKey, {fromModal: true});
}
function renderStatusMeta(elId, rows) {
  const el = document.getElementById(elId);
  if (!el) return;
  const safeRows = Array.isArray(rows) ? rows : [];
  el.innerHTML = safeRows.map(row => `<div class="k">${row[0]}</div><div>${row[1]}</div>`).join('');
}
function toggleSwitch(inputId, enabled, wrapperId) {
  const input = document.getElementById(inputId);
  if (input) input.value = enabled ? 'true' : 'false';
  const wrapper = document.getElementById(wrapperId);
  if (wrapper) {
    Array.from(wrapper.querySelectorAll('button')).forEach((button, index) => {
      const shouldActive = (enabled && index === 0) || (!enabled && index === 1);
      button.classList.toggle('active', shouldActive);
    });
  }
  const stateLabel = document.getElementById(`${inputId}_state`);
  if (stateLabel) stateLabel.textContent = `当前：${enabled ? '开启' : '关闭'}`;
}
function collectIndexedValues(prefix, count) {
  const values = [];
  for (let i = 1; i <= count; i += 1) {
    const el = document.getElementById(`${prefix}${i}`);
    const value = String(el?.value || '').trim();
    if (value) values.push(value);
  }
  return values;
}
function fillIndexedValues(prefix, values, count) {
  const safeValues = Array.isArray(values) ? values : [];
  for (let i = 1; i <= count; i += 1) {
    const el = document.getElementById(`${prefix}${i}`);
    if (el) el.value = safeValues[i - 1] || '';
  }
}
function clearInlineField(inputId, label) {
  const input = document.getElementById(inputId);
  if (!input) return;
  input.value = '';
  showToast(`${label}已删除`, 'success');
}
function saveInlineField(label, inputId) {
  const input = document.getElementById(inputId);
  if (!input) return;
  const value = String(input.value || '').trim();
  if (!value) throw new Error(`${label}不能为空`);
  input.value = value;
  showToast(`${label}已保存到表单`, 'success');
}
function clearInlineGroupBinding(index) {
  const input = document.getElementById(`wa_group_link_${index}`);
  const areaSelect = document.getElementById(`wa_group_area_${index}`);
  if (input) input.value = '';
  if (areaSelect) areaSelect.value = '';
  showToast(`第${index}组群链接已删除`, 'success');
}
function saveInlineGroupBinding(index) {
  const input = document.getElementById(`wa_group_link_${index}`);
  const areaSelect = document.getElementById(`wa_group_area_${index}`);
  const link = String(input?.value || '').trim();
  const area = String(areaSelect?.value || '').trim();
  if (!link) throw new Error(`第${index}组群链接不能为空`);
  if (!area) throw new Error(`第${index}组群链接必须选择地区`);
  if (input) input.value = link;
  showToast(`第${index}组群链接和地区已保存到表单`, 'success');
}
function slugifyAccountKey(text) {
  const raw = String(text || '').trim().toLowerCase();
  const normalized = raw.replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  return normalized || `wa-account-${Date.now()}`;
}
function ensureApprovalAccountKey() {
  const keyField = document.getElementById('wa_account_key');
  const current = String(keyField?.value || '').trim();
  if (current) return current;
  const name = document.getElementById('wa_account_name').value;
  const type = document.getElementById('wa_responsible_type').value === 'official_group' ? 'official' : 'registration';
  const generated = `${type}-${slugifyAccountKey(name)}`;
  if (keyField) keyField.value = generated;
  return generated;
}
function renderNotifyRobotSelect(options, currentValue='', elementId='wa_group_notify_profile_name_1') {
  const select = document.getElementById(elementId);
  if (!select) return;
  const rows = Array.isArray(options) ? options : [];
  const normalizedCurrent = String(currentValue || '').trim();
  if (!rows.length) {
    select.innerHTML = '<option value="">暂无可用机器人</option>';
    return;
  }
  const normalizedRows = rows.map(item => ({
    value: String(item.profile_name || '').trim(),
    label: String(item.label || item.robot_name || item.profile_name || '').trim(),
  })).filter(item => item.value);
  const fallbackValue = normalizedRows[0]?.value || '';
  const effectiveCurrent = normalizedRows.some(item => item.value === normalizedCurrent)
    ? normalizedCurrent
    : fallbackValue;
  select.innerHTML = ['<option value="">请选择通知机器人</option>', ...normalizedRows.map(item => {
    const selected = item.value === effectiveCurrent ? ' selected' : '';
    return `<option value="${item.value}"${selected}>${item.label}</option>`;
  })].join('');
  if (effectiveCurrent) {
    select.value = effectiveCurrent;
  }
}
function renderAllGroupNotifyRobotSelects(options, currentValues=[]) {
  const safeValues = Array.isArray(currentValues) ? currentValues : [];
  for (let i = 1; i <= 3; i += 1) {
    renderNotifyRobotSelect(options, safeValues[i - 1] || '', `wa_group_notify_profile_name_${i}`);
  }
}
function renderAreaSelect(options, currentValue='', elementId='wa_group_area_1') {
  const select = document.getElementById(elementId);
  if (!select) return;
  const rows = Array.isArray(options) ? options : [];
  const normalizedCurrent = String(currentValue || '').trim();
  if (!rows.length) {
    select.innerHTML = '<option value="">暂无地区选项</option>';
    return;
  }
  const normalizedRows = rows.map(item => ({
    value: String(item.value || item.label || '').trim(),
    label: String(item.label || item.value || '').trim(),
  })).filter(item => item.value);
  if (normalizedCurrent && !normalizedRows.some(item => item.value === normalizedCurrent)) {
    normalizedRows.push({value: normalizedCurrent, label: normalizedCurrent});
  }
  select.innerHTML = ['<option value="">请选择地区</option>', ...normalizedRows.map(item => {
    const selected = item.value === normalizedCurrent ? ' selected' : '';
    return `<option value="${item.value}"${selected}>${item.label}</option>`;
  })].join('');
  if (normalizedCurrent) {
    select.value = normalizedCurrent;
  }
}
function renderAllGroupAreaSelects(options, currentValues=[]) {
  const safeValues = Array.isArray(currentValues) ? currentValues : [];
  for (let i = 1; i <= 3; i += 1) {
    renderAreaSelect(options, safeValues[i - 1] || '', `wa_group_area_${i}`);
  }
}
function collectGroupScheduleWindows(groupIndex) {
  const rows = [];
  for (let i = 1; i <= 3; i += 1) {
    const value = String(document.getElementById(`wa_group_schedule_window_${groupIndex}_${i}`)?.value || '').trim();
    if (!value) continue;
    const parts = value.split('-').map(item => item.trim());
    rows.push({start: parts[0] || '', end: parts[1] || ''});
  }
  return rows;
}
function fillGroupScheduleWindows(groupIndex, values) {
  const safeValues = Array.isArray(values) ? values : [];
  for (let i = 1; i <= 3; i += 1) {
    const input = document.getElementById(`wa_group_schedule_window_${groupIndex}_${i}`);
    if (!input) continue;
    const row = safeValues[i - 1] || {};
    const start = String(row.start || '').trim();
    const end = String(row.end || '').trim();
    input.value = start && end ? `${start}-${end}` : '';
  }
}
function collectGroupBindings(count) {
  const rows = [];
  for (let i = 1; i <= count; i += 1) {
    const link = String(document.getElementById(`wa_group_link_${i}`)?.value || '').trim();
    const groupName = String(document.getElementById(`wa_group_name_${i}`)?.value || '').trim();
    const area = String(document.getElementById(`wa_group_area_${i}`)?.value || '').trim();
    const notifyProfileName = String(document.getElementById(`wa_group_notify_profile_name_${i}`)?.value || '').trim();
    const enabled = document.getElementById(`wa_group_enabled_${i}`)?.value !== 'false';
    const registrationGroup = String(document.getElementById(`wa_group_registration_group_${i}`)?.value || '').trim();
    const groupId = String(document.getElementById(`wa_group_group_id_${i}`)?.value || '').trim();
    const approvalCountThreshold = Number(document.getElementById(`wa_group_approval_count_threshold_${i}`)?.value || 0);
    const approvalTimeoutMinutes = Number(document.getElementById(`wa_group_approval_timeout_minutes_${i}`)?.value || 0);
    const autoRecoverWorker = document.getElementById(`wa_group_auto_recover_worker_${i}`)?.value === 'true';
    const scheduleWindows = collectGroupScheduleWindows(i);
    if (!link && !groupName && !area && !notifyProfileName && !registrationGroup && !groupId && !scheduleWindows.length) continue;
    if (link && !area) throw new Error(`第${i}组群链接必须选择地区后才能保存`);
    if (!link && area) throw new Error(`第${i}组地区已选择，但缺少群链接`);
    if (link && !notifyProfileName) throw new Error(`第${i}组群链接必须选择通知机器人后才能保存`);
    rows.push({
      link,
      group_name: groupName,
      area,
      notify_profile_name: notifyProfileName,
      enabled,
      registration_group: registrationGroup,
      group_id: groupId,
      approval_count_threshold: approvalCountThreshold,
      approval_timeout_minutes: approvalTimeoutMinutes,
      auto_recover_worker: autoRecoverWorker,
      schedule_windows: scheduleWindows,
    });
  }
  return rows;
}
async function reloadAreaOptions(currentValues=null) {
  const effectiveCurrentValues = Array.isArray(currentValues) && currentValues.length
    ? currentValues
    : [1, 2, 3].map(index => String(document.getElementById(`wa_group_area_${index}`)?.value || '').trim());
  const data = await loadJson('/api/ops/whatsapp-approval-area-options');
  window.__approvalAreaOptions = Array.isArray(data.options) ? data.options : [];
  renderAllGroupAreaSelects(window.__approvalAreaOptions, effectiveCurrentValues);
  const sourceOptions = Array.isArray(data.source_options) && data.source_options.length ? data.source_options : window.__approvalAreaOptions;
  const text = sourceOptions.map(item => String(item.value || item.label || '').trim()).filter(Boolean).join('\\n');
  const textarea = document.getElementById('area_options_text');
  if (textarea) textarea.value = text;
}
async function saveAreaOptions() {
  const textarea = document.getElementById('area_options_text');
  const options = String(textarea?.value || '').split('\\n').map(item => item.trim()).filter(Boolean);
  const data = await loadJson('/api/ops/whatsapp-approval-area-options', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({options}),
  });
  window.__approvalAreaOptions = Array.isArray(data.options) ? data.options : [];
  renderAllGroupAreaSelects(window.__approvalAreaOptions, [1, 2, 3].map(index => String(document.getElementById(`wa_group_area_${index}`)?.value || '').trim()));
  const sourceOptions = Array.isArray(data.source_options) && data.source_options.length ? data.source_options : window.__approvalAreaOptions;
  if (textarea) textarea.value = sourceOptions.map(item => String(item.value || item.label || '').trim()).filter(Boolean).join('\\n');
  showToast('保存地区选项成功', 'success');
}
function renderBindingCardState(index, binding) {
  const card = document.getElementById(`wa_binding_card_${index}`);
  if (!card) return;
  const hasLink = Boolean(String(binding?.link || '').trim());
  card.classList.toggle('is-empty', !hasLink);
}
function formatVerifierFrameworkStatus(status) {
  const normalized = String(status || '').trim();
  if (normalized === 'live_probe_ready') return '已接入群状态探针';
  if (normalized === 'seed_required') return '待补齐';
  if (normalized === 'unavailable') return '暂不可用';
  return normalized || '-';
}
function formatOfficialBridgeMode(mode) {
  const normalized = String(mode || '').trim();
  if (normalized === 'manual_queue') return '人工队列';
  return normalized || '-';
}
function formatOfficialBridgeHealthStatus(status) {
  const normalized = String(status || '').trim();
  if (normalized === 'healthy') return '正常';
  if (normalized === 'unreachable') return '不可达';
  if (normalized === 'degraded') return '异常';
  if (normalized === 'error') return '异常';
  return normalized || '-';
}
function bindingVerifierReadinessText(verifier) {
  return verifier?.ready ? '已就绪' : '待校验';
}
function bindingVerifierStatusText(verifier) {
  const status = String(verifier?.status || '').trim();
  if (status === 'live_probe_ready') return '已接入群状态探针';
  if (status === 'mapped_live_probe_ready') return '已命中当前探针';
  if (status === 'inferred_live_probe_ready') return '按当前探针推断';
  if (status === 'monitor_disabled') return '未开启本群监控';
  if (status === 'other_binding_live_probe_active') return '等待切到本群探针';
  if (status === 'mapping_mismatch') return '映射与当前探针不一致';
  if (status === 'runtime_unavailable') return '独立扫码服务未就绪';
  if (status === 'login_unready') return '账号未完成登录校验';
  if (status === 'binding_target_missing') return '缺少群目标标识';
  if (status === 'probe_unavailable') return '当前探针不可用';
  return status || '-';
}
function escapeHtmlAttr(value) {
  return String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
function formatApprovalCountdownClock(totalSeconds) {
  const normalized = Math.max(Number(totalSeconds) || 0, 0);
  const hours = Math.floor(normalized / 3600);
  const minutes = Math.floor((normalized % 3600) / 60);
  const seconds = normalized % 60;
  return [hours, minutes, seconds].map(item => String(item).padStart(2, '0')).join(':');
}
function formatApprovalCountdownText(binding) {
  const pendingCount = Number(binding?.next_approval_pending_count || 0);
  const ready = Boolean(binding?.next_approval_ready);
  const remainingSecondsValue = binding?.next_approval_remaining_seconds;
  const hasRemainingSeconds = remainingSecondsValue !== null && remainingSecondsValue !== undefined && remainingSecondsValue !== '';
  const remainingSeconds = hasRemainingSeconds ? Math.max(Number(remainingSecondsValue) || 0, 0) : null;
  const timeoutMinutes = Number(binding?.next_approval_timeout_minutes || 0);
  const oldestPendingAt = String(binding?.next_approval_oldest_pending_at || '').trim();
  const fallbackText = String(binding?.next_approval_eta_text || '').trim() || '暂无待审批';
  if (ready) return '00:00:00';
  if (remainingSeconds !== null) return formatApprovalCountdownClock(remainingSeconds);
  if (!oldestPendingAt || timeoutMinutes <= 0) return pendingCount <= 0 ? '—' : fallbackText;
  const oldestMs = Date.parse(oldestPendingAt);
  if (!Number.isFinite(oldestMs)) return pendingCount <= 0 ? '—' : fallbackText;
  const computedRemainingSeconds = Math.max(Math.ceil(((oldestMs + timeoutMinutes * 60 * 1000) - Date.now()) / 1000), 0);
  return formatApprovalCountdownClock(computedRemainingSeconds);
}
function refreshApprovalCountdownNodes() {
  document.querySelectorAll('[data-next-approval-countdown]').forEach(node => {
    const pendingCount = Number(node.dataset.nextApprovalPendingCount || 0);
    const ready = String(node.dataset.nextApprovalReady || '') === '1';
    const remainingSecondsText = String(node.dataset.nextApprovalRemainingSeconds || '').trim();
    const remainingSeconds = remainingSecondsText === '' ? null : Number(remainingSecondsText);
    const renderedAtText = String(node.dataset.nextApprovalRenderedAtMs || '').trim();
    const renderedAtMs = renderedAtText === '' ? null : Number(renderedAtText);
    const elapsedSeconds = Number.isFinite(renderedAtMs) ? Math.max(Math.floor((Date.now() - renderedAtMs) / 1000), 0) : 0;
    const adjustedRemainingSeconds = Number.isFinite(remainingSeconds) ? Math.max(remainingSeconds - elapsedSeconds, 0) : null;
    const timeoutMinutes = Number(node.dataset.nextApprovalTimeoutMinutes || 0);
    const oldestPendingAt = String(node.dataset.nextApprovalOldestPendingAt || '').trim();
    const fallbackText = String(node.dataset.nextApprovalFallbackText || '').trim() || '暂无待审批';
    node.textContent = formatApprovalCountdownText({
      next_approval_pending_count: pendingCount,
      next_approval_ready: ready,
      next_approval_remaining_seconds: adjustedRemainingSeconds,
      next_approval_timeout_minutes: timeoutMinutes,
      next_approval_oldest_pending_at: oldestPendingAt,
      next_approval_eta_text: fallbackText,
    });
  });
}
function startApprovalCountdownTicker() {
  if (window.__approvalCountdownTickerStarted) return;
  window.__approvalCountdownTickerStarted = true;
  refreshApprovalCountdownNodes();
  window.setInterval(refreshApprovalCountdownNodes, 1000);
}
function bindingSummaryHtml(binding, row, bindingIndex) {
  const scheduleText = Array.isArray(binding.schedule_windows) && binding.schedule_windows.length
    ? binding.schedule_windows.map(item => `${item.start}-${item.end}`).join(' / ')
    : '未设置（默认全天）';
  const verifier = binding.membership_verifier || {};
  const monitoringEnabled = binding.enabled !== false;
  const scheduleActiveNow = Boolean(binding.schedule_runtime?.active_now);
  const bindingBadgeText = !monitoringEnabled ? '不监控' : (scheduleActiveNow ? '当前生效' : '时段外待命');
  const bindingTitle = binding.group_name || binding.link || '未配置群链接';
  const verifierDetail = String(verifier.detail || '').trim();
  const showVerifierDetail = Boolean(verifierDetail) && !['inferred_live_probe_ready'].includes(String(verifier.status || '').trim());
  const nextApprovalEtaText = String(binding.next_approval_eta_text || '').trim() || '暂无待审批';
  const nextApprovalCountdownText = formatApprovalCountdownText(binding);
  const nextApprovalRenderedAtMs = Date.now();
  const nextApprovalCountdownMeta = `data-next-approval-countdown="1" data-next-approval-ready="${binding.next_approval_ready ? '1' : '0'}" data-next-approval-pending-count="${Number(binding.next_approval_pending_count || 0)}" data-next-approval-remaining-seconds="${Number(binding.next_approval_remaining_seconds || 0)}" data-next-approval-rendered-at-ms="${nextApprovalRenderedAtMs}" data-next-approval-timeout-minutes="${Number(binding.next_approval_timeout_minutes || 0)}" data-next-approval-oldest-pending-at="${escapeHtmlAttr(binding.next_approval_oldest_pending_at || '')}" data-next-approval-fallback-text="${escapeHtmlAttr(nextApprovalEtaText)}"`;
  const accountKey = String(row?.account_key || '').trim();
  const accountKeyEscaped = accountKey.replace(/'/g, "&#39;");
  const bindingPendingKey = `${accountKey}::${bindingIndex}`;
  const pendingAction = (window.__approvalBindingTogglePendingByKey || {})[bindingPendingKey] || '';
  const probeRefreshPending = Boolean((window.__approvalBindingProbeRefreshPendingByKey || {})[bindingPendingKey]);
  const monitorButtonClass = pendingAction ? 'pending' : (monitoringEnabled ? 'enabled' : 'disabled');
  const monitorButtonText = pendingAction === 'enabling'
    ? '正在开启'
    : (pendingAction === 'disabling' ? '正在关闭' : (monitoringEnabled ? '监控中' : '不监控'));
  return `<div class="binding-card ${binding.link ? '' : 'is-empty'}">
    <div class="binding-card-head">
      <div>
        <div class="binding-title">${bindingTitle}</div>
        <div class="muted" style="margin-top:4px;">${binding.link || '-'} · ${binding.area || '-'} · ${binding.notify_robot_name || binding.notify_profile_name || '-'}</div>
      </div>
      <span class="binding-badge">${bindingBadgeText}</span>
    </div>
    <div class="binding-meta-grid">
      <div><div class="field-hint">本群监控</div><div style="margin-top:6px;"><button type="button" class="card-monitor-toggle ${monitorButtonClass}" onclick="setApprovalBindingEnabled('${accountKeyEscaped}', ${bindingIndex}, ${monitoringEnabled ? 'false' : 'true'})" ${pendingAction ? 'disabled' : ''}>${monitorButtonText}</button></div></div>
      <div><div class="field-hint">审批条件</div><div>${binding.approval_rule_text || '-'}</div></div>
      <div><div class="field-hint">自动恢复</div><div>${binding.auto_recover_worker ? '开启' : '关闭'}</div></div>
      <div><div class="field-hint">时间段</div><div>${scheduleText}</div></div>
      <div><div class="field-hint">距离下次审批</div><div><span ${nextApprovalCountdownMeta}>${nextApprovalCountdownText}</span></div></div>
      <div><div class="field-hint">真实校验</div><div>${bindingVerifierReadinessText(verifier)} · ${bindingVerifierStatusText(verifier)}</div><div style="margin-top:6px;"><button type="button" class="secondary ${probeRefreshPending ? 'button-loading' : ''}" onclick="refreshApprovalBindingProbe('${accountKeyEscaped}', ${bindingIndex})" ${probeRefreshPending ? 'disabled' : ''}>${probeRefreshPending ? '刷新中…' : '实时刷新探针'}</button></div></div>
    </div>
    ${showVerifierDetail ? `<div class="mini-note" style="margin-top:8px;">${verifierDetail}</div>` : ''}
  </div>`;
}
function accountCardHtml(row) {
  const groupBindings = Array.isArray(row.group_binding_runtimes) && row.group_binding_runtimes.length
    ? row.group_binding_runtimes
    : (Array.isArray(row.group_link_bindings) ? row.group_link_bindings : []);
  const verificationChecks = Array.isArray(row.verification_checks) ? row.verification_checks : [];
  const verificationText = verificationChecks.length
    ? verificationChecks.map(item => `${item.ok ? '✅' : '⚠️'} ${item.detail || item.code || '-'}`).join('<br/>')
    : '-';
  const sessionState = (window.__approvalSessionStateByAccount && window.__approvalSessionStateByAccount[row.account_key]) || row.session_state || {};
  const accountKeyEscaped = String(row.account_key || '').replace(/'/g, "&#39;");
  const pendingAction = (window.__approvalTogglePendingByAccount || {})[String(row.account_key || '').trim()] || '';
  const monitorButtonClass = pendingAction ? 'pending' : (row.enabled ? 'enabled' : 'disabled');
  const monitorButtonText = pendingAction === 'enabling'
    ? '正在开启'
    : (pendingAction === 'disabling' ? '正在关闭' : (row.enabled ? '监控中' : '已关闭'));
  const isSessionLoading = Boolean(window.__approvalSessionLoadingByAccount && window.__approvalSessionLoadingByAccount[row.account_key]);
  const loginStatusText = sessionState.login_verified
    ? '账号已登录，可以正常使用'
    : (sessionState.login_check_message || (sessionState.qr_available ? '已出二维码，待扫码登录' : '未登录'));
  const loginStatusCode = String(sessionState.login_check_status || '').trim();
  const alertConfig = loginStatusCode === 'account_restricted'
    ? { level: 'red', title: '账号受限', detail: loginStatusText }
    : (loginStatusCode === 'auth_failed'
      ? { level: 'amber', title: '登录异常', detail: loginStatusText }
      : (loginStatusCode === 'waiting_for_scan'
        ? { level: 'blue', title: '等待扫码', detail: loginStatusText }
        : null));
  const accountAlert = alertConfig
    ? `<div class="account-alert ${alertConfig.level}"><strong>${alertConfig.title}</strong><div>${alertConfig.detail}</div></div>`
    : '';
  const qrBlock = sessionState.qr_ascii
    ? `<div style="margin-top:10px;"><div class="field-hint" style="margin-bottom:6px;">绑定二维码</div><pre style="margin:0; padding:12px; overflow:auto; background:#0f172a; color:#e2e8f0; border-radius:12px; font-size:10px; line-height:1.05;">${String(sessionState.qr_ascii || '').replace(/</g, '&lt;').replace(/>/g, '&gt;')}</pre><div class="muted" style="margin-top:6px;">请用这个账号的 WhatsApp - 关联设备 扫描上面的二维码。</div></div>`
    : '';
  const noteValue = String(row.notes || '').trim();
  return `<div class="account-card">
    <h3>
      <span class="account-head-left">
        <span class="account-head-title">${row.account_name || row.account_key || ''}</span>
        <button type="button" class="card-monitor-toggle ${monitorButtonClass}" onclick="setApprovalAccountEnabled('${accountKeyEscaped}', ${row.enabled ? 'false' : 'true'})" ${pendingAction ? 'disabled' : ''}>${monitorButtonText}</button>
      </span>
      <span><span class="status-dot ${row.status_color || 'gray'}"></span>${row.status_text || '-'}</span>
    </h3>
    <div class="account-meta">
      <div class="k">账号键</div><div>${row.account_key || '-'}</div>
      <div class="k">负责类型</div><div>${row.responsible_type === 'official_group' ? '官方群' : '注册群'}</div>
      <div class="k">群数量</div><div>${row.group_count || 0}</div>
      <div class="k">配置状态</div><div>${row.verification_status_label || row.verification_status || '-'}</div>
      <div class="k">登录状态</div><div>${loginStatusText}</div>
      ${noteValue ? `<div class="k">备注</div><div>${noteValue}</div>` : ''}
    </div>
    ${accountAlert}
    <div style="margin-top:14px;">
      <div class="field-hint" style="margin-bottom:8px;">逐群绑定详情</div>
      <div class="binding-list">${groupBindings.length ? groupBindings.map((binding, index) => bindingSummaryHtml(binding, row, index)).join('') : '<div class="binding-card is-empty">暂无群绑定</div>'}</div>
    </div>
    <details class="advanced-fields" style="margin-top:14px;">
      <summary>校验摘要</summary>
      <div class="advanced-fields-body"><div class="muted">${verificationText}</div></div>
    </details>
    ${qrBlock}
    <div class="link-actions">
      <button type="button" class="secondary" onclick="fillApprovalAccountForm('${accountKeyEscaped}')">回填编辑</button>
      <button type="button" class="${isSessionLoading ? 'button-loading' : ''}" ${isSessionLoading ? 'disabled' : ''} onclick="startApprovalAccountSession('${accountKeyEscaped}')">${isSessionLoading ? '生成中…' : '生成二维码'}</button>
      <button type="button" class="secondary" onclick="deleteApprovalAccount('${accountKeyEscaped}')">删除账号</button>
    </div>
    <details class="advanced-fields" style="margin-top:10px;">
      <summary>更多操作</summary>
      <div class="advanced-fields-body">
        <div class="link-actions" style="margin-top:0;">
          <button type="button" onclick="startApprovalAccountRuntime('${accountKeyEscaped}')">启动扫码服务</button>
          <button type="button" class="secondary" onclick="stopApprovalAccountRuntime('${accountKeyEscaped}')">停止扫码服务</button>
          <button type="button" class="secondary" onclick="refreshApprovalAccountSession('${accountKeyEscaped}')">刷新状态</button>
          <button type="button" class="secondary" onclick="resetApprovalAccountSession('${accountKeyEscaped}')">重置会话</button>
        </div>
      </div>
    </details>
  </div>`;
}
function renderApprovalAccountRows() {
  document.getElementById('approvalAccountRows').innerHTML = window.__approvalAccounts.length
    ? window.__approvalAccounts.map(accountCardHtml).join('')
    : '<div class="muted">暂无 WhatsApp 审批账号，请先新增。</div>';
  startApprovalCountdownTicker();
  refreshApprovalCountdownNodes();
}
async function refreshApprovalBindingProbe(accountKey, bindingIndex) {
  const normalized = String(accountKey || '').trim();
  if (!normalized) return;
  const pendingKey = `${normalized}::${bindingIndex}`;
  window.__approvalBindingProbeRefreshPendingByKey = window.__approvalBindingProbeRefreshPendingByKey || {};
  window.__approvalBindingProbeRefreshPendingByKey[pendingKey] = true;
  renderApprovalAccountRows();
  try {
    await reloadApprovalAccounts();
    showToast('群探针状态已刷新', 'success');
  } finally {
    delete window.__approvalBindingProbeRefreshPendingByKey[pendingKey];
    renderApprovalAccountRows();
  }
}
async function startApprovalAccountRuntime(accountKey) {
  const normalized = String(accountKey || '').trim();
  if (!normalized) throw new Error('account_key is required');
  const data = await loadJson(`/api/ops/whatsapp-approval-accounts/${encodeURIComponent(normalized)}/runtime/start`, {method: 'POST'});
  await reloadApprovalAccounts();
  showToast('扫码服务已启动', 'success');
  return data;
}
async function stopApprovalAccountRuntime(accountKey) {
  const normalized = String(accountKey || '').trim();
  if (!normalized) throw new Error('account_key is required');
  const data = await loadJson(`/api/ops/whatsapp-approval-accounts/${encodeURIComponent(normalized)}/runtime/stop`, {method: 'POST'});
  await reloadApprovalAccounts();
  showToast('扫码服务已停止', 'success');
  return data;
}
async function refreshApprovalAccountSession(accountKey, options = {}) {
  const normalized = String(accountKey || '').trim();
  if (!normalized) return;
  const data = await loadJson(`/api/ops/whatsapp-approval-accounts/${encodeURIComponent(normalized)}/session`);
  window.__approvalSessionStateByAccount = window.__approvalSessionStateByAccount || {};
  window.__approvalSessionStateByAccount[normalized] = mergeApprovalSessionState(window.__approvalSessionStateByAccount[normalized], data.session || {});
  renderApprovalAccountRows();
  if (options.keepModal && window.__approvalQrModalState?.accountKey === normalized) {
    openApprovalQrModal(normalized, {sessionState: window.__approvalSessionStateByAccount[normalized], loading: false});
  }
  return data;
}
async function startApprovalAccountSession(accountKey, options = {}) {
  const normalized = String(accountKey || '').trim();
  if (!normalized) throw new Error('account_key is required');
  window.__approvalSessionLoadingByAccount = window.__approvalSessionLoadingByAccount || {};
  window.__approvalSessionLoadingByAccount[normalized] = true;
  renderApprovalAccountRows();
  openApprovalQrModal(normalized, {loading: true, sessionState: (window.__approvalSessionStateByAccount || {})[normalized] || {}, resetSuccessAnnounced: true});
  try {
    const data = await loadJson(`/api/ops/whatsapp-approval-accounts/${encodeURIComponent(normalized)}/session/start`, {method: 'POST'});
    window.__approvalSessionStateByAccount = window.__approvalSessionStateByAccount || {};
    window.__approvalSessionStateByAccount[normalized] = mergeApprovalSessionState(window.__approvalSessionStateByAccount[normalized], data.session || {});
    renderApprovalAccountRows();
    openApprovalQrModal(normalized, {sessionState: window.__approvalSessionStateByAccount[normalized], loading: false});
    showToast('二维码已生成，请直接在弹窗里扫码', 'success');
    return data;
  } catch (error) {
    openApprovalQrModal(normalized, {loading: false, error: error.message || '生成二维码失败'});
    throw error;
  } finally {
    window.__approvalSessionLoadingByAccount[normalized] = false;
    renderApprovalAccountRows();
  }
}
async function resetApprovalAccountSession(accountKey) {
  const normalized = String(accountKey || '').trim();
  if (!normalized) throw new Error('account_key is required');
  const data = await loadJson(`/api/ops/whatsapp-approval-accounts/${encodeURIComponent(normalized)}/session/reset`, {method: 'POST'});
  window.__approvalSessionStateByAccount = window.__approvalSessionStateByAccount || {};
  window.__approvalSessionStateByAccount[normalized] = mergeApprovalSessionState(window.__approvalSessionStateByAccount[normalized], data.session || {});
  renderApprovalAccountRows();
  openApprovalQrModal(normalized, {sessionState: window.__approvalSessionStateByAccount[normalized], loading: false, resetSuccessAnnounced: true});
  showToast('会话已重置，并重新生成新的绑定二维码', 'success');
  return data;
}
function parseScheduleWindowsText(text) {
  const rows = String(text || '').split('\\n').map(item => item.trim()).filter(Boolean);
  return rows.map(item => {
    const parts = item.split('-').map(part => part.trim());
    return {start: parts[0] || '', end: parts[1] || ''};
  });
}
async function reloadApprovalAccounts() {
  const data = await loadJson('/api/ops/whatsapp-approval-accounts');
  const previousSessionState = window.__approvalSessionStateByAccount || {};
  window.__approvalAccounts = Array.isArray(data.rows) ? data.rows : [];
  window.__approvalSessionStateByAccount = Object.fromEntries(window.__approvalAccounts.map(item => {
    const accountKey = String(item.account_key || '').trim();
    return [accountKey, mergeApprovalSessionState(previousSessionState[accountKey], item.session_state || {})];
  }).filter(item => item[0]));
  window.__notifyRobotOptions = Array.isArray(data.notify_robot_options) ? data.notify_robot_options : [];
  window.__approvalAreaOptions = Array.isArray(data.area_options) ? data.area_options : [];
  renderAllGroupNotifyRobotSelects(
    window.__notifyRobotOptions,
    [1, 2, 3].map(index => String(document.getElementById(`wa_group_notify_profile_name_${index}`)?.value || '').trim()),
  );
  renderAllGroupAreaSelects(
    window.__approvalAreaOptions,
    [1, 2, 3].map(index => String(document.getElementById(`wa_group_area_${index}`)?.value || '').trim()),
  );
  renderApprovalAccountRows();
  if (window.__approvalQrModalState?.open && window.__approvalQrModalState.accountKey) {
    const modalAccountKey = String(window.__approvalQrModalState.accountKey || '').trim();
    openApprovalQrModal(modalAccountKey, {
      sessionState: window.__approvalSessionStateByAccount[modalAccountKey] || {},
      loading: Boolean(window.__approvalSessionLoadingByAccount && window.__approvalSessionLoadingByAccount[modalAccountKey]),
    });
  }
  const summary = data.summary || {};
  document.getElementById('waAccountCount').textContent = String(summary.total_accounts ?? 0);
  document.getElementById('waEnabledAccountCount').textContent = String(summary.active_now_accounts ?? 0);
  renderRegistrationGroupOverview();
}
async function reloadApprovalCandidates() {
  const data = await loadJson('/api/ops/whatsapp-approval-candidates');
  const summary = data.summary || {};
  const framework = data.verifier_framework || {};
  renderStatusMeta('approvalCandidateMeta', [
    ['可调度账号', String(summary.eligible_count ?? 0)],
    ['注册群', String(summary.registration_group_count ?? 0)],
    ['官方群', String(summary.official_group_count ?? 0)],
    ['真实校验已就绪', String(summary.verifier_ready_count ?? 0)],
  ]);
  renderStatusMeta('approvalVerifierMeta', [
    ['状态', formatVerifierFrameworkStatus(framework.status)],
    ['真实校验可用', String(Boolean(framework.real_membership_check_ready))],
    ['需人工补齐', String(Boolean(framework.requires_manual_seed))],
    ['说明', framework.detail || '-'],
  ]);
}
function fillApprovalAccountForm(accountKey) {
  const rows = Array.isArray(window.__approvalAccounts) ? window.__approvalAccounts : [];
  const row = rows.find(item => String(item.account_key || '') === String(accountKey || ''));
  if (!row) return;
  document.getElementById('wa_account_key').value = row.account_key || '';
  document.getElementById('wa_account_name').value = row.account_name || '';
  document.getElementById('wa_responsible_type').value = row.responsible_type || 'registration_group';
  document.getElementById('wa_enabled').value = Boolean(row.enabled) ? 'true' : 'false';
  const groupBindings = Array.isArray(row.group_binding_runtimes) && row.group_binding_runtimes.length
    ? row.group_binding_runtimes
    : (Array.isArray(row.group_link_bindings) ? row.group_link_bindings : []);
  fillIndexedValues('wa_group_link_', groupBindings.map(item => item.link || ''), 3);
  fillIndexedValues('wa_group_name_', groupBindings.map(item => item.group_name || ''), 3);
  renderAllGroupAreaSelects(window.__approvalAreaOptions, groupBindings.map(item => item.area || ''));
  renderAllGroupNotifyRobotSelects(window.__notifyRobotOptions, groupBindings.map(item => item.notify_profile_name || ''));
  for (let i = 1; i <= 3; i += 1) {
    const binding = groupBindings[i - 1] || {};
    document.getElementById(`wa_group_enabled_${i}`).value = binding.enabled === false ? 'false' : 'true';
    document.getElementById(`wa_group_registration_group_${i}`).value = String(binding.registration_group || '');
    document.getElementById(`wa_group_group_id_${i}`).value = String(binding.group_id || '');
    document.getElementById(`wa_group_approval_count_threshold_${i}`).value = String(binding.approval_count_threshold || 30);
    document.getElementById(`wa_group_approval_timeout_minutes_${i}`).value = String(binding.approval_timeout_minutes || 30);
    document.getElementById(`wa_group_auto_recover_worker_${i}`).value = binding.auto_recover_worker === false ? 'false' : 'true';
    fillGroupScheduleWindows(i, binding.schedule_windows || []);
    renderBindingCardState(i, binding);
  }
  document.getElementById('wa_notes').value = row.notes || '';
  document.getElementById('wa_account_name').scrollIntoView({behavior: 'smooth', block: 'center'});
}
function clearApprovalAccountForm() {
  document.getElementById('wa_account_key').value = '';
  document.getElementById('wa_account_name').value = '';
  document.getElementById('wa_responsible_type').value = 'registration_group';
  document.getElementById('wa_enabled').value = 'true';
  fillIndexedValues('wa_group_link_', [], 3);
  fillIndexedValues('wa_group_name_', [], 3);
  renderAllGroupAreaSelects(window.__approvalAreaOptions, []);
  renderAllGroupNotifyRobotSelects(window.__notifyRobotOptions, []);
  for (let i = 1; i <= 3; i += 1) {
    document.getElementById(`wa_group_enabled_${i}`).value = 'true';
    document.getElementById(`wa_group_registration_group_${i}`).value = '';
    document.getElementById(`wa_group_group_id_${i}`).value = '';
    document.getElementById(`wa_group_approval_count_threshold_${i}`).value = '30';
    document.getElementById(`wa_group_approval_timeout_minutes_${i}`).value = '30';
    document.getElementById(`wa_group_auto_recover_worker_${i}`).value = 'true';
    fillGroupScheduleWindows(i, []);
    renderBindingCardState(i, {});
  }
  document.getElementById('wa_notes').value = '';
}
async function saveApprovalAccount() {
  try {
    const accountKey = ensureApprovalAccountKey();
    if (!accountKey) throw new Error('account_key is required.');
    const groupLinkBindings = collectGroupBindings(3);
    const primaryBinding = groupLinkBindings[0] || {};
    const payload = {
      account_name: document.getElementById('wa_account_name').value.trim(),
      responsible_type: document.getElementById('wa_responsible_type').value,
      enabled: document.getElementById('wa_enabled').value === 'true',
      group_link_bindings: groupLinkBindings,
      group_links: groupLinkBindings.map(item => item.link),
      area: primaryBinding.area || '',
      notify_profile_name: primaryBinding.notify_profile_name || '',
      approval_count_threshold: Number(primaryBinding.approval_count_threshold || 0),
      approval_timeout_minutes: Number(primaryBinding.approval_timeout_minutes || 0),
      auto_recover_worker: primaryBinding.auto_recover_worker !== false,
      schedule_windows: Array.isArray(primaryBinding.schedule_windows) ? primaryBinding.schedule_windows : [],
      notes: document.getElementById('wa_notes').value.trim(),
    };
    await loadJson(`/api/ops/whatsapp-approval-accounts/${encodeURIComponent(accountKey)}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    showToast('保存 WhatsApp 审批账号成功', 'success');
    await reloadApprovalAccounts();
  } catch (err) {
    showToast(`保存审批账号失败：${err.message || err}`, 'error');
    throw err;
  }
}
function buildApprovalAccountPayloadFromRow(row, overrides = {}) {
  const bindings = Array.isArray(row.group_binding_runtimes) && row.group_binding_runtimes.length
    ? row.group_binding_runtimes
    : (Array.isArray(row.group_link_bindings) ? row.group_link_bindings : []);
  const normalizedBindings = bindings.map(item => ({
    link: String(item.link || '').trim(),
    group_name: String(item.group_name || '').trim(),
    area: String(item.area || '').trim(),
    notify_profile_name: String(item.notify_profile_name || '').trim(),
    enabled: item.enabled !== false,
    registration_group: String(item.registration_group || '').trim(),
    group_id: String(item.group_id || '').trim(),
    approval_count_threshold: Number(item.approval_count_threshold || 0),
    approval_timeout_minutes: Number(item.approval_timeout_minutes || 0),
    auto_recover_worker: item.auto_recover_worker !== false,
    schedule_windows: Array.isArray(item.schedule_windows) ? item.schedule_windows.map(win => ({
      start: String(win.start || '').trim(),
      end: String(win.end || '').trim(),
    })) : [],
  }));
  const payload = {
    account_name: String(row.account_name || '').trim(),
    responsible_type: String(row.responsible_type || 'registration_group').trim() || 'registration_group',
    enabled: row.enabled !== false,
    group_link_bindings: normalizedBindings,
    group_links: normalizedBindings.map(item => String(item.link || '').trim()).filter(Boolean),
    area: String(row.area || '').trim(),
    notify_profile_name: String(row.notify_profile_name || '').trim(),
    approval_count_threshold: Number(row.approval_count_threshold || 0),
    approval_timeout_minutes: Number(row.approval_timeout_minutes || 0),
    auto_recover_worker: row.auto_recover_worker !== false,
    schedule_windows: Array.isArray(row.schedule_windows) ? row.schedule_windows.map(win => ({
      start: String(win.start || '').trim(),
      end: String(win.end || '').trim(),
    })) : [],
    notes: String(row.notes || '').trim(),
  };
  return {...payload, ...overrides};
}
async function setApprovalAccountEnabled(accountKey, enabled) {
  const normalized = String(accountKey || '').trim();
  if (!normalized) throw new Error('account_key is required.');
  window.__approvalTogglePendingByAccount = window.__approvalTogglePendingByAccount || {};
  window.__approvalTogglePendingByAccount[normalized] = enabled ? 'enabling' : 'disabling';
  renderApprovalAccountRows();
  const rows = Array.isArray(window.__approvalAccounts) ? window.__approvalAccounts : [];
  const row = rows.find(item => String(item.account_key || '') === normalized);
  if (!row) {
    delete window.__approvalTogglePendingByAccount[normalized];
    renderApprovalAccountRows();
    throw new Error(`account not found: ${normalized}`);
  }
  const payload = buildApprovalAccountPayloadFromRow(row, {enabled: Boolean(enabled)});
  try {
    await loadJson(`/api/ops/whatsapp-approval-accounts/${encodeURIComponent(normalized)}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    await reloadApprovalAccounts();
    showToast(`已${enabled ? '开启' : '关闭'}账号监控`, 'success');
  } finally {
    delete window.__approvalTogglePendingByAccount[normalized];
    renderApprovalAccountRows();
  }
}
async function setApprovalBindingEnabled(accountKey, bindingIndex, enabled) {
  const normalized = String(accountKey || '').trim();
  const index = Number(bindingIndex);
  if (!normalized) throw new Error('account_key is required.');
  if (!Number.isInteger(index) || index < 0) throw new Error('binding_index is invalid.');
  window.__approvalBindingTogglePendingByKey = window.__approvalBindingTogglePendingByKey || {};
  const pendingKey = `${normalized}::${index}`;
  window.__approvalBindingTogglePendingByKey[pendingKey] = enabled ? 'enabling' : 'disabling';
  renderApprovalAccountRows();
  const rows = Array.isArray(window.__approvalAccounts) ? window.__approvalAccounts : [];
  const row = rows.find(item => String(item.account_key || '') === normalized);
  if (!row) {
    delete window.__approvalBindingTogglePendingByKey[pendingKey];
    renderApprovalAccountRows();
    throw new Error(`account not found: ${normalized}`);
  }
  const bindings = Array.isArray(row.group_binding_runtimes) && row.group_binding_runtimes.length
    ? row.group_binding_runtimes
    : (Array.isArray(row.group_link_bindings) ? row.group_link_bindings : []);
  if (!bindings[index]) {
    delete window.__approvalBindingTogglePendingByKey[pendingKey];
    renderApprovalAccountRows();
    throw new Error(`binding not found: ${normalized}#${index}`);
  }
  const nextBindings = bindings.map((item, itemIndex) => itemIndex === index ? {...item, enabled: Boolean(enabled)} : item);
  const payload = buildApprovalAccountPayloadFromRow(row, {
    group_link_bindings: nextBindings.map(item => ({
      link: String(item.link || '').trim(),
      group_name: String(item.group_name || '').trim(),
      area: String(item.area || '').trim(),
      notify_profile_name: String(item.notify_profile_name || '').trim(),
      enabled: item.enabled !== false,
      registration_group: String(item.registration_group || '').trim(),
      group_id: String(item.group_id || '').trim(),
      approval_count_threshold: Number(item.approval_count_threshold || 0),
      approval_timeout_minutes: Number(item.approval_timeout_minutes || 0),
      auto_recover_worker: item.auto_recover_worker !== false,
      schedule_windows: Array.isArray(item.schedule_windows) ? item.schedule_windows.map(win => ({
        start: String(win.start || '').trim(),
        end: String(win.end || '').trim(),
      })) : [],
    })),
    group_links: nextBindings.map(item => String(item.link || '').trim()).filter(Boolean),
  });
  try {
    await loadJson(`/api/ops/whatsapp-approval-accounts/${encodeURIComponent(normalized)}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    await reloadApprovalAccounts();
    showToast(`已${enabled ? '开启' : '关闭'}本群监控`, 'success');
  } finally {
    delete window.__approvalBindingTogglePendingByKey[pendingKey];
    renderApprovalAccountRows();
  }
}
async function deleteApprovalAccount(accountKey) {
  const normalized = String(accountKey || '').trim();
  if (!normalized) return;
  const confirmed = window.confirm(`确认删除这个 WhatsApp 账号吗？\n账号键: ${normalized}`);
  if (!confirmed) return;
  await loadJson(`/api/ops/whatsapp-approval-accounts/${encodeURIComponent(normalized)}`, {method: 'DELETE'});
  if (document.getElementById('wa_account_key').value.trim() === normalized) {
    clearApprovalAccountForm();
  }
  showToast(`删除审批账号成功：${normalized}`, 'success');
  await reloadApprovalAccounts();
}
async function reloadOfficialBridgeSummary() {
  const data = await loadJson('/api/ops/official-group-bridge-summary');
  window.__officialBridgeSummaryData = data;
  const health = data.health || {};
  const summary = data.summary || {};
  document.getElementById('officialPendingCount').textContent = String(summary.pending_count ?? 0);
  renderStatusMeta('officialBridgeSummaryMeta', [
    ['状态', formatOfficialBridgeHealthStatus(health.status)],
    ['模式', formatOfficialBridgeMode(health.mode)],
    ['目标群数', String(Object.keys(summary.by_target_group || {}).length)],
    ['待处理', String(summary.pending_count ?? 0)],
    ['今日新增', String(summary.today_created_count ?? 0)],
    ['超时超1小时', String(summary.pending_timeout_over_1h_count ?? 0)],
  ]);
}
function renderRegistrationGroupOverview() {
  if (!document.getElementById('workerHealthMeta')) return;
  const rows = Array.isArray(window.__approvalAccounts) ? window.__approvalAccounts : [];
  const registrationRows = rows.filter(item => String(item.responsible_type || '') === 'registration_group');
  const bindings = registrationRows.flatMap(item => Array.isArray(item.group_binding_runtimes) && item.group_binding_runtimes.length
    ? item.group_binding_runtimes
    : (Array.isArray(item.group_link_bindings) ? item.group_link_bindings : []));
  const monitoredBindings = bindings.filter(item => item?.enabled !== false);
  const activeBindings = monitoredBindings.filter(item => Boolean(item?.schedule_runtime?.active_now)).length;
  const readyBindings = monitoredBindings.filter(item => Boolean(item?.membership_verifier?.ready)).length;
  const runtime = (window.__productionOpsDaemonData || {}).runtime || {};
  const status = runtime.status || {};
  const workerState = status.worker_state || {};
  const probeCandidates = [status.decision_group_state?.payload, status.fresh_probe?.payload, status.worker_state?.payload]
    .filter(item => item && typeof item === 'object');
  const probeNames = [...new Set(probeCandidates.map(item => String(item.group_name || item.group_id || '').trim()).filter(Boolean))];
  const pendingValues = probeCandidates.map(item => Number(item.pending_count)).filter(item => Number.isFinite(item));
  renderStatusMeta('workerHealthMeta', [
    ['状态', workerState.ok ? '正常' : '异常'],
    ['注册账号', String(registrationRows.length)],
    ['绑定群数', String(bindings.length)],
    ['当前生效', `${activeBindings}/${bindings.length || 0}`],
    ['真实校验', `${readyBindings}/${bindings.length || 0}`],
    ['当前探针', probeNames[0] || '-'],
    ['待审批', pendingValues.length ? String(Math.max(...pendingValues)) : '-'],
  ]);
}
function applyProductionOpsDaemonConfig(data) {
  window.__productionOpsDaemonData = data;
  const config = data.config || {};
  const runtime = data.runtime || {};
  const status = runtime.status || {};
  const workerState = status.worker_state || {};
  const releaseEvaluation = status.release_evaluation || {};
  const formalApproval = status.formal_approval || {};
  const releasePayload = releaseEvaluation.payload || {};
  const checkedAt = status.checked_at || '暂无';
  const pendingIncidents = Array.isArray(status.incidents) ? status.incidents.length : 0;
  const notificationCount = Array.isArray(status.notifications) ? status.notifications.length : 0;
  const launchdState = runtime.launch_agent_installed ? '已安装' : '未安装';
  document.getElementById('daemonEnabledState').textContent = config.enabled ? '开启' : '关闭';
  document.getElementById('productionOpsRuntimeHint').textContent = `守护进程=${launchdState} · 最近检查=${checkedAt} · 异常=${pendingIncidents} · 通知=${notificationCount}`;
  document.getElementById('productionOpsPathsHint').textContent = `状态文件：${runtime.status_path || '-'} · 运行时配置：${runtime.env_path || '-'}${pendingIncidents ? ` · 当前异常数：${pendingIncidents}` : ''}`;
  renderRegistrationGroupOverview();
}
async function reloadProductionOpsDaemonConfig() {
  const data = await loadJson('/api/ops/production-ops-daemon');
  applyProductionOpsDaemonConfig(data);
}
reloadProductionOpsDaemonConfig().catch(err => showToast(err.message, 'error'));
reloadAreaOptions().catch(err => showToast(err.message, 'error'));
reloadApprovalAccounts().catch(err => showToast(err.message, 'error'));
reloadOfficialBridgeSummary().catch(err => showToast(err.message, 'error'));
setInterval(() => {
  reloadProductionOpsDaemonConfig().catch(err => showToast(err.message, 'error'));
  reloadAreaOptions().catch(err => showToast(err.message, 'error'));
  reloadApprovalAccounts().catch(err => showToast(err.message, 'error'));
  reloadOfficialBridgeSummary().catch(err => showToast(err.message, 'error'));
}, 15000);
</script>
</body>
</html>
"""


OPS_PAGE_HTML = """
<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>运营操作台 MVP</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 24px; background: #f6f8fb; color: #1f2937; }
    h1 { margin-top: 0; }
    .grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin-bottom: 24px; }
    .card { background: white; border-radius: 12px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
    .card h2 { font-size: 16px; margin: 0 0 12px 0; }
    .metric { font-size: 28px; font-weight: 700; }
    table { width: 100%; border-collapse: collapse; background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
    th, td { padding: 10px 12px; border-bottom: 1px solid #e5e7eb; text-align: left; font-size: 14px; }
    th { background: #eef2ff; }
    .section { margin-top: 24px; }
    .muted { color: #6b7280; font-size: 13px; }
    .review-field { display: flex; flex-direction: column; gap: 4px; margin-top: 6px; }
    .review-field input, .review-field textarea { width: 100%; box-sizing: border-box; font-size: 12px; padding: 6px 8px; }
    .review-field label { font-size: 12px; color: #6b7280; }
    .toast { position: fixed; right: 24px; bottom: 24px; min-width: 240px; max-width: 360px; background: #111827; color: #fff; padding: 12px 14px; border-radius: 10px; box-shadow: 0 8px 24px rgba(0,0,0,.2); display: none; z-index: 1000; }
    .toast.success { background: #065f46; }
    .toast.error { background: #991b1b; }
    .page-shell { max-width: 1360px; margin: 0 auto; }
    .shell-nav { position: sticky; top: 0; z-index: 20; display:flex; gap:10px; flex-wrap:wrap; margin: 0 0 16px 0; padding: 10px 0 12px; background: rgba(246,248,251,.96); backdrop-filter: blur(8px); }
    .shell-nav a { color:#2563eb; text-decoration:none; font-size:13px; padding:6px 10px; border-radius:999px; background:#eef2ff; }
    .hero { background:#ffffff; border:1px solid #e5e7eb; border-radius:16px; padding:20px; box-shadow: 0 1px 3px rgba(0,0,0,.06); margin-bottom:16px; }
    .hero .eyebrow { color:#6366f1; font-size:12px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; margin-bottom:8px; }
    .hero .subtitle { color:#4b5563; font-size:14px; margin-top:8px; }
    .queue-overview-grid { display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:16px; }
    .queue-layout { display:grid; gap:24px; }
    .section-title { margin: 0 0 12px 0; font-size: 20px; }
  </style>
</head>
<body>
  <div class="page-shell">
    <div class="shell-nav">
      <a href="/ops">运营工作台</a>
      <a href="/ops/intake-bot-presets">收口配置中心</a>
      <a href="/ops/production-ops">群审批控制台</a>
      <a href="/ops/official-group-bridge">官方群审批桥接台</a>
    </div>
    <div class="hero">
      <div class="eyebrow">Operations</div>
      <h1>运营工作台</h1>
      <div class="subtitle">运营操作台 MVP · 面向人工处理、队列推进与异常回查的统一工作台。</div>
      <div class=\"muted\">数据来源：/api/ops/dashboard/summary · /api/ops/manual-review-queue · /api/ops/bind-queue · /api/ops/group-queue · /api/ops/parser-quality-summary</div>
      <div class=\"muted\" style=\"margin-top:8px;\"><a href=\"/ops/intake-bot-presets\">前往收口机器人配置中心</a> · <a href=\"/ops/production-ops\">前往群审批控制台</a></div>
    </div>

  <div class=\"card\" style=\"margin-top:16px;\">
    <h2>工作台总览</h2>
    <div class="muted" style="margin-bottom:8px;">处理队列</div>
    <h2>AI 下一步处理建议</h2>
    <div id=\"nextActionHint\" class=\"muted\">加载中...</div>
    <pre id=\"nextActionJson\" style=\"white-space: pre-wrap; overflow:auto; margin-top:12px;\"></pre>
  </div>

  <div class=\"section\">
    <h2 class=\"section-title\">批次处理</h2>
    <h2>审批批次队列</h2>
    <div class=\"grid\" style=\"grid-template-columns: repeat(2, minmax(0, 1fr));\">
      <div class=\"card\">
        <h2>注册群批次</h2>
        <table>
          <thead><tr><th>群组</th><th>ready</th><th>释放人数</th><th>原因</th></tr></thead>
          <tbody id=\"registrationBatchRows\"></tbody>
        </table>
      </div>
      <div class=\"card\">
        <h2>官方群批次</h2>
        <table>
          <thead><tr><th>群组</th><th>ready</th><th>释放人数</th><th>原因</th></tr></thead>
          <tbody id=\"officialBatchRows\"></tbody>
        </table>
      </div>
    </div>
  </div>

  <div class=\"grid queue-overview-grid\">
    <div class=\"card\"><h2>待复核</h2><div id=\"manualReviewCount\" class=\"metric\">-</div></div>
    <div class=\"card\"><h2>待绑定</h2><div id=\"bindQueueCount\" class=\"metric\">-</div></div>
    <div class=\"card\"><h2>待入群</h2><div id=\"groupQueueCount\" class=\"metric\">-</div></div>
  </div>

  <div class=\"grid queue-overview-grid\">
    <div class=\"card\"><h2>绑定成功</h2><div id=\"bindSuccessCount\" class=\"metric\">-</div></div>
    <div class=\"card\"><h2>解析冲突</h2><div id=\"parserConflictCount\" class=\"metric\">-</div></div>
    <div class=\"card\"><h2>修正次数</h2><div id=\"correctionCount\" class=\"metric\">-</div></div>
  </div>

  <div class=\"queue-layout\">
  <div class=\"section\">
    <h2 class=\"section-title\">处理队列</h2>
    <h2>人工复核队列</h2>
    <table>
      <thead><tr><th>lead_id</th><th>手机号</th><th>用户ID</th><th>置信度</th><th>解析状态</th><th>复核原因</th><th>推荐动作</th><th>操作</th></tr></thead>
      <tbody id=\"manualReviewRows\"></tbody>
    </table>
  </div>

  <div class=\"section\">
    <h2>待绑定列表</h2>
    <table>
      <thead><tr><th>lead_id</th><th>手机号</th><th>yw_id</th><th>应用</th><th>公会</th><th>注册群组</th><th>状态</th><th>操作</th></tr></thead>
      <tbody id=\"bindQueueRows\"></tbody>
    </table>
  </div>

  <div class=\"section\">
    <h2>待入群列表</h2>
    <table>
      <thead><tr><th>lead_id</th><th>手机号</th><th>yw_id</th><th>应用</th><th>公会</th><th>注册群组</th><th>状态</th><th>操作</th></tr></thead>
      <tbody id=\"groupQueueRows\"></tbody>
    </table>
  </div>

  <div class=\"section\">
    <h2 class=\"section-title\">客服通知</h2>
    <h2>客服通知列表</h2>
    <div class=\"muted\">支持未读/已读筛选与关键词搜索（手机号、用户ID）。</div>
    <div style=\"display:flex; gap:8px; margin:12px 0;\">
      <select id=\"notificationStatus\">
        <option value=\"\">全部</option>
        <option value=\"unread\">未读</option>
        <option value=\"read\">已读</option>
      </select>
      <input id=\"notificationQuery\" placeholder=\"搜索手机号或用户ID\" />
      <button onclick=\"reloadNotifications()\">筛选</button>
    </div>
    <table>
      <thead><tr><th>时间</th><th>类型</th><th>手机号</th><th>用户ID</th><th>写入结果</th><th>原因</th><th>操作</th></tr></thead>
      <tbody id=\"notificationRows\"></tbody>
    </table>
  </div>

  <div class=\"section\">
    <h2>详情查看</h2>
    <div class=\"muted\">点击“查看详情”会加载该 lead 的 timeline JSON。</div>
    <pre id=\"leadDetail\" class=\"card\" style=\"white-space: pre-wrap; overflow:auto;\">尚未选择 lead</pre>
  </div>

  </div>

  <div id=\"manualReviewToast\" class=\"toast\"></div>

<script>
async function loadJson(url, options) {
  const res = await fetch(url, options || {});
  if (!res.ok) {
    let detail = '';
    try {
      const data = await res.json();
      detail = data.detail || JSON.stringify(data);
    } catch (_) {}
    throw new Error(detail || `Failed to load ${url}: ${res.status}`);
  }
  return await res.json();
}
async function showDetail(leadId) {
  const data = await loadJson(`/api/leads/${leadId}/timeline`);
  document.getElementById('leadDetail').innerHTML = renderLeadDetail(data);
}
function renderRecognitionCodeSummary(row) {
  const personCode = row.person_code || '—';
  const guildInviteCode = row.guild_invite_code || '—';
  return `
    <div class="review-field" style="margin-top:8px; padding:8px; background:#f8fafc; border:1px solid #e5e7eb; border-radius:8px;">
      <label>识别摘要</label>
      <div class="muted">用户个人绑定码：${personCode}</div>
      <div class="muted">公会固定邀请码：${guildInviteCode}</div>
    </div>
  `;
}
function renderLeadDetail(data) {
  const submissions = data.account_submissions || [];
  const recognized = [...submissions].reverse().find(item => item.recognition_raw && (item.recognition_raw.person_code || item.recognition_raw.guild_invite_code || item.recognition_raw.normalized));
  const recognition = recognized?.recognition_raw || {};
  const normalized = recognition.normalized || {};
  const personCode = recognition.person_code || normalized.person_code || '—';
  const guildInviteCode = recognition.guild_invite_code || normalized.guild_invite_code || '—';
  return `
    <div style="margin-bottom:12px; padding:12px; background:#f8fafc; border:1px solid #e5e7eb; border-radius:10px;">
      <div style="font-weight:600; margin-bottom:6px;">识别摘要</div>
      <div class="muted">用户个人绑定码：${personCode}</div>
      <div class="muted">公会固定邀请码：${guildInviteCode}</div>
    </div>
    <div style="font-weight:600; margin-bottom:6px;">原始 timeline JSON</div>
    <pre style="white-space: pre-wrap; overflow:auto; margin:0;">${JSON.stringify(data, null, 2)}</pre>
  `;
}
async function bindAction(taskId, status) {
  const resultReason = status === 'success' ? 'manual operator marked bind success' : prompt('请输入绑定失败原因', 'ID错误或后台绑定失败') || 'manual bind failure';
  await loadJson(`/api/tasks/${taskId}/bind-check-result`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      status,
      result_code: status === 'success' ? 'bind_ok' : 'bind_failed',
      result_reason: resultReason,
      finished_at: new Date().toISOString(),
      raw_result: {}
    })
  });
  location.reload();
}
async function runNativeOcr(taskId) {
  try {
    const result = await loadJson(`/api/tasks/${taskId}/native-ocr-run`, {method: 'POST'});
    await reloadManualReviewQueue();
    showToast(`OCR执行完成：${result.status} → ${result.next_action || ''}`, 'success');
  } catch (err) {
    showToast(`OCR执行失败：${err.message}`, 'error');
    throw err;
  }
}
async function groupAction(taskId, status) {
  const resultReason = status === 'success' ? 'manual operator marked group join success' : prompt('请输入入群失败原因', '管理员未通过或用户未申请') || 'manual group join failure';
  await loadJson(`/api/tasks/${taskId}/group-join-result`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      status,
      result_code: status === 'success' ? 'join_ok' : 'join_failed',
      result_reason: resultReason,
      finished_at: new Date().toISOString(),
      raw_result: {}
    })
  });
  location.reload();
}
function bindRowHtml(row) {
  return `<tr>
    <td>${row.lead_id ?? ''}</td>
    <td>+${row.area_code ?? ''} ${row.mobile ?? ''}</td>
    <td>${row.yw_id ?? ''}</td>
    <td>${row.app_name ?? ''}</td>
    <td>${row.dept_name ?? ''}</td>
    <td>${row.pendaftaran_group ?? ''}</td>
    <td>${row.current_status ?? ''}</td>
    <td>
      <button onclick=\"showDetail('${row.lead_id}')\">查看详情</button>
      ${row.current_status === 'recognition_pending'
        ? `<button onclick=\"runNativeOcr('${row.task_id}')\">运行OCR</button>`
        : `<button onclick=\"bindAction('${row.task_id}','success')\">绑定成功</button>
      <button onclick=\"bindAction('${row.task_id}','failed')\">绑定失败</button>`}
    </td>
  </tr>`;
}
function groupRowHtml(row) {
  return `<tr>
    <td>${row.lead_id ?? ''}</td>
    <td>+${row.area_code ?? ''} ${row.mobile ?? ''}</td>
    <td>${row.yw_id ?? ''}</td>
    <td>${row.app_name ?? ''}</td>
    <td>${row.dept_name ?? ''}</td>
    <td>${row.pendaftaran_group ?? ''}</td>
    <td>${row.current_status ?? ''}</td>
    <td>
      <button onclick=\"showDetail('${row.lead_id}')\">查看详情</button>
      <button onclick=\"groupAction('${row.task_id}','success')\">入群成功</button>
      <button onclick=\"groupAction('${row.task_id}','failed')\">入群失败</button>
    </td>
  </tr>`;
}
function renderManualReviewEditor(row) {
  const accountId = row.yw_id ?? '';
  const appName = row.app_name ?? '';
  const deptName = row.dept_name ?? '';
  const groupName = row.pendaftaran_group ?? '';
  return `
    <div class="review-field"><label>账号ID</label><input id="manualReviewFieldValue-${row.lead_id}" value="${accountId}" placeholder="人工确认后的账号ID" /></div>
    <div class="review-field"><label>应用</label><input id="manualReviewAppName-${row.lead_id}" value="${appName}" placeholder="Linky / FUMI" /></div>
    <div class="review-field"><label>公会</label><input id="manualReviewDeptName-${row.lead_id}" value="${deptName}" placeholder="Piso / Permata" /></div>
    <div class="review-field"><label>注册群组</label><input id="manualReviewRegistrationGroup-${row.lead_id}" value="${groupName}" placeholder="Piso-23" /></div>
    <div class="review-field"><label>备注</label><textarea id="manualReviewNote-${row.lead_id}" placeholder="填写复核说明"></textarea></div>
  `;
}
function manualReviewRowHtml(row) {
  const reasons = (row.review_reason_codes || []).join(', ');
  return `<tr>
    <td>${row.lead_id ?? ''}</td>
    <td>+${row.area_code ?? ''} ${row.mobile ?? ''}</td>
    <td>${row.yw_id ?? ''}</td>
    <td>${row.parser_confidence ?? ''}</td>
    <td>${row.parser_status ?? ''}</td>
    <td>${reasons}</td>
    <td>${row.recommended_next_action ?? ''}<div style="margin-top:8px; min-width:240px;">${renderManualReviewEditor(row)}${renderRecognitionCodeSummary(row)}</div></td>
    <td>
      <button onclick=\"showDetail('${row.lead_id}')\">查看详情</button>
      <button onclick=\"approveManualReview('${row.lead_id}')\">确认可绑定</button>
      <button onclick=\"retryRecognition('${row.lead_id}')\">重试识别</button>
      <button onclick=\"rejectManualReview('${row.lead_id}')\">驳回提交</button>
    </td>
  </tr>`;
}
function notificationRowHtml(row) {
  return `<tr>
    <td>${row.created_at ?? ''}</td>
    <td>${row.notification_type ?? ''}</td>
    <td>${row.mobile ?? ''}</td>
    <td>${row.yw_id ?? ''}</td>
    <td>${row.write_result ?? ''}</td>
    <td>${row.reason ?? ''}</td>
    <td>${row.is_read ? '已读' : `<button onclick=\"markNotificationRead('${row.notification_id}')\">标记已读</button>`}</td>
  </tr>`;
}
function approvalBatchRowHtml(row) {
  return `<tr>
    <td>${row.registration_group ?? ''}</td>
    <td>${row.ready ? 'yes' : 'no'}</td>
    <td>${row.release_count ?? 0}</td>
    <td>${row.reason_code ?? ''}</td>
  </tr>`;
}
async function reloadNotifications() {
  const status = document.getElementById('notificationStatus').value;
  const query = document.getElementById('notificationQuery').value;
  const params = new URLSearchParams();
  if (status) params.set('status', status);
  if (query) params.set('query', query);
  const url = '/api/ops/operator-notifications' + (params.toString() ? `?${params.toString()}` : '');
  const notifications = await loadJson(url);
  document.getElementById('notificationRows').innerHTML = notifications.rows.map(notificationRowHtml).join('');
}
async function markNotificationRead(notificationId) {
  await loadJson(`/api/ops/operator-notifications/${notificationId}/read`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({read_by: 'ops_console'})
  });
  await reloadNotifications();
}
function showToast(message, type='success') {
  const toast = document.getElementById('manualReviewToast');
  toast.textContent = message;
  toast.className = `toast ${type}`;
  toast.style.display = 'block';
  clearTimeout(window.__manualReviewToastTimer);
  window.__manualReviewToastTimer = setTimeout(() => {
    toast.style.display = 'none';
  }, 2400);
}
async function reloadManualReviewQueue() {
  const summary = await loadJson('/api/ops/dashboard/summary');
  document.getElementById('manualReviewCount').textContent = summary.manual_review_count;
  document.getElementById('bindQueueCount').textContent = summary.bind_queue_count;
  document.getElementById('groupQueueCount').textContent = summary.group_queue_count;
  document.getElementById('bindSuccessCount').textContent = summary.bind_success_count;
  document.getElementById('parserConflictCount').textContent = summary.parser_conflict_count;
  document.getElementById('correctionCount').textContent = summary.correction_count;

  const manualReviewQueue = await loadJson('/api/ops/manual-review-queue');
  document.getElementById('manualReviewRows').innerHTML = manualReviewQueue.rows.map(manualReviewRowHtml).join('');

  const bindQueue = await loadJson('/api/ops/bind-queue');
  document.getElementById('bindQueueRows').innerHTML = bindQueue.rows.map(bindRowHtml).join('');

  const groupQueue = await loadJson('/api/ops/group-queue');
  document.getElementById('groupQueueRows').innerHTML = groupQueue.rows.map(groupRowHtml).join('');
}
async function submitManualReviewDecision(leadId, payload) {
  try {
    const result = await loadJson(`/api/ops/manual-review/${leadId}/resolve`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    await reloadManualReviewQueue();
    await reloadNotifications();
    showToast(`复核已提交：${result.decision} → ${result.next_action || 'manual_followup'}`, 'success');
    return result;
  } catch (err) {
    showToast(`复核提交失败：${err.message}`, 'error');
    throw err;
  }
}
function getManualReviewFieldValue(leadId, field) {
  const el = document.getElementById(`manualReview${field}-${leadId}`);
  return el ? el.value.trim() : '';
}
async function approveManualReview(leadId) {
  const accountId = getManualReviewFieldValue(leadId, 'FieldValue');
  if (!accountId) {
    alert('请先填写人工确认后的账号ID');
    return;
  }
  await submitManualReviewDecision(leadId, {
    decision: 'approve_bind',
    reviewed_by: 'ops_console',
    review_note: getManualReviewFieldValue(leadId, 'Note') || 'ops console approve bind',
    account_id: accountId,
    app_name: getManualReviewFieldValue(leadId, 'AppName') || undefined,
    dept_name: getManualReviewFieldValue(leadId, 'DeptName') || undefined,
    registration_group: getManualReviewFieldValue(leadId, 'RegistrationGroup') || undefined,
    submitted_at: new Date().toISOString()
  });
}
async function retryRecognition(leadId) {
  await submitManualReviewDecision(leadId, {
    decision: 'request_recognition_retry',
    reviewed_by: 'ops_console',
    review_note: getManualReviewFieldValue(leadId, 'Note') || 'ops console requested recognition retry',
    submitted_at: new Date().toISOString()
  });
}
async function rejectManualReview(leadId) {
  await submitManualReviewDecision(leadId, {
    decision: 'reject_submission',
    reviewed_by: 'ops_console',
    review_note: getManualReviewFieldValue(leadId, 'Note') || 'ops console rejected submission',
    submitted_at: new Date().toISOString()
  });
}
async function init() {
  const nextAction = await loadJson('/api/ops/next-action');
  document.getElementById('nextActionHint').textContent = nextAction.kind === 'none'
    ? '当前没有待处理任务'
    : `优先处理：${nextAction.kind} · lead ${nextAction.row?.lead_id || ''}`;
  document.getElementById('nextActionJson').textContent = JSON.stringify(nextAction, null, 2);

  const batchQueue = await loadJson('/api/ops/approval-batch-queue');
  document.getElementById('registrationBatchRows').innerHTML = batchQueue.registration_groups.map(approvalBatchRowHtml).join('');
  document.getElementById('officialBatchRows').innerHTML = batchQueue.official_groups.map(approvalBatchRowHtml).join('');

  await reloadManualReviewQueue();

  await reloadNotifications();
}
init().catch(err => {
  console.error(err);
  alert('加载运营操作台失败：' + err.message);
});
</script>
  </div>
</body>
</html>
"""


class LeadUpsertRequest(BaseModel):
    trace_id: str
    source_platform: str
    source_campaign: Optional[str] = None
    source_page_id: str
    country: str
    area_code: int
    mobile: str
    yw_id: Optional[str] = None
    app_name: Optional[str] = None
    dept_name: Optional[str] = None
    pendaftaran_group: Optional[str] = None
    inviter_id: Optional[str] = None
    occurred_at: Optional[str] = None
    parser_confidence: Optional[float] = None
    parser_missing_fields: list[str] = Field(default_factory=list)
    parser_conflicts: list[str] = Field(default_factory=list)
    parser_raw_text: Optional[str] = None
    parser_raw_ocr_text: Optional[str] = None
    parser_version: str = 'manual_cs_parser_v2'
    parser_status: str = 'unknown'
    review_reason_codes: list[str] = Field(default_factory=list)
    routing_decision: Optional[str] = None
    recommended_next_action: Optional[str] = None
    review_status: str = 'not_needed'


class EventCollectRequest(BaseModel):
    trace_id: str
    lead_id: Optional[str] = None
    event_type: str
    event_source: str
    event_value: Optional[str] = None
    page_id: Optional[str] = None
    session_id: Optional[str] = None
    operator_id: Optional[str] = None
    operator_name: Optional[str] = None
    raw_payload: Dict[str, Any] = Field(default_factory=dict)
    happened_at: Optional[str] = None


class TaskCreateRequest(BaseModel):
    lead_id: str
    task_type: str
    priority: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str
    created_by: str
    created_at: str


class TaskResultRequest(BaseModel):
    status: str
    result_code: Optional[str] = None
    result_reason: Optional[str] = None
    toast_text: Optional[str] = None
    evidence_url: Optional[str] = None
    retry_count: int = 0
    executor_type: Optional[str] = None
    executor_id: Optional[str] = None
    finished_at: str
    raw_result: Dict[str, Any] = Field(default_factory=dict)


class CustomerSyncRequest(BaseModel):
    lead_id: str
    task_id: str
    yw_id: Optional[str] = None
    mobile: str
    area_code: int
    crm_patch: Dict[str, Any] = Field(default_factory=dict)
    sync_mode: str = "upsert"


class AccountSubmissionRequest(BaseModel):
    lead_id: str
    task_id: Optional[str] = None
    submission_type: str
    account_id: Optional[str] = None
    account_id_type: Optional[str] = None
    file_url: Optional[str] = None
    file_type: Optional[str] = None
    source_channel: Optional[str] = None
    source_bot_app_id: Optional[str] = None
    source_message_id: Optional[str] = None
    source_chat_id: Optional[str] = None
    submitted_by: Optional[str] = None
    submitted_at: str
    remark: Optional[str] = None


class ManualCsSubmissionRequest(BaseModel):
    mobile: str
    registration_group: str
    app_name: str
    dept_name: str
    invite_code: Optional[str] = None
    app_name_explicit: bool = False
    dept_name_explicit: bool = False
    submission_type: str
    account_id: Optional[str] = None
    file_url: Optional[str] = None
    file_type: Optional[str] = None
    image_ocr_text: Optional[str] = None
    submitted_by: str
    source_channel: str = "manual_cs_lark"
    source_bot_app_id: Optional[str] = None
    source_message_id: Optional[str] = None
    source_chat_id: Optional[str] = None
    remark: Optional[str] = None
    submitted_at: str


class ManualReviewResolveRequest(BaseModel):
    decision: str
    reviewed_by: str
    review_note: Optional[str] = None
    account_id: Optional[str] = None
    app_name: Optional[str] = None
    dept_name: Optional[str] = None
    registration_group: Optional[str] = None
    submitted_at: str


class RecognitionResultRequest(BaseModel):
    status: str
    recognized_account_id: Optional[str] = None
    result_code: Optional[str] = None
    result_reason: Optional[str] = None
    finished_at: str
    raw_result: Dict[str, Any] = Field(default_factory=dict)


class BindCheckResultRequest(BaseModel):
    status: str
    result_code: Optional[str] = None
    result_reason: Optional[str] = None
    finished_at: str
    raw_result: Dict[str, Any] = Field(default_factory=dict)


class GroupJoinResultRequest(BaseModel):
    status: str
    result_code: Optional[str] = None
    result_reason: Optional[str] = None
    finished_at: str
    raw_result: Dict[str, Any] = Field(default_factory=dict)


class VoucherAttachRequest(BaseModel):
    image_path: str
    remark_suffix: Optional[str] = None


class RegistrationGroupApprovalBatchRequest(BaseModel):
    registration_group: str
    registration_group_name: Optional[str] = None
    approved_count: int
    approved_by: Optional[str] = None
    approved_by_name: Optional[str] = None
    source_platform: Optional[str] = None
    source_campaign: Optional[str] = None
    source_adset: Optional[str] = None
    source_ad: Optional[str] = None
    approved_at: str
    area: str = "Indonesia"
    remark: Optional[str] = None
    approval_run_id: Optional[str] = None


class RegistrationGroupApprovalDecisionRequest(BaseModel):
    registration_group: str
    decision: str = 'approve'
    decided_at: str
    decided_by: Optional[str] = None
    decided_by_name: Optional[str] = None
    source_platform: Optional[str] = None
    source_campaign: Optional[str] = None
    source_adset: Optional[str] = None
    source_ad: Optional[str] = None
    target_name_hint: Optional[str] = None
    target_phone_hint: Optional[str] = None
    approved_count: int = 1
    area: str = 'Indonesia'
    remark: Optional[str] = None
    force_immediate: bool = False
    expected_pending_count: Optional[int] = None
    expected_member_count: Optional[int] = None
    expected_requester_ids: Optional[List[str]] = None
    expected_requesters: Optional[List[Dict[str, Any]]] = None


class OfficialGroupApprovalCheckRequest(BaseModel):
    lead_id: str
    target_group: str
    checked_at: str
    checked_by: Optional[str] = None
    checked_by_name: Optional[str] = None
    source_platform: Optional[str] = None
    source_campaign: Optional[str] = None
    source_adset: Optional[str] = None
    source_ad: Optional[str] = None
    target_phone_hint: Optional[str] = None
    target_requester_id: Optional[str] = None
    remark: Optional[str] = None


class OfficialGroupApprovalDecisionRequest(BaseModel):
    lead_id: str
    target_group: str
    decision: str = 'approve'
    decided_at: str
    decided_by: Optional[str] = None
    decided_by_name: Optional[str] = None
    source_platform: Optional[str] = None
    source_campaign: Optional[str] = None
    source_adset: Optional[str] = None
    source_ad: Optional[str] = None
    target_name_hint: Optional[str] = None
    target_phone_hint: Optional[str] = None
    target_requester_id: Optional[str] = None
    remark: Optional[str] = None


class OfficialGroupApprovalRetryRequest(BaseModel):
    target_group: str
    decided_at: str
    decided_by: Optional[str] = None
    decided_by_name: Optional[str] = None
    source_platform: Optional[str] = None
    source_campaign: Optional[str] = None
    source_adset: Optional[str] = None
    source_ad: Optional[str] = None
    remark: Optional[str] = None


class OfficialGroupBatchRunRequest(BaseModel):
    decided_at: str
    decided_by: Optional[str] = None
    decided_by_name: Optional[str] = None
    source_platform: Optional[str] = None
    source_campaign: Optional[str] = None
    source_adset: Optional[str] = None
    source_ad: Optional[str] = None
    remark: Optional[str] = None
    limit_groups: int = 10
    limit_leads_per_group: Optional[int] = None
    allow_crm_only_test_match: bool = False


class NotificationReadRequest(BaseModel):
    read_by: Optional[str] = None


class IntakeBotPresetUpdateRequest(BaseModel):
    app_id: Optional[str] = None
    robot_name: Optional[str] = None
    default_app: str
    default_guild: str


class GuildExecutorUpdateRequest(BaseModel):
    backend_url: str
    login_username: str
    password_secret_ref: Optional[str] = None
    proxy_url: Optional[str] = None
    proxy_region: Optional[str] = None
    proxy_type: Optional[str] = None
    enabled: bool = True
    browser_profile_key: Optional[str] = None
    bind_concurrency: int = 1
    request_timeout_seconds: int = 30
    notes: Optional[str] = None


class ProductionOpsDaemonConfigUpdateRequest(BaseModel):
    enabled: bool = False
    registration_group: Optional[str] = None
    api_base_url: Optional[str] = None
    worker_base_url: Optional[str] = None
    interval_seconds: float = 20.0
    notify_chat_id: Optional[str] = None
    area: Optional[str] = None
    remark: Optional[str] = None
    approved_count: int = 1
    auto_recover_worker: bool = True


class ApprovalScheduleWindowRequest(BaseModel):
    start: str
    end: str


class ApprovalGroupBindingRequest(BaseModel):
    link: Optional[str] = None
    group_name: Optional[str] = None
    area: Optional[str] = None
    notify_profile_name: Optional[str] = None
    enabled: Optional[bool] = True
    registration_group: Optional[str] = None
    group_id: Optional[str] = None
    approval_count_threshold: Optional[int] = None
    approval_timeout_minutes: Optional[int] = None
    auto_recover_worker: Optional[bool] = None
    schedule_windows: list[ApprovalScheduleWindowRequest] = []


class WhatsAppApprovalAccountUpdateRequest(BaseModel):
    account_name: str
    responsible_type: str
    group_links: list[str] = []
    group_link_bindings: list[ApprovalGroupBindingRequest] = []
    area: Optional[str] = None
    notify_profile_name: Optional[str] = None
    approval_rule: Optional[str] = None
    approval_count_threshold: Optional[int] = None
    approval_timeout_minutes: Optional[int] = None
    auto_recover_worker: bool = True
    schedule_windows: list[ApprovalScheduleWindowRequest] = []
    enabled: bool = True
    notes: Optional[str] = None


class WhatsAppApprovalAreaOptionsUpdateRequest(BaseModel):
    options: list[str]


class SubmissionResubmitRequest(BaseModel):
    corrected_by: str
    submitted_at: str
    mobile: Optional[str] = None
    registration_group: Optional[str] = None
    invite_code: Optional[str] = None
    account_id: Optional[str] = None
    remark: Optional[str] = None


GUILD_EXECUTOR_PROXY_REGION_OPTIONS: list[dict[str, str]] = [
    {'value': '北京', 'label': '北京'},
    {'value': '上海', 'label': '上海'},
    {'value': '广州', 'label': '广州'},
    {'value': '深圳', 'label': '深圳'},
    {'value': '杭州', 'label': '杭州'},
    {'value': '南京', 'label': '南京'},
    {'value': '苏州', 'label': '苏州'},
    {'value': '成都', 'label': '成都'},
    {'value': '重庆', 'label': '重庆'},
    {'value': '武汉', 'label': '武汉'},
    {'value': '西安', 'label': '西安'},
    {'value': '郑州', 'label': '郑州'},
    {'value': '长沙', 'label': '长沙'},
    {'value': '厦门', 'label': '厦门'},
    {'value': '福州', 'label': '福州'},
]
GUILD_EXECUTOR_PROXY_REGION_VALUES: set[str] = {item['value'] for item in GUILD_EXECUTOR_PROXY_REGION_OPTIONS}
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_OPS_DAEMON_LABEL = 'com.chauncey.mcn.production-ops-daemon'
PRODUCTION_OPS_DAEMON_LAUNCH_AGENT_PATH = Path.home() / 'Library' / 'LaunchAgents' / f'{PRODUCTION_OPS_DAEMON_LABEL}.plist'
PRODUCTION_OPS_DAEMON_ENV_PATH = PROJECT_ROOT / 'data' / 'production_ops_daemon.env'
PRODUCTION_OPS_DAEMON_STATUS_PATH = PROJECT_ROOT / 'data' / 'production_ops_daemon_status.json'
PRODUCTION_OPS_DAEMON_INSTALL_SCRIPT = PROJECT_ROOT / 'scripts' / 'install_production_ops_daemon_launch_agent.sh'
PRODUCTION_OPS_DAEMON_UNINSTALL_SCRIPT = PROJECT_ROOT / 'scripts' / 'uninstall_production_ops_daemon_launch_agent.sh'
WHATSAPP_APPROVAL_DEFAULT_COUNT_THRESHOLD = 30
WHATSAPP_APPROVAL_DEFAULT_TIMEOUT_MINUTES = 30
WHATSAPP_APPROVAL_WORKER_ROOT = PROJECT_ROOT / 'webjs-approval-worker'
WHATSAPP_APPROVAL_WORKER_AUTH_ACCOUNTS_DIR=WHATSAPP_APPROVAL_WORKER_ROOT / '.wwebjs_auth_accounts'
WHATSAPP_APPROVAL_WORKER_RUNTIME_DIR = PROJECT_ROOT / 'data' / 'whatsapp_approval_worker_runtimes'
WHATSAPP_APPROVAL_WORKER_LOG_DIR = PROJECT_ROOT / 'logs' / 'whatsapp_approval_workers'
WHATSAPP_APPROVAL_WORKER_RESTART_SCRIPT = PROJECT_ROOT / 'scripts' / 'restart_registration_group_webjs_worker.sh'


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return default
    return normalized if normalized > 0 else default


def _legacy_approval_thresholds(rule: str) -> tuple[int, int]:
    normalized = str(rule or '').strip()
    if normalized == 'timeout_30m':
        return WHATSAPP_APPROVAL_DEFAULT_COUNT_THRESHOLD, WHATSAPP_APPROVAL_DEFAULT_TIMEOUT_MINUTES
    return WHATSAPP_APPROVAL_DEFAULT_COUNT_THRESHOLD, WHATSAPP_APPROVAL_DEFAULT_TIMEOUT_MINUTES


def _approval_condition_text(count_threshold: int, timeout_minutes: int) -> str:
    return f'满{count_threshold}人或满{timeout_minutes}分钟放行（满足其一即可）'


def _normalize_schedule_windows_payload(items: list[Any]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        start = str(item.get('start') or '').strip()
        end = str(item.get('end') or '').strip()
        if not re.fullmatch(r'\d{2}:\d{2}', start) or not re.fullmatch(r'\d{2}:\d{2}', end):
            continue
        normalized.append({'start': start, 'end': end})
    return normalized


WHATSAPP_APPROVAL_DEFAULT_AREA_OPTIONS: list[dict[str, str]] = [
    {'value': 'Indonesia', 'label': 'Indonesia'},
    {'value': 'Brazil', 'label': 'Brazil'},
    {'value': 'Mexico', 'label': 'Mexico'},
]


def _normalize_area_options(options: list[str]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in options:
        value = str(item or '').strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append({'value': value, 'label': value})
    return normalized


def _normalize_group_link_bindings(bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in bindings or []:
        if not isinstance(item, dict):
            continue
        link = str(item.get('link') or '').strip()
        area = str(item.get('area') or '').strip()
        if not link:
            continue
        key = (link, area)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({
            'link': link,
            'group_name': str(item.get('group_name') or '').strip(),
            'area': area,
            'notify_profile_name': str(item.get('notify_profile_name') or '').strip(),
            'enabled': False if item.get('enabled') is False else True,
            'registration_group': str(item.get('registration_group') or '').strip(),
            'group_id': str(item.get('group_id') or '').strip(),
            'approval_count_threshold': item.get('approval_count_threshold'),
            'approval_timeout_minutes': item.get('approval_timeout_minutes'),
            'auto_recover_worker': item.get('auto_recover_worker'),
            'schedule_windows': item.get('schedule_windows') if isinstance(item.get('schedule_windows'), list) else [],
        })
    return normalized


def _preferred_group_binding(bindings: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [dict(item or {}) for item in (bindings or []) if isinstance(item, dict)]
    if not rows:
        return {}
    for item in rows:
        if item.get('enabled') is not False:
            return item
    return rows[0]


WHATSAPP_APPROVAL_AREA_OPTIONS: list[dict[str, str]] = _normalize_area_options(
    [item['value'] for item in WHATSAPP_APPROVAL_DEFAULT_AREA_OPTIONS]
)
WHATSAPP_APPROVAL_AREA_VALUES: set[str] = {item['value'] for item in WHATSAPP_APPROVAL_AREA_OPTIONS}


class ApprovalBatchEvaluateRequest(BaseModel):
    approval_type: str
    registration_group: str
    pending_count: int
    oldest_pending_at: Optional[str] = None
    now: str
    batch_size: Optional[int] = None
    timeout_minutes: Optional[int] = None


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._memory_conn: Optional[sqlite3.Connection] = None
        self._ensure_parent()
        self._init_schema()

    def _ensure_parent(self) -> None:
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def _configure_connection(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout = 30000')
        if self.db_path != ":memory:":
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')

    def connect(self) -> sqlite3.Connection:
        if self.db_path == ":memory:":
            if self._memory_conn is None:
                self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False, timeout=30.0)
                self._configure_connection(self._memory_conn)
            return self._memory_conn
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        self._configure_connection(conn)
        return conn

    def _init_schema(self) -> None:
        conn = self.connect()
        conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS leads (
                    lead_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    source_platform TEXT NOT NULL,
                    source_campaign TEXT,
                    source_page_id TEXT NOT NULL,
                    country TEXT NOT NULL,
                    area_code INTEGER NOT NULL,
                    mobile TEXT NOT NULL,
                    yw_id TEXT,
                    app_name TEXT,
                    dept_name TEXT,
                    pendaftaran_group TEXT,
                    inviter_id TEXT,
                    parser_confidence REAL,
                    parser_missing_fields TEXT NOT NULL DEFAULT '[]',
                    parser_conflicts TEXT NOT NULL DEFAULT '[]',
                    parser_raw_text TEXT,
                    parser_raw_ocr_text TEXT,
                    parser_version TEXT NOT NULL DEFAULT 'manual_cs_parser_v2',
                    parser_status TEXT NOT NULL DEFAULT 'unknown',
                    review_reason_codes TEXT NOT NULL DEFAULT '[]',
                    routing_decision TEXT,
                    recommended_next_action TEXT,
                    review_status TEXT NOT NULL DEFAULT 'not_needed',
                    review_notes TEXT,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    correction_count INTEGER NOT NULL DEFAULT 0,
                    current_status TEXT NOT NULL,
                    matched_customer_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(area_code, mobile)
                );

                CREATE TABLE IF NOT EXISTS customer_projection (
                    customer_id TEXT PRIMARY KEY,
                    lead_id TEXT NOT NULL,
                    mobile TEXT NOT NULL,
                    area_code INTEGER NOT NULL,
                    yw_id TEXT,
                    pendaftaran_group TEXT,
                    payment_status TEXT,
                    user_quality TEXT,
                    remark TEXT,
                    join_group TEXT,
                    file_url TEXT,
                    pz_status INTEGER,
                    updated_at TEXT NOT NULL,
                    UNIQUE(area_code, mobile)
                );

                CREATE TABLE IF NOT EXISTS lead_events (
                    event_id TEXT PRIMARY KEY,
                    lead_id TEXT,
                    trace_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_source TEXT NOT NULL,
                    event_value TEXT,
                    page_id TEXT,
                    session_id TEXT,
                    operator_id TEXT,
                    operator_name TEXT,
                    raw_payload TEXT NOT NULL,
                    happened_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS automation_tasks (
                    task_id TEXT PRIMARY KEY,
                    lead_id TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_code TEXT,
                    result_reason TEXT,
                    toast_text TEXT,
                    evidence_url TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    executor_type TEXT,
                    executor_id TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    raw_result TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS sync_logs (
                    sync_log_id TEXT PRIMARY KEY,
                    lead_id TEXT,
                    task_id TEXT,
                    sync_type TEXT NOT NULL,
                    target_system TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_snapshot TEXT NOT NULL,
                    response_snapshot TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS account_submissions (
                    submission_id TEXT PRIMARY KEY,
                    lead_id TEXT NOT NULL,
                    task_id TEXT,
                    submission_type TEXT NOT NULL,
                    account_id TEXT,
                    account_id_type TEXT,
                    file_url TEXT,
                    file_type TEXT,
                    source_channel TEXT,
                    submitted_by TEXT,
                    recognition_status TEXT NOT NULL DEFAULT 'not_needed',
                    recognized_account_id TEXT,
                    recognition_raw TEXT NOT NULL DEFAULT '{}',
                    submitted_at TEXT NOT NULL,
                    remark TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bind_check_jobs (
                    job_id TEXT PRIMARY KEY,
                    lead_id TEXT NOT NULL,
                    submission_id TEXT,
                    account_id TEXT NOT NULL,
                    guild_code TEXT,
                    check_source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_code TEXT,
                    result_reason TEXT,
                    raw_result TEXT NOT NULL DEFAULT '{}',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    scheduled_at TEXT NOT NULL,
                    finished_at TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS group_join_jobs (
                    job_id TEXT PRIMARY KEY,
                    lead_id TEXT NOT NULL,
                    submission_id TEXT,
                    account_id TEXT,
                    target_group TEXT,
                    join_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_code TEXT,
                    result_reason TEXT,
                    raw_result TEXT NOT NULL DEFAULT '{}',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    scheduled_at TEXT NOT NULL,
                    finished_at TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS guild_executors (
                    guild_name TEXT PRIMARY KEY,
                    backend_url TEXT NOT NULL,
                    login_username TEXT NOT NULL,
                    password_secret_ref TEXT,
                    proxy_url TEXT,
                    proxy_region TEXT,
                    proxy_type TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    browser_profile_key TEXT,
                    bind_concurrency INTEGER NOT NULL DEFAULT 1,
                    request_timeout_seconds INTEGER NOT NULL DEFAULT 30,
                    notes TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS production_ops_daemon_configs (
                    config_name TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    registration_group TEXT NOT NULL,
                    api_base_url TEXT NOT NULL,
                    worker_base_url TEXT NOT NULL,
                    interval_seconds REAL NOT NULL DEFAULT 20,
                    notify_chat_id TEXT,
                    area TEXT,
                    remark TEXT,
                    approved_count INTEGER NOT NULL DEFAULT 1,
                    auto_recover_worker INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS whatsapp_approval_accounts (
                    account_key TEXT PRIMARY KEY,
                    account_name TEXT NOT NULL,
                    responsible_type TEXT NOT NULL,
                    group_links TEXT NOT NULL DEFAULT '[]',
                    area TEXT,
                    notify_profile_name TEXT,
                    approval_rule TEXT NOT NULL DEFAULT 'count_30',
                    approval_count_threshold INTEGER NOT NULL DEFAULT 30,
                    approval_timeout_minutes INTEGER NOT NULL DEFAULT 30,
                    auto_recover_worker INTEGER NOT NULL DEFAULT 1,
                    schedule_windows TEXT NOT NULL DEFAULT '[]',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    verification_status TEXT NOT NULL DEFAULT 'pending_verification',
                    notes TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS whatsapp_approval_area_options (
                    option_key TEXT PRIMARY KEY,
                    options_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lead_status_history (
                    history_id TEXT PRIMARY KEY,
                    lead_id TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    trigger_source TEXT NOT NULL,
                    trigger_event_id TEXT,
                    trigger_task_id TEXT,
                    operator_id TEXT,
                    operator_name TEXT,
                    remark TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS operator_notifications (
                    notification_id TEXT PRIMARY KEY,
                    lead_id TEXT NOT NULL,
                    notification_type TEXT NOT NULL,
                    mobile TEXT NOT NULL,
                    yw_id TEXT,
                    write_result TEXT NOT NULL,
                    reason TEXT,
                    is_read INTEGER NOT NULL DEFAULT 0,
                    read_at TEXT,
                    read_by TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS manual_review_history (
                    review_id TEXT PRIMARY KEY,
                    lead_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reviewed_by TEXT NOT NULL,
                    review_note TEXT,
                    snapshot_before TEXT NOT NULL DEFAULT '{}',
                    snapshot_after TEXT NOT NULL DEFAULT '{}',
                    created_task_id TEXT,
                    submitted_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lead_corrections (
                    correction_id TEXT PRIMARY KEY,
                    lead_id TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    corrected_by TEXT NOT NULL,
                    review_id TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS intake_bot_presets (
                    profile_name TEXT PRIMARY KEY,
                    app_id TEXT,
                    robot_name TEXT,
                    default_app TEXT,
                    default_guild TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS crm_option_cache (
                    option_type TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    row_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (option_type, display_name)
                );

                CREATE TABLE IF NOT EXISTS ingress_events (
                    event_id TEXT PRIMARY KEY,
                    ingress_type TEXT NOT NULL,
                    source_key TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_snapshot TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    processed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS ingress_jobs (
                    job_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS registration_group_approval_batch_runs (
                    approval_run_id TEXT PRIMARY KEY,
                    sync_log_id TEXT,
                    status TEXT NOT NULL,
                    request_snapshot TEXT NOT NULL,
                    response_snapshot TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS operator_audit_log (
                    audit_id TEXT PRIMARY KEY,
                    lead_id TEXT,
                    ingress_event_id TEXT,
                    event_type TEXT NOT NULL,
                    event_source TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
        for statement in [
            "ALTER TABLE leads ADD COLUMN occurred_at TEXT",
            "ALTER TABLE leads ADD COLUMN parser_confidence REAL",
            "ALTER TABLE leads ADD COLUMN parser_missing_fields TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE leads ADD COLUMN parser_conflicts TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE leads ADD COLUMN parser_raw_text TEXT",
            "ALTER TABLE leads ADD COLUMN parser_raw_ocr_text TEXT",
            "ALTER TABLE leads ADD COLUMN parser_version TEXT NOT NULL DEFAULT 'manual_cs_parser_v2'",
            "ALTER TABLE leads ADD COLUMN parser_status TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE leads ADD COLUMN review_reason_codes TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE leads ADD COLUMN routing_decision TEXT",
            "ALTER TABLE leads ADD COLUMN recommended_next_action TEXT",
            "ALTER TABLE leads ADD COLUMN review_status TEXT NOT NULL DEFAULT 'not_needed'",
            "ALTER TABLE leads ADD COLUMN review_notes TEXT",
            "ALTER TABLE leads ADD COLUMN reviewed_by TEXT",
            "ALTER TABLE leads ADD COLUMN reviewed_at TEXT",
            "ALTER TABLE leads ADD COLUMN correction_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE leads ADD COLUMN crm_verified_payload TEXT",
            "ALTER TABLE leads ADD COLUMN crm_verified_app_name TEXT",
            "ALTER TABLE leads ADD COLUMN crm_verified_dept_name TEXT",
            "ALTER TABLE leads ADD COLUMN crm_verified_registration_group TEXT",
            "ALTER TABLE leads ADD COLUMN crm_verified_official_group TEXT",
            "ALTER TABLE leads ADD COLUMN crm_verified_at TEXT",
            "ALTER TABLE operator_notifications ADD COLUMN is_read INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE operator_notifications ADD COLUMN read_at TEXT",
            "ALTER TABLE operator_notifications ADD COLUMN read_by TEXT",
            "ALTER TABLE intake_bot_presets ADD COLUMN robot_name TEXT",
            "ALTER TABLE automation_tasks ADD COLUMN started_at TEXT",
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN area TEXT",
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN notify_profile_name TEXT",
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN approval_rule TEXT NOT NULL DEFAULT 'count_30'",
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN approval_count_threshold INTEGER NOT NULL DEFAULT 30",
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN approval_timeout_minutes INTEGER NOT NULL DEFAULT 30",
            "ALTER TABLE whatsapp_approval_accounts ADD COLUMN auto_recover_worker INTEGER NOT NULL DEFAULT 1",
        ]:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass
        for statement in [
            "CREATE INDEX IF NOT EXISTS idx_sync_logs_lead_created_at ON sync_logs (lead_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_account_submissions_lead_created_at ON account_submissions (lead_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_operator_notifications_lead_created_at ON operator_notifications (lead_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_operator_notifications_unread ON operator_notifications (is_read, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_automation_tasks_lead_type_status ON automation_tasks (lead_id, task_type, status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_ingress_events_status_created_at ON ingress_events (status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_ingress_jobs_status_available_at ON ingress_jobs (status, available_at)",
            "CREATE INDEX IF NOT EXISTS idx_registration_group_approval_batch_runs_updated_at ON registration_group_approval_batch_runs (updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_operator_audit_log_lead_created_at ON operator_audit_log (lead_id, created_at)",
        ]:
            conn.execute(statement)
        conn.commit()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_datetime(value: str) -> datetime:
    text = str(value or '').strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def create_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _run_registration_group_executor_warmup(executor: Any) -> None:
    try:
        executor.warmup()
    except Exception as exc:
        print(f'Registration group executor warmup degraded at startup: {exc}')


def _schedule_registration_group_executor_warmup(executor: Any) -> str:
    if executor is None or not hasattr(executor, 'warmup') or not callable(getattr(executor, 'warmup')):
        return 'unsupported'
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _run_registration_group_executor_warmup(executor)
        return 'inline'

    thread = threading.Thread(
        target=_run_registration_group_executor_warmup,
        args=(executor,),
        name='registration-group-executor-warmup',
        daemon=True,
    )
    thread.start()
    return 'threaded_deferred_inside_asyncio_loop'


class LiveLarkReplyAdapter:
    def __init__(self, *, app_id: str, app_secret: str, domain: str = 'lark') -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = 'https://open.larksuite.com' if domain == 'lark' else 'https://open.feishu.cn'
        self._tenant_access_token: Optional[str] = None

    def _normalize_text_markup(self, text: str) -> str:
        normalized = str(text or '')
        normalized = re.sub(r'\*\*(.+?)\*\*', lambda m: f"<b>{m.group(1)}</b>", normalized)
        return normalized

    def _get_tenant_access_token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token
        response = requests.post(
            f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        if body.get('code') != 0:
            raise RuntimeError(f"tenant_access_token failed: {body}")
        self._tenant_access_token = body['tenant_access_token']
        return self._tenant_access_token

    def _post_im_message(self, *, url: str, payload: dict) -> dict:
        token = self._get_tenant_access_token()
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        if body.get('code') != 0:
            raise RuntimeError(f"im_message failed: {body}")
        return body

    def reply_text(self, *, message_id: str, text: str) -> dict:
        normalized_text = self._normalize_text_markup(text)
        return self._post_im_message(
            url=f"{self.base_url}/open-apis/im/v1/messages/{message_id}/reply",
            payload={"msg_type": "text", "content": json.dumps({"text": normalized_text}, ensure_ascii=False)},
        )

    def send_text(self, *, chat_id: str, text: str) -> dict:
        normalized_text = self._normalize_text_markup(text)
        return self._post_im_message(
            url=f"{self.base_url}/open-apis/im/v1/messages?receive_id_type=chat_id",
            payload={"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": normalized_text}, ensure_ascii=False)},
        )


class Service:
    def __init__(self, db: Database, crm_adapter: Any = None, ocr_adapter: Any = None, lark_media_adapter: Any = None, lark_reply_adapter: Any = None, lark_reply_adapter_by_app_id: Optional[Dict[str, Any]] = None, media_cache_dir: Optional[str] = None, lark_default_app_name: Optional[str] = None, lark_default_dept_name: Optional[str] = None, current_lark_app_id: Optional[str] = None, auto_bind_simulation: bool = False, bind_simulator: Any = None, real_bind_executor: Any = None, registration_group_approval_executor: Any = None, official_group_approval_executor: Any = None, official_group_target_map: Optional[Dict[str, str]] = None, auto_bind_simulation_success_rate: float = 0.5, auto_bind_simulation_seed: Optional[int] = None, crm_base_url: Optional[str] = None, crm_username: Optional[str] = None, crm_login_error: Optional[str] = None, ingress_async_default: bool = False, ingress_worker_enabled: bool = False, ingress_worker_poll_interval: float = 0.5, ingress_worker_count: int = 1, ingress_rate_limit_per_minute: int = 600, external_call_rate_limit_per_minute: int = 300, require_invite_code: bool = False, crm_retry_delays_seconds: Optional[List[int]] = None, crm_retry_max_attempts: int = 3, bind_retry_max_attempts: int = 2, official_group_approval_webhook_url: Optional[str] = None) -> None:
        self.db = db
        self.crm_adapter = crm_adapter
        self.ocr_adapter = ocr_adapter
        self.lark_media_adapter = lark_media_adapter
        self.lark_reply_adapter = lark_reply_adapter
        self._lark_reply_adapter_by_app_id = {
            str(k).strip(): v for k, v in dict(lark_reply_adapter_by_app_id or {}).items() if str(k).strip() and v is not None
        }
        self._profile_reply_adapter_cache: Dict[str, Any] = {}
        self.lark_default_app_name = lark_default_app_name
        self.lark_default_dept_name = lark_default_dept_name
        self.current_lark_app_id = current_lark_app_id
        self.auto_bind_simulation = auto_bind_simulation
        self.bind_simulator = bind_simulator
        self.real_bind_executor = real_bind_executor
        self.registration_group_approval_executor = registration_group_approval_executor
        self.official_group_approval_executor = official_group_approval_executor
        self.official_group_target_map = {
            str(k).strip().lower(): str(v).strip()
            for k, v in dict(official_group_target_map or {}).items()
            if str(k).strip() and str(v).strip()
        }
        self.official_group_approval_webhook_url = str(official_group_approval_webhook_url or '').strip() or None
        self.auto_bind_simulation_success_rate = max(0.0, min(1.0, float(auto_bind_simulation_success_rate or 0.5)))
        self._bind_random = random.Random(auto_bind_simulation_seed) if auto_bind_simulation_seed is not None else random.Random()
        self.media_cache_dir = Path(media_cache_dir or './data/lark_media_cache')
        self.media_cache_dir.mkdir(parents=True, exist_ok=True)
        self.crm_base_url = crm_base_url
        self.crm_username = crm_username
        self.crm_login_error = crm_login_error
        self.require_invite_code = require_invite_code
        self.crm_retry_delays_seconds = [max(0, int(v)) for v in list(crm_retry_delays_seconds or [5, 10, 20])]
        self.crm_retry_max_attempts = max(1, int(crm_retry_max_attempts or len(self.crm_retry_delays_seconds) or 1))
        self.bind_retry_max_attempts = max(0, int(bind_retry_max_attempts if bind_retry_max_attempts is not None else 2))
        self.ingress_async_default = ingress_async_default
        self.ingress_worker_enabled = ingress_worker_enabled
        self.ingress_worker_poll_interval = max(0.1, float(ingress_worker_poll_interval or 0.5))
        self.ingress_worker_count = max(1, int(ingress_worker_count or 1))
        self.ingress_rate_limiter = TokenBucketRateLimiter(rate=max(1, int(ingress_rate_limit_per_minute or 600)), window_seconds=60)
        self.external_call_rate_limiter = TokenBucketRateLimiter(rate=max(1, int(external_call_rate_limit_per_minute or 300)), window_seconds=60)
        self.reply_circuit_breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=30)
        self.crm_circuit_breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=30)
        self.ocr_circuit_breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=30)
        self._worker_threads: List[threading.Thread] = []
        self._worker_stop = threading.Event()
        self._registration_group_approval_batch_lock = threading.Lock()
        self._task_residue_reconcile_interval_seconds = 60.0
        self._bind_processing_stale_seconds = 900.0
        self._group_join_pending_stale_seconds = 900.0
        self._crm_task_stale_seconds = 900.0
        self._task_residue_last_reconciled_monotonic = 0.0
        self._crm_option_cache: Dict[str, Dict[str, Dict[str, Any]]] = {
            'app': {},
            'guild': {},
        }
        self._load_persisted_crm_option_cache()
        if self.ingress_worker_enabled:
            self._start_ingress_worker()

    def _start_ingress_worker(self) -> None:
        self._worker_threads = [thread for thread in self._worker_threads if thread.is_alive()]
        if len(self._worker_threads) >= self.ingress_worker_count:
            return
        self._worker_stop.clear()
        start_index = len(self._worker_threads)
        for idx in range(start_index, self.ingress_worker_count):
            thread = threading.Thread(target=self._worker_loop, name=f'ingress-worker-{idx + 1}', daemon=True)
            thread.start()
            self._worker_threads.append(thread)

    def process_next_worker_tick(self) -> Optional[Dict[str, Any]]:
        self.reconcile_task_residue()
        return self.process_next_ingress_job() or self.process_next_automation_task()

    def _worker_loop(self) -> None:
        while not self._worker_stop.is_set():
            try:
                processed = self.process_next_worker_tick()
                if not processed:
                    time.sleep(self.ingress_worker_poll_interval)
            except Exception:
                time.sleep(self.ingress_worker_poll_interval)

    @staticmethod
    def _task_status_rank(status: str) -> int:
        normalized = str(status or '').strip().lower()
        if normalized == 'success':
            return 0
        if normalized == 'failed':
            return 1
        if normalized == 'processing':
            return 2
        if normalized == 'pending':
            return 3
        return 4

    @staticmethod
    def _parse_task_payload_dict(raw_payload: Any) -> Dict[str, Any]:
        try:
            parsed = json.loads(raw_payload or '{}')
        except Exception:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _parse_task_raw_result_dict(raw_result: Any) -> Dict[str, Any]:
        try:
            parsed = json.loads(raw_result or '{}')
        except Exception:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    def _finalize_bind_task_residue(
        self,
        conn: sqlite3.Connection,
        *,
        row: Dict[str, Any],
        resolved_status: str,
        result_code: str,
        result_reason: str,
        now_iso: str,
    ) -> None:
        raw_result = self._parse_task_raw_result_dict(row.get('raw_result'))
        raw_result.update({
            'execution_disposition': 'auto_reconciled',
            'auto_reconciled': True,
            'auto_reconcile_reason': result_code,
            'auto_reconciled_at': now_iso,
        })
        conn.execute(
            """
            UPDATE automation_tasks
            SET status = ?, result_code = ?, result_reason = ?, finished_at = ?, raw_result = ?
            WHERE task_id = ?
            """,
            (
                resolved_status,
                result_code,
                result_reason,
                now_iso,
                json.dumps(raw_result, ensure_ascii=False),
                row['task_id'],
            ),
        )
        self._record_audit_event(
            conn,
            event_type='automation_task_residue_reconciled',
            event_source='task_residue_reconciler',
            payload={
                'task_type': 'bind_check',
                'task_id': row['task_id'],
                'lead_id': row['lead_id'],
                'resolved_status': resolved_status,
                'result_code': result_code,
                'result_reason': result_reason,
            },
            lead_id=str(row.get('lead_id') or '').strip() or None,
        )

    def _finalize_group_join_task_residue(
        self,
        conn: sqlite3.Connection,
        *,
        row: Dict[str, Any],
        resolved_status: str,
        result_code: str,
        result_reason: str,
        now_iso: str,
        update_lead_status: Optional[str] = None,
    ) -> None:
        raw_result = self._parse_task_raw_result_dict(row.get('raw_result'))
        payload_dict = self._parse_task_payload_dict(row.get('payload'))
        target_group = str(raw_result.get('target_group') or payload_dict.get('target_group') or row.get('resolved_target_group') or '').strip() or None
        raw_result.update({
            'execution_disposition': 'auto_reconciled',
            'auto_reconciled': True,
            'auto_reconcile_reason': result_code,
            'auto_reconciled_at': now_iso,
        })
        if target_group:
            raw_result.setdefault('target_group', target_group)
        conn.execute(
            """
            UPDATE automation_tasks
            SET status = ?, result_code = ?, result_reason = ?, finished_at = ?, raw_result = ?
            WHERE task_id = ?
            """,
            (
                resolved_status,
                result_code,
                result_reason,
                now_iso,
                json.dumps(raw_result, ensure_ascii=False),
                row['task_id'],
            ),
        )
        if update_lead_status:
            current_status = str(row.get('current_status') or '').strip()
            conn.execute(
                "UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?",
                (update_lead_status, now_iso, row['lead_id']),
            )
            if current_status and current_status != update_lead_status:
                self._record_status_history(
                    conn,
                    lead_id=row['lead_id'],
                    from_status=current_status,
                    to_status=update_lead_status,
                    trigger_type='group_join_auto_reconciled',
                    trigger_source='task_residue_reconciler',
                    trigger_task_id=row['task_id'],
                    remark=result_code,
                )
        self._record_audit_event(
            conn,
            event_type='automation_task_residue_reconciled',
            event_source='task_residue_reconciler',
            payload={
                'task_type': 'group_join',
                'task_id': row['task_id'],
                'lead_id': row['lead_id'],
                'resolved_status': resolved_status,
                'result_code': result_code,
                'result_reason': result_reason,
                'target_group': target_group,
                'update_lead_status': update_lead_status,
            },
            lead_id=str(row.get('lead_id') or '').strip() or None,
        )

    def _finalize_crm_task_residue(
        self,
        conn: sqlite3.Connection,
        *,
        row: Dict[str, Any],
        resolved_status: str,
        result_code: str,
        result_reason: str,
        now_iso: str,
    ) -> None:
        raw_result = self._parse_task_raw_result_dict(row.get('raw_result'))
        raw_result.update({
            'execution_disposition': 'auto_reconciled',
            'auto_reconciled': True,
            'auto_reconcile_reason': result_code,
            'auto_reconciled_at': now_iso,
        })
        conn.execute(
            """
            UPDATE automation_tasks
            SET status = ?, result_code = ?, result_reason = ?, finished_at = ?, raw_result = ?
            WHERE task_id = ?
            """,
            (
                resolved_status,
                result_code,
                result_reason,
                now_iso,
                json.dumps(raw_result, ensure_ascii=False),
                row['task_id'],
            ),
        )
        self._record_audit_event(
            conn,
            event_type='automation_task_residue_reconciled',
            event_source='task_residue_reconciler',
            payload={
                'task_type': str(row.get('task_type') or ''),
                'task_id': row['task_id'],
                'lead_id': row.get('lead_id'),
                'resolved_status': resolved_status,
                'result_code': result_code,
                'result_reason': result_reason,
            },
            lead_id=str(row.get('lead_id') or '').strip() or None,
        )

    def reconcile_task_residue(self, *, force: bool = False) -> Dict[str, Any]:
        now_monotonic = time.monotonic()
        if not force and (now_monotonic - self._task_residue_last_reconciled_monotonic) < self._task_residue_reconcile_interval_seconds:
            return {'attempted': False, 'skipped': True}
        now_iso = utc_now()
        now_dt = parse_iso_datetime(now_iso)
        bind_reconciled = 0
        group_reconciled = 0
        crm_reconciled = 0
        with self.db.connect() as conn:
            bind_rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.lead_id, t.status, t.created_at, t.started_at, t.raw_result,
                       COALESCE(l.current_status, '') AS current_status
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check' AND t.status = 'processing'
                ORDER BY datetime(COALESCE(t.started_at, t.created_at)) ASC, t.task_id ASC
                """
            ).fetchall()]
            for row in bind_rows:
                anchor = str(row.get('started_at') or row.get('created_at') or '').strip()
                if not anchor:
                    continue
                try:
                    age_seconds = max(0.0, (now_dt - parse_iso_datetime(anchor)).total_seconds())
                except Exception:
                    continue
                if age_seconds < self._bind_processing_stale_seconds:
                    continue
                lead_status = str(row.get('current_status') or '').strip().lower()
                if lead_status in {'bind_success', 'group_join_pending', 'group_join_failed', 'group_join_success', 'synced'}:
                    self._finalize_bind_task_residue(
                        conn,
                        row=row,
                        resolved_status='success',
                        result_code='bind_auto_reconciled_success',
                        result_reason='bind processing residue auto-closed from downstream terminal state',
                        now_iso=now_iso,
                    )
                    bind_reconciled += 1
                elif lead_status in {'bind_failed', 'manual_review_pending'}:
                    self._finalize_bind_task_residue(
                        conn,
                        row=row,
                        resolved_status='failed',
                        result_code='bind_auto_reconciled_failed',
                        result_reason='bind processing residue auto-closed from lead failed state',
                        now_iso=now_iso,
                    )
                    bind_reconciled += 1

            pending_group_rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.lead_id, t.status, t.created_at, t.started_at, t.payload, t.raw_result,
                       COALESCE(l.current_status, '') AS current_status,
                       COALESCE(l.mobile, '') AS mobile,
                       COALESCE(l.area_code, 0) AS area_code,
                       COALESCE(l.country, '') AS country,
                       COALESCE(l.crm_verified_official_group, '') AS crm_verified_official_group,
                       COALESCE(l.updated_at, '') AS lead_updated_at
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'group_join' AND t.status = 'pending'
                ORDER BY t.lead_id ASC, datetime(t.created_at) ASC, t.task_id ASC
                """
            ).fetchall()]
            grouped_pending: Dict[str, List[Dict[str, Any]]] = {}
            for row in pending_group_rows:
                grouped_pending.setdefault(str(row.get('lead_id') or '').strip(), []).append(row)
            for lead_id, rows in grouped_pending.items():
                active_rows = [row for row in rows if row.get('task_id')]
                if not active_rows:
                    continue
                newest = sorted(
                    active_rows,
                    key=lambda item: (str(item.get('created_at') or ''), str(item.get('task_id') or '')),
                    reverse=True,
                )[0]
                lead_status = str(newest.get('current_status') or '').strip().lower()
                payload_dict = self._parse_task_payload_dict(newest.get('payload'))
                resolved_target_group = str(
                    payload_dict.get('target_group') or newest.get('crm_verified_official_group') or ''
                ).strip()
                if not resolved_target_group:
                    lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
                    if lead_row:
                        resolved_target_group = str(self._resolve_official_group_target_group(lead=dict(lead_row)) or '').strip()
                newest['resolved_target_group'] = resolved_target_group
                for older in active_rows:
                    if older['task_id'] == newest['task_id']:
                        continue
                    older['resolved_target_group'] = resolved_target_group
                    self._finalize_group_join_task_residue(
                        conn,
                        row=older,
                        resolved_status='cancelled',
                        result_code='group_join_auto_superseded_duplicate',
                        result_reason='older duplicate pending group_join task auto-cancelled',
                        now_iso=now_iso,
                    )
                    group_reconciled += 1
                anchor = str(newest.get('started_at') or newest.get('created_at') or '').strip()
                try:
                    newest_age_seconds = max(0.0, (now_dt - parse_iso_datetime(anchor)).total_seconds()) if anchor else 0.0
                except Exception:
                    newest_age_seconds = 0.0
                if lead_status in {'group_join_success', 'synced'}:
                    self._finalize_group_join_task_residue(
                        conn,
                        row=newest,
                        resolved_status='success',
                        result_code='group_join_auto_reconciled_success',
                        result_reason='pending group_join residue auto-closed from lead success state',
                        now_iso=now_iso,
                    )
                    group_reconciled += 1
                    continue
                if lead_status in {'group_join_failed', 'bind_failed'}:
                    self._finalize_group_join_task_residue(
                        conn,
                        row=newest,
                        resolved_status='failed',
                        result_code='group_join_auto_reconciled_failed',
                        result_reason='pending group_join residue auto-closed from lead failed state',
                        now_iso=now_iso,
                    )
                    group_reconciled += 1
                    continue
                if lead_status not in {'bind_success', 'group_join_pending'}:
                    continue
                if newest_age_seconds < self._group_join_pending_stale_seconds:
                    continue
                verified_official_group = str(newest.get('crm_verified_official_group') or '').strip()
                if verified_official_group and (not resolved_target_group or verified_official_group == resolved_target_group):
                    self._finalize_group_join_task_residue(
                        conn,
                        row=newest,
                        resolved_status='success',
                        result_code='group_join_auto_reconciled_success_from_verified_official_group',
                        result_reason='pending group_join residue auto-closed from verified official-group evidence',
                        now_iso=now_iso,
                        update_lead_status='group_join_success',
                    )
                    group_reconciled += 1
                    continue
                if not resolved_target_group:
                    continue
                requester_still_pending = self._official_group_requester_pending_in_runtime(
                    target_group=resolved_target_group,
                    target_phone_hint=str(newest.get('mobile') or '').strip() or None,
                    target_requester_id=None,
                )
                if requester_still_pending:
                    continue
                self._finalize_group_join_task_residue(
                    conn,
                    row=newest,
                    resolved_status='failed',
                    result_code='group_join_auto_closed_missing_runtime_requester',
                    result_reason='pending group_join residue auto-closed because runtime no longer has matching requester',
                    now_iso=now_iso,
                    update_lead_status='group_join_failed',
                )
                group_reconciled += 1

            crm_rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.lead_id, t.task_type, t.status, t.created_at, t.started_at, t.raw_result,
                       COALESCE(l.current_status, '') AS current_status,
                       COALESCE(l.crm_verified_at, '') AS crm_verified_at
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type IN ('crm_sync', 'crm_sync_retry')
                  AND t.status IN ('pending', 'processing')
                ORDER BY datetime(COALESCE(t.started_at, t.created_at)) ASC, t.task_id ASC
                """
            ).fetchall()]
            for row in crm_rows:
                anchor = str(row.get('started_at') or row.get('created_at') or '').strip()
                if not anchor:
                    continue
                try:
                    age_seconds = max(0.0, (now_dt - parse_iso_datetime(anchor)).total_seconds())
                except Exception:
                    continue
                if age_seconds < self._crm_task_stale_seconds:
                    continue
                lead_id = str(row.get('lead_id') or '').strip()
                latest_success = None
                latest_failure = None
                if lead_id:
                    latest_success = conn.execute(
                        "SELECT response_snapshot FROM sync_logs WHERE lead_id = ? AND sync_type = 'customer_upsert' AND target_system = 'crm' AND status = 'success' ORDER BY created_at DESC LIMIT 1",
                        (lead_id,),
                    ).fetchone()
                    latest_failure = conn.execute(
                        "SELECT response_snapshot FROM sync_logs WHERE lead_id = ? AND sync_type = 'customer_upsert' AND target_system = 'crm' AND status = 'failed' ORDER BY created_at DESC LIMIT 1",
                        (lead_id,),
                    ).fetchone()
                if str(row.get('current_status') or '').strip() == 'synced' or str(row.get('crm_verified_at') or '').strip() or latest_success:
                    self._finalize_crm_task_residue(
                        conn,
                        row=row,
                        resolved_status='success',
                        result_code='crm_auto_reconciled_success',
                        result_reason='stale crm task auto-closed from verified CRM evidence',
                        now_iso=now_iso,
                    )
                    crm_reconciled += 1
                    continue
                current_status = str(row.get('current_status') or '').strip().lower()
                if current_status in {'group_join_failed', 'group_join_success', 'synced'} and latest_failure:
                    self._finalize_crm_task_residue(
                        conn,
                        row=row,
                        resolved_status='failed',
                        result_code='crm_auto_reconciled_failed',
                        result_reason='stale crm task auto-closed from downstream terminal state and failed crm evidence',
                        now_iso=now_iso,
                    )
                    crm_reconciled += 1
            conn.commit()
        self._task_residue_last_reconciled_monotonic = now_monotonic
        return {
            'attempted': True,
            'bind_reconciled_count': bind_reconciled,
            'group_join_reconciled_count': group_reconciled,
            'crm_reconciled_count': crm_reconciled,
        }

    def _record_audit_event(
        self,
        conn: sqlite3.Connection,
        *,
        event_type: str,
        event_source: str,
        payload: Dict[str, Any],
        lead_id: Optional[str] = None,
        ingress_event_id: Optional[str] = None,
    ) -> None:
        conn.execute(
            "INSERT INTO operator_audit_log (audit_id, lead_id, ingress_event_id, event_type, event_source, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                create_id('audit'),
                lead_id,
                ingress_event_id,
                event_type,
                event_source,
                json.dumps(payload, ensure_ascii=False),
                utc_now(),
            ),
        )

    def _enqueue_ingress_event(self, *, ingress_type: str, source_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        idempotency_key = fingerprint_payload(ingress_type=ingress_type, payload=payload)
        now = utc_now()
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT event_id, status, result_snapshot FROM ingress_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                result_snapshot = existing['result_snapshot'] or '{}'
                self._record_audit_event(
                    conn,
                    event_type='ingress_event_reused',
                    event_source=ingress_type,
                    payload={'idempotency_key': idempotency_key, 'status': existing['status']},
                    ingress_event_id=existing['event_id'],
                )
                conn.commit()
                return {
                    'event_id': existing['event_id'],
                    'queued': str(existing['status']) in {'queued', 'processing'},
                    'duplicate': True,
                    'status': existing['status'],
                    'result_snapshot': json.loads(result_snapshot),
                }
            event_id = create_id('ingress')
            job_id = create_id('job')
            conn.execute(
                "INSERT INTO ingress_events (event_id, ingress_type, source_key, idempotency_key, payload, status, result_snapshot, created_at, updated_at, processed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, ingress_type, source_key, idempotency_key, json.dumps(payload, ensure_ascii=False), 'queued', '{}', now, now, None),
            )
            conn.execute(
                "INSERT INTO ingress_jobs (job_id, event_id, status, attempt_count, available_at, last_error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, event_id, 'queued', 0, now, None, now, now),
            )
            self._record_audit_event(
                conn,
                event_type='ingress_event_enqueued',
                event_source=ingress_type,
                payload={'idempotency_key': idempotency_key, 'source_key': source_key},
                ingress_event_id=event_id,
            )
            conn.commit()
            return {'event_id': event_id, 'queued': True, 'duplicate': False, 'status': 'queued'}

    def _persist_ingress_job_result(self, *, row: sqlite3.Row, event: sqlite3.Row, status: str, error_text: Optional[str], result: Dict[str, Any]) -> None:
        last_exc: Optional[Exception] = None
        for attempt in range(5):
            try:
                with self.db.connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    now = utc_now()
                    conn.execute("UPDATE ingress_jobs SET status = ?, last_error = ?, updated_at = ? WHERE job_id = ?", (status, error_text, now, row['job_id']))
                    conn.execute("UPDATE ingress_events SET status = ?, result_snapshot = ?, updated_at = ?, processed_at = ? WHERE event_id = ?", (status, json.dumps(result, ensure_ascii=False), now, now, row['event_id']))
                    self._record_audit_event(
                        conn,
                        event_type='ingress_event_processed',
                        event_source=event['ingress_type'],
                        payload={'status': status, 'error': error_text, 'result': result},
                        ingress_event_id=row['event_id'],
                    )
                    conn.commit()
                    return
            except sqlite3.OperationalError as exc:
                if 'locked' not in str(exc).lower():
                    raise
                last_exc = exc
                time.sleep(0.2 * (attempt + 1))
        if last_exc is not None:
            raise last_exc

    def process_next_ingress_job(self) -> Optional[Dict[str, Any]]:
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                "SELECT job_id, event_id FROM ingress_jobs WHERE status = 'queued' ORDER BY available_at ASC, created_at ASC LIMIT 1"
            ).fetchone()
            if not row:
                conn.commit()
                return None
            now = utc_now()
            cursor = conn.execute(
                "UPDATE ingress_jobs SET status = 'processing', attempt_count = attempt_count + 1, updated_at = ? WHERE job_id = ? AND status = 'queued'",
                (now, row['job_id']),
            )
            if cursor.rowcount <= 0:
                conn.commit()
                return None
            conn.execute("UPDATE ingress_events SET status = 'processing', updated_at = ? WHERE event_id = ?", (now, row['event_id']))
            event = conn.execute("SELECT ingress_type, payload FROM ingress_events WHERE event_id = ?", (row['event_id'],)).fetchone()
            conn.commit()
        if not event:
            return None
        payload = json.loads(event['payload'] or '{}')
        try:
            if event['ingress_type'] == 'lark_event':
                result = self._handle_lark_event_sync(payload)
            elif event['ingress_type'] == 'manual_cs_submission':
                result = self._submit_manual_cs_sync(ManualCsSubmissionRequest(**payload))
            elif event['ingress_type'] == 'registration_group_approval_decision':
                result = self._registration_group_approval_decision_sync(
                    RegistrationGroupApprovalDecisionRequest(**{k: v for k, v in payload.items() if k != 'approval_run_id'}),
                    approval_run_id=str(payload.get('approval_run_id') or '').strip() or None,
                )
            else:
                raise RuntimeError(f'unsupported ingress_type: {event["ingress_type"]}')
            status = 'done'
            error_text = None
        except Exception as exc:
            result = {'accepted': False, 'reason': 'ingress_processing_failed', 'error': str(exc)}
            status = 'failed'
            error_text = str(exc)
        self._persist_ingress_job_result(row=row, event=event, status=status, error_text=error_text, result=result)
        return {'event_id': row['event_id'], 'status': status, 'result': result}

    def _build_bind_execution_result(self, *, task_id: str) -> BindCheckResultRequest:
        with self.db.connect() as conn:
            task = conn.execute("SELECT lead_id, payload FROM automation_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="task not found")
            task_payload = json.loads(task['payload'] or '{}')
            lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (task['lead_id'],)).fetchone()
        if not lead_row:
            raise HTTPException(status_code=404, detail="lead not found")
        lead = dict(lead_row)
        invite_code = str(lead.get('inviter_id') or '').strip().upper()
        account_id = str(task_payload.get('account_id') or '').strip()
        expected_guild = self._resolve_expected_bind_guild(task_payload=task_payload, lead_row=lead_row) or str(lead.get('dept_name') or '').strip()
        context = {
            'task_id': task_id,
            'lead_id': task['lead_id'],
            'submission_id': str(task_payload.get('submission_id') or ''),
            'account_id': account_id,
            'mobile': str(lead.get('mobile') or ''),
            'app_name': str(lead.get('app_name') or ''),
            'dept_name': expected_guild,
            'registration_group': str(lead.get('pendaftaran_group') or ''),
            'invite_code': invite_code,
            'source_bot_app_id': str(task_payload.get('source_bot_app_id') or ''),
        }
        executor = self.resolve_guild_executor(expected_guild)
        if executor:
            context.update({
                'executor_backend_url': str(executor.get('backend_url') or ''),
                'executor_login_username': str(executor.get('login_username') or ''),
                'executor_password_secret_ref': str(executor.get('password_secret_ref') or ''),
                'executor_proxy_url': str(executor.get('proxy_url') or ''),
                'executor_proxy_region': str(executor.get('proxy_region') or ''),
                'executor_proxy_type': str(executor.get('proxy_type') or ''),
                'executor_browser_profile_key': str(executor.get('browser_profile_key') or ''),
                'executor_bind_concurrency': int(executor.get('bind_concurrency') or 1),
                'executor_request_timeout_seconds': int(executor.get('request_timeout_seconds') or 30),
            })
        if callable(self.bind_simulator):
            simulated = self.bind_simulator(context)
            if not isinstance(simulated, dict):
                raise RuntimeError('bind simulator must return a dict')
            return BindCheckResultRequest(
                status=str(simulated.get('status') or 'failed'),
                result_code=simulated.get('result_code'),
                result_reason=simulated.get('result_reason'),
                finished_at=utc_now(),
                raw_result=simulated.get('raw_result') or {},
            )
        if callable(self.real_bind_executor):
            executed = self.real_bind_executor(context)
            if not isinstance(executed, dict):
                raise RuntimeError('real bind executor must return a dict')
            return BindCheckResultRequest(
                status=str(executed.get('status') or 'failed'),
                result_code=executed.get('result_code'),
                result_reason=executed.get('result_reason'),
                finished_at=utc_now(),
                raw_result=executed.get('raw_result') or {},
            )
        return BindCheckResultRequest(
            status='failed',
            result_code='bind_unauthorized',
            result_reason='AxiosError: Request failed with status code 401',
            finished_at=utc_now(),
            raw_result={'guild_code': lead.get('dept_name') or '', 'invite_code': invite_code, 'auth_required': True},
        )

    def _select_next_bind_task(self) -> Optional[Dict[str, Any]]:
        now = utc_now()
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.payload, t.lead_id, t.created_at, l.dept_name
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check' AND t.status = 'pending'
                ORDER BY t.created_at ASC
                LIMIT 200
                """
            ).fetchall()]
            if not rows:
                return None
            processing_rows = [dict(r) for r in conn.execute(
                """
                SELECT COALESCE(l.dept_name, '') AS guild_name, COUNT(*) AS processing_count
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check' AND t.status = 'processing'
                GROUP BY COALESCE(l.dept_name, '')
                """
            ).fetchall()]
            processing_counts = {str(r.get('guild_name') or '').strip(): int(r.get('processing_count') or 0) for r in processing_rows}
            executor_cache: Dict[str, Optional[Dict[str, Any]]] = {}
            for row in rows:
                guild_name = str(row.get('dept_name') or '').strip()
                if guild_name not in executor_cache:
                    executor_cache[guild_name] = self.resolve_guild_executor(guild_name) if guild_name else None
                executor = executor_cache[guild_name]
                bind_limit = int((executor or {}).get('bind_concurrency') or 1)
                if processing_counts.get(guild_name, 0) >= max(1, bind_limit):
                    continue
                cursor = conn.execute(
                    "UPDATE automation_tasks SET status = 'processing', started_at = COALESCE(started_at, ?) WHERE task_id = ? AND status = 'pending'",
                    (now, row['task_id']),
                )
                if cursor.rowcount:
                    conn.commit()
                    row['started_at'] = row.get('started_at') or now
                    row['task_type'] = 'bind_check'
                    return row
            return None

    def _select_next_crm_retry_task(self) -> Optional[Dict[str, Any]]:
        now = utc_now()
        now_dt = parse_iso_datetime(now)
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.payload, t.lead_id, t.created_at, t.retry_count, t.result_code, t.result_reason
                FROM automation_tasks t
                WHERE t.task_type = 'crm_sync_retry' AND t.status = 'pending'
                ORDER BY t.created_at ASC
                LIMIT 200
                """
            ).fetchall()]
            for row in rows:
                try:
                    payload = json.loads(row.get('payload') or '{}')
                except Exception:
                    payload = {}
                next_retry_at = str(payload.get('next_retry_at') or '').strip()
                if next_retry_at:
                    try:
                        if parse_iso_datetime(next_retry_at) > now_dt:
                            continue
                    except Exception:
                        pass
                cursor = conn.execute(
                    "UPDATE automation_tasks SET status = 'processing', started_at = COALESCE(started_at, ?) WHERE task_id = ? AND status = 'pending'",
                    (now, row['task_id']),
                )
                if cursor.rowcount:
                    conn.commit()
                    row['started_at'] = row.get('started_at') or now
                    row['task_type'] = 'crm_sync_retry'
                    row['payload_dict'] = payload
                    return row
            return None

    def _calculate_bind_metrics(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            pending_rows = [dict(r) for r in conn.execute(
                """
                SELECT COALESCE(l.dept_name, '') AS guild_name, MIN(t.created_at) AS oldest_created_at, COUNT(*) AS pending_count
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check' AND t.status = 'pending'
                GROUP BY COALESCE(l.dept_name, '')
                """
            ).fetchall()]
            processing_rows = [dict(r) for r in conn.execute(
                """
                SELECT COALESCE(l.dept_name, '') AS guild_name, COUNT(*) AS processing_count
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check' AND t.status = 'processing'
                GROUP BY COALESCE(l.dept_name, '')
                """
            ).fetchall()]
            completed_rows = [dict(r) for r in conn.execute(
                """
                SELECT t.created_at, t.started_at, t.finished_at
                FROM automation_tasks t
                WHERE t.task_type = 'bind_check'
                  AND t.started_at IS NOT NULL
                  AND t.finished_at IS NOT NULL
                  AND t.status IN ('success', 'failed')
                ORDER BY t.finished_at DESC
                LIMIT 100
                """
            ).fetchall()]
        now_dt = datetime.now(timezone.utc)
        pending_by_guild = {str(r.get('guild_name') or '').strip(): r for r in pending_rows}
        processing_by_guild = {str(r.get('guild_name') or '').strip(): int(r.get('processing_count') or 0) for r in processing_rows}
        guild_names = sorted(set(pending_by_guild.keys()) | set(processing_by_guild.keys()))
        per_guild = []
        oldest_pending_age_seconds = 0.0
        for guild_name in guild_names:
            executor = self.resolve_guild_executor(guild_name) if guild_name else None
            bind_limit = int((executor or {}).get('bind_concurrency') or 1)
            pending_count = int((pending_by_guild.get(guild_name) or {}).get('pending_count') or 0)
            processing_count = int(processing_by_guild.get(guild_name) or 0)
            oldest_created_at = (pending_by_guild.get(guild_name) or {}).get('oldest_created_at')
            oldest_age = 0.0
            if oldest_created_at:
                oldest_age = max(0.0, round((now_dt - parse_iso_datetime(str(oldest_created_at))).total_seconds(), 3))
                oldest_pending_age_seconds = max(oldest_pending_age_seconds, oldest_age)
            per_guild.append({
                'guild_name': guild_name,
                'pending_count': pending_count,
                'processing_count': processing_count,
                'bind_concurrency': max(1, bind_limit),
                'available_slots': max(0, max(1, bind_limit) - processing_count),
                'oldest_pending_age_seconds': oldest_age,
            })
        queue_waits = []
        execution_times = []
        end_to_end_times = []
        for row in completed_rows:
            try:
                created_at = parse_iso_datetime(str(row.get('created_at') or ''))
                started_at = parse_iso_datetime(str(row.get('started_at') or ''))
                finished_at = parse_iso_datetime(str(row.get('finished_at') or ''))
            except Exception:
                continue
            queue_waits.append(max(0.0, (started_at - created_at).total_seconds()))
            execution_times.append(max(0.0, (finished_at - started_at).total_seconds()))
            end_to_end_times.append(max(0.0, (finished_at - created_at).total_seconds()))
        def _avg(values: List[float]) -> float:
            if not values:
                return 0.0
            return round(sum(values) / len(values), 3)
        return {
            'recent_completed_count': len(end_to_end_times),
            'oldest_pending_age_seconds': round(oldest_pending_age_seconds, 3),
            'avg_queue_wait_seconds': _avg(queue_waits),
            'avg_execution_seconds': _avg(execution_times),
            'avg_end_to_end_seconds': _avg(end_to_end_times),
            'per_guild': per_guild,
        }

    def _recent_runtime_traces(self, *, bind_limit: int = 10, crm_limit: int = 10) -> Dict[str, Any]:
        with self.db.connect() as conn:
            bind_rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.lead_id, t.status, t.result_code, t.result_reason, t.created_at, t.started_at, t.finished_at,
                       COALESCE(l.dept_name, '') AS guild_name, COALESCE(l.mobile, '') AS mobile, COALESCE(l.yw_id, '') AS account_id,
                       COALESCE(l.pendaftaran_group, '') AS registration_group
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check'
                ORDER BY COALESCE(t.finished_at, t.started_at, t.created_at) DESC
                LIMIT ?
                """,
                (max(1, min(int(bind_limit or 10), 50)),),
            ).fetchall()]
            crm_rows = [dict(r) for r in conn.execute(
                """
                SELECT sl.sync_log_id, sl.lead_id, sl.task_id, sl.status, sl.sync_type, sl.target_system, sl.created_at,
                       COALESCE(l.dept_name, '') AS guild_name, COALESCE(l.mobile, '') AS mobile, COALESCE(l.yw_id, '') AS account_id,
                       COALESCE(l.pendaftaran_group, '') AS registration_group,
                       sl.request_snapshot, sl.response_snapshot
                FROM sync_logs sl
                LEFT JOIN leads l ON l.lead_id = sl.lead_id
                WHERE sl.target_system = 'crm'
                ORDER BY sl.created_at DESC
                LIMIT ?
                """,
                (max(1, min(int(crm_limit or 10), 50)),),
            ).fetchall()]
        recent_bind_traces = []
        for row in bind_rows:
            queue_wait = None
            execution = None
            end_to_end = None
            try:
                created_at = parse_iso_datetime(str(row.get('created_at') or '')) if row.get('created_at') else None
                started_at = parse_iso_datetime(str(row.get('started_at') or '')) if row.get('started_at') else None
                finished_at = parse_iso_datetime(str(row.get('finished_at') or '')) if row.get('finished_at') else None
                if created_at and started_at:
                    queue_wait = round(max(0.0, (started_at - created_at).total_seconds()), 3)
                if started_at and finished_at:
                    execution = round(max(0.0, (finished_at - started_at).total_seconds()), 3)
                if created_at and finished_at:
                    end_to_end = round(max(0.0, (finished_at - created_at).total_seconds()), 3)
            except Exception:
                pass
            recent_bind_traces.append({
                'task_id': row.get('task_id'),
                'lead_id': row.get('lead_id'),
                'guild_name': row.get('guild_name') or '',
                'mobile': row.get('mobile') or '',
                'account_id': row.get('account_id') or '',
                'registration_group': row.get('registration_group') or '',
                'status': row.get('status'),
                'result_code': row.get('result_code'),
                'result_reason': row.get('result_reason'),
                'created_at': row.get('created_at'),
                'started_at': row.get('started_at'),
                'finished_at': row.get('finished_at'),
                'queue_wait_seconds': queue_wait,
                'execution_seconds': execution,
                'end_to_end_seconds': end_to_end,
            })
        recent_crm_traces = []
        for row in crm_rows:
            request_snapshot = json.loads(row.get('request_snapshot') or '{}') if row.get('request_snapshot') else {}
            response_snapshot = json.loads(row.get('response_snapshot') or '{}') if row.get('response_snapshot') else {}
            recent_crm_traces.append({
                'sync_log_id': row.get('sync_log_id'),
                'lead_id': row.get('lead_id'),
                'task_id': row.get('task_id'),
                'status': row.get('status'),
                'sync_type': row.get('sync_type'),
                'target_system': row.get('target_system'),
                'created_at': row.get('created_at'),
                'guild_name': row.get('guild_name') or '',
                'mobile': row.get('mobile') or '',
                'account_id': row.get('account_id') or '',
                'registration_group': row.get('registration_group') or '',
                'request_app_name': request_snapshot.get('appName') if isinstance(request_snapshot, dict) else None,
                'request_dept_name': request_snapshot.get('deptName') if isinstance(request_snapshot, dict) else None,
                'request_group': request_snapshot.get('pendaftaranGroup') if isinstance(request_snapshot, dict) else None,
                'verified_after_write': bool((response_snapshot.get('verified_after_write') if isinstance(response_snapshot, dict) else False)),
                'action': response_snapshot.get('action') if isinstance(response_snapshot, dict) else None,
                'crm_response_code': ((response_snapshot.get('crm_response') or {}).get('code') if isinstance(response_snapshot, dict) and isinstance(response_snapshot.get('crm_response'), dict) else None),
            })
        return {
            'recent_bind_traces': recent_bind_traces,
            'recent_crm_traces': recent_crm_traces,
        }

    def process_next_automation_task(self) -> Optional[Dict[str, Any]]:
        row = self._select_next_bind_task()
        if row:
            try:
                payload = self._build_bind_execution_result(task_id=row['task_id'])
                result = self.bind_check_result(row['task_id'], payload)
            except Exception as exc:
                payload = BindCheckResultRequest(
                    status='failed',
                    result_code='bind_execution_error',
                    result_reason=str(exc),
                    finished_at=utc_now(),
                    raw_result={},
                )
                result = self.bind_check_result(row['task_id'], payload)
            executor = None
            with self.db.connect() as conn:
                lead_row = conn.execute("SELECT dept_name FROM leads WHERE lead_id = ?", (row['lead_id'],)).fetchone()
            if lead_row:
                executor = self.get_guild_executor(str(lead_row['dept_name'] or '').strip()) if self.resolve_guild_executor(str(lead_row['dept_name'] or '').strip()) else None
            task_payload = json.loads(row['payload'] or '{}')
            source_bot_app_id = str(task_payload.get('source_bot_app_id') or '').strip()
            message_id = str(task_payload.get('source_message_id') or '').strip()
            chat_id = str(task_payload.get('source_chat_id') or '').strip()
            if message_id or chat_id:
                reply_adapter = self._resolve_lark_reply_adapter(app_id=source_bot_app_id or None)
                with self.db.connect() as conn:
                    lead_row = conn.execute("SELECT mobile, area_code, pendaftaran_group, inviter_id FROM leads WHERE lead_id = ?", (row['lead_id'],)).fetchone()
                reply_envelope = {
                    'accepted': bool(result.get('lead_status') == 'bind_success' and result.get('reason') != 'crm_sync_failed'),
                    'reason': result.get('reason'),
                    'result_code': result.get('result_code'),
                    'result_reason': result.get('result_reason'),
                    'bind_failure_category': result.get('bind_failure_category'),
                    'lead_status': result.get('lead_status'),
                    'next_action': result.get('next_action'),
                    'requires_human_action': result.get('requires_human_action'),
                    'human_action_type': result.get('human_action_type'),
                    'reply_phone': str((lead_row['mobile'] if lead_row else '') or '-'),
                    'reply_area_code': int((lead_row['area_code'] if lead_row and lead_row['area_code'] is not None else 0) or 0),
                    'reply_id': str(task_payload.get('account_id') or '-'),
                    'reply_group': str((lead_row['pendaftaran_group'] if lead_row else '') or '-'),
                    'reply_code': str((lead_row['inviter_id'] if lead_row else '') or '-'),
                }
                if self._should_emit_lark_reply(reply_envelope):
                    reply_text = self._format_lark_reply_text(reply_envelope)
                    result['reply_text'] = reply_text
                    self._reply_lark_message(message_id=message_id, chat_id=chat_id, text=reply_text, adapter=reply_adapter)
            if executor is not None:
                result['executor'] = executor
            result['task_type'] = 'bind_check'
            return result

        retry_row = self._select_next_crm_retry_task()
        if not retry_row:
            return None
        result = self._process_crm_retry_task(retry_row)
        result['task_type'] = 'crm_sync_retry'
        return result

    def list_ingress_queue(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT j.job_id, j.event_id, j.status, j.attempt_count, j.available_at, j.last_error, e.ingress_type, e.source_key, e.created_at, e.updated_at FROM ingress_jobs j JOIN ingress_events e ON e.event_id = j.event_id ORDER BY e.created_at DESC LIMIT 200"
            ).fetchall()]
            return {'rows': rows}

    def operator_audit_log(self, *, limit: int = 200) -> Dict[str, Any]:
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT audit_id, lead_id, ingress_event_id, event_type, event_source, payload, created_at FROM operator_audit_log ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit or 200), 1000)),),
            ).fetchall()]
            return {'rows': rows}

    def _pending_bind_human_actions(self, *, limit: int = 20) -> list[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.lead_id, t.status, t.result_code, t.result_reason, t.created_at, t.started_at, t.finished_at, t.raw_result,
                       COALESCE(l.dept_name, '') AS guild_name,
                       COALESCE(l.mobile, '') AS mobile,
                       COALESCE(l.yw_id, '') AS account_id,
                       COALESCE(l.pendaftaran_group, '') AS registration_group
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check' AND t.status = 'failed'
                ORDER BY COALESCE(t.finished_at, t.created_at) DESC
                LIMIT ?
                """,
                (max(1, min(int(limit or 20), 100)),),
            ).fetchall()]
        pending = []
        for row in rows:
            try:
                raw_result = json.loads(row.get('raw_result') or '{}')
            except Exception:
                raw_result = {}
            human = self._classify_bind_human_action(
                result_code=row.get('result_code'),
                result_reason=row.get('result_reason'),
                raw_result=raw_result,
            )
            if not human.get('requires_human_action'):
                continue
            pending.append({
                'task_id': row.get('task_id'),
                'lead_id': row.get('lead_id'),
                'guild_name': row.get('guild_name') or '',
                'mobile': row.get('mobile') or '',
                'account_id': row.get('account_id') or '',
                'registration_group': row.get('registration_group') or '',
                'status': row.get('status'),
                'result_code': row.get('result_code'),
                'result_reason': row.get('result_reason'),
                'human_action_type': human.get('human_action_type'),
                'created_at': row.get('created_at'),
                'started_at': row.get('started_at'),
                'finished_at': row.get('finished_at'),
            })
        return pending

    def runtime_health(self) -> Dict[str, Any]:
        crm_adapter_health = self.crm_adapter.health_snapshot() if self.crm_adapter is not None and hasattr(self.crm_adapter, 'health_snapshot') else {}
        bind_metrics = self._calculate_bind_metrics()
        runtime_traces = self._recent_runtime_traces()
        pending_bind_human_actions = self._pending_bind_human_actions()
        with self.db.connect() as conn:
            ingress_queued = conn.execute("SELECT COUNT(*) FROM ingress_jobs WHERE status = 'queued'").fetchone()[0]
            ingress_processing = conn.execute("SELECT COUNT(*) FROM ingress_jobs WHERE status = 'processing'").fetchone()[0]
            pending_bind_tasks = conn.execute("SELECT COUNT(*) FROM automation_tasks WHERE task_type = 'bind_check' AND status = 'pending'").fetchone()[0]
            processing_bind_tasks = conn.execute("SELECT COUNT(*) FROM automation_tasks WHERE task_type = 'bind_check' AND status = 'processing'").fetchone()[0]
        registration_group_approval_health = self.registration_group_approval_executor_health()
        official_group_approval_health = self.official_group_approval_executor_health()
        return {
            'crm': {
                'enabled': self.crm_adapter is not None,
                'base_url': self.crm_base_url or getattr(self.crm_adapter, 'base_url', None),
                'username': self.crm_username or getattr(self.crm_adapter, 'username', None),
                'login_error': self.crm_login_error or crm_adapter_health.get('login_error'),
                'status': crm_adapter_health.get('status') or ('degraded' if (self.crm_login_error and self.crm_adapter is not None) else ('healthy' if self.crm_adapter is not None else 'disabled')),
                'token_ready': crm_adapter_health.get('token_ready'),
                'last_login_attempt_at': crm_adapter_health.get('last_login_attempt_at'),
                'last_login_ok_at': crm_adapter_health.get('last_login_ok_at'),
                'login_retry_cooldown_seconds': crm_adapter_health.get('login_retry_cooldown_seconds'),
            },
            'lark': {
                'default_app': self.lark_default_app_name,
                'default_guild': self.lark_default_dept_name,
                'current_app_id': self.current_lark_app_id,
            },
            'simulation': {
                'auto_bind_simulation': self.auto_bind_simulation,
                'success_rate': self.auto_bind_simulation_success_rate,
                'mode': 'simulated' if self.auto_bind_simulation else 'live',
            },
            'registration_group_approval': registration_group_approval_health,
            'official_group_approval': official_group_approval_health,
            'ingress': {
                'async_default': self.ingress_async_default,
                'worker_enabled': self.ingress_worker_enabled,
                'worker_count': self.ingress_worker_count,
                'worker_alive': any(thread.is_alive() for thread in self._worker_threads),
                'active_worker_threads': sum(1 for thread in self._worker_threads if thread.is_alive()),
                'queued_jobs': ingress_queued,
                'processing_jobs': ingress_processing,
                'pending_bind_tasks': pending_bind_tasks,
                'processing_bind_tasks': processing_bind_tasks,
                'require_invite_code': self.require_invite_code,
                'bind_metrics': bind_metrics,
                'pending_bind_human_actions': pending_bind_human_actions,
                'pending_bind_human_action_count': len(pending_bind_human_actions),
                'recent_bind_traces': runtime_traces['recent_bind_traces'],
                'recent_crm_traces': runtime_traces['recent_crm_traces'],
            },
        }

    def _classify_manual_cs_submission(
        self,
        *,
        payload: ManualCsSubmissionRequest,
        parsed_payload: Dict[str, Any],
        final_account_id: Optional[str],
        final_mobile: str,
        final_registration_group: Optional[str],
        final_app_name: Optional[str],
        final_dept_name: Optional[str],
        final_invite_code: Optional[str],
    ) -> Dict[str, Any]:
        review_reason_codes = list(parsed_payload.get('conflicts', []) or [])
        confidence = float(parsed_payload.get('confidence') or 0.0)
        critical_missing = [
            name for name, value in {
                'mobile': final_mobile,
                'registration_group': final_registration_group,
                'app_name': final_app_name,
                'dept_name': final_dept_name,
            }.items() if not value
        ]
        if critical_missing:
            review_reason_codes.extend(f'missing_{name}' for name in critical_missing)
        if confidence < 0.75:
            review_reason_codes.append('low_confidence')
        review_reason_codes = list(dict.fromkeys(review_reason_codes))

        if 'account_id_conflict' in review_reason_codes:
            return {
                'parser_version': 'manual_cs_parser_v2',
                'parser_status': 'conflict',
                'routing_decision': 'manual_review',
                'recommended_next_action': 'review_account_conflict',
                'review_reason_codes': review_reason_codes,
                'review_status': 'pending',
            }
        if critical_missing:
            return {
                'parser_version': 'manual_cs_parser_v2',
                'parser_status': 'missing_fields',
                'routing_decision': 'manual_review',
                'recommended_next_action': 'fill_missing_fields',
                'review_reason_codes': review_reason_codes,
                'review_status': 'pending',
            }
        if payload.submission_type == 'screenshot' and not final_account_id:
            return {
                'parser_version': 'manual_cs_parser_v2',
                'parser_status': 'needs_recognition',
                'routing_decision': 'queue_account_recognition',
                'recommended_next_action': 'queue_account_recognition',
                'review_reason_codes': review_reason_codes,
                'review_status': 'not_needed',
            }
        if confidence < 0.75:
            return {
                'parser_version': 'manual_cs_parser_v2',
                'parser_status': 'low_confidence',
                'routing_decision': 'manual_review',
                'recommended_next_action': 'review_low_confidence',
                'review_reason_codes': review_reason_codes,
                'review_status': 'pending',
            }
        return {
            'parser_version': 'manual_cs_parser_v2',
            'parser_status': 'ready',
            'routing_decision': 'queue_bind_check' if final_account_id else 'queue_account_recognition',
            'recommended_next_action': 'queue_bind_check' if final_account_id else 'queue_account_recognition',
            'review_reason_codes': review_reason_codes,
            'review_status': 'not_needed',
        }

    def _record_status_history(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
        from_status: Optional[str],
        to_status: str,
        trigger_type: str,
        trigger_source: str,
        trigger_event_id: Optional[str] = None,
        trigger_task_id: Optional[str] = None,
        operator_id: Optional[str] = None,
        operator_name: Optional[str] = None,
        remark: Optional[str] = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO lead_status_history (
                history_id, lead_id, from_status, to_status, trigger_type, trigger_source,
                trigger_event_id, trigger_task_id, operator_id, operator_name, remark, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                create_id("hist"),
                lead_id,
                from_status,
                to_status,
                trigger_type,
                trigger_source,
                trigger_event_id,
                trigger_task_id,
                operator_id,
                operator_name,
                remark,
                utc_now(),
            ),
        )

    def _record_sync_log(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: Optional[str],
        task_id: Optional[str],
        sync_type: str,
        target_system: str,
        status: str,
        request_snapshot: Any,
        response_snapshot: Any,
    ) -> None:
        conn.execute(
            "INSERT INTO sync_logs (sync_log_id, lead_id, task_id, sync_type, target_system, status, request_snapshot, response_snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                create_id("sync"),
                lead_id,
                task_id,
                sync_type,
                target_system,
                status,
                json.dumps(request_snapshot, ensure_ascii=False),
                json.dumps(response_snapshot, ensure_ascii=False),
                utc_now(),
            ),
        )

    def _load_persisted_crm_option_cache(self) -> None:
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    "SELECT option_type, display_name, row_json FROM crm_option_cache"
                ).fetchall()
        except Exception:
            return
        for row in rows:
            option_type = str(row['option_type'] or '').strip()
            display_name = str(row['display_name'] or '').strip().lower()
            if not option_type or not display_name:
                continue
            try:
                payload = json.loads(row['row_json'] or '{}')
            except Exception:
                payload = {}
            if isinstance(payload, dict) and payload:
                self._crm_option_cache.setdefault(option_type, {})[display_name] = payload

    def _persist_crm_option_row(self, *, option_type: str, display_name: str, row: Dict[str, Any]) -> None:
        normalized_name = str(display_name or '').strip().lower()
        if not option_type or not normalized_name or not isinstance(row, dict) or not row:
            return
        try:
            with self.db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO crm_option_cache (option_type, display_name, row_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(option_type, display_name)
                    DO UPDATE SET row_json = excluded.row_json, updated_at = excluded.updated_at
                    """,
                    (
                        option_type,
                        normalized_name,
                        json.dumps(row, ensure_ascii=False),
                        utc_now(),
                    ),
                )
                conn.commit()
        except Exception:
            return

    def _resolve_lead_notification_context(self, conn: sqlite3.Connection, lead_id: str) -> tuple[str, Optional[str]]:
        lead = conn.execute(
            'SELECT mobile, yw_id FROM leads WHERE lead_id = ?',
            (lead_id,),
        ).fetchone()
        mobile = str((lead['mobile'] if lead else '') or '')
        yw_id = str((lead['yw_id'] if lead else '') or '').strip() or None
        return mobile, yw_id

    def _auto_resolve_prior_failed_notifications(self, conn: sqlite3.Connection, *, lead_id: str) -> None:
        conn.execute(
            """
            UPDATE operator_notifications
            SET is_read = 1, read_at = ?, read_by = ?
            WHERE lead_id = ?
              AND notification_type = 'crm_record_failed'
              AND is_read = 0
            """,
            (utc_now(), 'system:auto_resolved', lead_id),
        )

    def _notification_recent_duplicate_exists(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
        notification_type: str,
        write_result: str,
        reason: Optional[str],
        window_seconds: int = 900,
    ) -> bool:
        row = conn.execute(
            """
            SELECT created_at FROM operator_notifications
            WHERE lead_id = ?
              AND notification_type = ?
              AND write_result = ?
              AND COALESCE(reason, '') = COALESCE(?, '')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (lead_id, notification_type, write_result, reason),
        ).fetchone()
        if not row:
            return False
        try:
            return abs((parse_iso_datetime(utc_now()) - parse_iso_datetime(str(row['created_at'] or ''))).total_seconds()) <= window_seconds
        except Exception:
            return False

    def _queue_operator_notification(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
        notification_type: str,
        mobile: str,
        yw_id: Optional[str],
        write_result: str,
        reason: Optional[str] = None,
    ) -> None:
        if notification_type == 'crm_record_success' or write_result == 'success':
            self._auto_resolve_prior_failed_notifications(conn, lead_id=lead_id)
        elif self._notification_recent_duplicate_exists(
            conn,
            lead_id=lead_id,
            notification_type=notification_type,
            write_result=write_result,
            reason=reason,
        ):
            return
        conn.execute(
            """
            INSERT INTO operator_notifications (
                notification_id, lead_id, notification_type, mobile, yw_id, write_result, reason, is_read, read_at, read_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                create_id("notify"),
                lead_id,
                notification_type,
                mobile,
                yw_id,
                write_result,
                reason,
                0,
                None,
                None,
                utc_now(),
            ),
        )

    def _record_verified_crm_state(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
        crm_payload: Dict[str, Any],
        official_group: Optional[str] = None,
    ) -> None:
        conn.execute(
            """
            UPDATE leads
            SET crm_verified_payload = ?,
                crm_verified_app_name = ?,
                crm_verified_dept_name = ?,
                crm_verified_registration_group = ?,
                crm_verified_official_group = COALESCE(?, crm_verified_official_group),
                crm_verified_at = ?,
                updated_at = ?
            WHERE lead_id = ?
            """,
            (
                json.dumps(crm_payload, ensure_ascii=False),
                str(crm_payload.get('appName') or '').strip() or None,
                str(crm_payload.get('deptName') or '').strip() or None,
                str(crm_payload.get('pendaftaranGroup') or '').strip() or None,
                str(official_group or '').strip() or None,
                utc_now(),
                utc_now(),
                lead_id,
            ),
        )

    @staticmethod
    def _sync_log_indicates_verified_crm_success(response_snapshot: Dict[str, Any]) -> bool:
        if not isinstance(response_snapshot, dict):
            return False
        verified_after_write = response_snapshot.get('verified_after_write')
        if verified_after_write is True:
            return True
        if verified_after_write not in (None, False):
            return bool(verified_after_write)
        crm_response = response_snapshot.get('crm_response') or {}
        crm_code = crm_response.get('code') if isinstance(crm_response, dict) else None
        if crm_code != 0:
            return False
        action = str(response_snapshot.get('action') or '').strip().lower()
        return action in {'create', 'verify_before_retry'}

    def _restore_verified_crm_state_from_sync_logs(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
    ) -> bool:
        rows = conn.execute(
            """
            SELECT request_snapshot, response_snapshot
            FROM sync_logs
            WHERE lead_id = ?
              AND sync_type = 'customer_upsert'
              AND status = 'success'
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (lead_id,),
        ).fetchall()
        for sync_row in rows:
            try:
                response_snapshot = json.loads(sync_row['response_snapshot'] or '{}')
            except Exception:
                response_snapshot = {}
            if not self._sync_log_indicates_verified_crm_success(response_snapshot):
                continue
            try:
                request_snapshot = json.loads(sync_row['request_snapshot'] or '{}')
            except Exception:
                request_snapshot = {}
            if not isinstance(request_snapshot, dict):
                continue
            app_name = str(request_snapshot.get('appName') or '').strip()
            registration_group = str(request_snapshot.get('pendaftaranGroup') or '').strip()
            if not app_name or not registration_group:
                continue
            self._record_verified_crm_state(
                conn,
                lead_id=lead_id,
                crm_payload=request_snapshot,
                official_group=str(request_snapshot.get('wa') or '').strip() or None,
            )
            return True
        return False

    def _find_recent_verified_duplicate_lead(
        self,
        conn: sqlite3.Connection,
        *,
        mobile: str,
        area_code: int,
        account_id: Optional[str],
        app_name: Optional[str],
        dept_name: Optional[str],
        registration_group: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        normalized_mobile = str(mobile or '').strip()
        normalized_account_id = str(account_id or '').strip()
        normalized_app_name = str(app_name or '').strip().lower()
        normalized_dept_name = str(dept_name or '').strip().lower()
        normalized_registration_group = str(registration_group or '').strip().lower()
        if not normalized_mobile or not normalized_account_id or not normalized_app_name:
            return None
        row = conn.execute(
            """
            SELECT lead_id, mobile, yw_id, app_name, dept_name, pendaftaran_group,
                   crm_verified_app_name, crm_verified_dept_name, crm_verified_registration_group
            FROM leads
            WHERE area_code = ? AND mobile = ? AND COALESCE(yw_id, '') = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (area_code, normalized_mobile, normalized_account_id),
        ).fetchone()
        if not row:
            return None
        if not row['crm_verified_app_name'] and not row['crm_verified_registration_group']:
            if self._restore_verified_crm_state_from_sync_logs(conn, lead_id=str(row['lead_id'] or '').strip()):
                row = conn.execute(
                    """
                    SELECT lead_id, mobile, yw_id, app_name, dept_name, pendaftaran_group,
                           crm_verified_app_name, crm_verified_dept_name, crm_verified_registration_group
                    FROM leads
                    WHERE lead_id = ?
                    LIMIT 1
                    """,
                    (row['lead_id'],),
                ).fetchone() or row
        effective_app_name = str(row['crm_verified_app_name'] or row['app_name'] or '').strip().lower()
        effective_dept_name = str(row['crm_verified_dept_name'] or row['dept_name'] or '').strip().lower()
        effective_group = str(row['crm_verified_registration_group'] or row['pendaftaran_group'] or '').strip().lower()
        if effective_app_name != normalized_app_name:
            return None
        if normalized_dept_name and effective_dept_name and effective_dept_name != normalized_dept_name:
            return None
        if normalized_registration_group and effective_group and effective_group != normalized_registration_group:
            return None
        sync_rows = conn.execute(
            "SELECT status, response_snapshot FROM sync_logs WHERE lead_id = ? ORDER BY created_at DESC LIMIT 50",
            (row['lead_id'],),
        ).fetchall()
        for latest_sync in sync_rows:
            if str(latest_sync['status'] or '').strip().lower() != 'success':
                continue
            try:
                snapshot = json.loads(latest_sync['response_snapshot'] or '{}')
            except Exception:
                snapshot = {}
            if self._sync_log_indicates_verified_crm_success(snapshot):
                return dict(row)
        return None

    def _find_recent_cross_channel_duplicate_submission(
        self,
        conn: sqlite3.Connection,
        *,
        mobile: str,
        area_code: int,
        account_id: Optional[str],
        app_name: Optional[str],
        dept_name: Optional[str],
        registration_group: Optional[str],
        source_channel: str,
        submitted_at: str,
        window_seconds: int = 120,
    ) -> Optional[Dict[str, Any]]:
        normalized_mobile = str(mobile or '').strip()
        normalized_account_id = str(account_id or '').strip()
        normalized_app_name = str(app_name or '').strip().lower()
        normalized_dept_name = str(dept_name or '').strip().lower()
        normalized_registration_group = str(registration_group or '').strip().lower()
        if not normalized_mobile or not normalized_account_id:
            return None
        submitted_dt = parse_iso_datetime(submitted_at)
        rows = conn.execute(
            """
            SELECT s.submission_id, s.lead_id, s.source_channel, s.submitted_by, s.submitted_at, s.created_at,
                   l.area_code, l.mobile, l.yw_id, l.app_name, l.dept_name, l.pendaftaran_group
            FROM account_submissions s
            JOIN leads l ON l.lead_id = s.lead_id
            WHERE l.area_code = ? AND l.mobile = ? AND COALESCE(l.yw_id, '') = ?
            ORDER BY s.created_at DESC
            LIMIT 10
            """,
            (area_code, normalized_mobile, normalized_account_id),
        ).fetchall()
        for row in rows:
            existing_source = str(row['source_channel'] or '').strip()
            if not existing_source or existing_source == str(source_channel or '').strip():
                continue
            existing_app_name = str(row['app_name'] or '').strip().lower()
            existing_dept_name = str(row['dept_name'] or '').strip().lower()
            existing_registration_group = str(row['pendaftaran_group'] or '').strip().lower()
            if (
                existing_app_name != normalized_app_name
                or existing_dept_name != normalized_dept_name
                or existing_registration_group != normalized_registration_group
            ):
                continue
            try:
                existing_dt = parse_iso_datetime(str(row['submitted_at'] or row['created_at'] or ''))
            except Exception:
                existing_dt = parse_iso_datetime(str(row['created_at'] or submitted_at))
            if abs((submitted_dt - existing_dt).total_seconds()) <= window_seconds:
                return dict(row)
        return None

    def _build_duplicate_submission_response(
        self,
        conn: sqlite3.Connection,
        *,
        duplicate_submission: Dict[str, Any],
        parsed_result: Dict[str, Any],
        accepted_override: Optional[bool] = None,
        result_reason_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        lead_row = conn.execute("SELECT lead_id, matched_customer_id, current_status FROM leads WHERE lead_id = ?", (duplicate_submission['lead_id'],)).fetchone()
        latest_sync = conn.execute(
            "SELECT status, response_snapshot FROM sync_logs WHERE lead_id = ? ORDER BY created_at DESC LIMIT 1",
            (duplicate_submission['lead_id'],),
        ).fetchone()
        response = {
            'deduped': True,
            'duplicate_submission_id': duplicate_submission.get('submission_id'),
            'duplicate_source_channel': duplicate_submission.get('source_channel'),
            'lead_id': duplicate_submission.get('lead_id'),
            'matched_customer_id': lead_row['matched_customer_id'] if lead_row else None,
            'parsed_payload': parsed_result,
            'reply_phone': f"+{parsed_result.get('area_code')} {parsed_result.get('mobile')}" if parsed_result.get('area_code') and parsed_result.get('mobile') else (parsed_result.get('mobile') or '-'),
            'reply_id': parsed_result.get('account_id') or '-',
            'reply_group': parsed_result.get('registration_group') or '-',
        }
        if latest_sync and str(latest_sync['status'] or '').strip().lower() == 'success':
            verified_after_write = False
            if latest_sync['response_snapshot']:
                try:
                    snapshot = json.loads(latest_sync['response_snapshot'])
                except Exception:
                    snapshot = {}
                verified_after_write = bool(snapshot.get('verified_after_write'))
            accepted_success = True if accepted_override is None else bool(accepted_override)
            if accepted_success:
                response.update({
                    'accepted': True,
                    'next_action': 'queue_group_join',
                    'lead_status': lead_row['current_status'] if lead_row else 'bind_success',
                    'crm_verified': verified_after_write,
                    'current_submission_crm_verified': verified_after_write,
                })
            else:
                response.update({
                    'accepted': False,
                    'reason': 'crm_sync_failed',
                    'result_reason': result_reason_override or 'Data duplication.',
                    'next_action': 'retry_crm_sync',
                    'lead_status': lead_row['current_status'] if lead_row else 'bind_success',
                })
        else:
            result_reason = result_reason_override or 'Duplicate intake ignored after previous failed attempt.'
            if latest_sync and latest_sync['response_snapshot']:
                try:
                    snapshot = json.loads(latest_sync['response_snapshot'])
                except Exception:
                    snapshot = {}
                mapping_failure = str(snapshot.get('mapping_failure') or '').strip()
                if mapping_failure:
                    result_reason = mapping_failure
                else:
                    crm_response = snapshot.get('crm_response') or {}
                    if crm_response:
                        result_reason = self._normalize_crm_failure_reason(crm_response, fallback_found=False)
            response.update({
                'accepted': False,
                'reason': 'crm_sync_failed',
                'result_reason': result_reason,
                'next_action': 'retry_crm_sync',
                'lead_status': lead_row['current_status'] if lead_row else 'bind_failed',
            })
        response['reply_text'] = self._format_lark_reply_text(response)
        return response

    def _build_simulated_bind_result(self, *, lead_id: str, task_id: str, lead: Dict[str, Any], submission_id: str, account_id: Optional[str], source_channel: str) -> Dict[str, Any]:
        context = {
            'lead_id': lead_id,
            'task_id': task_id,
            'submission_id': submission_id,
            'account_id': str(account_id or ''),
            'mobile': str(lead.get('mobile') or ''),
            'app_name': str(lead.get('app_name') or ''),
            'dept_name': str(lead.get('dept_name') or ''),
            'registration_group': str(lead.get('pendaftaran_group') or ''),
            'source_channel': str(source_channel or ''),
        }
        if callable(self.bind_simulator):
            simulated = self.bind_simulator(context)
            if not isinstance(simulated, dict):
                raise RuntimeError('bind simulator must return a dict')
            return simulated

        resolved_dept = self._resolve_crm_dept_mapping(context['dept_name'])
        if self._bind_random.random() < self.auto_bind_simulation_success_rate:
            return {
                'status': 'success',
                'result_code': 'bind_ok_simulated',
                'result_reason': 'simulated bind success',
                'raw_result': {
                    'guild_code': context['dept_name'],
                    'deptName': resolved_dept['deptName'],
                    'deptId': resolved_dept['deptId'],
                    'simulated': True,
                },
            }
        failure_reason = self._bind_random.choice([
            'already joined another guild',
            'device account limit reached',
            'account id not eligible for binding',
        ])
        return {
            'status': 'failed',
            'result_code': 'bind_failed_simulated',
            'result_reason': failure_reason,
            'raw_result': {
                'guild_code': context['dept_name'],
                'simulated': True,
            },
        }

    def _maybe_auto_simulate_bind_after_intake(self, *, lead: Dict[str, Any], payload: ManualCsSubmissionRequest, parsed_result: Dict[str, Any], account_submission: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.auto_bind_simulation:
            return None
        if account_submission.get('next_action') != 'queue_bind_check':
            return None
        task_id = str(account_submission.get('task_id') or '')
        if not task_id:
            return None
        simulated = self._build_simulated_bind_result(
            lead_id=lead['lead_id'],
            task_id=task_id,
            lead=lead,
            submission_id=str(account_submission.get('submission_id') or ''),
            account_id=parsed_result.get('account_id'),
            source_channel=payload.source_channel,
        )
        bind_result = self.bind_check_result(
            task_id,
            BindCheckResultRequest(
                status=str(simulated.get('status') or 'failed'),
                result_code=simulated.get('result_code'),
                result_reason=simulated.get('result_reason'),
                finished_at=payload.submitted_at,
                raw_result=simulated.get('raw_result') or {},
            ),
        )
        accepted = bind_result.get('next_action') == 'queue_group_join'
        response = {
            'accepted': accepted,
            'simulation_applied': True,
            'simulated_bind_status': str(simulated.get('status') or ''),
            'task_id': task_id,
            'submission_id': account_submission.get('submission_id'),
            'lead_id': lead['lead_id'],
            'matched_customer_id': lead.get('matched_customer_id'),
            'next_action': bind_result.get('next_action'),
            'lead_status': bind_result.get('lead_status'),
            'routing_decision': 'queue_bind_check',
            'review_reason_codes': [],
            'parsed_payload': parsed_result,
            'reply_phone': parsed_result.get('mobile') or '-',
            'reply_id': parsed_result.get('account_id') or '-',
            'reply_group': parsed_result.get('registration_group') or '-',
            'result_reason': simulated.get('result_reason') or bind_result.get('result_reason') or '',
            'result_code': simulated.get('result_code') or bind_result.get('result_code') or '',
        }
        if bind_result.get('reason') == 'crm_sync_failed':
            response['reason'] = 'crm_sync_failed'
            response['result_reason'] = bind_result.get('result_reason') or response['result_reason']
        elif bind_result.get('reason') == 'crm_sync_retry_pending':
            response['reason'] = 'crm_sync_retry_pending'
            response['result_reason'] = bind_result.get('result_reason') or response['result_reason']
        elif not accepted:
            bind_reason = str(bind_result.get('reason') or '').strip()
            response['reason'] = bind_reason if bind_reason == 'bind_backend_guild_mismatch' else 'simulated_bind_failed'
            response['result_reason'] = bind_result.get('result_reason') or response['result_reason']
        return response

    def operator_notifications(self, *, status: Optional[str] = None, query: Optional[str] = None) -> Dict[str, Any]:
        sql = """
            SELECT notification_id, lead_id, notification_type, mobile, yw_id, write_result, reason,
                   is_read, read_at, read_by, created_at
            FROM operator_notifications
        """
        conditions = []
        params: list[Any] = []
        if status == 'unread':
            conditions.append('is_read = 0')
        elif status == 'read':
            conditions.append('is_read = 1')
        if query:
            conditions.append('(mobile LIKE ? OR COALESCE(yw_id, \'\') LIKE ?)')
            like = f"%{query}%"
            params.extend([like, like])
        if conditions:
            sql += ' WHERE ' + ' AND '.join(conditions)
        sql += ' ORDER BY created_at DESC'
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
            for row in rows:
                row['is_read'] = bool(row['is_read'])
                row['message_title'] = 'Lark收口通知'
                message_lines = [
                    f"用户手机: {row.get('mobile') or ''}",
                    f"用户ID: {row.get('yw_id') or ''}",
                    f"写入结果: {row.get('write_result') or ''}",
                ]
                if row.get('reason'):
                    message_lines.append(f"失败原因: {row['reason']}")
                row['message_text'] = "\n".join(message_lines)
            return {"rows": rows}

    def mark_operator_notification_read(self, notification_id: str, *, read_by: Optional[str] = None) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute('SELECT notification_id FROM operator_notifications WHERE notification_id = ?', (notification_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail='notification not found')
            conn.execute(
                'UPDATE operator_notifications SET is_read = 1, read_at = ?, read_by = ? WHERE notification_id = ?',
                (utc_now(), read_by, notification_id),
            )
            return {'notification_id': notification_id, 'updated': True}

    def evaluate_approval_batch(self, payload: ApprovalBatchEvaluateRequest) -> Dict[str, Any]:
        rules = {
            'registration_group': {'batch_size': 30, 'timeout_minutes': 30},
            'official_group': {'batch_size': 10, 'timeout_minutes': 30},
        }
        if payload.approval_type not in rules:
            raise HTTPException(status_code=400, detail='unsupported approval_type')
        default_rule = rules[payload.approval_type]
        rule = {
            'batch_size': _coerce_positive_int(payload.batch_size, default_rule['batch_size']),
            'timeout_minutes': _coerce_positive_int(payload.timeout_minutes, default_rule['timeout_minutes']),
        }
        pending_count = max(int(payload.pending_count), 0)
        now = parse_iso_datetime(payload.now)
        cycle_end = self._approval_cycle_next_boundary(now=now, timeout_minutes=rule['timeout_minutes'])
        cycle_start = cycle_end - timedelta(minutes=rule['timeout_minutes'])
        remaining_seconds = max(int((cycle_end - now).total_seconds()), 0)
        remaining_minutes = max((remaining_seconds + 59) // 60, 0)

        if pending_count <= 0:
            return {
                'approval_type': payload.approval_type,
                'registration_group': payload.registration_group,
                'pending_count': pending_count,
                'oldest_pending_at': payload.oldest_pending_at,
                'ready': False,
                'release_count': 0,
                'reason_code': 'waiting_next_cycle',
                'batch_size': rule['batch_size'],
                'timeout_minutes': rule['timeout_minutes'],
                'elapsed_minutes': max(0, int((now - cycle_start).total_seconds() // 60)),
                'remaining_minutes': remaining_minutes,
                'remaining_seconds': remaining_seconds,
                'cycle_started_at': cycle_start.isoformat(),
                'cycle_ends_at': cycle_end.isoformat(),
            }

        oldest = parse_iso_datetime(payload.oldest_pending_at)
        next_boundary_after_oldest = self._approval_cycle_next_boundary(now=oldest, timeout_minutes=rule['timeout_minutes'])
        active_cycle_start = next_boundary_after_oldest - timedelta(minutes=rule['timeout_minutes'])
        elapsed_minutes = max(0, int((now - active_cycle_start).total_seconds() // 60))
        if pending_count >= rule['batch_size']:
            return {
                'approval_type': payload.approval_type,
                'registration_group': payload.registration_group,
                'pending_count': pending_count,
                'oldest_pending_at': payload.oldest_pending_at,
                'ready': True,
                'release_count': pending_count,
                'reason_code': 'batch_size_reached',
                'batch_size': rule['batch_size'],
                'timeout_minutes': rule['timeout_minutes'],
                'elapsed_minutes': elapsed_minutes,
                'remaining_minutes': 0,
                'remaining_seconds': 0,
                'cycle_started_at': active_cycle_start.isoformat(),
                'cycle_ends_at': next_boundary_after_oldest.isoformat(),
            }
        if now >= next_boundary_after_oldest:
            return {
                'approval_type': payload.approval_type,
                'registration_group': payload.registration_group,
                'pending_count': pending_count,
                'oldest_pending_at': payload.oldest_pending_at,
                'ready': True,
                'release_count': pending_count,
                'reason_code': 'timeout_flush',
                'batch_size': rule['batch_size'],
                'timeout_minutes': rule['timeout_minutes'],
                'elapsed_minutes': elapsed_minutes,
                'remaining_minutes': 0,
                'remaining_seconds': 0,
                'cycle_started_at': active_cycle_start.isoformat(),
                'cycle_ends_at': next_boundary_after_oldest.isoformat(),
            }
        remaining_seconds = max(int((next_boundary_after_oldest - now).total_seconds()), 0)
        remaining_minutes = max((remaining_seconds + 59) // 60, 0)
        return {
            'approval_type': payload.approval_type,
            'registration_group': payload.registration_group,
            'pending_count': pending_count,
            'oldest_pending_at': payload.oldest_pending_at,
            'ready': False,
            'release_count': 0,
            'reason_code': 'waiting_for_batch',
            'batch_size': rule['batch_size'],
            'timeout_minutes': rule['timeout_minutes'],
            'elapsed_minutes': elapsed_minutes,
            'remaining_minutes': remaining_minutes,
            'remaining_seconds': remaining_seconds,
            'cycle_started_at': active_cycle_start.isoformat(),
            'cycle_ends_at': next_boundary_after_oldest.isoformat(),
        }

    @staticmethod
    def _approval_cycle_next_boundary(*, now: datetime, timeout_minutes: int) -> datetime:
        interval_seconds = max(int(timeout_minutes or 0), 1) * 60
        local_tz = timezone(timedelta(hours=8))
        localized_now = now.astimezone(local_tz)
        local_day_start = localized_now.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed_seconds = int((localized_now - local_day_start).total_seconds())
        next_boundary_seconds = ((elapsed_seconds // interval_seconds) + 1) * interval_seconds
        next_boundary_local = local_day_start + timedelta(seconds=next_boundary_seconds)
        return next_boundary_local.astimezone(now.tzinfo or timezone.utc)

    @staticmethod
    def _binding_oldest_pending_at(probe: Dict[str, Any]) -> Optional[str]:
        requesters = list(probe.get('requesters') or []) if isinstance(probe.get('requesters'), list) else []
        oldest_candidates: List[str] = []
        for requester in requesters:
            if not isinstance(requester, dict):
                continue
            requested_at_iso = str(requester.get('requestedAtIso') or '').strip()
            if requested_at_iso:
                oldest_candidates.append(requested_at_iso)
                continue
            requested_at_unix = requester.get('requestedAtUnix')
            if requested_at_unix not in (None, ''):
                try:
                    oldest_candidates.append(datetime.fromtimestamp(float(requested_at_unix), tz=timezone.utc).isoformat())
                except Exception:
                    pass
        return min(oldest_candidates) if oldest_candidates else None

    @staticmethod
    def _binding_next_approval_eta_text(*, pending_count: int, batch_size: int, timeout_minutes: int, elapsed_minutes: int, ready: bool, reason_code: str, remaining_minutes: int) -> str:
        if pending_count <= 0:
            return f'当前无待审批；约{remaining_minutes}分钟后进入下一轮审批'
        if ready:
            if reason_code == 'batch_size_reached':
                return '已达到人数阈值，可立即审批'
            if reason_code == 'timeout_flush':
                return '已达到超时阈值，可立即审批'
            return '当前可立即审批'
        return f'约{remaining_minutes}分钟后自动审批；若先满{batch_size}人则提前审批'

    def _build_binding_next_approval_runtime(self, *, responsible_type: str, binding: Dict[str, Any], probe: Dict[str, Any]) -> Dict[str, Any]:
        normalized_type = str(responsible_type or '').strip()
        if normalized_type not in {'registration_group', 'official_group'}:
            return {}
        pending_count = max(int(probe.get('pending_count') or 0), 0)
        oldest_pending_at = self._binding_oldest_pending_at(probe)
        batch_size = max(int(binding.get('approval_count_threshold') or 0), 1)
        timeout_minutes = max(int(binding.get('approval_timeout_minutes') or 0), 1)
        now = parse_iso_datetime(utc_now())
        if pending_count <= 0:
            cycle_end = self._approval_cycle_next_boundary(now=now, timeout_minutes=timeout_minutes)
            remaining_seconds = max(int((cycle_end - now).total_seconds()), 0)
            remaining_minutes = max((remaining_seconds + 59) // 60, 0)
            elapsed_minutes = max(0, timeout_minutes - remaining_minutes)
            return {
                'next_approval_ready': False,
                'next_approval_reason_code': 'waiting_next_cycle',
                'next_approval_eta_text': self._binding_next_approval_eta_text(
                    pending_count=pending_count,
                    batch_size=batch_size,
                    timeout_minutes=timeout_minutes,
                    elapsed_minutes=elapsed_minutes,
                    ready=False,
                    reason_code='waiting_next_cycle',
                    remaining_minutes=remaining_minutes,
                ),
                'next_approval_pending_count': pending_count,
                'next_approval_batch_size': batch_size,
                'next_approval_timeout_minutes': timeout_minutes,
                'next_approval_elapsed_minutes': elapsed_minutes,
                'next_approval_remaining_minutes': remaining_minutes,
                'next_approval_remaining_seconds': remaining_seconds,
                'next_approval_oldest_pending_at': oldest_pending_at,
            }
        if not oldest_pending_at:
            return {
                'next_approval_ready': False,
                'next_approval_reason_code': 'oldest_pending_unknown',
                'next_approval_eta_text': '已有待审批，等待更多实时数据后再计算',
                'next_approval_pending_count': pending_count,
                'next_approval_batch_size': batch_size,
                'next_approval_timeout_minutes': timeout_minutes,
                'next_approval_elapsed_minutes': 0,
                'next_approval_remaining_minutes': timeout_minutes,
                'next_approval_remaining_seconds': timeout_minutes * 60,
                'next_approval_oldest_pending_at': oldest_pending_at,
            }
        oldest = parse_iso_datetime(oldest_pending_at)
        cycle_end = self._approval_cycle_next_boundary(now=oldest, timeout_minutes=timeout_minutes)
        cycle_start = cycle_end - timedelta(minutes=timeout_minutes)
        elapsed_minutes = max(0, int((now - cycle_start).total_seconds() // 60))
        ready = pending_count >= batch_size or now >= cycle_end
        reason_code = 'batch_size_reached' if pending_count >= batch_size else ('timeout_flush' if now >= cycle_end else 'waiting_for_batch')
        remaining_seconds = 0 if ready else max(int((cycle_end - now).total_seconds()), 0)
        remaining_minutes = 0 if ready else max((remaining_seconds + 59) // 60, 0)
        return {
            'next_approval_ready': ready,
            'next_approval_reason_code': reason_code,
            'next_approval_eta_text': self._binding_next_approval_eta_text(
                pending_count=pending_count,
                batch_size=batch_size,
                timeout_minutes=timeout_minutes,
                elapsed_minutes=elapsed_minutes,
                ready=ready,
                reason_code=reason_code,
                remaining_minutes=remaining_minutes,
            ),
            'next_approval_pending_count': pending_count,
            'next_approval_batch_size': batch_size,
            'next_approval_timeout_minutes': timeout_minutes,
            'next_approval_elapsed_minutes': elapsed_minutes,
            'next_approval_remaining_minutes': remaining_minutes,
            'next_approval_remaining_seconds': remaining_seconds,
            'next_approval_oldest_pending_at': oldest_pending_at,
        }

    def _request_whatsapp_approval_group_state(self, base_url: str, registration_group: str, *, timeout_seconds: float = 30.0) -> Dict[str, Any]:
        normalized_base_url = str(base_url or '').strip().rstrip('/')
        normalized_group = str(registration_group or '').strip()
        if not normalized_base_url:
            raise RuntimeError('whatsapp approval runtime base_url is required')
        if not normalized_group:
            raise RuntimeError('registration_group is required')
        return fetch_json(
            f"{normalized_base_url}/group-state",
            method='POST',
            payload={'registration_group': normalized_group},
            timeout=max(float(timeout_seconds or 0.0), 0.1),
        )

    def _request_whatsapp_approval_group_state_with_retry(
        self,
        base_url: str,
        registration_group: str,
        *,
        attempts: int = 3,
        retry_delay_seconds: float = 0.0,
        timeout_seconds: float = 30.0,
    ) -> Dict[str, Any]:
        normalized_attempts = max(1, int(attempts or 1))
        last_error: Optional[Exception] = None
        for index in range(normalized_attempts):
            try:
                try:
                    return self._request_whatsapp_approval_group_state(base_url, registration_group, timeout_seconds=timeout_seconds)
                except TypeError as exc:
                    if 'timeout_seconds' not in str(exc):
                        raise
                    return self._request_whatsapp_approval_group_state(base_url, registration_group)
            except Exception as exc:
                last_error = exc
                if index >= normalized_attempts - 1:
                    break
                if retry_delay_seconds > 0:
                    time.sleep(retry_delay_seconds)
        if last_error is not None:
            raise last_error
        raise RuntimeError('group state probe failed without error')

    @staticmethod
    def _whatsapp_binding_probe_target(binding: Dict[str, Any]) -> str:
        return (
            str(binding.get('group_id') or '').strip()
            or str(binding.get('link') or '').strip()
            or str(binding.get('registration_group') or '').strip()
            or str(binding.get('group_name') or '').strip()
        )

    def _probe_whatsapp_binding_group_state(
        self,
        *,
        responsible_type: str,
        binding: Dict[str, Any],
        runtime_state: Optional[Dict[str, Any]] = None,
        session_state: Optional[Dict[str, Any]] = None,
        allow_shared_fallback: bool = True,
        attempts: int = 3,
        retry_delay_seconds: float = 0.0,
        timeout_seconds: float = 30.0,
    ) -> Dict[str, Any]:
        normalized_type = str(responsible_type or '').strip()
        target = self._whatsapp_binding_probe_target(binding)
        if not normalized_type or not target:
            return {}
        candidate_base_urls: List[str] = []
        runtime_state = dict(runtime_state or {})
        session_state = dict(session_state or {})
        runtime_base_url = str(runtime_state.get('base_url') or '').strip().rstrip('/')
        if runtime_state.get('active') and runtime_base_url:
            if normalized_type == 'official_group':
                if bool(session_state.get('login_verified')):
                    candidate_base_urls.append(runtime_base_url)
            else:
                candidate_base_urls.append(runtime_base_url)
        if normalized_type == 'registration_group' and allow_shared_fallback:
            config = self.get_production_ops_daemon_config().get('config') or {}
            shared_base_url = str(config.get('worker_base_url') or 'http://127.0.0.1:8787').strip().rstrip('/')
            if shared_base_url and shared_base_url not in candidate_base_urls:
                candidate_base_urls.append(shared_base_url)
        last_error = None
        for base_url in candidate_base_urls:
            try:
                payload = self._request_whatsapp_approval_group_state_with_retry(
                    base_url,
                    target,
                    attempts=attempts,
                    retry_delay_seconds=retry_delay_seconds,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:
                last_error = exc
                continue
            if isinstance(payload, dict):
                normalized = dict(payload)
                normalized['source_base_url'] = base_url
                normalized['probe_target'] = target
                return normalized
        if last_error:
            return {'error': str(last_error), 'probe_target': target}
        return {}

    def _apply_live_group_identity_to_binding(
        self,
        binding: Dict[str, Any],
        *,
        responsible_type: str,
        runtime_state: Optional[Dict[str, Any]] = None,
        session_state: Optional[Dict[str, Any]] = None,
        allow_shared_fallback: bool = True,
        overwrite_existing_name: bool = False,
        attempts: int = 3,
        retry_delay_seconds: float = 0.0,
        timeout_seconds: float = 30.0,
    ) -> Dict[str, Any]:
        probe = self._probe_whatsapp_binding_group_state(
            responsible_type=responsible_type,
            binding=binding,
            runtime_state=runtime_state,
            session_state=session_state,
            allow_shared_fallback=allow_shared_fallback,
            attempts=attempts,
            retry_delay_seconds=retry_delay_seconds,
            timeout_seconds=timeout_seconds,
        )
        live_group_name = str(probe.get('group_name') or '').strip()
        live_group_id = str(probe.get('group_id') or '').strip()
        if live_group_name and (overwrite_existing_name or not str(binding.get('group_name') or '').strip()):
            binding['group_name'] = live_group_name
        if live_group_id and not str(binding.get('group_id') or '').strip():
            binding['group_id'] = live_group_id
        return probe

    def _persist_registration_group_binding_live_names(
        self,
        account_key: str,
        bindings: list[dict[str, Any]],
        runtime_rows: list[dict[str, Any]],
        binding_verifiers: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key or not bindings:
            return bindings
        changed = False
        updated_bindings: list[dict[str, Any]] = []
        live_ready_statuses = {'mapped_live_probe_ready', 'inferred_live_probe_ready', 'live_probe_ready'}
        for binding, runtime_row, verifier in zip(bindings, runtime_rows, binding_verifiers):
            current = dict(binding or {})
            status = str((verifier or {}).get('status') or '').strip()
            live_group_name = str((runtime_row or {}).get('runtime_probe_group_name') or '').strip()
            live_group_id = str((runtime_row or {}).get('runtime_probe_group_id') or '').strip()
            if status in live_ready_statuses:
                if live_group_name and live_group_name != str(current.get('group_name') or '').strip():
                    current['group_name'] = live_group_name
                    changed = True
                if live_group_id and live_group_id != str(current.get('group_id') or '').strip():
                    current['group_id'] = live_group_id
                    changed = True
            updated_bindings.append(current)
        if not changed:
            return updated_bindings
        now_iso = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                'UPDATE whatsapp_approval_accounts SET group_links = ?, updated_at = ? WHERE account_key = ?',
                (json.dumps(updated_bindings, ensure_ascii=False), now_iso, normalized_key),
            )
            conn.commit()
        return updated_bindings

    def _registration_group_runtime_queue_rows(self, *, now_iso: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        seen_groups: set[str] = set()
        try:
            accounts_payload = self.list_whatsapp_approval_accounts() or {}
            accounts = list(accounts_payload.get('rows') or accounts_payload.get('accounts') or [])
        except Exception:
            accounts = []
        for account in accounts:
            if str(account.get('responsible_type') or '').strip() != 'registration_group':
                continue
            if not bool(account.get('enabled')):
                continue
            runtime_state = account.get('runtime_state') or {}
            account_key = str(account.get('account_key') or '').strip()
            worker_base_url = str(runtime_state.get('base_url') or account.get('worker_base_url') or '').strip()
            if not account_key or not bool(runtime_state.get('active')) or not worker_base_url:
                continue
            bindings = list(account.get('group_binding_runtimes') or account.get('group_link_bindings') or [])
            for binding in bindings:
                if not isinstance(binding, dict):
                    continue
                if binding.get('enabled') is False:
                    continue
                schedule_runtime = binding.get('schedule_runtime') or self._schedule_runtime(binding.get('schedule_windows') or [])
                if not bool(schedule_runtime.get('active_now')):
                    continue
                queue_group = (
                    str(binding.get('registration_group') or '').strip()
                    or str(binding.get('group_id') or '').strip()
                    or str(binding.get('link') or '').strip()
                    or str(binding.get('group_name') or '').strip()
                )
                binding_target = (
                    str(binding.get('group_id') or '').strip()
                    or str(binding.get('link') or '').strip()
                    or str(binding.get('group_name') or '').strip()
                    or queue_group
                )
                if not queue_group or not binding_target or queue_group in seen_groups:
                    continue
                requesters = list(binding.get('live_requesters') or []) if isinstance(binding.get('live_requesters'), list) else []
                pending_count = binding.get('pending_count')
                group_name = str(binding.get('group_name') or '').strip()
                group_id = str(binding.get('group_id') or '').strip()
                if pending_count is None:
                    try:
                        group_state = self._request_whatsapp_approval_group_state_with_retry(worker_base_url, binding_target)
                    except Exception:
                        continue
                    pending_count = max(int(group_state.get('pending_count') or 0), 0)
                    if not requesters:
                        requesters = list(group_state.get('requesters') or []) if isinstance(group_state.get('requesters'), list) else []
                    group_name = str(group_state.get('group_name') or group_name or queue_group).strip() or queue_group
                    group_id = str(group_state.get('group_id') or group_id).strip()
                else:
                    pending_count = max(int(pending_count or 0), 0)
                oldest_candidates: List[str] = []
                for requester in requesters:
                    if not isinstance(requester, dict):
                        continue
                    requested_at_iso = str(requester.get('requestedAtIso') or '').strip()
                    if requested_at_iso:
                        oldest_candidates.append(requested_at_iso)
                        continue
                    requested_at_unix = requester.get('requestedAtUnix')
                    if requested_at_unix not in (None, ''):
                        try:
                            oldest_candidates.append(datetime.fromtimestamp(float(requested_at_unix), tz=timezone.utc).isoformat())
                        except Exception:
                            pass
                oldest_pending_at = min(oldest_candidates) if pending_count > 0 and oldest_candidates else None
                evaluated = self.evaluate_approval_batch(
                    ApprovalBatchEvaluateRequest(
                        approval_type='registration_group',
                        registration_group=queue_group,
                        pending_count=pending_count,
                        oldest_pending_at=oldest_pending_at,
                        now=now_iso,
                        batch_size=int(binding.get('approval_count_threshold') or account.get('approval_count_threshold') or 0),
                        timeout_minutes=int(binding.get('approval_timeout_minutes') or account.get('approval_timeout_minutes') or 0),
                    )
                )
                evaluated.update({
                    'source': 'registration_runtime_group_state',
                    'binding_link': str(binding.get('link') or '').strip() or None,
                    'group_name': group_name or queue_group,
                    'group_id': group_id or None,
                    'account_key': account_key,
                    'account_name': str(account.get('account_name') or '').strip() or None,
                    'worker_base_url': worker_base_url or None,
                    'requesters': requesters,
                })
                rows.append(evaluated)
                seen_groups.add(queue_group)
        if rows:
            return rows
        try:
            registration_health = (self.runtime_health() or {}).get('registration_group_approval') or {}
        except Exception:
            registration_health = {}
        monitor_target = registration_health.get('monitor_target') or {}
        worker_base_url = str(monitor_target.get('worker_base_url') or registration_health.get('base_url') or '').strip()
        binding_target = str(monitor_target.get('registration_group') or '').strip()
        if not bool(registration_health.get('configured')) or not worker_base_url or not binding_target:
            return rows
        try:
            group_state = self._request_whatsapp_approval_group_state_with_retry(worker_base_url, binding_target)
        except Exception:
            return rows
        requesters = list(group_state.get('requesters') or []) if isinstance(group_state.get('requesters'), list) else []
        oldest_candidates: List[str] = []
        for requester in requesters:
            if not isinstance(requester, dict):
                continue
            requested_at_iso = str(requester.get('requestedAtIso') or '').strip()
            if requested_at_iso:
                oldest_candidates.append(requested_at_iso)
                continue
            requested_at_unix = requester.get('requestedAtUnix')
            if requested_at_unix not in (None, ''):
                try:
                    oldest_candidates.append(datetime.fromtimestamp(float(requested_at_unix), tz=timezone.utc).isoformat())
                except Exception:
                    pass
        pending_count = max(int(group_state.get('pending_count') or 0), 0)
        oldest_pending_at = min(oldest_candidates) if pending_count > 0 and oldest_candidates else None
        queue_group = str(group_state.get('group_name') or binding_target).strip() or binding_target
        evaluated = self.evaluate_approval_batch(
            ApprovalBatchEvaluateRequest(
                approval_type='registration_group',
                registration_group=queue_group,
                pending_count=pending_count,
                oldest_pending_at=oldest_pending_at,
                now=now_iso,
                batch_size=30,
                timeout_minutes=30,
            )
        )
        evaluated.update({
            'source': 'registration_runtime_monitor_target',
            'binding_link': None,
            'group_name': str(group_state.get('group_name') or queue_group).strip() or queue_group,
            'group_id': str(group_state.get('group_id') or binding_target).strip() or binding_target,
            'account_key': str(monitor_target.get('account_key') or '').strip() or None,
            'account_name': None,
            'worker_base_url': worker_base_url,
            'requesters': requesters,
        })
        return [evaluated]

    def _official_group_runtime_queue_rows(self, *, now_iso: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        seen_targets: set[str] = set()
        with self.db.connect() as conn:
            account_rows = conn.execute(
                "SELECT account_key, account_name, group_links, enabled FROM whatsapp_approval_accounts WHERE responsible_type = 'official_group' AND enabled = 1 ORDER BY updated_at DESC, account_key ASC"
            ).fetchall()
        for account_row in account_rows:
            account = dict(account_row)
            account_key = str(account.get('account_key') or '').strip()
            if not account_key:
                continue
            runtime_state = self._build_whatsapp_approval_runtime_state(account_key, allow_shared_fallback=False)
            if not bool(runtime_state.get('active')) or not str(runtime_state.get('base_url') or '').strip():
                continue
            try:
                bindings = json.loads(account.get('group_links') or '[]')
            except Exception:
                bindings = []
            normalized_bindings = _normalize_group_link_bindings(bindings if isinstance(bindings, list) else [])
            for binding in normalized_bindings:
                if binding.get('enabled') is False:
                    continue
                schedule_runtime = self._schedule_runtime(binding.get('schedule_windows') or [])
                if not bool(schedule_runtime.get('active_now')):
                    continue
                binding_target = (
                    str(binding.get('group_id') or '').strip()
                    or str(binding.get('link') or '').strip()
                    or str(binding.get('group_name') or '').strip()
                    or str(binding.get('registration_group') or '').strip()
                )
                if not binding_target or binding_target in seen_targets:
                    continue
                try:
                    group_state = self._request_whatsapp_approval_group_state_with_retry(str(runtime_state.get('base_url') or ''), binding_target)
                except Exception:
                    continue
                requesters = list(group_state.get('requesters') or []) if isinstance(group_state.get('requesters'), list) else []
                oldest_candidates: List[str] = []
                for requester in requesters:
                    if not isinstance(requester, dict):
                        continue
                    requested_at_iso = str(requester.get('requestedAtIso') or '').strip()
                    if requested_at_iso:
                        oldest_candidates.append(requested_at_iso)
                        continue
                    requested_at_unix = requester.get('requestedAtUnix')
                    if requested_at_unix not in (None, ''):
                        try:
                            oldest_candidates.append(datetime.fromtimestamp(float(requested_at_unix), tz=timezone.utc).isoformat())
                        except Exception:
                            pass
                pending_count = max(int(group_state.get('pending_count') or 0), 0)
                oldest_pending_at = min(oldest_candidates) if pending_count > 0 and oldest_candidates else None
                live_group_name = str(group_state.get('group_name') or '').strip()
                display_group = live_group_name or str(binding.get('group_name') or binding_target).strip() or binding_target
                routing_target = (
                    str(binding.get('registration_group') or '').strip()
                    or str(binding.get('link') or '').strip()
                    or str(group_state.get('group_id') or binding.get('group_id') or '').strip()
                    or display_group
                )
                evaluated = self.evaluate_approval_batch(
                    ApprovalBatchEvaluateRequest(
                        approval_type='official_group',
                        registration_group=display_group,
                        pending_count=max(int(group_state.get('pending_count') or 0), 0),
                        oldest_pending_at=oldest_pending_at,
                        now=now_iso,
                        batch_size=int(binding.get('approval_count_threshold') or 0),
                        timeout_minutes=int(binding.get('approval_timeout_minutes') or 0),
                    )
                )
                evaluated.update({
                    'source': 'official_runtime_group_state',
                    'target_group': routing_target,
                    'binding_link': str(binding.get('link') or '').strip() or None,
                    'binding_registration_group': str(binding.get('registration_group') or '').strip() or None,
                    'notify_profile_name': str(binding.get('notify_profile_name') or '').strip() or None,
                    'notify_robot_name': str(binding.get('notify_robot_name') or '').strip() or self._notify_robot_name(binding.get('notify_profile_name')) or None,
                    'group_name': display_group,
                    'group_id': str(group_state.get('group_id') or binding.get('group_id') or '').strip() or None,
                    'account_key': account_key,
                    'account_name': str(account.get('account_name') or '').strip() or None,
                    'worker_base_url': str(runtime_state.get('base_url') or '').strip() or None,
                    'requesters': requesters,
                })
                rows.append(evaluated)
                seen_targets.add(binding_target)
        return rows

    def approval_batch_queue(self) -> Dict[str, Any]:
        self.reconcile_task_residue()
        now = utc_now()
        registration_statuses = ('new', 'engaged', 'manual_review_pending', 'recognition_pending', 'account_submitted', 'bind_check_pending', 'bind_failed')
        official_statuses = ('bind_success', 'group_join_pending', 'group_join_failed', 'group_join_success', 'synced')
        with self.db.connect() as conn:
            registration_rows = [dict(r) for r in conn.execute(
                f"""
                SELECT pendaftaran_group AS registration_group, COUNT(*) AS pending_count,
                       MIN(updated_at) AS oldest_pending_at
                FROM leads
                WHERE pendaftaran_group IS NOT NULL
                  AND current_status IN ({','.join(['?'] * len(registration_statuses))})
                GROUP BY pendaftaran_group
                ORDER BY pending_count DESC, pendaftaran_group ASC
                """,
                registration_statuses,
            ).fetchall()]
            official_rows = [dict(r) for r in conn.execute(
                f"""
                SELECT pendaftaran_group AS registration_group, COUNT(*) AS pending_count,
                       MIN(updated_at) AS oldest_pending_at
                FROM leads
                WHERE pendaftaran_group IS NOT NULL
                  AND current_status IN ({','.join(['?'] * len(official_statuses))})
                GROUP BY pendaftaran_group
                ORDER BY pending_count DESC, pendaftaran_group ASC
                """,
                official_statuses,
            ).fetchall()]
        official_runtime_rows = self._official_group_runtime_queue_rows(now_iso=now)
        registration_runtime_rows = self._registration_group_runtime_queue_rows(now_iso=now)
        with self.db.connect() as conn:
            official_runtime_scope_configured = bool(conn.execute(
                "SELECT 1 FROM whatsapp_approval_accounts WHERE responsible_type = 'official_group' AND enabled = 1 LIMIT 1"
            ).fetchone())
        if official_runtime_scope_configured:
            evaluated_official_rows = official_runtime_rows
        else:
            evaluated_official_rows = [
                self.evaluate_approval_batch(
                    ApprovalBatchEvaluateRequest(
                        approval_type='official_group',
                        registration_group=row['registration_group'],
                        pending_count=row['pending_count'],
                        oldest_pending_at=row['oldest_pending_at'] or now,
                        now=now,
                    )
                ) for row in official_rows
            ]
        evaluated_registration_rows = registration_runtime_rows or [
            self.evaluate_approval_batch(
                ApprovalBatchEvaluateRequest(
                    approval_type='registration_group',
                    registration_group=row['registration_group'],
                    pending_count=row['pending_count'],
                    oldest_pending_at=row['oldest_pending_at'] or now,
                    now=now,
                )
            ) for row in registration_rows
        ]
        return {
            'registration_groups': evaluated_registration_rows,
            'official_groups': evaluated_official_rows,
        }

    def upsert_lead(self, payload: LeadUpsertRequest) -> Dict[str, Any]:
        now = utc_now()
        parser_confidence = getattr(payload, 'parser_confidence', None)
        parser_missing_fields = getattr(payload, 'parser_missing_fields', []) or []
        parser_conflicts = getattr(payload, 'parser_conflicts', []) or []
        parser_raw_text = getattr(payload, 'parser_raw_text', None)
        parser_raw_ocr_text = getattr(payload, 'parser_raw_ocr_text', None)
        parser_version = getattr(payload, 'parser_version', 'manual_cs_parser_v2')
        parser_status = getattr(payload, 'parser_status', 'unknown')
        review_reason_codes = getattr(payload, 'review_reason_codes', []) or []
        routing_decision = getattr(payload, 'routing_decision', None)
        recommended_next_action = getattr(payload, 'recommended_next_action', None)
        review_status = getattr(payload, 'review_status', 'not_needed')
        normalized_mobile, normalized_area_code, normalized_country = normalize_phone_identity(
            mobile=payload.mobile,
            area_code=payload.area_code,
            country=payload.country,
        )
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT lead_id, matched_customer_id FROM leads WHERE area_code = ? AND mobile = ?",
                (normalized_area_code, normalized_mobile),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE leads
                    SET trace_id = ?, source_platform = ?, source_campaign = ?, source_page_id = ?, country = ?,
                        yw_id = COALESCE(?, yw_id), app_name = COALESCE(?, app_name), dept_name = COALESCE(?, dept_name),
                        pendaftaran_group = COALESCE(?, pendaftaran_group), inviter_id = COALESCE(?, inviter_id),
                        parser_confidence = COALESCE(?, parser_confidence),
                        parser_missing_fields = ?, parser_conflicts = ?, parser_raw_text = COALESCE(?, parser_raw_text),
                        parser_raw_ocr_text = COALESCE(?, parser_raw_ocr_text), parser_version = ?, parser_status = ?,
                        review_reason_codes = ?, routing_decision = COALESCE(?, routing_decision),
                        recommended_next_action = COALESCE(?, recommended_next_action), review_status = ?, updated_at = ?
                    WHERE lead_id = ?
                    """,
                    (
                        payload.trace_id,
                        payload.source_platform,
                        payload.source_campaign,
                        payload.source_page_id,
                        normalized_country,
                        payload.yw_id,
                        payload.app_name,
                        payload.dept_name,
                        payload.pendaftaran_group,
                        payload.inviter_id,
                        parser_confidence,
                        json.dumps(parser_missing_fields, ensure_ascii=False),
                        json.dumps(parser_conflicts, ensure_ascii=False),
                        parser_raw_text,
                        parser_raw_ocr_text,
                        parser_version,
                        parser_status,
                        json.dumps(review_reason_codes, ensure_ascii=False),
                        routing_decision,
                        recommended_next_action,
                        review_status,
                        now,
                        row["lead_id"],
                    ),
                )
                return {
                    "lead_id": row["lead_id"],
                    "matched_customer_id": row["matched_customer_id"],
                    "is_new": False,
                    "current_status": "new",
                }

            lead_id = create_id("lead")
            customer_id = create_id("cust")
            conn.execute(
                """
                INSERT INTO leads (
                    lead_id, trace_id, source_platform, source_campaign, source_page_id, country, area_code, mobile,
                    yw_id, app_name, dept_name, pendaftaran_group, inviter_id,
                    parser_confidence, parser_missing_fields, parser_conflicts, parser_raw_text, parser_raw_ocr_text,
                    parser_version, parser_status, review_reason_codes, routing_decision, recommended_next_action, review_status,
                    current_status, matched_customer_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lead_id,
                    payload.trace_id,
                    payload.source_platform,
                    payload.source_campaign,
                    payload.source_page_id,
                    normalized_country,
                    normalized_area_code,
                    normalized_mobile,
                    payload.yw_id,
                    payload.app_name,
                    payload.dept_name,
                    payload.pendaftaran_group,
                    payload.inviter_id,
                    parser_confidence,
                    json.dumps(parser_missing_fields, ensure_ascii=False),
                    json.dumps(parser_conflicts, ensure_ascii=False),
                    parser_raw_text,
                    parser_raw_ocr_text,
                    parser_version,
                    parser_status,
                    json.dumps(review_reason_codes, ensure_ascii=False),
                    routing_decision,
                    recommended_next_action,
                    review_status,
                    "new",
                    customer_id,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO customer_projection (customer_id, lead_id, mobile, area_code, yw_id, pendaftaran_group, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (customer_id, lead_id, normalized_mobile, normalized_area_code, payload.yw_id, payload.pendaftaran_group, now),
            )
            self._record_status_history(
                conn,
                lead_id=lead_id,
                from_status=None,
                to_status="new",
                trigger_type="lead_created",
                trigger_source="leads_upsert",
            )
            return {
                "lead_id": lead_id,
                "matched_customer_id": customer_id,
                "is_new": True,
                "current_status": "new",
            }

    def collect_event(self, payload: EventCollectRequest) -> Dict[str, Any]:
        event_id = create_id("evt")
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO lead_events (
                    event_id, lead_id, trace_id, event_type, event_source, event_value, page_id, session_id,
                    operator_id, operator_name, raw_payload, happened_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    payload.lead_id,
                    payload.trace_id,
                    payload.event_type,
                    payload.event_source,
                    payload.event_value,
                    payload.page_id,
                    payload.session_id,
                    payload.operator_id,
                    payload.operator_name,
                    json.dumps(payload.raw_payload, ensure_ascii=False),
                    payload.happened_at or now,
                    now,
                ),
            )
            if payload.lead_id and payload.event_type in {"contact_clicked", "account_id_submitted", "wa_redirected"}:
                current = conn.execute("SELECT current_status FROM leads WHERE lead_id = ?", (payload.lead_id,)).fetchone()
                from_status = current["current_status"] if current else None
                conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("engaged", now, payload.lead_id))
                self._record_status_history(
                    conn,
                    lead_id=payload.lead_id,
                    from_status=from_status,
                    to_status="engaged",
                    trigger_type=payload.event_type,
                    trigger_source=payload.event_source,
                    trigger_event_id=event_id,
                    operator_id=payload.operator_id,
                    operator_name=payload.operator_name,
                )
        return {"event_id": event_id, "accepted": True}

    def create_task(self, payload: TaskCreateRequest) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT task_id, status FROM automation_tasks WHERE dedupe_key = ?", (payload.dedupe_key,)).fetchone()
            if row:
                return {"task_id": row["task_id"], "status": row["status"]}
            task_id = create_id("task")
            conn.execute(
                """
                INSERT INTO automation_tasks (
                    task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    payload.lead_id,
                    payload.task_type,
                    payload.priority,
                    json.dumps(payload.payload, ensure_ascii=False),
                    payload.dedupe_key,
                    payload.created_by,
                    payload.created_at,
                    "pending",
                ),
            )
            conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("processing", utc_now(), payload.lead_id))
            return {"task_id": task_id, "status": "pending"}

    def task_result(self, task_id: str, payload: TaskResultRequest) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT lead_id FROM automation_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="task not found")
            conn.execute(
                """
                UPDATE automation_tasks
                SET status = ?, result_code = ?, result_reason = ?, toast_text = ?, evidence_url = ?, retry_count = ?,
                    executor_type = ?, executor_id = ?, finished_at = ?, raw_result = ?
                WHERE task_id = ?
                """,
                (
                    payload.status,
                    payload.result_code,
                    payload.result_reason,
                    payload.toast_text,
                    payload.evidence_url,
                    payload.retry_count,
                    payload.executor_type,
                    payload.executor_id,
                    payload.finished_at,
                    json.dumps(payload.raw_result, ensure_ascii=False),
                    task_id,
                ),
            )
            lead_status = "success" if payload.status == "success" else "failed" if payload.status == "failed" else "manual_review"
            conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", (lead_status, utc_now(), row["lead_id"]))
            return {"task_id": task_id, "crm_sync_status": "pending", "next_action": "sync_customer"}

    def customer_sync(self, payload: CustomerSyncRequest) -> Dict[str, Any]:
        now = utc_now()
        with self.db.connect() as conn:
            lead = conn.execute("SELECT matched_customer_id FROM leads WHERE lead_id = ?", (payload.lead_id,)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail="lead not found")
            customer_id = lead["matched_customer_id"]
            row = conn.execute("SELECT customer_id FROM customer_projection WHERE customer_id = ?", (customer_id,)).fetchone()
            action = "update" if row else "insert"
            patch = payload.crm_patch
            if row:
                conn.execute(
                    """
                    UPDATE customer_projection
                    SET yw_id = COALESCE(?, yw_id), pendaftaran_group = COALESCE(?, pendaftaran_group),
                        payment_status = COALESCE(?, payment_status), user_quality = COALESCE(?, user_quality),
                        remark = COALESCE(?, remark), join_group = COALESCE(?, join_group),
                        file_url = COALESCE(?, file_url), pz_status = COALESCE(?, pz_status), updated_at = ?
                    WHERE customer_id = ?
                    """,
                    (
                        payload.yw_id,
                        patch.get("pendaftaran_group"),
                        patch.get("payment_status"),
                        patch.get("user_quality"),
                        patch.get("remark"),
                        patch.get("join_group"),
                        patch.get("file_url"),
                        patch.get("pz_status"),
                        now,
                        customer_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO customer_projection (
                        customer_id, lead_id, mobile, area_code, yw_id, pendaftaran_group, payment_status,
                        user_quality, remark, join_group, file_url, pz_status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        customer_id,
                        payload.lead_id,
                        payload.mobile,
                        payload.area_code,
                        payload.yw_id,
                        patch.get("pendaftaran_group"),
                        patch.get("payment_status"),
                        patch.get("user_quality"),
                        patch.get("remark"),
                        patch.get("join_group"),
                        patch.get("file_url"),
                        patch.get("pz_status"),
                        now,
                    ),
                )
            conn.execute(
                "INSERT INTO sync_logs (sync_log_id, lead_id, task_id, sync_type, target_system, status, request_snapshot, response_snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    create_id("sync"),
                    payload.lead_id,
                    payload.task_id,
                    payload.sync_mode,
                    "crm",
                    "success",
                    json.dumps(payload.crm_patch, ensure_ascii=False),
                    json.dumps({"customer_id": customer_id, "action": action}, ensure_ascii=False),
                    now,
                ),
            )
            conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("synced", now, payload.lead_id))
            self._queue_operator_notification(
                conn,
                lead_id=payload.lead_id,
                notification_type="crm_record_success",
                mobile=payload.mobile,
                yw_id=payload.yw_id,
                write_result="success",
            )
            return {"customer_id": customer_id, "action": action, "sync_status": "success"}

    def submit_account(self, payload: AccountSubmissionRequest) -> Dict[str, Any]:
        now = utc_now()
        submission_type = (payload.submission_type or "").strip()
        if submission_type not in {"account_id", "screenshot"}:
            raise HTTPException(status_code=400, detail="unsupported submission_type")
        with self.db.connect() as conn:
            lead = conn.execute("SELECT lead_id FROM leads WHERE lead_id = ?", (payload.lead_id,)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail="lead not found")

            submission_id = create_id("sub")
            recognition_status = "not_needed" if submission_type == "account_id" else "pending"
            normalized_account_id = None
            next_action = "queue_account_recognition"
            task_type = "account_recognition"
            task_payload = {
                "submission_id": submission_id,
                "lead_id": payload.lead_id,
                "submission_type": submission_type,
                "file_url": payload.file_url,
                "source_bot_app_id": payload.source_bot_app_id,
                "source_message_id": payload.source_message_id,
                "source_chat_id": payload.source_chat_id,
            }
            current_status = "recognition_pending"

            if submission_type == "account_id":
                if not str(payload.account_id or "").isdigit():
                    raise HTTPException(status_code=400, detail="account_id must be numeric")
                normalized_account_id = str(payload.account_id)
                next_action = "queue_bind_check"
                task_type = "bind_check"
                task_payload = {
                    "submission_id": submission_id,
                    "lead_id": payload.lead_id,
                    "account_id": normalized_account_id,
                    "source_bot_app_id": payload.source_bot_app_id,
                    "source_message_id": payload.source_message_id,
                    "source_chat_id": payload.source_chat_id,
                }
                current_status = "account_submitted"

            conn.execute(
                """
                INSERT INTO account_submissions (
                    submission_id, lead_id, task_id, submission_type, account_id, account_id_type,
                    file_url, file_type, source_channel, submitted_by, recognition_status,
                    recognized_account_id, recognition_raw, submitted_at, remark, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    payload.lead_id,
                    payload.task_id,
                    submission_type,
                    payload.account_id,
                    payload.account_id_type,
                    payload.file_url,
                    payload.file_type,
                    payload.source_channel,
                    payload.submitted_by,
                    recognition_status,
                    normalized_account_id,
                    json.dumps({}, ensure_ascii=False),
                    payload.submitted_at,
                    payload.remark,
                    now,
                    now,
                ),
            )

            dedupe_key = f"{task_type}:{payload.lead_id}:{submission_id}"
            task_id = create_id("task")
            conn.execute(
                """
                INSERT INTO automation_tasks (
                    task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    payload.lead_id,
                    task_type,
                    "P0",
                    json.dumps(task_payload, ensure_ascii=False),
                    dedupe_key,
                    payload.submitted_by or "system",
                    payload.submitted_at,
                    "pending",
                ),
            )
            conn.execute(
                "UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?",
                (current_status, now, payload.lead_id),
            )
            self._record_status_history(
                conn,
                lead_id=payload.lead_id,
                from_status="engaged",
                to_status=current_status,
                trigger_type="account_submission",
                trigger_source=payload.source_channel or "account_submissions",
                trigger_task_id=task_id,
                operator_name=payload.submitted_by,
            )
            return {
                "accepted": True,
                "submission_id": submission_id,
                "normalized_account_id": normalized_account_id,
                "next_action": next_action,
                "task_type": task_type,
                "task_id": task_id,
                "recognition_status": recognition_status,
            }

    def submit_manual_cs(self, payload: ManualCsSubmissionRequest) -> Dict[str, Any]:
        source_key = str(payload.submitted_by or payload.source_channel or 'manual_cs').strip() or 'manual_cs'
        if self.ingress_async_default:
            if not self.ingress_rate_limiter.allow(f'manual:{source_key}'):
                raise HTTPException(status_code=429, detail='manual intake rate limited')
            queued = self._enqueue_ingress_event(
                ingress_type='manual_cs_submission',
                source_key=source_key,
                payload=payload.dict(),
            )
            return {
                'accepted': True,
                'queued': True,
                'ingress_event_id': queued['event_id'],
                'duplicate': queued['duplicate'],
                'next_action': 'queued_for_processing',
            }
        return self._submit_manual_cs_sync(payload)

    def _submit_manual_cs_sync(self, payload: ManualCsSubmissionRequest) -> Dict[str, Any]:
        if not str(payload.mobile or '').strip() or not str(payload.registration_group or '').strip() or not str(payload.app_name or '').strip() or not str(payload.dept_name or '').strip() or not str(payload.submitted_by or '').strip() or not str(payload.submitted_at or '').strip():
            raise HTTPException(status_code=400, detail="mobile, registration_group, app_name, dept_name, submitted_by, submitted_at are required")
        if payload.submission_type == "account_id" and not str(payload.account_id or "").strip():
            raise HTTPException(status_code=400, detail="account_id is required when submission_type=account_id")
        if payload.submission_type == "screenshot" and not str(payload.file_url or "").strip():
            raise HTTPException(status_code=400, detail="file_url is required when submission_type=screenshot")
        if payload.submission_type == "screenshot" and not str(payload.file_type or "").strip():
            raise HTTPException(status_code=400, detail="file_type is required when submission_type=screenshot")

        parser_text = (
            f"手机号 {payload.mobile} 应用 {payload.app_name} 公会 {payload.dept_name} 注册群组 {payload.registration_group} "
            f"ID {payload.account_id or ''} 个人邀请码 {payload.invite_code or ''}"
        )
        if payload.remark:
            parser_text = f"{payload.remark}\n{parser_text}"
        parsed_payload = parse_manual_cs_message(text=parser_text, image_ocr_text=payload.image_ocr_text)

        normalized_mobile, normalized_area_code, normalized_country = normalize_phone_identity(
            mobile=payload.mobile,
            area_code=0,
            country="",
        )
        explicit_fields = extract_explicit_intake_fields(payload.remark or '')
        explicit_app_name = str(explicit_fields.get('app_name') or '').strip()
        explicit_dept_name = str(explicit_fields.get('dept_name') or '').strip()
        explicit_invite_code = str(explicit_fields.get('invite_code') or '').strip().upper()
        if payload.app_name_explicit and not explicit_app_name:
            explicit_app_name = str(payload.app_name or '').strip()
        if payload.dept_name_explicit and not explicit_dept_name:
            explicit_dept_name = str(payload.dept_name or '').strip()
        if not explicit_invite_code:
            explicit_invite_code = str(payload.invite_code or '').strip().upper()

        current_default_app = str(self.lark_default_app_name or '').strip()
        current_default_dept = str(self.lark_default_dept_name or '').strip()
        prefer_payload_over_defaults = str(payload.source_channel or '').strip() == 'manual_cs_lark'
        final_mobile = parsed_payload.get('mobile') or normalized_mobile
        final_area_code = parsed_payload.get('area_code') or normalized_area_code
        final_country = parsed_payload.get('country') or normalized_country
        final_registration_group = payload.registration_group or parsed_payload.get('registration_group')
        if prefer_payload_over_defaults:
            final_app_name = (
                explicit_app_name
                or payload.app_name
                or parsed_payload.get('app_name')
                or current_default_app
            )
            final_dept_name = (
                explicit_dept_name
                or payload.dept_name
                or parsed_payload.get('dept_name')
                or current_default_dept
            )
        else:
            final_app_name = (
                explicit_app_name
                or current_default_app
                or payload.app_name
                or parsed_payload.get('app_name')
            )
            final_dept_name = (
                explicit_dept_name
                or current_default_dept
                or payload.dept_name
                or parsed_payload.get('dept_name')
            )
        final_account_id = payload.account_id or parsed_payload.get('account_id')
        invite_code_meta = normalize_invite_code_candidate(explicit_invite_code or parsed_payload.get('evidence', {}).get('invite_code_raw_input') or str(payload.invite_code or '').strip().upper() or None)
        final_invite_code = str(invite_code_meta.get('normalized') or '').strip().upper() if invite_code_meta.get('is_valid') else None

        invite_validation_error = validate_invite_code_field(explicit_invite_code or parsed_payload.get('evidence', {}).get('invite_code_raw_input') or str(payload.invite_code or '').strip().upper() or None, invite_code_meta=invite_code_meta)
        if invite_validation_error:
            return {
                'accepted': False,
                'reason': invite_validation_error['reason'],
                'reply_phone': final_mobile or '-',
                'reply_id': final_account_id or '-',
                'reply_group': final_registration_group or '-',
                'reply_code': invite_code_meta.get('raw_input') or '-',
                'reply_error_text': invite_validation_error['reply_text'],
            }

        if (
            (explicit_app_name and current_default_app and explicit_app_name.lower() != current_default_app.lower())
            or (explicit_dept_name and current_default_dept and explicit_dept_name.lower() != current_default_dept.lower())
        ):
            return {
                'accepted': False,
                'reason': 'app_agency_mismatch',
                'reply_phone': final_mobile or '-',
                'reply_id': final_account_id or '-',
                'reply_group': final_registration_group or '-',
            }

        classification = self._classify_manual_cs_submission(
            payload=payload,
            parsed_payload=parsed_payload,
            final_account_id=final_account_id,
            final_mobile=final_mobile,
            final_registration_group=final_registration_group,
            final_app_name=final_app_name,
            final_dept_name=final_dept_name,
            final_invite_code=final_invite_code,
        )

        parsed_result = {
            **parsed_payload,
            'mobile': final_mobile,
            'area_code': final_area_code,
            'country': final_country,
            'registration_group': final_registration_group,
            'app_name': final_app_name,
            'dept_name': final_dept_name,
            'account_id': final_account_id,
            'invite_code': final_invite_code,
        }
        with self.db.connect() as conn:
            duplicate_submission = self._find_recent_cross_channel_duplicate_submission(
                conn,
                mobile=final_mobile,
                area_code=final_area_code or 62,
                account_id=final_account_id,
                app_name=final_app_name,
                dept_name=final_dept_name,
                registration_group=final_registration_group,
                source_channel=payload.source_channel,
                submitted_at=payload.submitted_at,
            )
            if duplicate_submission:
                return self._build_duplicate_submission_response(
                    conn,
                    duplicate_submission=duplicate_submission,
                    parsed_result=parsed_result,
                )
            existing_verified_lead = self._find_recent_verified_duplicate_lead(
                conn,
                mobile=final_mobile,
                area_code=final_area_code or 62,
                account_id=final_account_id,
                app_name=final_app_name,
                dept_name=final_dept_name,
                registration_group=final_registration_group,
            )
            if existing_verified_lead:
                return self._build_duplicate_submission_response(
                    conn,
                    duplicate_submission={
                        'submission_id': None,
                        'source_channel': 'local_verified_duplicate',
                        'lead_id': existing_verified_lead['lead_id'],
                    },
                    parsed_result=parsed_result,
                    accepted_override=False,
                    result_reason_override='Data duplication.',
                )

        lead = self.upsert_lead(
            LeadUpsertRequest(
                trace_id=create_id("trace"),
                source_platform="manual_cs",
                source_campaign=payload.source_channel,
                source_page_id=payload.source_channel,
                country=final_country or "Indonesia",
                area_code=final_area_code or 62,
                mobile=final_mobile,
                yw_id=final_account_id,
                app_name=final_app_name,
                dept_name=final_dept_name,
                pendaftaran_group=final_registration_group,
                inviter_id=final_invite_code,
                occurred_at=payload.submitted_at,
                parser_confidence=parsed_payload.get('confidence'),
                parser_missing_fields=parsed_payload.get('missing_fields', []),
                parser_conflicts=parsed_payload.get('conflicts', []),
                parser_raw_text=parsed_payload.get('raw_text'),
                parser_raw_ocr_text=parsed_payload.get('raw_ocr_text'),
                parser_version=classification['parser_version'],
                parser_status=classification['parser_status'],
                review_reason_codes=classification['review_reason_codes'],
                routing_decision=classification['routing_decision'],
                recommended_next_action=classification['recommended_next_action'],
                review_status=classification['review_status'],
            )
        )

        if classification['routing_decision'] == 'manual_review':
            with self.db.connect() as conn:
                review_task_id = create_id('task')
                conn.execute(
                    """
                    INSERT INTO automation_tasks (
                        task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review_task_id,
                        lead['lead_id'],
                        'manual_review',
                        'P0',
                        json.dumps({
                            'submission_type': payload.submission_type,
                            'file_url': payload.file_url,
                            'file_type': payload.file_type,
                            'source_channel': payload.source_channel,
                            'submitted_by': payload.submitted_by,
                            'remark': payload.remark,
                            'parsed_payload': parsed_result,
                        }, ensure_ascii=False),
                        f"manual_review:{lead['lead_id']}:{payload.submitted_at}",
                        payload.submitted_by,
                        payload.submitted_at,
                        'pending',
                    ),
                )
                current = conn.execute("SELECT current_status FROM leads WHERE lead_id = ?", (lead['lead_id'],)).fetchone()
                from_status = current['current_status'] if current else 'new'
                conn.execute(
                    "UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?",
                    ('manual_review_pending', utc_now(), lead['lead_id']),
                )
                self._record_status_history(
                    conn,
                    lead_id=lead['lead_id'],
                    from_status=from_status,
                    to_status='manual_review_pending',
                    trigger_type='manual_cs_routed',
                    trigger_source='manual_cs_submission',
                    trigger_task_id=review_task_id,
                    operator_name=payload.submitted_by,
                    remark=';'.join(classification['review_reason_codes']),
                )
            return {
                "accepted": True,
                "lead_id": lead["lead_id"],
                "matched_customer_id": lead["matched_customer_id"],
                "submission_id": None,
                "task_id": review_task_id,
                "next_action": "manual_review",
                "routing_decision": classification['routing_decision'],
                "review_reason_codes": classification['review_reason_codes'],
                "parsed_payload": parsed_result,
            }

        account_submission = self.submit_account(
            AccountSubmissionRequest(
                lead_id=lead["lead_id"],
                submission_type=payload.submission_type,
                account_id=final_account_id,
                account_id_type="platform_uid" if final_account_id else None,
                file_url=payload.file_url,
                file_type=payload.file_type,
                source_channel=payload.source_channel,
                source_bot_app_id=payload.source_bot_app_id,
                source_message_id=payload.source_message_id,
                submitted_by=payload.submitted_by,
                submitted_at=payload.submitted_at,
                remark=payload.remark,
            )
        )
        simulated_result = self._maybe_auto_simulate_bind_after_intake(
            lead=lead,
            payload=payload,
            parsed_result=parsed_result,
            account_submission=account_submission,
        )
        if simulated_result is not None:
            return simulated_result
        return {
            "accepted": True,
            "lead_id": lead["lead_id"],
            "matched_customer_id": lead["matched_customer_id"],
            "submission_id": account_submission["submission_id"],
            "task_id": account_submission["task_id"],
            "next_action": account_submission["next_action"],
            "routing_decision": classification['routing_decision'],
            "review_reason_codes": classification['review_reason_codes'],
            "parsed_payload": parsed_result,
        }

    def _reply_lark_message(self, *, message_id: Optional[str], text: str, chat_id: Optional[str] = None, adapter: Any = None) -> None:
        active_adapter = adapter or self.lark_reply_adapter
        if active_adapter is None or not str(text or '').strip():
            return
        try:
            if message_id and hasattr(active_adapter, 'reply_text'):
                self.external_call_rate_limiter.allow('reply:lark')
                self.reply_circuit_breaker.call(lambda: active_adapter.reply_text(message_id=message_id, text=text))
                return
            if chat_id and hasattr(active_adapter, 'send_text'):
                self.external_call_rate_limiter.allow('reply:lark')
                self.reply_circuit_breaker.call(lambda: active_adapter.send_text(chat_id=chat_id, text=text))
                return
        except Exception as exc:
            if chat_id and hasattr(active_adapter, 'send_text'):
                try:
                    self.reply_circuit_breaker.call(lambda: active_adapter.send_text(chat_id=chat_id, text=text))
                    return
                except Exception as fallback_exc:
                    print(f"Lark reply failed for {message_id or 'unknown'} and fallback chat send failed for {chat_id}: {fallback_exc}")
                    return
            print(f"Lark reply failed for {message_id or chat_id or 'unknown'}: {exc}")

    def _load_profile_env_map(self, profile_name: str) -> Dict[str, str]:
        normalized_profile = str(profile_name or '').strip()
        if not normalized_profile:
            return {}
        env_path = Path.home() / '.hermes' / 'profiles' / normalized_profile / '.env'
        if not env_path.exists():
            return {}
        values: Dict[str, str] = {}
        try:
            for raw_line in env_path.read_text(encoding='utf-8', errors='ignore').splitlines():
                line = raw_line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                values[str(key).strip()] = value.strip().strip('"').strip("'")
        except Exception:
            return {}
        return values

    def _official_group_success_notification_already_sent(self, conn: sqlite3.Connection, approval_run_id: str) -> bool:
        normalized_run_id = str(approval_run_id or '').strip()
        if not normalized_run_id:
            return False
        row = conn.execute(
            "SELECT 1 FROM operator_audit_log WHERE event_type = 'official_group_success_notification_sent' AND payload LIKE ? LIMIT 1",
            (f'%\"approval_run_id\": \"{normalized_run_id}\"%',),
        ).fetchone()
        return row is not None

    def _record_official_group_success_notification_sent(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: Optional[str],
        approval_run_id: str,
        approval_run_ids: List[str],
        notify_profile_name: Optional[str],
        notify_robot_name: Optional[str],
        message_text: str,
        dedupe_key: Optional[str],
        target_group: Optional[str],
        group_name: Optional[str],
    ) -> None:
        self._record_audit_event(
            conn,
            event_type='official_group_success_notification_sent',
            event_source='official_group_batch_runner',
            payload={
                'lead_id': lead_id,
                'approval_run_id': approval_run_id,
                'approval_run_ids': [str(item).strip() for item in list(approval_run_ids or []) if str(item).strip()],
                'notify_profile_name': str(notify_profile_name or '').strip() or None,
                'notify_robot_name': str(notify_robot_name or '').strip() or None,
                'message_text': message_text,
                'dedupe_key': str(dedupe_key or '').strip() or None,
                'target_group': str(target_group or '').strip() or None,
                'group_name': str(group_name or '').strip() or None,
            },
            lead_id=str(lead_id or '').strip() or None,
        )

    def _send_official_group_success_notifications(
        self,
        *,
        decided_at: str,
        ready_groups: List[Dict[str, Any]],
        results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        success_rows: List[Dict[str, Any]] = []
        approval_run_lead_map: Dict[str, Optional[str]] = {}
        with self.db.connect() as conn:
            for item in list(results or []):
                if not isinstance(item, dict) or not item.get('executed'):
                    continue
                executor_result = item.get('executor_result') if isinstance(item.get('executor_result'), dict) else {}
                if str(executor_result.get('status') or '').strip().lower() != 'success':
                    continue
                if executor_result.get('verified') is False:
                    continue
                raw_result = executor_result.get('raw_result') if isinstance(executor_result.get('raw_result'), dict) else {}
                approval_run_id = str(raw_result.get('approval_run_id') or '').strip()
                if approval_run_id and self._official_group_success_notification_already_sent(conn, approval_run_id):
                    continue
                success_rows.append(item)
                if approval_run_id:
                    approval_run_lead_map[approval_run_id] = str(item.get('lead_id') or '').strip() or None
        if not success_rows:
            return []
        checked_at = str(decided_at or '').strip() or datetime.now(timezone.utc).isoformat()
        cycle = {
            'checked_at': checked_at,
            'registration_group': str((ready_groups[0] or {}).get('registration_group') or '').strip() if ready_groups else '',
            'official_group_dispatch': {
                'triggered': True,
                'ok': True,
                'ready_groups': ready_groups,
                'result': {'results': success_rows},
            },
        }
        incidents = [
            item
            for item in build_success_notifications(cycle)
            if isinstance(item, dict) and str(item.get('code') or '').strip() == 'official_group_approval_succeeded'
        ]
        if not incidents:
            return []
        notifications: List[Dict[str, Any]] = []
        with self.db.connect() as conn:
            for incident in incidents:
                details = incident.get('details') if isinstance(incident.get('details'), dict) else {}
                approval_run_ids = [
                    str(item).strip()
                    for item in list(details.get('approval_run_ids') or [])
                    if str(item).strip()
                ]
                if approval_run_ids and all(self._official_group_success_notification_already_sent(conn, item) for item in approval_run_ids):
                    notifications.append({
                        'code': incident.get('code'),
                        'status': 'skipped_duplicate',
                        'dedupe_key': incident.get('dedupe_key'),
                        'approval_run_ids': approval_run_ids,
                    })
                    continue
                notify_profile_name = str(incident.get('notify_profile_name') or '').strip()
                notify_robot_name = str(incident.get('notify_robot_name') or '').strip()
                payload = {
                    'code': incident.get('code'),
                    'dedupe_key': incident.get('dedupe_key'),
                    'approval_run_ids': approval_run_ids,
                    'notify_profile_name': notify_profile_name or None,
                    'notify_robot_name': notify_robot_name or None,
                }
                if not notify_profile_name:
                    payload['status'] = 'skipped_notify_profile_missing'
                    notifications.append(payload)
                    continue
                env_values = self._load_profile_env_map(notify_profile_name)
                app_id = str(env_values.get('LARK_APP_ID') or env_values.get('FEISHU_APP_ID') or '').strip()
                app_secret = str(env_values.get('LARK_APP_SECRET') or env_values.get('FEISHU_APP_SECRET') or '').strip()
                chat_id = str(env_values.get('LARK_HOME_CHANNEL') or env_values.get('FEISHU_HOME_CHANNEL') or '').strip()
                domain = str(env_values.get('LARK_DOMAIN') or env_values.get('FEISHU_DOMAIN') or 'lark').strip() or 'lark'
                if not app_id or not app_secret or not chat_id:
                    payload['status'] = 'skipped_no_notifier'
                    notifications.append(payload)
                    continue
                adapter = LiveLarkReplyAdapter(app_id=app_id, app_secret=app_secret, domain=domain)
                monitor_target = {
                    'notify_profile_name': notify_profile_name,
                    'notify_robot_name': notify_robot_name or None,
                    'group_name': details.get('group_name'),
                }
                effective_cycle = {**cycle, 'monitor_target': monitor_target}
                message_text = format_lark_alert('production-ops-daemon', incident, effective_cycle)
                try:
                    self.external_call_rate_limiter.allow(f'official-group-success-notify:{notify_profile_name}')
                    response = adapter.send_text(chat_id=chat_id, text=message_text)
                    payload['status'] = 'sent'
                    payload['response'] = response
                    for approval_run_id in approval_run_ids:
                        self._record_official_group_success_notification_sent(
                            conn,
                            lead_id=approval_run_lead_map.get(approval_run_id),
                            approval_run_id=approval_run_id,
                            approval_run_ids=approval_run_ids,
                            notify_profile_name=notify_profile_name,
                            notify_robot_name=notify_robot_name,
                            message_text=message_text,
                            dedupe_key=str(incident.get('dedupe_key') or '').strip() or None,
                            target_group=str(details.get('target_group') or '').strip() or None,
                            group_name=str(details.get('group_name') or '').strip() or None,
                        )
                    conn.commit()
                except Exception as exc:
                    payload['status'] = 'failed'
                    payload['error'] = str(exc)
                notifications.append(payload)
        return notifications

    def _resolve_lark_reply_adapter(self, *, app_id: Optional[str] = None) -> Any:
        normalized_app_id = str(app_id or '').strip()
        if normalized_app_id and normalized_app_id in self._lark_reply_adapter_by_app_id:
            return self._lark_reply_adapter_by_app_id[normalized_app_id]
        if normalized_app_id and self.current_lark_app_id and normalized_app_id == str(self.current_lark_app_id).strip() and self.lark_reply_adapter is not None:
            return self.lark_reply_adapter
        if not normalized_app_id:
            return self.lark_reply_adapter
        if normalized_app_id in self._profile_reply_adapter_cache:
            return self._profile_reply_adapter_cache[normalized_app_id]
        preset = self.resolve_intake_bot_preset(app_id=normalized_app_id)
        profile_name = str(preset.get('profile_name') or '').strip()
        env_values = self._load_profile_env_map(profile_name)
        env_app_id = str(env_values.get('LARK_APP_ID') or env_values.get('FEISHU_APP_ID') or '').strip()
        env_app_secret = str(env_values.get('LARK_APP_SECRET') or env_values.get('FEISHU_APP_SECRET') or '').strip()
        env_domain = str(env_values.get('LARK_DOMAIN') or env_values.get('FEISHU_DOMAIN') or 'lark').strip() or 'lark'
        if env_app_id and env_app_secret and env_app_id == normalized_app_id:
            adapter = LiveLarkReplyAdapter(app_id=env_app_id, app_secret=env_app_secret, domain=env_domain)
            self._profile_reply_adapter_cache[normalized_app_id] = adapter
            return adapter
        return self.lark_reply_adapter

    def _should_emit_lark_reply(self, result: Dict[str, Any]) -> bool:
        if not isinstance(result, dict):
            return False
        if str(result.get('next_action') or '').strip() in {'queue_bind_check', 'queue_bind_retry', 'queue_account_recognition', 'queued_for_processing', 'queue_crm_sync_retry'}:
            return False
        return True

    def _is_verified_success_result(self, result: Dict[str, Any]) -> bool:
        if not isinstance(result, dict) or not result.get('accepted'):
            return False
        lead_status = str(result.get('lead_status') or '').strip()
        if lead_status not in {'bind_success', 'group_join_pending', 'group_join_success', 'synced'}:
            return False
        return bool(
            result.get('crm_verified')
            or result.get('verified_after_write')
            or result.get('current_submission_crm_verified')
        )

    def _format_operator_crm_failure_reason(self, *, retried: bool = False) -> str:
        return 'CRM failed after retries. Check manually.' if retried else 'CRM failed. Check manually.'

    def _format_lark_reply_text(self, result: Dict[str, Any]) -> str:
        parsed_payload = result.get('parsed_payload') or {}
        reply_area_code = result.get('reply_area_code')
        if reply_area_code is None and isinstance(parsed_payload, dict):
            reply_area_code = parsed_payload.get('area_code')
        phone = format_display_phone(result.get('reply_phone'), area_code=reply_area_code)
        account_id = str(result.get('reply_id') or '-').strip() or '-'
        group = str(result.get('reply_group') or '-').strip() or '-'
        code = str(
            result.get('reply_code')
            or result.get('invite_code')
            or (parsed_payload.get('invite_code') if isinstance(parsed_payload, dict) else '')
            or '-'
        ).strip() or '-'
        if result.get('accepted'):
            if self._is_verified_success_result(result):
                return (
                    '**✅ Success**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            return (
                '**❌ Failed**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') in {'app_guild_mismatch', 'app_agency_mismatch', 'bind_backend_guild_mismatch'}:
            return (
                '**🚫 I do not handle this app/agency.**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') == 'irrelevant_message':
            return (
                '**🚫I only register host information**\n'
                '**📮Send:**\n'
                'Phone:\n'
                'ID:\n'
                'Group:\n'
                'Code:\n'
                '**📌Example:**\n'
                'Phone: +62 13800000000  ID: 123456  Group: Group-1  Code: EKVFGQ'
            )
        if result.get('reason') == 'missing_required_fields':
            missing_fields = result.get('reply_missing_fields') or []
            missing_text = ', '.join(missing_fields) if missing_fields else 'required fields'
            return (
                f'**🚫 Missing: {missing_text}**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') == 'invalid_phone_format':
            return (
                '**🚫 Invalid phone format. Use +<country code> <number>.**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') == 'invalid_group_format':
            return (
                '**🚫 Invalid group format. Use English-Number, e.g. Piso-12.**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') == 'invalid_account_id_format':
            return (
                f"**🚫 {str(result.get('reply_error_text') or 'Invalid ID.')}**\n"
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') == 'invalid_invite_code_format':
            return (
                f"**🚫 {str(result.get('reply_error_text') or 'Invalid Code. Use 6 English letters or letters+digits only.')}**\n"
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') == 'crm_sync_failed':
            result_code = str(result.get('result_code') or '').strip()
            headline = '**❌ CRM sync failed: Bind succeeded but CRM retried.**' if result_code in {'crm_retry_exhausted', 'crm_retry_failed'} else '**❌ CRM Failed**'
            return (
                f'{headline}\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') in {'app_guild_mismatch', 'app_agency_mismatch', 'bind_backend_guild_mismatch'}:
            return (
                '**🚫 I do not handle this app/agency.**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        if result.get('reason') in {'simulated_bind_failed', 'bind_check_failed'}:
            reason_text = str(result.get('result_reason') or 'bind failed').strip() or 'bind failed'
            lowered_reason = reason_text.lower()
            failure_category = str(result.get('bind_failure_category') or '').strip()
            if '401' in reason_text:
                return (
                    '**❌ Failed：Error Code Unable to Bind**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if (
                result.get('result_code') == 'bind_executor_profile_not_configured'
                or 'no chrome profile mapping configured' in lowered_reason
            ):
                return (
                    '**🚫 I do not handle this app/agency.**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if 'batas maksimum guild' in lowered_reason or 'maximum guild' in lowered_reason:
                return (
                    '**❌ Device Duplicate Registration**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if 'the streamer was in other guild' in lowered_reason:
                return (
                    '**❌ Bind failed: The streamer was in another agency**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if failure_category == 'invalid_personal_code':
                return (
                    '**❌ Bind failed: Invalid personal code**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            if failure_category in {'auth_required', 'session_expired', 'captcha_required', 'manual_continue_required'}:
                return (
                    '**❌ Bind failed: Backend session requires manual recovery**\n'
                    f'Phone: {phone}\n'
                    f'ID: {account_id}\n'
                    f'Group: {group}\n'
                    f'Code: {code}'
                )
            return (
                f'**❌ Bind failed: {reason_text}**\n'
                f'Phone: {phone}\n'
                f'ID: {account_id}\n'
                f'Group: {group}\n'
                f'Code: {code}'
            )
        return (
            '**❌ Failed**\n'
            f'Phone: {phone}\n'
            f'ID: {account_id}\n'
            f'Group: {group}\n'
            f'Code: {code}'
        )

    def handle_lark_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if payload.get('type') == 'url_verification':
            return {'challenge': payload.get('challenge', '')}
        header = payload.get('header') or {}
        event_type = header.get('event_type') or payload.get('event_type')
        event = payload.get('event') or {}
        message = event.get('message') or {}
        message_id = str(message.get('message_id') or '')
        sender = event.get('sender') or {}
        sender_id = (sender.get('sender_id') or {}).get('open_id') or 'lark_unknown'
        gateway_direct = bool(payload.get('_gateway_direct'))
        if self.ingress_async_default and not gateway_direct and event_type == 'im.message.receive_v1' and message_id:
            if not self.ingress_rate_limiter.allow(f'lark:{sender_id}'):
                raise HTTPException(status_code=429, detail='lark ingress rate limited')
            queued = self._enqueue_ingress_event(
                ingress_type='lark_event',
                source_key=f'lark:{sender_id}',
                payload=payload,
            )
            return {
                'accepted': True,
                'queued': True,
                'ingress_event_id': queued['event_id'],
                'duplicate': queued['duplicate'],
                'message_id': message_id,
                'next_action': 'queued_for_processing',
            }
        return self._handle_lark_event_sync(payload)

    def _handle_lark_event_sync(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if payload.get('type') == 'url_verification':
            return {'challenge': payload.get('challenge', '')}

        gateway_direct = bool(payload.get('_gateway_direct'))

        def _finalize(message_id: Optional[str], result: Dict[str, Any]) -> Dict[str, Any]:
            if self._should_emit_lark_reply(result):
                reply_text = self._format_lark_reply_text(result)
                result['reply_text'] = reply_text
                if not gateway_direct:
                    self._reply_lark_message(message_id=message_id, text=reply_text)
            else:
                result['reply_text'] = ''
            return result

        header = payload.get('header') or {}
        event_type = header.get('event_type') or payload.get('event_type')
        if event_type != 'im.message.receive_v1':
            return {'accepted': False, 'ignored': True, 'reason': 'unsupported_event_type', 'event_type': event_type}

        bot_app_id = str(payload.get('_bot_app_id') or header.get('app_id') or '').strip()
        active_preset = self.resolve_intake_bot_preset(app_id=bot_app_id or None)
        active_default_app = str(active_preset.get('default_app') or self.lark_default_app_name or '').strip() or None
        active_default_dept = str(active_preset.get('default_guild') or self.lark_default_dept_name or '').strip() or None

        event = payload.get('event') or {}
        message = event.get('message') or {}
        message_id = message.get('message_id')
        chat_type = message.get('chat_type') or 'p2p'
        mentions = message.get('mentions') or []
        if chat_type == 'group' and not mentions:
            result = {
                'accepted': False,
                'ignored': True,
                'reason': 'group_message_without_mention',
                'reply_phone': '-',
                'reply_id': '-',
                'reply_group': '-',
            }
            return _finalize(message_id, result)

        content = message.get('content') or '{}'
        try:
            content_obj = json.loads(content)
        except Exception:
            content_obj = {'text': str(content)}
        sender = event.get('sender') or {}
        sender_id = (sender.get('sender_id') or {}).get('open_id') or 'lark_unknown'
        message_type = (message.get('message_type') or 'text')

        if message_type == 'image':
            if self.lark_media_adapter is None:
                raise HTTPException(status_code=503, detail='lark media adapter not configured')
            message_id = message.get('message_id') or create_id('msg')
            file_key = content_obj.get('image_key') or content_obj.get('file_key')
            if not file_key:
                result = {
                    'accepted': False,
                    'ignored': True,
                    'reason': 'missing_image_key',
                    'reply_phone': '-',
                    'reply_id': '-',
                    'reply_group': '-',
                }
                self._reply_lark_message(message_id=message_id, text=self._format_lark_reply_text(result))
                return result
            suffix = '.bin'
            cache_path = self.media_cache_dir / f"{message_id}_{file_key}{suffix}"
            downloaded = False
            if not cache_path.exists():
                image_bytes = self.lark_media_adapter.download_image(message_id, file_key)
                cache_path.write_bytes(image_bytes)
                downloaded = True
            result = {
                'accepted': True,
                'source': 'lark_event_bridge',
                'chat_type': chat_type,
                'message_type': 'image',
                'message_id': message_id,
                'file_key': file_key,
                'cached': True,
                'downloaded': downloaded,
                'cached_file_url': str(cache_path),
                'next_action': 'await_text_context',
                'reply_phone': '-',
                'reply_id': '-',
                'reply_group': '-',
            }
            return _finalize(message_id, result)

        if message_type != 'text':
            result = {
                'accepted': False,
                'ignored': True,
                'reason': 'unsupported_message_type',
                'reply_phone': '-',
                'reply_id': '-',
                'reply_group': '-',
            }
            return _finalize(message_id, result)

        text = str(content_obj.get('text') or '').strip()
        media_urls = content_obj.get('media_urls') or []
        image_ocr_text = None
        if media_urls and self.ocr_adapter is not None:
            first_media = str(media_urls[0] or '').strip()
            if first_media:
                try:
                    extracted = self.ocr_adapter.extract_text(first_media)
                    image_ocr_text = str((extracted or {}).get('raw_text') or '').strip() or None
                except Exception:
                    image_ocr_text = None
        if not text:
            result = {
                'accepted': False,
                'ignored': True,
                'reason': 'empty_text',
                'reply_phone': '-',
                'reply_id': '-',
                'reply_group': '-',
            }
            return _finalize(message_id, result)
        cleaned_text = re.sub(r'@[^\s]+\s*', '', text).strip()
        cleaned_text = (
            cleaned_text
            .replace('\\\\+', '+')
            .replace('\\\\-', '-')
            .replace('\\\\[', '[')
            .replace('\\\\]', ']')
            .replace('\\+', '+')
            .replace('\\-', '-')
            .replace('\\[', '[')
            .replace('\\]', ']')
        )
        parsed_text = parse_manual_cs_message(text=cleaned_text, image_ocr_text=image_ocr_text)
        bare_candidates = extract_bare_multiline_candidates(cleaned_text)
        explicit_fields = extract_explicit_intake_fields(cleaned_text)

        mobile_match = PHONE_CANDIDATE_PATTERN.search(cleaned_text)
        registration_group_match = re.search(r'(?:注册群组|group)\s*[:：]?\s*([A-Za-z]+(?:-\d+)?)', cleaned_text, flags=re.IGNORECASE)
        app_match = re.search(r'\b(Linky|FUMI)\b', cleaned_text, flags=re.IGNORECASE)
        dept_match = re.search(r'(?:公会|guild|dept)\s*[:：]?\s*([A-Za-z]+)', cleaned_text, flags=re.IGNORECASE)
        account_match = re.search(r'(?:^|\b)(?:id|uid|ywid|用户id|用户ID)\s*[:：是]?\s*(\d{6,})', cleaned_text, flags=re.IGNORECASE)
        invite_match = INVITE_CODE_CAPTURE_PATTERN.search(cleaned_text)
        invite_match_value = str(invite_match.group(1) or '').strip() if invite_match else None

        resolved_phone = (
            str(explicit_fields.get('mobile') or '').strip()
            or str(bare_candidates.get('mobile_line') or '').strip()
            or (mobile_match.group(1) if mobile_match else '-')
        )
        if resolved_phone != '-' and ('*' in resolved_phone or re.search(r'[^\d\s+\-().]', resolved_phone)):
            normalized_reply_phone = resolved_phone
        else:
            normalized_reply_phone = format_display_phone(resolved_phone if resolved_phone != '-' else None, area_code=(parsed_text.get('area_code') if isinstance(parsed_text, dict) else None))
        resolved_group = registration_group_match.group(1) if registration_group_match else (bare_candidates.get('registration_group_line') or parsed_text.get('registration_group') or None)
        resolved_account_id = account_match.group(1) if account_match else (bare_candidates.get('account_id_line') or parsed_text.get('account_id') or None)
        invite_code_meta = normalize_invite_code_candidate(
            invite_match_value
            or explicit_fields.get('invite_code')
            or parsed_text.get('evidence', {}).get('invite_code_raw_input')
            or parsed_text.get('invite_code')
            or None
        )
        resolved_invite_code = str(invite_code_meta.get('normalized') or '').strip().upper() if invite_code_meta.get('is_valid') else None
        resolved_app_name = app_match.group(1) if app_match else (parsed_text.get('app_name') or active_default_app or None)
        resolved_dept_name = (
            str(explicit_fields.get('dept_name') or '').strip()
            or (dept_match.group(1) if dept_match else '')
            or parsed_text.get('dept_name')
            or active_default_dept
            or None
        )
        invalid_group_candidate = extract_invalid_group_candidate(cleaned_text)

        explicit_app_name = str(explicit_fields.get('app_name') or (app_match.group(1) if app_match else '')).strip() or None
        explicit_dept_name = str(explicit_fields.get('dept_name') or (dept_match.group(1) if dept_match else '')).strip() or None
        inferred_group_guild = self._infer_executor_guild_from_registration_group(resolved_group)
        if (
            (explicit_app_name and active_default_app and explicit_app_name.lower() != active_default_app.lower())
            or (explicit_dept_name and active_default_dept and explicit_dept_name.lower() != active_default_dept.lower())
            or (
                inferred_group_guild
                and active_default_dept
                and inferred_group_guild.strip().lower() != str(active_default_dept).strip().lower()
            )
        ):
            result = {
                'accepted': False,
                'ignored': True,
                'reason': 'app_guild_mismatch',
                'reply_phone': normalized_reply_phone,
                'reply_id': resolved_account_id or '-',
                'reply_group': resolved_group or '-',
                'reply_code': resolved_invite_code or invite_code_meta.get('raw_input') or '-',
            }
            return _finalize(message_id, result)

        if invalid_group_candidate and not resolved_group:
            result = {
                'accepted': False,
                'ignored': True,
                'reason': 'invalid_group_format',
                'reply_phone': normalized_reply_phone,
                'reply_id': resolved_account_id or '-',
                'reply_group': invalid_group_candidate,
            }
            return _finalize(message_id, result)

        missing_labels = []
        if not resolved_group:
            missing_labels.append('Group')
        if not resolved_account_id:
            missing_labels.append('ID')
        has_phone_input = bool(str(explicit_fields.get('mobile') or '').strip() or bare_candidates.get('mobile_line') or mobile_match)
        if not has_phone_input:
            missing_labels.append('Phone')

        if not has_phone_input and not resolved_group and not resolved_account_id and not resolved_invite_code:
            result = {
                'accepted': False,
                'ignored': True,
                'reason': 'irrelevant_message',
                'reply_phone': normalized_reply_phone,
                'reply_id': resolved_account_id or '-',
                'reply_group': resolved_group or '-',
            }
            return _finalize(message_id, result)
        if missing_labels:
            result = {
                'accepted': False,
                'ignored': True,
                'reason': 'missing_required_fields',
                'reply_phone': normalized_reply_phone,
                'reply_id': resolved_account_id or '-',
                'reply_group': resolved_group or '-',
                'reply_missing_fields': missing_labels,
            }
            return _finalize(message_id, result)

        fast_validation_error = validate_fast_intake_fields(
            mobile=normalized_reply_phone if normalized_reply_phone != '-' else None,
            app_name=resolved_app_name,
            account_id=resolved_account_id,
        )
        if fast_validation_error:
            result = {
                'accepted': False,
                'ignored': True,
                'reason': fast_validation_error['reason'],
                'reply_phone': normalized_reply_phone,
                'reply_id': resolved_account_id or '-',
                'reply_group': resolved_group or '-',
                'reply_error_text': fast_validation_error['reply_text'],
            }
            return _finalize(message_id, result)
        invite_validation_error = validate_invite_code_field(invite_match_value or explicit_fields.get('invite_code') or parsed_text.get('evidence', {}).get('invite_code_raw_input') or None, invite_code_meta=invite_code_meta)
        if invite_validation_error:
            result = {
                'accepted': False,
                'ignored': True,
                'reason': invite_validation_error['reason'],
                'reply_phone': normalized_reply_phone,
                'reply_id': resolved_account_id or '-',
                'reply_group': resolved_group or '-',
                'reply_code': invite_code_meta.get('raw_input') or '-',
                'reply_error_text': invite_validation_error['reply_text'],
            }
            return _finalize(message_id, result)
        if not resolved_invite_code and self.require_invite_code:
            result = {
                'accepted': False,
                'ignored': True,
                'reason': 'missing_required_fields',
                'reply_phone': normalized_reply_phone,
                'reply_id': resolved_account_id or '-',
                'reply_group': resolved_group or '-',
                'reply_missing_fields': ['Code'],
            }
            return _finalize(message_id, result)

        intake_response = self._submit_manual_cs_sync(
            ManualCsSubmissionRequest(
                mobile=normalized_reply_phone,
                registration_group=resolved_group,
                app_name=resolved_app_name,
                dept_name=resolved_dept_name,
                invite_code=resolved_invite_code,
                app_name_explicit=bool(explicit_app_name),
                dept_name_explicit=bool(explicit_dept_name),
                submission_type='account_id' if resolved_account_id else 'screenshot',
                account_id=resolved_account_id,
                file_url='https://placeholder.lark.local/pending-image' if not resolved_account_id else None,
                file_type='text/plain' if not resolved_account_id else None,
                submitted_by=f'lark:{sender_id}',
                source_channel='manual_cs_lark',
                source_bot_app_id=bot_app_id or None,
                source_message_id=message_id,
                source_chat_id=str(message.get('chat_id') or '') or None,
                remark=cleaned_text,
                submitted_at=utc_now(),
            )
        )
        intake_response['source'] = 'lark_event_bridge'
        intake_response['chat_type'] = chat_type
        intake_response['reply_phone'] = normalized_reply_phone
        intake_response['reply_id'] = resolved_account_id or '-'
        intake_response['reply_group'] = resolved_group or '-'
        return _finalize(message_id, intake_response)

    def recognition_result(self, task_id: str, payload: RecognitionResultRequest) -> Dict[str, Any]:
        now = utc_now()
        with self.db.connect() as conn:
            task = conn.execute("SELECT lead_id, payload FROM automation_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="task not found")
            task_payload = json.loads(task["payload"] or "{}")
            submission_id = task_payload.get("submission_id")
            if not submission_id:
                raise HTTPException(status_code=400, detail="submission_id missing from task payload")
            recognized_account_id = payload.recognized_account_id if payload.status == "success" else None
            recognition_status = "success" if payload.status == "success" else "failed"
            conn.execute(
                """
                UPDATE account_submissions
                SET recognition_status = ?, recognized_account_id = ?, recognition_raw = ?, updated_at = ?
                WHERE submission_id = ?
                """,
                (
                    recognition_status,
                    recognized_account_id,
                    json.dumps(payload.raw_result, ensure_ascii=False),
                    now,
                    submission_id,
                ),
            )
            conn.execute(
                """
                UPDATE automation_tasks
                SET status = ?, result_code = ?, result_reason = ?, finished_at = ?, raw_result = ?
                WHERE task_id = ?
                """,
                (
                    payload.status,
                    payload.result_code,
                    payload.result_reason,
                    payload.finished_at,
                    json.dumps(payload.raw_result, ensure_ascii=False),
                    task_id,
                ),
            )
            if payload.status == "success" and recognized_account_id and str(recognized_account_id).isdigit():
                bind_task_id = create_id("task")
                bind_payload = {
                    "submission_id": submission_id,
                    "lead_id": task["lead_id"],
                    "account_id": str(recognized_account_id),
                    "source_bot_app_id": task_payload.get("source_bot_app_id"),
                    "source_message_id": task_payload.get("source_message_id"),
                }
                conn.execute(
                    """
                    INSERT INTO automation_tasks (
                        task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bind_task_id,
                        task["lead_id"],
                        "bind_check",
                        "P0",
                        json.dumps(bind_payload, ensure_ascii=False),
                        f"bind_check:{task['lead_id']}:{submission_id}",
                        "system",
                        payload.finished_at,
                        "pending",
                    ),
                )
                conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("account_submitted", now, task["lead_id"]))
                self._record_status_history(
                    conn,
                    lead_id=task["lead_id"],
                    from_status="recognition_pending",
                    to_status="account_submitted",
                    trigger_type="recognition_success",
                    trigger_source="recognition_result",
                    trigger_task_id=task_id,
                    remark=str(recognized_account_id),
                )
                return {
                    "task_id": task_id,
                    "lead_status": "account_submitted",
                    "next_action": "queue_bind_check",
                    "bind_task_type": "bind_check",
                    "recognized_account_id": str(recognized_account_id),
                }
            conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("re_engage_pending", now, task["lead_id"]))
            self._record_status_history(
                conn,
                lead_id=task["lead_id"],
                from_status="recognition_pending",
                to_status="re_engage_pending",
                trigger_type="recognition_failed",
                trigger_source="recognition_result",
                trigger_task_id=task_id,
            )
            return {
                "task_id": task_id,
                "lead_status": "re_engage_pending",
                "next_action": "manual_recovery",
                "recognized_account_id": recognized_account_id,
            }

    def run_native_ocr(self, task_id: str) -> Dict[str, Any]:
        if self.ocr_adapter is None:
            raise HTTPException(status_code=503, detail='ocr adapter not configured')
        with self.db.connect() as conn:
            task = conn.execute("SELECT task_id, lead_id, task_type, payload, status FROM automation_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail='task not found')
            if task['task_type'] != 'account_recognition':
                raise HTTPException(status_code=400, detail='task is not account_recognition')
            task_payload = json.loads(task['payload'] or '{}')
            file_url = task_payload.get('file_url')
            if not file_url:
                raise HTTPException(status_code=400, detail='file_url missing from task payload')
            conn.execute("UPDATE automation_tasks SET status = ? WHERE task_id = ?", ('running', task_id))

        extracted = self.ocr_adapter.extract_text(file_url)
        raw_text = str((extracted or {}).get('raw_text') or '').strip()
        normalized = normalize_native_ocr_fields(raw_text)
        recognized_account_id = normalized.get('account_id')
        status = 'success' if str(recognized_account_id or '').isdigit() else 'failed'
        result_code = 'recognized' if status == 'success' else 'ocr_no_account_id'
        result_reason = 'native ocr success' if status == 'success' else 'native ocr failed to extract account id'
        result = self.recognition_result(
            task_id,
            RecognitionResultRequest(
                status=status,
                recognized_account_id=str(recognized_account_id) if recognized_account_id else None,
                result_code=result_code,
                result_reason=result_reason,
                finished_at=utc_now(),
                raw_result={
                    'ocr_engine': (extracted or {}).get('engine'),
                    'ocr_raw_text': raw_text,
                    'normalized': normalized,
                    'person_code': normalized.get('person_code'),
                    'guild_invite_code': normalized.get('guild_invite_code'),
                    'invite_code': normalized.get('invite_code'),
                },
            ),
        )
        return {
            'task_id': task_id,
            'status': status,
            'recognized_account_id': str(recognized_account_id) if recognized_account_id else None,
            'person_code': normalized.get('person_code'),
            'guild_invite_code': normalized.get('guild_invite_code'),
            'invite_code': normalized.get('invite_code'),
            **result,
        }

    def _infer_executor_guild_from_registration_group(self, registration_group: Optional[str]) -> Optional[str]:
        normalized_group = str(registration_group or '').strip()
        if not normalized_group:
            return None
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT guild_name, enabled FROM guild_executors").fetchall()]
        lowered_group = normalized_group.lower()
        for row in rows:
            guild_name = str(row.get('guild_name') or '').strip()
            if not guild_name or not bool(row.get('enabled', 1)):
                continue
            if lowered_group == guild_name.lower() or lowered_group.startswith(f"{guild_name.lower()}-"):
                return guild_name
        return None

    def _resolve_expected_bind_guild(self, *, task_payload: Dict[str, Any], lead_row: Optional[sqlite3.Row]) -> Optional[str]:
        registration_group = ''
        lead_guild = ''
        crm_verified_guild = ''
        if lead_row:
            registration_group = str(lead_row['pendaftaran_group'] or '').strip()
            lead_guild = str(lead_row['dept_name'] or '').strip()
            crm_verified_guild = str(lead_row['crm_verified_dept_name'] or '').strip()
        inferred_executor_guild = self._infer_executor_guild_from_registration_group(registration_group)
        if crm_verified_guild:
            return crm_verified_guild
        bot_app_id = str(task_payload.get('source_bot_app_id') or '').strip()
        if bot_app_id:
            preset = self.resolve_intake_bot_preset(app_id=bot_app_id)
            preset_guild = str(preset.get('default_guild') or '').strip()
            if preset_guild:
                if inferred_executor_guild and inferred_executor_guild.strip().lower() != preset_guild.strip().lower():
                    preset_executor = self.resolve_guild_executor(preset_guild)
                    if not preset_executor:
                        return inferred_executor_guild
                return preset_guild
        if inferred_executor_guild:
            return inferred_executor_guild
        if lead_guild:
            return lead_guild
        return None

    def _extract_backend_bind_guild(self, raw_result: Optional[Dict[str, Any]]) -> Optional[str]:
        if not isinstance(raw_result, dict):
            return None
        for key in ('deptName', 'guild_code', 'guildName', 'guild'):
            value = str(raw_result.get(key) or '').strip()
            if value:
                return value
        return None

    def _detect_bind_backend_guild_mismatch(self, *, task_payload: Dict[str, Any], lead_row: Optional[sqlite3.Row], raw_result: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        expected_guild = self._resolve_expected_bind_guild(task_payload=task_payload, lead_row=lead_row)
        backend_guild = self._extract_backend_bind_guild(raw_result)
        if not expected_guild or not backend_guild:
            return None
        if expected_guild.strip().lower() == backend_guild.strip().lower():
            return None
        return {
            'expected_guild': expected_guild,
            'backend_guild': backend_guild,
            'result_reason': f'Configured guild {expected_guild} does not match backend guild {backend_guild}.',
        }

    def _classify_bind_human_action(self, *, result_code: Optional[str], result_reason: Optional[str], raw_result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized_code = str(result_code or '').strip().lower()
        normalized_reason = str(result_reason or '').strip().lower()
        raw = raw_result or {}
        if raw.get('captcha_required'):
            return {'requires_human_action': True, 'human_action_type': 'captcha_required'}
        if raw.get('manual_continue_required'):
            return {'requires_human_action': True, 'human_action_type': 'manual_continue_required'}
        if raw.get('session_expired'):
            return {'requires_human_action': True, 'human_action_type': 'session_expired'}
        if raw.get('auth_required'):
            return {'requires_human_action': True, 'human_action_type': 'auth_required'}
        if normalized_code in {'bind_unauthorized', 'auth_required'}:
            return {'requires_human_action': True, 'human_action_type': 'auth_required'}
        if normalized_code in {'session_expired', 'bind_session_expired'}:
            return {'requires_human_action': True, 'human_action_type': 'session_expired'}
        if normalized_code in {'captcha_required', 'bind_captcha_required'}:
            return {'requires_human_action': True, 'human_action_type': 'captcha_required'}
        if normalized_code in {'manual_continue_required', 'bind_manual_continue_required'}:
            return {'requires_human_action': True, 'human_action_type': 'manual_continue_required'}
        if 'please re-login' in normalized_reason or 're-login' in normalized_reason:
            return {'requires_human_action': True, 'human_action_type': 'session_expired'}
        if 'captcha' in normalized_reason:
            return {'requires_human_action': True, 'human_action_type': 'captcha_required'}
        if 'status code 401' in normalized_reason or 'unauthorized' in normalized_reason or 'forbidden' in normalized_reason:
            return {'requires_human_action': True, 'human_action_type': 'auth_required'}
        return {'requires_human_action': False, 'human_action_type': None}

    def _classify_bind_failure(self, *, result_code: Optional[str], result_reason: Optional[str], raw_result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized_code = str(result_code or '').strip().lower()
        reason_text = str(result_reason or '').strip()
        normalized_reason = reason_text.lower()
        human = self._classify_bind_human_action(
            result_code=result_code,
            result_reason=result_reason,
            raw_result=raw_result,
        )
        if normalized_code == 'bind_backend_guild_mismatch':
            return {
                'failure_category': 'routing_mismatch',
                'failure_stage': 'bind',
                'retryable': False,
                'requires_human_action': False,
                'human_action_type': None,
                'operator_reason': 'Bot/guild routing mismatch. Check app/agency mapping.',
            }
        if normalized_code == 'bind_executor_profile_not_configured' or 'no chrome profile mapping configured' in normalized_reason:
            return {
                'failure_category': 'routing_mismatch',
                'failure_stage': 'bind',
                'retryable': False,
                'requires_human_action': False,
                'human_action_type': None,
                'operator_reason': 'No guild executor profile mapping. Check app/agency routing.',
            }
        if 'the streamer was in other guild' in normalized_reason:
            return {
                'failure_category': 'already_in_other_agency',
                'failure_stage': 'bind',
                'retryable': False,
                'requires_human_action': False,
                'human_action_type': None,
                'operator_reason': 'Account is already in another agency.',
            }
        if 'batas maksimum guild' in normalized_reason or 'maximum guild' in normalized_reason:
            return {
                'failure_category': 'device_duplicate_registration',
                'failure_stage': 'bind',
                'retryable': False,
                'requires_human_action': False,
                'human_action_type': None,
                'operator_reason': 'Device/account has reached the guild join limit.',
            }
        if 'invalid person code' in normalized_reason or 'invalid invite code' in normalized_reason:
            return {
                'failure_category': 'invalid_personal_code',
                'failure_stage': 'bind',
                'retryable': False,
                'requires_human_action': False,
                'human_action_type': None,
                'operator_reason': 'Personal bind code is invalid for this agency.',
            }
        if human.get('requires_human_action'):
            human_action_type = human.get('human_action_type')
            human_reason_map = {
                'auth_required': 'Bind backend authorization expired. Re-login required.',
                'session_expired': 'Bind backend session expired. Re-login required.',
                'captcha_required': 'Bind backend requires captcha/manual confirmation.',
                'manual_continue_required': 'Bind backend requires manual confirmation to continue.',
            }
            return {
                'failure_category': human_action_type or 'manual_intervention_required',
                'failure_stage': 'bind',
                'retryable': False,
                'requires_human_action': True,
                'human_action_type': human_action_type,
                'operator_reason': human_reason_map.get(str(human_action_type or '').strip(), reason_text or 'Bind requires manual intervention.'),
            }
        retryable_keywords = (
            'timeout',
            'timed out',
            'gateway',
            'temporarily',
            'unavailable',
            'connection',
            'reset',
            'broken pipe',
            'empty response',
            'non-json response',
            'econnreset',
            'net::err',
            'chrome-error://',
        )
        retryable_codes = {
            'bind_execution_error',
            'bind_backend_http_500',
            'bind_backend_http_502',
            'bind_backend_http_503',
            'bind_backend_http_504',
            'bind_backend_timeout',
            'bind_transport_error',
        }
        if normalized_code in retryable_codes or any(keyword in normalized_reason for keyword in retryable_keywords):
            return {
                'failure_category': 'technical_retryable',
                'failure_stage': 'bind',
                'retryable': True,
                'requires_human_action': False,
                'human_action_type': None,
                'operator_reason': 'Temporary bind execution error. System will retry automatically.',
            }
        return {
            'failure_category': 'bind_failed',
            'failure_stage': 'bind',
            'retryable': False,
            'requires_human_action': False,
            'human_action_type': None,
            'operator_reason': reason_text or 'Bind failed.',
        }

    def _format_operator_bind_failure_reason(self, *, failure_meta: Dict[str, Any], raw_reason: Optional[str], retried: bool = False) -> str:
        category = str((failure_meta or {}).get('failure_category') or '').strip()
        base_reason = str((failure_meta or {}).get('operator_reason') or '').strip() or str(raw_reason or '').strip() or 'Bind failed.'
        if category == 'technical_retryable' and retried:
            return f'Bind failed after {self.bind_retry_max_attempts} retries. Check guild executor/network manually.'
        return base_reason

    def _build_bind_retry_task_payload(self, *, source_payload: Dict[str, Any], retry_count: int) -> Dict[str, Any]:
        payload = dict(source_payload or {})
        payload['retry_count'] = retry_count
        return payload

    def _schedule_bind_retry_task(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
        source_task_payload: Dict[str, Any],
        source_created_by: Optional[str],
        retry_count: int,
    ) -> Dict[str, Any]:
        submission_id = str(source_task_payload.get('submission_id') or '').strip()
        payload = self._build_bind_retry_task_payload(source_payload=source_task_payload, retry_count=retry_count)
        task_id = create_id('task')
        dedupe_parts = ['bind_retry', lead_id, submission_id or 'no_submission', str(retry_count)]
        conn.execute(
            """
            INSERT INTO automation_tasks (
                task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status,
                result_code, result_reason, retry_count, raw_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                lead_id,
                'bind_check',
                'P0',
                json.dumps(payload, ensure_ascii=False),
                ':'.join(dedupe_parts),
                source_created_by or 'system:auto_retry_bind',
                utc_now(),
                'pending',
                'bind_retry_pending',
                f'bind retry scheduled {retry_count}/{self.bind_retry_max_attempts}',
                retry_count,
                json.dumps({'retry_count': retry_count}, ensure_ascii=False),
            ),
        )
        return {'task_id': task_id, 'retry_count': retry_count}

    def _sync_crm_after_bind_success(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
        account_id: Optional[str],
        task_id: str,
        bind_result_reason: Optional[str],
        bind_raw_result: Optional[Dict[str, Any]],
        submission_id: Optional[str] = None,
        reply_context: Optional[Dict[str, Any]] = None,
        retry_attempt: int = 0,
        suppress_failure_notification: bool = False,
    ) -> Dict[str, Any]:
        crm_sync_failed = None
        crm_retry_pending = False
        crm_retryable = False
        crm_payload = None
        crm_response = None
        verified_row = None
        lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        if self.crm_adapter is not None and lead_row:
            lead_dict = dict(lead_row)
            resolved_app = self._resolve_crm_app_mapping(lead_dict.get('app_name'))
            resolved_dept = self._resolve_crm_dept_mapping(
                (bind_raw_result or {}).get('deptName') or lead_dict.get('dept_name'),
                (bind_raw_result or {}).get('deptId'),
            )
            crm_payload = {
                'mobile': str(lead_dict.get('mobile') or ''),
                'ywId': str(account_id or ''),
                'name': '',
                'remark': bind_result_reason or '',
                'dept': '',
                'wa': '',
                'areaCode': str(lead_dict.get('area_code') or ''),
                'inviterId': lead_dict.get('inviter_id'),
                'appName': resolved_app['appName'],
                'appId': resolved_app['appId'],
                'pendaftaranGroup': lead_dict.get('pendaftaran_group') or '',
                'paymentStatus': '',
                'pzStatus': 0,
                'userQuality': '',
                'fileUrl': '',
                'deptName': resolved_dept['deptName'],
                'deptId': resolved_dept['deptId'],
            }
            mapping_failure = self._precheck_crm_mapping_failure(
                resolved_app=resolved_app,
                resolved_dept=resolved_dept,
            )
            if mapping_failure:
                self._record_sync_log(
                    conn,
                    lead_id=lead_id,
                    task_id=task_id,
                    sync_type='customer_upsert',
                    target_system='crm',
                    status='failed',
                    request_snapshot=crm_payload,
                    response_snapshot={
                        'action': 'mapping_precheck',
                        'mapping_failure': mapping_failure,
                        'resolved_app': resolved_app,
                        'resolved_dept': resolved_dept,
                        'submission_id': submission_id,
                        'retry_attempt': retry_attempt,
                    },
                )
                crm_sync_failed = mapping_failure
            else:
                verified_row = None
                if retry_attempt > 0:
                    verified_row = self._find_existing_customer_with_fallback(
                        yw_id=account_id,
                        mobile=lead_dict.get('mobile'),
                        app_name=resolved_app['appName'],
                        dept_name=resolved_dept['deptName'],
                        registration_group=lead_dict.get('pendaftaran_group') or '',
                    )
                if verified_row:
                    self._record_sync_log(
                        conn,
                        lead_id=lead_id,
                        task_id=task_id,
                        sync_type='customer_upsert',
                        target_system='crm',
                        status='success',
                        request_snapshot=crm_payload,
                        response_snapshot={
                            'action': 'verify_before_retry',
                            'crm_response': {'code': 0, 'msg': 'verified_existing_before_retry'},
                            'verified_after_write': True,
                            'submission_id': submission_id,
                            'retry_attempt': retry_attempt,
                            'reply_context': reply_context or {},
                        },
                    )
                    self._record_verified_crm_state(conn, lead_id=lead_id, crm_payload=crm_payload)
                    mobile, yw_id = self._resolve_lead_notification_context(conn, lead_id)
                    self._queue_operator_notification(
                        conn,
                        lead_id=lead_id,
                        notification_type='crm_record_success',
                        mobile=mobile,
                        yw_id=yw_id,
                        write_result='success',
                    )
                else:
                    crm_response = self.crm_adapter.create_customer(crm_payload)
                    crm_action = 'create'
                    verified_after_write = None
                    if crm_response.get('code') == 0:
                        verified_after_write = self._find_existing_customer_with_fallback(
                            yw_id=account_id,
                            mobile=lead_dict.get('mobile'),
                            app_name=resolved_app['appName'],
                            dept_name=resolved_dept['deptName'],
                            registration_group=lead_dict.get('pendaftaran_group') or '',
                        )
                    verified_row = verified_after_write
                    self._record_sync_log(
                        conn,
                        lead_id=lead_id,
                        task_id=task_id,
                        sync_type='customer_upsert',
                        target_system='crm',
                        status='success' if crm_response.get('code') == 0 and verified_after_write else 'failed',
                        request_snapshot=crm_payload,
                        response_snapshot={
                            'action': crm_action,
                            'crm_response': crm_response,
                            'verified_after_write': bool(verified_after_write),
                            'submission_id': submission_id,
                            'retry_attempt': retry_attempt,
                            'reply_context': reply_context or {},
                        },
                    )
                    if crm_response.get('code') != 0:
                        crm_sync_failed = self._normalize_crm_failure_reason(
                            crm_response,
                            fallback_found=False,
                        )
                        crm_retryable = self._is_retryable_crm_failure(crm_response)
                        crm_retry_pending = crm_retryable and retry_attempt < self.crm_retry_max_attempts
                    elif not verified_after_write:
                        crm_sync_failed = 'CRM write could not be verified.'
                    else:
                        self._record_verified_crm_state(conn, lead_id=lead_id, crm_payload=crm_payload)
                        mobile, yw_id = self._resolve_lead_notification_context(conn, lead_id)
                        self._queue_operator_notification(
                            conn,
                            lead_id=lead_id,
                            notification_type='crm_record_success',
                            mobile=mobile,
                            yw_id=yw_id,
                            write_result='success',
                        )
        if crm_sync_failed and not (crm_retry_pending or suppress_failure_notification):
            lead_mobile_row = conn.execute("SELECT mobile FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            self._queue_operator_notification(
                conn,
                lead_id=lead_id,
                notification_type="crm_record_failed",
                mobile=(lead_mobile_row['mobile'] if lead_mobile_row else ''),
                yw_id=account_id,
                write_result="failed",
                reason=self._format_operator_crm_failure_reason(retried=retry_attempt > 0),
            )
        return {
            'crm_sync_failed': crm_sync_failed,
            'crm_verified': crm_sync_failed is None,
            'current_submission_crm_verified': crm_sync_failed is None,
            'crm_retry_pending': crm_retry_pending,
            'crm_retryable': crm_retryable,
            'crm_verified_row': verified_row,
            'crm_payload': crm_payload,
            'crm_response': crm_response,
        }

    def _is_retryable_crm_failure(self, crm_response: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(crm_response, dict):
            return False
        if self._crm_response_looks_like_duplicate(crm_response):
            return False
        code = crm_response.get('code')
        msg = str(crm_response.get('msg') or '').strip().lower()
        if isinstance(code, int) and code >= 500:
            return True
        retry_keywords = (
            '服务器内部异常',
            'internal',
            'timeout',
            'timed out',
            'gateway',
            'temporarily',
            'unavailable',
            'non-json response',
            'connection',
            'reset',
            'broken pipe',
        )
        lowered = msg.lower()
        return any(keyword.lower() in lowered for keyword in retry_keywords)

    def _build_crm_retry_task_payload(
        self,
        *,
        submission_id: str,
        lead_id: str,
        account_id: str,
        bind_result_reason: str,
        bind_raw_result: Optional[Dict[str, Any]],
        source_payload: Optional[Dict[str, Any]],
        retry_count: int,
        next_retry_at: str,
    ) -> Dict[str, Any]:
        source_payload = source_payload or {}
        return {
            'submission_id': submission_id,
            'lead_id': lead_id,
            'account_id': account_id,
            'bind_result_reason': bind_result_reason,
            'bind_raw_result': bind_raw_result or {},
            'source_message_id': str(source_payload.get('source_message_id') or ''),
            'source_chat_id': str(source_payload.get('source_chat_id') or ''),
            'source_bot_app_id': str(source_payload.get('source_bot_app_id') or ''),
            'retry_count': retry_count,
            'next_retry_at': next_retry_at,
        }

    def _schedule_crm_retry_task(
        self,
        conn: sqlite3.Connection,
        *,
        submission_id: str,
        lead_id: str,
        account_id: str,
        bind_result_reason: str,
        bind_raw_result: Optional[Dict[str, Any]],
        source_payload: Optional[Dict[str, Any]],
        retry_count: int,
    ) -> Optional[Dict[str, Any]]:
        if retry_count > self.crm_retry_max_attempts:
            return None
        delay_index = min(max(retry_count - 1, 0), max(len(self.crm_retry_delays_seconds) - 1, 0))
        delay_seconds = int(self.crm_retry_delays_seconds[delay_index]) if self.crm_retry_delays_seconds else 0
        next_retry_dt = datetime.now(timezone.utc) + timedelta(seconds=max(0, delay_seconds))
        next_retry_at = next_retry_dt.isoformat()
        payload = self._build_crm_retry_task_payload(
            submission_id=submission_id,
            lead_id=lead_id,
            account_id=account_id,
            bind_result_reason=bind_result_reason,
            bind_raw_result=bind_raw_result,
            source_payload=source_payload,
            retry_count=retry_count,
            next_retry_at=next_retry_at,
        )
        existing = conn.execute(
            "SELECT task_id FROM automation_tasks WHERE dedupe_key = ? LIMIT 1",
            (f'crm_retry:{submission_id}',),
        ).fetchone()
        now = utc_now()
        if existing:
            task_id = str(existing['task_id'])
            conn.execute(
                """
                UPDATE automation_tasks
                SET payload = ?, priority = ?, created_by = ?, status = 'pending', retry_count = ?,
                    result_code = ?, result_reason = ?, started_at = NULL, finished_at = NULL, raw_result = ?, created_at = ?
                WHERE task_id = ?
                """,
                (
                    json.dumps(payload, ensure_ascii=False),
                    'P0',
                    'system:auto_retry_crm',
                    retry_count,
                    'crm_retry_pending',
                    f'crm retry scheduled attempt {retry_count}/{self.crm_retry_max_attempts}',
                    json.dumps({'retry_count': retry_count, 'next_retry_at': next_retry_at}, ensure_ascii=False),
                    now,
                    task_id,
                ),
            )
        else:
            task_id = create_id('task')
            conn.execute(
                """
                INSERT INTO automation_tasks (
                    task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status,
                    result_code, result_reason, retry_count, raw_result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    lead_id,
                    'crm_sync_retry',
                    'P0',
                    json.dumps(payload, ensure_ascii=False),
                    f'crm_retry:{submission_id}',
                    'system:auto_retry_crm',
                    now,
                    'pending',
                    'crm_retry_pending',
                    f'crm retry scheduled attempt {retry_count}/{self.crm_retry_max_attempts}',
                    retry_count,
                    json.dumps({'retry_count': retry_count, 'next_retry_at': next_retry_at}, ensure_ascii=False),
                ),
            )
        return {'task_id': task_id, 'retry_count': retry_count, 'next_retry_at': next_retry_at}

    def _process_crm_retry_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(task.get('payload_dict') or {})
        task_id = str(task.get('task_id') or '')
        submission_id = str(payload.get('submission_id') or '').strip()
        lead_id = str(payload.get('lead_id') or task.get('lead_id') or '').strip()
        account_id = str(payload.get('account_id') or '').strip()
        bind_result_reason = str(payload.get('bind_result_reason') or '').strip()
        bind_raw_result = payload.get('bind_raw_result') if isinstance(payload.get('bind_raw_result'), dict) else {}
        reply_context = {
            'source_message_id': str(payload.get('source_message_id') or ''),
            'source_chat_id': str(payload.get('source_chat_id') or ''),
            'source_bot_app_id': str(payload.get('source_bot_app_id') or ''),
        }
        retry_count = int(payload.get('retry_count') or task.get('retry_count') or 0)
        now = utc_now()
        with self.db.connect() as conn:
            crm_sync = self._sync_crm_after_bind_success(
                conn,
                lead_id=lead_id,
                account_id=account_id,
                task_id=task_id,
                bind_result_reason=bind_result_reason,
                bind_raw_result=bind_raw_result,
                submission_id=submission_id,
                reply_context=reply_context,
                retry_attempt=retry_count,
                suppress_failure_notification=True,
            )
            final_verified_row = crm_sync.get('crm_verified_row') if isinstance(crm_sync.get('crm_verified_row'), dict) else None
            if crm_sync.get('crm_sync_failed') and not crm_sync.get('crm_retry_pending') and not final_verified_row:
                crm_payload = crm_sync.get('crm_payload') if isinstance(crm_sync.get('crm_payload'), dict) else {}
                try:
                    final_verified_row = self._find_existing_customer_with_fallback(
                        yw_id=account_id,
                        mobile=crm_payload.get('mobile'),
                        app_name=crm_payload.get('appName'),
                        dept_name=crm_payload.get('deptName'),
                        registration_group=crm_payload.get('pendaftaranGroup') or '',
                    )
                except Exception:
                    final_verified_row = None
                if final_verified_row:
                    self._record_sync_log(
                        conn,
                        lead_id=lead_id,
                        task_id=task_id,
                        sync_type='customer_upsert',
                        target_system='crm',
                        status='success',
                        request_snapshot=crm_payload,
                        response_snapshot={
                            'action': 'verify_after_final_retry',
                            'crm_response': {'code': 0, 'msg': 'verified_existing_after_final_retry'},
                            'verified_after_write': True,
                            'submission_id': submission_id,
                            'retry_attempt': retry_count,
                            'reply_context': reply_context or {},
                        },
                    )
                    self._record_verified_crm_state(conn, lead_id=lead_id, crm_payload=crm_payload)
                    crm_sync = {
                        **crm_sync,
                        'crm_sync_failed': None,
                        'crm_verified': True,
                        'current_submission_crm_verified': True,
                        'crm_verified_row': final_verified_row,
                    }
            if crm_sync.get('crm_sync_failed') is None:
                conn.execute(
                    "UPDATE automation_tasks SET status = 'success', result_code = ?, result_reason = ?, finished_at = ?, raw_result = ? WHERE task_id = ?",
                    (
                        'crm_retry_succeeded',
                        'crm retry succeeded and verified',
                        now,
                        json.dumps({'crm_verified': True}, ensure_ascii=False),
                        task_id,
                    ),
                )
                created_group_join = self._queue_group_join_after_verified_crm(
                    conn,
                    lead_id=lead_id,
                    submission_id=submission_id or None,
                    account_id=account_id,
                    created_at=now,
                )
                conn.commit()
                result = {
                    'task_id': task_id,
                    'lead_status': 'bind_success',
                    'next_action': 'queue_group_join',
                    'crm_verified': True,
                    'current_submission_crm_verified': True,
                    **created_group_join,
                    'accepted': True,
                    'reason': None,
                    'result_reason': None,
                }
            elif crm_sync.get('crm_retry_pending'):
                scheduled = self._schedule_crm_retry_task(
                    conn,
                    submission_id=submission_id,
                    lead_id=lead_id,
                    account_id=account_id,
                    bind_result_reason=bind_result_reason,
                    bind_raw_result=bind_raw_result,
                    source_payload=reply_context,
                    retry_count=retry_count + 1,
                )
                if scheduled:
                    conn.commit()
                    return {
                        'task_id': task_id,
                        'lead_status': 'bind_success',
                        'next_action': 'queue_crm_sync_retry',
                        'reason': 'crm_sync_retry_pending',
                        'result_reason': crm_sync.get('crm_sync_failed'),
                        'crm_verified': False,
                        'current_submission_crm_verified': False,
                        'accepted': False,
                        'retry_task_id': scheduled['task_id'],
                        'retry_count': scheduled['retry_count'],
                        'next_retry_at': scheduled['next_retry_at'],
                    }
                mobile, yw_id = self._resolve_lead_notification_context(conn, lead_id)
                response_code = crm_sync.get('crm_response_code')
                detail = crm_sync.get('crm_sync_failed') or 'CRM write was rejected.'
                final_reason = f'crm retry exhausted after {retry_count} attempts: {detail}' + (f' (code={response_code})' if response_code not in (None, '') else '')
                self._queue_operator_notification(
                    conn,
                    lead_id=lead_id,
                    notification_type='crm_record_failed',
                    mobile=mobile,
                    yw_id=yw_id,
                    write_result='failed',
                    reason=self._format_operator_crm_failure_reason(retried=True),
                )
                conn.execute(
                    "UPDATE automation_tasks SET status = 'failed', result_code = ?, result_reason = ?, finished_at = ?, raw_result = ? WHERE task_id = ?",
                    ('crm_retry_exhausted', final_reason, now, json.dumps({'crm_retry_exhausted': True}, ensure_ascii=False), task_id),
                )
                conn.commit()
                result = {
                    'task_id': task_id,
                    'lead_status': 'bind_success',
                    'next_action': 'retry_crm_sync',
                    'reason': 'crm_sync_failed',
                    'result_code': 'crm_retry_exhausted',
                    'result_reason': final_reason,
                    'crm_verified': False,
                    'current_submission_crm_verified': False,
                    'accepted': False,
                }
            else:
                mobile, yw_id = self._resolve_lead_notification_context(conn, lead_id)
                response_code = crm_sync.get('crm_response_code')
                detail = crm_sync.get('crm_sync_failed') or 'CRM write was rejected.'
                final_reason = f'crm retry exhausted after {retry_count} attempts: {detail}' + (f' (code={response_code})' if response_code not in (None, '') else '')
                self._queue_operator_notification(
                    conn,
                    lead_id=lead_id,
                    notification_type='crm_record_failed',
                    mobile=mobile,
                    yw_id=yw_id,
                    write_result='failed',
                    reason=self._format_operator_crm_failure_reason(retried=True),
                )
                conn.execute(
                    "UPDATE automation_tasks SET status = 'failed', result_code = ?, result_reason = ?, finished_at = ?, raw_result = ? WHERE task_id = ?",
                    ('crm_retry_failed', final_reason, now, json.dumps({'crm_retry_failed': True}, ensure_ascii=False), task_id),
                )
                conn.commit()
                result = {
                    'task_id': task_id,
                    'lead_status': 'bind_success',
                    'next_action': 'retry_crm_sync',
                    'reason': 'crm_sync_failed',
                    'result_code': 'crm_retry_failed',
                    'result_reason': final_reason,
                    'crm_verified': False,
                    'current_submission_crm_verified': False,
                    'accepted': False,
                }

        message_id = reply_context.get('source_message_id') or ''
        chat_id = reply_context.get('source_chat_id') or ''
        if message_id or chat_id:
            with self.db.connect() as conn:
                lead_row = conn.execute("SELECT mobile, area_code, pendaftaran_group, inviter_id FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            reply_envelope = {
                'accepted': bool(result.get('accepted')),
                'reason': result.get('reason'),
                'result_code': result.get('result_code'),
                'result_reason': result.get('result_reason'),
                'lead_status': result.get('lead_status'),
                'next_action': result.get('next_action'),
                'reply_phone': str((lead_row['mobile'] if lead_row else '') or '-'),
                'reply_area_code': int((lead_row['area_code'] if lead_row and lead_row['area_code'] is not None else 0) or 0),
                'reply_id': account_id or '-',
                'reply_group': str((lead_row['pendaftaran_group'] if lead_row else '') or '-'),
                'reply_code': str((lead_row['inviter_id'] if lead_row else '') or '-'),
                'crm_verified': result.get('crm_verified'),
                'current_submission_crm_verified': result.get('current_submission_crm_verified'),
            }
            if self._should_emit_lark_reply(reply_envelope):
                reply_adapter = self._resolve_lark_reply_adapter(app_id=reply_context.get('source_bot_app_id') or None)
                reply_text = self._format_lark_reply_text(reply_envelope)
                result['reply_text'] = reply_text
                self._reply_lark_message(message_id=message_id, chat_id=chat_id, text=reply_text, adapter=reply_adapter)
        return result

    def _queue_group_join_after_verified_crm(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
        submission_id: Optional[str],
        account_id: Optional[str],
        created_at: str,
    ) -> Dict[str, Any]:
        existing_group_join = conn.execute(
            "SELECT task_id FROM automation_tasks WHERE lead_id = ? AND task_type = 'group_join' AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            (lead_id,),
        ).fetchone()
        if existing_group_join:
            group_join_task_id = existing_group_join['task_id']
        else:
            group_join_task_id = create_id("task")
            lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            lead = dict(lead_row) if lead_row else {}
            resolved_target_group = self._resolve_official_group_target_group(lead=lead)
            group_payload = {
                "submission_id": submission_id,
                "lead_id": lead_id,
                "account_id": account_id,
                "target_group": resolved_target_group,
            }
            conn.execute(
                """
                INSERT INTO automation_tasks (
                    task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_join_task_id,
                    lead_id,
                    "group_join",
                    "P0",
                    json.dumps(group_payload, ensure_ascii=False),
                    f"group_join:{lead_id}:{submission_id}",
                    "system",
                    created_at,
                    "pending",
                ),
            )
        return {
            'group_join_task_type': 'group_join',
            'group_join_task_id': group_join_task_id,
        }

    def _resolve_official_group_target_group(self, *, lead: Dict[str, Any]) -> Optional[str]:
        if not isinstance(lead, dict):
            return None
        direct_candidate = str(lead.get('crm_verified_official_group') or '').strip()
        known_target_groups = {
            str(value or '').strip()
            for value in dict(self.official_group_target_map or {}).values()
            if str(value or '').strip()
        }
        if direct_candidate and direct_candidate in known_target_groups:
            return direct_candidate
        registration_group = str(lead.get('pendaftaran_group') or '').strip()
        dept_name = str(lead.get('crm_verified_dept_name') or lead.get('dept_name') or '').strip()
        app_name = str(lead.get('crm_verified_app_name') or lead.get('app_name') or '').strip()
        registration_prefix = registration_group.split('-', 1)[0].strip() if registration_group else ''
        lookup_keys = [
            f'registration_group:{registration_group.lower()}' if registration_group else '',
            f'registration_group_prefix:{registration_prefix.lower()}' if registration_prefix else '',
            f'dept_name:{dept_name.lower()}' if dept_name else '',
            f'app_name:{app_name.lower()}' if app_name else '',
            registration_group.lower() if registration_group else '',
            registration_prefix.lower() if registration_prefix else '',
            dept_name.lower() if dept_name else '',
            app_name.lower() if app_name else '',
        ]
        for key in lookup_keys:
            if not key:
                continue
            candidate = str(self.official_group_target_map.get(key) or '').strip()
            if candidate:
                return candidate
        if direct_candidate:
            return direct_candidate
        return None

    @staticmethod
    def _official_group_phone_match_keys(*, phone: Any, area_code: Any = 0, country: Any = '') -> set[str]:
        keys: set[str] = set()
        raw = str(phone or '').strip()
        digits_only = ''.join(ch for ch in raw if ch.isdigit())
        if digits_only:
            keys.add(digits_only)
        try:
            normalized_mobile, normalized_area_code, _ = normalize_phone_identity(
                mobile=raw,
                area_code=int(area_code or 0),
                country=str(country or '').strip(),
            )
        except Exception:
            normalized_mobile, normalized_area_code = digits_only, int(area_code or 0)
        normalized_mobile = str(normalized_mobile or '').strip()
        if normalized_mobile:
            keys.add(normalized_mobile)
            if normalized_area_code:
                keys.add(f'{int(normalized_area_code)}{normalized_mobile}')
        return keys

    def _match_official_group_requesters_to_leads(
        self,
        *,
        lead_rows: List[sqlite3.Row],
        requesters: List[Dict[str, Any]],
        release_count: int,
    ) -> Tuple[List[sqlite3.Row], List[Dict[str, Any]]]:
        if not requesters:
            return [], []
        candidate_entries: List[Dict[str, Any]] = []
        for lead_row in lead_rows:
            lead = dict(lead_row)
            phone_keys = self._official_group_phone_match_keys(
                phone=lead.get('mobile'),
                area_code=lead.get('area_code'),
                country=lead.get('country'),
            )
            candidate_entries.append({
                'lead_row': lead_row,
                'lead_id': str(lead.get('lead_id') or '').strip(),
                'phone_keys': phone_keys,
            })
        selected_rows: List[Dict[str, Any]] = []
        unmatched_requesters: List[Dict[str, Any]] = []
        used_lead_ids: set[str] = set()
        for requester in list(requesters or [])[:max(0, release_count)]:
            requester_id = str((requester or {}).get('requesterId') or '').strip()
            requester_phone_keys = set()
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=(requester or {}).get('phoneNormalized')))
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=(requester or {}).get('phoneRaw')))
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=(requester or {}).get('debugLidPhoneRaw')))
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=(requester or {}).get('debugContactNumberRaw')))
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=requester_id))
            matches = [
                entry for entry in candidate_entries
                if entry['lead_id'] and entry['lead_id'] not in used_lead_ids and requester_phone_keys.intersection(entry['phone_keys'])
            ]
            if len(matches) == 1:
                matched_lead = dict(matches[0]['lead_row'])
                requester_phone_candidates = self._official_group_requester_phone_candidates(requester)
                matched_lead['matched_requester_phone_hint'] = requester_phone_candidates[0] if requester_phone_candidates else None
                matched_lead['matched_requester_name_hint'] = str((requester or {}).get('displayName') or '').strip() or None
                matched_lead['matched_requester_id'] = requester_id or None
                selected_rows.append(matched_lead)
                used_lead_ids.add(matches[0]['lead_id'])
                continue
            unmatched_requesters.append({
                'requester_id': requester_id or None,
                'display_name': str((requester or {}).get('displayName') or '').strip() or None,
                'phone_raw': str((requester or {}).get('phoneRaw') or '').strip() or None,
                'phone_normalized': str((requester or {}).get('phoneNormalized') or '').strip() or None,
                'debugLidPhoneRaw': str((requester or {}).get('debugLidPhoneRaw') or '').strip() or None,
                'debugContactNumberRaw': str((requester or {}).get('debugContactNumberRaw') or '').strip() or None,
                'requested_at_iso': str((requester or {}).get('requestedAtIso') or '').strip() or None,
                'requested_at_unix': (requester or {}).get('requestedAtUnix'),
                'match_count': len(matches),
            })
        return selected_rows, unmatched_requesters

    def _official_group_requester_phone_candidates(self, requester: Dict[str, Any]) -> List[str]:
        candidates: List[str] = []
        seen: set[str] = set()

        def add_candidate(value: Any) -> None:
            raw = str(value or '').strip()
            if not raw:
                return
            digits = ''.join(ch for ch in raw if ch.isdigit())
            if digits and digits not in seen:
                seen.add(digits)
                candidates.append(digits)
            if raw.startswith('+'):
                try:
                    normalized_mobile, normalized_area_code, _ = normalize_phone_identity(mobile=raw, area_code=0, country='')
                except Exception:
                    normalized_mobile, normalized_area_code = digits, 0
                normalized_mobile = str(normalized_mobile or '').strip()
                if normalized_mobile and normalized_mobile not in seen:
                    seen.add(normalized_mobile)
                    candidates.append(normalized_mobile)
                if normalized_mobile and normalized_area_code:
                    prefixed = f'{int(normalized_area_code)}{normalized_mobile}'
                    if prefixed not in seen:
                        seen.add(prefixed)
                        candidates.append(prefixed)
            elif digits:
                for prefix in sorted(PHONE_PREFIX_COUNTRY_MAP.keys(), key=len, reverse=True):
                    if digits.startswith(prefix) and len(digits) > len(prefix) + 5:
                        local = digits[len(prefix):]
                        if local and local not in seen:
                            seen.add(local)
                            candidates.append(local)
                        break

        add_candidate((requester or {}).get('debugLidPhoneRaw'))
        add_candidate((requester or {}).get('debugContactNumberRaw'))
        add_candidate((requester or {}).get('phoneNormalized'))
        add_candidate((requester or {}).get('phone_normalized'))
        add_candidate((requester or {}).get('phoneRaw'))
        add_candidate((requester or {}).get('phone_raw'))
        add_candidate((requester or {}).get('requesterId'))
        add_candidate((requester or {}).get('requester_id'))
        return candidates

    def _find_crm_customer_for_official_group_requester(self, requester: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        if self.crm_adapter is None:
            return None, None
        for mobile_candidate in self._official_group_requester_phone_candidates(requester):
            try:
                row = self.crm_adapter.find_customer(mobile=mobile_candidate)
            except Exception:
                row = None
            if row:
                return dict(row), mobile_candidate
        return None, None

    def _materialize_crm_only_test_lead_for_official_group_requester(
        self,
        *,
        requester: Dict[str, Any],
        crm_row: Dict[str, Any],
        target_group: str,
        created_at: str,
    ) -> Optional[Dict[str, Any]]:
        matched_mobile = str(crm_row.get('mobile') or '').strip()
        if not matched_mobile:
            return None
        full_phone_candidates = self._official_group_requester_phone_candidates(requester)
        inferred_area_code = 0
        inferred_country = ''
        for candidate in full_phone_candidates:
            if candidate.endswith(matched_mobile) and len(candidate) > len(matched_mobile):
                prefix = candidate[:-len(matched_mobile)]
                if prefix in PHONE_PREFIX_COUNTRY_MAP:
                    inferred_area_code = int(prefix)
                    inferred_country = PHONE_PREFIX_COUNTRY_MAP[prefix]
                    break
        runtime_mobile = matched_mobile
        for candidate in full_phone_candidates:
            if candidate.endswith(matched_mobile) and len(candidate) > len(matched_mobile):
                runtime_mobile = candidate
                break
        lead_payload = LeadUpsertRequest(
            trace_id=f"official-group-crm-only:{str((requester or {}).get('requesterId') or '').strip() or uuid.uuid4().hex[:12]}",
            source_platform='official_group_crm_only_test',
            source_page_id='official_group_crm_only_test',
            country=inferred_country or 'Unknown',
            area_code=inferred_area_code,
            mobile=runtime_mobile,
            yw_id=str(crm_row.get('ywId') or '').strip() or None,
            app_name=str(crm_row.get('appName') or '').strip() or None,
            dept_name=str(crm_row.get('deptName') or '').strip() or None,
            pendaftaran_group=str(crm_row.get('pendaftaranGroup') or '').strip() or None,
            parser_status='crm_only_test_match',
            parser_version='official_group_crm_only_test_v1',
            parser_raw_text=json.dumps({'requester': requester, 'crm_row': crm_row}, ensure_ascii=False),
            occurred_at=created_at,
        )
        lead_result = self.upsert_lead(lead_payload)
        lead_id = str(lead_result.get('lead_id') or '').strip()
        if not lead_id:
            return None
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE leads
                SET yw_id = ?,
                    app_name = ?,
                    dept_name = ?,
                    pendaftaran_group = ?,
                    matched_customer_id = ?,
                    current_status = 'bind_success',
                    updated_at = ?
                WHERE lead_id = ?
                """,
                (
                    str(crm_row.get('ywId') or '').strip() or None,
                    str(crm_row.get('appName') or '').strip() or None,
                    str(crm_row.get('deptName') or '').strip() or None,
                    str(crm_row.get('pendaftaranGroup') or '').strip() or None,
                    str(crm_row.get('id') or '').strip() or None,
                    created_at,
                    lead_id,
                ),
            )
            self._record_verified_crm_state(conn, lead_id=lead_id, crm_payload=dict(crm_row), official_group=None)
            self._queue_group_join_after_verified_crm(
                conn,
                lead_id=lead_id,
                submission_id=None,
                account_id=str(crm_row.get('ywId') or '').strip() or None,
                created_at=created_at,
            )
            self._record_audit_event(
                conn,
                event_type='official_group_crm_only_test_lead_materialized',
                event_source='official_group_batch_runner',
                payload={
                    'lead_id': lead_id,
                    'target_group': target_group,
                    'requester': requester,
                    'crm_row': crm_row,
                },
                lead_id=lead_id,
            )
            conn.commit()
            lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        if not lead_row:
            return None
        lead_payload = dict(lead_row)
        lead_payload['crm_only_test_target_group'] = target_group
        return lead_payload

    def bind_check_result(self, task_id: str, payload: BindCheckResultRequest) -> Dict[str, Any]:
        now = utc_now()
        with self.db.connect() as conn:
            task = conn.execute("SELECT lead_id, payload, retry_count, created_by FROM automation_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="task not found")
            task_payload = json.loads(task["payload"] or "{}")
            submission_id = task_payload.get("submission_id")
            account_id = task_payload.get("account_id")
            current_retry_count = int(task['retry_count'] or 0)
            attempt_number = current_retry_count + 1
            lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (task['lead_id'],)).fetchone()
            effective_raw_result = dict(payload.raw_result or {})
            effective_status = payload.status
            effective_result_code = payload.result_code
            effective_result_reason = payload.result_reason
            bind_human_action = self._classify_bind_human_action(
                result_code=effective_result_code,
                result_reason=effective_result_reason,
                raw_result=effective_raw_result,
            )
            bind_failure_meta = self._classify_bind_failure(
                result_code=effective_result_code,
                result_reason=effective_result_reason,
                raw_result=effective_raw_result,
            )
            effective_raw_result.update({k: v for k, v in bind_human_action.items() if v is not None})
            effective_raw_result.update({k: v for k, v in bind_failure_meta.items() if v is not None})
            effective_raw_result['attempt_number'] = attempt_number
            effective_raw_result['retry_count'] = current_retry_count
            if payload.status == "success":
                mismatch = self._detect_bind_backend_guild_mismatch(
                    task_payload=task_payload,
                    lead_row=lead_row,
                    raw_result=effective_raw_result,
                )
                if mismatch:
                    effective_status = 'failed'
                    effective_result_code = 'bind_backend_guild_mismatch'
                    effective_result_reason = mismatch['result_reason']
                    effective_raw_result.update(mismatch)
                    bind_failure_meta = self._classify_bind_failure(
                        result_code=effective_result_code,
                        result_reason=effective_result_reason,
                        raw_result=effective_raw_result,
                    )
                    effective_raw_result.update({k: v for k, v in bind_failure_meta.items() if v is not None})
            conn.execute(
                """
                INSERT OR REPLACE INTO bind_check_jobs (
                    job_id, lead_id, submission_id, account_id, guild_code, check_source, status,
                    result_code, result_reason, raw_result, retry_count, scheduled_at, finished_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    task["lead_id"],
                    submission_id,
                    account_id,
                    (effective_raw_result or {}).get("guild_code"),
                    "manual_backend",
                    effective_status,
                    effective_result_code,
                    effective_result_reason,
                    json.dumps(effective_raw_result, ensure_ascii=False),
                    current_retry_count,
                    payload.finished_at,
                    payload.finished_at,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE automation_tasks
                SET status = ?, result_code = ?, result_reason = ?, finished_at = ?, raw_result = ?
                WHERE task_id = ?
                """,
                (
                    effective_status,
                    effective_result_code,
                    effective_result_reason,
                    payload.finished_at,
                    json.dumps(effective_raw_result, ensure_ascii=False),
                    task_id,
                ),
            )
            if effective_status == "success":
                conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("bind_success", now, task["lead_id"]))
                self._record_status_history(
                    conn,
                    lead_id=task["lead_id"],
                    from_status="bind_check_pending",
                    to_status="bind_success",
                    trigger_type="bind_check_success",
                    trigger_source="bind_check_result",
                    trigger_task_id=task_id,
                )
                reply_context = {
                    'source_message_id': str(task_payload.get('source_message_id') or ''),
                    'source_chat_id': str(task_payload.get('source_chat_id') or ''),
                    'source_bot_app_id': str(task_payload.get('source_bot_app_id') or ''),
                }
                crm_sync = self._sync_crm_after_bind_success(
                    conn,
                    lead_id=task['lead_id'],
                    account_id=account_id,
                    task_id=task_id,
                    bind_result_reason=effective_result_reason,
                    bind_raw_result=effective_raw_result,
                    submission_id=submission_id,
                    reply_context=reply_context,
                )
                crm_sync_failed = crm_sync['crm_sync_failed']
                if crm_sync_failed:
                    if crm_sync.get('crm_retry_pending'):
                        scheduled = self._schedule_crm_retry_task(
                            conn,
                            submission_id=str(submission_id or ''),
                            lead_id=task['lead_id'],
                            account_id=str(account_id or ''),
                            bind_result_reason=effective_result_reason or '',
                            bind_raw_result=effective_raw_result,
                            source_payload=reply_context,
                            retry_count=1,
                        )
                        if scheduled:
                            return {
                                "task_id": task_id,
                                "lead_status": "bind_success",
                                "next_action": "queue_crm_sync_retry",
                                "reason": "crm_sync_retry_pending",
                                "result_reason": crm_sync_failed,
                                "group_join_task_type": None,
                                "crm_verified": False,
                                "current_submission_crm_verified": False,
                                "requires_human_action": False,
                                "human_action_type": None,
                                "retry_task_id": scheduled['task_id'],
                                "retry_count": scheduled['retry_count'],
                                "next_retry_at": scheduled['next_retry_at'],
                            }
                    return {
                        "task_id": task_id,
                        "lead_status": "bind_success",
                        "next_action": "retry_crm_sync",
                        "reason": "crm_sync_failed",
                        "result_reason": crm_sync_failed,
                        "group_join_task_type": None,
                        "crm_verified": False,
                        "current_submission_crm_verified": False,
                        "requires_human_action": False,
                        "human_action_type": None,
                    }
                group_join_meta = self._queue_group_join_after_verified_crm(
                    conn,
                    lead_id=task['lead_id'],
                    submission_id=submission_id,
                    account_id=account_id,
                    created_at=payload.finished_at,
                )
                return {
                    "task_id": task_id,
                    "lead_status": "bind_success",
                    "next_action": "queue_group_join",
                    **group_join_meta,
                    "crm_verified": True,
                    "current_submission_crm_verified": True,
                    "requires_human_action": False,
                    "human_action_type": None,
                }
            should_retry_bind = bool(bind_failure_meta.get('retryable')) and current_retry_count < self.bind_retry_max_attempts
            if should_retry_bind:
                retry_meta = self._schedule_bind_retry_task(
                    conn,
                    lead_id=task['lead_id'],
                    source_task_payload=task_payload,
                    source_created_by=str(task['created_by'] or '').strip() or None,
                    retry_count=current_retry_count + 1,
                )
                conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("bind_check_pending", now, task["lead_id"]))
                self._record_status_history(
                    conn,
                    lead_id=task["lead_id"],
                    from_status="bind_check_pending",
                    to_status="bind_check_pending",
                    trigger_type="bind_check_retry_scheduled",
                    trigger_source="bind_check_result",
                    trigger_task_id=retry_meta['task_id'],
                    remark=f"retry {retry_meta['retry_count']}/{self.bind_retry_max_attempts}",
                )
                return {
                    "task_id": task_id,
                    "lead_status": "bind_check_pending",
                    "next_action": "queue_bind_retry",
                    "reason": "bind_retry_pending",
                    "result_code": effective_result_code,
                    "result_reason": effective_result_reason,
                    "group_join_task_type": None,
                    "requires_human_action": False,
                    "human_action_type": None,
                    "bind_failure_category": bind_failure_meta.get('failure_category'),
                    "retry_task_id": retry_meta['task_id'],
                    "retry_count": retry_meta['retry_count'],
                }
            conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("bind_failed", now, task["lead_id"]))
            lead_row = conn.execute("SELECT mobile FROM leads WHERE lead_id = ?", (task['lead_id'],)).fetchone()
            self._queue_operator_notification(
                conn,
                lead_id=task['lead_id'],
                notification_type="bind_check_failed",
                mobile=(lead_row['mobile'] if lead_row else ''),
                yw_id=account_id,
                write_result="failed",
                reason=self._format_operator_bind_failure_reason(
                    failure_meta=bind_failure_meta,
                    raw_reason=effective_result_reason,
                    retried=current_retry_count >= self.bind_retry_max_attempts,
                ),
            )
            self._record_status_history(
                conn,
                lead_id=task["lead_id"],
                from_status="bind_check_pending",
                to_status="bind_failed",
                trigger_type="bind_check_failed",
                trigger_source="bind_check_result",
                trigger_task_id=task_id,
            )
            return {
                "task_id": task_id,
                "lead_status": "bind_failed",
                "next_action": "queue_reengagement",
                "reason": "bind_backend_guild_mismatch" if effective_result_code == "bind_backend_guild_mismatch" else "bind_check_failed",
                "result_reason": effective_result_reason,
                "group_join_task_type": None,
                "requires_human_action": bool(bind_human_action.get('requires_human_action')),
                "human_action_type": bind_human_action.get('human_action_type'),
                "bind_failure_category": bind_failure_meta.get('failure_category'),
            }

    def group_join_result(self, task_id: str, payload: GroupJoinResultRequest) -> Dict[str, Any]:
        now = utc_now()
        with self.db.connect() as conn:
            task = conn.execute("SELECT lead_id, payload FROM automation_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="task not found")
            task_payload = json.loads(task["payload"] or "{}")
            submission_id = task_payload.get("submission_id")
            account_id = task_payload.get("account_id")
            conn.execute(
                """
                INSERT OR REPLACE INTO group_join_jobs (
                    job_id, lead_id, submission_id, account_id, target_group, join_type, status,
                    result_code, result_reason, raw_result, retry_count, scheduled_at, finished_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    task["lead_id"],
                    submission_id,
                    account_id,
                    (payload.raw_result or {}).get("target_group"),
                    "official_group",
                    payload.status,
                    payload.result_code,
                    payload.result_reason,
                    json.dumps(payload.raw_result, ensure_ascii=False),
                    0,
                    payload.finished_at,
                    payload.finished_at,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE automation_tasks
                SET status = ?, result_code = ?, result_reason = ?, finished_at = ?, raw_result = ?
                WHERE task_id = ?
                """,
                (
                    payload.status,
                    payload.result_code,
                    payload.result_reason,
                    payload.finished_at,
                    json.dumps(payload.raw_result, ensure_ascii=False),
                    task_id,
                ),
            )
            if payload.status == "success":
                conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("group_join_success", now, task["lead_id"]))
                crm_sync_status = 'skipped'
                crm_result_reason = None
                if self.crm_adapter is not None:
                    lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (task['lead_id'],)).fetchone()
                    if lead_row:
                        lead_dict = dict(lead_row)
                        existing = self.crm_adapter.find_customer(yw_id=account_id, mobile=lead_dict.get('mobile'))
                        if existing:
                            crm_payload = dict(existing)
                            raw_result = payload.raw_result or {}
                            official_group_display_name = self._resolve_official_group_display_name(
                                target_group=str(raw_result.get('target_group') or '').strip(),
                                raw_result=raw_result,
                            )
                            if official_group_display_name:
                                crm_payload['wa'] = official_group_display_name
                            crm_payload['pendaftaranGroup'] = lead_dict.get('pendaftaran_group') or existing.get('pendaftaranGroup') or ''
                            crm_response = self.crm_adapter.update_customer(crm_payload)
                            verified_row = None
                            official_group_for_verify = official_group_display_name or str(raw_result.get('target_group') or '').strip()
                            if crm_response.get('code') == 0:
                                verified_row = self._find_existing_customer_with_fallback(
                                    yw_id=account_id,
                                    mobile=lead_dict.get('mobile'),
                                    app_name=crm_payload.get('appName'),
                                    dept_name=crm_payload.get('deptName'),
                                    registration_group=crm_payload.get('pendaftaranGroup'),
                                    official_group=official_group_for_verify,
                                )
                            crm_sync_status = 'success' if crm_response.get('code') == 0 and verified_row else 'failed'
                            if crm_response.get('code') != 0:
                                crm_result_reason = self._normalize_crm_failure_reason(crm_response, fallback_found=False)
                            elif not verified_row:
                                crm_result_reason = 'CRM write could not be verified.'
                            else:
                                self._record_verified_crm_state(
                                    conn,
                                    lead_id=task['lead_id'],
                                    crm_payload=crm_payload,
                                    official_group=crm_payload.get('wa'),
                                )
                            self._record_sync_log(
                                conn,
                                lead_id=task['lead_id'],
                                task_id=task_id,
                                sync_type='official_group_update',
                                target_system='crm',
                                status=crm_sync_status,
                                request_snapshot=crm_payload,
                                response_snapshot={
                                    'action': 'update',
                                    'crm_response': crm_response,
                                    'verified_after_write': bool(verified_row),
                                },
                            )
                self._record_status_history(
                    conn,
                    lead_id=task["lead_id"],
                    from_status="group_join_pending",
                    to_status="group_join_success",
                    trigger_type="group_join_success",
                    trigger_source="group_join_result",
                    trigger_task_id=task_id,
                )
                return {
                    "task_id": task_id,
                    "lead_status": "group_join_success",
                    "next_action": "close_or_education",
                    "crm_sync_status": crm_sync_status,
                    "crm_result_reason": crm_result_reason,
                    "crm_verified": crm_sync_status == 'success',
                    "current_submission_crm_verified": crm_sync_status == 'success',
                }
            conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("group_join_failed", now, task["lead_id"]))
            self._record_status_history(
                conn,
                lead_id=task["lead_id"],
                from_status="group_join_pending",
                to_status="group_join_failed",
                trigger_type="group_join_failed",
                trigger_source="group_join_result",
                trigger_task_id=task_id,
            )
            return {
                "task_id": task_id,
                "lead_status": "group_join_failed",
                "next_action": "queue_reengagement",
            }

    def ops_manual_review_queue(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                """
                SELECT l.lead_id, l.mobile, l.area_code, l.yw_id, l.app_name, l.dept_name, l.pendaftaran_group,
                       l.current_status, l.updated_at, l.parser_confidence, l.parser_status,
                       l.review_reason_codes, l.routing_decision, l.recommended_next_action,
                       l.parser_raw_ocr_text,
                       (SELECT t.task_id FROM automation_tasks t
                         WHERE t.lead_id = l.lead_id AND t.task_type = 'manual_review'
                         ORDER BY t.created_at DESC LIMIT 1) AS task_id
                FROM leads l
                WHERE l.current_status = 'manual_review_pending'
                ORDER BY l.updated_at DESC
                """
            ).fetchall()]
            for row in rows:
                row['review_reason_codes'] = json.loads(row.get('review_reason_codes') or '[]')
                recognition_codes = {}
                latest_submission = conn.execute(
                    "SELECT recognition_raw FROM account_submissions WHERE lead_id = ? ORDER BY created_at DESC LIMIT 1",
                    (row['lead_id'],),
                ).fetchone()
                if latest_submission and latest_submission['recognition_raw']:
                    recognition_raw = json.loads(latest_submission['recognition_raw'] or '{}')
                    recognition_codes = recognition_raw.get('normalized') or recognition_raw
                elif row.get('parser_raw_ocr_text'):
                    recognition_codes = normalize_native_ocr_fields(row['parser_raw_ocr_text'])
                row['person_code'] = recognition_codes.get('person_code')
                row['guild_invite_code'] = recognition_codes.get('guild_invite_code')
            return {'rows': rows}

    def resolve_manual_review(self, lead_id: str, payload: ManualReviewResolveRequest) -> Dict[str, Any]:
        if payload.decision not in {'approve_bind', 'reject_submission', 'request_recognition_retry'}:
            raise HTTPException(status_code=400, detail='unsupported decision')
        with self.db.connect() as conn:
            lead = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail='lead not found')
            lead_dict = dict(lead)
            if lead_dict.get('current_status') != 'manual_review_pending':
                raise HTTPException(status_code=400, detail='lead is not pending manual review')
            latest_review_task = conn.execute(
                "SELECT task_id, payload FROM automation_tasks WHERE lead_id = ? AND task_type = 'manual_review' ORDER BY created_at DESC LIMIT 1",
                (lead_id,),
            ).fetchone()
            review_id = create_id('review')
            correction_count = 0
            snapshot_before = {
                'yw_id': lead_dict.get('yw_id'),
                'app_name': lead_dict.get('app_name'),
                'dept_name': lead_dict.get('dept_name'),
                'registration_group': lead_dict.get('pendaftaran_group'),
                'parser_status': lead_dict.get('parser_status'),
                'routing_decision': lead_dict.get('routing_decision'),
            }
            updates = {
                'yw_id': payload.account_id or lead_dict.get('yw_id'),
                'app_name': payload.app_name or lead_dict.get('app_name'),
                'dept_name': payload.dept_name or lead_dict.get('dept_name'),
                'pendaftaran_group': payload.registration_group or lead_dict.get('pendaftaran_group'),
            }
            for field_name, old_value, new_value in [
                ('account_id', lead_dict.get('yw_id'), updates['yw_id']),
                ('app_name', lead_dict.get('app_name'), updates['app_name']),
                ('dept_name', lead_dict.get('dept_name'), updates['dept_name']),
                ('registration_group', lead_dict.get('pendaftaran_group'), updates['pendaftaran_group']),
            ]:
                if (old_value or '') != (new_value or '') and new_value is not None:
                    correction_count += 1
                    conn.execute(
                        """
                        INSERT INTO lead_corrections (
                            correction_id, lead_id, field_name, old_value, new_value, corrected_by, review_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (create_id('corr'), lead_id, field_name, old_value, new_value, payload.reviewed_by, review_id, utc_now()),
                    )
            if correction_count == 0 and lead_dict.get('parser_status') == 'conflict' and payload.account_id:
                correction_count += 1
                conn.execute(
                    """
                    INSERT INTO lead_corrections (
                        correction_id, lead_id, field_name, old_value, new_value, corrected_by, review_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        create_id('corr'),
                        lead_id,
                        'account_id',
                        'conflict_resolved',
                        payload.account_id,
                        payload.reviewed_by,
                        review_id,
                        utc_now(),
                    ),
                )
            review_task_payload = json.loads((latest_review_task['payload'] if latest_review_task else '{}') or '{}')
            created_task_id = None
            next_action = 'manual_followup'
            review_status = 'rejected'
            if payload.decision == 'approve_bind':
                account_id = updates['yw_id']
                if not str(account_id or '').isdigit():
                    raise HTTPException(status_code=400, detail='account_id is required for approve_bind')
                conn.execute(
                    """
                    UPDATE leads
                    SET yw_id = ?, app_name = ?, dept_name = ?, pendaftaran_group = ?, parser_status = ?,
                        routing_decision = ?, recommended_next_action = ?, review_status = ?, review_notes = ?,
                        reviewed_by = ?, reviewed_at = ?, correction_count = correction_count + ?, updated_at = ?
                    WHERE lead_id = ?
                    """,
                    (
                        updates['yw_id'], updates['app_name'], updates['dept_name'], updates['pendaftaran_group'], 'reviewed_ready',
                        'queue_bind_check', 'queue_bind_check', 'approved', payload.review_note,
                        payload.reviewed_by, payload.submitted_at, correction_count, utc_now(), lead_id,
                    ),
                )
                created = self.submit_account(
                    AccountSubmissionRequest(
                        lead_id=lead_id,
                        submission_type='account_id',
                        account_id=str(account_id),
                        account_id_type='platform_uid',
                        source_channel='manual_review',
                        submitted_by=payload.reviewed_by,
                        submitted_at=payload.submitted_at,
                        remark=payload.review_note,
                    )
                )
                created_task_id = created['task_id']
                next_action = created['next_action']
                review_status = 'approved'
            elif payload.decision == 'request_recognition_retry':
                conn.execute(
                    """
                    UPDATE leads
                    SET parser_status = ?, routing_decision = ?, recommended_next_action = ?, review_status = ?,
                        review_notes = ?, reviewed_by = ?, reviewed_at = ?, updated_at = ?
                    WHERE lead_id = ?
                    """,
                    (
                        'needs_recognition',
                        'queue_account_recognition',
                        'queue_account_recognition',
                        'retry_requested',
                        payload.review_note,
                        payload.reviewed_by,
                        payload.submitted_at,
                        utc_now(),
                        lead_id,
                    ),
                )
                created = self.submit_account(
                    AccountSubmissionRequest(
                        lead_id=lead_id,
                        submission_type='screenshot',
                        file_url=review_task_payload.get('file_url'),
                        file_type=review_task_payload.get('file_type'),
                        source_channel='manual_review_retry',
                        submitted_by=payload.reviewed_by,
                        submitted_at=payload.submitted_at,
                        remark=payload.review_note,
                    )
                )
                created_task_id = created['task_id']
                next_action = created['next_action']
                review_status = 'retry_requested'
            else:
                conn.execute(
                    """
                    UPDATE leads
                    SET parser_status = ?, routing_decision = ?, recommended_next_action = ?, review_status = ?,
                        review_notes = ?, reviewed_by = ?, reviewed_at = ?, updated_at = ?
                    WHERE lead_id = ?
                    """,
                    ('rejected', 'manual_followup', 'manual_followup', 'rejected', payload.review_note, payload.reviewed_by, payload.submitted_at, utc_now(), lead_id),
                )
                conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ('re_engage_pending', utc_now(), lead_id))
                self._record_status_history(
                    conn,
                    lead_id=lead_id,
                    from_status='manual_review_pending',
                    to_status='re_engage_pending',
                    trigger_type='manual_review_rejected',
                    trigger_source='ops_manual_review',
                    trigger_task_id=latest_review_task['task_id'] if latest_review_task else None,
                    operator_name=payload.reviewed_by,
                    remark=payload.review_note,
                )
            if latest_review_task:
                conn.execute(
                    "UPDATE automation_tasks SET status = ?, result_code = ?, result_reason = ?, finished_at = ?, raw_result = ? WHERE task_id = ?",
                    ('success', payload.decision, payload.review_note, payload.submitted_at, json.dumps({'decision': payload.decision}, ensure_ascii=False), latest_review_task['task_id']),
                )
            snapshot_after = {
                'yw_id': updates['yw_id'],
                'app_name': updates['app_name'],
                'dept_name': updates['dept_name'],
                'registration_group': updates['pendaftaran_group'],
                'decision': payload.decision,
                'next_action': next_action,
            }
            conn.execute(
                """
                INSERT INTO manual_review_history (
                    review_id, lead_id, decision, reviewed_by, review_note, snapshot_before, snapshot_after,
                    created_task_id, submitted_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    lead_id,
                    payload.decision,
                    payload.reviewed_by,
                    payload.review_note,
                    json.dumps(snapshot_before, ensure_ascii=False),
                    json.dumps(snapshot_after, ensure_ascii=False),
                    created_task_id,
                    payload.submitted_at,
                    utc_now(),
                ),
            )
            return {
                'accepted': True,
                'lead_id': lead_id,
                'decision': payload.decision,
                'task_id': created_task_id,
                'next_action': next_action,
                'correction_count': correction_count,
                'review_status': review_status,
            }

    def parser_quality_summary(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            manual_review_count = conn.execute("SELECT COUNT(*) FROM leads WHERE review_status IN ('pending','approved','rejected')").fetchone()[0]
            approved_review_count = conn.execute("SELECT COUNT(*) FROM leads WHERE review_status = 'approved'").fetchone()[0]
            parser_conflict_count = conn.execute("SELECT COUNT(*) FROM leads WHERE parser_status = 'conflict' OR review_reason_codes LIKE '%account_id_conflict%'").fetchone()[0]
            low_confidence_count = conn.execute("SELECT COUNT(*) FROM leads WHERE parser_status = 'low_confidence'").fetchone()[0]
            correction_count = conn.execute("SELECT COUNT(*) FROM lead_corrections").fetchone()[0]
            return {
                'manual_review_count': manual_review_count,
                'approved_review_count': approved_review_count,
                'parser_conflict_count': parser_conflict_count,
                'low_confidence_count': low_confidence_count,
                'correction_count': correction_count,
            }

    def lead_timeline(self, lead_id: str) -> Dict[str, Any]:
        with self.db.connect() as conn:
            lead = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail="lead not found")
            lead_dict = dict(lead)
            lead_dict['parser_missing_fields'] = json.loads(lead_dict.get('parser_missing_fields') or '[]')
            lead_dict['parser_conflicts'] = json.loads(lead_dict.get('parser_conflicts') or '[]')
            lead_dict['review_reason_codes'] = json.loads(lead_dict.get('review_reason_codes') or '[]')
            lead_dict['crm_verified_payload'] = json.loads(lead_dict.get('crm_verified_payload') or 'null')
            events = [dict(row) for row in conn.execute("SELECT * FROM lead_events WHERE lead_id = ? ORDER BY created_at ASC", (lead_id,)).fetchall()]
            tasks = [dict(row) for row in conn.execute("SELECT * FROM automation_tasks WHERE lead_id = ? ORDER BY created_at ASC", (lead_id,)).fetchall()]
            sync_logs = [dict(row) for row in conn.execute("SELECT * FROM sync_logs WHERE lead_id = ? ORDER BY created_at ASC", (lead_id,)).fetchall()]
            submissions = [dict(row) for row in conn.execute("SELECT * FROM account_submissions WHERE lead_id = ? ORDER BY created_at ASC", (lead_id,)).fetchall()]
            status_history = [dict(row) for row in conn.execute("SELECT * FROM lead_status_history WHERE lead_id = ? ORDER BY created_at ASC", (lead_id,)).fetchall()]
            review_history = [dict(row) for row in conn.execute("SELECT * FROM manual_review_history WHERE lead_id = ? ORDER BY created_at ASC", (lead_id,)).fetchall()]
            correction_history = [dict(row) for row in conn.execute("SELECT * FROM lead_corrections WHERE lead_id = ? ORDER BY created_at ASC", (lead_id,)).fetchall()]
            for task in tasks:
                task['payload'] = json.loads(task.get('payload') or '{}')
                task['raw_result'] = json.loads(task.get('raw_result') or '{}')
            for submission in submissions:
                submission['recognition_raw'] = json.loads(submission.get('recognition_raw') or '{}')
            return {
                "lead": lead_dict,
                "events": events,
                "tasks": tasks,
                "sync_logs": sync_logs,
                "account_submissions": submissions,
                "status_history": status_history,
                "review_history": review_history,
                "correction_history": correction_history,
            }

    def funnel_report(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    source_platform,
                    COALESCE(source_campaign, '') AS source_campaign,
                    country,
                    COUNT(*) AS lead_count,
                    SUM(CASE WHEN current_status NOT IN ('new') THEN 1 ELSE 0 END) AS engaged_count,
                    SUM(CASE WHEN current_status IN ('account_submitted','bind_check_pending','bind_success','bind_failed','group_join_pending','group_join_success','group_join_failed','re_engage_pending','closed','synced') THEN 1 ELSE 0 END) AS account_submitted_count,
                    SUM(CASE WHEN current_status IN ('bind_success','group_join_pending','group_join_success','group_join_failed','closed','synced') THEN 1 ELSE 0 END) AS bind_success_count,
                    SUM(CASE WHEN current_status IN ('group_join_success','closed','synced') THEN 1 ELSE 0 END) AS group_join_success_count
                FROM leads
                GROUP BY source_platform, COALESCE(source_campaign, ''), country
                ORDER BY source_platform, source_campaign, country
                """
            ).fetchall()
            return {"rows": [dict(r) for r in rows]}

    def attach_voucher_for_lead(self, lead_id: str, image_path: str, remark_suffix: Optional[str] = None) -> Dict[str, Any]:
        if self.crm_adapter is None:
            raise HTTPException(status_code=400, detail='crm adapter not configured')
        with self.db.connect() as conn:
            lead = conn.execute("SELECT lead_id, mobile, area_code, yw_id FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail='lead not found')
            lead_dict = dict(lead)
            existing = self.crm_adapter.find_customer(yw_id=lead_dict.get('yw_id'), mobile=lead_dict.get('mobile'))
            if not existing:
                raise HTTPException(status_code=404, detail='crm customer not found for lead')
            image_url = self.crm_adapter.upload_voucher(customer_id=str(existing['id']), image_path=image_path)
            self.crm_adapter.attach_voucher(existing, image_url, remark_suffix=remark_suffix)
            return {'lead_id': lead_id, 'crm_customer_id': existing['id'], 'image_url': image_url, 'attached': True}

    def _resolve_registration_group_display_name(
        self,
        *,
        registration_group: str,
        raw_result: Optional[Dict[str, Any]] = None,
        expected_group_state: Optional[Dict[str, Any]] = None,
        current_group_state: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        for source in (raw_result, current_group_state, expected_group_state):
            if not isinstance(source, dict):
                continue
            for key in ('group_name', 'registration_group_name'):
                value = str(source.get(key) or '').strip()
                if value:
                    return value
        value = str(registration_group or '').strip()
        return value or None

    def _resolve_official_group_display_name(
        self,
        *,
        target_group: str,
        raw_result: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        if isinstance(raw_result, dict):
            for key in ('group_name', 'official_group_name'):
                value = str(raw_result.get(key) or '').strip()
                if value:
                    return value
            nested_raw_result = raw_result.get('raw_result')
            if isinstance(nested_raw_result, dict):
                for key in ('group_name', 'official_group_name'):
                    value = str(nested_raw_result.get(key) or '').strip()
                    if value:
                        return value
        binding_match = self._find_whatsapp_approval_account_binding(
            responsible_type='official_group',
            target_group=target_group,
        )
        binding = binding_match.get('binding') if isinstance(binding_match, dict) else {}
        if isinstance(binding, dict):
            value = str(binding.get('group_name') or '').strip()
            if value:
                return value
        return None

    def _official_group_target_aliases(self, *, target_group: str) -> set[str]:
        aliases: set[str] = set()
        normalized_target = str(target_group or '').strip()
        if normalized_target:
            aliases.add(normalized_target.lower())
        binding_match = self._find_whatsapp_approval_account_binding(
            responsible_type='official_group',
            target_group=normalized_target,
        )
        binding = binding_match.get('binding') if isinstance(binding_match, dict) else {}
        if isinstance(binding, dict):
            for key in ('group_name', 'registration_group', 'group_id', 'link'):
                value = str(binding.get(key) or '').strip().lower()
                if value:
                    aliases.add(value)
        aliases.discard('')
        return aliases

    def _official_group_value_matches_target(self, *, value: Any, target_group: str) -> bool:
        normalized_value = str(value or '').strip().lower()
        if not normalized_value:
            return False
        if normalized_value in self._official_group_target_aliases(target_group=target_group):
            return True
        value_binding_match = self._find_whatsapp_approval_account_binding(
            responsible_type='official_group',
            target_group=normalized_value,
        )
        value_binding = value_binding_match.get('binding') if isinstance(value_binding_match, dict) else {}
        if isinstance(value_binding, dict):
            value_aliases = {
                str(value_binding.get('group_name') or '').strip().lower(),
                str(value_binding.get('registration_group') or '').strip().lower(),
                str(value_binding.get('group_id') or '').strip().lower(),
                str(value_binding.get('link') or '').strip().lower(),
            }
            value_aliases.discard('')
            if str(target_group or '').strip().lower() in value_aliases:
                return True
        return False

    def create_registration_group_approval_batch(self, payload: RegistrationGroupApprovalBatchRequest) -> Dict[str, Any]:
        if self.crm_adapter is None:
            raise HTTPException(status_code=400, detail='crm adapter not configured')
        resolved_group_no = str(payload.registration_group_name or '').strip() or str(payload.registration_group or '').strip()
        request_snapshot = {
            'registration_group': payload.registration_group,
            'registration_group_name': payload.registration_group_name,
            'approved_count': payload.approved_count,
            'approved_by': payload.approved_by,
            'approved_by_name': payload.approved_by_name,
            'source_platform': payload.source_platform,
            'source_campaign': payload.source_campaign,
            'source_adset': payload.source_adset,
            'source_ad': payload.source_ad,
            'approved_at': payload.approved_at,
            'area': payload.area,
            'remark': payload.remark,
            'approval_run_id': payload.approval_run_id,
        }
        normalized_run_id = str(payload.approval_run_id or '').strip()
        crm_payload = {
            'area': payload.area,
            'groupNo': resolved_group_no,
            'groupPeopleNum': str(payload.approved_count),
        }
        request_snapshot_with_payload = {
            **request_snapshot,
            'crm_payload': crm_payload,
        }
        with self._registration_group_approval_batch_lock:
            if normalized_run_id:
                claimed = self._claim_registration_group_approval_batch_run(normalized_run_id, request_snapshot_with_payload)
                if not claimed.get('claimed'):
                    existing = dict(claimed.get('row') or {})
                    if str(existing.get('status') or '').strip() == 'processing':
                        existing = self._wait_for_registration_group_approval_batch_run(normalized_run_id) or existing
                    if existing:
                        return self._build_registration_group_approval_batch_existing_response(
                            existing,
                            request_snapshot=request_snapshot,
                            fallback_crm_payload=crm_payload,
                        )
            started = time.perf_counter()
            try:
                crm_response = self.crm_adapter.create_registration_group_batch(crm_payload)
            except Exception as exc:
                crm_response = {
                    'code': -1,
                    'msg': str(exc),
                    'error_type': type(exc).__name__,
                }
            elapsed_seconds = round(time.perf_counter() - started, 3)
            sync_status = 'success' if crm_response.get('code') == 0 else 'failed'
            sync_log_id = create_id('sync')
            now = utc_now()
            with self.db.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute(
                    "INSERT INTO sync_logs (sync_log_id, lead_id, task_id, sync_type, target_system, status, request_snapshot, response_snapshot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sync_log_id,
                        None,
                        None,
                        'registration_group_approval_batch',
                        'crm',
                        sync_status,
                        json.dumps(request_snapshot_with_payload, ensure_ascii=False),
                        json.dumps(crm_response, ensure_ascii=False),
                        now,
                    ),
                )
                if normalized_run_id:
                    conn.execute(
                        "UPDATE registration_group_approval_batch_runs SET sync_log_id = ?, status = ?, request_snapshot = ?, response_snapshot = ?, updated_at = ? WHERE approval_run_id = ?",
                        (
                            sync_log_id,
                            sync_status,
                            json.dumps(request_snapshot_with_payload, ensure_ascii=False),
                            json.dumps(crm_response, ensure_ascii=False),
                            now,
                            normalized_run_id,
                        ),
                    )
                conn.commit()
        return {
            'accepted': True,
            'crm_sync_status': sync_status,
            'crm_payload': crm_payload,
            'crm_response': crm_response,
            'approval_run_id': payload.approval_run_id,
            'request_snapshot': request_snapshot,
            'elapsed_seconds': elapsed_seconds,
        }

    def _build_registration_group_approval_batch_existing_response(
        self,
        existing: Dict[str, Any],
        *,
        request_snapshot: Dict[str, Any],
        fallback_crm_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        existing_request = dict(existing.get('request_snapshot_dict') or {})
        existing_response = dict(existing.get('response_snapshot_dict') or {})
        existing_status = str(existing.get('status') or 'failed').strip() or 'failed'
        return {
            'accepted': True,
            'crm_sync_status': 'processing' if existing_status == 'processing' else ('success' if existing_status == 'success' else 'failed'),
            'crm_payload': dict(existing_request.get('crm_payload') or fallback_crm_payload),
            'crm_response': existing_response,
            'approval_run_id': str(existing.get('approval_run_id') or existing_request.get('approval_run_id') or '').strip() or None,
            'request_snapshot': {k: existing_request.get(k) for k in request_snapshot.keys()},
            'elapsed_seconds': 0.0,
            'duplicate': True,
        }

    def _claim_registration_group_approval_batch_run(self, approval_run_id: str, request_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        normalized_run_id = str(approval_run_id or '').strip()
        if not normalized_run_id:
            return {'claimed': True}
        now = utc_now()
        serialized_request = json.dumps(request_snapshot, ensure_ascii=False)
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                "SELECT approval_run_id, sync_log_id, status, request_snapshot, response_snapshot, created_at, updated_at FROM registration_group_approval_batch_runs WHERE approval_run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO registration_group_approval_batch_runs (approval_run_id, sync_log_id, status, request_snapshot, response_snapshot, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (normalized_run_id, None, 'processing', serialized_request, json.dumps({}, ensure_ascii=False), now, now),
                )
                conn.commit()
                return {'claimed': True}
            row_dict = dict(row)
            status = str(row_dict.get('status') or '').strip()
            if status == 'failed':
                cursor = conn.execute(
                    "UPDATE registration_group_approval_batch_runs SET sync_log_id = NULL, status = 'processing', request_snapshot = ?, response_snapshot = ?, updated_at = ? WHERE approval_run_id = ? AND status = 'failed'",
                    (serialized_request, json.dumps({}, ensure_ascii=False), now, normalized_run_id),
                )
                if cursor.rowcount > 0:
                    conn.commit()
                    return {'claimed': True}
                row = conn.execute(
                    "SELECT approval_run_id, sync_log_id, status, request_snapshot, response_snapshot, created_at, updated_at FROM registration_group_approval_batch_runs WHERE approval_run_id = ?",
                    (normalized_run_id,),
                ).fetchone()
                row_dict = dict(row) if row else row_dict
            conn.commit()
        return {'claimed': False, 'row': self._deserialize_registration_group_approval_batch_run_row(row_dict)}

    def _wait_for_registration_group_approval_batch_run(self, approval_run_id: str, *, timeout_seconds: float = 5.0, poll_interval_seconds: float = 0.05) -> Optional[Dict[str, Any]]:
        deadline = time.perf_counter() + max(0.1, float(timeout_seconds or 0.0))
        while time.perf_counter() < deadline:
            row = self._find_registration_group_approval_batch_sync_log(approval_run_id)
            if row is None:
                time.sleep(poll_interval_seconds)
                continue
            if str(row.get('status') or '').strip() != 'processing':
                return row
            time.sleep(poll_interval_seconds)
        return self._find_registration_group_approval_batch_sync_log(approval_run_id)

    def _deserialize_registration_group_approval_batch_run_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(row or {})
        try:
            normalized['request_snapshot_dict'] = json.loads(normalized.get('request_snapshot') or '{}')
        except Exception:
            normalized['request_snapshot_dict'] = {}
        try:
            normalized['response_snapshot_dict'] = json.loads(normalized.get('response_snapshot') or '{}')
        except Exception:
            normalized['response_snapshot_dict'] = {}
        return normalized

    def _find_registration_group_approval_batch_sync_log(self, approval_run_id: str) -> Optional[Dict[str, Any]]:
        normalized_run_id = str(approval_run_id or '').strip()
        if not normalized_run_id:
            return None
        with self.db.connect() as conn:
            batch_row = conn.execute(
                "SELECT approval_run_id, sync_log_id, status, request_snapshot, response_snapshot, created_at, updated_at FROM registration_group_approval_batch_runs WHERE approval_run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            if batch_row:
                row = dict(batch_row)
                try:
                    row['request_snapshot_dict'] = json.loads(row.get('request_snapshot') or '{}')
                except Exception:
                    row['request_snapshot_dict'] = {}
                try:
                    row['response_snapshot_dict'] = json.loads(row.get('response_snapshot') or '{}')
                except Exception:
                    row['response_snapshot_dict'] = {}
                return row
            rows = [dict(r) for r in conn.execute(
                "SELECT sync_log_id, status, request_snapshot, response_snapshot, created_at FROM sync_logs WHERE sync_type = 'registration_group_approval_batch' ORDER BY created_at DESC LIMIT 500"
            ).fetchall()]
        for row in rows:
            try:
                request_snapshot = json.loads(row.get('request_snapshot') or '{}')
            except Exception:
                request_snapshot = {}
            if str(request_snapshot.get('approval_run_id') or '').strip() != normalized_run_id:
                continue
            try:
                response_snapshot = json.loads(row.get('response_snapshot') or '{}')
            except Exception:
                response_snapshot = {}
            row['request_snapshot_dict'] = request_snapshot
            row['response_snapshot_dict'] = response_snapshot
            return row
        return None

    def _find_registration_group_approval_ingress_event(self, approval_run_id: str) -> Optional[Dict[str, Any]]:
        normalized_run_id = str(approval_run_id or '').strip()
        if not normalized_run_id:
            return None
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT event_id, ingress_type, status, payload, result_snapshot, created_at, updated_at, processed_at FROM ingress_events WHERE ingress_type = 'registration_group_approval_decision' ORDER BY created_at DESC LIMIT 500"
            ).fetchall()]
        for row in rows:
            try:
                payload = json.loads(row.get('payload') or '{}')
            except Exception:
                payload = {}
            try:
                result_snapshot = json.loads(row.get('result_snapshot') or '{}')
            except Exception:
                result_snapshot = {}
            if str(payload.get('approval_run_id') or result_snapshot.get('approval_run_id') or '').strip() != normalized_run_id:
                continue
            row['payload_dict'] = payload
            row['result_snapshot_dict'] = result_snapshot
            return row
        return None

    def registration_group_approval_decision_status(self, approval_run_id: str) -> Dict[str, Any]:
        row = self._find_registration_group_approval_ingress_event(approval_run_id)
        if row is None:
            raise HTTPException(status_code=404, detail='registration group approval run not found')
        result = dict(row.get('result_snapshot_dict') or {})
        batch_sync = self._find_registration_group_approval_batch_sync_log(approval_run_id)
        if result and str(result.get('crm_recorded')).strip().lower() != 'true' and batch_sync:
            batch_status = str(batch_sync.get('status') or '').strip().lower()
            batch_response = dict(batch_sync.get('response_snapshot_dict') or {})
            batch_request = dict(batch_sync.get('request_snapshot_dict') or {})
            if batch_status == 'success':
                result['crm_recorded'] = True
                raw_result = dict(result.get('raw_result') or {})
                crm_batch = dict(raw_result.get('crm_batch') or {})
                crm_batch['accepted'] = True
                crm_batch['crm_sync_status'] = 'success'
                crm_batch['crm_payload'] = dict(batch_request.get('crm_payload') or crm_batch.get('crm_payload') or {})
                crm_batch['crm_response'] = batch_response
                crm_batch['approval_run_id'] = str(batch_request.get('approval_run_id') or approval_run_id).strip() or approval_run_id
                crm_batch['request_snapshot'] = {
                    'registration_group': batch_request.get('registration_group'),
                    'registration_group_name': batch_request.get('registration_group_name'),
                    'approved_count': batch_request.get('approved_count'),
                    'approved_by': batch_request.get('approved_by'),
                    'approved_by_name': batch_request.get('approved_by_name'),
                    'source_platform': batch_request.get('source_platform'),
                    'source_campaign': batch_request.get('source_campaign'),
                    'source_adset': batch_request.get('source_adset'),
                    'source_ad': batch_request.get('source_ad'),
                    'approved_at': batch_request.get('approved_at'),
                    'area': batch_request.get('area'),
                    'remark': batch_request.get('remark'),
                    'approval_run_id': batch_request.get('approval_run_id'),
                }
                raw_result['crm_batch'] = crm_batch
                result['raw_result'] = raw_result
                result['crm_batch'] = crm_batch
        return {
            'approval_run_id': approval_run_id,
            'ingress_event_id': row['event_id'],
            'status': row['status'],
            'created_at': row.get('created_at'),
            'updated_at': row.get('updated_at'),
            'processed_at': row.get('processed_at'),
            'result': result,
        }

    def _registration_group_active_monitor_target_health(self) -> Optional[Dict[str, Any]]:
        try:
            production_ops = self.get_production_ops_daemon_config() or {}
        except Exception:
            return None
        runtime = dict(production_ops.get('runtime') or {})
        status = dict(runtime.get('status') or {})
        monitor_target = dict(status.get('monitor_target') or {})
        if str(monitor_target.get('source') or '').strip() != 'account_binding':
            return None
        base_url = str(monitor_target.get('worker_base_url') or '').strip().rstrip('/')
        if not base_url:
            return None
        try:
            health = self._request_whatsapp_approval_worker_health(base_url)
        except Exception:
            return None
        if not isinstance(health, dict) or not health:
            return None
        normalized = dict(health)
        normalized.setdefault('configured', True)
        normalized.setdefault('provider', 'whatsapp_webjs_bridge')
        normalized.setdefault('base_url', base_url)
        normalized.setdefault('supports', normalized.get('supports') or ['approve', 'strict_queue_and_member_verify', 'crm_batch_writeback_ready'])
        normalized['routed_via'] = 'production_ops_monitor_target'
        normalized['monitor_target'] = {
            'account_key': str(monitor_target.get('account_key') or '').strip() or None,
            'registration_group': str(monitor_target.get('registration_group') or '').strip() or None,
            'worker_base_url': base_url,
            'source': 'account_binding',
        }
        return normalized

    def registration_group_approval_executor_health(self) -> Dict[str, Any]:
        executor = self.registration_group_approval_executor
        prefers_active_runtime = (
            executor is None
            or type(executor).__name__ == 'WebjsBridgeRegistrationGroupApprovalExecutor'
            or str(getattr(executor, 'base_url', '') or '').strip().startswith('http://127.0.0.1:8787')
        )
        if prefers_active_runtime:
            routed_health = self._registration_group_active_monitor_target_health()
            if routed_health:
                supports = routed_health.get('supports')
                if supports is None:
                    routed_health['supports'] = []
                return routed_health
        if executor is None:
            return {
                'configured': False,
                'status': 'unconfigured',
                'provider': None,
                'supports': [],
            }
        if hasattr(executor, 'health') and callable(getattr(executor, 'health')):
            try:
                health = executor.health() or {}
                if isinstance(health, dict):
                    supports = health.get('supports')
                    if supports is None:
                        health['supports'] = []
                    return health
            except Exception as exc:
                return {
                    'configured': True,
                    'status': 'error',
                    'provider': type(executor).__name__,
                    'supports': [],
                    'error': str(exc),
                }
        return {
            'configured': True,
            'status': 'configured',
            'provider': type(executor).__name__,
            'supports': [],
        }

    def registration_group_approval_executor_warmup(self) -> Dict[str, Any]:
        executor = self.registration_group_approval_executor
        if executor is None:
            return {
                'configured': False,
                'status': 'unconfigured',
                'provider': None,
                'supports': [],
                'warmed': False,
            }
        if hasattr(executor, 'warmup') and callable(getattr(executor, 'warmup')):
            try:
                result = executor.warmup() or {}
                if isinstance(result, dict):
                    result.setdefault('warmed', bool(result.get('status') == 'warm'))
                    result.setdefault('supports', result.get('supports') or [])
                    return result
            except Exception as exc:
                return {
                    'configured': True,
                    'status': 'error',
                    'provider': type(executor).__name__,
                    'supports': [],
                    'warmed': False,
                    'error': str(exc),
                }
        health = self.registration_group_approval_executor_health()
        health['warmed'] = False
        health['warmup_supported'] = False
        return health

    def registration_group_approval_executor_group_state(self, registration_group: str) -> Dict[str, Any]:
        normalized_group = str(registration_group or '').strip()
        if not normalized_group:
            raise HTTPException(status_code=400, detail='registration_group is required')
        routed_runtime = self._resolve_whatsapp_approval_runtime_executor(target_group=normalized_group, responsible_type='registration_group')
        executor = (routed_runtime or {}).get('executor') or self.registration_group_approval_executor
        if executor is None:
            return {
                'configured': False,
                'status': 'unconfigured',
                'provider': None,
                'group_name': normalized_group,
                'group_id': None,
                'pending_count': None,
                'member_count': None,
                'requester_ids': [],
            }
        if hasattr(executor, 'group_state') and callable(getattr(executor, 'group_state')):
            result = executor.group_state(normalized_group) or {}
            if not isinstance(result, dict):
                raise HTTPException(status_code=500, detail='registration group approval executor group_state must return dict result')
            normalized = dict(result)
            normalized.setdefault('configured', True)
            normalized.setdefault('group_name', normalized_group)
            normalized.setdefault('group_id', None)
            normalized.setdefault('pending_count', None)
            normalized.setdefault('member_count', None)
            normalized.setdefault('requester_ids', [])
            if routed_runtime:
                normalized['routed_runtime'] = {
                    'account_key': routed_runtime.get('account_key'),
                    'account_name': routed_runtime.get('account_name'),
                    'base_url': (routed_runtime.get('runtime_state') or {}).get('base_url'),
                }
            return normalized
        raise HTTPException(status_code=400, detail='registration group approval executor group_state not supported')

    def _registration_group_approval_evidence_summary(self, result: Dict[str, Any]) -> Dict[str, Any]:
        raw_result = dict((result or {}).get('raw_result') or {})
        pending_before = raw_result.get('pending_before')
        pending_after = raw_result.get('pending_after')
        member_count_before = raw_result.get('member_count_before')
        member_count_after = raw_result.get('member_count_after')
        queue_delta = bool(result.get('queue_delta'))
        if not queue_delta and pending_before is not None and pending_after is not None:
            try:
                queue_delta = int(pending_after) < int(pending_before)
            except Exception:
                queue_delta = False
        member_count_delta = None
        if member_count_before is not None and member_count_after is not None:
            try:
                member_count_delta = int(member_count_after) - int(member_count_before)
            except Exception:
                member_count_delta = None
        member_confirmed = bool(result.get('member_confirmed'))
        target_member = dict((result or {}).get('target_member') or {})
        approval_may_have_executed = bool(
            queue_delta
            or member_confirmed
            or (member_count_delta is not None and member_count_delta > 0)
        )
        return {
            'pending_before': pending_before,
            'pending_after': pending_after,
            'member_count_before': member_count_before,
            'member_count_after': member_count_after,
            'queue_delta': queue_delta,
            'member_count_delta': member_count_delta,
            'member_confirmed': member_confirmed,
            'approval_may_have_executed': approval_may_have_executed,
            'target_member_name': target_member.get('name'),
            'target_member_phone_raw': target_member.get('phone_raw'),
            'target_member_phone_normalized': target_member.get('phone_normalized'),
        }

    def registration_group_approval_decision(self, payload: RegistrationGroupApprovalDecisionRequest) -> Dict[str, Any]:
        approval_run_id = f"registration_group_approval_{uuid.uuid4().hex[:12]}"
        if self.ingress_async_default:
            source_key = str(payload.registration_group or 'registration_group_approval').strip() or 'registration_group_approval'
            queued_payload = payload.model_dump() if hasattr(payload, 'model_dump') else payload.dict()
            queued_payload['approval_run_id'] = approval_run_id
            queued = self._enqueue_ingress_event(
                ingress_type='registration_group_approval_decision',
                source_key=source_key,
                payload=queued_payload,
            )
            result_snapshot = dict(queued.get('result_snapshot') or {})
            if not approval_run_id:
                approval_run_id = str(result_snapshot.get('approval_run_id') or approval_run_id)
            if queued.get('duplicate'):
                existing = self._find_registration_group_approval_ingress_event(approval_run_id)
                if existing is not None:
                    approval_run_id = str((existing.get('payload_dict') or {}).get('approval_run_id') or (existing.get('result_snapshot_dict') or {}).get('approval_run_id') or approval_run_id)
            return {
                'accepted': True,
                'queued': True,
                'duplicate': bool(queued.get('duplicate')),
                'status': queued.get('status') or 'queued',
                'next_action': 'queued_for_processing',
                'approval_run_id': approval_run_id,
                'ingress_event_id': queued.get('event_id'),
                'executed': False,
                'verified': False,
                'verification_pending': False,
                'crm_recorded': False,
            }
        return self._registration_group_approval_decision_sync(payload, approval_run_id=approval_run_id)

    @staticmethod
    def _registration_group_requester_fingerprint(group_state: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        fingerprint: List[Dict[str, Any]] = []
        for item in (group_state or {}).get('requesters') or []:
            if not isinstance(item, dict):
                continue
            requester_id = str(item.get('requesterId') or '').strip()
            if not requester_id:
                continue
            fingerprint.append({
                'requesterId': requester_id,
                'requestedAtUnix': item.get('requestedAtUnix'),
            })
        fingerprint.sort(key=lambda item: (item['requesterId'], '' if item.get('requestedAtUnix') is None else str(item.get('requestedAtUnix'))))
        return fingerprint

    @staticmethod
    def _registration_group_expected_group_state(payload: RegistrationGroupApprovalDecisionRequest) -> Optional[Dict[str, Any]]:
        expected_requesters: List[Dict[str, Any]] = []
        for item in payload.expected_requesters or []:
            if not isinstance(item, dict):
                continue
            requester_id = str(item.get('requesterId') or '').strip()
            if not requester_id:
                continue
            expected_requesters.append({
                'requesterId': requester_id,
                'requestedAtUnix': item.get('requestedAtUnix'),
            })
        expected_requester_ids = [
            str(item).strip() for item in (payload.expected_requester_ids or []) if str(item).strip()
        ]
        if not expected_requesters and not expected_requester_ids and payload.expected_pending_count is None and payload.expected_member_count is None:
            return None
        return {
            'pending_count': payload.expected_pending_count,
            'member_count': payload.expected_member_count,
            'requester_ids': expected_requester_ids,
            'requesters': expected_requesters,
        }

    def _registration_group_queue_changed_before_execute_response(
        self,
        *,
        payload: RegistrationGroupApprovalDecisionRequest,
        decision: str,
        approval_run_id: str,
        started: float,
        expected_group_state: Dict[str, Any],
        current_group_state: Dict[str, Any],
    ) -> Dict[str, Any]:
        total_elapsed_seconds = round(time.perf_counter() - started, 3)
        expected_fingerprint = self._registration_group_requester_fingerprint(expected_group_state)
        current_fingerprint = self._registration_group_requester_fingerprint(current_group_state)
        evidence_summary = {
            'pending_before': expected_group_state.get('pending_count'),
            'pending_after': current_group_state.get('pending_count'),
            'member_count_before': expected_group_state.get('member_count'),
            'member_count_after': current_group_state.get('member_count'),
            'queue_delta': True,
            'member_count_delta': None,
            'member_confirmed': False,
            'approval_may_have_executed': False,
            'target_member_name': None,
            'target_member_phone_raw': None,
            'target_member_phone_normalized': None,
        }
        try:
            if expected_group_state.get('member_count') is not None and current_group_state.get('member_count') is not None:
                evidence_summary['member_count_delta'] = int(current_group_state.get('member_count')) - int(expected_group_state.get('member_count'))
        except Exception:
            evidence_summary['member_count_delta'] = None
        return {
            'registration_group': payload.registration_group,
            'decision': decision,
            'approval_run_id': approval_run_id,
            'executed': False,
            'verified': False,
            'verification_pending': False,
            'crm_recorded': False,
            'status': 'failed',
            'result_code': 'requester_fingerprint_changed_before_approval',
            'result_reason': 'registration group queue changed before approval execution; retry with a fresh snapshot',
            'approved_count': max(1, int(payload.approved_count or 1)),
            'approved_at': payload.decided_at,
            'elapsed_seconds': total_elapsed_seconds,
            'crm_elapsed_seconds': 0.0,
            'total_elapsed_seconds': total_elapsed_seconds,
            'force_immediate': payload.force_immediate,
            'target_member': {},
            'evidence_summary': evidence_summary,
            'raw_result': {
                'approval_run_id': approval_run_id,
                'execution_disposition': 'blocked_before_execution',
                'expected_group_state': expected_group_state,
                'current_group_state': current_group_state,
                'expected_requester_fingerprint': expected_fingerprint,
                'current_requester_fingerprint': current_fingerprint,
            },
            'crm_batch': None,
        }

    def _registration_group_approval_decision_sync(
        self,
        payload: RegistrationGroupApprovalDecisionRequest,
        *,
        approval_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        decision = str(payload.decision or 'approve').strip().lower() or 'approve'
        if decision != 'approve':
            raise HTTPException(status_code=400, detail='unsupported decision')
        routed_runtime = self._resolve_whatsapp_approval_runtime_executor(target_group=str(payload.registration_group or '').strip(), responsible_type='registration_group')
        executor = (routed_runtime or {}).get('executor') or self.registration_group_approval_executor
        if executor is None:
            raise HTTPException(status_code=400, detail='registration group approval executor not configured')
        approval_run_id = str(approval_run_id or '').strip() or f"registration_group_approval_{uuid.uuid4().hex[:12]}"
        expected_group_state = self._registration_group_expected_group_state(payload)
        current_group_state = self.registration_group_approval_executor_group_state(payload.registration_group)
        current_pending_count = max(0, int(current_group_state.get('pending_count') or 0))
        if current_pending_count <= 0:
            total_elapsed_seconds = round(time.perf_counter() - started, 3)
            return {
                'registration_group': payload.registration_group,
                'decision': decision,
                'approval_run_id': approval_run_id,
                'executed': False,
                'verified': False,
                'verification_pending': False,
                'crm_recorded': False,
                'status': 'failed',
                'result_code': 'no_pending_request',
                'result_reason': 'registration group has no pending requests at execution time',
                'approved_count': 0,
                'approved_at': payload.decided_at,
                'elapsed_seconds': total_elapsed_seconds,
                'crm_elapsed_seconds': 0.0,
                'total_elapsed_seconds': total_elapsed_seconds,
                'force_immediate': payload.force_immediate,
                'target_member': {},
                'evidence_summary': {
                    'pending_before': current_pending_count,
                    'pending_after': current_pending_count,
                    'member_count_before': current_group_state.get('member_count'),
                    'member_count_after': current_group_state.get('member_count'),
                    'queue_delta': False,
                    'member_count_delta': 0,
                    'member_confirmed': False,
                    'approval_may_have_executed': False,
                    'target_member_name': None,
                    'target_member_phone_raw': None,
                    'target_member_phone_normalized': None,
                },
                'raw_result': {
                    'approval_run_id': approval_run_id,
                    'current_group_state': current_group_state,
                    'execution_disposition': 'blocked_before_execution',
                },
                'crm_batch': None,
            }
        if expected_group_state is not None:
            expected_fingerprint = self._registration_group_requester_fingerprint(expected_group_state)
            current_fingerprint = self._registration_group_requester_fingerprint(current_group_state)
            fingerprint_changed = bool(expected_fingerprint) and expected_fingerprint != current_fingerprint
            pending_changed = (
                payload.expected_pending_count is not None
                and int(payload.expected_pending_count) != current_pending_count
            )
            if fingerprint_changed or pending_changed:
                return self._registration_group_queue_changed_before_execute_response(
                    payload=payload,
                    decision=decision,
                    approval_run_id=approval_run_id,
                    started=started,
                    expected_group_state=expected_group_state,
                    current_group_state=current_group_state,
                )
        execution_context = {
            'registration_group': payload.registration_group,
            'decision': decision,
            'decided_at': payload.decided_at,
            'decided_by': payload.decided_by,
            'decided_by_name': payload.decided_by_name,
            'source_platform': payload.source_platform,
            'source_campaign': payload.source_campaign,
            'source_adset': payload.source_adset,
            'source_ad': payload.source_ad,
            'target_name_hint': payload.target_name_hint,
            'target_phone_hint': payload.target_phone_hint,
            'approved_count': min(max(1, int(payload.approved_count or 1)), current_pending_count),
            'area': payload.area,
            'remark': payload.remark,
            'force_immediate': payload.force_immediate,
            'approval_run_id': approval_run_id,
            'latest_group_state_before_approve': current_group_state,
            'expected_group_state': expected_group_state,
            'approval_runtime_route': {
                'account_key': (routed_runtime or {}).get('account_key'),
                'account_name': (routed_runtime or {}).get('account_name'),
                'base_url': ((routed_runtime or {}).get('runtime_state') or {}).get('base_url'),
                'binding': (routed_runtime or {}).get('binding') or {},
                'responsible_type': 'registration_group',
            } if routed_runtime else None,
        }
        executor_timeout_seconds = max(10.0, float(os.getenv('REGISTRATION_GROUP_APPROVAL_EXECUTOR_TIMEOUT_SECONDS', '45') or 45))
        if hasattr(executor, 'approve') and callable(getattr(executor, 'approve')):
            call_target = lambda: executor.approve(execution_context)
        elif callable(executor):
            call_target = lambda: executor(execution_context)
        else:
            raise HTTPException(status_code=500, detail='registration group approval executor is not callable')
        result_holder: Dict[str, Any] = {}
        error_holder: Dict[str, BaseException] = {}

        def _run_executor_call() -> None:
            try:
                result_holder['result'] = call_target()
            except BaseException as exc:
                error_holder['error'] = exc

        executor_thread = threading.Thread(
            target=_run_executor_call,
            name=f'registration-group-approval-{approval_run_id}',
            daemon=True,
        )
        executor_thread.start()
        executor_thread.join(timeout=executor_timeout_seconds)
        if executor_thread.is_alive():
            timeout_elapsed = round(time.perf_counter() - started, 3)
            return {
                'registration_group': payload.registration_group,
                'decision': decision,
                'approval_run_id': approval_run_id,
                'executed': False,
                'verified': False,
                'verification_pending': False,
                'crm_recorded': False,
                'status': 'failed',
                'result_code': 'executor_timeout',
                'result_reason': f'registration group approval executor exceeded {executor_timeout_seconds:.0f}s timeout',
                'approved_count': int(payload.approved_count or 1),
                'approved_at': payload.decided_at,
                'elapsed_seconds': timeout_elapsed,
                'crm_elapsed_seconds': 0.0,
                'total_elapsed_seconds': timeout_elapsed,
                'force_immediate': payload.force_immediate,
                'target_member': {},
                'evidence_summary': {
                    'pending_before': None,
                    'pending_after': None,
                    'member_count_before': None,
                    'member_count_after': None,
                    'queue_delta': False,
                    'member_count_delta': None,
                    'member_confirmed': False,
                    'approval_may_have_executed': False,
                    'target_member_name': None,
                    'target_member_phone_raw': None,
                    'target_member_phone_normalized': None,
                },
                'raw_result': {
                    'approval_run_id': approval_run_id,
                    'executor_timeout_seconds': executor_timeout_seconds,
                },
                'crm_batch': None,
            }
        if 'error' in error_holder:
            raise error_holder['error']
        result = result_holder.get('result')
        if not isinstance(result, dict):
            raise HTTPException(status_code=500, detail='registration group approval executor must return dict result')
        raw_result = dict(result.get('raw_result') or {})
        raw_result.setdefault('approval_run_id', approval_run_id)
        crm_registration_group_name = self._resolve_registration_group_display_name(
            registration_group=payload.registration_group,
            raw_result=raw_result,
            expected_group_state=expected_group_state if isinstance(expected_group_state, dict) else None,
            current_group_state=current_group_state if isinstance(current_group_state, dict) else None,
        )
        verified = bool(result.get('verified'))
        evidence_summary = self._registration_group_approval_evidence_summary({**result, 'raw_result': raw_result})
        raw_result.setdefault('evidence_summary', evidence_summary)
        executed = True
        requested_approved_count = max(1, int(payload.approved_count or result.get('approved_count') or 1))
        approval_results = raw_result.get('approval_results')
        approved_success_count = None
        if isinstance(approval_results, list):
            approved_success_count = 0
            for item in approval_results:
                if not isinstance(item, dict):
                    continue
                error_value = item.get('error')
                if error_value in (None, '', 0, '0'):
                    approved_success_count += 1
                    continue
                try:
                    if int(error_value) == 409:
                        approved_success_count += 1
                except Exception:
                    continue
        observed_queue_consumed_count = None
        pending_before = evidence_summary.get('pending_before')
        pending_after = evidence_summary.get('pending_after')
        if pending_before is not None and pending_after is not None:
            try:
                observed_queue_consumed_count = max(0, int(pending_before) - int(pending_after))
            except Exception:
                observed_queue_consumed_count = None
        approved_count = requested_approved_count
        if (
            verified
            and requested_approved_count > 1
            and approved_success_count is not None
            and approved_success_count < requested_approved_count
        ):
            raw_result['verification_consistency_error'] = 'batch_success_count_mismatch'
            raw_result['verification_consistency_detail'] = {
                'requested_approved_count': requested_approved_count,
                'approved_success_count': approved_success_count,
            }
            if observed_queue_consumed_count and observed_queue_consumed_count > 0 and evidence_summary.get('approval_may_have_executed'):
                approved_count = observed_queue_consumed_count
                raw_result['verification_consistency_detail'].update({
                    'resolved_approved_count': approved_count,
                    'resolution': 'queue_consumed',
                })
            else:
                verified = False
        if verified and observed_queue_consumed_count and observed_queue_consumed_count > 0:
            approved_count = observed_queue_consumed_count
        verification_pending = bool(not verified and evidence_summary.get('approval_may_have_executed'))
        approved_at = str(result.get('approved_at') or result.get('finished_at') or payload.decided_at)
        target_member = result.get('target_member') or {}
        resolved_source_ad = payload.source_ad or ' '.join(
            part for part in [
                str(target_member.get('name') or '').strip(),
                str(target_member.get('phone_raw') or '').strip(),
            ] if part
        ) or None
        response_status = result.get('status')
        response_code = result.get('result_code')
        response_reason = result.get('result_reason')
        if verification_pending:
            response_status = 'pending_verification'
            response_code = 'approval_consumed_waiting_verification'
            response_reason = 'approval likely executed but strict verification is still pending'
        crm_batch = None
        crm_recorded = False
        crm_elapsed_seconds = 0.0
        if verified:
            crm_batch = self.create_registration_group_approval_batch(
                RegistrationGroupApprovalBatchRequest(
                    registration_group=payload.registration_group,
                    registration_group_name=crm_registration_group_name,
                    approved_count=approved_count,
                    approved_by=payload.decided_by,
                    approved_by_name=payload.decided_by_name,
                    source_platform=payload.source_platform,
                    source_campaign=payload.source_campaign,
                    source_adset=payload.source_adset,
                    source_ad=resolved_source_ad,
                    approved_at=approved_at,
                    area=payload.area,
                    remark=payload.remark,
                    approval_run_id=approval_run_id,
                )
            )
            crm_elapsed_seconds = round(float(crm_batch.get('elapsed_seconds') or 0.0), 3)
            crm_recorded = crm_batch.get('crm_sync_status') == 'success'
        total_elapsed_seconds = round(time.perf_counter() - started, 3)
        return {
            'registration_group': payload.registration_group,
            'decision': decision,
            'approval_run_id': approval_run_id,
            'executed': executed,
            'verified': verified,
            'verification_pending': verification_pending,
            'crm_recorded': crm_recorded,
            'status': response_status,
            'result_code': response_code,
            'result_reason': response_reason,
            'approved_count': approved_count,
            'approved_at': approved_at,
            'elapsed_seconds': result.get('elapsed_seconds'),
            'crm_elapsed_seconds': crm_elapsed_seconds,
            'total_elapsed_seconds': total_elapsed_seconds,
            'force_immediate': payload.force_immediate,
            'target_member': target_member,
            'evidence_summary': evidence_summary,
            'raw_result': raw_result,
            'crm_batch': crm_batch,
        }

    def _latest_group_join_task(self, conn: sqlite3.Connection, *, lead_id: str) -> Optional[Dict[str, Any]]:
        row = conn.execute(
            """
            SELECT task_id, lead_id, task_type, status, payload, result_code, result_reason, created_at, finished_at
            FROM automation_tasks
            WHERE lead_id = ? AND task_type = 'group_join'
            ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'failed' THEN 1 WHEN 'running' THEN 2 ELSE 3 END,
                     created_at DESC
            LIMIT 1
            """,
            (lead_id,),
        ).fetchone()
        return dict(row) if row else None

    def official_group_approval_decision(self, payload: OfficialGroupApprovalDecisionRequest) -> Dict[str, Any]:
        decision = str(payload.decision or 'approve').strip().lower() or 'approve'
        if decision != 'approve':
            raise HTTPException(status_code=400, detail='unsupported decision')
        check_result = self.official_group_approval_check(
            OfficialGroupApprovalCheckRequest(
                lead_id=payload.lead_id,
                target_group=payload.target_group,
                checked_at=payload.decided_at,
                checked_by=payload.decided_by,
                checked_by_name=payload.decided_by_name,
                source_platform=payload.source_platform,
                source_campaign=payload.source_campaign,
                source_adset=payload.source_adset,
                source_ad=payload.source_ad,
                target_phone_hint=payload.target_phone_hint,
                target_requester_id=payload.target_requester_id,
                remark=payload.remark,
            )
        )
        if not check_result.get('eligible'):
            with self.db.connect() as conn:
                self._record_audit_event(
                    conn,
                    event_type='official_group_approval_decision_skipped',
                    event_source='official_group_approval_decision',
                    payload={
                        **check_result,
                        'decision': decision,
                        'decided_by': payload.decided_by,
                        'decided_by_name': payload.decided_by_name,
                        'remark': payload.remark,
                    },
                    lead_id=str(payload.lead_id or '').strip() or None,
                )
                conn.commit()
            return {
                'lead_id': payload.lead_id,
                'target_group': payload.target_group,
                'decision': decision,
                'executed': False,
                **check_result,
            }
        decided_at = parse_iso_datetime(payload.decided_at).isoformat()
        with self.db.connect() as conn:
            lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (payload.lead_id,)).fetchone()
            if not lead_row:
                raise HTTPException(status_code=404, detail='lead not found')
            lead = dict(lead_row)
            task = self._latest_group_join_task(conn, lead_id=str(payload.lead_id or '').strip())
            if not task:
                raise HTTPException(status_code=400, detail='group_join task not found for lead')
        routed_runtime = self._resolve_whatsapp_approval_runtime_executor(target_group=str(payload.target_group or '').strip(), responsible_type='official_group')
        if routed_runtime:
            runtime_executor = routed_runtime['executor']
            runtime_binding = routed_runtime.get('binding') or {}
            runtime_group_target = str(
                runtime_binding.get('link')
                or runtime_binding.get('group_id')
                or payload.target_group
                or ''
            ).strip()
            runtime_context = {
                'registration_group': runtime_group_target,
                'decision': decision,
                'decided_at': payload.decided_at,
                'decided_by': payload.decided_by,
                'decided_by_name': payload.decided_by_name,
                'source_platform': payload.source_platform,
                'source_campaign': payload.source_campaign,
                'source_adset': payload.source_adset,
                'source_ad': payload.source_ad,
                'target_name_hint': str(payload.target_name_hint or lead.get('name') or lead.get('full_name') or '').strip() or None,
                'target_phone_hint': str(payload.target_phone_hint or lead.get('mobile') or '').strip() or None,
                'approved_count': 1,
                'area': str(lead.get('country') or lead.get('area') or 'Indonesia').strip() or 'Indonesia',
                'remark': payload.remark,
                'force_immediate': True,
                'approval_run_id': f"official_group_approval_{uuid.uuid4().hex[:12]}",
                'approval_runtime_route': {
                    'account_key': routed_runtime.get('account_key'),
                    'account_name': routed_runtime.get('account_name'),
                    'base_url': (routed_runtime.get('runtime_state') or {}).get('base_url'),
                    'binding': runtime_binding,
                    'responsible_type': 'official_group',
                    'resolved_group_target': runtime_group_target,
                },
            }
            executor_result = runtime_executor.approve(runtime_context) or {}
        else:
            if self.official_group_approval_executor is None:
                raise HTTPException(status_code=400, detail='official group approval executor not configured')
            executor_result = self.official_group_approval_executor.approve(
                target_group=str(payload.target_group or '').strip(),
                lead=lead,
                crm_snapshot=check_result.get('crm_snapshot') or {},
                task=task,
            ) or {}
        executor_raw_result = dict(executor_result.get('raw_result') or {})
        official_group_display_name_for_result = self._resolve_official_group_display_name(
            target_group=str(payload.target_group or '').strip(),
            raw_result=executor_raw_result,
        ) or str(payload.target_group or '').strip()
        execution_disposition = str(executor_raw_result.get('execution_disposition') or '').strip().lower()
        retryable = bool(executor_raw_result.get('retryable'))
        requires_human_action = bool(executor_raw_result.get('requires_human_action'))
        human_action_type = None
        if execution_disposition == 'retryable_failed' or retryable:
            follow_up_action = 'retry_official_group_approval'
            retryable = True
            requires_human_action = False
        elif execution_disposition == 'manual_required' or requires_human_action:
            follow_up_action = 'manual_continue_official_group_approval'
            requires_human_action = True
            retryable = False
            lowered_reason = f"{executor_result.get('result_code') or ''} {executor_result.get('result_reason') or ''}".lower()
            if 'captcha' in lowered_reason:
                human_action_type = 'captcha_required'
            elif 'auth' in lowered_reason or 'login' in lowered_reason:
                human_action_type = 'auth_required'
            elif 'session' in lowered_reason or 'expired' in lowered_reason:
                human_action_type = 'session_expired'
            else:
                human_action_type = 'manual_continue_required'
        else:
            follow_up_action = 'close_or_education' if str(executor_result.get('status') or '').strip().lower() == 'success' else 'queue_reengagement'
        group_join_payload = GroupJoinResultRequest(
            status=str(executor_result.get('status') or 'failed'),
            result_code=executor_result.get('result_code'),
            result_reason=executor_result.get('result_reason'),
            finished_at=decided_at,
            raw_result={
                **dict(executor_result.get('raw_result') or {}),
                'target_group': str(payload.target_group or '').strip(),
                'group_name': official_group_display_name_for_result,
                'decision': decision,
                'decided_by': payload.decided_by,
                'decided_by_name': payload.decided_by_name,
            },
        )
        decision_result = self.group_join_result(task['task_id'], group_join_payload)
        with self.db.connect() as conn:
            self._record_audit_event(
                conn,
                event_type='official_group_approval_decision_executed',
                event_source='official_group_approval_decision',
                payload={
                    'lead_id': payload.lead_id,
                    'target_group': payload.target_group,
                    'decision': decision,
                    'task_id': task['task_id'],
                    'eligibility': check_result,
                    'executor_result': executor_result,
                    'decision_result': decision_result,
                    'follow_up_action': follow_up_action,
                    'retryable': retryable,
                    'requires_human_action': requires_human_action,
                    'human_action_type': human_action_type,
                    'remark': payload.remark,
                },
                lead_id=str(payload.lead_id or '').strip() or None,
            )
            conn.commit()
        return {
            'lead_id': payload.lead_id,
            'target_group': payload.target_group,
            'decision': decision,
            'executed': True,
            'task_id': task['task_id'],
            'eligible': True,
            'reason_code': check_result.get('reason_code'),
            'next_action': decision_result.get('next_action'),
            'follow_up_action': follow_up_action,
            'retryable': retryable,
            'requires_human_action': requires_human_action,
            'human_action_type': human_action_type,
            'executor_result': executor_result,
            'decision_result': decision_result,
        }

    def retry_official_group_approval(self, lead_id: str, payload: OfficialGroupApprovalRetryRequest) -> Dict[str, Any]:
        normalized_lead_id = str(lead_id or '').strip()
        if not normalized_lead_id:
            raise HTTPException(status_code=400, detail='lead_id is required')
        return self.official_group_approval_decision(
            OfficialGroupApprovalDecisionRequest(
                lead_id=normalized_lead_id,
                target_group=payload.target_group,
                decision='approve',
                decided_at=payload.decided_at,
                decided_by=payload.decided_by,
                decided_by_name=payload.decided_by_name,
                source_platform=payload.source_platform,
                source_campaign=payload.source_campaign,
                source_adset=payload.source_adset,
                source_ad=payload.source_ad,
                remark=payload.remark,
            )
        )

    def official_group_approval_executor_health(self) -> Dict[str, Any]:
        executor = self.official_group_approval_executor
        if executor is None:
            return {
                'configured': False,
                'status': 'unconfigured',
                'provider': None,
                'supports': [],
            }
        health_fn = getattr(executor, 'health', None)
        if callable(health_fn):
            snapshot = health_fn() or {}
            return {
                'configured': True,
                'status': str(snapshot.get('status') or 'unknown'),
                'provider': snapshot.get('provider'),
                'supports': list(snapshot.get('supports') or []),
                'schema_version': snapshot.get('schema_version'),
                'details': snapshot,
            }
        return {
            'configured': True,
            'status': 'unknown',
            'provider': executor.__class__.__name__,
            'supports': ['approve'] if hasattr(executor, 'approve') else [],
        }

    def _official_group_summary_bucket(self) -> Dict[str, int]:
        return {
            'approved_count': 0,
            'failed_count': 0,
            'skipped_duplicate_count': 0,
            'retryable_failed_count': 0,
            'manual_required_count': 0,
        }

    def _normalize_official_group_summary_target_group(self, value: Any) -> str:
        normalized = str(value or '').strip()
        return normalized if normalized.startswith('official-group-') else ''

    def _resolve_group_join_task_target_group(self, row: Dict[str, Any]) -> str:
        payload: Dict[str, Any] = {}
        raw_result: Dict[str, Any] = {}
        try:
            payload = json.loads(row.get('payload') or '{}') if isinstance(row.get('payload'), str) else (row.get('payload') or {})
        except Exception:
            payload = {}
        try:
            raw_result = json.loads(row.get('raw_result') or '{}') if isinstance(row.get('raw_result'), str) else (row.get('raw_result') or {})
        except Exception:
            raw_result = {}
        return self._normalize_official_group_summary_target_group(
            row.get('target_group')
            or payload.get('target_group')
            or raw_result.get('target_group')
            or ''
        )

    def _fetch_official_group_bridge_pending_counts(self) -> Optional[Dict[str, Any]]:
        executor = self.official_group_approval_executor
        if executor is None:
            return None
        webhook_url = str(getattr(executor, 'webhook_url', '') or '').strip()
        if not webhook_url:
            try:
                health = executor.health() if hasattr(executor, 'health') else {}
            except Exception:
                health = {}
            webhook_url = str((health or {}).get('webhook_url') or '').strip()
        if not webhook_url or '/official-group/approve' not in webhook_url:
            return None
        base_url = webhook_url.split('/official-group/approve', 1)[0].rstrip('/')
        if not base_url:
            return None
        try:
            summary = fetch_json(f'{base_url}/ops/official-group-bridge/summary', timeout=5.0)
        except Exception:
            return None
        by_target_group_raw = summary.get('by_target_group') or {}
        if not isinstance(by_target_group_raw, dict):
            by_target_group_raw = {}
        by_target_group: Dict[str, Dict[str, int]] = {}
        total_pending = 0
        for target_group, bucket in by_target_group_raw.items():
            normalized_target = str(target_group or '').strip()
            if not normalized_target:
                continue
            pending_value = 0
            if isinstance(bucket, dict):
                try:
                    pending_value = max(int(bucket.get('pending_count') or 0), 0)
                except Exception:
                    pending_value = 0
            total_pending += pending_value
            by_target_group[normalized_target] = {'pending_count': pending_value}
        return {
            'pending_count': total_pending,
            'by_target_group': by_target_group,
        }

    def official_group_approval_summary(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            fallback_pending_count = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE current_status IN ('bind_success', 'group_join_pending', 'group_join_failed')"
            ).fetchone()[0]
            latest_task_rows = [dict(row) for row in conn.execute(
                """
                WITH ranked AS (
                    SELECT task_id, lead_id, status, payload, raw_result, created_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY lead_id
                               ORDER BY datetime(COALESCE(finished_at, created_at)) DESC, datetime(created_at) DESC, task_id DESC
                           ) AS rn
                    FROM automation_tasks
                    WHERE task_type = 'group_join'
                )
                SELECT task_id, lead_id, status, payload, raw_result, created_at
                FROM ranked
                WHERE rn = 1
                """
            ).fetchall()]
            skipped_rows = conn.execute(
                """
                SELECT payload FROM operator_audit_log
                WHERE event_type = 'official_group_approval_decision_skipped'
                ORDER BY created_at DESC
                """
            ).fetchall()

        runtime_rows = self._official_group_runtime_queue_rows(now_iso=utc_now())
        pending_count = int(sum(max(int(row.get('pending_count') or 0), 0) for row in runtime_rows) if runtime_rows else int(fallback_pending_count or 0))
        bridge_pending = self._fetch_official_group_bridge_pending_counts()

        active_target_groups: set[str] = set()
        for row in runtime_rows:
            normalized_target = self._normalize_official_group_summary_target_group(row.get('target_group'))
            if normalized_target:
                active_target_groups.add(normalized_target)
        if bridge_pending is not None:
            for target_group, pending_bucket in (bridge_pending.get('by_target_group') or {}).items():
                normalized_target = self._normalize_official_group_summary_target_group(target_group)
                if not normalized_target:
                    continue
                if max(int((pending_bucket or {}).get('pending_count') or 0), 0) > 0:
                    active_target_groups.add(normalized_target)

        scoped_summary = bool(active_target_groups)
        by_target_group: Dict[str, Dict[str, int]] = {}

        for row in latest_task_rows:
            target_group = self._resolve_group_join_task_target_group(row)
            if not target_group:
                continue
            if scoped_summary and target_group not in active_target_groups:
                continue
            by_target_group.setdefault(target_group, self._official_group_summary_bucket())
            status = str(row.get('status') or '').strip().lower()
            try:
                parsed_raw = json.loads(row.get('raw_result') or '{}') if isinstance(row.get('raw_result'), str) else (row.get('raw_result') or {})
            except Exception:
                parsed_raw = {}
            disposition = str(parsed_raw.get('execution_disposition') or '').strip().lower()
            if status == 'success':
                by_target_group[target_group]['approved_count'] += 1
                continue
            if status != 'failed':
                continue
            if disposition == 'retryable_failed':
                by_target_group[target_group]['retryable_failed_count'] += 1
            elif disposition == 'manual_required':
                by_target_group[target_group]['manual_required_count'] += 1
            else:
                by_target_group[target_group]['failed_count'] = int(by_target_group[target_group].get('failed_count') or 0) + 1

        for row in skipped_rows:
            try:
                payload = json.loads(row['payload'] or '{}')
            except Exception:
                payload = {}
            if str(payload.get('reason_code') or '') != 'already_in_target_group':
                continue
            target_group = self._normalize_official_group_summary_target_group(payload.get('target_group'))
            if not target_group:
                continue
            if scoped_summary and target_group not in active_target_groups:
                continue
            by_target_group.setdefault(target_group, self._official_group_summary_bucket())
            by_target_group[target_group]['skipped_duplicate_count'] += 1

        if bridge_pending is not None:
            for target_group, bucket in list(by_target_group.items()):
                pending_bucket = (bridge_pending.get('by_target_group') or {}).get(target_group) or {}
                bucket['manual_required_count'] = max(int(pending_bucket.get('pending_count') or 0), 0)
            if not scoped_summary:
                for target_group, pending_bucket in (bridge_pending.get('by_target_group') or {}).items():
                    normalized_target = self._normalize_official_group_summary_target_group(target_group)
                    if not normalized_target:
                        continue
                    by_target_group.setdefault(normalized_target, self._official_group_summary_bucket())
                    by_target_group[normalized_target]['manual_required_count'] = max(int((pending_bucket or {}).get('pending_count') or 0), 0)

        filtered_by_target_group = {}
        for target_group, bucket in by_target_group.items():
            normalized_bucket = {
                'approved_count': int(bucket.get('approved_count') or 0),
                'failed_count': int(bucket.get('failed_count') or 0),
                'skipped_duplicate_count': int(bucket.get('skipped_duplicate_count') or 0),
                'retryable_failed_count': int(bucket.get('retryable_failed_count') or 0),
                'manual_required_count': int(bucket.get('manual_required_count') or 0),
            }
            if any(normalized_bucket.values()):
                filtered_by_target_group[target_group] = normalized_bucket

        approved_count = sum(bucket['approved_count'] for bucket in filtered_by_target_group.values())
        failed_count = sum(bucket['failed_count'] for bucket in filtered_by_target_group.values())
        skipped_duplicate_count = sum(bucket['skipped_duplicate_count'] for bucket in filtered_by_target_group.values())
        retryable_failed_count = sum(bucket['retryable_failed_count'] for bucket in filtered_by_target_group.values())
        manual_required_count = sum(bucket['manual_required_count'] for bucket in filtered_by_target_group.values())
        return {
            'view_scope': 'current_active_scope',
            'pending_count': int(pending_count or 0),
            'approved_count': approved_count,
            'failed_count': failed_count,
            'skipped_duplicate_count': skipped_duplicate_count,
            'retryable_failed_count': retryable_failed_count,
            'manual_required_count': manual_required_count,
            'by_target_group': filtered_by_target_group,
        }

    def run_ready_official_group_batches(self, payload: OfficialGroupBatchRunRequest) -> Dict[str, Any]:
        now_iso = parse_iso_datetime(payload.decided_at).isoformat()
        batch_queue = self.approval_batch_queue()
        ready_groups = [row for row in list(batch_queue.get('official_groups') or []) if bool(row.get('ready'))]
        ready_groups = ready_groups[:max(1, int(payload.limit_groups or 10))]
        official_statuses = ('bind_success', 'group_join_pending', 'group_join_failed', 'group_join_success', 'synced')
        results: list[dict[str, Any]] = []
        unresolved_count = 0
        executed_count = 0
        skipped_count = 0
        for group in ready_groups:
            registration_group = str(group.get('registration_group') or '').strip()
            target_group_filter = str(group.get('target_group') or '').strip()
            requesters = list(group.get('requesters') or []) if isinstance(group.get('requesters'), list) else []
            release_count = int(group.get('release_count') or group.get('pending_count') or 0)
            if payload.limit_leads_per_group is not None:
                release_count = min(release_count, max(1, int(payload.limit_leads_per_group or 1)))
            with self.db.connect() as conn:
                if target_group_filter:
                    candidate_rows = conn.execute(
                        f"""
                        SELECT * FROM leads
                        WHERE current_status IN ({','.join(['?'] * len(official_statuses))})
                        ORDER BY updated_at ASC, created_at ASC
                        """,
                        official_statuses,
                    ).fetchall()
                    filtered_rows = []
                    for lead_row in candidate_rows:
                        lead = dict(lead_row)
                        if str(self._resolve_official_group_target_group(lead=lead) or '').strip() == target_group_filter:
                            filtered_rows.append(lead_row)
                    lead_rows, unmatched_requesters = self._match_official_group_requesters_to_leads(
                        lead_rows=filtered_rows,
                        requesters=requesters,
                        release_count=release_count,
                    )
                else:
                    base_rows = conn.execute(
                        f"""
                        SELECT * FROM leads
                        WHERE pendaftaran_group = ?
                          AND current_status IN ({','.join(['?'] * len(official_statuses))})
                        ORDER BY updated_at ASC, created_at ASC
                        """,
                        (registration_group, *official_statuses),
                    ).fetchall()
                    lead_rows, unmatched_requesters = self._match_official_group_requesters_to_leads(
                        lead_rows=base_rows,
                        requesters=requesters,
                        release_count=release_count,
                    )
            for unmatched in unmatched_requesters:
                crm_only_test_lead = None
                if payload.allow_crm_only_test_match:
                    crm_row, _ = self._find_crm_customer_for_official_group_requester(unmatched)
                    if crm_row:
                        crm_only_test_lead = self._materialize_crm_only_test_lead_for_official_group_requester(
                            requester=unmatched,
                            crm_row=crm_row,
                            target_group=target_group_filter,
                            created_at=now_iso,
                        )
                if crm_only_test_lead:
                    lead_rows.append(crm_only_test_lead)
                    continue
                skipped_count += 1
                detail = {
                    'registration_group': registration_group,
                    'target_group': target_group_filter or None,
                    'reason_code': 'official_group_requester_unmatched',
                    'next_action': 'manual_review_official_group_approval',
                    'requester': unmatched,
                    'mobile': str(
                        (unmatched or {}).get('phoneNormalized')
                        or (unmatched or {}).get('phone_normalized')
                        or (unmatched or {}).get('phoneRaw')
                        or (unmatched or {}).get('phone_raw')
                        or (unmatched or {}).get('debugLidPhoneRaw')
                        or ''
                    ).strip() or None,
                }
                results.append(detail)
            for lead_row in lead_rows:
                lead = dict(lead_row)
                target_group = str(
                    lead.get('crm_only_test_target_group')
                    or self._resolve_official_group_target_group(lead=lead)
                    or ''
                ).strip()
                if not target_group:
                    unresolved_count += 1
                    detail = {
                        'lead_id': lead.get('lead_id'),
                        'registration_group': registration_group,
                        'reason_code': 'official_group_target_unresolved',
                        'next_action': 'configure_official_group_target_mapping',
                    }
                    with self.db.connect() as conn:
                        self._record_audit_event(
                            conn,
                            event_type='official_group_approval_target_unresolved',
                            event_source='official_group_batch_runner',
                            payload=detail,
                            lead_id=str(lead.get('lead_id') or '').strip() or None,
                        )
                        conn.commit()
                    results.append(detail)
                    continue
                result = self.official_group_approval_decision(
                    OfficialGroupApprovalDecisionRequest(
                        lead_id=str(lead.get('lead_id') or '').strip(),
                        target_group=target_group,
                        decision='approve',
                        decided_at=now_iso,
                        decided_by=payload.decided_by,
                        decided_by_name=payload.decided_by_name,
                        source_platform=payload.source_platform,
                        source_campaign=payload.source_campaign,
                        source_adset=payload.source_adset,
                        source_ad=payload.source_ad,
                        remark=payload.remark,
                        target_name_hint=str(lead.get('matched_requester_name_hint') or '').strip() or None,
                        target_phone_hint=str(lead.get('matched_requester_phone_hint') or '').strip() or None,
                        target_requester_id=str(lead.get('matched_requester_id') or '').strip() or None,
                    )
                )
                if not result.get('executed') and str(result.get('next_action') or '').strip() == 'manual_review_official_group_approval':
                    result = {
                        **result,
                        'group_name': str(self._resolve_official_group_display_name(target_group=target_group) or target_group).strip() or target_group,
                        'mobile': str(lead.get('mobile') or lead.get('matched_requester_phone_hint') or '').strip() or None,
                    }
                results.append(result)
                if result.get('executed'):
                    executed_count += 1
                else:
                    skipped_count += 1
        notification_results = self._send_official_group_success_notifications(
            decided_at=now_iso,
            ready_groups=ready_groups,
            results=results,
        )
        return {
            'executed': True,
            'decided_at': now_iso,
            'ready_group_count': len(ready_groups),
            'executed_count': executed_count,
            'skipped_count': skipped_count,
            'unresolved_count': unresolved_count,
            'results': results,
            'notification_results': notification_results,
        }

    def _official_group_has_abnormal_marker(self, lead: Dict[str, Any]) -> Tuple[bool, List[str]]:
        if not isinstance(lead, dict):
            return False, []
        reasons: List[str] = []
        current_status = str(lead.get('current_status') or '').strip().lower()
        review_status = str(lead.get('review_status') or '').strip().lower()
        routing_decision = str(lead.get('routing_decision') or '').strip().lower()
        if current_status == 'manual_review_pending':
            reasons.append('manual_review_pending')
        if review_status in {'pending', 'retry_requested', 'rejected'}:
            reasons.append(f'review_status:{review_status}')
        if routing_decision == 'manual_review':
            reasons.append('routing_decision:manual_review')
        return bool(reasons), reasons

    def _official_group_requester_pending_in_runtime(
        self,
        *,
        target_group: str,
        target_phone_hint: Optional[str] = None,
        target_requester_id: Optional[str] = None,
    ) -> bool:
        routed_runtime = self._resolve_whatsapp_approval_runtime_executor(target_group=target_group, responsible_type='official_group')
        if not routed_runtime:
            return False
        runtime_state = dict(routed_runtime.get('runtime_state') or {})
        runtime_base_url = str(runtime_state.get('base_url') or '').strip()
        binding = dict(routed_runtime.get('binding') or {})
        probe_target = (
            str(binding.get('group_id') or '').strip()
            or str(binding.get('link') or '').strip()
            or str(binding.get('registration_group') or '').strip()
            or str(binding.get('group_name') or '').strip()
        )
        if not runtime_base_url or not probe_target:
            return False
        try:
            group_state = self._request_whatsapp_approval_group_state_with_retry(runtime_base_url, probe_target)
        except Exception:
            return False
        target_requester_id_normalized = str(target_requester_id or '').strip()
        target_phone_keys = self._official_group_phone_match_keys(phone=target_phone_hint)
        for requester in list(group_state.get('requesters') or []):
            if not isinstance(requester, dict):
                continue
            requester_id = str(requester.get('requesterId') or '').strip()
            if target_requester_id_normalized and requester_id and requester_id == target_requester_id_normalized:
                return True
            requester_phone_keys = set()
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=requester.get('phoneNormalized')))
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=requester.get('phoneRaw')))
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=requester.get('debugLidPhoneRaw')))
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=requester.get('debugContactNumberRaw')))
            requester_phone_keys.update(self._official_group_phone_match_keys(phone=requester_id))
            if target_phone_keys and requester_phone_keys.intersection(target_phone_keys):
                return True
        return False

    def official_group_approval_check(self, payload: OfficialGroupApprovalCheckRequest) -> Dict[str, Any]:
        lead_id = str(payload.lead_id or '').strip()
        target_group = str(payload.target_group or '').strip()
        if not lead_id:
            raise HTTPException(status_code=400, detail='lead_id is required')
        if not target_group:
            raise HTTPException(status_code=400, detail='target_group is required')
        checked_at = parse_iso_datetime(payload.checked_at)
        checked_at_iso = checked_at.isoformat()
        with self.db.connect() as conn:
            lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            if not lead_row:
                raise HTTPException(status_code=404, detail='lead not found')
            lead = dict(lead_row)
            current_status = str(lead.get('current_status') or '')
            crm_verified = bool(
                lead.get('crm_verified_at')
                or lead.get('crm_verified_payload')
                or lead.get('crm_verified_app_name')
                or lead.get('crm_verified_registration_group')
            )
            if not crm_verified and self._restore_verified_crm_state_from_sync_logs(conn, lead_id=lead_id):
                lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
                lead = dict(lead_row)
                current_status = str(lead.get('current_status') or '')
                crm_verified = bool(
                    lead.get('crm_verified_at')
                    or lead.get('crm_verified_payload')
                    or lead.get('crm_verified_app_name')
                    or lead.get('crm_verified_registration_group')
                )
            abnormal_flagged, abnormal_reasons = self._official_group_has_abnormal_marker(lead)
            result: Dict[str, Any] = {
                'lead_id': lead_id,
                'target_group': target_group,
                'checked_at': checked_at_iso,
                'checked_by': payload.checked_by,
                'checked_by_name': payload.checked_by_name,
                'current_status': current_status,
                'crm_verified': crm_verified,
                'crm_customer_found': False,
                'crm_snapshot': None,
                'eligible': False,
                'reason_code': 'unknown',
                'reason_detail': None,
                'next_action': 'manual_review_official_group_approval',
                'abnormal_flagged': abnormal_flagged,
                'abnormal_reasons': abnormal_reasons,
            }
            if abnormal_flagged:
                result.update({
                    'reason_code': 'abnormal_flagged',
                    'reason_detail': f'Lead is marked abnormal: {", ".join(abnormal_reasons)}',
                    'next_action': 'manual_review_official_group_approval',
                })
            elif self.crm_adapter is None:
                result.update({
                    'reason_code': 'crm_adapter_not_configured',
                    'reason_detail': 'CRM adapter is unavailable.',
                    'next_action': 'manual_review_official_group_approval',
                })
            else:
                target_requester_still_pending = self._official_group_requester_pending_in_runtime(
                    target_group=target_group,
                    target_phone_hint=payload.target_phone_hint,
                    target_requester_id=payload.target_requester_id,
                )
                result['target_requester_still_pending'] = target_requester_still_pending
                crm_row = self._find_existing_customer_with_fallback(
                    yw_id=lead.get('yw_id'),
                    mobile=lead.get('mobile'),
                    app_name=lead.get('crm_verified_app_name') or lead.get('app_name'),
                    dept_name=lead.get('crm_verified_dept_name') or lead.get('dept_name'),
                    registration_group=lead.get('crm_verified_registration_group') or lead.get('pendaftaran_group'),
                    official_group=None,
                )
                result['crm_customer_found'] = bool(crm_row)
                cached_verified_payload: Dict[str, Any] = {}
                try:
                    parsed_verified_payload = json.loads(lead.get('crm_verified_payload') or '{}')
                except Exception:
                    parsed_verified_payload = {}
                if isinstance(parsed_verified_payload, dict):
                    cached_verified_payload = parsed_verified_payload
                cached_snapshot = {
                    'id': cached_verified_payload.get('id') or lead.get('matched_customer_id'),
                    'mobile': cached_verified_payload.get('mobile') or lead.get('mobile'),
                    'ywId': cached_verified_payload.get('ywId') or lead.get('yw_id'),
                    'appName': cached_verified_payload.get('appName') or lead.get('crm_verified_app_name') or lead.get('app_name'),
                    'deptName': cached_verified_payload.get('deptName') or lead.get('crm_verified_dept_name') or lead.get('dept_name'),
                    'pendaftaranGroup': cached_verified_payload.get('pendaftaranGroup') or lead.get('crm_verified_registration_group') or lead.get('pendaftaran_group'),
                    'wa': cached_verified_payload.get('wa') or lead.get('crm_verified_official_group') or '',
                    'joinGroup': cached_verified_payload.get('joinGroup'),
                }
                if crm_row:
                    result['crm_snapshot'] = {
                        'id': crm_row.get('id'),
                        'mobile': crm_row.get('mobile'),
                        'ywId': crm_row.get('ywId'),
                        'appName': crm_row.get('appName'),
                        'deptName': crm_row.get('deptName'),
                        'pendaftaranGroup': crm_row.get('pendaftaranGroup'),
                        'wa': crm_row.get('wa'),
                        'joinGroup': crm_row.get('joinGroup'),
                        'source': 'live_crm',
                    }
                    self._record_verified_crm_state(
                        conn,
                        lead_id=lead_id,
                        crm_payload=crm_row,
                        official_group=str(crm_row.get('wa') or '').strip() or None,
                    )
                    lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
                    lead = dict(lead_row)
                elif crm_verified:
                    result['crm_snapshot'] = {
                        **cached_snapshot,
                        'source': 'local_verified_cache',
                    }
                if crm_row and self._official_group_value_matches_target(
                    value=crm_row.get('wa'),
                    target_group=target_group,
                ) and not target_requester_still_pending:
                    result.update({
                        'reason_code': 'already_in_target_group',
                        'reason_detail': 'CRM already points to the requested official group.',
                        'next_action': 'skip_duplicate_group_approval',
                    })
                elif not crm_row and self._official_group_value_matches_target(
                    value=cached_snapshot.get('wa'),
                    target_group=target_group,
                ) and not target_requester_still_pending:
                    result.update({
                        'reason_code': 'already_in_target_group',
                        'reason_detail': 'Local verified CRM snapshot already points to the requested official group.',
                        'next_action': 'skip_duplicate_group_approval',
                    })
                elif not crm_row and crm_verified:
                    result.update({
                        'eligible': True,
                        'reason_code': 'eligible',
                        'reason_detail': 'Local verified CRM snapshot is present; official-group approval is allowed even though live CRM lookup missed.',
                        'next_action': 'approve_official_group',
                    })
                elif not crm_row:
                    result.update({
                        'reason_code': 'crm_customer_not_found',
                        'reason_detail': 'No matching CRM customer was found for approval gating.',
                        'next_action': 'manual_review_official_group_approval',
                    })
                else:
                    result.update({
                        'eligible': True,
                        'reason_code': 'eligible',
                        'reason_detail': 'CRM customer found and no abnormal marker is present; official-group approval is allowed.',
                        'next_action': 'approve_official_group',
                    })
            self._record_audit_event(
                conn,
                event_type='official_group_approval_eligibility_checked',
                event_source='official_group_approval_check',
                payload={
                    **result,
                    'source_platform': payload.source_platform,
                    'source_campaign': payload.source_campaign,
                    'source_adset': payload.source_adset,
                    'source_ad': payload.source_ad,
                    'remark': payload.remark,
                },
                lead_id=lead_id,
            )
            conn.commit()
            return result

    def ops_bind_queue(self) -> Dict[str, Any]:
        statuses = ('account_submitted', 'recognition_pending', 'bind_check_pending')
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                f"""
                SELECT l.lead_id, l.mobile, l.area_code, l.yw_id, l.app_name, l.dept_name, l.pendaftaran_group,
                       l.current_status, l.updated_at, l.parser_confidence, l.parser_missing_fields, l.parser_conflicts,
                       COALESCE(
                         (SELECT t.task_id FROM automation_tasks t
                           WHERE t.lead_id = l.lead_id AND t.task_type = 'account_recognition'
                           ORDER BY t.created_at DESC LIMIT 1),
                         (SELECT t.task_id FROM automation_tasks t
                           WHERE t.lead_id = l.lead_id AND t.task_type = 'bind_check'
                           ORDER BY t.created_at DESC LIMIT 1)
                       ) AS task_id
                FROM leads l
                WHERE l.current_status IN ({','.join(['?']*len(statuses))})
                ORDER BY l.updated_at DESC
                """,
                statuses,
            ).fetchall()]
            for row in rows:
                row['parser_missing_fields'] = json.loads(row.get('parser_missing_fields') or '[]')
                row['parser_conflicts'] = json.loads(row.get('parser_conflicts') or '[]')
            return {'rows': rows}

    def ops_group_queue(self) -> Dict[str, Any]:
        statuses = ('bind_success', 'group_join_pending')
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                f"""
                SELECT l.lead_id, l.mobile, l.area_code, l.yw_id, l.app_name, l.dept_name, l.pendaftaran_group,
                       l.current_status, l.updated_at, l.parser_confidence, l.parser_missing_fields, l.parser_conflicts,
                       (SELECT t.task_id FROM automation_tasks t
                         WHERE t.lead_id = l.lead_id AND t.task_type = 'group_join'
                         ORDER BY t.created_at DESC LIMIT 1) AS task_id
                FROM leads l
                WHERE l.current_status IN ({','.join(['?']*len(statuses))})
                ORDER BY l.updated_at DESC
                """,
                statuses,
            ).fetchall()]
            return {'rows': rows}

    def ops_dashboard_summary(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            bind_queue_count = conn.execute("SELECT COUNT(*) FROM leads WHERE current_status IN ('account_submitted','recognition_pending','bind_check_pending')").fetchone()[0]
            manual_review_count = conn.execute("SELECT COUNT(*) FROM leads WHERE current_status = 'manual_review_pending'").fetchone()[0]
            group_queue_count = conn.execute("SELECT COUNT(*) FROM leads WHERE current_status IN ('bind_success','group_join_pending')").fetchone()[0]
            bind_success_count = conn.execute("SELECT COUNT(*) FROM leads WHERE current_status = 'bind_success'").fetchone()[0]
            bind_failed_count = conn.execute("SELECT COUNT(*) FROM leads WHERE current_status = 'bind_failed'").fetchone()[0]
            group_join_success_count = conn.execute("SELECT COUNT(*) FROM leads WHERE current_status = 'group_join_success'").fetchone()[0]
            crm_synced_count = conn.execute("SELECT COUNT(*) FROM leads WHERE current_status = 'synced'").fetchone()[0]
            voucher_uploaded_count = conn.execute("SELECT COUNT(*) FROM customer_projection WHERE pz_status = 1").fetchone()[0]
            parser_conflict_count = conn.execute("SELECT COUNT(*) FROM leads WHERE parser_status = 'conflict'").fetchone()[0]
            correction_count = conn.execute("SELECT COUNT(*) FROM lead_corrections").fetchone()[0]
            return {
                'bind_queue_count': bind_queue_count,
                'manual_review_count': manual_review_count,
                'group_queue_count': group_queue_count,
                'bind_success_count': bind_success_count,
                'bind_failed_count': bind_failed_count,
                'group_join_success_count': group_join_success_count,
                'crm_synced_count': crm_synced_count,
                'voucher_uploaded_count': voucher_uploaded_count,
                'parser_conflict_count': parser_conflict_count,
                'correction_count': correction_count,
            }

    def ops_next_bind_task(self) -> Dict[str, Any]:
        queue = self.ops_bind_queue()['rows']
        return {'kind': 'bind', 'row': queue[0]} if queue else {'kind': 'none', 'row': None}

    def ops_next_group_task(self) -> Dict[str, Any]:
        queue = self.ops_group_queue()['rows']
        return {'kind': 'group', 'row': queue[0]} if queue else {'kind': 'none', 'row': None}

    def ops_next_action(self) -> Dict[str, Any]:
        candidates = []
        review_rows = self.ops_manual_review_queue()['rows']
        for row in review_rows:
            candidates.append({'kind': 'manual_review', 'row': row, 'score': 110, 'reason': '存在待人工复核的数据，优先处理脏数据入口'})
        bind_rows = self.ops_bind_queue()['rows']
        for row in bind_rows:
            if row.get('current_status') == 'bind_failed':
                candidates.append({'kind': 'bind', 'row': row, 'score': 100, 'reason': '绑定失败优先复核与再次沟通'})
            elif row.get('current_status') == 'bind_check_pending':
                candidates.append({'kind': 'bind', 'row': row, 'score': 90, 'reason': '存在待回写的绑定结果'})
            elif row.get('current_status') == 'recognition_pending':
                candidates.append({'kind': 'bind', 'row': row, 'score': 80, 'reason': '截图待识别，需先得到账号ID'})
            else:
                candidates.append({'kind': 'bind', 'row': row, 'score': 70, 'reason': '存在待处理的账号绑定任务'})

        with self.db.connect() as conn:
            crm_sync_rows = [dict(r) for r in conn.execute(
                """
                SELECT l.lead_id, l.mobile, l.area_code, l.yw_id, l.app_name, l.dept_name, l.pendaftaran_group,
                       l.current_status, l.updated_at
                FROM leads l
                WHERE l.current_status = 'bind_success'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM sync_logs sl
                      WHERE sl.lead_id = l.lead_id
                        AND sl.target_system = 'crm'
                        AND sl.status = 'success'
                  )
                ORDER BY l.updated_at DESC
                """
            ).fetchall()]
        for row in crm_sync_rows:
            candidates.append({'kind': 'crm_sync', 'row': row, 'score': 60, 'reason': '公会绑定已成功，需入库 CRM'})

        batch_queue = self.approval_batch_queue()
        for bucket_name, rows in [('registration_approval_batch', batch_queue['registration_groups']), ('official_approval_batch', batch_queue['official_groups'])]:
            for row in rows:
                if row.get('ready'):
                    candidates.append({'kind': bucket_name, 'row': row, 'score': 55, 'reason': f"审批批次已就绪：{row.get('registration_group')}"})

        group_rows = self.ops_group_queue()['rows']
        for row in group_rows:
            if row.get('current_status') == 'group_join_failed':
                candidates.append({'kind': 'group', 'row': row, 'score': 50, 'reason': '官方群处理失败，需优先重试或复核'})
            else:
                candidates.append({'kind': 'group', 'row': row, 'score': 40, 'reason': '存在待处理的官方群审批/入群任务'})

        if not candidates:
            return {'kind': 'none', 'row': None, 'score': 0, 'reason': '当前没有待处理任务'}

        candidates.sort(key=lambda x: (-x['score'], x['row'].get('updated_at') or ''))
        return candidates[0]

    def _fetch_intake_bot_preset_rows(self) -> list[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT profile_name, app_id, robot_name, default_app, default_guild, enabled, updated_at FROM intake_bot_presets ORDER BY profile_name ASC"
            ).fetchall()]
        for row in rows:
            normalized_profile = str(row.get('profile_name') or '').strip()
            robot_name = str(row.get('robot_name') or '').strip()
            row['robot_name'] = robot_name or normalized_profile
        return rows

    def _list_notify_robot_options(self) -> list[Dict[str, Any]]:
        profile_name = 'wa-approval-broadcast'
        robot_name = '审批bot01'
        return [{
            'profile_name': profile_name,
            'robot_name': robot_name,
            'label': robot_name,
            'app_id': 'cli_a97b238cefb89e18',
        }]

    def list_whatsapp_approval_area_options(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT options_json, updated_at FROM whatsapp_approval_area_options WHERE option_key = 'default'"
            ).fetchone()
            account_rows = conn.execute(
                "SELECT area, group_links FROM whatsapp_approval_accounts"
            ).fetchall()
        source_options = list(WHATSAPP_APPROVAL_DEFAULT_AREA_OPTIONS)
        updated_at = None
        configured_values: list[str] = []
        if row:
            try:
                loaded = json.loads(str(row['options_json'] or '[]'))
            except Exception:
                loaded = []
            normalized = _normalize_area_options(loaded if isinstance(loaded, list) else [])
            if normalized:
                source_options = normalized
            updated_at = row['updated_at']
        for account_row in account_rows:
            area_value = str(account_row['area'] or '').strip()
            if area_value:
                configured_values.append(area_value)
            try:
                loaded_group_links = json.loads(str(account_row['group_links'] or '[]'))
            except Exception:
                loaded_group_links = []
            if isinstance(loaded_group_links, list):
                for item in loaded_group_links:
                    if isinstance(item, dict):
                        binding_area = str(item.get('area') or '').strip()
                        if binding_area:
                            configured_values.append(binding_area)
        merged = _normalize_area_options([*(item['value'] for item in source_options), *configured_values])
        return {
            'options': merged,
            'source_options': source_options,
            'updated_at': updated_at,
        }

    def update_whatsapp_approval_area_options(self, payload: WhatsAppApprovalAreaOptionsUpdateRequest) -> Dict[str, Any]:
        normalized = _normalize_area_options(payload.options or [])
        if not normalized:
            raise HTTPException(status_code=400, detail='at least one area option is required')
        row = {
            'option_key': 'default',
            'options_json': json.dumps([item['value'] for item in normalized], ensure_ascii=False),
            'updated_at': utc_now(),
        }
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO whatsapp_approval_area_options (option_key, options_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(option_key)
                DO UPDATE SET options_json = excluded.options_json,
                              updated_at = excluded.updated_at
                """,
                (row['option_key'], row['options_json'], row['updated_at']),
            )
            conn.commit()
        return {
            'saved': True,
            'options': normalized,
            'source_options': normalized,
            'updated_at': row['updated_at'],
        }

    def _notify_robot_name(self, profile_name: Optional[str]) -> str:
        normalized = str(profile_name or '').strip()
        if not normalized:
            return ''
        for option in self._list_notify_robot_options():
            if str(option.get('profile_name') or '').strip() == normalized:
                return str(option.get('robot_name') or normalized).strip() or normalized
        return normalized

    @staticmethod
    def _whatsapp_approval_session_account_key(account_key: str) -> str:
        normalized = re.sub(r'[^a-z0-9]+', '-', str(account_key or '').strip().lower()).strip('-')
        return normalized or 'default'

    def _whatsapp_approval_session_client_id(self, account_key: str) -> str:
        return f"wa-approval-{self._whatsapp_approval_session_account_key(account_key)}"

    def _whatsapp_approval_session_auth_path(self, account_key: str) -> Path:
        return WHATSAPP_APPROVAL_WORKER_AUTH_ACCOUNTS_DIR / self._whatsapp_approval_session_account_key(account_key)

    def _whatsapp_approval_runtime_state_path(self, account_key: str) -> Path:
        return WHATSAPP_APPROVAL_WORKER_RUNTIME_DIR / f"{self._whatsapp_approval_session_account_key(account_key)}.json"

    def _whatsapp_approval_runtime_log_path(self, account_key: str) -> Path:
        return WHATSAPP_APPROVAL_WORKER_LOG_DIR / f"{self._whatsapp_approval_session_account_key(account_key)}.log"

    def _pick_whatsapp_approval_runtime_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('127.0.0.1', 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _pid_running(pid: Any) -> bool:
        try:
            normalized = int(pid)
        except (TypeError, ValueError):
            return False
        if normalized <= 0:
            return False
        try:
            os.kill(normalized, 0)
        except OSError:
            return False
        return True

    def _list_whatsapp_approval_runtime_processes(self, auth_path: str) -> List[int]:
        normalized_auth_path = str(auth_path or '').strip()
        if not normalized_auth_path:
            return []
        try:
            result = subprocess.run(
                ['ps', '-axo', 'pid=,command='],
                capture_output=True,
                text=True,
                check=True,
            )
        except Exception:
            return []
        matched: List[int] = []
        for raw_line in str(result.stdout or '').splitlines():
            line = str(raw_line or '').strip()
            if not line or normalized_auth_path not in line:
                continue
            parts = line.split(None, 1)
            if not parts:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            if pid > 0 and pid not in matched:
                matched.append(pid)
        return matched

    def _terminate_whatsapp_approval_runtime_processes(self, pids: List[int]) -> None:
        seen: List[int] = []
        for raw_pid in pids:
            try:
                pid = int(raw_pid)
            except (TypeError, ValueError):
                continue
            if pid <= 0 or pid in seen:
                continue
            seen.append(pid)
            if not self._pid_running(pid):
                continue
            try:
                os.kill(pid, 15)
            except OSError:
                continue
        deadline = time.time() + 2.0
        while time.time() < deadline:
            remaining = [pid for pid in seen if self._pid_running(pid)]
            if not remaining:
                return
            time.sleep(0.2)
        for pid in seen:
            if not self._pid_running(pid):
                continue
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    def _read_whatsapp_approval_runtime_meta(self, account_key: str) -> Dict[str, Any]:
        path = self._whatsapp_approval_runtime_state_path(account_key)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_whatsapp_approval_runtime_meta(self, account_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        path = self._whatsapp_approval_runtime_state_path(account_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = dict(payload or {})
        row['account_key'] = str(account_key or '').strip()
        path.write_text(json.dumps(row, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return row

    def _request_whatsapp_approval_worker_health(self, base_url: str) -> Dict[str, Any]:
        normalized_base_url = str(base_url or '').strip().rstrip('/')
        if not normalized_base_url:
            raise RuntimeError('worker base_url is required')
        response = requests.get(f'{normalized_base_url}/health', timeout=10.0)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError('worker health must be a JSON object')
        return payload

    def _current_whatsapp_approval_worker_health(self) -> Dict[str, Any]:
        config = self.get_production_ops_daemon_config().get('config') or {}
        worker_base_url = str(config.get('worker_base_url') or 'http://127.0.0.1:8787').strip().rstrip('/')
        return self._request_whatsapp_approval_worker_health(worker_base_url)

    def _build_whatsapp_approval_runtime_state(
        self,
        account_key: str,
        *,
        worker_health: Optional[Dict[str, Any]] = None,
        allow_shared_fallback: bool = True,
    ) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        expected_client_id = self._whatsapp_approval_session_client_id(normalized_key)
        expected_approval_client_id = f"{expected_client_id}-approval"
        expected_auth_path = str(self._whatsapp_approval_session_auth_path(normalized_key))
        meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
        base_runtime = {
            'account_key': normalized_key,
            'mode': 'dedicated_runtime',
            'source': 'dedicated' if meta else 'shared',
            'configured': bool(meta),
            'active': False,
            'pid': meta.get('pid'),
            'port': meta.get('port'),
            'base_url': str(meta.get('base_url') or '').strip() or None,
            'auth_path': str(meta.get('auth_path') or expected_auth_path).strip(),
            'client_id': str(meta.get('client_id') or expected_client_id).strip(),
            'log_path': str(meta.get('log_path') or self._whatsapp_approval_runtime_log_path(normalized_key)).strip(),
            'meta_path': str(self._whatsapp_approval_runtime_state_path(normalized_key)),
            'started_at': meta.get('started_at'),
            'stopped_at': meta.get('stopped_at'),
            'status': 'not_started',
            'ready': False,
            'authenticated': False,
            'session_target_match': False,
            'status_text': '尚未启动独立 Runtime',
            'health_error': None,
        }
        if meta:
            pid_running = self._pid_running(meta.get('pid')) or bool(worker_health)
            if not pid_running and worker_health is None and base_runtime['base_url']:
                try:
                    worker_health = self._request_whatsapp_approval_worker_health(base_runtime['base_url'])
                    pid_running = True
                except Exception as exc:
                    base_runtime['health_error'] = str(exc)
            base_runtime['active'] = pid_running
            if pid_running:
                health_payload = worker_health
                if health_payload is None and base_runtime['base_url']:
                    try:
                        health_payload = self._request_whatsapp_approval_worker_health(base_runtime['base_url'])
                    except Exception as exc:
                        base_runtime['health_error'] = str(exc)
                if isinstance(health_payload, dict) and health_payload:
                    approval_payload = health_payload.get('approval_client') if isinstance(health_payload.get('approval_client'), dict) else {}
                    current_client_id = str(approval_payload.get('client_id') or health_payload.get('client_id') or '').strip()
                    current_auth_path = str(approval_payload.get('auth_path') or health_payload.get('auth_path') or '').strip()
                    base_runtime['status'] = str(approval_payload.get('status') or health_payload.get('status') or '').strip() or 'running'
                    base_runtime['ready'] = bool(approval_payload.get('ready'))
                    base_runtime['authenticated'] = bool(approval_payload.get('authenticated'))
                    base_runtime['session_target_match'] = bool(current_client_id in {expected_client_id, expected_approval_client_id} and current_auth_path == expected_auth_path)
                    base_runtime['status_text'] = '独立 Runtime 运行中'
                else:
                    base_runtime['status'] = 'running'
                    base_runtime['status_text'] = '独立 Runtime 已启动，健康检查暂未就绪'
            else:
                base_runtime['status'] = 'stopped'
                base_runtime['status_text'] = '独立 Runtime 已停止'
            return base_runtime

        if allow_shared_fallback:
            health_payload = worker_health
            if health_payload is None:
                try:
                    health_payload = self._current_whatsapp_approval_worker_health()
                except Exception as exc:
                    base_runtime['source'] = 'unavailable'
                    base_runtime['health_error'] = str(exc)
                    base_runtime['status'] = 'unavailable'
                    base_runtime['status_text'] = '共享 8787 worker 当前不可达'
                    return base_runtime
            approval_payload = health_payload.get('approval_client') if isinstance(health_payload.get('approval_client'), dict) else {}
            current_client_id = str(approval_payload.get('client_id') or health_payload.get('client_id') or '').strip()
            current_auth_path = str(approval_payload.get('auth_path') or health_payload.get('auth_path') or '').strip()
            config = self.get_production_ops_daemon_config().get('config') or {}
            shared_base_url = str(config.get('worker_base_url') or 'http://127.0.0.1:8787').strip().rstrip('/')
            port_match = re.search(r':(\d+)$', shared_base_url)
            base_runtime.update({
                'active': True,
                'port': int(port_match.group(1)) if port_match else None,
                'base_url': shared_base_url,
                'status': str(approval_payload.get('status') or health_payload.get('status') or '').strip() or 'shared',
                'ready': bool(approval_payload.get('ready')),
                'authenticated': bool(approval_payload.get('authenticated')),
                'session_target_match': bool(current_client_id in {expected_client_id, expected_approval_client_id} and current_auth_path == expected_auth_path),
                'status_text': '当前仍在复用共享 8787 worker',
            })
        return base_runtime

    def _build_runtime_registration_group_executor(self, base_url: str):
        from app.registration_group_webjs_executor import WebjsBridgeRegistrationGroupApprovalExecutor

        fallback = self.registration_group_approval_executor
        token = str(getattr(fallback, 'token', '') or os.getenv('REGISTRATION_GROUP_APPROVAL_WEBJS_TOKEN') or '').strip() or None
        timeout_seconds = float(getattr(fallback, 'timeout_seconds', 35.0) or 35.0)
        return WebjsBridgeRegistrationGroupApprovalExecutor(
            base_url=str(base_url or '').strip(),
            token=token,
            timeout_seconds=timeout_seconds,
        )

    def _find_whatsapp_approval_account_binding(self, *, responsible_type: str, target_group: str) -> Optional[Dict[str, Any]]:
        normalized_target = str(target_group or '').strip().lower()
        normalized_type = str(responsible_type or '').strip().lower()
        if not normalized_target or not normalized_type:
            return None
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT account_key, account_name, responsible_type, group_links, enabled FROM whatsapp_approval_accounts WHERE responsible_type = ? AND enabled = 1 ORDER BY updated_at DESC, account_key ASC",
                (normalized_type,),
            ).fetchall()
        for row in rows:
            payload = dict(row)
            try:
                bindings = json.loads(payload.get('group_links') or '[]')
            except Exception:
                bindings = []
            normalized_bindings = _normalize_group_link_bindings(bindings if isinstance(bindings, list) else [])
            for binding in normalized_bindings:
                if binding.get('enabled') is False:
                    continue
                candidates = {
                    str(binding.get('group_name') or '').strip().lower(),
                    str(binding.get('registration_group') or '').strip().lower(),
                    str(binding.get('group_id') or '').strip().lower(),
                    str(binding.get('link') or '').strip().lower(),
                }
                candidates.discard('')
                if normalized_target in candidates:
                    return {
                        'account_key': str(payload.get('account_key') or '').strip(),
                        'account_name': str(payload.get('account_name') or '').strip(),
                        'responsible_type': normalized_type,
                        'binding': dict(binding),
                    }
        return None

    def _resolve_whatsapp_approval_runtime_executor(self, *, target_group: str, responsible_type: str) -> Optional[Dict[str, Any]]:
        match = self._find_whatsapp_approval_account_binding(responsible_type=responsible_type, target_group=target_group)
        if not match:
            return None
        runtime_state = self._build_whatsapp_approval_runtime_state(match['account_key'], allow_shared_fallback=False)
        if not runtime_state.get('active') or not runtime_state.get('base_url'):
            return None
        executor = self._build_runtime_registration_group_executor(str(runtime_state.get('base_url') or ''))
        return {
            'account_key': match['account_key'],
            'account_name': match.get('account_name'),
            'binding': match.get('binding') or {},
            'runtime_state': runtime_state,
            'executor': executor,
        }

    def _render_whatsapp_approval_qr_ascii(self, qr_text: str) -> str:
        normalized_qr = str(qr_text or '').strip()
        if not normalized_qr:
            return ''
        script = "const qrcode=require('qrcode-terminal'); qrcode.generate(process.argv[1], {small:true});"
        completed = subprocess.run(
            ['node', '-e', script, normalized_qr],
            cwd=str(WHATSAPP_APPROVAL_WORKER_ROOT),
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or 'failed to render qr').strip())
        return str(completed.stdout or '').strip()

    def _build_whatsapp_approval_session_state(
        self,
        account_key: str,
        *,
        worker_health: Optional[Dict[str, Any]] = None,
        include_qr_ascii: bool = False,
    ) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        expected_client_id = self._whatsapp_approval_session_client_id(normalized_key)
        expected_approval_client_id = f'{expected_client_id}-approval'
        expected_auth_path = self._whatsapp_approval_session_auth_path(normalized_key)
        payload = dict(worker_health or {})
        approval_payload = payload.get('approval_client') if isinstance(payload.get('approval_client'), dict) else {}
        selected_payload = approval_payload if approval_payload else payload
        current_client_id = str(selected_payload.get('client_id') or payload.get('client_id') or '').strip()
        current_auth_path = str(selected_payload.get('auth_path') or payload.get('auth_path') or '').strip()
        qr_text = str(selected_payload.get('last_qr') or payload.get('last_qr') or '').strip()
        session_target_match = bool(
            current_client_id in {expected_client_id, expected_approval_client_id}
            and current_auth_path == str(expected_auth_path)
        )
        authenticated = bool(selected_payload.get('authenticated'))
        ready = bool(selected_payload.get('ready'))
        login_verified = bool(authenticated and session_target_match)
        session_status = str(selected_payload.get('status') or payload.get('status') or '').strip()
        last_error = str(selected_payload.get('last_error') or payload.get('last_error') or '').strip()
        last_disconnected_reason = str(selected_payload.get('last_disconnected_reason') or payload.get('last_disconnected_reason') or '').strip()
        combined_failure_text = ' '.join(part for part in [session_status, last_error, last_disconnected_reason] if part).lower()
        restricted_markers = (
            'smb_tos_block',
            'policy violation',
            'account can no longer use whatsapp',
            'this account can no longer use whatsapp',
            'temporarily banned',
            'permanently banned',
            'account restricted',
            'account has been banned',
        )
        if login_verified:
            login_check_status = 'passed'
            login_check_message = '账号已登录，可以正常使用。'
        elif any(marker in combined_failure_text for marker in restricted_markers):
            login_check_status = 'account_restricted'
            login_check_message = '账号疑似受限，需先在手机端核查封禁/限制状态后再处理。'
        elif qr_text:
            login_check_status = 'waiting_for_scan'
            login_check_message = '已生成二维码，等待扫码完成登录。'
        elif session_status.lower() in {'auth_failure', 'failed', 'disconnected'} or last_error or last_disconnected_reason:
            login_check_status = 'auth_failed'
            login_check_message = '登录态异常或已失效，需重新登录后再使用。'
        else:
            login_check_status = 'pending_runtime'
            login_check_message = '正在准备登录会话，请稍候。'
        session = {
            'account_key': normalized_key,
            'auth_strategy': str(selected_payload.get('auth_strategy') or payload.get('auth_strategy') or '').strip(),
            'status': session_status,
            'ready': ready,
            'authenticated': authenticated,
            'client_id': current_client_id,
            'expected_client_id': expected_client_id,
            'expected_approval_client_id': expected_approval_client_id,
            'auth_path': current_auth_path,
            'expected_auth_path': str(expected_auth_path),
            'session_target_match': session_target_match,
            'qr_available': bool(qr_text),
            'qr_text': qr_text if qr_text else None,
            'qr_ascii': None,
            'last_qr_at': selected_payload.get('last_qr_at') or payload.get('last_qr_at'),
            'bound': authenticated and session_target_match,
            'mode': 'dedicated_localauth',
            'login_verified': login_verified,
            'login_check_status': login_check_status,
            'login_check_message': login_check_message,
        }
        if include_qr_ascii and qr_text:
            session['qr_ascii'] = self._render_whatsapp_approval_qr_ascii(qr_text)
        return session

    def _get_whatsapp_approval_account_row(self, account_key: str) -> Optional[Dict[str, Any]]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT account_key, account_name, responsible_type, group_links, area, notify_profile_name, approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker, schedule_windows, enabled, verification_status, notes, updated_at FROM whatsapp_approval_accounts WHERE account_key = ?",
                (normalized_key,),
            ).fetchone()
        return dict(row) if row else None

    def get_whatsapp_approval_account_runtime(self, account_key: str) -> Dict[str, Any]:
        account_row = self._get_whatsapp_approval_account_row(account_key)
        if not account_row:
            raise HTTPException(status_code=404, detail='whatsapp approval account not found')
        runtime_state = self._build_whatsapp_approval_runtime_state(account_key)
        return {
            'account': self._build_whatsapp_approval_account_runtime(account_row, runtime_state=runtime_state),
            'runtime': runtime_state,
        }

    def start_whatsapp_approval_account_runtime(self, account_key: str, *, reset: bool = False) -> Dict[str, Any]:
        account_row = self._get_whatsapp_approval_account_row(account_key)
        if not account_row:
            raise HTTPException(status_code=404, detail='whatsapp approval account not found')
        normalized_key = str(account_key or '').strip()
        if self._read_whatsapp_approval_runtime_meta(normalized_key):
            self.stop_whatsapp_approval_account_runtime(normalized_key)
        auth_path = self._whatsapp_approval_session_auth_path(normalized_key)
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        if reset and auth_path.exists():
            shutil.rmtree(auth_path)
        port = self._pick_whatsapp_approval_runtime_port()
        base_url = f'http://127.0.0.1:{port}'
        log_path = self._whatsapp_approval_runtime_log_path(normalized_key)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update({
            'REGISTRATION_GROUP_APPROVAL_WEBJS_PORT': str(port),
            'REGISTRATION_GROUP_APPROVAL_WEBJS_HOST': '127.0.0.1',
            'REGISTRATION_GROUP_APPROVAL_WEBJS_AUTH_MODE': 'dedicated_localauth',
            'REGISTRATION_GROUP_APPROVAL_WEBJS_AUTH_DATA_PATH': str(auth_path),
            'REGISTRATION_GROUP_APPROVAL_WEBJS_CLIENT_ID': self._whatsapp_approval_session_client_id(normalized_key),
            'REGISTRATION_GROUP_APPROVAL_WEBJS_EVENT_LOG': str(log_path.with_suffix('.jsonl')),
        })
        with log_path.open('a', encoding='utf-8') as log_file:
            proc = subprocess.Popen(
                ['npm', 'start'],
                cwd=str(WHATSAPP_APPROVAL_WORKER_ROOT),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        meta = self._write_whatsapp_approval_runtime_meta(normalized_key, {
            'account_key': normalized_key,
            'pid': proc.pid,
            'port': port,
            'base_url': base_url,
            'auth_path': str(auth_path),
            'client_id': self._whatsapp_approval_session_client_id(normalized_key),
            'log_path': str(log_path),
            'started_at': utc_now(),
            'reset': reset,
        })
        deadline = time.time() + 20.0
        worker_health: Dict[str, Any] = {}
        last_error = ''
        while time.time() < deadline:
            try:
                worker_health = self._request_whatsapp_approval_worker_health(base_url)
                break
            except Exception as exc:
                last_error = str(exc)
                time.sleep(0.5)
        if not worker_health:
            self.stop_whatsapp_approval_account_runtime(normalized_key)
            raise HTTPException(status_code=500, detail=last_error or 'failed to start dedicated whatsapp approval runtime')
        runtime_state = self._build_whatsapp_approval_runtime_state(normalized_key, worker_health=worker_health, allow_shared_fallback=False)
        return {
            'started': True,
            'reset': reset,
            'account': self._build_whatsapp_approval_account_runtime(account_row, runtime_state=runtime_state, worker_health=worker_health),
            'runtime': runtime_state,
            'meta': meta,
        }

    def stop_whatsapp_approval_account_runtime(self, account_key: str) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        meta = self._read_whatsapp_approval_runtime_meta(normalized_key)
        pid = meta.get('pid')
        auth_path = str(meta.get('auth_path') or self._whatsapp_approval_session_auth_path(normalized_key)).strip()
        runtime_pids: List[int] = []
        if pid:
            try:
                runtime_pids.append(int(pid))
            except (TypeError, ValueError):
                pass
        runtime_pids.extend(self._list_whatsapp_approval_runtime_processes(auth_path))
        self._terminate_whatsapp_approval_runtime_processes(runtime_pids)
        if meta:
            meta['stopped_at'] = utc_now()
            self._write_whatsapp_approval_runtime_meta(normalized_key, meta)
        runtime_state = self._build_whatsapp_approval_runtime_state(normalized_key, allow_shared_fallback=False)
        runtime_state['active'] = False
        runtime_state['status'] = 'stopped'
        runtime_state['status_text'] = '独立 Runtime 已停止'
        return {
            'stopped': True,
            'runtime': runtime_state,
        }

    def get_whatsapp_approval_account_session(self, account_key: str, *, include_qr_ascii: bool = True) -> Dict[str, Any]:
        account_row = self._get_whatsapp_approval_account_row(account_key)
        if not account_row:
            raise HTTPException(status_code=404, detail='whatsapp approval account not found')
        runtime_state = self._build_whatsapp_approval_runtime_state(account_key)
        worker_health = {}
        if runtime_state.get('active') and runtime_state.get('base_url') and runtime_state.get('source') == 'dedicated':
            worker_health = self._request_whatsapp_approval_worker_health(str(runtime_state.get('base_url') or ''))
        elif runtime_state.get('source') == 'shared':
            worker_health = self._current_whatsapp_approval_worker_health()
        return {
            'account': self._build_whatsapp_approval_account_runtime(account_row, runtime_state=runtime_state, worker_health=worker_health),
            'runtime': runtime_state,
            'session': self._build_whatsapp_approval_session_state(account_key, worker_health=worker_health, include_qr_ascii=include_qr_ascii),
        }

    def start_whatsapp_approval_account_session(self, account_key: str, *, reset: bool = False) -> Dict[str, Any]:
        account_row = self._get_whatsapp_approval_account_row(account_key)
        if not account_row:
            raise HTTPException(status_code=404, detail='whatsapp approval account not found')
        runtime_result = self.start_whatsapp_approval_account_runtime(account_key, reset=reset)
        runtime_state = runtime_result.get('runtime') or {}
        base_url = str(runtime_state.get('base_url') or '').strip()
        worker_health: Dict[str, Any] = {}
        try:
            response = requests.post(f'{base_url}/warmup', timeout=15.0)
            response.raise_for_status()
            worker_health = response.json()
            if not isinstance(worker_health, dict):
                raise HTTPException(status_code=500, detail='runtime warmup must return a JSON object')
        except Exception:
            worker_health = self._request_whatsapp_approval_worker_health(base_url)
        runtime_state = self._build_whatsapp_approval_runtime_state(account_key, worker_health=worker_health, allow_shared_fallback=False)
        return {
            'started': True,
            'reset': reset,
            'account': self._build_whatsapp_approval_account_runtime(account_row, runtime_state=runtime_state, worker_health=worker_health),
            'runtime': runtime_state,
            'session': self._build_whatsapp_approval_session_state(account_key, worker_health=worker_health, include_qr_ascii=True),
        }

    def reset_whatsapp_approval_account_session(self, account_key: str) -> Dict[str, Any]:
        return self.start_whatsapp_approval_account_session(account_key, reset=True)

    def _upsert_intake_bot_preset_row(self, *, profile_name: str, app_id: Optional[str], robot_name: Optional[str], default_app: str, default_guild: str, enabled: int = 1) -> Dict[str, Any]:
        normalized_profile_name = str(profile_name or '').strip()
        normalized_robot_name = str(robot_name or '').strip() or normalized_profile_name
        row = {
            'profile_name': normalized_profile_name,
            'app_id': str(app_id or '').strip(),
            'robot_name': normalized_robot_name,
            'default_app': str(default_app or '').strip(),
            'default_guild': str(default_guild or '').strip(),
            'enabled': int(enabled),
            'updated_at': utc_now(),
        }
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO intake_bot_presets (profile_name, app_id, robot_name, default_app, default_guild, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_name)
                DO UPDATE SET app_id = excluded.app_id,
                              robot_name = excluded.robot_name,
                              default_app = excluded.default_app,
                              default_guild = excluded.default_guild,
                              enabled = excluded.enabled,
                              updated_at = excluded.updated_at
                """,
                (row['profile_name'], row['app_id'], row['robot_name'], row['default_app'], row['default_guild'], row['enabled'], row['updated_at']),
            )
            conn.commit()
        return row

    def list_guild_executors(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT guild_name, backend_url, login_username, proxy_url, proxy_region, proxy_type, enabled, browser_profile_key, bind_concurrency, request_timeout_seconds, notes, updated_at, CASE WHEN COALESCE(password_secret_ref, '') != '' THEN 1 ELSE 0 END AS password_configured FROM guild_executors ORDER BY guild_name ASC"
            ).fetchall()]
        for row in rows:
            row['enabled'] = bool(row.get('enabled'))
            row['password_configured'] = bool(row.get('password_configured'))
        return {
            'rows': rows,
            'proxy_region_options': GUILD_EXECUTOR_PROXY_REGION_OPTIONS,
        }

    def _official_group_bridge_console_base_url(self) -> Optional[str]:
        webhook_url = str(self.official_group_approval_webhook_url or '').strip()
        if not webhook_url:
            return None
        if webhook_url.endswith('/official-group/approve'):
            return webhook_url[:-len('/official-group/approve')]
        return webhook_url.rstrip('/')

    def _official_group_bridge_summary_payload(self) -> Dict[str, Any]:
        base_url = self._official_group_bridge_console_base_url()
        if not base_url:
            return {'configured': False, 'health': {}, 'summary': {}}
        def _get_json(url: str) -> Dict[str, Any]:
            response = requests.get(url, timeout=10.0)
            response.raise_for_status()
            return response.json()
        try:
            health = _get_json(f'{base_url}/ops/official-group-bridge/health')
        except Exception as exc:
            health = {'status': 'unreachable', 'error': str(exc)}
        try:
            summary = _get_json(f'{base_url}/ops/official-group-bridge/summary')
        except Exception as exc:
            summary = {'status': 'unreachable', 'error': str(exc)}
        return {
            'configured': True,
            'base_url': base_url,
            'health': health,
            'summary': summary,
        }

    def _current_local_minutes(self) -> int:
        now = datetime.now().astimezone()
        return (now.hour * 60) + now.minute

    def _schedule_window_contains_minutes(self, start: str, end: str, minute_of_day: int) -> bool:
        start_hour, start_minute = [int(part) for part in start.split(':', 1)]
        end_hour, end_minute = [int(part) for part in end.split(':', 1)]
        start_total = (start_hour * 60) + start_minute
        end_total = (end_hour * 60) + end_minute
        if start_total <= end_total:
            return start_total <= minute_of_day <= end_total
        return minute_of_day >= start_total or minute_of_day <= end_total

    def _schedule_runtime(self, schedule_windows: List[Dict[str, Any]]) -> Dict[str, Any]:
        normalized_windows: List[Dict[str, str]] = []
        for item in schedule_windows or []:
            start = str((item or {}).get('start') or '').strip()
            end = str((item or {}).get('end') or '').strip()
            if re.fullmatch(r'\d{2}:\d{2}', start) and re.fullmatch(r'\d{2}:\d{2}', end):
                normalized_windows.append({'start': start, 'end': end})
        if not normalized_windows:
            return {
                'configured': False,
                'active_now': True,
                'status': 'always_on',
                'label': '未设置时间段（默认全天）',
            }
        current_minute = self._current_local_minutes()
        active_now = any(self._schedule_window_contains_minutes(item['start'], item['end'], current_minute) for item in normalized_windows)
        return {
            'configured': True,
            'active_now': active_now,
            'status': 'active_window' if active_now else 'outside_window',
            'label': '当前时段生效中' if active_now else '当前不在监控时段',
        }

    def _production_ops_daemon_snapshot(self) -> Dict[str, Any]:
        return self.get_production_ops_daemon_config()

    @staticmethod
    def _extract_live_group_probe(production_ops: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        runtime = dict((production_ops or {}).get('runtime') or {})
        status = dict(runtime.get('status') or {})
        candidates = [
            ('decision_group_state', ((status.get('decision_group_state') or {}).get('payload') if isinstance(status.get('decision_group_state'), dict) else None)),
            ('fresh_probe', ((status.get('fresh_probe') or {}).get('payload') if isinstance(status.get('fresh_probe'), dict) else None)),
            ('worker_state', ((status.get('worker_state') or {}).get('payload') if isinstance(status.get('worker_state'), dict) else None)),
        ]
        for source, payload in candidates:
            if not isinstance(payload, dict):
                continue
            group_name = str(payload.get('group_name') or '').strip()
            group_id = str(payload.get('group_id') or '').strip()
            pending_count = payload.get('pending_count')
            member_count = payload.get('member_count')
            if group_name or group_id or pending_count is not None or member_count is not None:
                return {
                    'source': source,
                    'group_name': group_name,
                    'group_id': group_id,
                    'pending_count': pending_count,
                    'member_count': member_count,
                    'requester_ids': list(payload.get('requester_ids') or []),
                    'requesters': list(payload.get('requesters') or []),
                }
        return {
            'source': None,
            'group_name': '',
            'group_id': '',
            'pending_count': None,
            'member_count': None,
            'requester_ids': [],
            'requesters': [],
        }

    def _approval_membership_verifier_state(self, *, responsible_type: str, production_ops: Optional[Dict[str, Any]] = None, official_bridge: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if str(responsible_type or '').strip() != 'registration_group':
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'not_supported',
                'detail': '官方群当前仅接入 bridge 健康度与请求队列摘要，真实群成员/管理员权限校验执行器尚未接入。',
                'source': 'official_group_bridge_only',
                'probe': {},
            }
        executor_health = self.registration_group_approval_executor_health()
        supports = {str(item).strip() for item in (executor_health.get('supports') or []) if str(item).strip()}
        probe = self._extract_live_group_probe(production_ops)
        has_live_probe = bool(probe.get('group_name') or probe.get('group_id') or probe.get('member_count') is not None)
        ready = bool(has_live_probe and ('strict_queue_and_member_verify' in supports or 'approve' in supports))
        if ready:
            group_label = str(probe.get('group_name') or probe.get('group_id') or '-').strip() or '-'
            detail = self._format_group_probe_ready_detail(
                scope_text='注册群',
                probe_label=group_label,
                pending_count=probe.get('pending_count'),
                member_count=probe.get('member_count'),
                executor_text='共享执行器',
            )
            status = 'live_probe_ready'
            requires_manual_seed = False
        elif executor_health.get('configured'):
            detail = '注册群审批执行器已配置，但当前未拿到可用的实时群状态探针结果；暂不能判定真实成员/管理员权限。'
            status = 'probe_unavailable'
            requires_manual_seed = True
        else:
            detail = '注册群审批执行器未配置，暂不能做真实群成员/管理员权限校验。'
            status = 'executor_unconfigured'
            requires_manual_seed = True
        return {
            'ready': ready,
            'requires_manual_seed': requires_manual_seed,
            'status': status,
            'detail': detail,
            'source': probe.get('source'),
            'probe': probe,
        }

    @staticmethod
    def _format_group_probe_ready_detail(
        *,
        scope_text: str,
        probe_label: str,
        pending_count: Any,
        member_count: Any,
        executor_text: str,
        suffix: str = '',
    ) -> str:
        pending_text = pending_count if pending_count is not None else '-'
        return f'已接入群状态探针：待审批 {pending_text} 人。已有管理员权限。'

    @staticmethod
    def _binding_membership_verifier_state(
        binding: Dict[str, Any],
        account_verifier: Dict[str, Any],
        *,
        responsible_type: str,
        production_ops: Optional[Dict[str, Any]] = None,
        live_probe: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if str(responsible_type or '').strip() != 'registration_group':
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'not_supported',
                'detail': '官方群绑定当前不支持逐群真实成员/管理员权限校验。',
            }
        if binding.get('enabled') is False:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'monitor_disabled',
                'detail': '该群绑定未开启监控。',
            }
        binding_probe = dict(live_probe or {})
        binding_probe_error = str(binding_probe.get('error') or '').strip()
        has_binding_probe = bool(
            binding_probe.get('group_name')
            or binding_probe.get('group_id')
            or binding_probe.get('pending_count') is not None
            or binding_probe.get('member_count') is not None
        )
        if has_binding_probe:
            probe = binding_probe
        else:
            probe = dict(account_verifier.get('probe') or {})
            if not account_verifier.get('ready'):
                if binding_probe_error:
                    return {
                        'ready': False,
                        'requires_manual_seed': True,
                        'status': 'probe_unavailable',
                        'detail': f'群状态探针读取失败：{binding_probe_error}',
                        'source': binding_probe.get('source') or binding_probe.get('source_base_url'),
                        'probe': binding_probe,
                    }
                return {
                    'ready': False,
                    'requires_manual_seed': True,
                    'status': account_verifier.get('status') or 'probe_unavailable',
                    'detail': account_verifier.get('detail') or '当前未拿到共享执行器实时探针结果。',
                }
        binding_group = str(binding.get('registration_group') or '').strip()
        binding_group_id = str(binding.get('group_id') or '').strip()
        binding_link = str(binding.get('link') or '').strip()
        probe_group = str(probe.get('group_name') or '').strip()
        probe_group_id = str(probe.get('group_id') or '').strip()
        runtime = dict((production_ops or {}).get('runtime') or {})
        status = dict(runtime.get('status') or {})
        monitor_target = dict(status.get('monitor_target') or {}) if isinstance(status.get('monitor_target'), dict) else {}
        monitor_registration_group = str(monitor_target.get('registration_group') or '').strip()
        monitor_group_id = str(((status.get('decision_group_state') or {}).get('payload') or {}).get('group_id') or '').strip() if isinstance(status.get('decision_group_state'), dict) else ''
        monitor_binding_link = str(monitor_target.get('binding_link') or '').strip()
        monitor_group_name = str(monitor_target.get('group_name') or monitor_target.get('binding_group_name') or '').strip()
        if binding_group or binding_group_id:
            group_ok = (not binding_group) or (binding_group and binding_group == probe_group)
            group_id_ok = (not binding_group_id) or (binding_group_id and binding_group_id == probe_group_id)
            if group_ok and group_id_ok:
                probe_label = str(binding_group or probe_group or binding_group_id or probe_group_id or '-').strip() or '-'
                suffix_parts = []
                if binding_group_id or probe_group_id:
                    suffix_parts.append(f'当前群ID：{binding_group_id or probe_group_id}')
                return {
                    'ready': True,
                    'requires_manual_seed': False,
                    'status': 'mapped_live_probe_ready',
                    'detail': Service._format_group_probe_ready_detail(
                        scope_text='注册群',
                        probe_label=probe_label,
                        pending_count=probe.get('pending_count'),
                        member_count=probe.get('member_count'),
                        executor_text='共享执行器',
                        suffix='；'.join(suffix_parts),
                    ),
                    'source': probe.get('source') or probe.get('source_base_url'),
                    'probe': probe,
                }
            if not has_binding_probe:
                monitor_target_matches_binding = any([
                    bool(binding_link and monitor_binding_link and binding_link == monitor_binding_link),
                    bool(binding_group and monitor_registration_group and binding_group == monitor_registration_group),
                    bool(binding_group_id and monitor_group_id and binding_group_id == monitor_group_id),
                ])
                if monitor_binding_link or monitor_registration_group or monitor_group_id:
                    if not monitor_target_matches_binding:
                        target_label = monitor_group_name or monitor_binding_link or monitor_registration_group or monitor_group_id or '-'
                        return {
                            'ready': False,
                            'requires_manual_seed': True,
                            'status': 'other_binding_live_probe_active',
                            'detail': f'当前真实探针正在读取另一条已启用群绑定：{target_label}。当前群尚未切到该探针。',
                        }
            mismatch = []
            if binding_group and binding_group != probe_group:
                mismatch.append(f'registration_group={binding_group} ≠ {probe_group or "-"}')
            if binding_group_id and binding_group_id != probe_group_id:
                mismatch.append(f'group_id={binding_group_id} ≠ {probe_group_id or "-"}')
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'mapping_mismatch',
                'detail': '绑定映射与当前真实探针不一致：' + '；'.join(mismatch),
                'source': probe.get('source') or probe.get('source_base_url'),
                'probe': probe,
            }
        if binding_probe_error and not has_binding_probe:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'probe_unavailable',
                'detail': f'群状态探针读取失败：{binding_probe_error}',
                'source': binding_probe.get('source') or binding_probe.get('source_base_url'),
                'probe': binding_probe,
            }
        return {
            'ready': True,
            'requires_manual_seed': False,
            'status': 'inferred_live_probe_ready',
            'detail': '',
            'source': probe.get('source') or probe.get('source_base_url'),
            'probe': probe,
        }

    def _official_group_binding_membership_verifier_state(
        self,
        binding: Dict[str, Any],
        *,
        runtime_state: Dict[str, Any],
        session_state: Dict[str, Any],
        live_probe: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if binding.get('enabled') is False:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'monitor_disabled',
                'detail': '该群绑定未开启监控。',
                'source': None,
                'probe': {},
            }
        base_url = str(runtime_state.get('base_url') or '').strip()
        if not bool(runtime_state.get('active')) or not base_url:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'runtime_unavailable',
                'detail': '当前账号扫码服务未就绪，暂不能做逐群真实校验。',
                'source': None,
                'probe': {},
            }
        if not bool(session_state.get('login_verified')):
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'login_unready',
                'detail': '当前账号未完成登录校验，暂不能做逐群真实校验。',
                'source': None,
                'probe': {},
            }
        binding_target = (
            str(binding.get('group_id') or '').strip()
            or str(binding.get('link') or '').strip()
            or str(binding.get('registration_group') or '').strip()
            or str(binding.get('group_name') or '').strip()
        )
        if not binding_target:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'binding_target_missing',
                'detail': '当前群绑定缺少可用于逐群校验的目标标识。',
                'source': None,
                'probe': {},
            }
        probe = dict(live_probe or {})
        if probe.get('error') and not (probe.get('group_name') or probe.get('group_id')):
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'probe_unavailable',
                'detail': f"群状态探针读取失败：{probe.get('error')}",
                'source': 'official_group_runtime_group_state',
                'probe': {},
            }
        if not (probe.get('group_name') or probe.get('group_id')):
            try:
                probe = self._request_whatsapp_approval_group_state_with_retry(base_url, binding_target)
            except Exception as exc:
                return {
                    'ready': False,
                    'requires_manual_seed': True,
                    'status': 'probe_unavailable',
                    'detail': f'群状态探针读取失败：{exc}',
                    'source': 'official_group_runtime_group_state',
                    'probe': {},
                }
        probe_payload = {
            'source': 'official_group_runtime_group_state',
            'group_name': str(probe.get('group_name') or '').strip(),
            'group_id': str(probe.get('group_id') or '').strip(),
            'pending_count': probe.get('pending_count'),
            'member_count': probe.get('member_count'),
            'requester_ids': list(probe.get('requester_ids') or []),
            'requesters': list(probe.get('requesters') or []),
        }
        live_group_name = str(probe_payload.get('group_name') or '').strip()
        configured_group_name = str(binding.get('group_name') or '').strip()
        detail_suffix = ''
        if configured_group_name and live_group_name and configured_group_name != live_group_name:
            detail_suffix = f'当前配置名为 {configured_group_name}，实时群名为 {live_group_name}。'
        detail = self._format_group_probe_ready_detail(
            scope_text='官方群',
            probe_label=live_group_name or configured_group_name or binding_target,
            pending_count=probe_payload.get('pending_count'),
            member_count=probe_payload.get('member_count'),
            executor_text='dedicated runtime',
            suffix=detail_suffix,
        )
        return {
            'ready': True,
            'requires_manual_seed': False,
            'status': 'live_probe_ready',
            'detail': detail,
            'source': 'official_group_runtime_group_state',
            'probe': probe_payload,
        }

    @staticmethod
    def _official_group_account_membership_verifier(
        binding_verifiers: List[Dict[str, Any]],
        *,
        enabled_binding_count: int,
    ) -> Dict[str, Any]:
        monitored = [item for item in binding_verifiers if item.get('status') != 'monitor_disabled']
        if enabled_binding_count <= 0:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'monitor_disabled',
                'detail': '当前未开启任何官方群绑定监控。',
                'source': None,
                'probe': {},
                'binding_count': 0,
            }
        if not monitored:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'probe_unavailable',
                'detail': '当前没有可用于逐群真实校验的已开启群绑定。',
                'source': None,
                'probe': {},
                'binding_count': 0,
            }
        ready_count = sum(1 for item in monitored if item.get('ready'))
        if ready_count == len(monitored):
            first_probe = dict(monitored[0].get('probe') or {}) if monitored else {}
            return {
                'ready': True,
                'requires_manual_seed': False,
                'status': 'live_probe_ready',
                'detail': Service._format_group_probe_ready_detail(
                    scope_text='官方群',
                    probe_label=str(first_probe.get('group_name') or first_probe.get('group_id') or '-').strip() or '-',
                    pending_count=first_probe.get('pending_count'),
                    member_count=first_probe.get('member_count'),
                    executor_text='dedicated runtime',
                ),
                'source': 'official_group_runtime_group_state',
                'probe': first_probe,
                'binding_count': len(monitored),
            }
        first_failed = next((item for item in monitored if not item.get('ready')), monitored[0])
        return {
            'ready': False,
            'requires_manual_seed': True,
            'status': first_failed.get('status') or 'probe_unavailable',
            'detail': f'当前仅有 {ready_count}/{len(monitored)} 条官方群绑定完成真实成员/管理员权限校验；{first_failed.get("detail") or "仍有绑定未拿到实时探针结果。"}',
            'source': first_failed.get('source'),
            'probe': dict(first_failed.get('probe') or {}),
            'binding_count': len(monitored),
        }

    def _build_whatsapp_approval_account_runtime(self, row: Dict[str, Any], *, production_ops: Optional[Dict[str, Any]] = None, official_bridge: Optional[Dict[str, Any]] = None, worker_health: Optional[Dict[str, Any]] = None, runtime_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        serialized = dict(row)
        raw_group_links = []
        try:
            raw_group_links = json.loads(serialized.get('group_links') or '[]')
        except Exception:
            raw_group_links = []
        if not isinstance(raw_group_links, list):
            raw_group_links = []
        default_area = str(serialized.get('area') or '').strip()
        default_notify_profile_name = str(serialized.get('notify_profile_name') or '').strip()
        legacy_count_threshold, legacy_timeout_minutes = _legacy_approval_thresholds(serialized.get('approval_rule'))
        default_approval_count_threshold = _coerce_positive_int(serialized.get('approval_count_threshold'), legacy_count_threshold)
        default_approval_timeout_minutes = _coerce_positive_int(serialized.get('approval_timeout_minutes'), legacy_timeout_minutes)
        default_auto_recover_worker = bool(serialized.get('auto_recover_worker'))
        account_schedule_windows = _normalize_schedule_windows_payload(json.loads(serialized.get('schedule_windows') or '[]') if str(serialized.get('schedule_windows') or '').strip() else []) if isinstance(serialized.get('schedule_windows'), str) else _normalize_schedule_windows_payload(serialized.get('schedule_windows') or [])

        group_link_bindings: list[dict[str, Any]] = []
        for item in raw_group_links:
            if isinstance(item, dict):
                link = str(item.get('link') or '').strip()
                area = str(item.get('area') or '').strip()
                registration_group = str(item.get('registration_group') or '').strip()
                group_id = str(item.get('group_id') or '').strip()
                if link:
                    group_link_bindings.append({
                        'link': link,
                        'group_name': str(item.get('group_name') or '').strip(),
                        'area': area,
                        'notify_profile_name': str(item.get('notify_profile_name') or default_notify_profile_name).strip(),
                        'enabled': False if item.get('enabled') is False else True,
                        'registration_group': registration_group,
                        'group_id': group_id,
                        'approval_count_threshold': item.get('approval_count_threshold'),
                        'approval_timeout_minutes': item.get('approval_timeout_minutes'),
                        'auto_recover_worker': item.get('auto_recover_worker'),
                        'schedule_windows': item.get('schedule_windows') if isinstance(item.get('schedule_windows'), list) else account_schedule_windows,
                    })
            else:
                link = str(item or '').strip()
                if link:
                    group_link_bindings.append({
                        'link': link,
                        'group_name': '',
                        'area': default_area,
                        'notify_profile_name': default_notify_profile_name,
                        'registration_group': '',
                        'group_id': '',
                        'approval_count_threshold': default_approval_count_threshold,
                        'approval_timeout_minutes': default_approval_timeout_minutes,
                        'auto_recover_worker': default_auto_recover_worker,
                        'schedule_windows': account_schedule_windows,
                    })

        group_link_bindings = _normalize_group_link_bindings(group_link_bindings)
        for item in group_link_bindings:
            item['notify_profile_name'] = str(item.get('notify_profile_name') or default_notify_profile_name).strip()
            item['approval_count_threshold'] = _coerce_positive_int(item.get('approval_count_threshold'), default_approval_count_threshold)
            item['approval_timeout_minutes'] = _coerce_positive_int(item.get('approval_timeout_minutes'), default_approval_timeout_minutes)
            item['auto_recover_worker'] = default_auto_recover_worker if item.get('auto_recover_worker') is None else bool(item.get('auto_recover_worker'))
            item['schedule_windows'] = _normalize_schedule_windows_payload(item.get('schedule_windows') or account_schedule_windows)
            item['schedule_runtime'] = self._schedule_runtime(item['schedule_windows'])
            item['notify_robot_name'] = self._notify_robot_name(item.get('notify_profile_name'))
            item['approval_rule_text'] = _approval_condition_text(item['approval_count_threshold'], item['approval_timeout_minutes'])
        serialized['group_link_bindings'] = group_link_bindings
        serialized['group_links'] = [str(item.get('link') or '').strip() for item in group_link_bindings if str(item.get('link') or '').strip()]
        if not default_area and group_link_bindings:
            default_area = str(group_link_bindings[0].get('area') or '').strip()
        serialized['enabled'] = bool(serialized.get('enabled'))
        serialized['group_count'] = len(serialized['group_links'])

        production_ops = production_ops or self._production_ops_daemon_snapshot()
        production_runtime = production_ops.get('runtime') or {}
        production_status = production_runtime.get('status') or {}
        official_bridge = official_bridge or self._official_group_bridge_summary_payload()
        official_health = official_bridge.get('health') or {}
        official_summary = official_bridge.get('summary') or {}

        invalid_group_links = []
        invalid_binding_areas = []
        missing_binding_notify = []
        enabled_binding_count = 0
        binding_runtime_rows: list[dict[str, Any]] = []
        for item in group_link_bindings:
            link = str(item.get('link') or '').strip()
            area = str(item.get('area') or '').strip()
            notify_profile_name = str(item.get('notify_profile_name') or '').strip()
            registration_group = str(item.get('registration_group') or '').strip()
            group_id = str(item.get('group_id') or '').strip()
            binding_enabled = bool(item.get('enabled', True))
            link_ok = bool(re.fullmatch(r'https://chat\.whatsapp\.com/[A-Za-z0-9_-]+', link))
            if not link_ok:
                invalid_group_links.append(link)
            if not area:
                invalid_binding_areas.append(link)
            if not notify_profile_name:
                missing_binding_notify.append(link)
            if binding_enabled:
                enabled_binding_count += 1
            binding_runtime_rows.append({
                'link': link,
                'group_name': str(item.get('group_name') or '').strip(),
                'area': area,
                'notify_profile_name': notify_profile_name,
                'enabled': binding_enabled,
                'registration_group': registration_group,
                'group_id': group_id,
                'notify_robot_name': item.get('notify_robot_name') or self._notify_robot_name(notify_profile_name),
                'approval_count_threshold': item.get('approval_count_threshold'),
                'approval_timeout_minutes': item.get('approval_timeout_minutes'),
                'approval_rule_text': item.get('approval_rule_text') or _approval_condition_text(
                    _coerce_positive_int(item.get('approval_count_threshold'), default_approval_count_threshold),
                    _coerce_positive_int(item.get('approval_timeout_minutes'), default_approval_timeout_minutes),
                ),
                'auto_recover_worker': bool(item.get('auto_recover_worker')),
                'schedule_windows': item.get('schedule_windows') or [],
                'schedule_runtime': item.get('schedule_runtime') or self._schedule_runtime(item.get('schedule_windows') or []),
                'link_ok': link_ok,
            })

        if serialized.get('responsible_type') == 'registration_group':
            service_ready = True
            service_scope = {
                'code': 'registration_group_console',
                'label': '注册群账号级守护',
                'ready': service_ready,
                'detail': '当前账号已具备独立守护配置；共享 daemon 运行态请以上方实时状态卡片为准' if service_ready else '当前账号守护配置未就绪',
                'runtime': {
                    'launch_agent_installed': bool(production_runtime.get('launch_agent_installed')),
                    'checked_at': (production_status.get('status') or {}).get('checked_at') if isinstance(production_status.get('status'), dict) else production_status.get('checked_at'),
                    'runtime_mode': 'shared-daemon-status + account-scoped-configuration',
                },
            }
        else:
            service_ready = official_bridge.get('configured') and official_health.get('status') == 'healthy'
            service_scope = {
                'code': 'official_group_bridge',
                'label': '官方群审批桥接台',
                'ready': bool(service_ready),
                'detail': '官方群 bridge 健康，可继续接统一调度' if service_ready else '官方群 bridge 未就绪，需先恢复 bridge 服务',
                'runtime': {
                    'mode': official_health.get('mode'),
                    'pending_count': official_summary.get('pending_count'),
                    'resolved_count': official_summary.get('resolved_count'),
                },
            }

        active_binding_count = sum(1 for item in binding_runtime_rows if item.get('enabled') and (item.get('schedule_runtime') or {}).get('active_now'))
        all_binding_areas_ok = not invalid_binding_areas
        all_binding_notify_ok = not missing_binding_notify
        all_binding_rules_ok = all(
            _coerce_positive_int(item.get('approval_count_threshold'), default_approval_count_threshold) > 0
            and _coerce_positive_int(item.get('approval_timeout_minutes'), default_approval_timeout_minutes) > 0
            for item in binding_runtime_rows
        )
        has_monitored_bindings = enabled_binding_count > 0
        runtime_state = runtime_state or self._build_whatsapp_approval_runtime_state(serialized.get('account_key') or '', worker_health=worker_health)
        if runtime_state.get('active') and worker_health:
            session_state = self._build_whatsapp_approval_session_state(serialized.get('account_key') or '', worker_health=worker_health, include_qr_ascii=False)
        else:
            session_state = self._build_whatsapp_approval_session_state(serialized.get('account_key') or '', worker_health=worker_health if runtime_state.get('source') == 'shared' else {}, include_qr_ascii=False)

        responsible_type = str(serialized.get('responsible_type') or '').strip()
        original_group_link_bindings = [dict(item or {}) for item in (serialized.get('group_link_bindings') or [])]
        binding_live_probes: list[dict[str, Any]] = []
        for item in original_group_link_bindings:
            probe = self._apply_live_group_identity_to_binding(
                dict(item or {}),
                responsible_type=responsible_type,
                runtime_state=runtime_state,
                session_state=session_state,
                allow_shared_fallback=responsible_type == 'registration_group',
                attempts=1 if responsible_type == 'registration_group' else 3,
                timeout_seconds=2.0,
            )
            binding_live_probes.append(probe if isinstance(probe, dict) else {})
        for runtime_row, binding, probe in zip(binding_runtime_rows, original_group_link_bindings, binding_live_probes):
            live_group_name = str((probe or {}).get('group_name') or '').strip()
            live_group_id = str((probe or {}).get('group_id') or '').strip()
            runtime_row['runtime_probe_group_name'] = live_group_name
            runtime_row['runtime_probe_group_id'] = live_group_id
            runtime_row['group_name'] = live_group_name or str(binding.get('group_name') or '').strip()
            runtime_row['group_id'] = live_group_id or str(binding.get('group_id') or '').strip()
            runtime_row.update(self._build_binding_next_approval_runtime(
                responsible_type=responsible_type,
                binding=runtime_row,
                probe=probe if isinstance(probe, dict) else {},
            ))

        membership_verifier = self._approval_membership_verifier_state(
            responsible_type=str(serialized.get('responsible_type') or '').strip(),
            production_ops=production_ops,
            official_bridge=official_bridge,
        )
        if str(serialized.get('responsible_type') or '').strip() == 'official_group':
            binding_verifiers = [
                self._official_group_binding_membership_verifier_state(
                    item,
                    runtime_state=runtime_state,
                    session_state=session_state,
                    live_probe=probe,
                )
                for item, probe in zip(binding_runtime_rows, binding_live_probes)
            ]
            account_membership_verifier = self._official_group_account_membership_verifier(
                binding_verifiers,
                enabled_binding_count=enabled_binding_count,
            )
        else:
            binding_verifiers = [
                self._binding_membership_verifier_state(
                    item,
                    membership_verifier,
                    responsible_type=str(serialized.get('responsible_type') or '').strip(),
                    production_ops=production_ops,
                    live_probe=probe,
                )
                for item, probe in zip(binding_runtime_rows, binding_live_probes)
            ]
            monitored_binding_verifiers = [
                verifier for item, verifier in zip(binding_runtime_rows, binding_verifiers) if item.get('enabled')
            ]
            ready_binding_verifiers = [verifier for verifier in monitored_binding_verifiers if verifier.get('ready')]
            bindings_membership_ready = bool(monitored_binding_verifiers) and len(ready_binding_verifiers) == len(monitored_binding_verifiers)
            if not monitored_binding_verifiers:
                bindings_membership_ready = bool(membership_verifier.get('ready')) if not binding_runtime_rows else False
            if monitored_binding_verifiers:
                if bindings_membership_ready:
                    representative_verifier = ready_binding_verifiers[0]
                    account_membership_verifier = {
                        **membership_verifier,
                        'ready': True,
                        'requires_manual_seed': False,
                        'status': representative_verifier.get('status') or membership_verifier.get('status') or 'live_probe_ready',
                        'detail': representative_verifier.get('detail') or membership_verifier.get('detail') or '-',
                        'source': representative_verifier.get('source') or membership_verifier.get('source'),
                        'probe': dict(representative_verifier.get('probe') or membership_verifier.get('probe') or {}),
                        'binding_count': len(monitored_binding_verifiers),
                    }
                else:
                    first_failed = next((item for item in monitored_binding_verifiers if not item.get('ready')), monitored_binding_verifiers[0])
                    ready_count = len(ready_binding_verifiers)
                    account_membership_verifier = {
                        **membership_verifier,
                        'ready': False,
                        'requires_manual_seed': True,
                        'status': first_failed.get('status') or membership_verifier.get('status') or 'probe_unavailable',
                        'detail': f'当前仅有 {ready_count}/{len(monitored_binding_verifiers)} 条注册群绑定完成真实成员/管理员权限校验；{first_failed.get("detail") or membership_verifier.get("detail") or "仍有绑定未拿到实时探针结果。"}',
                        'source': first_failed.get('source') or membership_verifier.get('source'),
                        'probe': dict(first_failed.get('probe') or membership_verifier.get('probe') or {}),
                        'binding_count': len(monitored_binding_verifiers),
                    }
            else:
                account_membership_verifier = {
                    **membership_verifier,
                    'ready': bindings_membership_ready,
                    'requires_manual_seed': bool(membership_verifier.get('requires_manual_seed')),
                    'binding_count': len(monitored_binding_verifiers),
                }
        for item, verifier in zip(binding_runtime_rows, binding_verifiers):
            item['membership_verifier'] = verifier

        if responsible_type in {'registration_group', 'official_group'}:
            updated_bindings = self._persist_registration_group_binding_live_names(
                str(serialized.get('account_key') or '').strip(),
                original_group_link_bindings,
                binding_runtime_rows,
                binding_verifiers,
            )
            if updated_bindings != original_group_link_bindings:
                serialized['group_link_bindings'] = updated_bindings
                serialized['group_links'] = [
                    str(item.get('link') or '').strip()
                    for item in serialized['group_link_bindings']
                    if str(item.get('link') or '').strip()
                ]

        verification_checks = [
            {
                'code': 'group_link_format',
                'ok': not invalid_group_links,
                'detail': '群链接格式有效' if not invalid_group_links else f'存在 {len(invalid_group_links)} 条群链接格式异常',
            },
            {
                'code': 'group_link_area_binding',
                'ok': all_binding_areas_ok,
                'detail': '每条群链接都已绑定地区' if all_binding_areas_ok else f'存在 {len(invalid_binding_areas)} 条群链接未绑定地区',
            },
            {
                'code': 'binding_notify_robot',
                'ok': all_binding_notify_ok,
                'detail': '每条群绑定都已配置通知机器人' if all_binding_notify_ok else f'存在 {len(missing_binding_notify)} 条群绑定未配置通知机器人',
            },
            {
                'code': 'binding_approval_rule',
                'ok': all_binding_rules_ok,
                'detail': '每条群绑定都已配置审批条件' if all_binding_rules_ok else '存在群绑定审批条件不完整',
            },
            {
                'code': 'binding_schedule_window',
                'ok': True,
                'detail': f'当前有 {active_binding_count}/{enabled_binding_count or 0} 条已监控群绑定在监控时段内生效',
            },
            {
                'code': 'binding_monitor_enabled',
                'ok': has_monitored_bindings,
                'detail': f'当前已开启 {enabled_binding_count}/{len(binding_runtime_rows) or 0} 条群绑定监控' if has_monitored_bindings else '当前未开启任何群绑定监控',
            },
            {
                'code': 'service_scope_ready',
                'ok': bool(service_scope.get('ready')),
                'detail': service_scope.get('detail') or '-',
            },
            {
                'code': 'admin_membership_verification',
                'ok': bool(account_membership_verifier.get('ready')),
                'detail': account_membership_verifier.get('detail') or '-',
            },
        ]

        config_ready = (
            not invalid_group_links
            and all_binding_areas_ok
            and all_binding_notify_ok
            and all_binding_rules_ok
            and has_monitored_bindings
            and bool(service_scope.get('ready'))
        )
        if invalid_group_links:
            verification_status = 'invalid_group_links'
        elif not has_monitored_bindings:
            verification_status = 'monitor_disabled'
        elif config_ready:
            verification_status = 'ready'
        else:
            verification_status = 'service_unready'

        account_schedule_runtime = self._schedule_runtime(account_schedule_windows)
        account_active_now = bool(account_schedule_runtime.get('active_now')) if account_schedule_windows else (active_binding_count > 0 if binding_runtime_rows else True)
        representative_binding = _preferred_group_binding(binding_runtime_rows)
        representative_schedule_windows = list(representative_binding.get('schedule_windows') or account_schedule_windows)
        representative_schedule_runtime = representative_binding.get('schedule_runtime') or self._schedule_runtime(representative_schedule_windows)
        schedule_runtime = representative_schedule_runtime if representative_binding else account_schedule_runtime
        serialized['schedule_active_now'] = bool(account_active_now)
        serialized['schedule_runtime'] = schedule_runtime
        serialized['area'] = str(representative_binding.get('area') or default_area).strip()
        serialized['notify_profile_name'] = str(representative_binding.get('notify_profile_name') or default_notify_profile_name).strip()
        serialized['notify_robot_name'] = str(representative_binding.get('notify_robot_name') or self._notify_robot_name(serialized['notify_profile_name'])).strip()
        serialized['approval_count_threshold'] = _coerce_positive_int(representative_binding.get('approval_count_threshold'), default_approval_count_threshold)
        serialized['approval_timeout_minutes'] = _coerce_positive_int(representative_binding.get('approval_timeout_minutes'), default_approval_timeout_minutes)
        serialized['approval_rule_text'] = representative_binding.get('approval_rule_text') or _approval_condition_text(serialized['approval_count_threshold'], serialized['approval_timeout_minutes'])
        serialized['approval_condition_text'] = serialized['approval_rule_text']
        serialized['auto_recover_worker'] = default_auto_recover_worker if representative_binding.get('auto_recover_worker') is None else bool(representative_binding.get('auto_recover_worker'))
        serialized['schedule_windows'] = representative_schedule_windows
        serialized['group_binding_runtimes'] = binding_runtime_rows
        serialized['runtime_state'] = runtime_state
        serialized['session_state'] = session_state

        production_ready = bool(config_ready and session_state.get('login_verified'))
        login_check_status = str(session_state.get('login_check_status') or '').strip()
        if production_ready:
            verification_status = 'ready'
        elif login_check_status == 'account_restricted':
            verification_status = 'account_restricted'
        elif login_check_status == 'auth_failed':
            verification_status = 'auth_failed'
        elif invalid_group_links:
            verification_status = 'invalid_group_links'
        elif not has_monitored_bindings:
            verification_status = 'monitor_disabled'
        elif not config_ready:
            verification_status = 'service_unready'
        else:
            verification_status = 'login_unready'

        if not serialized['enabled']:
            runtime_status = 'disabled'
            status_color = 'gray'
            status_text = '已关闭'
            next_action = '如需纳入自动监控，请先开启账号'
        elif not has_monitored_bindings:
            runtime_status = 'blocked'
            status_color = 'gray'
            status_text = '未监控'
            next_action = '至少开启 1 个群监控后再纳入自动审批'
        elif invalid_group_links or not config_ready:
            runtime_status = 'blocked'
            status_color = 'amber'
            status_text = '待补齐'
            next_action = '先补齐群绑定配置，再纳入统一调度'
        elif login_check_status == 'account_restricted':
            runtime_status = 'blocked'
            status_color = 'red'
            status_text = '账号受限'
            next_action = '先在手机端核查封禁/限制状态，确认恢复后再重新登录'
        elif login_check_status == 'auth_failed':
            runtime_status = 'blocked'
            status_color = 'amber'
            status_text = '登录异常'
            next_action = '先重新登录账号，再继续可用性检测'
        elif not session_state.get('login_verified'):
            runtime_status = 'blocked'
            status_color = 'amber'
            status_text = '待登录'
            next_action = '先完成扫码登录并通过可用性检测'
        elif not account_active_now:
            runtime_status = 'off_schedule'
            status_color = 'blue'
            status_text = '时段外待命'
            next_action = '等待进入任一群绑定监控时段后自动生效'
        else:
            runtime_status = 'active'
            status_color = 'green'
            status_text = '运行中'
            next_action = '可直接纳入统一调度'

        serialized['verification_status'] = verification_status
        serialized['verification_status_label'] = {
            'ready': '可投产',
            'invalid_group_links': '群链接配置异常',
            'monitor_disabled': '未启用监控群',
            'service_unready': '服务未就绪',
            'login_unready': '待登录',
            'account_restricted': '账号受限',
            'auth_failed': '登录异常',
        }.get(verification_status, verification_status)
        serialized['membership_verifier'] = account_membership_verifier
        serialized['verification_scope_text'] = account_membership_verifier.get('detail') if account_membership_verifier.get('ready') else '当前控制台配置与调度就绪度已完成；逐群映射或真实校验结果见下方“真实校验”明细。'
        serialized['verification_checks'] = verification_checks
        serialized['service_scope'] = service_scope
        serialized['runtime_status'] = runtime_status
        serialized['status_color'] = status_color
        serialized['status_text'] = status_text
        serialized['next_action'] = next_action
        return serialized

    def list_whatsapp_approval_accounts(self) -> Dict[str, Any]:
        production_ops = self._production_ops_daemon_snapshot()
        official_bridge = self._official_group_bridge_summary_payload()
        try:
            shared_worker_health = self._current_whatsapp_approval_worker_health()
        except Exception:
            shared_worker_health = {}
        rows: list[Dict[str, Any]] = []
        with self.db.connect() as conn:
            raw_rows = conn.execute(
                "SELECT account_key, account_name, responsible_type, group_links, area, notify_profile_name, approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker, schedule_windows, enabled, verification_status, notes, updated_at FROM whatsapp_approval_accounts ORDER BY updated_at DESC, account_key ASC"
            ).fetchall()
        for raw_row in raw_rows:
            row = dict(raw_row)
            account_key = str(row.get('account_key') or '').strip()
            has_dedicated_meta = bool(self._read_whatsapp_approval_runtime_meta(account_key))
            account_worker_health: Dict[str, Any] = {}
            runtime_state = self._build_whatsapp_approval_runtime_state(
                account_key,
                worker_health=None if has_dedicated_meta else shared_worker_health,
            )
            if runtime_state.get('source') == 'shared':
                account_worker_health = shared_worker_health
            elif runtime_state.get('active') and runtime_state.get('base_url'):
                try:
                    account_worker_health = self._request_whatsapp_approval_worker_health(str(runtime_state.get('base_url') or ''))
                    runtime_state = self._build_whatsapp_approval_runtime_state(account_key, worker_health=account_worker_health, allow_shared_fallback=False)
                except Exception:
                    account_worker_health = {}
            rows.append(self._build_whatsapp_approval_account_runtime(row, production_ops=production_ops, official_bridge=official_bridge, worker_health=account_worker_health, runtime_state=runtime_state))
        area_option_payload = self.list_whatsapp_approval_area_options()
        return {
            'rows': rows,
            'notify_robot_options': self._list_notify_robot_options(),
            'area_options': area_option_payload['options'],
            'area_option_source': area_option_payload['source_options'],
            'summary': {
                'total_accounts': len(rows),
                'enabled_accounts': sum(1 for row in rows if row.get('enabled')),
                'registration_group_accounts': sum(1 for row in rows if row.get('responsible_type') == 'registration_group'),
                'official_group_accounts': sum(1 for row in rows if row.get('responsible_type') == 'official_group'),
                'active_now_accounts': sum(1 for row in rows if row.get('runtime_status') == 'active'),
                'ready_accounts': sum(1 for row in rows if row.get('verification_status') == 'ready'),
                'verification_pending_accounts': sum(1 for row in rows if row.get('verification_status') != 'ready'),
            },
        }

    def list_whatsapp_approval_candidates(self) -> Dict[str, Any]:
        account_state = self.list_whatsapp_approval_accounts()
        rows = []
        for row in account_state.get('rows') or []:
            membership_verifier = dict(row.get('membership_verifier') or {})
            candidate_status = 'eligible' if row.get('runtime_status') == 'active' and row.get('verification_status') == 'ready' else 'not_ready'
            verification_scope = {
                'configuration_ready': row.get('verification_status') == 'ready',
                'schedule_active_now': bool(row.get('schedule_active_now')),
                'service_scope_ready': bool((row.get('service_scope') or {}).get('ready')),
                'real_membership_check_ready': bool(membership_verifier.get('ready')),
                'requires_manual_seed': bool(membership_verifier.get('requires_manual_seed', not membership_verifier.get('ready'))),
            }
            rows.append({
                'account_key': row.get('account_key'),
                'account_name': row.get('account_name'),
                'responsible_type': row.get('responsible_type'),
                'candidate_status': candidate_status,
                'runtime_status': row.get('runtime_status'),
                'verification_status': row.get('verification_status'),
                'status_text': row.get('status_text'),
                'group_count': row.get('group_count'),
                'next_action': row.get('next_action'),
                'verification_scope': verification_scope,
                'membership_verifier': membership_verifier,
            })
        rows.sort(key=lambda item: (0 if item.get('candidate_status') == 'eligible' else 1, str(item.get('account_key') or '')))
        verifier_ready_count = sum(1 for row in rows if (row.get('verification_scope') or {}).get('real_membership_check_ready'))
        any_manual_seed = any((row.get('verification_scope') or {}).get('requires_manual_seed') for row in rows)
        framework_status = 'live_probe_ready' if verifier_ready_count else ('seed_required' if any_manual_seed else 'unavailable')
        if verifier_ready_count:
            framework_detail = '已接入真实注册群状态探针；具备实时群成员/管理员权限校验能力的账号会在候选池中标记为 real_membership_check_ready=true。'
        elif any_manual_seed:
            framework_detail = '部分账号仍缺真实成员/管理员校验探针，需继续补齐执行器种子或 bridge 能力。'
        else:
            framework_detail = '当前没有可用于真实成员/管理员权限校验的账号执行器。'
        return {
            'rows': rows,
            'summary': {
                'eligible_count': sum(1 for row in rows if row.get('candidate_status') == 'eligible'),
                'registration_group_count': sum(1 for row in rows if row.get('responsible_type') == 'registration_group'),
                'official_group_count': sum(1 for row in rows if row.get('responsible_type') == 'official_group'),
                'verifier_ready_count': verifier_ready_count,
            },
            'verifier_framework': {
                'status': framework_status,
                'real_membership_check_ready': bool(verifier_ready_count),
                'requires_manual_seed': any_manual_seed,
                'detail': framework_detail,
            },
        }

    def update_whatsapp_approval_account(self, account_key: str, payload: WhatsAppApprovalAccountUpdateRequest) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            raise HTTPException(status_code=400, detail='account_key is required')
        account_name = str(payload.account_name or '').strip()
        if not account_name:
            raise HTTPException(status_code=400, detail='account_name is required')
        responsible_type = str(payload.responsible_type or '').strip()
        if responsible_type not in {'registration_group', 'official_group'}:
            raise HTTPException(status_code=400, detail='responsible_type must be registration_group or official_group')
        raw_bindings = [
            {
                'link': str(item.link or '').strip(),
                'group_name': str(item.group_name or '').strip(),
                'area': str(item.area or '').strip(),
                'notify_profile_name': str(item.notify_profile_name or '').strip(),
                'enabled': False if item.enabled is False else True,
                'registration_group': str(item.registration_group or '').strip(),
                'group_id': str(item.group_id or '').strip(),
                'approval_count_threshold': item.approval_count_threshold,
                'approval_timeout_minutes': item.approval_timeout_minutes,
                'auto_recover_worker': item.auto_recover_worker,
                'schedule_windows': [
                    {'start': str(window.start or '').strip(), 'end': str(window.end or '').strip()}
                    for window in (item.schedule_windows or [])
                ],
            }
            for item in (payload.group_link_bindings or [])
        ]
        if not raw_bindings:
            fallback_links = [str(item or '').strip() for item in (payload.group_links or []) if str(item or '').strip()]
            fallback_area = str(payload.area or '').strip()
            fallback_notify = str(payload.notify_profile_name or '').strip()
            legacy_count_threshold, legacy_timeout_minutes = _legacy_approval_thresholds(payload.approval_rule)
            fallback_count = _coerce_positive_int(payload.approval_count_threshold, legacy_count_threshold)
            fallback_timeout = _coerce_positive_int(payload.approval_timeout_minutes, legacy_timeout_minutes)
            fallback_schedule_windows = [
                {'start': str(item.start or '').strip(), 'end': str(item.end or '').strip()}
                for item in (payload.schedule_windows or [])
            ]
            raw_bindings = [{
                'link': link,
                'group_name': '',
                'area': fallback_area,
                'notify_profile_name': fallback_notify,
                'enabled': True,
                'registration_group': '',
                'group_id': '',
                'approval_count_threshold': fallback_count,
                'approval_timeout_minutes': fallback_timeout,
                'auto_recover_worker': payload.auto_recover_worker,
                'schedule_windows': fallback_schedule_windows,
            } for link in fallback_links]
        group_link_bindings = []
        for index, item in enumerate(raw_bindings, start=1):
            link = str(item.get('link') or '').strip()
            area = str(item.get('area') or '').strip()
            notify_profile_name = str(item.get('notify_profile_name') or '').strip()
            registration_group = str(item.get('registration_group') or '').strip()
            group_id = str(item.get('group_id') or '').strip()
            if not link and not area and not notify_profile_name and not registration_group and not group_id:
                continue
            if link and not area:
                raise HTTPException(status_code=400, detail=f'group link #{index} must select an area')
            if area and not link:
                raise HTTPException(status_code=400, detail=f'group link #{index} is missing its link')
            if not notify_profile_name:
                raise HTTPException(status_code=400, detail=f'group link #{index} must select a notify robot')
            schedule_windows = _normalize_schedule_windows_payload(item.get('schedule_windows') or [])
            if any(not re.fullmatch(r'\d{2}:\d{2}', str(window.get('start') or '')) or not re.fullmatch(r'\d{2}:\d{2}', str(window.get('end') or '')) for window in (item.get('schedule_windows') or [])):
                raise HTTPException(status_code=400, detail=f'group link #{index} schedule window must use HH:MM format')
            group_link_bindings.append({
                'link': link,
                'group_name': str(item.get('group_name') or '').strip(),
                'area': area,
                'notify_profile_name': notify_profile_name,
                'enabled': False if item.get('enabled') is False else True,
                'registration_group': registration_group,
                'group_id': group_id,
                'approval_count_threshold': item.get('approval_count_threshold'),
                'approval_timeout_minutes': item.get('approval_timeout_minutes'),
                'auto_recover_worker': item.get('auto_recover_worker'),
                'schedule_windows': schedule_windows,
            })
        group_link_bindings = _normalize_group_link_bindings(group_link_bindings)
        group_links = [item['link'] for item in group_link_bindings]
        if not group_links:
            raise HTTPException(status_code=400, detail='at least one group link is required')
        if len(group_links) > 3:
            raise HTTPException(status_code=400, detail='each WhatsApp admin account can manage at most 3 groups in this console')
        area_options = self.list_whatsapp_approval_area_options()['options']
        area_values = {str(item.get('value') or '').strip() for item in area_options}
        area_values.discard('')
        for index, item in enumerate(group_link_bindings, start=1):
            if str(item.get('area') or '').strip() not in area_values:
                raise HTTPException(status_code=400, detail=f'group link #{index} area must be selected from configured options')
        area = str(group_link_bindings[0].get('area') or '').strip()
        valid_notify_profiles = {str(item.get('profile_name') or '').strip() for item in self._list_notify_robot_options()}
        valid_notify_profiles.discard('')
        for index, item in enumerate(group_link_bindings, start=1):
            binding_notify_profile_name = str(item.get('notify_profile_name') or '').strip()
            if binding_notify_profile_name not in valid_notify_profiles:
                raise HTTPException(status_code=400, detail=f'group link #{index} notify_profile_name must be selected from configured Lark robots')
            item['approval_count_threshold'] = _coerce_positive_int(item.get('approval_count_threshold'), WHATSAPP_APPROVAL_DEFAULT_COUNT_THRESHOLD)
            item['approval_timeout_minutes'] = _coerce_positive_int(item.get('approval_timeout_minutes'), WHATSAPP_APPROVAL_DEFAULT_TIMEOUT_MINUTES)
            if item['approval_count_threshold'] <= 0:
                raise HTTPException(status_code=400, detail=f'group link #{index} approval_count_threshold must be a positive integer')
            if item['approval_timeout_minutes'] <= 0:
                raise HTTPException(status_code=400, detail=f'group link #{index} approval_timeout_minutes must be a positive integer')
            item['auto_recover_worker'] = bool(item.get('auto_recover_worker')) if item.get('auto_recover_worker') is not None else bool(payload.auto_recover_worker)
        runtime_state = self._build_whatsapp_approval_runtime_state(normalized_key)
        if runtime_state.get('active'):
            session_state = self._build_whatsapp_approval_session_state(normalized_key, include_qr_ascii=False)
        else:
            session_state = {}
        for item in group_link_bindings:
            self._apply_live_group_identity_to_binding(
                item,
                responsible_type=responsible_type,
                runtime_state=runtime_state,
                session_state=session_state,
                allow_shared_fallback=responsible_type == 'registration_group',
            )
        representative_binding = _preferred_group_binding(group_link_bindings)
        area = str(representative_binding.get('area') or '').strip()
        notify_profile_name = str(representative_binding.get('notify_profile_name') or '').strip()
        approval_count_threshold = _coerce_positive_int(representative_binding.get('approval_count_threshold'), WHATSAPP_APPROVAL_DEFAULT_COUNT_THRESHOLD)
        approval_timeout_minutes = _coerce_positive_int(representative_binding.get('approval_timeout_minutes'), WHATSAPP_APPROVAL_DEFAULT_TIMEOUT_MINUTES)
        schedule_windows = _normalize_schedule_windows_payload(representative_binding.get('schedule_windows') or [])
        row = {
            'account_key': normalized_key,
            'account_name': account_name,
            'responsible_type': responsible_type,
            'group_links': json.dumps(group_link_bindings, ensure_ascii=False),
            'area': area,
            'notify_profile_name': notify_profile_name,
            'approval_rule': 'threshold_or_timeout',
            'approval_count_threshold': approval_count_threshold,
            'approval_timeout_minutes': approval_timeout_minutes,
            'auto_recover_worker': 1 if representative_binding.get('auto_recover_worker') else 0,
            'schedule_windows': json.dumps(schedule_windows, ensure_ascii=False),
            'enabled': 1 if payload.enabled else 0,
            'verification_status': 'pending_verification',
            'notes': str(payload.notes or '').strip(),
            'updated_at': utc_now(),
        }
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO whatsapp_approval_accounts (
                    account_key, account_name, responsible_type, group_links, area, notify_profile_name, approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker, schedule_windows,
                    enabled, verification_status, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_key)
                DO UPDATE SET account_name = excluded.account_name,
                              responsible_type = excluded.responsible_type,
                              group_links = excluded.group_links,
                              area = excluded.area,
                              notify_profile_name = excluded.notify_profile_name,
                              approval_rule = excluded.approval_rule,
                              approval_count_threshold = excluded.approval_count_threshold,
                              approval_timeout_minutes = excluded.approval_timeout_minutes,
                              auto_recover_worker = excluded.auto_recover_worker,
                              schedule_windows = excluded.schedule_windows,
                              enabled = excluded.enabled,
                              verification_status = excluded.verification_status,
                              notes = excluded.notes,
                              updated_at = excluded.updated_at
                """,
                (
                    row['account_key'], row['account_name'], row['responsible_type'], row['group_links'], row['area'], row['notify_profile_name'], row['approval_rule'], row['approval_count_threshold'], row['approval_timeout_minutes'], row['auto_recover_worker'], row['schedule_windows'],
                    row['enabled'], row['verification_status'], row['notes'], row['updated_at'],
                ),
            )
            conn.commit()
        return {
            'saved': True,
            'account': self._build_whatsapp_approval_account_runtime(row, runtime_state=runtime_state),
        }

    def delete_whatsapp_approval_account(self, account_key: str) -> Dict[str, Any]:
        normalized_key = str(account_key or '').strip()
        if not normalized_key:
            raise HTTPException(status_code=400, detail='account_key is required')
        self.stop_whatsapp_approval_account_runtime(normalized_key)
        with self.db.connect() as conn:
            conn.execute('DELETE FROM whatsapp_approval_accounts WHERE account_key = ?', (normalized_key,))
            conn.commit()
        return {'deleted': True, 'account_key': normalized_key}

    def _default_production_ops_daemon_config(self) -> Dict[str, Any]:
        launch_agent_installed = PRODUCTION_OPS_DAEMON_LAUNCH_AGENT_PATH.exists()
        return {
            'config_name': 'default',
            'enabled': launch_agent_installed,
            'registration_group': '🇮🇩3️⃣7️⃣Grup Registrasi Resmi Linky 💎',
            'api_base_url': 'http://127.0.0.1:8011',
            'worker_base_url': 'http://127.0.0.1:8787',
            'interval_seconds': 20.0,
            'notify_chat_id': str(os.getenv('FEISHU_HOME_CHANNEL') or '').strip(),
            'area': 'Indonesia',
            'remark': 'production auto approval daemon',
            'approved_count': 1,
            'auto_recover_worker': True,
            'updated_at': utc_now(),
        }

    def _persist_production_ops_daemon_env(self, row: Dict[str, Any]) -> None:
        if self.db.db_path == ':memory:' or str(os.getenv('PRODUCTION_OPS_DAEMON_SKIP_RUNTIME_SYNC') or '').strip().lower() in {'1', 'true', 'yes', 'on'}:
            return
        existing_env: Dict[str, str] = {}
        if PRODUCTION_OPS_DAEMON_ENV_PATH.exists():
            try:
                for raw_line in PRODUCTION_OPS_DAEMON_ENV_PATH.read_text(encoding='utf-8', errors='ignore').splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    existing_env[str(key).strip()] = value.strip().strip('"').strip("'")
            except Exception:
                existing_env = {}
        env_rows = {
            'PRODUCTION_OPS_API_BASE_URL': str(row.get('api_base_url') or '').strip(),
            'PRODUCTION_OPS_WORKER_BASE_URL': str(row.get('worker_base_url') or '').strip(),
            'PRODUCTION_OPS_REGISTRATION_GROUP': str(row.get('registration_group') or '').strip(),
            'PRODUCTION_OPS_INTERVAL_SECONDS': str(row.get('interval_seconds') or 20),
            'PRODUCTION_OPS_NOTIFY_CHAT_ID': str(row.get('notify_chat_id') or '').strip(),
            'PRODUCTION_OPS_AREA': str(row.get('area') or '').strip(),
            'PRODUCTION_OPS_REMARK': str(row.get('remark') or '').strip(),
            'PRODUCTION_OPS_APPROVED_COUNT': str(row.get('approved_count') or 1),
            'PRODUCTION_OPS_AUTO_RECOVER_WORKER': '1' if row.get('auto_recover_worker') else '0',
        }
        for key in ('PRODUCTION_OPS_FEISHU_APP_ID', 'PRODUCTION_OPS_FEISHU_APP_SECRET', 'PRODUCTION_OPS_FEISHU_DOMAIN'):
            existing_value = str(existing_env.get(key) or '').strip()
            if existing_value:
                env_rows[key] = existing_value
        lines = [f"{key}={shlex.quote(value)}" for key, value in env_rows.items()]
        PRODUCTION_OPS_DAEMON_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        PRODUCTION_OPS_DAEMON_ENV_PATH.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def _sync_production_ops_daemon_launch_agent(self, *, enabled: bool) -> Dict[str, Any]:
        if self.db.db_path == ':memory:' or str(os.getenv('PRODUCTION_OPS_DAEMON_SKIP_RUNTIME_SYNC') or '').strip().lower() in {'1', 'true', 'yes', 'on'}:
            return {'attempted': False, 'skipped': True}
        script_path = PRODUCTION_OPS_DAEMON_INSTALL_SCRIPT if enabled else PRODUCTION_OPS_DAEMON_UNINSTALL_SCRIPT
        completed = subprocess.run([str(script_path)], capture_output=True, text=True, cwd=str(PROJECT_ROOT))
        return {
            'attempted': True,
            'enabled': enabled,
            'returncode': completed.returncode,
            'stdout': completed.stdout,
            'stderr': completed.stderr,
            'ok': completed.returncode == 0,
        }

    def get_production_ops_daemon_config(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT config_name, enabled, registration_group, api_base_url, worker_base_url, interval_seconds, notify_chat_id, area, remark, approved_count, auto_recover_worker, updated_at FROM production_ops_daemon_configs WHERE config_name = 'default'"
            ).fetchone()
        if not row:
            config = self._default_production_ops_daemon_config()
        else:
            config = dict(row)
            config['enabled'] = bool(config.get('enabled'))
            config['auto_recover_worker'] = bool(config.get('auto_recover_worker'))
        runtime_status = {}
        if PRODUCTION_OPS_DAEMON_STATUS_PATH.exists():
            try:
                runtime_status = json.loads(PRODUCTION_OPS_DAEMON_STATUS_PATH.read_text(encoding='utf-8'))
                if not isinstance(runtime_status, dict):
                    runtime_status = {}
            except Exception:
                runtime_status = {}
        return {
            'config': config,
            'runtime': {
                'launch_agent_installed': PRODUCTION_OPS_DAEMON_LAUNCH_AGENT_PATH.exists(),
                'status_path': str(PRODUCTION_OPS_DAEMON_STATUS_PATH),
                'env_path': str(PRODUCTION_OPS_DAEMON_ENV_PATH),
                'status': runtime_status,
            },
        }

    def update_production_ops_daemon_config(self, payload: ProductionOpsDaemonConfigUpdateRequest) -> Dict[str, Any]:
        existing = self.get_production_ops_daemon_config()['config']
        registration_group = str(payload.registration_group or '').strip() or str(existing.get('registration_group') or self._default_production_ops_daemon_config().get('registration_group') or '').strip()
        row = {
            'config_name': 'default',
            'enabled': 1 if payload.enabled else 0,
            'registration_group': registration_group,
            'api_base_url': str(payload.api_base_url or existing.get('api_base_url') or 'http://127.0.0.1:8011').strip(),
            'worker_base_url': str(payload.worker_base_url or existing.get('worker_base_url') or 'http://127.0.0.1:8787').strip(),
            'interval_seconds': max(5.0, float(payload.interval_seconds or existing.get('interval_seconds') or 20.0)),
            'notify_chat_id': str(payload.notify_chat_id or '').strip(),
            'area': str(payload.area or existing.get('area') or 'Indonesia').strip(),
            'remark': str(payload.remark or existing.get('remark') or 'production auto approval daemon').strip(),
            'approved_count': max(1, int(payload.approved_count or existing.get('approved_count') or 1)),
            'auto_recover_worker': 1 if payload.auto_recover_worker else 0,
            'updated_at': utc_now(),
        }
        if not row['api_base_url']:
            raise HTTPException(status_code=400, detail='api_base_url is required')
        if not row['worker_base_url']:
            raise HTTPException(status_code=400, detail='worker_base_url is required')
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO production_ops_daemon_configs (
                    config_name, enabled, registration_group, api_base_url, worker_base_url, interval_seconds,
                    notify_chat_id, area, remark, approved_count, auto_recover_worker, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_name)
                DO UPDATE SET enabled = excluded.enabled,
                              registration_group = excluded.registration_group,
                              api_base_url = excluded.api_base_url,
                              worker_base_url = excluded.worker_base_url,
                              interval_seconds = excluded.interval_seconds,
                              notify_chat_id = excluded.notify_chat_id,
                              area = excluded.area,
                              remark = excluded.remark,
                              approved_count = excluded.approved_count,
                              auto_recover_worker = excluded.auto_recover_worker,
                              updated_at = excluded.updated_at
                """,
                (
                    row['config_name'], row['enabled'], row['registration_group'], row['api_base_url'], row['worker_base_url'], row['interval_seconds'],
                    row['notify_chat_id'], row['area'], row['remark'], row['approved_count'], row['auto_recover_worker'], row['updated_at'],
                ),
            )
            conn.commit()
        self._persist_production_ops_daemon_env({**row, 'enabled': bool(row['enabled']), 'auto_recover_worker': bool(row['auto_recover_worker'])})
        runtime_sync = self._sync_production_ops_daemon_launch_agent(enabled=bool(row['enabled']))
        return {
            'saved': True,
            'config': {
                **row,
                'enabled': bool(row['enabled']),
                'auto_recover_worker': bool(row['auto_recover_worker']),
            },
            'runtime_sync': runtime_sync,
        }

    def guild_executor_health(self) -> Dict[str, Any]:
        executors = self.list_guild_executors()['rows']
        human_actions = self._pending_bind_human_actions(limit=100)
        human_by_guild = {}
        for item in human_actions:
            guild_name = str(item.get('guild_name') or '').strip()
            if guild_name and guild_name not in human_by_guild:
                human_by_guild[guild_name] = item
        with self.db.connect() as conn:
            latest_bind_rows = [dict(r) for r in conn.execute(
                """
                SELECT x.guild_name, x.task_id, x.status, x.result_code, x.result_reason, x.created_at, x.started_at, x.finished_at
                FROM (
                    SELECT COALESCE(l.dept_name, '') AS guild_name,
                           t.task_id,
                           t.status,
                           t.result_code,
                           t.result_reason,
                           t.created_at,
                           t.started_at,
                           t.finished_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(l.dept_name, '')
                               ORDER BY COALESCE(t.finished_at, t.started_at, t.created_at) DESC
                           ) AS rn
                    FROM automation_tasks t
                    LEFT JOIN leads l ON l.lead_id = t.lead_id
                    WHERE t.task_type = 'bind_check'
                ) x
                WHERE x.rn = 1
                """
            ).fetchall()]
            processing_rows = [dict(r) for r in conn.execute(
                """
                SELECT COALESCE(l.dept_name, '') AS guild_name, COUNT(*) AS processing_count
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check' AND t.status = 'processing'
                GROUP BY COALESCE(l.dept_name, '')
                """
            ).fetchall()]
        latest_by_guild = {str(r.get('guild_name') or '').strip(): r for r in latest_bind_rows}
        processing_by_guild = {str(r.get('guild_name') or '').strip(): int(r.get('processing_count') or 0) for r in processing_rows}
        rows = []
        for executor in executors:
            guild_name = str(executor.get('guild_name') or '').strip()
            latest = latest_by_guild.get(guild_name, {})
            human = human_by_guild.get(guild_name, {})
            bind_concurrency = int(executor.get('bind_concurrency') or 1)
            processing_count = int(processing_by_guild.get(guild_name) or 0)
            human_visible = bool(human)
            if human_visible and latest:
                latest_task_id = str(latest.get('task_id') or '').strip()
                human_task_id = str(human.get('task_id') or '').strip()
                latest_code = str(latest.get('result_code') or '').strip().lower()
                latest_reason = str(latest.get('result_reason') or '').strip().lower()
                latest_still_requires_human = latest_code in {'bind_unauthorized', 'auth_required', 'session_expired', 'bind_session_expired', 'captcha_required', 'bind_captcha_required', 'manual_continue_required', 'bind_manual_continue_required'} or 're-login' in latest_reason or 'status code 401' in latest_reason or 'unauthorized' in latest_reason or 'forbidden' in latest_reason or 'captcha' in latest_reason
                if latest_task_id and human_task_id and latest_task_id != human_task_id and not latest_still_requires_human:
                    human_visible = False
            rows.append({
                'guild_name': guild_name,
                'enabled': bool(executor.get('enabled')),
                'browser_profile_key': executor.get('browser_profile_key') or '',
                'proxy_region': executor.get('proxy_region') or '',
                'bind_concurrency': bind_concurrency,
                'processing_count': processing_count,
                'available_slots': max(0, max(1, bind_concurrency) - processing_count),
                'last_bind_task_id': latest.get('task_id'),
                'last_bind_status': latest.get('status'),
                'last_bind_result_code': latest.get('result_code'),
                'last_bind_result_reason': latest.get('result_reason'),
                'last_bind_created_at': latest.get('created_at'),
                'last_bind_started_at': latest.get('started_at'),
                'last_bind_finished_at': latest.get('finished_at'),
                'requires_human_action': human_visible,
                'human_action_type': human.get('human_action_type') if human_visible else None,
                'human_action_task_id': human.get('task_id') if human_visible else None,
            })
        return {'rows': rows}

    def resolve_guild_executor(self, guild_name: Optional[str]) -> Optional[Dict[str, Any]]:
        normalized_guild_name = str(guild_name or '').strip()
        if not normalized_guild_name:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT guild_name, backend_url, login_username, password_secret_ref, proxy_url, proxy_region, proxy_type, enabled, browser_profile_key, bind_concurrency, request_timeout_seconds, notes, updated_at FROM guild_executors WHERE guild_name = ?",
                (normalized_guild_name,),
            ).fetchone()
        if not row:
            return None
        resolved = dict(row)
        resolved['enabled'] = bool(resolved.get('enabled'))
        resolved['password_configured'] = bool(str(resolved.get('password_secret_ref') or '').strip())
        return resolved

    def get_guild_executor(self, guild_name: str) -> Dict[str, Any]:
        resolved = self.resolve_guild_executor(guild_name)
        if not resolved:
            raise HTTPException(status_code=404, detail='guild executor not found')
        return {
            'guild_name': resolved['guild_name'],
            'backend_url': resolved['backend_url'],
            'login_username': resolved['login_username'],
            'proxy_url': resolved.get('proxy_url') or '',
            'proxy_region': resolved.get('proxy_region') or '',
            'proxy_type': resolved.get('proxy_type') or '',
            'enabled': bool(resolved.get('enabled')),
            'browser_profile_key': resolved.get('browser_profile_key') or '',
            'bind_concurrency': int(resolved.get('bind_concurrency') or 1),
            'request_timeout_seconds': int(resolved.get('request_timeout_seconds') or 30),
            'notes': resolved.get('notes') or '',
            'password_configured': bool(resolved.get('password_configured')),
            'updated_at': resolved.get('updated_at'),
        }

    def retry_bind_submission(self, submission_id: str) -> Dict[str, Any]:
        normalized_submission_id = str(submission_id or '').strip()
        if not normalized_submission_id:
            raise HTTPException(status_code=400, detail='submission_id is required')
        with self.db.connect() as conn:
            submission = conn.execute("SELECT * FROM account_submissions WHERE submission_id = ?", (normalized_submission_id,)).fetchone()
            if not submission:
                raise HTTPException(status_code=404, detail='submission not found')
            submission_dict = dict(submission)
            lead = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (submission_dict['lead_id'],)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail='lead not found')
            account_id = str(submission_dict.get('recognized_account_id') or submission_dict.get('account_id') or '').strip()
            if not account_id:
                raise HTTPException(status_code=400, detail='submission has no account_id for bind retry')
            latest_bind = conn.execute(
                "SELECT task_id, status FROM automation_tasks WHERE lead_id = ? AND task_type = 'bind_check' ORDER BY created_at DESC LIMIT 1",
                (submission_dict['lead_id'],),
            ).fetchone()
            retry_task_id = create_id('task')
            retry_payload = {
                'submission_id': normalized_submission_id,
                'lead_id': submission_dict['lead_id'],
                'account_id': account_id,
            }
            if latest_bind:
                latest_row = conn.execute("SELECT payload FROM automation_tasks WHERE task_id = ?", (latest_bind['task_id'],)).fetchone()
                latest_payload = json.loads((latest_row['payload'] if latest_row else '{}') or '{}')
                for key in ('source_bot_app_id', 'source_message_id', 'source_chat_id'):
                    if latest_payload.get(key):
                        retry_payload[key] = latest_payload.get(key)
            created_at = utc_now()
            conn.execute(
                """
                INSERT INTO automation_tasks (
                    task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    retry_task_id,
                    submission_dict['lead_id'],
                    'bind_check',
                    'P0',
                    json.dumps(retry_payload, ensure_ascii=False),
                    f"bind_retry:{normalized_submission_id}:{retry_task_id}",
                    'system:retry_bind',
                    created_at,
                    'pending',
                ),
            )
            conn.execute(
                "UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?",
                ('bind_check_pending', created_at, submission_dict['lead_id']),
            )
            self._record_status_history(
                conn,
                lead_id=submission_dict['lead_id'],
                from_status=str((lead['current_status'] if lead else '') or ''),
                to_status='bind_check_pending',
                trigger_type='technical_retry_bind',
                trigger_source='ops_retry_bind',
                trigger_task_id=retry_task_id,
                operator_name='system:retry_bind',
                remark=f'retry original submission {normalized_submission_id}',
            )
            conn.commit()
        return {
            'accepted': True,
            'retry_type': 'bind',
            'submission_id': normalized_submission_id,
            'task_id': retry_task_id,
            'next_action': 'queue_bind_check',
            'created_new_submission': False,
        }

    def retry_crm_submission(self, submission_id: str) -> Dict[str, Any]:
        normalized_submission_id = str(submission_id or '').strip()
        if not normalized_submission_id:
            raise HTTPException(status_code=400, detail='submission_id is required')
        if self.crm_adapter is None:
            raise HTTPException(status_code=400, detail='crm adapter not configured')
        with self.db.connect() as conn:
            submission = conn.execute("SELECT * FROM account_submissions WHERE submission_id = ?", (normalized_submission_id,)).fetchone()
            if not submission:
                raise HTTPException(status_code=404, detail='submission not found')
            submission_dict = dict(submission)
            lead = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (submission_dict['lead_id'],)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail='lead not found')
            latest_successful_bind = conn.execute(
                "SELECT task_id, result_reason, raw_result FROM automation_tasks WHERE lead_id = ? AND task_type = 'bind_check' AND status = 'success' ORDER BY COALESCE(finished_at, created_at) DESC LIMIT 1",
                (submission_dict['lead_id'],),
            ).fetchone()
            if not latest_successful_bind:
                raise HTTPException(status_code=400, detail='no successful bind context available for crm retry')
            retry_task_id = create_id('task')
            raw_result = json.loads(str(latest_successful_bind['raw_result'] or '{}')) if latest_successful_bind['raw_result'] else {}
            conn.execute(
                """
                INSERT INTO automation_tasks (
                    task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status, result_code, result_reason, finished_at, raw_result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    retry_task_id,
                    submission_dict['lead_id'],
                    'crm_sync_retry',
                    'P0',
                    json.dumps({'submission_id': normalized_submission_id, 'lead_id': submission_dict['lead_id'], 'account_id': str(submission_dict.get('recognized_account_id') or submission_dict.get('account_id') or '')}, ensure_ascii=False),
                    f"crm_retry:{normalized_submission_id}:{retry_task_id}",
                    'system:retry_crm',
                    utc_now(),
                    'success',
                    'crm_retry_started',
                    'crm retry started from latest successful bind context',
                    utc_now(),
                    json.dumps(raw_result, ensure_ascii=False),
                ),
            )
            crm_sync = self._sync_crm_after_bind_success(
                conn,
                lead_id=submission_dict['lead_id'],
                account_id=str(submission_dict.get('recognized_account_id') or submission_dict.get('account_id') or ''),
                task_id=retry_task_id,
                bind_result_reason=str(latest_successful_bind['result_reason'] or ''),
                bind_raw_result=raw_result,
            )
            created_group_join = None
            if not crm_sync['crm_sync_failed']:
                created_group_join = self._queue_group_join_after_verified_crm(
                    conn,
                    lead_id=submission_dict['lead_id'],
                    submission_id=normalized_submission_id,
                    account_id=str(submission_dict.get('recognized_account_id') or submission_dict.get('account_id') or ''),
                    created_at=utc_now(),
                )
            conn.commit()
        return {
            'accepted': crm_sync['crm_sync_failed'] is None,
            'retry_type': 'crm',
            'submission_id': normalized_submission_id,
            'task_id': retry_task_id,
            'next_action': 'queue_group_join' if created_group_join else 'retry_crm_sync',
            'result_reason': crm_sync['crm_sync_failed'],
            'crm_verified': crm_sync['crm_verified'],
            'created_new_submission': False,
            'group_join_task_id': (created_group_join or {}).get('group_join_task_id'),
        }

    def resubmit_corrected_submission(self, submission_id: str, payload: SubmissionResubmitRequest) -> Dict[str, Any]:
        normalized_submission_id = str(submission_id or '').strip()
        if not normalized_submission_id:
            raise HTTPException(status_code=400, detail='submission_id is required')
        with self.db.connect() as conn:
            submission = conn.execute("SELECT * FROM account_submissions WHERE submission_id = ?", (normalized_submission_id,)).fetchone()
            if not submission:
                raise HTTPException(status_code=404, detail='submission not found')
            submission_dict = dict(submission)
            lead = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (submission_dict['lead_id'],)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail='lead not found')
            lead_dict = dict(lead)
        final_mobile = str(payload.mobile or '').strip() or format_display_phone(lead_dict.get('mobile'), area_code=lead_dict.get('area_code'))
        final_group = str(payload.registration_group or '').strip() or str(lead_dict.get('pendaftaran_group') or '').strip()
        final_code = str(payload.invite_code or '').strip().upper() or str(lead_dict.get('inviter_id') or '').strip().upper()
        final_account_id = str(payload.account_id or '').strip() or str(submission_dict.get('recognized_account_id') or submission_dict.get('account_id') or lead_dict.get('yw_id') or '').strip()
        resubmit = self._submit_manual_cs_sync(
            ManualCsSubmissionRequest(
                mobile=final_mobile,
                registration_group=final_group,
                app_name=str(lead_dict.get('app_name') or '').strip(),
                dept_name=str(lead_dict.get('dept_name') or '').strip(),
                invite_code=final_code,
                submission_type='account_id',
                account_id=final_account_id,
                submitted_by=str(payload.corrected_by or '').strip(),
                source_channel=str(submission_dict.get('source_channel') or 'manual_cs_lark'),
                remark=(str(payload.remark or '').strip() or str(submission_dict.get('remark') or '').strip() or None),
                submitted_at=payload.submitted_at,
            )
        )
        updated_lead_id = str(resubmit.get('lead_id') or submission_dict['lead_id'])
        corrections = {
            'mobile': (format_display_phone(lead_dict.get('mobile'), area_code=lead_dict.get('area_code')), final_mobile),
            'registration_group': (str(lead_dict.get('pendaftaran_group') or '').strip(), final_group),
            'invite_code': (str(lead_dict.get('inviter_id') or '').strip().upper(), final_code),
            'account_id': (str(lead_dict.get('yw_id') or '').strip(), final_account_id),
        }
        now = utc_now()
        with self.db.connect() as conn:
            correction_count = 0
            for field_name, (old_value, new_value) in corrections.items():
                if str(old_value or '') == str(new_value or ''):
                    continue
                correction_count += 1
                conn.execute(
                    """
                    INSERT INTO lead_corrections (
                        correction_id, lead_id, field_name, old_value, new_value, corrected_by, review_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (create_id('corr'), updated_lead_id, field_name, old_value, new_value, payload.corrected_by, normalized_submission_id, now),
                )
            if correction_count:
                conn.execute(
                    "UPDATE leads SET correction_count = COALESCE(correction_count, 0) + ?, updated_at = ? WHERE lead_id = ?",
                    (correction_count, now, updated_lead_id),
                )
            conn.commit()
        resubmit['original_submission_id'] = normalized_submission_id
        resubmit['created_new_submission'] = True
        resubmit['resubmit_type'] = 'manual_corrected_submission'
        return resubmit

    def exception_queue(self) -> Dict[str, Any]:
        rows: list[Dict[str, Any]] = []
        with self.db.connect() as conn:
            bind_rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.lead_id, COALESCE(l.mobile, '') AS mobile, COALESCE(l.yw_id, '') AS account_id,
                       COALESCE(l.pendaftaran_group, '') AS registration_group, COALESCE(l.dept_name, '') AS guild_name,
                       t.result_code, t.result_reason, COALESCE(t.finished_at, t.created_at) AS created_at
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check' AND t.status = 'failed'
                ORDER BY COALESCE(t.finished_at, t.created_at) DESC
                LIMIT 50
                """
            ).fetchall()]
            for row in bind_rows:
                human = self._classify_bind_human_action(result_code=row.get('result_code'), result_reason=row.get('result_reason'), raw_result={})
                rows.append({
                    'lead_id': row['lead_id'],
                    'submission_id': None,
                    'task_id': row['task_id'],
                    'current_status': 'bind_failed',
                    'exception_type': human['human_action_type'] or 'bind_failure',
                    'reason': row['result_reason'],
                    'latest_action': 'retry_bind' if not human['requires_human_action'] else 'manual_reauth',
                    'guild_name': row['guild_name'],
                    'mobile': row['mobile'],
                    'account_id': row['account_id'],
                    'registration_group': row['registration_group'],
                    'created_at': row['created_at'],
                })
            crm_rows = [dict(r) for r in conn.execute(
                """
                SELECT n.notification_id, n.lead_id, n.mobile, n.yw_id, n.reason, n.created_at,
                       COALESCE(l.current_status, '') AS current_status, COALESCE(l.dept_name, '') AS guild_name,
                       COALESCE(l.pendaftaran_group, '') AS registration_group
                FROM operator_notifications n
                LEFT JOIN leads l ON l.lead_id = n.lead_id
                WHERE n.notification_type = 'crm_record_failed' AND n.is_read = 0
                ORDER BY n.created_at DESC
                LIMIT 50
                """
            ).fetchall()]
            for row in crm_rows:
                rows.append({
                    'lead_id': row['lead_id'],
                    'submission_id': None,
                    'task_id': row['notification_id'],
                    'current_status': row['current_status'],
                    'exception_type': 'crm_failure',
                    'reason': row['reason'],
                    'latest_action': 'retry_crm',
                    'guild_name': row['guild_name'],
                    'mobile': row['mobile'],
                    'account_id': row['yw_id'],
                    'registration_group': row['registration_group'],
                    'created_at': row['created_at'],
                })
            group_rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.lead_id, t.result_reason, t.raw_result, COALESCE(t.finished_at, t.created_at) AS created_at,
                       COALESCE(l.mobile, '') AS mobile, COALESCE(l.yw_id, '') AS account_id,
                       COALESCE(l.pendaftaran_group, '') AS registration_group, COALESCE(l.dept_name, '') AS guild_name,
                       COALESCE(l.current_status, '') AS current_status
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'group_join' AND t.status = 'failed'
                ORDER BY COALESCE(t.finished_at, t.created_at) DESC
                LIMIT 50
                """
            ).fetchall()]
            for row in group_rows:
                latest_action = 'retry_group_join'
                try:
                    raw_result = json.loads(row.get('raw_result') or '{}')
                except Exception:
                    raw_result = {}
                disposition = str(raw_result.get('execution_disposition') or '').strip().lower()
                if disposition == 'retryable_failed':
                    latest_action = 'retry_official_group_approval'
                elif disposition == 'manual_required':
                    latest_action = 'manual_continue_official_group_approval'
                rows.append({
                    'lead_id': row['lead_id'],
                    'submission_id': None,
                    'task_id': row['task_id'],
                    'current_status': row['current_status'],
                    'exception_type': 'group_join_failure',
                    'reason': row['result_reason'],
                    'latest_action': latest_action,
                    'guild_name': row['guild_name'],
                    'mobile': row['mobile'],
                    'account_id': row['account_id'],
                    'registration_group': row['registration_group'],
                    'created_at': row['created_at'],
                })
            timeout_rows = [dict(r) for r in conn.execute(
                """
                SELECT s.submission_id, s.lead_id, s.created_at, COALESCE(l.current_status, '') AS current_status,
                       COALESCE(l.mobile, '') AS mobile, COALESCE(l.yw_id, '') AS account_id,
                       COALESCE(l.pendaftaran_group, '') AS registration_group, COALESCE(l.dept_name, '') AS guild_name
                FROM account_submissions s
                LEFT JOIN leads l ON l.lead_id = s.lead_id
                WHERE l.current_status IN ('account_submitted','bind_check_pending','bind_success','group_join_pending')
                ORDER BY s.created_at DESC
                LIMIT 100
                """
            ).fetchall()]
        now_dt = parse_iso_datetime(utc_now())
        for row in timeout_rows:
            created_dt = parse_iso_datetime(str(row['created_at']))
            if (now_dt - created_dt).total_seconds() < 300:
                continue
            rows.append({
                'lead_id': row['lead_id'],
                'submission_id': row['submission_id'],
                'task_id': None,
                'current_status': row['current_status'],
                'exception_type': 'submission_timeout',
                'reason': 'submission has not reached a terminal state within 5 minutes',
                'latest_action': 'inspect_timeline',
                'guild_name': row['guild_name'],
                'mobile': row['mobile'],
                'account_id': row['account_id'],
                'registration_group': row['registration_group'],
                'created_at': row['created_at'],
            })
        rows.sort(key=lambda item: str(item.get('created_at') or ''), reverse=True)
        return {'rows': rows[:100]}

    def sla_summary(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            submission_rows = [dict(r) for r in conn.execute(
                """
                SELECT s.submission_id, s.lead_id, s.created_at,
                       COALESCE(l.current_status, '') AS current_status,
                       COALESCE(l.dept_name, '') AS guild_name
                FROM account_submissions s
                LEFT JOIN leads l ON l.lead_id = s.lead_id
                ORDER BY s.created_at DESC
                LIMIT 500
                """
            ).fetchall()]
            failure_rows = [dict(r) for r in conn.execute(
                """
                SELECT notification_type, COALESCE(reason, '') AS reason, COUNT(*) AS cnt
                FROM operator_notifications
                WHERE write_result = 'failed'
                GROUP BY notification_type, COALESCE(reason, '')
                ORDER BY cnt DESC, notification_type ASC
                LIMIT 10
                """
            ).fetchall()]
        now_dt = parse_iso_datetime(utc_now())
        total = len(submission_rows)
        success_count = 0
        failed_count = 0
        pending_count = 0
        timeout_count = 0
        by_guild: Dict[str, Dict[str, Any]] = {}
        terminal_success_statuses = {'group_join_success', 'synced'}
        terminal_failure_statuses = {'bind_failed', 'group_join_failed'}
        for row in submission_rows:
            status = str(row.get('current_status') or '')
            guild = str(row.get('guild_name') or '').strip() or '-'
            bucket = by_guild.setdefault(guild, {'guild_name': guild, 'submission_count': 0, 'success_count': 0, 'failed_count': 0, 'pending_count': 0})
            bucket['submission_count'] += 1
            if status in terminal_success_statuses:
                success_count += 1
                bucket['success_count'] += 1
            elif status in terminal_failure_statuses:
                failed_count += 1
                bucket['failed_count'] += 1
            else:
                pending_count += 1
                bucket['pending_count'] += 1
            created_at = str(row.get('created_at') or '')
            if created_at:
                try:
                    if (now_dt - parse_iso_datetime(created_at)).total_seconds() >= 300 and status not in terminal_success_statuses | terminal_failure_statuses:
                        timeout_count += 1
                except Exception:
                    pass
        top_failure_reasons = [
            {
                'notification_type': row['notification_type'],
                'reason': row['reason'],
                'count': int(row['cnt'] or 0),
            }
            for row in failure_rows
        ]
        return {
            'submission_total': total,
            'success_count': success_count,
            'failed_count': failed_count,
            'pending_count': pending_count,
            'timeout_over_5m_count': timeout_count,
            'top_failure_reasons': top_failure_reasons,
            'by_guild': sorted(by_guild.values(), key=lambda item: item['guild_name']),
        }

    def delete_guild_executor(self, guild_name: str) -> Dict[str, Any]:
        normalized_guild_name = str(guild_name or '').strip()
        if not normalized_guild_name:
            raise HTTPException(status_code=400, detail='guild_name is required')
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT guild_name FROM guild_executors WHERE guild_name = ?",
                (normalized_guild_name,),
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail='guild executor not found')
            conn.execute("DELETE FROM guild_executors WHERE guild_name = ?", (normalized_guild_name,))
            conn.commit()
        return {'deleted': True, 'guild_name': normalized_guild_name}

    def update_guild_executor(self, guild_name: str, payload: GuildExecutorUpdateRequest) -> Dict[str, Any]:
        normalized_guild_name = str(guild_name or '').strip()
        if not normalized_guild_name:
            raise HTTPException(status_code=400, detail='guild_name is required')
        row = {
            'guild_name': normalized_guild_name,
            'backend_url': str(payload.backend_url or '').strip(),
            'login_username': str(payload.login_username or '').strip(),
            'password_secret_ref': str(payload.password_secret_ref or '').strip(),
            'proxy_url': str(payload.proxy_url or '').strip(),
            'proxy_region': str(payload.proxy_region or '').strip(),
            'proxy_type': str(payload.proxy_type or '').strip() or 'http',
            'enabled': 1 if payload.enabled else 0,
            'browser_profile_key': str(payload.browser_profile_key or '').strip(),
            'bind_concurrency': max(1, int(payload.bind_concurrency or 1)),
            'request_timeout_seconds': max(5, int(payload.request_timeout_seconds or 30)),
            'notes': str(payload.notes or '').strip(),
            'updated_at': utc_now(),
        }
        if not row['browser_profile_key']:
            slug = re.sub(r'[^a-z0-9]+', '-', normalized_guild_name.lower()).strip('-') or 'default'
            row['browser_profile_key'] = f'guild-{slug}'
        if not row['backend_url']:
            raise HTTPException(status_code=400, detail='backend_url is required')
        if not row['login_username']:
            raise HTTPException(status_code=400, detail='login_username is required')
        if row['proxy_region'] and row['proxy_region'] not in GUILD_EXECUTOR_PROXY_REGION_VALUES:
            raise HTTPException(status_code=400, detail='proxy_region must be one of the configured city options')
        with self.db.connect() as conn:
            if row['proxy_region']:
                existing_region_owner = conn.execute(
                    "SELECT guild_name FROM guild_executors WHERE proxy_region = ? AND guild_name != ? LIMIT 1",
                    (row['proxy_region'], row['guild_name']),
                ).fetchone()
                if existing_region_owner:
                    raise HTTPException(status_code=400, detail=f"proxy_region is already assigned to guild {existing_region_owner['guild_name']}")
            conn.execute(
                """
                INSERT INTO guild_executors (
                    guild_name, backend_url, login_username, password_secret_ref, proxy_url, proxy_region,
                    proxy_type, enabled, browser_profile_key, bind_concurrency, request_timeout_seconds,
                    notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_name)
                DO UPDATE SET backend_url = excluded.backend_url,
                              login_username = excluded.login_username,
                              password_secret_ref = CASE WHEN excluded.password_secret_ref != '' THEN excluded.password_secret_ref ELSE guild_executors.password_secret_ref END,
                              proxy_url = excluded.proxy_url,
                              proxy_region = excluded.proxy_region,
                              proxy_type = excluded.proxy_type,
                              enabled = excluded.enabled,
                              browser_profile_key = excluded.browser_profile_key,
                              bind_concurrency = excluded.bind_concurrency,
                              request_timeout_seconds = excluded.request_timeout_seconds,
                              notes = excluded.notes,
                              updated_at = excluded.updated_at
                """,
                (
                    row['guild_name'], row['backend_url'], row['login_username'], row['password_secret_ref'], row['proxy_url'], row['proxy_region'],
                    row['proxy_type'], row['enabled'], row['browser_profile_key'], row['bind_concurrency'], row['request_timeout_seconds'],
                    row['notes'], row['updated_at'],
                ),
            )
            conn.commit()
        return {
            'saved': True,
            'guild_name': row['guild_name'],
            'backend_url': row['backend_url'],
            'login_username': row['login_username'],
            'proxy_url': row['proxy_url'],
            'proxy_region': row['proxy_region'],
            'proxy_type': row['proxy_type'],
            'enabled': bool(row['enabled']),
            'browser_profile_key': row['browser_profile_key'],
            'bind_concurrency': row['bind_concurrency'],
            'request_timeout_seconds': row['request_timeout_seconds'],
            'notes': row['notes'],
            'password_configured': bool(row['password_secret_ref']),
            'updated_at': row['updated_at'],
        }

    def ensure_current_intake_preset(self) -> Dict[str, Any]:
        rows = self._fetch_intake_bot_preset_rows()
        existing = next((row for row in rows if row.get('profile_name') == 'current'), None)
        if existing:
            self.lark_default_app_name = str(existing.get('default_app') or '').strip() or self.lark_default_app_name
            self.lark_default_dept_name = str(existing.get('default_guild') or '').strip() or self.lark_default_dept_name
            if existing.get('app_id'):
                self.current_lark_app_id = existing.get('app_id')
            if self.lark_default_app_name:
                self._resolve_crm_app_mapping(self.lark_default_app_name)
            if self.lark_default_dept_name:
                self._resolve_crm_dept_mapping(self.lark_default_dept_name)
            return existing
        return self._upsert_intake_bot_preset_row(
            profile_name='current',
            app_id=self.current_lark_app_id,
            robot_name='current',
            default_app=self.lark_default_app_name or '',
            default_guild=self.lark_default_dept_name or '',
            enabled=1,
        )

    def resolve_intake_bot_preset(self, *, app_id: Optional[str] = None, profile_name: Optional[str] = None) -> Dict[str, Any]:
        current_row = self.ensure_current_intake_preset()
        rows = self._fetch_intake_bot_preset_rows()
        normalized_profile = str(profile_name or '').strip()
        normalized_app_id = str(app_id or '').strip()
        if normalized_profile:
            matched = next((row for row in rows if str(row.get('profile_name') or '').strip() == normalized_profile), None)
            if matched:
                return {**matched, 'matched_by': 'profile_name'}
        if normalized_app_id:
            matched = next((row for row in rows if str(row.get('app_id') or '').strip() == normalized_app_id), None)
            if matched:
                return {**matched, 'matched_by': 'app_id'}
        return {**current_row, 'matched_by': 'fallback_current'}

    def _normalize_crm_dropdown_options(self, rows: Any, *, candidate_keys: list[str]) -> list[dict[str, str]]:
        seen: set[str] = set()
        options: list[dict[str, str]] = []
        for row in rows or []:
            if isinstance(row, dict):
                raw_value = next((row.get(key) for key in candidate_keys if row.get(key)), None)
            else:
                raw_value = next((getattr(row, key, None) for key in candidate_keys if getattr(row, key, None)), None)
            value = str(raw_value or '').strip()
            if not value or value in seen:
                continue
            seen.add(value)
            options.append({'label': value, 'value': value})
        options.sort(key=lambda item: item['label'].lower())
        return options

    def _list_cached_crm_dropdown_options(self, *, option_type: str, candidate_keys: list[str]) -> list[dict[str, str]]:
        rows = list((self._crm_option_cache.get(option_type) or {}).values())
        return self._normalize_crm_dropdown_options(rows, candidate_keys=candidate_keys)

    def _crm_dropdown_candidate_keys(self, option_type: str) -> list[str]:
        if option_type == 'app':
            return ['name', 'ywName', 'appName', 'label', 'value']
        return ['deptName', 'name', 'label', 'value']

    def _list_crm_dropdown_options(self, *, option_type: str) -> Dict[str, Any]:
        candidate_keys = self._crm_dropdown_candidate_keys(option_type)
        if self.crm_adapter is not None:
            try:
                if option_type == 'app' and hasattr(self.crm_adapter, 'get_apps'):
                    rows = self.crm_adapter.get_apps()
                elif option_type == 'guild' and hasattr(self.crm_adapter, 'get_depts'):
                    rows = self.crm_adapter.get_depts()
                else:
                    rows = []
                self._cache_crm_option_rows(option_type=option_type, rows=rows, candidate_keys=candidate_keys)
                options = self._normalize_crm_dropdown_options(rows, candidate_keys=candidate_keys)
                return {'options': options, 'source': 'live' if options else 'unavailable'}
            except Exception as exc:
                print(f'Failed to load CRM dropdown options for {option_type}: {exc}')
        cached_options = self._list_cached_crm_dropdown_options(option_type=option_type, candidate_keys=candidate_keys)
        if cached_options:
            return {'options': cached_options, 'source': 'cache'}
        return {'options': [], 'source': 'unavailable'}

    def _cache_crm_option_rows(self, *, option_type: str, rows: Any, candidate_keys: list[str]) -> None:
        bucket = self._crm_option_cache.setdefault(option_type, {})
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            persisted = False
            for key in candidate_keys:
                raw_value = str(row.get(key) or '').strip()
                if raw_value:
                    bucket[raw_value.lower()] = dict(row)
                    if not persisted:
                        self._persist_crm_option_row(option_type=option_type, display_name=raw_value, row=dict(row))
                        persisted = True

    def _get_cached_crm_option_row(self, *, option_type: str, display_name: Optional[str]) -> Optional[Dict[str, Any]]:
        normalized_name = str(display_name or '').strip().lower()
        if not normalized_name:
            return None
        cached = self._crm_option_cache.get(option_type, {}).get(normalized_name)
        if not cached:
            return None
        row = dict(cached)
        row['_mapping_source'] = 'cache'
        return row

    def _resolve_crm_option_row(self, *, option_type: str, display_name: Optional[str]) -> Optional[Dict[str, Any]]:
        normalized_name = str(display_name or '').strip()
        if not normalized_name:
            return None
        if self.crm_adapter is None:
            return self._get_cached_crm_option_row(option_type=option_type, display_name=display_name)
        try:
            if option_type == 'app' and hasattr(self.crm_adapter, 'get_apps'):
                rows = self.crm_adapter.get_apps()
                candidate_keys = ['name', 'ywName', 'appName', 'label', 'value']
            elif option_type == 'guild' and hasattr(self.crm_adapter, 'get_depts'):
                rows = self.crm_adapter.get_depts()
                candidate_keys = ['deptName', 'name', 'label', 'value']
            else:
                return self._get_cached_crm_option_row(option_type=option_type, display_name=display_name)
        except Exception as exc:
            cached = self._get_cached_crm_option_row(option_type=option_type, display_name=display_name)
            if cached:
                return cached
            print(f'Failed to resolve CRM option row for {option_type}: {exc}')
            return {
                '_mapping_error': str(exc),
                '_mapping_source': 'unavailable',
            }

        self._cache_crm_option_rows(option_type=option_type, rows=rows, candidate_keys=candidate_keys)
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            for key in candidate_keys:
                raw_value = row.get(key)
                if str(raw_value or '').strip().lower() == normalized_name.lower():
                    live_row = dict(row)
                    live_row['_mapping_source'] = 'live'
                    return live_row
        return self._get_cached_crm_option_row(option_type=option_type, display_name=display_name)

    def _resolve_crm_app_mapping(self, app_name: Optional[str]) -> Dict[str, str]:
        row = self._resolve_crm_option_row(option_type='app', display_name=app_name)
        resolved_name = str(app_name or '').strip()
        if not row:
            return {'appName': resolved_name, 'appId': '', 'mapping_source': 'missing'}
        if row.get('_mapping_error'):
            return {
                'appName': resolved_name,
                'appId': '',
                'mapping_source': str(row.get('_mapping_source') or 'unavailable'),
                'mapping_error': str(row.get('_mapping_error') or ''),
            }
        return {
            'appName': str(
                row.get('name')
                or row.get('ywName')
                or row.get('appName')
                or resolved_name
            ).strip(),
            'appId': str(row.get('id') or row.get('appId') or row.get('value') or '').strip(),
            'mapping_source': str(row.get('_mapping_source') or 'live'),
        }

    def _resolve_crm_dept_mapping(self, dept_name: Optional[str], dept_id: Optional[str] = None) -> Dict[str, str]:
        resolved_dept_id = str(dept_id or '').strip()
        resolved_name = str(dept_name or '').strip()
        if resolved_dept_id:
            return {'deptName': resolved_name, 'deptId': resolved_dept_id, 'mapping_source': 'provided'}
        row = self._resolve_crm_option_row(option_type='guild', display_name=dept_name)
        if not row:
            return {'deptName': resolved_name, 'deptId': '', 'mapping_source': 'missing'}
        if row.get('_mapping_error'):
            return {
                'deptName': resolved_name,
                'deptId': '',
                'mapping_source': str(row.get('_mapping_source') or 'unavailable'),
                'mapping_error': str(row.get('_mapping_error') or ''),
            }
        return {
            'deptName': str(row.get('deptName') or row.get('name') or resolved_name).strip(),
            'deptId': str(row.get('deptId') or row.get('id') or row.get('value') or '').strip(),
            'mapping_source': str(row.get('_mapping_source') or 'live'),
        }

    def _precheck_crm_mapping_failure(self, *, resolved_app: Dict[str, Any], resolved_dept: Dict[str, Any]) -> Optional[str]:
        app_name = str(resolved_app.get('appName') or '').strip()
        app_id = str(resolved_app.get('appId') or '').strip()
        app_mapping_error = str(resolved_app.get('mapping_error') or '').strip()
        app_mapping_source = str(resolved_app.get('mapping_source') or '').strip()
        if app_name and not app_id:
            if app_mapping_source == 'unavailable' or 'get_apps' in app_mapping_error or 'non-json' in app_mapping_error.lower() or '502' in app_mapping_error:
                return 'Please retry once.'
            return 'CRM app mapping is missing. Please contact the administrator.'
        return None

    def _crm_response_looks_like_duplicate(self, crm_response: Dict[str, Any]) -> bool:
        code = crm_response.get('code')
        msg = str(crm_response.get('msg') or '')
        if code == 10002:
            return True
        lowered = msg.lower()
        return ('已存在' in msg) or ('duplicate' in lowered) or ('already exists' in lowered)

    def _extract_crm_duplicate_hints(self, crm_response: Dict[str, Any]) -> Dict[str, str]:
        msg = str(crm_response.get('msg') or '')
        hints: Dict[str, str] = {}
        id_match = re.search(r'用户\s*ID\s*(\d{6,12})', msg, flags=re.IGNORECASE)
        mobile_match = re.search(r'手机号码\s*(\d{6,15})', msg)
        if id_match:
            hints['yw_id'] = id_match.group(1)
        if mobile_match:
            hints['mobile'] = mobile_match.group(1)
        return hints

    def _crm_mobile_matches_expected(self, *, expected_mobile: Optional[str], actual_mobile: Optional[str]) -> bool:
        expected = str(expected_mobile or '').strip()
        actual = str(actual_mobile or '').strip()
        if not expected:
            return True
        if not actual:
            return False
        if expected == actual:
            return True
        expected_keys = self._official_group_phone_match_keys(phone=expected)
        actual_keys = self._official_group_phone_match_keys(phone=actual)
        if expected_keys.intersection(actual_keys):
            return True
        expected_digits = ''.join(ch for ch in expected if ch.isdigit())
        actual_digits = ''.join(ch for ch in actual if ch.isdigit())
        if expected_digits and actual_digits and expected_digits == actual_digits:
            return True
        for prefix in sorted(PHONE_PREFIX_COUNTRY_MAP.keys(), key=len, reverse=True):
            if expected_digits.startswith(prefix) and expected_digits[len(prefix):] == actual_digits:
                return True
            if actual_digits.startswith(prefix) and actual_digits[len(prefix):] == expected_digits:
                return True
        return False

    def _crm_row_matches_expected(
        self,
        row: Dict[str, Any],
        *,
        yw_id: Optional[str] = None,
        mobile: Optional[str] = None,
        app_name: Optional[str] = None,
        dept_name: Optional[str] = None,
        registration_group: Optional[str] = None,
        official_group: Optional[str] = None,
    ) -> bool:
        if not row:
            return False
        expected_pairs = [
            (str(yw_id or '').strip(), str(row.get('ywId') or '').strip()),
            (str(app_name or '').strip(), str(row.get('appName') or '').strip()),
            (str(dept_name or '').strip(), str(row.get('deptName') or '').strip()),
            (str(registration_group or '').strip(), str(row.get('pendaftaranGroup') or '').strip()),
            (str(official_group or '').strip(), str(row.get('wa') or '').strip()),
        ]
        for expected, actual in expected_pairs:
            if expected and expected != actual:
                return False
        return self._crm_mobile_matches_expected(expected_mobile=mobile, actual_mobile=row.get('mobile'))

    def _find_existing_customer_with_fallback(self, *, yw_id: Optional[str], mobile: Optional[str], crm_response: Optional[Dict[str, Any]] = None, app_name: Optional[str] = None, dept_name: Optional[str] = None, registration_group: Optional[str] = None, official_group: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if self.crm_adapter is None:
            return None
        attempts: list[Dict[str, Optional[str]]] = []
        seen: set[tuple[str, str]] = set()
        for candidate in [
            {'yw_id': yw_id, 'mobile': mobile},
            {'yw_id': yw_id, 'mobile': None},
            {'yw_id': None, 'mobile': mobile},
        ]:
            key = (str(candidate.get('yw_id') or ''), str(candidate.get('mobile') or ''))
            if key not in seen:
                seen.add(key)
                attempts.append(candidate)
        for candidate in [self._extract_crm_duplicate_hints(crm_response or {})] if crm_response else []:
            if candidate:
                key = (str(candidate.get('yw_id') or ''), str(candidate.get('mobile') or ''))
                if key not in seen:
                    seen.add(key)
                    attempts.append(candidate)
                for narrowed in [
                    {'yw_id': candidate.get('yw_id'), 'mobile': None},
                    {'yw_id': None, 'mobile': candidate.get('mobile')},
                ]:
                    key = (str(narrowed.get('yw_id') or ''), str(narrowed.get('mobile') or ''))
                    if key not in seen:
                        seen.add(key)
                        attempts.append(narrowed)
        for candidate in attempts:
            if not candidate.get('yw_id') and not candidate.get('mobile'):
                continue
            try:
                row = self.crm_adapter.find_customer(yw_id=candidate.get('yw_id'), mobile=candidate.get('mobile'))
            except Exception:
                row = None
            if row and self._crm_row_matches_expected(
                row,
                yw_id=yw_id,
                mobile=mobile,
                app_name=app_name,
                dept_name=dept_name,
                registration_group=registration_group,
                official_group=official_group,
            ):
                return row
        return None

    def _normalize_crm_failure_reason(self, crm_response: Dict[str, Any], *, fallback_found: bool) -> str:
        if self._crm_response_looks_like_duplicate(crm_response):
            if fallback_found:
                return 'Data duplication.'
            return 'Data duplication.'
        return 'CRM write was rejected.'

    def _validate_intake_preset_dropdown_value(self, *, field_name: str, option_type: str, value: str) -> str:
        normalized_value = str(value or '').strip()
        if not normalized_value:
            raise HTTPException(status_code=400, detail=f'{field_name} is required.')
        dropdown_state = self._list_crm_dropdown_options(option_type=option_type)
        options = dropdown_state.get('options') or []
        option_values = {str((item or {}).get('value') or '').strip() for item in options}
        option_values.discard('')
        if not option_values:
            raise HTTPException(status_code=400, detail=f'{field_name} CRM dropdown options are unavailable. Please restore CRM options first.')
        if normalized_value not in option_values:
            raise HTTPException(status_code=400, detail=f'{field_name} must be selected from CRM dropdown options.')
        return normalized_value

    def list_intake_bot_presets(self) -> Dict[str, Any]:
        self.ensure_current_intake_preset()
        all_rows = self._fetch_intake_bot_preset_rows()
        rows = [row for row in all_rows if str(row.get('profile_name') or '').strip() != 'current']
        if not rows:
            rows = all_rows
        app_dropdown = self._list_crm_dropdown_options(option_type='app')
        guild_dropdown = self._list_crm_dropdown_options(option_type='guild')
        return {
            'rows': rows,
            'app_options': app_dropdown.get('options') or [],
            'guild_options': guild_dropdown.get('options') or [],
            'app_options_source': str(app_dropdown.get('source') or 'unavailable'),
            'guild_options_source': str(guild_dropdown.get('source') or 'unavailable'),
        }

    def update_intake_bot_preset(self, profile_name: str, payload: IntakeBotPresetUpdateRequest) -> Dict[str, Any]:
        self.ensure_current_intake_preset()
        normalized_profile_name = str(profile_name or '').strip()
        if not normalized_profile_name:
            raise HTTPException(status_code=400, detail='profile_name is required')
        normalized_app = self._validate_intake_preset_dropdown_value(
            field_name='default_app',
            option_type='app',
            value=payload.default_app,
        )
        normalized_guild = self._validate_intake_preset_dropdown_value(
            field_name='default_guild',
            option_type='guild',
            value=payload.default_guild,
        )
        existing = next((row for row in self._fetch_intake_bot_preset_rows() if str(row.get('profile_name') or '').strip() == normalized_profile_name), None)
        normalized_app_id = str(payload.app_id or (existing or {}).get('app_id') or '').strip()
        if normalized_profile_name == 'current' and not normalized_app_id:
            normalized_app_id = str(self.current_lark_app_id or '').strip()
        if normalized_profile_name != 'current' and not normalized_app_id:
            raise HTTPException(status_code=400, detail='app_id is required when creating a new bot preset.')
        normalized_robot_name = str(payload.robot_name or (existing or {}).get('robot_name') or normalized_profile_name).strip() or normalized_profile_name
        saved_row = self._upsert_intake_bot_preset_row(
            profile_name=normalized_profile_name,
            app_id=normalized_app_id,
            robot_name=normalized_robot_name,
            default_app=normalized_app,
            default_guild=normalized_guild,
            enabled=int((existing or {}).get('enabled') or 1),
        )
        if normalized_profile_name == 'current':
            self.lark_default_app_name = normalized_app
            self.lark_default_dept_name = normalized_guild
            if normalized_app_id:
                self.current_lark_app_id = normalized_app_id
            # Best-effort prewarm so the write path can survive later CRM dropdown flakiness.
            self._resolve_crm_app_mapping(normalized_app)
            self._resolve_crm_dept_mapping(normalized_guild)
        return {
            'saved': True,
            **saved_row,
        }

    def daily_summary(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            lead_count = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            engaged_count = conn.execute("SELECT COUNT(*) FROM lead_events WHERE event_type IN ('contact_clicked', 'wa_redirected', 'account_id_submitted')").fetchone()[0]
            account_submitted_count = conn.execute("SELECT COUNT(*) FROM lead_events WHERE event_type = 'account_id_submitted'").fetchone()[0]
            success_count = conn.execute("SELECT COUNT(*) FROM automation_tasks WHERE status = 'success'").fetchone()[0]
            failed_count = conn.execute("SELECT COUNT(*) FROM automation_tasks WHERE status = 'failed'").fetchone()[0]
            pending_count = conn.execute("SELECT COUNT(*) FROM automation_tasks WHERE status IN ('pending', 'running', 'retry_waiting')").fetchone()[0]
            task_count = conn.execute("SELECT COUNT(*) FROM automation_tasks").fetchone()[0]
        return {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "lead_count": lead_count,
            "engaged_count": engaged_count,
            "account_submitted_count": account_submitted_count,
            "task_count": task_count,
            "completed_task_count": success_count,
            "success_count": success_count,
            "failed_count": failed_count,
            "pending_count": pending_count,
            "top_fail_reasons": [],
            "group_breakdown": [],
            "operator_breakdown": [],
        }


def create_app(settings: Optional[Dict[str, Any]] = None) -> FastAPI:
    cfg = {"DB_PATH": DEFAULT_DB_PATH}
    if settings:
        cfg.update(settings)
    db = Database(cfg["DB_PATH"])
    crm_adapter = cfg.get('CRM_ADAPTER')
    ocr_adapter = cfg.get('OCR_ADAPTER')
    lark_media_adapter = cfg.get('LARK_MEDIA_ADAPTER')
    lark_reply_adapter = cfg.get('LARK_REPLY_ADAPTER')
    lark_reply_adapter_by_app_id = cfg.get('LARK_REPLY_ADAPTER_BY_APP_ID') or {}
    registration_group_approval_executor = cfg.get('REGISTRATION_GROUP_APPROVAL_EXECUTOR')
    registration_group_approval_executor_kind = cfg.get('REGISTRATION_GROUP_APPROVAL_EXECUTOR_KIND') or os.getenv('REGISTRATION_GROUP_APPROVAL_EXECUTOR_KIND')
    official_group_approval_executor = cfg.get('OFFICIAL_GROUP_APPROVAL_EXECUTOR')
    official_group_approval_executor_kind = cfg.get('OFFICIAL_GROUP_APPROVAL_EXECUTOR_KIND') or os.getenv('OFFICIAL_GROUP_APPROVAL_EXECUTOR_KIND')
    official_group_approval_webhook_url = cfg.get('OFFICIAL_GROUP_APPROVAL_WEBHOOK_URL') or os.getenv('OFFICIAL_GROUP_APPROVAL_WEBHOOK_URL')
    official_group_approval_webhook_token = cfg.get('OFFICIAL_GROUP_APPROVAL_WEBHOOK_TOKEN') or os.getenv('OFFICIAL_GROUP_APPROVAL_WEBHOOK_TOKEN')
    official_group_approval_webhook_session = cfg.get('OFFICIAL_GROUP_APPROVAL_WEBHOOK_SESSION')
    official_group_approval_webhook_timeout_seconds = cfg.get('OFFICIAL_GROUP_APPROVAL_WEBHOOK_TIMEOUT_SECONDS') or os.getenv('OFFICIAL_GROUP_APPROVAL_WEBHOOK_TIMEOUT_SECONDS') or 20
    official_group_target_map_raw = cfg.get('OFFICIAL_GROUP_TARGET_MAP') or os.getenv('OFFICIAL_GROUP_TARGET_MAP') or '{}'
    official_group_target_map = {}
    if isinstance(official_group_target_map_raw, dict):
        official_group_target_map = {
            str(k).strip(): str(v).strip()
            for k, v in official_group_target_map_raw.items()
            if str(k).strip() and str(v).strip()
        }
    else:
        try:
            parsed_official_group_target_map = json.loads(official_group_target_map_raw)
            if isinstance(parsed_official_group_target_map, dict):
                official_group_target_map = {
                    str(k).strip(): str(v).strip()
                    for k, v in parsed_official_group_target_map.items()
                    if str(k).strip() and str(v).strip()
                }
        except Exception:
            official_group_target_map = {}
    media_cache_dir = cfg.get('MEDIA_CACHE_DIR')
    lark_default_app_name = cfg.get('LARK_DEFAULT_APP_NAME') or os.getenv('LARK_DEFAULT_APP_NAME')
    lark_default_dept_name = cfg.get('LARK_DEFAULT_DEPT_NAME') or os.getenv('LARK_DEFAULT_DEPT_NAME')
    app_id = cfg.get('LARK_APP_ID') or cfg.get('FEISHU_APP_ID') or os.getenv('LARK_APP_ID') or os.getenv('FEISHU_APP_ID')
    app_secret = cfg.get('LARK_APP_SECRET') or cfg.get('FEISHU_APP_SECRET') or os.getenv('LARK_APP_SECRET') or os.getenv('FEISHU_APP_SECRET')
    app_domain = cfg.get('LARK_DOMAIN') or cfg.get('FEISHU_DOMAIN') or os.getenv('LARK_DOMAIN') or os.getenv('FEISHU_DOMAIN') or 'lark'
    crm_base_url = cfg.get('CRM_BASE_URL') or os.getenv('CRM_BASE_URL')
    crm_username = cfg.get('CRM_USERNAME') or os.getenv('CRM_USERNAME')
    crm_password = cfg.get('CRM_PASSWORD') or os.getenv('CRM_PASSWORD')
    auto_bind_simulation = bool(cfg.get('AUTO_BIND_SIMULATION') or str(os.getenv('AUTO_BIND_SIMULATION') or '').strip().lower() in {'1', 'true', 'yes', 'on'})
    allow_live_bind_simulation = bool(cfg.get('ALLOW_LIVE_BIND_SIMULATION') or str(os.getenv('ALLOW_LIVE_BIND_SIMULATION') or '').strip().lower() in {'1', 'true', 'yes', 'on'})
    if auto_bind_simulation and cfg["DB_PATH"] != ':memory:' and not allow_live_bind_simulation:
        auto_bind_simulation = False
    bind_simulator = cfg.get('BIND_SIMULATOR')
    real_bind_executor = cfg.get('REAL_BIND_EXECUTOR')
    enable_chrome_bind_executor = bool(cfg.get('ENABLE_CHROME_BIND_EXECUTOR') or str(os.getenv('ENABLE_CHROME_BIND_EXECUTOR') or '').strip().lower() in {'1', 'true', 'yes', 'on'})
    chrome_profile_map_raw = cfg.get('BIND_CHROME_PROFILE_MAP') or os.getenv('BIND_CHROME_PROFILE_MAP') or '{}'
    chrome_profile_map = {}
    if not real_bind_executor and enable_chrome_bind_executor:
        try:
            parsed_profile_map = json.loads(chrome_profile_map_raw)
            if isinstance(parsed_profile_map, dict):
                chrome_profile_map = {str(k): str(v) for k, v in parsed_profile_map.items()}
        except Exception:
            chrome_profile_map = {}
        if chrome_profile_map:
            from app.live_bind_executor import LiveChromeBindExecutor
            real_bind_executor = LiveChromeBindExecutor(
                profile_map=chrome_profile_map,
                chrome_binary=cfg.get('CHROME_BINARY') or os.getenv('CHROME_BINARY'),
                chrome_user_data_root=cfg.get('CHROME_USER_DATA_ROOT') or os.getenv('CHROME_USER_DATA_ROOT'),
            )
    auto_bind_simulation_success_rate = cfg.get('AUTO_BIND_SIMULATION_SUCCESS_RATE') or os.getenv('AUTO_BIND_SIMULATION_SUCCESS_RATE') or 0.5
    auto_bind_simulation_seed = cfg.get('AUTO_BIND_SIMULATION_SEED')
    if auto_bind_simulation_seed is None and os.getenv('AUTO_BIND_SIMULATION_SEED'):
        auto_bind_simulation_seed = int(os.getenv('AUTO_BIND_SIMULATION_SEED'))
    ingress_async_default = (
        bool(cfg.get('INGRESS_ASYNC_DEFAULT'))
        if 'INGRESS_ASYNC_DEFAULT' in cfg
        else (cfg["DB_PATH"] != ':memory:' and str(os.getenv('INGRESS_ASYNC_DEFAULT') or 'true').strip().lower() in {'1', 'true', 'yes', 'on'})
    )
    ingress_worker_enabled = (
        bool(cfg.get('INGRESS_WORKER_ENABLED'))
        if 'INGRESS_WORKER_ENABLED' in cfg
        else (ingress_async_default and str(os.getenv('INGRESS_WORKER_ENABLED') or 'true').strip().lower() in {'1', 'true', 'yes', 'on'})
    )
    ingress_worker_poll_interval = cfg.get('INGRESS_WORKER_POLL_INTERVAL') or os.getenv('INGRESS_WORKER_POLL_INTERVAL') or 0.5
    ingress_worker_count = cfg.get('INGRESS_WORKER_COUNT') or os.getenv('INGRESS_WORKER_COUNT') or 1
    ingress_rate_limit_per_minute = cfg.get('INGRESS_RATE_LIMIT_PER_MINUTE') or os.getenv('INGRESS_RATE_LIMIT_PER_MINUTE') or 600
    external_call_rate_limit_per_minute = cfg.get('EXTERNAL_CALL_RATE_LIMIT_PER_MINUTE') or os.getenv('EXTERNAL_CALL_RATE_LIMIT_PER_MINUTE') or 300
    require_invite_code = (
        bool(cfg.get('REQUIRE_INVITE_CODE'))
        if 'REQUIRE_INVITE_CODE' in cfg
        else cfg["DB_PATH"] != ':memory:'
    )
    crm_retry_delays_seconds = cfg.get('CRM_RETRY_DELAYS_SECONDS')
    if crm_retry_delays_seconds is None:
        raw_retry_delays = os.getenv('CRM_RETRY_DELAYS_SECONDS')
        if raw_retry_delays:
            crm_retry_delays_seconds = [part.strip() for part in str(raw_retry_delays).split(',') if str(part).strip()]
    crm_retry_max_attempts = cfg.get('CRM_RETRY_MAX_ATTEMPTS') or os.getenv('CRM_RETRY_MAX_ATTEMPTS') or 3
    bind_retry_max_attempts = cfg.get('BIND_RETRY_MAX_ATTEMPTS') or os.getenv('BIND_RETRY_MAX_ATTEMPTS') or 2
    crm_login_error = None
    if crm_adapter is None and crm_base_url and crm_username and crm_password:
        candidate_crm_adapter = LiveCrmAdapter(
            base_url=crm_base_url,
            username=crm_username,
            password=crm_password,
        )
        crm_adapter = candidate_crm_adapter
        try:
            candidate_crm_adapter.login()
        except Exception as exc:
            crm_login_error = str(exc)
            print(f'CRM login degraded at startup: {crm_login_error}')
    if ocr_adapter is None and ((cfg.get('ENABLE_RAPIDOCR') is True) or str(cfg.get('ENABLE_RAPIDOCR') or os.getenv('ENABLE_RAPIDOCR') or '').strip().lower() in {'1', 'true', 'yes', 'on'}):
        ocr_adapter = RapidOcrAdapter()
    normalized_registration_group_executor_kind = str(registration_group_approval_executor_kind or '').strip().lower()
    if registration_group_approval_executor is None and normalized_registration_group_executor_kind == 'live_whatsapp':
        from app.registration_group_executor import LiveWarmWhatsAppRegistrationGroupApprovalExecutor
        registration_group_initial_wait_ms = int(cfg.get('WHATSAPP_INITIAL_WAIT_MS') or os.getenv('WHATSAPP_INITIAL_WAIT_MS') or 500)
        registration_group_navigation_wait_ms = int(cfg.get('WHATSAPP_NAVIGATION_WAIT_MS') or os.getenv('WHATSAPP_NAVIGATION_WAIT_MS') or 120)
        registration_group_post_click_wait_ms = int(cfg.get('WHATSAPP_POST_CLICK_WAIT_MS') or os.getenv('WHATSAPP_POST_CLICK_WAIT_MS') or 80)
        registration_group_verify_timeout_ms = int(cfg.get('WHATSAPP_VERIFY_TIMEOUT_MS') or os.getenv('WHATSAPP_VERIFY_TIMEOUT_MS') or 1200)
        registration_group_verify_poll_ms = int(cfg.get('WHATSAPP_VERIFY_POLL_MS') or os.getenv('WHATSAPP_VERIFY_POLL_MS') or 80)
        registration_group_strict_reload_verify = str(cfg.get('WHATSAPP_STRICT_RELOAD_VERIFY') or os.getenv('WHATSAPP_STRICT_RELOAD_VERIFY') or 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
        registration_group_approval_executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(
            chrome_user_data_root=cfg.get('WHATSAPP_CHROME_USER_DATA_ROOT') or os.getenv('WHATSAPP_CHROME_USER_DATA_ROOT') or cfg.get('CHROME_USER_DATA_ROOT') or os.getenv('CHROME_USER_DATA_ROOT'),
            profile_dir=cfg.get('WHATSAPP_PROFILE_DIR') or os.getenv('WHATSAPP_PROFILE_DIR') or 'Profile 25',
            registration_list_item_index=int(cfg.get('WHATSAPP_REGISTRATION_LIST_ITEM_INDEX') or os.getenv('WHATSAPP_REGISTRATION_LIST_ITEM_INDEX') or 0),
            registration_group_name=cfg.get('WHATSAPP_REGISTRATION_GROUP_NAME') or os.getenv('WHATSAPP_REGISTRATION_GROUP_NAME') or '8️⃣5️⃣',
            temp_user_data_dir=cfg.get('WHATSAPP_REGISTRATION_APPROVAL_TEMP_DIR') or os.getenv('WHATSAPP_REGISTRATION_APPROVAL_TEMP_DIR') or '/tmp/chrome-whatsapp-registration-group-approval',
            initial_wait_ms=registration_group_initial_wait_ms,
            navigation_wait_ms=registration_group_navigation_wait_ms,
            post_click_wait_ms=registration_group_post_click_wait_ms,
            verify_timeout_ms=registration_group_verify_timeout_ms,
            verify_poll_ms=registration_group_verify_poll_ms,
            strict_reload_verify=registration_group_strict_reload_verify,
        )
    if registration_group_approval_executor is None and normalized_registration_group_executor_kind == 'webjs_bridge':
        from app.registration_group_webjs_executor import WebjsBridgeRegistrationGroupApprovalExecutor
        registration_group_approval_executor = WebjsBridgeRegistrationGroupApprovalExecutor(
            base_url=cfg.get('REGISTRATION_GROUP_APPROVAL_WEBJS_BASE_URL') or os.getenv('REGISTRATION_GROUP_APPROVAL_WEBJS_BASE_URL') or 'http://127.0.0.1:8787',
            token=cfg.get('REGISTRATION_GROUP_APPROVAL_WEBJS_TOKEN') or os.getenv('REGISTRATION_GROUP_APPROVAL_WEBJS_TOKEN'),
            timeout_seconds=float(cfg.get('REGISTRATION_GROUP_APPROVAL_WEBJS_TIMEOUT_SECONDS') or os.getenv('REGISTRATION_GROUP_APPROVAL_WEBJS_TIMEOUT_SECONDS') or 35),
        )
    if official_group_approval_executor is None and str(official_group_approval_executor_kind or '').strip().lower() == 'webhook' and official_group_approval_webhook_url:
        from app.official_group_executor import WebhookOfficialGroupApprovalExecutor
        official_group_approval_executor = WebhookOfficialGroupApprovalExecutor(
            webhook_url=official_group_approval_webhook_url,
            token=official_group_approval_webhook_token,
            session=official_group_approval_webhook_session,
            timeout_seconds=float(official_group_approval_webhook_timeout_seconds or 20),
        )
    auto_lark_reply = cfg.get('AUTO_LARK_REPLY', True)
    if auto_lark_reply and lark_reply_adapter is None and app_id and app_secret:
        lark_reply_adapter = LiveLarkReplyAdapter(app_id=app_id, app_secret=app_secret, domain=app_domain)
    service = Service(
        db,
        crm_adapter=crm_adapter,
        ocr_adapter=ocr_adapter,
        lark_media_adapter=lark_media_adapter,
        lark_reply_adapter=lark_reply_adapter,
        lark_reply_adapter_by_app_id=lark_reply_adapter_by_app_id,
        media_cache_dir=media_cache_dir,
        lark_default_app_name=lark_default_app_name,
        lark_default_dept_name=lark_default_dept_name,
        current_lark_app_id=app_id,
        auto_bind_simulation=auto_bind_simulation,
        bind_simulator=bind_simulator,
        real_bind_executor=real_bind_executor,
        registration_group_approval_executor=registration_group_approval_executor,
        official_group_approval_executor=official_group_approval_executor,
        official_group_target_map=official_group_target_map,
        auto_bind_simulation_success_rate=auto_bind_simulation_success_rate,
        auto_bind_simulation_seed=auto_bind_simulation_seed,
        crm_base_url=crm_base_url,
        crm_username=crm_username,
        crm_login_error=crm_login_error,
        ingress_async_default=ingress_async_default,
        ingress_worker_enabled=ingress_worker_enabled,
        ingress_worker_poll_interval=ingress_worker_poll_interval,
        ingress_worker_count=ingress_worker_count,
        ingress_rate_limit_per_minute=ingress_rate_limit_per_minute,
        external_call_rate_limit_per_minute=external_call_rate_limit_per_minute,
        require_invite_code=require_invite_code,
        crm_retry_delays_seconds=crm_retry_delays_seconds,
        crm_retry_max_attempts=int(crm_retry_max_attempts or 3),
        bind_retry_max_attempts=int(bind_retry_max_attempts or 2),
        official_group_approval_webhook_url=official_group_approval_webhook_url,
    )
    _schedule_registration_group_executor_warmup(registration_group_approval_executor)
    print(
        json.dumps(
            {
                'startup_health': service.runtime_health(),
            },
            ensure_ascii=False,
        )
    )
    service.ensure_current_intake_preset()
    app = FastAPI(title="MCN AI Automation")
    app.state.service = service

    def _official_group_bridge_console_base_url() -> Optional[str]:
        webhook_url = str(official_group_approval_webhook_url or '').strip()
        if not webhook_url:
            return None
        return webhook_url.replace('/official-group/approve', '')

    def _official_group_bridge_summary_payload() -> Dict[str, Any]:
        base_url = _official_group_bridge_console_base_url()
        if not base_url:
            return {
                'configured': False,
                'health': {},
                'summary': {},
            }

        def _get_json(url: str) -> Dict[str, Any]:
            response = requests.get(url, timeout=10.0)
            response.raise_for_status()
            return response.json()

        try:
            health = _get_json(f"{base_url}/ops/official-group-bridge/health")
        except Exception as exc:
            health = {'status': 'unreachable', 'error': str(exc)}
        try:
            summary = _get_json(f"{base_url}/ops/official-group-bridge/summary")
        except Exception as exc:
            summary = {'status': 'unreachable', 'error': str(exc)}
        return {
            'configured': True,
            'base_url': base_url,
            'health': health,
            'summary': summary,
        }

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get('/ops', response_class=HTMLResponse)
    def ops_page() -> str:
        return OPS_PAGE_HTML

    @app.get('/ops/intake-bot-presets', response_class=HTMLResponse)
    def intake_bot_presets_page() -> str:
        return INTAKE_BOT_PRESETS_PAGE_HTML

    @app.get('/ops/production-ops', response_class=HTMLResponse)
    def production_ops_page() -> str:
        return PRODUCTION_OPS_PAGE_HTML

    @app.get('/ops/official-group-bridge')
    def official_group_bridge_page_redirect() -> RedirectResponse:
        bridge_url = str(official_group_approval_webhook_url or '').strip()
        if not bridge_url:
            raise HTTPException(status_code=404, detail='official_group_bridge_not_configured')
        bridge_page_url = bridge_url.replace('/official-group/approve', '/ops/official-group-bridge')
        return RedirectResponse(url=bridge_page_url, status_code=307)

    @app.get('/api/ops/runtime-health')
    def ops_runtime_health() -> Dict[str, Any]:
        return service.runtime_health()

    @app.get('/api/ops/registration-group-approval-executor-health')
    def ops_registration_group_approval_executor_health() -> Dict[str, Any]:
        return service.registration_group_approval_executor_health()

    @app.post('/api/ops/registration-group-approval-executor-warmup')
    def ops_registration_group_approval_executor_warmup() -> Dict[str, Any]:
        return service.registration_group_approval_executor_warmup()

    @app.get('/api/ops/registration-group-approval-executor-group-state')
    def ops_registration_group_approval_executor_group_state(registration_group: str) -> Dict[str, Any]:
        return service.registration_group_approval_executor_group_state(registration_group)

    @app.get('/api/ops/ingress-queue')
    def ops_ingress_queue() -> Dict[str, Any]:
        return service.list_ingress_queue()

    @app.post('/api/ops/ingress-queue/run-next')
    def ops_ingress_queue_run_next() -> Dict[str, Any]:
        processed = service.process_next_worker_tick()
        if not processed:
            return {'processed': False}
        if 'status' in processed:
            return processed
        if 'task_id' in processed:
            normalized = dict(processed)
            normalized.setdefault('status', 'success')
            return normalized
        return processed

    @app.get('/api/ops/operator-audit-log')
    def ops_operator_audit_log(limit: int = 200) -> Dict[str, Any]:
        return service.operator_audit_log(limit=limit)

    @app.post('/api/leads/upsert')
    def leads_upsert(payload: LeadUpsertRequest) -> Dict[str, Any]:
        return service.upsert_lead(payload)

    @app.post("/api/events/collect")
    def events_collect(payload: EventCollectRequest) -> Dict[str, Any]:
        return service.collect_event(payload)

    @app.post("/api/tasks/create")
    def tasks_create(payload: TaskCreateRequest) -> Dict[str, Any]:
        return service.create_task(payload)

    @app.post("/api/tasks/{task_id}/result")
    def tasks_result(task_id: str, payload: TaskResultRequest) -> Dict[str, Any]:
        return service.task_result(task_id, payload)

    @app.post("/api/crm/customer-sync")
    def customer_sync(payload: CustomerSyncRequest):
        return service.customer_sync(payload)

    @app.post("/api/account-submissions")
    def account_submissions(payload: AccountSubmissionRequest):
        return service.submit_account(payload)

    @app.post("/api/intake/manual-cs-submissions")
    def manual_cs_submissions(payload: ManualCsSubmissionRequest):
        return service.submit_manual_cs(payload)

    @app.post("/api/intake/lark/events")
    def lark_events(payload: Dict[str, Any] = Body(...)):
        return service.handle_lark_event(payload)

    @app.post("/api/tasks/{task_id}/recognition-result")
    def recognition_result(task_id: str, payload: RecognitionResultRequest):
        return service.recognition_result(task_id, payload)

    @app.post("/api/tasks/{task_id}/native-ocr-run")
    def native_ocr_run(task_id: str):
        return service.run_native_ocr(task_id)

    @app.post("/api/tasks/{task_id}/bind-check-result")
    def bind_check_result(task_id: str, payload: BindCheckResultRequest):
        return service.bind_check_result(task_id, payload)

    @app.post("/api/tasks/{task_id}/group-join-result")
    def group_join_result(task_id: str, payload: GroupJoinResultRequest):
        return service.group_join_result(task_id, payload)

    @app.get("/api/leads/{lead_id}/timeline")
    def lead_timeline(lead_id: str):
        return service.lead_timeline(lead_id)

    @app.post("/api/leads/{lead_id}/voucher-attach")
    def voucher_attach(lead_id: str, payload: VoucherAttachRequest):
        return service.attach_voucher_for_lead(lead_id, payload.image_path, payload.remark_suffix)

    @app.post("/api/registration-groups/approval-batches")
    def registration_group_approval_batches(payload: RegistrationGroupApprovalBatchRequest):
        return service.create_registration_group_approval_batch(payload)

    @app.post("/api/registration-groups/approval-decisions")
    def registration_group_approval_decisions(payload: RegistrationGroupApprovalDecisionRequest):
        return service.registration_group_approval_decision(payload)

    @app.get("/api/registration-groups/approval-decisions/{approval_run_id}")
    def registration_group_approval_decision_status(approval_run_id: str):
        return service.registration_group_approval_decision_status(approval_run_id)

    @app.post("/api/official-groups/approval-checks")
    def official_group_approval_checks(payload: OfficialGroupApprovalCheckRequest):
        return service.official_group_approval_check(payload)

    @app.post("/api/official-groups/approval-decisions")
    def official_group_approval_decisions(payload: OfficialGroupApprovalDecisionRequest):
        return service.official_group_approval_decision(payload)

    @app.post('/api/ops/leads/{lead_id}/retry-official-group-approval')
    def ops_retry_official_group_approval(lead_id: str, payload: OfficialGroupApprovalRetryRequest):
        return service.retry_official_group_approval(lead_id, payload)

    @app.get('/api/ops/official-group-approval-executor-health')
    def ops_official_group_approval_executor_health():
        return service.official_group_approval_executor_health()

    @app.get('/api/ops/official-group-approval-summary')
    def ops_official_group_approval_summary():
        return service.official_group_approval_summary()

    @app.post('/api/ops/official-group-approval-batches/run-ready')
    def ops_run_ready_official_group_batches(payload: OfficialGroupBatchRunRequest):
        return service.run_ready_official_group_batches(payload)

    @app.get("/api/ops/manual-review-queue")
    def ops_manual_review_queue():
        return service.ops_manual_review_queue()

    @app.post("/api/ops/manual-review/{lead_id}/resolve")
    def ops_manual_review_resolve(lead_id: str, payload: ManualReviewResolveRequest):
        return service.resolve_manual_review(lead_id, payload)

    @app.get("/api/ops/bind-queue")
    def ops_bind_queue():
        return service.ops_bind_queue()

    @app.get("/api/ops/group-queue")
    def ops_group_queue():
        return service.ops_group_queue()

    @app.get("/api/ops/dashboard/summary")
    def ops_dashboard_summary():
        return service.ops_dashboard_summary()

    @app.get('/api/ops/intake-bot-presets')
    def ops_intake_bot_presets():
        return service.list_intake_bot_presets()

    @app.get('/api/ops/intake-bot-presets/resolve')
    def ops_resolve_intake_bot_preset(app_id: Optional[str] = None, profile_name: Optional[str] = None):
        return service.resolve_intake_bot_preset(app_id=app_id, profile_name=profile_name)

    @app.post('/api/ops/intake-bot-presets/{profile_name}')
    def ops_intake_bot_preset_update(profile_name: str, payload: IntakeBotPresetUpdateRequest):
        return service.update_intake_bot_preset(profile_name, payload)

    @app.get('/api/ops/guild-executors')
    def ops_guild_executors():
        return service.list_guild_executors()

    @app.get('/api/ops/production-ops-daemon')
    def ops_production_ops_daemon():
        return service.get_production_ops_daemon_config()

    @app.get('/api/ops/whatsapp-approval-accounts')
    def ops_whatsapp_approval_accounts():
        return service.list_whatsapp_approval_accounts()

    @app.get('/api/ops/whatsapp-approval-accounts/{account_key}/runtime')
    def ops_whatsapp_approval_account_runtime(account_key: str):
        return service.get_whatsapp_approval_account_runtime(account_key)

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/runtime/start')
    def ops_whatsapp_approval_account_runtime_start(account_key: str):
        return service.start_whatsapp_approval_account_runtime(account_key)

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/runtime/stop')
    def ops_whatsapp_approval_account_runtime_stop(account_key: str):
        return service.stop_whatsapp_approval_account_runtime(account_key)

    @app.get('/api/ops/whatsapp-approval-accounts/{account_key}/session')
    def ops_whatsapp_approval_account_session(account_key: str):
        return service.get_whatsapp_approval_account_session(account_key)

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/session/start')
    def ops_whatsapp_approval_account_session_start(account_key: str):
        return service.start_whatsapp_approval_account_session(account_key)

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}/session/reset')
    def ops_whatsapp_approval_account_session_reset(account_key: str):
        return service.reset_whatsapp_approval_account_session(account_key)

    @app.get('/api/ops/whatsapp-approval-area-options')
    def ops_whatsapp_approval_area_options():
        return service.list_whatsapp_approval_area_options()

    @app.post('/api/ops/whatsapp-approval-area-options')
    def ops_whatsapp_approval_area_options_update(payload: WhatsAppApprovalAreaOptionsUpdateRequest):
        return service.update_whatsapp_approval_area_options(payload)

    @app.get('/api/ops/whatsapp-approval-candidates')
    def ops_whatsapp_approval_candidates():
        return service.list_whatsapp_approval_candidates()

    @app.post('/api/ops/whatsapp-approval-accounts/{account_key}')
    def ops_whatsapp_approval_account_update(account_key: str, payload: WhatsAppApprovalAccountUpdateRequest):
        return service.update_whatsapp_approval_account(account_key, payload)

    @app.delete('/api/ops/whatsapp-approval-accounts/{account_key}')
    def ops_whatsapp_approval_account_delete(account_key: str):
        return service.delete_whatsapp_approval_account(account_key)

    @app.get('/api/ops/official-group-bridge-summary')
    def ops_official_group_bridge_summary():
        return _official_group_bridge_summary_payload()

    @app.post('/api/ops/production-ops-daemon')
    def ops_production_ops_daemon_update(payload: ProductionOpsDaemonConfigUpdateRequest):
        return service.update_production_ops_daemon_config(payload)

    @app.get('/api/ops/guild-executors/health')
    def ops_guild_executors_health():
        return service.guild_executor_health()

    @app.post('/api/ops/submissions/{submission_id}/retry-bind')
    def ops_retry_bind_submission(submission_id: str):
        return service.retry_bind_submission(submission_id)

    @app.post('/api/ops/submissions/{submission_id}/retry-crm')
    def ops_retry_crm_submission(submission_id: str):
        return service.retry_crm_submission(submission_id)

    @app.post('/api/ops/submissions/{submission_id}/resubmit')
    def ops_resubmit_submission(submission_id: str, payload: SubmissionResubmitRequest):
        return service.resubmit_corrected_submission(submission_id, payload)

    @app.get('/api/ops/exception-queue')
    def ops_exception_queue():
        return service.exception_queue()

    @app.get('/api/ops/sla-summary')
    def ops_sla_summary():
        return service.sla_summary()

    @app.get('/api/ops/guild-executors/{guild_name}')
    def ops_guild_executor(guild_name: str):
        return service.get_guild_executor(guild_name)

    @app.delete('/api/ops/guild-executors/{guild_name}')
    def ops_guild_executor_delete(guild_name: str):
        return service.delete_guild_executor(guild_name)

    @app.post('/api/ops/guild-executors/{guild_name}')
    def ops_guild_executor_update(guild_name: str, payload: GuildExecutorUpdateRequest):
        return service.update_guild_executor(guild_name, payload)

    @app.get("/api/ops/next-bind-task")
    def ops_next_bind_task():
        return service.ops_next_bind_task()

    @app.get("/api/ops/next-group-task")
    def ops_next_group_task():
        return service.ops_next_group_task()

    @app.get("/api/ops/next-action")
    def ops_next_action():
        return service.ops_next_action()

    @app.get("/api/ops/operator-notifications")
    def ops_operator_notifications(status: Optional[str] = None, query: Optional[str] = None):
        return service.operator_notifications(status=status, query=query)

    @app.post("/api/ops/operator-notifications/{notification_id}/read")
    def ops_operator_notification_read(notification_id: str, payload: NotificationReadRequest = Body(...)):
        return service.mark_operator_notification_read(notification_id, read_by=payload.read_by)

    @app.get("/api/ops/parser-quality-summary")
    def ops_parser_quality_summary():
        return service.parser_quality_summary()

    @app.post("/api/ops/approval-batches/evaluate")
    def ops_approval_batches_evaluate(payload: ApprovalBatchEvaluateRequest):
        return service.evaluate_approval_batch(payload)

    @app.get("/api/ops/approval-batch-queue")
    def ops_approval_batch_queue():
        return service.approval_batch_queue()

    @app.get("/api/reports/funnel")
    def reports_funnel():
        return service.funnel_report()

    @app.get("/api/reports/daily-summary")
    def reports_daily_summary():
        return service.daily_summary()

    return app


app = create_app()
