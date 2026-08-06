from __future__ import annotations

import os
from typing import Any, Dict

from app.baileys_accounts import apply_baileys_account_id_aliases


DEFAULT_BAILEYS_PROVIDER_BASE_URL = 'http://127.0.0.1:8788'

WHATSAPP_APPROVAL_RUNTIME_CONFIG_KEYS = (
    'provider_mode',
    'registration_group_runtime',
    'official_group_runtime',
    'group_assistant_runtime',
    'baileys_base_url',
    'provider_base_url',
    'baileys_token',
    'provider_token',
    'runtime_token',
    'baileys_account_id',
    'provider_account_id',
    'account_id',
)


def default_baileys_provider_base_url() -> str:
    return str(
        os.getenv('REGISTRATION_GROUP_BAILEYS_BASE_URL')
        or os.getenv('MCN_PROBE_BAILEYS_BASE_URL')
        or DEFAULT_BAILEYS_PROVIDER_BASE_URL
    ).strip().rstrip('/')


def baileys_default_provider_mode_for_responsible_type(responsible_type: str) -> str:
    normalized_type = str(responsible_type or '').strip().lower()
    if normalized_type == 'official_group':
        return 'baileys_manual_approve_gray'
    if normalized_type == 'registration_group':
        return 'baileys_primary'
    if normalized_type == 'group_atmosphere':
        return 'baileys_primary'
    return 'baileys_primary'


def baileys_runtime_key_for_responsible_type(responsible_type: str) -> str:
    normalized_type = str(responsible_type or '').strip().lower()
    if normalized_type == 'registration_group':
        return 'registration_group_runtime'
    if normalized_type == 'official_group':
        return 'official_group_runtime'
    if normalized_type in {'group_atmosphere', 'group_atmosphere_learning'}:
        return 'group_assistant_runtime'
    return ''


def apply_baileys_runtime_assignment_defaults(item: Dict[str, Any], *, responsible_type: str, baileys_account_id: str) -> Dict[str, Any]:
    row = apply_baileys_account_id_aliases(item or {}, baileys_account_id)
    normalized_account_id = str(baileys_account_id or '').strip()
    if not normalized_account_id:
        return row
    provider_mode = baileys_default_provider_mode_for_responsible_type(responsible_type)
    if not str(row.get('provider_mode') or '').strip():
        row['provider_mode'] = provider_mode
    runtime_key = baileys_runtime_key_for_responsible_type(responsible_type)
    if runtime_key and not str(row.get(runtime_key) or '').strip():
        row[runtime_key] = provider_mode
    base_url = str(row.get('baileys_base_url') or row.get('provider_base_url') or '').strip().rstrip('/')
    if not base_url:
        base_url = default_baileys_provider_base_url()
    if base_url:
        row['baileys_base_url'] = base_url
        if not str(row.get('provider_base_url') or '').strip():
            row['provider_base_url'] = base_url
    if row.get('baileys_enabled') is None:
        row['baileys_enabled'] = True
    return row


def apply_whatsapp_approval_runtime_defaults(item: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(item or {})
    fallback = dict(defaults or {})
    fallback_account_id = str(
        fallback.get('baileys_account_id')
        or fallback.get('provider_account_id')
        or fallback.get('account_id')
        or ''
    ).strip()
    for key in WHATSAPP_APPROVAL_RUNTIME_CONFIG_KEYS:
        if not str(row.get(key) or '').strip() and str(fallback.get(key) or '').strip():
            row[key] = fallback.get(key)
    if fallback_account_id:
        row['baileys_account_id'] = fallback_account_id
        row['provider_account_id'] = fallback_account_id
        row['account_id'] = fallback_account_id
    if not isinstance(row.get('provider_capabilities'), dict) or not row.get('provider_capabilities'):
        if isinstance(fallback.get('provider_capabilities'), dict) and fallback.get('provider_capabilities'):
            row['provider_capabilities'] = dict(fallback.get('provider_capabilities') or {})
    if row.get('baileys_enabled') is None and fallback.get('baileys_enabled') is not None:
        row['baileys_enabled'] = fallback.get('baileys_enabled')
    return row
