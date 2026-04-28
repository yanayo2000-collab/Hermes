from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def execute_formal_registration_group_approval(
    *,
    api_base_url: str,
    registration_group: str,
    area: str,
    remark: str,
    fetch_json: Callable[..., Dict[str, Any]],
    decision: str = 'approve',
    decided_by: str = 'Hermes',
    decided_by_name: str = 'Song Yuqi',
    approved_count: int = 1,
    poll_interval_seconds: float = 0.5,
    poll_timeout_seconds: float = 60.0,
    now_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    api_root = str(api_base_url or '').rstrip('/')
    decided_at = _utc_now_iso()
    payload = {
        'registration_group': str(registration_group or '').strip(),
        'decision': str(decision or 'approve').strip().lower() or 'approve',
        'decided_at': decided_at,
        'decided_by': str(decided_by or 'Hermes').strip() or 'Hermes',
        'decided_by_name': str(decided_by_name or 'Song Yuqi').strip() or 'Song Yuqi',
        'approved_count': max(1, int(approved_count or 1)),
        'area': str(area or '').strip(),
        'remark': str(remark or '').strip() or None,
        'force_immediate': True,
    }
    anchor_utc = _utc_now_iso()
    started = now_fn()
    accepted_response = fetch_json(
        f'{api_root}/api/registration-groups/approval-decisions',
        method='POST',
        payload=payload,
        timeout=max(10.0, poll_timeout_seconds),
    )
    approval_run_id = str(accepted_response.get('approval_run_id') or '').strip()
    if not approval_run_id:
        raise RuntimeError('approval_run_id missing from formal approval accepted response')

    polls = []
    deadline = started + max(1.0, float(poll_timeout_seconds))
    final_status: Optional[Dict[str, Any]] = None
    while True:
        status_payload = fetch_json(
            f'{api_root}/api/registration-groups/approval-decisions/{approval_run_id}',
            method='GET',
            timeout=max(10.0, poll_timeout_seconds),
        )
        polls.append({
            'ts': _utc_now_iso(),
            'status': status_payload.get('status'),
            'result_status': (status_payload.get('result') or {}).get('status'),
            'result_code': (status_payload.get('result') or {}).get('result_code'),
        })
        if status_payload.get('status') == 'done':
            final_status = status_payload
            break
        if now_fn() >= deadline:
            raise TimeoutError(f'formal approval polling timed out for {approval_run_id}')
        sleep_fn(max(0.0, float(poll_interval_seconds)))

    return {
        'anchor_utc': anchor_utc,
        'request_payload': payload,
        'accepted_response': accepted_response,
        'approval_run_id': approval_run_id,
        'polls': polls,
        'final_status': final_status,
    }
