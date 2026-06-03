from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple


APPROVAL_TRUTH_ZERO_TTL_SECONDS = 120
APPROVAL_TRUTH_PENDING_TTL_SECONDS = 300
APPROVAL_TRUTH_UNKNOWN_TTL_SECONDS = 60


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


def _payload_requester_ids(payload: Dict[str, Any]) -> list:
    requester_ids = [str(item or '').strip() for item in (payload.get('requester_ids') or []) if str(item or '').strip()]
    if requester_ids:
        return requester_ids
    derived = []
    for item in payload.get('requesters') or []:
        if isinstance(item, dict):
            candidate = str(item.get('requesterId') or item.get('requester_id') or '').strip()
            if candidate:
                derived.append(candidate)
    return derived


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: Any) -> Optional[datetime]:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        normalized = text.replace('Z', '+00:00')
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_int_or_none(value: Any) -> Optional[int]:
    return _as_int(value)


def compute_freshness_level(source_ts: Any, truth_status: Any, now: Optional[datetime] = None) -> str:
    parsed = parse_ts(source_ts)
    if parsed is None:
        return 'UNKNOWN'
    current = now or now_utc()
    age_seconds = max((current - parsed).total_seconds(), 0.0)
    normalized_status = str(truth_status or '').strip().lower()
    if normalized_status in {'confirmed_empty', 'trusted_confirmed_empty'}:
        ttl = APPROVAL_TRUTH_ZERO_TTL_SECONDS
    elif normalized_status in {'confirmed_pending', 'pending_detected', 'trusted_confirmed_pending'}:
        ttl = APPROVAL_TRUTH_PENDING_TTL_SECONDS
    else:
        ttl = APPROVAL_TRUTH_UNKNOWN_TTL_SECONDS
    if age_seconds <= ttl:
        return 'FRESH'
    if age_seconds <= ttl * 2:
        return 'STALE'
    return 'EXPIRED'


def build_approval_queue_display(truth: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> Dict[str, Any]:
    if not truth:
        return {
            'state': 'UNKNOWN',
            'primary_text': '审批队列待刷新',
            'secondary_text': '暂无法确认当前待审批人数',
            'count': None,
            'debug_count': None,
            'show_count': False,
            'severity': 'muted',
        }

    pending_count = normalize_int_or_none(truth.get('pending_count'))
    stale = bool(truth.get('stale'))

    if pending_count is None:
        return {
            'state': 'UNKNOWN',
            'primary_text': '审批队列待刷新',
            'secondary_text': '暂无法确认当前待审批人数',
            'count': None,
            'debug_count': None,
            'show_count': False,
            'severity': 'muted',
        }

    if stale:
        return {
            'state': 'STALE',
            'primary_text': f'当前审批列表 {pending_count} 人',
            'secondary_text': '',
            'count': None,
            'debug_count': pending_count,
            'show_count': False,
            'severity': 'warning',
        }

    return {
        'state': 'COUNT',
        'primary_text': f'待审批 {pending_count} 人',
        'secondary_text': '',
        'count': pending_count,
        'debug_count': None,
        'show_count': True,
        'severity': 'normal',
    }


def build_membership_verifier_safe_detail(verifier: Dict[str, Any]) -> str:
    parts = []
    if verifier.get('probe_connected') or verifier.get('ready') is True or str(verifier.get('status') or '').strip() in {'mapped_live_probe_ready', 'live_probe_ready', 'not_group_member', 'not_group_admin'}:
        parts.append('已接探针')
    if verifier.get('has_admin_permission') or verifier.get('is_admin') or str(verifier.get('status') or '').strip() == 'mapped_live_probe_ready':
        parts.append('已有管理员权限')
    group_name = str(verifier.get('group_name') or verifier.get('current_group_name') or verifier.get('matched_group_name') or '').strip()
    if group_name:
        parts.append(f'当前群：{group_name}')
    return '。'.join(parts)


def strip_pending_from_detail(detail: Any) -> str:
    text = str(detail or '').strip()
    if not text:
        return ''
    text = re.sub(r'待审批\s*\d+\s*人[。；;，,：:]*', '', text)
    text = re.sub(r'pending\s*[:：]?\s*\d+', '', text, flags=re.I)
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[。；;，,:：\s]+$', '', text)
    return text.strip()


def serialize_membership_verifier(verifier: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    result = dict(verifier or {})
    result.pop('pending_count', None)
    result.pop('probe_pending_count', None)
    result.pop('api_pending_count', None)
    result.pop('ui_pending_count', None)
    probe = dict(result.get('probe') or {}) if isinstance(result.get('probe'), dict) else {}
    probe.pop('pending_count', None)
    result['probe'] = probe if probe else result.get('probe', {})
    result['safe_detail'] = build_membership_verifier_safe_detail(result)
    result['detail'] = strip_pending_from_detail(result.get('detail', ''))
    result['detail_deprecated'] = True
    return result


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

    approval_state_status = str(payload.get('approval_state_status') or payload.get('status') or '').strip().lower()
    unverified_pending_reason = str(payload.get('unverified_pending_reason') or '').strip()

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
        requester_ids = _payload_requester_ids(payload)
        if approval_state_status == 'unverified_pending' or not requester_ids:
            status_code = 'pending_unverified'
            reason_code = unverified_pending_reason or 'pending_without_requester_ids'
            recoverable = True
            if not session_health:
                session_health = 'healthy'
            if not data_quality:
                data_quality = 'unverified'
        else:
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
        elif zero_pending_unverified or pending_zero_confidence in {'unverified', 'unknown', 'low'}:
            status_code = 'empty_unverified'
            reason_code = zero_pending_reason or 'zero_pending_unverified'
            pending_zero_confidence = pending_zero_confidence or 'unverified'
            zero_pending_unverified = True
        else:
            status_code = 'empty_unverified'
            reason_code = 'pending_zero_observed_without_empty_queue_evidence'
            pending_zero_confidence = pending_zero_confidence or 'unverified'
            zero_pending_unverified = True
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
        'requester_ids': _payload_requester_ids(payload),
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
