from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.registration_group_truth import build_approval_queue_display, normalize_approval_queue_truth_view, normalize_int_or_none, now_utc


def _parse_iso_datetime(value: str) -> datetime:
    text = str(value or '').strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_diagnostic_approval_queue_truth_view(
    current_truth: Optional[Dict[str, Any]],
    latest_probe: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    now = now_utc()
    current = dict(current_truth or {}) if isinstance(current_truth, dict) else {}
    latest = dict(latest_probe or {}) if isinstance(latest_probe, dict) else {}
    syncing = bool(current.get('syncing')) or bool(latest.get('syncing'))
    trust_status = str(current.get('trust_status') or '').strip() or None
    current_reason = str(current.get('confidence_reason') or current.get('reason_code') or '').strip() or None
    source_payload = dict(current.get('source') or {}) if isinstance(current.get('source'), dict) else {}
    source_mode = str(source_payload.get('mode') or '').strip() or None
    verified_at = str(current.get('verified_at') or current.get('source_ts') or current.get('checked_at') or '').strip() or None
    expires_at = str(current.get('expires_at') or '').strip() or None
    age_seconds = None
    if verified_at:
        try:
            age_seconds = max((now - _parse_iso_datetime(verified_at)).total_seconds(), 0.0)
        except Exception:
            age_seconds = None
    pending_count = current.get('pending_count')
    try:
        pending_count = int(pending_count) if pending_count is not None else None
    except Exception:
        pending_count = None
    member_count = normalize_int_or_none(current.get('member_count'))
    if member_count is None:
        member_count = normalize_int_or_none(current.get('memberCount'))
    if member_count is None and isinstance(current.get('facts'), dict):
        facts = dict(current.get('facts') or {})
        member_count = normalize_int_or_none(facts.get('member_count'))
        if member_count is None:
            member_count = normalize_int_or_none(facts.get('memberCount'))
    requester_ids = [str(item).strip() for item in (current.get('requester_ids') or []) if str(item).strip()] if isinstance(current.get('requester_ids'), list) else []
    stale = bool(current.get('stale'))
    if current and not stale and expires_at:
        try:
            stale = now >= _parse_iso_datetime(expires_at)
        except Exception:
            stale = False
    if current and not stale and age_seconds is not None and age_seconds > 300:
        stale = True
    freshness_level = 'UNKNOWN'
    if current:
        freshness_level = 'STALE' if stale else 'FRESH'
        if current_reason == 'historical_polluted_empty_downgraded':
            freshness_level = 'UNKNOWN'
    display_pending_count = pending_count
    if current_reason == 'historical_polluted_empty_downgraded':
        display_pending_count = None
    display = build_approval_queue_display({'pending_count': display_pending_count}, now)
    display_text = str(display.get('primary_text') or '').strip()
    can_manual_approve = bool(current.get('can_manual_approve')) and not stale and pending_count is not None and pending_count > 0
    return normalize_approval_queue_truth_view({
        'current_truth': current if current else None,
        'current_truth_raw': current if current else None,
        'latest_probe': latest if latest else None,
        'latest_probe_debug': latest if latest else None,
        'stale': stale,
        'pending_count': pending_count,
        'member_count': member_count,
        'requester_ids': requester_ids,
        'verified_at': verified_at,
        'freshness_level': freshness_level,
        'syncing': syncing,
        'trust_status': trust_status,
        'source': source_mode or None,
        'display_text': display_text,
        'can_manual_approve': can_manual_approve,
        'manual_approve_allowed': can_manual_approve,
        'action_allowed': can_manual_approve,
        'auto_approval_enabled': False,
        'age_seconds': age_seconds,
        'display_schema_version': int(current.get('display_schema_version') or 1),
        'display': display,
        'last_approval_action_ts': current.get('last_approval_action_ts'),
        'store_revision': int(current.get('store_revision') or 0),
        'probe_store_revision': int(latest.get('store_revision') or 0),
        'confidence_reason': current_reason,
    }, flow_type='registration_group')


def build_pending_truth_match_keys(*, lookup_keys: list[str], binding: Dict[str, Any], registration_group: str = '') -> set[str]:
    match_keys = {
        *(str(key or '').strip() for key in lookup_keys),
        str(registration_group or '').strip(),
        str(binding.get('registration_group') or '').strip(),
        str(binding.get('group_id') or '').strip(),
        str(binding.get('link') or '').strip(),
    }
    match_keys.discard('')
    return match_keys


def normalize_pending_truth_history_entry(
    *,
    object_key: str,
    truth_status: str,
    confidence: str,
    confidence_reason: str,
    facts: Dict[str, Any],
    source: Dict[str, Any],
    checked_at: str,
    expires_at: Optional[str],
    updated_at: str,
) -> Dict[str, Any]:
    normalized_truth_status = str(truth_status or '').strip()
    if normalized_truth_status == 'TRUSTED_CONFIRMED_PENDING':
        normalized_truth_status = 'confirmed_pending'
    elif normalized_truth_status == 'TRUSTED_CONFIRMED_EMPTY':
        normalized_truth_status = 'confirmed_empty'
    normalized_confidence = str(confidence or '').strip().lower()
    if normalized_confidence not in {'verified', 'untrusted'}:
        normalized_confidence = 'verified' if normalized_truth_status in {'confirmed_pending', 'confirmed_empty'} else 'untrusted'
    return {
        'object_key': str(object_key or '').strip(),
        'truth_status': normalized_truth_status,
        'confidence': normalized_confidence,
        'confidence_reason': str(confidence_reason or '').strip(),
        'facts': dict(facts or {}) if isinstance(facts, dict) else {},
        'source': dict(source or {}) if isinstance(source, dict) else {},
        'checked_at': str(checked_at or '').strip(),
        'expires_at': str(expires_at or '').strip() or None,
        'updated_at': str(updated_at or checked_at or '').strip(),
    }


def _pending_truth_source_payloads(source: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    decision_state = source.get('decision_group_state') if isinstance(source.get('decision_group_state'), dict) else {}
    decision_payload = decision_state.get('payload') if isinstance(decision_state.get('payload'), dict) else {}
    worker_state = source.get('worker_state') if isinstance(source.get('worker_state'), dict) else {}
    worker_payload = worker_state.get('payload') if isinstance(worker_state.get('payload'), dict) else {}
    return decision_payload, worker_payload


def _pending_truth_evidence_value(facts: Dict[str, Any], decision_payload: Dict[str, Any], worker_payload: Dict[str, Any], key: str) -> Any:
    return facts.get(key, decision_payload.get(key, worker_payload.get(key)))


def _pending_truth_row_is_fresh(row: Dict[str, Any], now: datetime) -> bool:
    expires_at_text = str(row.get('expires_at') or '').strip()
    if not expires_at_text:
        return True
    try:
        return _parse_iso_datetime(expires_at_text) >= now
    except Exception:
        return False


def select_pending_truth_confirmed_pending_candidate(rows: list[Dict[str, Any]], *, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    checked_now = now or now_utc()
    for row in rows:
        truth_status = str(row.get('truth_status') or '').strip().lower()
        confidence = str(row.get('confidence') or '').strip().lower()
        if truth_status != 'confirmed_pending' or confidence != 'verified':
            continue
        if not _pending_truth_row_is_fresh(row, checked_now):
            continue
        facts = dict(row.get('facts') or {}) if isinstance(row.get('facts'), dict) else {}
        source = dict(row.get('source') or {}) if isinstance(row.get('source'), dict) else {}
        pending_count = normalize_int_or_none(facts.get('pending_count'))
        if pending_count is None or pending_count <= 0:
            continue
        decision_payload, worker_payload = _pending_truth_source_payloads(source)

        def evidence(key: str) -> Any:
            return _pending_truth_evidence_value(facts, decision_payload, worker_payload, key)

        if not all(bool(evidence(key)) for key in ('login_verified', 'runtime_active', 'runtime_authenticated', 'runtime_ready', 'session_target_match', 'can_manage_membership_requests', 'self_is_admin', 'self_participant_found')):
            continue
        review_surface_ready = bool(evidence('review_surface_ready'))
        if not review_surface_ready:
            continue
        fallback_reason = str(source.get('fallback_reason') or decision_payload.get('fallback_reason') or worker_payload.get('fallback_reason') or '').strip()
        if fallback_reason:
            continue
        requester_ids = [str(item).strip() for item in (facts.get('requester_ids') or []) if str(item).strip()] if isinstance(facts.get('requester_ids'), list) else []
        requesters = list(facts.get('requesters') or []) if isinstance(facts.get('requesters'), list) else []
        if not requester_ids:
            for item in requesters:
                if isinstance(item, dict):
                    candidate_requester_id = str(item.get('requesterId') or item.get('requester_id') or '').strip()
                    if candidate_requester_id:
                        requester_ids.append(candidate_requester_id)
        if len(requester_ids) != pending_count:
            continue
        checked_at = str(row.get('checked_at') or '').strip() or now_utc().isoformat()
        merged_facts = dict(facts)
        merged_facts['requester_ids'] = requester_ids
        merged_facts['requesters'] = requesters
        for key in ('can_manage_membership_requests', 'self_is_admin', 'self_participant_found', 'review_surface_ready', 'empty_queue_visible', 'zero_pending_verified_by'):
            if merged_facts.get(key) is None:
                merged_facts[key] = evidence(key)
        merged_facts['review_surface_ready'] = review_surface_ready
        return {
            'checked_at': checked_at,
            'confidence_reason': str(row.get('confidence_reason') or '').strip() or 'pending_detected',
            'facts': merged_facts,
            'source': source,
        }
    return None


def select_pending_truth_confirmed_empty_candidate(rows: list[Dict[str, Any]], *, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    checked_now = now or now_utc()
    for row in rows:
        truth_status = str(row.get('truth_status') or '').strip().lower()
        confidence = str(row.get('confidence') or '').strip().lower()
        if truth_status != 'confirmed_empty' or confidence != 'verified':
            continue
        if not _pending_truth_row_is_fresh(row, checked_now):
            continue
        facts = dict(row.get('facts') or {}) if isinstance(row.get('facts'), dict) else {}
        source = dict(row.get('source') or {}) if isinstance(row.get('source'), dict) else {}
        pending_count = normalize_int_or_none(facts.get('pending_count'))
        if pending_count != 0 or bool(facts.get('zero_pending_unverified')):
            continue
        decision_payload, worker_payload = _pending_truth_source_payloads(source)

        def evidence(key: str) -> Any:
            return _pending_truth_evidence_value(facts, decision_payload, worker_payload, key)

        if not all(bool(evidence(key)) for key in ('login_verified', 'runtime_active', 'runtime_authenticated', 'runtime_ready', 'session_target_match', 'can_manage_membership_requests', 'self_is_admin', 'self_participant_found')):
            continue
        review_surface_ready = bool(evidence('review_surface_ready'))
        if not review_surface_ready:
            continue
        fallback_reason = str(source.get('fallback_reason') or decision_payload.get('fallback_reason') or worker_payload.get('fallback_reason') or '').strip()
        if fallback_reason:
            continue
        empty_queue_visible = bool(evidence('empty_queue_visible'))
        zero_pending_verified_by = str(evidence('zero_pending_verified_by') or '').strip()
        if not (empty_queue_visible or zero_pending_verified_by):
            continue
        merged_facts = dict(facts)
        for key in ('can_manage_membership_requests', 'self_is_admin', 'self_participant_found', 'review_surface_ready', 'empty_queue_visible', 'zero_pending_verified_by'):
            if merged_facts.get(key) is None:
                merged_facts[key] = evidence(key)
        merged_facts['review_surface_ready'] = review_surface_ready
        merged_facts['empty_queue_visible'] = empty_queue_visible
        merged_facts['zero_pending_verified_by'] = zero_pending_verified_by or None
        return {
            'checked_at': str(row.get('checked_at') or '').strip() or now_utc().isoformat(),
            'confidence_reason': str(row.get('confidence_reason') or '').strip() or 'empty_queue_confirmed',
            'facts': merged_facts,
            'source': source,
        }
    return None


def pending_truth_snapshot_group_state(rows: list[Dict[str, Any]], *, registration_group: str, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    checked_now = now or now_utc()
    for row in rows:
        truth_status = str(row.get('truth_status') or '').strip().lower()
        confidence = str(row.get('confidence') or '').strip().lower()
        if truth_status not in {'confirmed_pending', 'confirmed_empty'} or confidence != 'verified':
            continue
        if not _pending_truth_row_is_fresh(row, checked_now):
            continue
        facts = dict(row.get('facts') or {}) if isinstance(row.get('facts'), dict) else {}
        pending_count = normalize_int_or_none(facts.get('pending_count'))
        if pending_count is None:
            continue
        member_count = normalize_int_or_none(facts.get('member_count'))
        requester_ids = [str(item).strip() for item in (facts.get('requester_ids') or []) if str(item).strip()] if isinstance(facts.get('requester_ids'), list) else []
        requesters = list(facts.get('requesters') or []) if isinstance(facts.get('requesters'), list) else []
        source_payload = dict(row.get('source') or {}) if isinstance(row.get('source'), dict) else {}
        return {
            'group_id': str(facts.get('actual_group_id') or facts.get('configured_group_id') or registration_group).strip() or registration_group,
            'group_name': str(facts.get('actual_group_name') or facts.get('configured_group_name') or registration_group).strip() or registration_group,
            'pending_count': pending_count,
            'member_count': member_count,
            'requester_ids': requester_ids,
            'requesters': requesters,
            'source': str(source_payload.get('source') or 'mcn_truth_history').strip() or 'mcn_truth_history',
            'source_ts': str(row.get('checked_at') or '').strip() or None,
        }
    return None
