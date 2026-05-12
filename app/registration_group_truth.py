from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_int(value: Any) -> Optional[int]:
    try:
        if value in (None, ''):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _payload_from_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    payload = meta.get('payload')
    return dict(payload) if isinstance(payload, dict) else {}


def _select_authoritative_probe(status: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    decision_meta = _as_dict(status.get('decision_group_state'))
    decision_payload = _payload_from_meta(decision_meta)
    if decision_payload:
        return str(decision_meta.get('source') or 'decision_group_state').strip() or 'decision_group_state', decision_payload, decision_meta

    for key in ('worker_state',):
        meta = _as_dict(status.get(key))
        payload = _payload_from_meta(meta)
        if payload:
            source = str(meta.get('source') or key).strip() or key
            return source, payload, meta

    truth_meta = _as_dict(status.get('truth_state'))
    truth_payload = _payload_from_meta(truth_meta)
    if truth_payload:
        return str(truth_meta.get('source') or 'truth_state').strip() or 'truth_state', truth_payload, truth_meta
    return '', {}, {}


def build_truth_state(
    *,
    status: Optional[Dict[str, Any]] = None,
    runtime_state: Optional[Dict[str, Any]] = None,
    session_state: Optional[Dict[str, Any]] = None,
    monitor_target: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    status = _as_dict(status)
    runtime_state = _as_dict(runtime_state)
    session_state = _as_dict(session_state)
    monitor_target = _as_dict(monitor_target)

    source, payload, meta = _select_authoritative_probe(status)
    pending_count = _as_int(payload.get('pending_count'))
    member_count = _as_int(payload.get('member_count'))

    zero_pending_unverified = bool(meta.get('zero_pending_unverified'))
    zero_pending_reason = str(meta.get('zero_pending_unverified_reason') or '').strip()
    zero_pending_verified_by = str(meta.get('zero_pending_verified_by') or payload.get('zero_pending_verified_by') or '').strip()
    pending_zero_confidence = str(meta.get('pending_zero_confidence') or payload.get('pending_zero_confidence') or '').strip()
    zero_pending_recheck_attempted = bool(payload.get('zero_pending_recheck_attempted'))
    zero_pending_recheck_resolved = bool(payload.get('zero_pending_recheck_resolved'))
    source_ts = str(meta.get('source_ts') or payload.get('source_ts') or '').strip() or None
    data_quality = str(meta.get('data_quality') or payload.get('data_quality') or '').strip()
    session_health = str(meta.get('session_health') or payload.get('session_health') or '').strip()

    review_surface_ready = bool(payload.get('review_surface_ready'))
    empty_queue_visible = bool(payload.get('empty_queue_visible'))
    has_pending_section = bool(payload.get('has_pending_section'))
    has_pending_request_row = bool(payload.get('has_pending_request_row'))
    evidence_complete = bool(
        review_surface_ready
        and (empty_queue_visible or has_pending_section or has_pending_request_row or (pending_count is not None and pending_count > 0))
    )

    session_target_match = session_state.get('session_target_match') if 'session_target_match' in session_state else None
    login_verified = session_state.get('login_verified') if 'login_verified' in session_state else None

    runtime_active = bool(runtime_state.get('active')) if 'active' in runtime_state else None
    runtime_ready = bool(runtime_state.get('ready')) if 'ready' in runtime_state else None
    runtime_authenticated = bool(runtime_state.get('authenticated')) if 'authenticated' in runtime_state else None

    status_code = 'probe_unavailable'
    reason_code = 'probe_missing'
    recoverable = True

    if session_state and session_target_match is False:
        status_code = 'session_mismatch'
        reason_code = 'session_target_mismatch'
        if not session_health:
            session_health = 'recovering'
        if not data_quality:
            data_quality = 'error'
    elif runtime_state and any(value is False for value in (runtime_active, runtime_ready, runtime_authenticated) if value is not None):
        status_code = 'runtime_unhealthy'
        reason_code = 'runtime_not_ready'
        if not session_health:
            session_health = 'degraded'
        if not data_quality:
            data_quality = 'stale' if payload else 'error'
    elif pending_count is not None and pending_count > 0:
        status_code = 'confirmed_pending'
        reason_code = 'pending_detected'
        recoverable = False
        if not session_health:
            session_health = 'healthy'
        if not data_quality:
            data_quality = 'fresh'
    elif pending_count == 0:
        status_code = 'confirmed_empty'
        if empty_queue_visible or bool(zero_pending_verified_by):
            reason_code = 'empty_queue_confirmed'
            pending_zero_confidence = pending_zero_confidence or 'verified'
        elif zero_pending_recheck_attempted and zero_pending_recheck_resolved and not zero_pending_unverified:
            reason_code = 'zero_pending_recheck_confirmed'
            pending_zero_confidence = pending_zero_confidence or 'high'
        elif zero_pending_unverified:
            status_code = 'empty_unverified'
            reason_code = zero_pending_reason or 'zero_pending_unverified'
            pending_zero_confidence = pending_zero_confidence or 'unverified'
        else:
            reason_code = 'pending_zero_observed'
            pending_zero_confidence = pending_zero_confidence or 'unverified'
        recoverable = False
        if not session_health:
            session_health = 'healthy'
        if not data_quality:
            data_quality = 'fresh'
    elif payload:
        status_code = 'probe_observed'
        reason_code = 'observation_incomplete'
        if not session_health:
            session_health = 'healthy'
        if not data_quality:
            data_quality = 'stale'

    if not session_health:
        session_health = 'error' if not payload else 'healthy'
    if not data_quality:
        data_quality = 'error' if not payload else 'fresh'

    return {
        'status': status_code,
        'reason_code': reason_code,
        'source': source or None,
        'recoverable': recoverable,
        'pending_count': pending_count,
        'member_count': member_count,
        'group_name': str(payload.get('group_name') or monitor_target.get('group_name') or '').strip(),
        'group_id': str(payload.get('group_id') or monitor_target.get('group_id') or '').strip(),
        'requester_ids': list(payload.get('requester_ids') or []),
        'requesters': list(payload.get('requesters') or []),
        'zero_pending_unverified': zero_pending_unverified,
        'zero_pending_unverified_reason': zero_pending_reason or None,
        'zero_pending_verified_by': zero_pending_verified_by or None,
        'pending_zero_confidence': pending_zero_confidence or None,
        'review_surface_ready': review_surface_ready,
        'empty_queue_visible': empty_queue_visible,
        'has_pending_section': has_pending_section,
        'has_pending_request_row': has_pending_request_row,
        'evidence_complete': evidence_complete,
        'session_target_match': session_target_match,
        'login_verified': login_verified,
        'runtime_active': runtime_active,
        'runtime_ready': runtime_ready,
        'runtime_authenticated': runtime_authenticated,
        'source_ts': source_ts,
        'data_quality': data_quality,
        'session_health': session_health,
        'payload': payload,
    }
