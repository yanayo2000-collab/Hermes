from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.async_pipeline import CircuitBreaker, TokenBucketRateLimiter, fingerprint_payload
from app.crm_adapter import LiveCrmAdapter
from app.native_ocr import normalize_native_ocr_fields
from app.ocr_adapter import RapidOcrAdapter


PHONE_PREFIX_COUNTRY_MAP = {
    '62': 'Indonesia',
    '52': 'Mexico',
    '55': 'Brazil',
}

GLOBAL_PHONE_PATTERN = re.compile(r'^\+(\d{1,3})\s(\d{6,15})$')
PHONE_CANDIDATE_PATTERN = re.compile(r'(\+?\d[\d -]{8,}\d)')
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

    if normalized_area_code in {62, 52, 55} and not normalized_country:
        normalized_country = PHONE_PREFIX_COUNTRY_MAP[str(normalized_area_code)]

    return raw, normalized_area_code, normalized_country


def format_display_phone(phone: Optional[str], *, area_code: Optional[int] = None) -> str:
    raw = str(phone or '').strip()
    if not raw or raw == '-':
        return '-'
    if re.search(r'[^\d\s+\-]', raw):
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

    if phone_text and not re.fullmatch(r'\+\d{1,3}\s\d{6,15}', phone_text):
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
  </style>
