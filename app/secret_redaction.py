from __future__ import annotations

import copy
from typing import Any, Dict


SENSITIVE_PAYLOAD_KEYWORDS = {
    'authorization',
    'token',
    'secret',
    'password',
    'refresh_token',
    'oauth_token',
    'oauth_token_secret',
    'platform_authorization',
    'cms_refresh_token',
    'app_secret',
    'guild_backend_token',
}

NON_SECRET_KEY_SUFFIXES = ('_configured', '_ready', '_enabled', '_status')
REDACTED_SECRET_VALUE = '***REDACTED***'

RUNTIME_LOG_DROP_KEYS = {
    'pairingCode',
    'pairing_code',
    'qr',
    'qrAscii',
    'qrTerminal',
    'qrText',
    'qrImageDataUrl',
    'qr_ascii',
    'qr_terminal',
    'qr_text',
    'qr_image_data_url',
    'qrImage',
    'qr_image',
}

RUNTIME_LOG_REDACT_KEYS = {
    'display_phone_number',
    'login_phone',
    'login_phone_raw',
    'mobile',
    'phone',
    'phone_e164',
    'phoneJid',
    'phone_jid',
    'phoneRaw',
    'phone_raw',
    'phoneNormalized',
    'phone_normalized',
    'debugLidPhoneRaw',
    'debug_lid_phone_raw',
    'phoneNumber',
    'phone_number',
    'displayPhoneNumber',
    'jid',
}

RUNTIME_LOG_TRACE_LIST_KEYS = {
    'recent_bind_traces',
    'recent_crm_traces',
}


def is_sensitive_payload_key(key: Any) -> bool:
    normalized = str(key or '').strip().lower().replace('-', '_')
    if not normalized:
        return False
    if normalized.endswith(NON_SECRET_KEY_SUFFIXES):
        return False
    return any(keyword in normalized for keyword in SENSITIVE_PAYLOAD_KEYWORDS)


def _normalize_payload_key(key: Any) -> str:
    normalized = str(key or '').strip().replace('-', '_')
    chars = []
    for index, char in enumerate(normalized):
        if char.isupper() and index > 0 and normalized[index - 1] != '_':
            chars.append('_')
        chars.append(char.lower())
    return ''.join(chars)


def redact_sensitive_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            if is_sensitive_payload_key(key):
                redacted[key] = REDACTED_SECRET_VALUE if item not in (None, '') else item
            else:
                redacted[key] = redact_sensitive_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_payload(item) for item in value]
    return value


def _compact_runtime_requesters(value: Any, *, max_items: int) -> Any:
    if not isinstance(value, list):
        return compact_runtime_log_payload(value, max_list_items=max_items)
    compacted = []
    for item in value[:max_items]:
        if not isinstance(item, dict):
            compacted.append(item)
            continue
        compacted.append({
            'id': item.get('id') or item.get('requesterId') or item.get('phone') or item.get('displayName'),
            'requestedAtIso': item.get('requestedAtIso'),
            'requestedAtUnix': item.get('requestedAtUnix'),
        })
    if len(value) > max_items:
        compacted.append({'truncated': True, 'remaining': len(value) - max_items})
    return compacted


def _compact_runtime_trace_rows(value: Any, *, max_items: int) -> Any:
    if not isinstance(value, list):
        return compact_runtime_log_payload(value, max_list_items=max_items)
    keep_keys = {
        'task_id',
        'lead_id',
        'sync_log_id',
        'status',
        'result_code',
        'result_reason',
        'sync_type',
        'target_system',
        'action',
        'created_at',
        'started_at',
        'finished_at',
        'queue_wait_seconds',
        'execution_seconds',
        'end_to_end_seconds',
        'crm_response_code',
        'crm_total_elapsed_seconds',
    }
    compacted = []
    for item in value[:max_items]:
        if not isinstance(item, dict):
            compacted.append(compact_runtime_log_payload(item, max_list_items=max_items))
            continue
        compacted.append({key: compact_runtime_log_payload(item.get(key)) for key in keep_keys if key in item})
    if len(value) > max_items:
        compacted.append({'truncated': True, 'remaining': len(value) - max_items})
    return compacted


