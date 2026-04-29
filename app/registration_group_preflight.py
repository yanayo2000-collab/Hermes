from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


DEFAULT_EXPECTED_AUTH_STRATEGY = 'ChromeProfileCopy+NoAuth'


def _normalize_expected_auth_strategy(value: Any) -> str:
    normalized = str(value or '').strip()
    return normalized or DEFAULT_EXPECTED_AUTH_STRATEGY


def _as_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == '':
            return None
        return int(value)
    except Exception:
        return None


def _as_requester_ids(payload: Optional[Dict[str, Any]]) -> List[str]:
    values = (payload or {}).get('requester_ids') or []
    return [str(item).strip() for item in values if str(item).strip()]


def _requester_fingerprint(payload: Optional[Dict[str, Any]]) -> List[Tuple[str, Optional[int]]]:
    entries = []
    for item in (payload or {}).get('requesters') or []:
        requester_id = str((item or {}).get('requesterId') or '').strip()
        if not requester_id:
            continue
        entries.append((requester_id, _as_int((item or {}).get('requestedAtUnix'))))
    if entries:
        return sorted(entries)
    return sorted((requester_id, None) for requester_id in _as_requester_ids(payload))


def requester_fingerprint_entries(payload: Optional[Dict[str, Any]]) -> List[Dict[str, Optional[int]]]:
    return [
        {'requesterId': requester_id, 'requestedAtUnix': requested_at}
        for requester_id, requested_at in _requester_fingerprint(payload)
    ]


def _requester_ids_match(worker: Dict[str, Any], fresh: Dict[str, Any]) -> bool:
    worker_ids = _as_requester_ids(worker)
    fresh_ids = _as_requester_ids(fresh)
    return bool(worker_ids) and bool(fresh_ids) and sorted(worker_ids) == sorted(fresh_ids)


def _fingerprint_has_newer_timestamps(candidate: List[Tuple[str, Optional[int]]], baseline: List[Tuple[str, Optional[int]]]) -> bool:
    if not candidate or not baseline:
        return False
    baseline_map = {requester_id: requested_at for requester_id, requested_at in baseline}
    newer = False
    for requester_id, requested_at in candidate:
        if requester_id not in baseline_map:
            return False
        baseline_requested_at = baseline_map.get(requester_id)
        if requested_at is None or baseline_requested_at is None:
            return False
        if requested_at < baseline_requested_at:
            return False
        if requested_at > baseline_requested_at:
            newer = True
    return newer


def _state_regressed(current: Dict[str, Any], baseline: Dict[str, Any]) -> bool:
    current_pending = _as_int(current.get('pending_count'))
    current_members = _as_int(current.get('member_count'))
    baseline_pending = _as_int(baseline.get('pending_count'))
    baseline_members = _as_int(baseline.get('member_count'))
    current_ids = set(_as_requester_ids(current))
    baseline_ids = set(_as_requester_ids(baseline))

    if baseline_pending is not None and current_pending is not None and current_pending > baseline_pending:
        return True
    if baseline_members is not None and current_members is not None and current_members < baseline_members:
        return True
    if baseline_ids and current_ids and not current_ids.issubset(baseline_ids):
        return True
    return False


def evaluate_registration_group_webjs_preflight(
    *,
    registration_group: str,
    worker_health: Optional[Dict[str, Any]],
    worker_warmup: Optional[Dict[str, Any]],
    worker_group_state: Optional[Dict[str, Any]],
    fresh_group_state: Optional[Dict[str, Any]],
    last_verified_group_state: Optional[Dict[str, Any]] = None,
    expected_auth_strategy: Optional[str] = None,
) -> Dict[str, Any]:
    worker_health = dict(worker_health or {})
    worker_warmup = dict(worker_warmup or {})
    worker_group_state = dict(worker_group_state or {})
    fresh_group_state = dict(fresh_group_state or {})
    last_verified_group_state = dict(last_verified_group_state or {})

    reasons: List[str] = []
    warnings: List[str] = []
    expected_auth_strategy = _normalize_expected_auth_strategy(expected_auth_strategy)
    auth_strategy = str(worker_health.get('auth_strategy') or '').strip()
    worker_ready = bool(worker_health.get('ready'))
    worker_authenticated = bool(worker_health.get('authenticated'))
    worker_status = str(worker_health.get('status') or '').strip()
    warmup_outcome = str(worker_warmup.get('warmup_outcome') or '').strip()

    if auth_strategy and auth_strategy != expected_auth_strategy:
        reasons.append('unexpected_auth_strategy')
    if not worker_ready or not worker_authenticated or worker_status not in {'warm', 'ready'}:
        reasons.append('worker_not_ready')
    if warmup_outcome and warmup_outcome not in {'ready', 'warm'}:
        reasons.append('warmup_not_ready')

    worker_pending = _as_int(worker_group_state.get('pending_count'))
    worker_members = _as_int(worker_group_state.get('member_count'))
    fresh_pending = _as_int(fresh_group_state.get('pending_count'))
    fresh_members = _as_int(fresh_group_state.get('member_count'))
    worker_fingerprint = _requester_fingerprint(worker_group_state)
    fresh_fingerprint = _requester_fingerprint(fresh_group_state)
    requester_ids_match = _requester_ids_match(worker_group_state, fresh_group_state)

    stale_session_detected = False
    if last_verified_group_state:
        worker_regressed = _state_regressed(worker_group_state, last_verified_group_state)
        fresh_regressed = _state_regressed(fresh_group_state, last_verified_group_state)
        worker_matches_fresh = (
            worker_pending is not None and fresh_pending is not None and worker_pending == fresh_pending
            and worker_members is not None and fresh_members is not None and worker_members == fresh_members
            and worker_fingerprint == fresh_fingerprint
        )
        if worker_regressed and fresh_regressed and worker_matches_fresh:
            warnings.append('new_queue_detected_since_last_verified')
        elif worker_regressed and fresh_regressed and requester_ids_match and _fingerprint_has_newer_timestamps(worker_fingerprint, fresh_fingerprint):
            warnings.append('fresh_probe_requester_timestamps_stale')
        elif worker_regressed:
            reasons.append('worker_regressed_from_last_verified')
            stale_session_detected = True
        elif fresh_regressed:
            warnings.append('fresh_probe_regressed_from_last_verified')
    else:
        if worker_pending is not None and fresh_pending is not None and worker_pending != fresh_pending:
            reasons.append('pending_count_mismatch')
            stale_session_detected = True
        if worker_members is not None and fresh_members is not None and worker_members != fresh_members:
            reasons.append('member_count_mismatch')
            stale_session_detected = True
        if (
            worker_pending is not None and fresh_pending is not None and worker_pending == fresh_pending
            and worker_members is not None and fresh_members is not None and worker_members == fresh_members
            and worker_fingerprint and fresh_fingerprint and worker_fingerprint != fresh_fingerprint
        ):
            if requester_ids_match and _fingerprint_has_newer_timestamps(worker_fingerprint, fresh_fingerprint):
                warnings.append('fresh_probe_requester_timestamps_stale')
            else:
                reasons.append('requester_fingerprint_mismatch')
                stale_session_detected = True

    ok = not reasons
    return {
        'ok': ok,
        'registration_group': str(registration_group or '').strip(),
        'expected_auth_strategy': expected_auth_strategy,
        'stale_session_detected': stale_session_detected,
        'reasons': reasons,
        'warnings': warnings,
        'worker': {
            'health': worker_health,
            'warmup': worker_warmup,
            'group_state': worker_group_state,
        },
        'fresh_probe': fresh_group_state,
        'last_verified_group_state': last_verified_group_state,
    }
