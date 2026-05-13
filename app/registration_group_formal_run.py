from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalized_expected_requesters(expected_group_state: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for item in (expected_group_state or {}).get('requesters') or []:
        if not isinstance(item, dict):
            continue
        requester_id = str(item.get('requesterId') or '').strip()
        if not requester_id:
            continue
        normalized.append({
            'requesterId': requester_id,
            'requestedAtUnix': item.get('requestedAtUnix'),
        })
    return normalized


def _resolve_approval_batch_display_id(
    api_root: str,
    approval_run_id: str,
    fetch_json: Callable[..., Dict[str, Any]],
) -> Optional[str]:
    normalized_run_id = str(approval_run_id or '').strip()
    if not normalized_run_id:
        return None
    try:
        payload = fetch_json(
            f'{api_root}/api/ops/registration-group-approval-batch-members?approval_run_id={normalized_run_id}&limit=1',
            method='GET',
            timeout=15.0,
        )
    except Exception:
        return None
    members = payload.get('rows') or payload.get('members') or payload.get('items') or []
    if not isinstance(members, list) or not members:
        return None
    first = members[0] if isinstance(members[0], dict) else {}
    value = str(first.get('approval_batch_display_id') or '').strip()
    return value or None



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
    expected_group_state: Optional[Dict[str, Any]] = None,
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
    if expected_group_state:
        payload['expected_pending_count'] = expected_group_state.get('pending_count')
        payload['expected_member_count'] = expected_group_state.get('member_count')
        payload['expected_requester_ids'] = [
            str(item).strip() for item in (expected_group_state.get('requester_ids') or []) if str(item).strip()
        ]
        payload['expected_requesters'] = _normalized_expected_requesters(expected_group_state)
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
        'approval_batch_display_id': _resolve_approval_batch_display_id(api_root, approval_run_id, fetch_json),
        'polls': polls,
        'final_status': final_status,
    }