def compact_runtime_log_payload(value: Any, *, max_list_items: int = 20, max_string_length: int = 512) -> Any:
    """Return a log-safe runtime payload.

    Keeps operational status fields but removes QR/base64/session material and
    compacts pending requester lists so system logs cannot balloon or leak
    sensitive WhatsApp data.
    """
    if isinstance(value, dict):
        compacted: Dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = _normalize_payload_key(key)
            if key in RUNTIME_LOG_DROP_KEYS or normalized_key in RUNTIME_LOG_DROP_KEYS:
                continue
            if is_sensitive_payload_key(key) or key in RUNTIME_LOG_REDACT_KEYS or normalized_key in RUNTIME_LOG_REDACT_KEYS:
                compacted[key] = REDACTED_SECRET_VALUE if item not in (None, '') else item
                continue
            if key in {'requesters', 'pending_requesters', 'requesterDetails'} or normalized_key in {'requesters', 'pending_requesters', 'requester_details'}:
                compacted[key] = _compact_runtime_requesters(item, max_items=max_list_items)
                continue
            if normalized_key in RUNTIME_LOG_TRACE_LIST_KEYS:
                compacted[key] = _compact_runtime_trace_rows(item, max_items=max_list_items)
                continue
            compacted[key] = compact_runtime_log_payload(
                item,
                max_list_items=max_list_items,
                max_string_length=max_string_length,
            )
        return compacted
    if isinstance(value, list):
        compacted_items = [
            compact_runtime_log_payload(item, max_list_items=max_list_items, max_string_length=max_string_length)
            for item in value[:max_list_items]
        ]
        if len(value) > max_list_items:
            compacted_items.append({'truncated': True, 'remaining': len(value) - max_list_items})
        return compacted_items
    if isinstance(value, str) and len(value) > max_string_length:
        return f'{value[:max_string_length]}...<truncated {len(value) - max_string_length} chars>'
    return copy.deepcopy(value)


def _dict_or_empty(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _health_counts(rows: Any) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    if not isinstance(rows, list):
        return counts
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get('health') or row.get('actorHealth') or row.get('status') or 'unknown').strip() or 'unknown'
        counts[status] = counts.get(status, 0) + 1
    return counts


def summarize_startup_health_payload(value: Any) -> Dict[str, Any]:
    """Return a small startup log summary without account, group, QR, or trace details."""
    payload = _dict_or_empty(compact_runtime_log_payload(value, max_list_items=5, max_string_length=256))
    crm = _dict_or_empty(payload.get('crm'))
    lark = _dict_or_empty(payload.get('lark'))
    simulation = _dict_or_empty(payload.get('simulation'))
    bind_executor = _dict_or_empty(payload.get('bind_executor'))
    registration = _dict_or_empty(payload.get('registration_group_approval'))
    official = _dict_or_empty(payload.get('official_group_approval'))
    ingress = _dict_or_empty(payload.get('ingress'))
    scheduler = _dict_or_empty(registration.get('scheduler'))
    provider = _dict_or_empty(registration.get('provider'))
    bind_metrics = _dict_or_empty(ingress.get('bind_metrics'))
    provider_accounts = provider.get('accounts') if isinstance(provider.get('accounts'), list) else []
    provider_account_ids = provider.get('accountIds') if isinstance(provider.get('accountIds'), list) else []

    return {
        'crm': {
            'enabled': crm.get('enabled'),
            'status': crm.get('status'),
            'token_ready': crm.get('token_ready'),
            'login_error': crm.get('login_error'),
        },
        'lark': {
            'current_app_id_configured': bool(lark.get('current_app_id')),
        },
        'simulation': {
            'auto_bind_simulation': simulation.get('auto_bind_simulation'),
            'mode': simulation.get('mode'),
        },
        'bind_executor': {
            'configured': bind_executor.get('configured'),
            'status': bind_executor.get('status'),
            'mode': bind_executor.get('mode'),
            'executor_type': bind_executor.get('executor_type'),
            'simulator_configured': bind_executor.get('simulator_configured'),
        },
        'registration_group_approval': {
            'configured': registration.get('configured'),
            'status': registration.get('status'),
            'provider': registration.get('provider') if isinstance(registration.get('provider'), str) else registration.get('providerKind'),
            'ready': registration.get('ready'),
            'authenticated': registration.get('authenticated'),
            'scheduler': {
                'status': scheduler.get('status'),
                'queueDepth': scheduler.get('queueDepth'),
                'groups': scheduler.get('groups'),
                'actor_count': len(scheduler.get('actors') or []),
                'actor_health_counts': _health_counts(scheduler.get('actors')),
            },
            'provider_summary': {
                'ready': provider.get('ready'),
                'configured': provider.get('configured'),
                'account_count': len(provider_account_ids or provider_accounts),
                'account_health_counts': _health_counts(provider_accounts),
            },
        },
        'official_group_approval': {
            'configured': official.get('configured'),
            'status': official.get('status'),
            'provider': official.get('provider'),
            'schema_version': official.get('schema_version'),
        },
        'ingress': {
            'worker_enabled': ingress.get('worker_enabled'),
            'worker_alive': ingress.get('worker_alive'),
            'worker_count': ingress.get('worker_count'),
            'queued_jobs': ingress.get('queued_jobs'),
            'processing_jobs': ingress.get('processing_jobs'),
            'stale_processing_jobs': ingress.get('stale_processing_jobs'),
            'pending_bind_tasks': ingress.get('pending_bind_tasks'),
            'processing_bind_tasks': ingress.get('processing_bind_tasks'),
            'recent_completed_count': bind_metrics.get('recent_completed_count'),
            'oldest_pending_age_seconds': bind_metrics.get('oldest_pending_age_seconds'),
        },
    }
