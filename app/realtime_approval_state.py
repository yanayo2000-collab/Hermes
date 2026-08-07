from __future__ import annotations

import asyncio
import copy
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Set

from app.registration_group_truth import build_approval_queue_display, normalize_approval_queue_truth_view, serialize_membership_verifier


class RealtimeApprovalStateStore:
    """In-memory authoritative realtime state + event fanout for approval UI.

    This store never probes WhatsApp. It only accepts server-side snapshots that
    were already produced by backend/daemon code, diffs them, and publishes small
    patch events to browser subscribers. It also preserves a stronger per-binding verifier
    when a weak lightweight snapshot arrives later.
    """

    SNAPSHOT_METADATA_KEYS = (
        'notify_robot_options',
        'area_options',
        'area_option_source',
        'customer_service_options',
        'list_mode',
        'registration_group_cutover_enabled',
        'account_set_complete',
    )

    def __init__(self, *, max_events: int = 5000, delivery_target_ms: int = 100) -> None:
        self._snapshot: Dict[str, Any] = {
            'snapshot_mode': 'server_authoritative_realtime',
            'snapshot_version': 0,
            'event_id': 0,
            'generated_at': None,
            'rows': [],
        }
        self._event_id = 0
        self._snapshot_version = 0
        self._events: Deque[Dict[str, Any]] = deque(maxlen=max_events)
        self._subscribers: Set[asyncio.Queue] = set()
        self._event_lock = threading.RLock()
        self.delivery_target_ms = int(delivery_target_ms)
        self._group_revision_by_key: Dict[str, int] = {}

    def snapshot(self) -> Dict[str, Any]:
        return copy.deepcopy(self._snapshot)

    def events_since(self, last_event_id: int) -> List[Dict[str, Any]]:
        try:
            marker = int(last_event_id)
        except (TypeError, ValueError):
            marker = 0
        with self._event_lock:
            return [copy.deepcopy(event) for event in self._events if int(event.get('event_id') or 0) > marker]

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._event_lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._event_lock:
            self._subscribers.discard(queue)

    def ingest_snapshot(self, payload: Dict[str, Any], *, source: str = 'backend') -> Dict[str, Any]:
        incoming = self._normalize_snapshot(payload, source=source)
        previous = self._snapshot
        merged = self._merge_with_previous(previous, incoming, source=source)
        self._snapshot_version += 1
        merged['snapshot_version'] = self._snapshot_version
        merged['generated_at'] = self._now_iso()

        events = [] if int(previous.get('snapshot_version') or 0) <= 0 else self._diff_snapshots(previous, merged, source=source)
        self._snapshot = merged
        for event in events:
            self._publish(event)
        return {'snapshot': self.snapshot(), 'events': events}

    def _merge_with_previous(self, previous: Dict[str, Any], incoming: Dict[str, Any], *, source: str = 'backend') -> Dict[str, Any]:
        merged = copy.deepcopy(incoming)
        for key in self.SNAPSHOT_METADATA_KEYS:
            if key not in merged and key in previous:
                merged[key] = copy.deepcopy(previous.get(key))
        previous_accounts = {str(row.get('account_key') or '').strip(): row for row in previous.get('rows') or [] if isinstance(row, dict)}
        incoming_account_keys = {
            str(row.get('account_key') or '').strip()
            for row in merged.get('rows') or []
            if isinstance(row, dict) and str(row.get('account_key') or '').strip()
        }
        if self._is_partial_account_snapshot_source(source) and previous_accounts:
            incoming_accounts = {
                str(row.get('account_key') or '').strip(): row
                for row in merged.get('rows') or []
                if isinstance(row, dict) and str(row.get('account_key') or '').strip()
            }
            ordered_rows: List[Dict[str, Any]] = []
            consumed_keys: Set[str] = set()
            for previous_row in previous.get('rows') or []:
                if not isinstance(previous_row, dict):
                    continue
                account_key = str(previous_row.get('account_key') or '').strip()
                incoming_row = incoming_accounts.get(account_key)
                if incoming_row:
                    row = copy.deepcopy(previous_row)
                    row.update(copy.deepcopy(incoming_row))
                    row = self._merge_partial_group_rows_preserving_order(previous_row, row)
                    ordered_rows.append(row)
                    consumed_keys.add(account_key)
                elif account_key:
                    ordered_rows.append(copy.deepcopy(previous_row))
                    consumed_keys.add(account_key)
            for account_key, incoming_row in incoming_accounts.items():
                if account_key and account_key not in consumed_keys:
                    ordered_rows.append(copy.deepcopy(incoming_row))
            merged['rows'] = ordered_rows
        for row in merged.get('rows') or []:
            if not isinstance(row, dict):
                continue
            account_key = str(row.get('account_key') or '').strip()
            previous_row = previous_accounts.get(account_key)
            if not previous_row:
                self._assign_group_revisions(row)
                continue
            previous_groups = self._groups_by_key(previous_row)
            group_rows = row.get('group_binding_runtimes') or row.get('group_link_bindings') or []
            if not isinstance(group_rows, list):
                continue
            for index, group in enumerate(group_rows):
                if not isinstance(group, dict):
                    continue
                previous_group: Dict[str, Any] = {}
                for group_key in self._group_candidate_keys(group, index=index):
                    previous_group = previous_groups.get(group_key) or {}
                    if previous_group:
                        break
                incoming_verifier = group.get('membership_verifier') if isinstance(group.get('membership_verifier'), dict) else {}
                previous_verifier = previous_group.get('membership_verifier') if isinstance(previous_group.get('membership_verifier'), dict) else {}
                incoming_truth = group.get('approval_queue_truth') if isinstance(group.get('approval_queue_truth'), dict) else {}
                previous_truth = previous_group.get('approval_queue_truth') if isinstance(previous_group.get('approval_queue_truth'), dict) else {}
                incoming_zero_pending_weak = self._incoming_zero_pending_is_weak({
                    'next_approval_pending_count': group.get('next_approval_pending_count'),
                    'approval_queue_truth': incoming_truth,
                    'membership_verifier': incoming_verifier,
                })

                chosen_truth = self._choose_approval_truth(previous_truth, incoming_truth, source=source)
                chosen_permission_reason = self._approval_truth_permission_reason(chosen_truth)
                if chosen_truth:
                    group['approval_queue_truth'] = chosen_truth
                    chosen_pending_count = self._to_int(chosen_truth.get('pending_count'))
                    if chosen_permission_reason:
                        # A confirmed permission state is a first-class current truth.
                        # It must clear the old number instead of letting a partial
                        # realtime row keep the previous numeric truth on screen.
                        group['next_approval_pending_count'] = None
                    elif chosen_pending_count is not None:
                        group['next_approval_pending_count'] = chosen_pending_count

                preserve_previous_verifier = (
                    not chosen_permission_reason
                    and self._should_preserve_previous_group_probe(previous_group, group, source=source)
                )
                if chosen_permission_reason:
                    permission_verifier = copy.deepcopy(incoming_verifier)
                    permission_verifier['ready'] = False
                    permission_verifier['status'] = chosen_permission_reason
                    permission_verifier['is_admin'] = False
                    permission_verifier['has_admin_permission'] = False
                    permission_probe = permission_verifier.get('probe') if isinstance(permission_verifier.get('probe'), dict) else {}
                    permission_probe = copy.deepcopy(permission_probe)
                    permission_probe['permission_status'] = chosen_permission_reason
                    if chosen_permission_reason == 'not_group_member':
                        permission_probe['self_participant_found'] = False
                    elif chosen_permission_reason == 'not_group_admin':
                        permission_probe['self_participant_found'] = True
                        permission_probe['self_is_admin'] = False
                    permission_verifier['probe'] = permission_probe
                    group['membership_verifier'] = permission_verifier
                elif preserve_previous_verifier and previous_verifier:
                    group['membership_verifier'] = copy.deepcopy(previous_verifier)
                    for identity_field in (
                        'group_id',
                        'group_name',
                        'runtime_probe_group_id',
                        'runtime_probe_group_name',
                        'target_group_label',
                        'registration_group',
                        'link',
                    ):
                        previous_value = previous_group.get(identity_field)
                        incoming_value = group.get(identity_field)
                        if previous_value and (not incoming_value or identity_field.startswith('runtime_probe_')):
                            group[identity_field] = copy.deepcopy(previous_value)
                    if group.get('next_approval_pending_count') is None and previous_group.get('next_approval_pending_count') is not None:
                        group['next_approval_pending_count'] = previous_group.get('next_approval_pending_count')
                else:
                    group['membership_verifier'] = self._merge_membership_verifier(previous_verifier, incoming_verifier)

                previous_pending = self._to_int(previous_group.get('next_approval_pending_count'))
                incoming_pending = self._to_int(group.get('next_approval_pending_count'))
                current_truth = group.get('approval_queue_truth') if isinstance(group.get('approval_queue_truth'), dict) else {}
                if (
                    previous_pending is not None
                    and previous_pending > 0
                    and incoming_pending == 0
                    and incoming_zero_pending_weak
                    and not self._truth_has_verified_empty_evidence(current_truth)
                    and source == 'lightweight_snapshot_refresh'
                ):
                    group['next_approval_pending_count'] = previous_group.get('next_approval_pending_count')

                if isinstance(group.get('membership_verifier'), dict):
                    if not str(group['membership_verifier'].get('group_name') or '').strip() and str(group.get('group_name') or '').strip():
                        group['membership_verifier']['group_name'] = str(group.get('group_name') or '').strip()
                    if not str(group['membership_verifier'].get('current_group_name') or '').strip() and str(group.get('group_name') or '').strip():
                        group['membership_verifier']['current_group_name'] = str(group.get('group_name') or '').strip()
                group['membership_verifier'] = serialize_membership_verifier(group.get('membership_verifier'))
                self._assign_group_revision(group, previous_group)
        return merged

    def _merge_partial_group_rows_preserving_order(self, previous_row: Dict[str, Any], incoming_row: Dict[str, Any]) -> Dict[str, Any]:
        previous_groups = previous_row.get('group_binding_runtimes') or previous_row.get('group_link_bindings') or []
        incoming_groups = incoming_row.get('group_binding_runtimes') or incoming_row.get('group_link_bindings') or []
        if not isinstance(previous_groups, list) or not previous_groups:
            return incoming_row
        if not isinstance(incoming_groups, list) or not incoming_groups:
            row = copy.deepcopy(incoming_row)
            target_key = 'group_binding_runtimes' if isinstance(previous_row.get('group_binding_runtimes'), list) else 'group_link_bindings'
            row[target_key] = copy.deepcopy(previous_groups)
            return row

        incoming_candidates: Dict[str, Dict[str, Any]] = {}
        consumed_ids: Set[int] = set()
        for index, group in enumerate(incoming_groups):
            if not isinstance(group, dict):
                continue
            for key in self._stable_group_candidate_keys_without_index(group):
                incoming_candidates.setdefault(key, group)

        ordered_groups: List[Dict[str, Any]] = []
        for index, previous_group in enumerate(previous_groups):
            if not isinstance(previous_group, dict):
                continue
            incoming_group: Dict[str, Any] = {}
            for key in self._stable_group_candidate_keys_without_index(previous_group):
                candidate = incoming_candidates.get(key)
                if candidate and id(candidate) not in consumed_ids:
                    incoming_group = candidate
                    break
            if incoming_group:
                merged_group = copy.deepcopy(previous_group)
                merged_group.update(copy.deepcopy(incoming_group))
                ordered_groups.append(merged_group)
                consumed_ids.add(id(incoming_group))
            else:
                ordered_groups.append(copy.deepcopy(previous_group))

        for group in incoming_groups:
            if isinstance(group, dict) and id(group) not in consumed_ids:
                ordered_groups.append(copy.deepcopy(group))

        row = copy.deepcopy(incoming_row)
        target_key = 'group_binding_runtimes' if isinstance(incoming_row.get('group_binding_runtimes'), list) or isinstance(previous_row.get('group_binding_runtimes'), list) else 'group_link_bindings'
        row[target_key] = ordered_groups
        return row

    @staticmethod
    def _stable_group_candidate_keys_without_index(group: Dict[str, Any]) -> List[str]:
        probe = (group.get('membership_verifier') or {}).get('probe') if isinstance(group.get('membership_verifier'), dict) else {}
        keys = [
            str(group.get('binding_id') or '').strip(),
            str(group.get('group_id') or '').strip(),
            str(group.get('runtime_probe_group_id') or '').strip(),
            str((probe or {}).get('group_id') or '').strip() if isinstance(probe, dict) else '',
            str(group.get('registration_group') or '').strip(),
            str(group.get('link') or '').strip(),
            str(group.get('group_name') or '').strip(),
        ]
        return [key for key in keys if key]

    def _choose_approval_truth(self, previous_truth: Dict[str, Any], incoming_truth: Dict[str, Any], *, source: str) -> Dict[str, Any]:
        previous = self._finalize_truth(copy.deepcopy(previous_truth or {})) if previous_truth else {}
        inc = copy.deepcopy(incoming_truth or {})
        if not inc:
            return previous
        incoming = self._finalize_truth(inc)
        if previous and self._incoming_truth_is_older(previous, incoming):
            return previous
        if previous and self._should_preserve_previous_truth(previous, incoming, source=source):
            return previous
        return incoming

    @staticmethod
    def _incoming_truth_is_older(previous_truth: Dict[str, Any], incoming_truth: Dict[str, Any]) -> bool:
        previous_ts = RealtimeApprovalStateStore._approval_truth_timestamp(previous_truth)
        incoming_ts = RealtimeApprovalStateStore._approval_truth_timestamp(incoming_truth)
        return bool(previous_ts and incoming_ts and incoming_ts < previous_ts)

    @staticmethod
    def _approval_truth_timestamp(truth: Dict[str, Any]) -> Optional[datetime]:
        source = truth if isinstance(truth, dict) else {}
        current_truth = source.get('current_truth') if isinstance(source.get('current_truth'), dict) else {}
        payload = current_truth.get('payload') if isinstance(current_truth.get('payload'), dict) else {}
        for container in (source, current_truth, payload):
            for key in ('checked_at', 'verified_at', 'updated_at', 'source_ts', 'fetched_at', 'created_at'):
                parsed = RealtimeApprovalStateStore._parse_iso_datetime(container.get(key) if isinstance(container, dict) else None)
                if parsed:
                    return parsed
        return None

    @staticmethod
    def _parse_iso_datetime(value: Any) -> Optional[datetime]:
        text = str(value or '').strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _is_partial_account_snapshot_source(source: str) -> bool:
        normalized = str(source or '').strip()
        return normalized in {
            'manual_full_sync',
            'manual_approve',
            'manual_truth_refresh',
            'manual_probe',
            'manual_rebuild_identity',
            'approval_account_update',
            'approval_binding_delete',
            'truth_refresh',
            'full_sync',
            'probe_refresh',
            'rebuild_identity',
            'session_recovery_refresh',
        }

    @staticmethod
    def _should_preserve_previous_truth(previous_truth: Dict[str, Any], incoming_truth: Dict[str, Any], *, source: str) -> bool:
        if RealtimeApprovalStateStore._approval_truth_permission_reason(incoming_truth):
            return False
        if str(source or '').strip() in {'manual_truth_refresh', 'manual_full_sync', 'manual_approve', 'official_ready_precise_sync'}:
            return False
        previous_status = str(previous_truth.get('truth_status') or previous_truth.get('trust_status') or '').strip().lower()
        incoming_status = str(incoming_truth.get('truth_status') or incoming_truth.get('trust_status') or '').strip().lower()
        previous_trusted = previous_status in {'confirmed_empty', 'confirmed_pending', 'trusted_confirmed_empty', 'trusted_confirmed_pending'}
        incoming_trusted = incoming_status in {'confirmed_empty', 'confirmed_pending', 'trusted_confirmed_empty', 'trusted_confirmed_pending'}
        if not previous_trusted or incoming_trusted:
            return False
        previous_pending_count = RealtimeApprovalStateStore._to_int(previous_truth.get('pending_count'))
        incoming_pending_count = RealtimeApprovalStateStore._to_int(incoming_truth.get('pending_count'))
        if (
            previous_pending_count is not None
            and previous_pending_count > 0
            and incoming_pending_count == 0
            and not RealtimeApprovalStateStore._truth_has_verified_empty_evidence(incoming_truth)
        ):
            return True
        if incoming_pending_count is not None:
            return False
        incoming_display = incoming_truth.get('display') if isinstance(incoming_truth.get('display'), dict) else {}
        incoming_state = str(incoming_display.get('state') or incoming_truth.get('status') or '').strip().upper()
        incoming_reason = str(incoming_truth.get('confidence_reason') or incoming_truth.get('reason_code') or '').strip().lower()
        weak_incoming = incoming_state in {'VERIFYING', 'UNKNOWN', ''} or incoming_reason in {
            'api_pending_ui_not_converged',
            'probe_pending_ui_not_converged',
            'awaiting_refresh',
            'pending_probe_required',
            'truth_unknown',
        }
        return weak_incoming

    @staticmethod
    def _approval_truth_permission_reason(truth: Dict[str, Any]) -> str:
        source = truth if isinstance(truth, dict) else {}
        current_truth = source.get('current_truth') if isinstance(source.get('current_truth'), dict) else {}
        payload = current_truth.get('payload') if isinstance(current_truth.get('payload'), dict) else {}
        facts = current_truth.get('facts') if isinstance(current_truth.get('facts'), dict) else {}
        containers = (source, current_truth, payload, facts)
        statuses = {
            str(container.get(key) or '').strip().upper()
            for container in containers
            for key in ('truth_status', 'trust_status', 'status')
        }
        if 'GROUP_BANNED' in statuses:
            return 'group_banned'
        if 'PERMISSION_DENIED' not in statuses:
            return ''
        for container in containers:
            for key in ('reason_code', 'permission_status'):
                reason = str(container.get(key) or '').strip().lower()
                if reason in {'not_group_member', 'not_group_admin'}:
                    return reason
        return ''

    @staticmethod
    def _truth_has_verified_empty_evidence(truth: Dict[str, Any]) -> bool:
        source = truth if isinstance(truth, dict) else {}
        current_truth = source.get('current_truth') if isinstance(source.get('current_truth'), dict) else {}
        payload = current_truth.get('payload') if isinstance(current_truth.get('payload'), dict) else {}
        pending_count = RealtimeApprovalStateStore._to_int(
            source.get('pending_count')
            if source.get('pending_count') is not None
            else current_truth.get('pending_count')
        )
        if pending_count != 0:
            return False
        statuses = {
            str(source.get('truth_status') or source.get('trust_status') or source.get('status') or '').strip().lower(),
            str(current_truth.get('truth_status') or current_truth.get('trust_status') or current_truth.get('status') or '').strip().lower(),
            str(payload.get('truth_status') or payload.get('trust_status') or payload.get('status') or '').strip().lower(),
        }
        if statuses & {'confirmed_empty', 'trusted_confirmed_empty'}:
            return True
        display = source.get('display') if isinstance(source.get('display'), dict) else {}
        return bool(
            source.get('strong_empty_evidence')
            or current_truth.get('strong_empty_evidence')
            or payload.get('strong_empty_evidence')
            or source.get('zero_pending_verified_by')
            or current_truth.get('zero_pending_verified_by')
            or payload.get('zero_pending_verified_by')
            or (source.get('display_trusted') is True and str(display.get('state') or '').strip().upper() == 'EMPTY')
        )

    @staticmethod
    def _incoming_zero_pending_is_weak(group: Dict[str, Any]) -> bool:
        item = group if isinstance(group, dict) else {}
        truth = item.get('approval_queue_truth') if isinstance(item.get('approval_queue_truth'), dict) else {}
        if truth:
            status = str(truth.get('truth_status') or truth.get('trust_status') or truth.get('status') or '').strip().upper()
            display = truth.get('display') if isinstance(truth.get('display'), dict) else {}
            display_state = str(display.get('state') or '').strip().upper()
            if status in {'TRUTH_UNKNOWN', 'UNKNOWN', 'VERIFYING', 'STALE'} or display_state in {'UNKNOWN', 'VERIFYING', 'STALE', ''}:
                return True
        verifier = item.get('membership_verifier') if isinstance(item.get('membership_verifier'), dict) else {}
        if not verifier:
            return False
        probe = verifier.get('probe') if isinstance(verifier.get('probe'), dict) else {}
        status = str(verifier.get('status') or '').strip()
        quality = str(probe.get('data_quality') or probe.get('probe_data_quality') or '').strip()
        probe_present = bool(probe)
        return bool(
            verifier.get('ready') is False
            or status in {'probe_unavailable', 'mapped_live_probe_unavailable', 'unavailable', 'zero_pending_unverified'}
            or quality in {'unverified_zero', 'unknown', 'unavailable'}
            or (quality == '' and probe_present and verifier.get('ready') is False)
            or probe.get('zero_pending_unverified') is True
        )

    def _finalize_truth(self, truth: Dict[str, Any]) -> Dict[str, Any]:
        result = normalize_approval_queue_truth_view(copy.deepcopy(truth or {}))
        display = result.get('display') if isinstance(result.get('display'), dict) else {}
        if not display:
            result['display'] = build_approval_queue_display(result)
        else:
            result['display'] = copy.deepcopy(display)
        result['display_schema_version'] = int(result.get('display_schema_version') or 1)
        return result

    def _merge_membership_verifier(self, previous_verifier: Dict[str, Any], incoming_verifier: Dict[str, Any]) -> Dict[str, Any]:
        previous = dict(previous_verifier or {})
        incoming = dict(incoming_verifier or {})
        merged = dict(previous)
        merged.update(incoming)
        incoming_status = str(incoming.get('status') or '').strip()
        incoming_ready = incoming.get('ready') is True
        incoming_probe = dict(incoming.get('probe') or {}) if isinstance(incoming.get('probe'), dict) else {}
        incoming_weak = (
            not incoming_ready
            and incoming_status in {'probe_unavailable', 'mapped_live_probe_unavailable', 'unavailable', ''}
            and incoming_probe.get('member_count') is None
        )
        if incoming_weak:
            for key in ('ready', 'status', 'is_admin', 'has_admin_permission', 'probe_connected'):
                if key in previous:
                    merged[key] = previous.get(key)
        merged['probe'] = dict(previous.get('probe') or {})
        merged['probe'].update(dict(incoming.get('probe') or {}))
        for key in ('pending_count', 'probe_pending_count', 'api_pending_count', 'ui_pending_count'):
            merged.pop(key, None)
            if isinstance(merged.get('probe'), dict):
                merged['probe'].pop(key, None)
        return merged

    def _should_preserve_previous_group_probe(self, previous_group: Dict[str, Any], incoming_group: Dict[str, Any], *, source: str = 'backend') -> bool:
        previous_verifier = previous_group.get('membership_verifier') if isinstance(previous_group.get('membership_verifier'), dict) else {}
        incoming_verifier = incoming_group.get('membership_verifier') if isinstance(incoming_group.get('membership_verifier'), dict) else {}
        previous_status = str(previous_verifier.get('status') or '').strip()
        if previous_verifier.get('ready') is not True and previous_status not in {'not_group_member', 'not_group_admin', 'group_banned', 'zero_pending_unverified'}:
            return False
        if incoming_verifier.get('ready') is True:
            return False
        previous_probe = previous_verifier.get('probe') if isinstance(previous_verifier.get('probe'), dict) else {}
        incoming_probe = incoming_verifier.get('probe') if isinstance(incoming_verifier.get('probe'), dict) else {}
        previous_quality = str(previous_probe.get('data_quality') or previous_probe.get('probe_data_quality') or '').strip()
        incoming_quality = str(incoming_probe.get('data_quality') or incoming_probe.get('probe_data_quality') or '').strip()
        previous_has_member_count = previous_probe.get('member_count') is not None
        incoming_missing_member_count = incoming_probe.get('member_count') is None
        incoming_status = str(incoming_verifier.get('status') or '').strip()
        weak_incoming = (
            incoming_status in {'probe_unavailable', 'mapped_live_probe_unavailable', 'unavailable'}
            or incoming_quality in {'unverified_zero', 'unknown', 'unavailable', ''}
            or incoming_missing_member_count
        )
        strong_previous = (
            previous_status in {'not_group_member', 'not_group_admin', 'group_banned'}
            or previous_has_member_count
            or previous_quality in {'verified_zero', 'live_probe_ready', 'mapped_live_probe_ready', 'review_surface_verified_empty'}
        )
        incoming_pending = incoming_group.get('next_approval_pending_count')
        if isinstance(incoming_pending, int) and incoming_pending > 0:
            return False
        return bool(strong_previous and weak_incoming and source == 'lightweight_snapshot_refresh')

    def _normalize_snapshot(self, payload: Dict[str, Any], *, source: str) -> Dict[str, Any]:
        raw_rows = payload.get('rows') if isinstance(payload, dict) else []
        rows = copy.deepcopy(raw_rows if isinstance(raw_rows, list) else [])
        normalized_rows: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized_rows.append(self._normalize_account_row(row))
        snapshot = {
            'snapshot_mode': 'server_authoritative_realtime',
            'snapshot_version': self._snapshot_version,
            'event_id': self._event_id,
            'generated_at': payload.get('generated_at') if isinstance(payload, dict) else None,
            'state_source': source,
            'rows': normalized_rows,
        }
        if isinstance(payload, dict) and 'account_set_complete' in payload:
            snapshot['account_set_complete'] = bool(payload.get('account_set_complete'))
        elif not self._is_partial_account_snapshot_source(source):
            snapshot['account_set_complete'] = True
        if isinstance(payload, dict):
            for key in (*self.SNAPSHOT_METADATA_KEYS, 'summary'):
                if key in payload:
                    snapshot[key] = copy.deepcopy(payload.get(key))
        return snapshot

    def _normalize_account_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        normalized = copy.deepcopy(row)
        for payload_key in ('session_state', 'runtime_state', 'session', 'runtime'):
            if isinstance(normalized.get(payload_key), dict):
                normalized[payload_key] = self._strip_heavy_realtime_payloads(normalized.get(payload_key))
        session_state = normalized.get('session_state') if isinstance(normalized.get('session_state'), dict) else {}
        for field in ('login_state', 'ready', 'authenticated', 'login_verified', 'can_probe', 'login_check_status'):
            if normalized.get(field) is None and field in session_state:
                normalized[field] = copy.deepcopy(session_state.get(field))
        runtime_state = normalized.get('runtime_state') if isinstance(normalized.get('runtime_state'), dict) else {}
        if runtime_state.get('active') is True:
            normalized['monitor_runtime_active'] = True
        elif 'monitor_runtime_active' not in normalized and 'active' in runtime_state:
            normalized['monitor_runtime_active'] = bool(runtime_state.get('active'))
        if self._account_has_explicit_logged_out_signal(normalized):
            session_state = dict(session_state or {})
            session_state['login_verified'] = False
            session_state['authenticated'] = False
            session_state['ready'] = False
            session_state['can_probe'] = False
            normalized['session_state'] = session_state
            normalized['login_verified'] = False
            normalized['authenticated'] = False
            normalized['ready'] = False
            normalized['can_probe'] = False
        group_rows = normalized.get('group_binding_runtimes') or normalized.get('group_link_bindings') or []
        if isinstance(group_rows, list):
            normalized_groups = []
            for group in group_rows:
                if not isinstance(group, dict):
                    continue
                item = copy.deepcopy(group)
                if isinstance(item.get('membership_verifier'), dict):
                    item['membership_verifier'] = serialize_membership_verifier(item.get('membership_verifier'))
                if isinstance(item.get('approval_queue_truth'), dict):
                    item['approval_queue_truth'] = self._finalize_truth(item.get('approval_queue_truth'))
                normalized_groups.append(item)
            if isinstance(normalized.get('group_binding_runtimes'), list):
                normalized['group_binding_runtimes'] = normalized_groups
            else:
                normalized['group_link_bindings'] = normalized_groups
        return normalized

    @classmethod
    def _strip_heavy_realtime_payloads(cls, value: Any) -> Any:
        heavy_keys = {
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
        if isinstance(value, dict):
            return {
                key: cls._strip_heavy_realtime_payloads(item)
                for key, item in value.items()
                if key not in heavy_keys
            }
        if isinstance(value, list):
            return [cls._strip_heavy_realtime_payloads(item) for item in value]
        return copy.deepcopy(value)

    @staticmethod
    def _account_has_explicit_logged_out_signal(row: Dict[str, Any]) -> bool:
        session = row.get('session_state') if isinstance(row.get('session_state'), dict) else {}
        runtime = row.get('runtime_state') if isinstance(row.get('runtime_state'), dict) else {}
        if any(value is True for value in (
            session.get('login_verified'),
            session.get('can_probe'),
            session.get('ready'),
            session.get('authenticated'),
        )):
            return False
        login_status = str(session.get('login_check_status') or row.get('login_check_status') or '').strip()
        login_state = str(session.get('login_state') or row.get('login_state') or '').strip()
        runtime_status = str(runtime.get('status') or row.get('runtime_status') or '').strip()
        verification_status = str(row.get('verification_status') or '').strip()
        logged_out_statuses = {
            'account_restricted',
            'auth_failed',
            'blocked',
            'login_failed',
            'login_unready',
            'needs_scan',
            'not_logged_in',
            'not_started',
            'qr_expired',
            'qr_pending',
            'runtime_unavailable',
            'runtime_unhealthy',
            'service_unready',
            'stopped',
            'waiting_for_scan',
        }
        logged_out_states = {
            'account_restricted',
            'auth_failed',
            'login_failed',
            'not_logged_in',
            'not_started',
            'qr_expired',
            'session_mismatch',
            'service_unready',
            'stopped',
            'waiting_for_scan_qr_pending',
            'waiting_for_scan_qr_ready',
        }
        if login_status in logged_out_statuses or runtime_status in logged_out_statuses or verification_status in logged_out_statuses:
            return True
        if login_state in logged_out_states:
            return True
        if session.get('qr_available') is True or session.get('can_show_qr') is True or session.get('qr_stale') is True:
            return True
        return session.get('login_verified') is False and any(
            session.get(field) is False for field in ('authenticated', 'ready', 'can_probe')
        )

    def _diff_snapshots(self, previous: Dict[str, Any], current: Dict[str, Any], *, source: str) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        previous_accounts = {str(row.get('account_key') or '').strip(): row for row in previous.get('rows') or [] if isinstance(row, dict)}
        current_accounts = {str(row.get('account_key') or '').strip(): row for row in current.get('rows') or [] if isinstance(row, dict)}
        for account_key, current_row in current_accounts.items():
            if not account_key:
                continue
            previous_row = previous_accounts.get(account_key) or {}
            account_patch = self._account_patch(previous_row, current_row)
            if account_patch:
                events.append(self._new_event(
                    'account_state_patch',
                    account_key=account_key,
                    group_id='',
                    patch=account_patch,
                    source=source,
                ))
            previous_groups = self._groups_by_key(previous_row)
            current_groups = self._groups_by_key(current_row)
            seen_current_group_objects: Set[int] = set()
            for group_key, current_group in current_groups.items():
                current_object_id = id(current_group)
                if current_object_id in seen_current_group_objects:
                    continue
                seen_current_group_objects.add(current_object_id)
                previous_group = previous_groups.get(group_key) or {}
                group_patch = self._group_patch(previous_group, current_group)
                if group_patch:
                    events.append(self._new_event(
                        'group_probe_patch',
                        account_key=account_key,
                        group_id=str(current_group.get('group_id') or group_key),
                        binding_id=str(current_group.get('binding_id') or ''),
                        patch=group_patch,
                        source=source,
                    ))
        return events

    def _account_patch(self, previous: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
        patch: Dict[str, Any] = {}
        prev_session = previous.get('session_state') if isinstance(previous.get('session_state'), dict) else {}
        cur_session = current.get('session_state') if isinstance(current.get('session_state'), dict) else {}
        fields = ('runtime_status', 'verification_status', 'status_text')
        for field in fields:
            if previous.get(field) != current.get(field):
                patch[field] = current.get(field)
        for field in (
            'login_state',
            'ready',
            'authenticated',
            'login_verified',
            'can_probe',
            'login_check_status',
            'login_check_message',
            'qr_available',
            'can_show_qr',
            'qr_stale',
        ):
            if prev_session.get(field) != cur_session.get(field):
                patch[field] = cur_session.get(field)
        return patch

    def _group_patch(self, previous: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
        patch: Dict[str, Any] = {}
        if previous.get('next_approval_pending_count') != current.get('next_approval_pending_count'):
            patch['previous_pending_count'] = previous.get('next_approval_pending_count')
            patch['next_approval_pending_count'] = current.get('next_approval_pending_count')
        prev_verifier = previous.get('membership_verifier') if isinstance(previous.get('membership_verifier'), dict) else {}
        cur_verifier = current.get('membership_verifier') if isinstance(current.get('membership_verifier'), dict) else {}
        for field in ('ready', 'status', 'detail', 'safe_detail', 'detail_deprecated'):
            if prev_verifier.get(field) != cur_verifier.get(field):
                patch.setdefault('membership_verifier', {})[field] = cur_verifier.get(field)
        prev_probe = prev_verifier.get('probe') if isinstance(prev_verifier.get('probe'), dict) else {}
        cur_probe = cur_verifier.get('probe') if isinstance(cur_verifier.get('probe'), dict) else {}
        for field in ('probe_data_quality', 'data_quality', 'member_count'):
            if prev_probe.get(field) != cur_probe.get(field):
                patch.setdefault('probe', {})[field] = cur_probe.get(field)
        prev_truth = previous.get('approval_queue_truth') if isinstance(previous.get('approval_queue_truth'), dict) else {}
        cur_truth = current.get('approval_queue_truth') if isinstance(current.get('approval_queue_truth'), dict) else {}
        if prev_truth != cur_truth:
            patch['approval_queue_truth'] = copy.deepcopy(cur_truth)
        if current.get('group_name') and previous.get('group_name') != current.get('group_name'):
            patch['group_name'] = current.get('group_name')
        return patch

    def _groups_by_key(self, row: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        groups: Dict[str, Dict[str, Any]] = {}
        if not isinstance(row, dict):
            return groups
        group_rows = row.get('group_binding_runtimes') or row.get('group_link_bindings') or []
        if not isinstance(group_rows, list):
            return groups
        for index, group in enumerate(group_rows):
            if not isinstance(group, dict):
                continue
            for key in self._group_candidate_keys(group, index=index):
                if key and key not in groups:
                    groups[key] = group
        return groups

    @staticmethod
    def _group_candidate_keys(group: Dict[str, Any], *, index: int) -> List[str]:
        probe = (group.get('membership_verifier') or {}).get('probe') if isinstance(group.get('membership_verifier'), dict) else {}
        keys = [
            str(group.get('binding_id') or '').strip(),
            str(group.get('group_id') or '').strip(),
            str(group.get('runtime_probe_group_id') or '').strip(),
            str((probe or {}).get('group_id') or '').strip() if isinstance(probe, dict) else '',
            str(group.get('registration_group') or '').strip(),
            str(index),
        ]
        return [key for key in keys if key]

    def _assign_group_revisions(self, row: Dict[str, Any]) -> None:
        group_rows = row.get('group_binding_runtimes') or row.get('group_link_bindings') or []
        if not isinstance(group_rows, list):
            return
        for index, group in enumerate(group_rows):
            if isinstance(group, dict):
                self._assign_group_revision(group, {}, index=index)

    def _assign_group_revision(self, group: Dict[str, Any], previous_group: Dict[str, Any], *, index: int = 0) -> None:
        identity = self._stable_group_identity(group, index=index)
        previous_truth = previous_group.get('approval_queue_truth') if isinstance(previous_group.get('approval_queue_truth'), dict) else {}
        current_truth = group.get('approval_queue_truth') if isinstance(group.get('approval_queue_truth'), dict) else {}
        previous_revision = int(previous_truth.get('store_revision') or self._group_revision_by_key.get(identity, 0) or 0)
        if previous_truth == current_truth and previous_revision > 0:
            revision = previous_revision
        else:
            revision = previous_revision + 1 if previous_revision > 0 else 1
        self._group_revision_by_key[identity] = revision
        if current_truth:
            current_truth['store_revision'] = revision
            current_truth['display_schema_version'] = int(current_truth.get('display_schema_version') or 1)
            display = current_truth.get('display') if isinstance(current_truth.get('display'), dict) else {}
            if display:
                display['store_revision'] = revision

    @staticmethod
    def _stable_group_identity(group: Dict[str, Any], *, index: int = 0) -> str:
        return (
            str(group.get('binding_id') or '').strip()
            or str(group.get('group_id') or '').strip()
            or str(group.get('runtime_probe_group_id') or '').strip()
            or str(group.get('registration_group') or '').strip()
            or str(index)
        )

    def _new_event(self, event_type: str, *, account_key: str, group_id: str, binding_id: str = '', patch: Dict[str, Any], source: str) -> Dict[str, Any]:
        self._event_id += 1
        return {
            'event_id': self._event_id,
            'snapshot_version': self._snapshot_version,
            'type': event_type,
            'account_key': account_key,
            'group_id': group_id,
            'binding_id': str(binding_id or ''),
            'patch': copy.deepcopy(patch),
            'state_source': source,
            'server_emit_at': self._now_iso(),
            'delivery_target_ms': self.delivery_target_ms,
        }

    def _publish(self, event: Dict[str, Any]) -> None:
        with self._event_lock:
            self._events.append(copy.deepcopy(event))
            self._snapshot['event_id'] = self._event_id
            subscribers = tuple(self._subscribers)
        dead: List[asyncio.Queue] = []
        for queue in subscribers:
            try:
                queue.put_nowait(copy.deepcopy(event))
            except asyncio.QueueFull:
                dead.append(queue)
        for queue in dead:
            self.unsubscribe(queue)

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        try:
            return int(value) if value not in (None, '') else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
