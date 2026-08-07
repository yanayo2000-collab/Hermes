from __future__ import annotations

import json
from typing import Any, Dict, Optional


WHATSAPP_APPROVAL_TASK_SPECS: Dict[str, Dict[str, Any]] = {
    'manual_approve': {'task_type': 'whatsapp_manual_approve', 'priority': 5, 'timeout_seconds': 90, 'max_retries': 1},
    'full_sync': {'task_type': 'whatsapp_full_sync', 'priority': 10, 'timeout_seconds': 60, 'max_retries': 2},
    'truth_refresh': {'task_type': 'whatsapp_truth_refresh', 'priority': 10, 'timeout_seconds': 60, 'max_retries': 2},
    'probe_refresh': {'task_type': 'whatsapp_probe_refresh', 'priority': 20, 'timeout_seconds': 45, 'max_retries': 2},
    'rebuild_identity': {'task_type': 'whatsapp_rebuild_identity', 'priority': 15, 'timeout_seconds': 60, 'max_retries': 1},
}


def whatsapp_approval_task_specs() -> Dict[str, Dict[str, Any]]:
    return {operation: dict(spec) for operation, spec in WHATSAPP_APPROVAL_TASK_SPECS.items()}


def whatsapp_approval_operation_from_task_type(task_type: str) -> str:
    normalized = str(task_type or '').strip()
    for operation, spec in WHATSAPP_APPROVAL_TASK_SPECS.items():
        if spec.get('task_type') == normalized:
            return operation
    return ''


def is_whatsapp_approval_operation_task_type(task_type: str) -> bool:
    return bool(whatsapp_approval_operation_from_task_type(task_type))


def operation_task_is_terminal_status(status: str) -> bool:
    return str(status or '').strip() in {'success', 'failed', 'dead_letter'}


def operation_task_terminal_failure_status(task_type: str) -> str:
    return 'dead_letter' if is_whatsapp_approval_operation_task_type(task_type) else 'failed'


def operation_task_should_retry(*, retry_count: int, max_retries: int) -> bool:
    return int(retry_count or 0) < max(1, int(max_retries or 1))


def operation_task_lease_expiry_status(*, task_type: str, retry_count: int, max_retries: int) -> str:
    if is_whatsapp_approval_operation_task_type(task_type):
        return 'pending' if operation_task_should_retry(retry_count=retry_count, max_retries=max_retries) else 'dead_letter'
    return 'pending'


def operation_task_account_key_from_object_key(object_key: str) -> str:
    normalized = str(object_key or '').strip()
    return normalized.split(':', 1)[0].strip() if ':' in normalized else normalized


def parse_operation_task_row(row: Any) -> Dict[str, Any]:
    result = dict(row or {})
    for key in ('input_json', 'result_json'):
        try:
            result[key.replace('_json', '')] = json.loads(result.get(key) or '{}')
        except Exception:
            result[key.replace('_json', '')] = {}
    return result


def build_whatsapp_approval_task_envelope(
    *,
    account_key: str,
    binding_index: int,
    operation: str,
    object_key: str,
    spec: Dict[str, Any],
    input_payload: Optional[Dict[str, Any]] = None,
    priority: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
    max_retries: Optional[int] = None,
    created_by: str = '',
) -> Dict[str, Any]:
    normalized_account_key = str(account_key or '').strip()
    normalized_operation = str(operation or '').strip()
    task_type = str((spec or {}).get('task_type') or '').strip()
    normalized_object_key = str(object_key or '').strip()
    payload = {
        **dict(input_payload or {}),
        'account_key': normalized_account_key,
        'binding_index': int(binding_index),
        'operation': normalized_operation,
    }
    return {
        'task_type': task_type,
        'object_type': 'registration_group_binding',
        'object_key': normalized_object_key,
        'idempotency_key': f'{task_type}:{normalized_object_key}',
        'priority': int(priority if priority is not None else (spec or {}).get('priority') or 100),
        'timeout_seconds': int(timeout_seconds if timeout_seconds is not None else (spec or {}).get('timeout_seconds') or 60),
        'max_retries': int(max_retries if max_retries is not None else (spec or {}).get('max_retries') or 1),
        'input': payload,
        'input_json': json.dumps(payload, ensure_ascii=False, default=str),
        'created_by': str(created_by or '').strip(),
    }


def effective_whatsapp_approval_task_wait_timeout(
    *,
    operation: str,
    requested_wait_timeout: Optional[float],
    task_timeout_seconds: Optional[Any] = None,
    task_status: str = '',
    task_deduped: bool = False,
) -> float:
    requested = max(1.0, float(requested_wait_timeout or 120.0))
    normalized_operation = str(operation or '').strip()
    spec = WHATSAPP_APPROVAL_TASK_SPECS.get(normalized_operation) or {}
    try:
        execution_budget = float(task_timeout_seconds if task_timeout_seconds is not None else spec.get('timeout_seconds') or 60.0)
    except Exception:
        execution_budget = float(spec.get('timeout_seconds') or 60.0)
    execution_budget = max(1.0, execution_budget)
    if normalized_operation != 'truth_refresh':
        return requested
    extended_wait = max(requested, execution_budget + 60.0, 150.0)
    if task_deduped or str(task_status or '').strip() in {'pending', 'running'}:
        extended_wait = max(extended_wait, execution_budget + 105.0)
    return extended_wait