</head>
<body>
  <h1>收口机器人配置中心</h1>
  <div class="muted">实时修改默认 default_app / default_guild。仅允许使用 CRM 下拉选项；当选项不可用时禁止保存。</div>
  <div class="muted" style="margin-top:8px;">同页还会加载公会执行器配置：/api/ops/guild-executors</div>
  <div class="muted" style="margin-top:8px;"><a href="/ops">返回运营操作台</a></div>

  <div class="card tight">
    <div class="summary-grid">
      <div class="summary-item"><div class="label">收口机器人</div><div class="value" id="presetCount">-</div></div>
      <div class="summary-item"><div class="label">公会执行器</div><div class="value" id="executorCount">-</div></div>
      <div class="summary-item"><div class="label">已配置代理</div><div class="value" id="executorProxyCount">-</div></div>
      <div class="summary-item"><div class="label">已配置密码引用</div><div class="value" id="executorSecretCount">-</div></div>
    </div>
  </div>

  <div class="card">
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
    <h2 style=\"margin-top:0;\">公会执行器配置</h2>
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
setInterval(() => {
  reloadPresets().catch(err => showToast(err.message, 'error'));
  reloadGuildExecutors().catch(err => showToast(err.message, 'error'));
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
  </style>
</head>
<body>
  <h1>运营操作台 MVP</h1>
  <div class=\"muted\">数据来源：/api/ops/dashboard/summary · /api/ops/manual-review-queue · /api/ops/bind-queue · /api/ops/group-queue · /api/ops/parser-quality-summary</div>
  <div class=\"muted\" style=\"margin-top:8px;\"><a href=\"/ops/intake-bot-presets\">前往收口机器人配置中心</a></div>

  <div class=\"card\" style=\"margin-top:16px;\">
    <h2>AI 下一步处理建议</h2>
    <div id=\"nextActionHint\" class=\"muted\">加载中...</div>
    <pre id=\"nextActionJson\" style=\"white-space: pre-wrap; overflow:auto; margin-top:12px;\"></pre>
  </div>

  <div class=\"section\">
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

  <div class=\"grid\">
    <div class=\"card\"><h2>待复核</h2><div id=\"manualReviewCount\" class=\"metric\">-</div></div>
    <div class=\"card\"><h2>待绑定</h2><div id=\"bindQueueCount\" class=\"metric\">-</div></div>
    <div class=\"card\"><h2>待入群</h2><div id=\"groupQueueCount\" class=\"metric\">-</div></div>
  </div>

  <div class=\"grid\">
    <div class=\"card\"><h2>绑定成功</h2><div id=\"bindSuccessCount\" class=\"metric\">-</div></div>
    <div class=\"card\"><h2>解析冲突</h2><div id=\"parserConflictCount\" class=\"metric\">-</div></div>
    <div class=\"card\"><h2>修正次数</h2><div id=\"correctionCount\" class=\"metric\">-</div></div>
  </div>

  <div class=\"section\">
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
    approved_count: int = 1
    area: str = 'Indonesia'
    remark: Optional[str] = None
    force_immediate: bool = False


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


class ApprovalBatchEvaluateRequest(BaseModel):
    approval_type: str
    registration_group: str
    pending_count: int
    oldest_pending_at: str
    now: str


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._memory_conn: Optional[sqlite3.Connection] = None
        self._ensure_parent()
        self._init_schema()

    def _ensure_parent(self) -> None:
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        if self.db_path == ":memory:":
            if self._memory_conn is None:
                self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._memory_conn.row_factory = sqlite3.Row
            return self._memory_conn
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
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
    return 'deferred_inside_asyncio_loop'


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
    def __init__(self, db: Database, crm_adapter: Any = None, ocr_adapter: Any = None, lark_media_adapter: Any = None, lark_reply_adapter: Any = None, lark_reply_adapter_by_app_id: Optional[Dict[str, Any]] = None, media_cache_dir: Optional[str] = None, lark_default_app_name: Optional[str] = None, lark_default_dept_name: Optional[str] = None, current_lark_app_id: Optional[str] = None, auto_bind_simulation: bool = False, bind_simulator: Any = None, real_bind_executor: Any = None, registration_group_approval_executor: Any = None, official_group_approval_executor: Any = None, auto_bind_simulation_success_rate: float = 0.5, auto_bind_simulation_seed: Optional[int] = None, crm_base_url: Optional[str] = None, crm_username: Optional[str] = None, crm_login_error: Optional[str] = None, ingress_async_default: bool = False, ingress_worker_enabled: bool = False, ingress_worker_poll_interval: float = 0.5, ingress_worker_count: int = 1, ingress_rate_limit_per_minute: int = 600, external_call_rate_limit_per_minute: int = 300, require_invite_code: bool = False) -> None:
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
        self.auto_bind_simulation_success_rate = max(0.0, min(1.0, float(auto_bind_simulation_success_rate or 0.5)))
        self._bind_random = random.Random(auto_bind_simulation_seed) if auto_bind_simulation_seed is not None else random.Random()
        self.media_cache_dir = Path(media_cache_dir or './data/lark_media_cache')
        self.media_cache_dir.mkdir(parents=True, exist_ok=True)
        self.crm_base_url = crm_base_url
        self.crm_username = crm_username
        self.crm_login_error = crm_login_error
        self.require_invite_code = require_invite_code
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
        return self.process_next_ingress_job() or self.process_next_automation_task()

    def _worker_loop(self) -> None:
        while not self._worker_stop.is_set():
            try:
                processed = self.process_next_worker_tick()
                if not processed:
                    time.sleep(self.ingress_worker_poll_interval)
            except Exception:
                time.sleep(self.ingress_worker_poll_interval)

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

    def process_next_ingress_job(self) -> Optional[Dict[str, Any]]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT job_id, event_id FROM ingress_jobs WHERE status = 'queued' ORDER BY available_at ASC, created_at ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            now = utc_now()
            conn.execute("UPDATE ingress_jobs SET status = 'processing', attempt_count = attempt_count + 1, updated_at = ? WHERE job_id = ?", (now, row['job_id']))
            conn.execute("UPDATE ingress_events SET status = 'processing', updated_at = ? WHERE event_id = ?", (now, row['event_id']))
            conn.commit()
            event = conn.execute("SELECT ingress_type, payload FROM ingress_events WHERE event_id = ?", (row['event_id'],)).fetchone()
        if not event:
            return None
        payload = json.loads(event['payload'] or '{}')
        try:
            if event['ingress_type'] == 'lark_event':
                result = self._handle_lark_event_sync(payload)
            elif event['ingress_type'] == 'manual_cs_submission':
                result = self._submit_manual_cs_sync(ManualCsSubmissionRequest(**payload))
            else:
                raise RuntimeError(f'unsupported ingress_type: {event["ingress_type"]}')
            status = 'done'
            error_text = None
        except Exception as exc:
            result = {'accepted': False, 'reason': 'ingress_processing_failed', 'error': str(exc)}
            status = 'failed'
            error_text = str(exc)
        with self.db.connect() as conn:
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
        if not row:
            return None
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
                'result_reason': result.get('result_reason'),
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
            if snapshot.get('verified_after_write'):
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
            'registration_group': {'batch_size': 30, 'timeout_minutes': 20},
            'official_group': {'batch_size': 10, 'timeout_minutes': 30},
        }
        if payload.approval_type not in rules:
            raise HTTPException(status_code=400, detail='unsupported approval_type')
        rule = rules[payload.approval_type]
        pending_count = max(int(payload.pending_count), 0)
        oldest = parse_iso_datetime(payload.oldest_pending_at)
        now = parse_iso_datetime(payload.now)
        elapsed_minutes = max(0, int((now - oldest).total_seconds() // 60))
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
            }
        if pending_count > 0 and elapsed_minutes >= rule['timeout_minutes']:
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
            }
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
        }

    def approval_batch_queue(self) -> Dict[str, Any]:
        now = utc_now()
        registration_statuses = ('new', 'engaged', 'manual_review_pending', 'recognition_pending', 'account_submitted', 'bind_check_pending', 'bind_failed')
        official_statuses = ('bind_success', 'group_join_pending', 'group_join_failed')
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
        return {
            'registration_groups': [
                self.evaluate_approval_batch(
                    ApprovalBatchEvaluateRequest(
                        approval_type='registration_group',
                        registration_group=row['registration_group'],
                        pending_count=row['pending_count'],
                        oldest_pending_at=row['oldest_pending_at'] or now,
                        now=now,
                    )
                ) for row in registration_rows
            ],
            'official_groups': [
                self.evaluate_approval_batch(
                    ApprovalBatchEvaluateRequest(
                        approval_type='official_group',
                        registration_group=row['registration_group'],
                        pending_count=row['pending_count'],
                        oldest_pending_at=row['oldest_pending_at'] or now,
                        now=now,
                    )
                ) for row in official_rows
            ],
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
        if str(result.get('next_action') or '').strip() in {'queue_bind_check', 'queue_account_recognition', 'queued_for_processing'}:
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
            reason_text = str(result.get('result_reason') or 'CRM sync failed').strip() or 'CRM sync failed'
            return (
                f'**❌ CRM sync failed: {reason_text}**\n'
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
        if resolved_phone != '-' and ('*' in resolved_phone or re.search(r'[^\d\s+\-]', resolved_phone)):
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
        if (
            (explicit_app_name and active_default_app and explicit_app_name.lower() != active_default_app.lower())
            or (explicit_dept_name and active_default_dept and explicit_dept_name.lower() != active_default_dept.lower())
        ):
            result = {
                'accepted': False,
                'ignored': True,
                'reason': 'app_guild_mismatch',
                'reply_phone': normalized_reply_phone,
                'reply_id': resolved_account_id or '-',
                'reply_group': resolved_group or '-',
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

    def _resolve_expected_bind_guild(self, *, task_payload: Dict[str, Any], lead_row: Optional[sqlite3.Row]) -> Optional[str]:
        bot_app_id = str(task_payload.get('source_bot_app_id') or '').strip()
        if bot_app_id:
            preset = self.resolve_intake_bot_preset(app_id=bot_app_id)
            preset_guild = str(preset.get('default_guild') or '').strip()
            if preset_guild:
                return preset_guild
        if lead_row:
            lead_guild = str(lead_row['dept_name'] or '').strip()
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

    def _sync_crm_after_bind_success(
        self,
        conn: sqlite3.Connection,
        *,
        lead_id: str,
        account_id: Optional[str],
        task_id: str,
        bind_result_reason: Optional[str],
        bind_raw_result: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        crm_sync_failed = None
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
                    },
                )
                crm_sync_failed = mapping_failure
            else:
                crm_response = self.crm_adapter.create_customer(crm_payload)
                crm_action = 'create'
                verified_row = None
                if crm_response.get('code') == 0:
                    verified_row = self._find_existing_customer_with_fallback(
                        yw_id=account_id,
                        mobile=lead_dict.get('mobile'),
                        app_name=resolved_app['appName'],
                        dept_name=resolved_dept['deptName'],
                        registration_group=lead_dict.get('pendaftaran_group') or '',
                    )
                self._record_sync_log(
                    conn,
                    lead_id=lead_id,
                    task_id=task_id,
                    sync_type='customer_upsert',
                    target_system='crm',
                    status='success' if crm_response.get('code') == 0 and verified_row else 'failed',
                    request_snapshot=crm_payload,
                    response_snapshot={
                        'action': crm_action,
                        'crm_response': crm_response,
                        'verified_after_write': bool(verified_row),
                    },
                )
                if crm_response.get('code') != 0:
                    crm_sync_failed = self._normalize_crm_failure_reason(
                        crm_response,
                        fallback_found=False,
                    )
                elif not verified_row:
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
        if crm_sync_failed:
            lead_mobile_row = conn.execute("SELECT mobile FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            self._queue_operator_notification(
                conn,
                lead_id=lead_id,
                notification_type="crm_record_failed",
                mobile=(lead_mobile_row['mobile'] if lead_mobile_row else ''),
                yw_id=account_id,
                write_result="failed",
                reason=crm_sync_failed,
            )
        return {
            'crm_sync_failed': crm_sync_failed,
            'crm_verified': crm_sync_failed is None,
            'current_submission_crm_verified': crm_sync_failed is None,
        }

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
            group_payload = {
                "submission_id": submission_id,
                "lead_id": lead_id,
                "account_id": account_id,
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

    def bind_check_result(self, task_id: str, payload: BindCheckResultRequest) -> Dict[str, Any]:
        now = utc_now()
        with self.db.connect() as conn:
            task = conn.execute("SELECT lead_id, payload FROM automation_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not task:
                raise HTTPException(status_code=404, detail="task not found")
            task_payload = json.loads(task["payload"] or "{}")
            submission_id = task_payload.get("submission_id")
            account_id = task_payload.get("account_id")
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
            effective_raw_result.update({k: v for k, v in bind_human_action.items() if v is not None})
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
                crm_sync = self._sync_crm_after_bind_success(
                    conn,
                    lead_id=task['lead_id'],
                    account_id=account_id,
                    task_id=task_id,
                    bind_result_reason=effective_result_reason,
                    bind_raw_result=effective_raw_result,
                )
                crm_sync_failed = crm_sync['crm_sync_failed']
                if crm_sync_failed:
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
            conn.execute("UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?", ("bind_failed", now, task["lead_id"]))
            lead_row = conn.execute("SELECT mobile FROM leads WHERE lead_id = ?", (task['lead_id'],)).fetchone()
            self._queue_operator_notification(
                conn,
                lead_id=task['lead_id'],
                notification_type="bind_check_failed",
                mobile=(lead_row['mobile'] if lead_row else ''),
                yw_id=account_id,
                write_result="failed",
                reason=effective_result_reason,
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
                            crm_payload['wa'] = (payload.raw_result or {}).get('target_group') or ''
                            crm_payload['pendaftaranGroup'] = lead_dict.get('pendaftaran_group') or existing.get('pendaftaranGroup') or ''
                            crm_response = self.crm_adapter.update_customer(crm_payload)
                            verified_row = None
                            if crm_response.get('code') == 0:
                                verified_row = self._find_existing_customer_with_fallback(
                                    yw_id=account_id,
                                    mobile=lead_dict.get('mobile'),
                                    app_name=crm_payload.get('appName'),
                                    dept_name=crm_payload.get('deptName'),
                                    registration_group=crm_payload.get('pendaftaranGroup'),
                                    official_group=crm_payload.get('wa'),
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

    def create_registration_group_approval_batch(self, payload: RegistrationGroupApprovalBatchRequest) -> Dict[str, Any]:
        if self.crm_adapter is None:
            raise HTTPException(status_code=400, detail='crm adapter not configured')
        started = time.perf_counter()
        crm_payload = {
            'area': payload.area,
            'groupNo': payload.registration_group,
            'groupPeopleNum': str(payload.approved_count),
        }
        crm_response = self.crm_adapter.create_registration_group_batch(crm_payload)
        elapsed_seconds = round(time.perf_counter() - started, 3)
        with self.db.connect() as conn:
            self._record_sync_log(
                conn,
                lead_id=None,
                task_id=None,
                sync_type='registration_group_approval_batch',
                target_system='crm',
                status='success' if crm_response.get('code') == 0 else 'failed',
                request_snapshot={
                    'registration_group': payload.registration_group,
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
                    'crm_payload': crm_payload,
                },
                response_snapshot=crm_response,
            )
            conn.commit()
        return {
            'accepted': True,
            'crm_sync_status': 'success' if crm_response.get('code') == 0 else 'failed',
            'crm_payload': crm_payload,
            'crm_response': crm_response,
            'elapsed_seconds': elapsed_seconds,
        }

    def registration_group_approval_executor_health(self) -> Dict[str, Any]:
        executor = self.registration_group_approval_executor
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

    def registration_group_approval_decision(self, payload: RegistrationGroupApprovalDecisionRequest) -> Dict[str, Any]:
        started = time.perf_counter()
        decision = str(payload.decision or 'approve').strip().lower() or 'approve'
        if decision != 'approve':
            raise HTTPException(status_code=400, detail='unsupported decision')
        executor = self.registration_group_approval_executor
        if executor is None:
            raise HTTPException(status_code=400, detail='registration group approval executor not configured')
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
            'approved_count': payload.approved_count,
            'area': payload.area,
            'remark': payload.remark,
            'force_immediate': payload.force_immediate,
        }
        if hasattr(executor, 'approve') and callable(getattr(executor, 'approve')):
            result = executor.approve(execution_context)
        elif callable(executor):
            result = executor(execution_context)
        else:
            raise HTTPException(status_code=500, detail='registration group approval executor is not callable')
        if not isinstance(result, dict):
            raise HTTPException(status_code=500, detail='registration group approval executor must return dict result')
        verified = bool(result.get('verified'))
        executed = True
        approved_count = max(1, int(result.get('approved_count') or payload.approved_count or 1))
        approved_at = str(result.get('approved_at') or result.get('finished_at') or payload.decided_at)
        target_member = result.get('target_member') or {}
        resolved_source_ad = payload.source_ad or ' '.join(
            part for part in [
                str(target_member.get('name') or '').strip(),
                str(target_member.get('phone_raw') or '').strip(),
            ] if part
        ) or None
        crm_batch = None
        crm_recorded = False
        crm_elapsed_seconds = 0.0
        if verified:
            crm_batch = self.create_registration_group_approval_batch(
                RegistrationGroupApprovalBatchRequest(
                    registration_group=payload.registration_group,
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
                )
            )
            crm_elapsed_seconds = round(float(crm_batch.get('elapsed_seconds') or 0.0), 3)
            crm_recorded = True
        total_elapsed_seconds = round(time.perf_counter() - started, 3)
        return {
            'registration_group': payload.registration_group,
            'decision': decision,
            'executed': executed,
            'verified': verified,
            'crm_recorded': crm_recorded,
            'status': result.get('status'),
            'result_code': result.get('result_code'),
            'result_reason': result.get('result_reason'),
            'approved_count': approved_count,
            'approved_at': approved_at,
            'elapsed_seconds': result.get('elapsed_seconds'),
            'crm_elapsed_seconds': crm_elapsed_seconds,
            'total_elapsed_seconds': total_elapsed_seconds,
            'force_immediate': payload.force_immediate,
            'target_member': target_member,
            'raw_result': result.get('raw_result') or {},
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
        if self.official_group_approval_executor is None:
            raise HTTPException(status_code=400, detail='official group approval executor not configured')
        decided_at = parse_iso_datetime(payload.decided_at).isoformat()
        with self.db.connect() as conn:
            lead_row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (payload.lead_id,)).fetchone()
            if not lead_row:
                raise HTTPException(status_code=404, detail='lead not found')
            lead = dict(lead_row)
            task = self._latest_group_join_task(conn, lead_id=str(payload.lead_id or '').strip())
            if not task:
                raise HTTPException(status_code=400, detail='group_join task not found for lead')
        executor_result = self.official_group_approval_executor.approve(
            target_group=str(payload.target_group or '').strip(),
            lead=lead,
            crm_snapshot=check_result.get('crm_snapshot') or {},
            task=task,
        ) or {}
        executor_raw_result = dict(executor_result.get('raw_result') or {})
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

    def official_group_approval_summary(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE current_status IN ('bind_success', 'group_join_pending', 'group_join_failed')"
            ).fetchone()[0]
            skipped_duplicate_count = conn.execute(
                """
                SELECT COUNT(*) FROM operator_audit_log
                WHERE event_type = 'official_group_approval_decision_skipped'
                  AND payload LIKE '%"reason_code": "already_in_target_group"%'
                """
            ).fetchone()[0]
            by_target_group: Dict[str, Dict[str, int]] = {}
            approved_count = 0
            failed_count = 0
            retryable_failed_count = 0
            manual_required_count = 0
            group_rows = conn.execute(
                "SELECT target_group, status, raw_result FROM group_join_jobs WHERE join_type = 'official_group'"
            ).fetchall()
            for row in group_rows:
                target_group = str(row['target_group'] or '').strip()
                if not target_group:
                    try:
                        parsed_raw = json.loads(row['raw_result'] or '{}')
                    except Exception:
                        parsed_raw = {}
                    target_group = str(parsed_raw.get('target_group') or '').strip()
                if not target_group:
                    continue
                by_target_group.setdefault(target_group, {
                    'approved_count': 0,
                    'skipped_duplicate_count': 0,
                    'retryable_failed_count': 0,
                    'manual_required_count': 0,
                })
                status = str(row['status'] or '').strip().lower()
                try:
                    parsed_raw = json.loads(row['raw_result'] or '{}')
                except Exception:
                    parsed_raw = {}
                disposition = str(parsed_raw.get('execution_disposition') or '').strip().lower()
                if status == 'success':
                    approved_count += 1
                    by_target_group[target_group]['approved_count'] += 1
                elif status == 'failed':
                    failed_count += 1
                    if disposition == 'retryable_failed':
                        retryable_failed_count += 1
                        by_target_group[target_group]['retryable_failed_count'] += 1
                    elif disposition == 'manual_required':
                        manual_required_count += 1
                        by_target_group[target_group]['manual_required_count'] += 1
            skipped_rows = conn.execute(
                """
                SELECT payload FROM operator_audit_log
                WHERE event_type = 'official_group_approval_decision_skipped'
                ORDER BY created_at DESC
                """
            ).fetchall()
            for row in skipped_rows:
                try:
                    payload = json.loads(row['payload'] or '{}')
                except Exception:
                    payload = {}
                if str(payload.get('reason_code') or '') != 'already_in_target_group':
                    continue
                target_group = str(payload.get('target_group') or '').strip()
                if not target_group:
                    continue
                by_target_group.setdefault(target_group, {
                    'approved_count': 0,
                    'skipped_duplicate_count': 0,
                    'retryable_failed_count': 0,
                    'manual_required_count': 0,
                })
                by_target_group[target_group]['skipped_duplicate_count'] += 1
            return {
                'pending_count': int(pending_count or 0),
                'approved_count': int(approved_count or 0),
                'failed_count': int(failed_count or 0),
                'skipped_duplicate_count': int(skipped_duplicate_count or 0),
                'retryable_failed_count': int(retryable_failed_count or 0),
                'manual_required_count': int(manual_required_count or 0),
                'by_target_group': by_target_group,
            }

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
            }
            if current_status not in {'bind_success', 'group_join_pending', 'group_join_failed'}:
                result.update({
                    'reason_code': 'lead_not_ready_for_official_group',
                    'reason_detail': 'Lead has not reached the official-group approval stage.',
                    'next_action': 'wait_for_bind_and_crm',
                })
            elif not crm_verified:
                result.update({
                    'reason_code': 'crm_verification_missing',
                    'reason_detail': 'Current submission has not been CRM-verified yet.',
                    'next_action': 'retry_crm_sync',
                })
            elif self.crm_adapter is None:
                result.update({
                    'reason_code': 'crm_adapter_not_configured',
                    'reason_detail': 'CRM adapter is unavailable.',
                    'next_action': 'manual_review_official_group_approval',
                })
            else:
                crm_row = self._find_existing_customer_with_fallback(
                    yw_id=lead.get('yw_id'),
                    mobile=lead.get('mobile'),
                    app_name=lead.get('crm_verified_app_name') or lead.get('app_name'),
                    dept_name=lead.get('crm_verified_dept_name') or lead.get('dept_name'),
                    registration_group=lead.get('crm_verified_registration_group') or lead.get('pendaftaran_group'),
                    official_group=None,
                )
                result['crm_customer_found'] = bool(crm_row)
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
                    }
                if not crm_row:
                    result.update({
                        'reason_code': 'crm_customer_not_found',
                        'reason_detail': 'No matching CRM customer was found for approval gating.',
                        'next_action': 'manual_review_official_group_approval',
                    })
                elif str(crm_row.get('wa') or '').strip() == target_group:
                    result.update({
                        'reason_code': 'already_in_target_group',
                        'reason_detail': 'CRM already points to the requested official group.',
                        'next_action': 'skip_duplicate_group_approval',
                    })
                else:
                    result.update({
                        'eligible': True,
                        'reason_code': 'eligible',
                        'reason_detail': 'CRM verification passed for official-group approval.',
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
        statuses = ('account_submitted', 'recognition_pending', 'bind_check_pending', 'bind_failed')
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
        statuses = ('bind_success', 'group_join_pending', 'group_join_failed')
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
            bind_queue_count = conn.execute("SELECT COUNT(*) FROM leads WHERE current_status IN ('account_submitted','recognition_pending','bind_check_pending','bind_failed')").fetchone()[0]
            manual_review_count = conn.execute("SELECT COUNT(*) FROM leads WHERE current_status = 'manual_review_pending'").fetchone()[0]
            group_queue_count = conn.execute("SELECT COUNT(*) FROM leads WHERE current_status IN ('bind_success','group_join_pending','group_join_failed')").fetchone()[0]
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
                'requires_human_action': bool(human),
                'human_action_type': human.get('human_action_type'),
                'human_action_task_id': human.get('task_id'),
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
            (str(mobile or '').strip(), str(row.get('mobile') or '').strip()),
            (str(app_name or '').strip(), str(row.get('appName') or '').strip()),
            (str(dept_name or '').strip(), str(row.get('deptName') or '').strip()),
            (str(registration_group or '').strip(), str(row.get('pendaftaranGroup') or '').strip()),
            (str(official_group or '').strip(), str(row.get('wa') or '').strip()),
        ]
        for expected, actual in expected_pairs:
            if expected and expected != actual:
                return False
        return True

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
    if registration_group_approval_executor is None and str(registration_group_approval_executor_kind or '').strip().lower() == 'live_whatsapp':
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

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get('/ops', response_class=HTMLResponse)
    def ops_page() -> str:
        return OPS_PAGE_HTML

    @app.get('/ops/intake-bot-presets', response_class=HTMLResponse)
    def intake_bot_presets_page() -> str:
        return INTAKE_BOT_PRESETS_PAGE_HTML

    @app.get('/api/ops/runtime-health')
    def ops_runtime_health() -> Dict[str, Any]:
        return service.runtime_health()

    @app.get('/api/ops/registration-group-approval-executor-health')
    def ops_registration_group_approval_executor_health() -> Dict[str, Any]:
        return service.registration_group_approval_executor_health()

    @app.get('/api/ops/ingress-queue')
    def ops_ingress_queue() -> Dict[str, Any]:
        return service.list_ingress_queue()

    @app.post('/api/ops/ingress-queue/run-next')
    def ops_ingress_queue_run_next() -> Dict[str, Any]:
        return service.process_next_worker_tick() or {'processed': False}

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
