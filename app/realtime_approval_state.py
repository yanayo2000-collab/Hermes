from __future__ import annotations

import asyncio
import copy
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Iterable, List, Optional, Set


class RealtimeApprovalStateStore:
    """In-memory authoritative realtime state + event fanout for approval UI.

    This store never probes WhatsApp. It only accepts server-side snapshots that
    were already produced by backend/daemon code, diffs them, and publishes small
    patch events to browser subscribers.
    """

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
        self.delivery_target_ms = int(delivery_target_ms)

    def snapshot(self) -> Dict[str, Any]:
        return copy.deepcopy(self._snapshot)

    def events_since(self, last_event_id: int) -> List[Dict[str, Any]]:
        try:
            marker = int(last_event_id)
        except (TypeError, ValueError):
            marker = 0
        return [copy.deepcopy(event) for event in self._events if int(event.get('event_id') or 0) > marker]

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
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
        """Keep strong live probe facts from being overwritten by weak daemon snapshots.

        The approval page consumes this store as the server-authoritative realtime
        source. Daemon snapshots may briefly carry unverified/partial WebJS data
        (for example member_count=None + unverified_zero) while a manual probe has
        just produced a stronger mapped_live_probe_ready result. Preserve the
        stronger per-binding verifier until a new snapshot has equal/better probe
        quality or confirmed pending data.
        """
        merged = copy.deepcopy(incoming)
        previous_accounts = {str(row.get('account_key') or '').strip(): row for row in previous.get('rows') or [] if isinstance(row, dict)}
        for row in merged.get('rows') or []:
            if not isinstance(row, dict):
                continue
            account_key = str(row.get('account_key') or '').strip()
            previous_row = previous_accounts.get(account_key)
            if not previous_row:
                continue
            previous_groups = self._groups_by_key(previous_row)
            group_rows = row.get('group_binding_runtimes') or row.get('group_link_bindings') or []
            if not isinstance(group_rows, list):
                continue
            for index, group in enumerate(group_rows):
                if not isinstance(group, dict):
                    continue
                group_key = str(group.get('group_id') or group.get('link') or group.get('group_name') or index).strip()
                previous_group = previous_groups.get(group_key) or {}
                if self._should_preserve_previous_group_probe(previous_group, group, source=source):
                    preserved_verifier = previous_group.get('membership_verifier')
                    if isinstance(preserved_verifier, dict):
                        group['membership_verifier'] = copy.deepcopy(preserved_verifier)
                    if group.get('next_approval_pending_count') is None and previous_group.get('next_approval_pending_count') is not None:
                        group['next_approval_pending_count'] = previous_group.get('next_approval_pending_count')
        return merged

    def _should_preserve_previous_group_probe(self, previous_group: Dict[str, Any], incoming_group: Dict[str, Any], *, source: str = 'backend') -> bool:
        # HTTP polling snapshots are the page's server-authoritative refresh path.
        # Do not let an old in-memory verifier/detail (for example “待审批 11 人”)
        # survive forever when the backend/daemon snapshot no longer carries that
        # count. A fresh daemon/internal event can still preserve strong probe facts
        # through other source names.
        if str(source or '').strip() == 'lightweight_snapshot_refresh':
            return False
        previous_verifier = previous_group.get('membership_verifier') if isinstance(previous_group.get('membership_verifier'), dict) else {}
        incoming_verifier = incoming_group.get('membership_verifier') if isinstance(incoming_group.get('membership_verifier'), dict) else {}
        if previous_verifier.get('ready') is not True:
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
            previous_has_member_count
            or previous_quality in {'verified_zero', 'live_probe_ready', 'mapped_live_probe_ready', 'review_surface_verified_empty'}
            or str(previous_verifier.get('status') or '').strip() == 'mapped_live_probe_ready'
        )
        incoming_pending = incoming_group.get('next_approval_pending_count')
        if isinstance(incoming_pending, int) and incoming_pending > 0:
            return False
        return bool(strong_previous and weak_incoming)

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
        # Preserve static UI option lists carried by the backend account payload.
        # The approval page loads the lightweight realtime snapshot first; if these
        # keys are dropped here, edit modals render empty/stale dropdowns even
        # though the normal accounts API has the correct region/customer options.
        if isinstance(payload, dict):
            for key in ('notify_robot_options', 'area_options', 'area_option_source', 'customer_service_options', 'list_mode', 'summary'):
                if key in payload:
                    snapshot[key] = copy.deepcopy(payload.get(key))
        return snapshot

    def _normalize_account_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        normalized = copy.deepcopy(row)
        session_state = normalized.get('session_state') if isinstance(normalized.get('session_state'), dict) else {}
        for field in ('login_state', 'ready', 'authenticated', 'login_verified', 'can_probe', 'login_check_status'):
            if normalized.get(field) is None and field in session_state:
                normalized[field] = copy.deepcopy(session_state.get(field))
        runtime_state = normalized.get('runtime_state') if isinstance(normalized.get('runtime_state'), dict) else {}
        if runtime_state.get('active') is True:
            normalized['monitor_runtime_active'] = True
        elif 'monitor_runtime_active' not in normalized and 'active' in runtime_state:
            normalized['monitor_runtime_active'] = bool(runtime_state.get('active'))
        return normalized

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
            for group_key, current_group in current_groups.items():
                previous_group = previous_groups.get(group_key) or {}
                group_patch = self._group_patch(previous_group, current_group)
                if group_patch:
                    events.append(self._new_event(
                        'group_probe_patch',
                        account_key=account_key,
                        group_id=str(current_group.get('group_id') or group_key),
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
        for field in ('login_state', 'ready', 'authenticated', 'login_verified', 'can_probe', 'login_check_status'):
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
        for field in ('ready', 'status', 'detail'):
            if prev_verifier.get(field) != cur_verifier.get(field):
                patch.setdefault('membership_verifier', {})[field] = cur_verifier.get(field)
        prev_probe = prev_verifier.get('probe') if isinstance(prev_verifier.get('probe'), dict) else {}
        cur_probe = cur_verifier.get('probe') if isinstance(cur_verifier.get('probe'), dict) else {}
        for field in ('pending_count', 'probe_data_quality', 'data_quality'):
            if prev_probe.get(field) != cur_probe.get(field):
                patch.setdefault('probe', {})[field] = cur_probe.get(field)
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
            key = str(group.get('group_id') or group.get('link') or group.get('group_name') or index).strip()
            if key:
                groups[key] = group
        return groups

    def _new_event(self, event_type: str, *, account_key: str, group_id: str, patch: Dict[str, Any], source: str) -> Dict[str, Any]:
        self._event_id += 1
        return {
            'event_id': self._event_id,
            'snapshot_version': self._snapshot_version,
            'type': event_type,
            'account_key': account_key,
            'group_id': group_id,
            'patch': copy.deepcopy(patch),
            'state_source': source,
            'server_emit_at': self._now_iso(),
            'delivery_target_ms': self.delivery_target_ms,
        }

    def _publish(self, event: Dict[str, Any]) -> None:
        self._events.append(copy.deepcopy(event))
        self._snapshot['event_id'] = self._event_id
        dead: List[asyncio.Queue] = []
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(copy.deepcopy(event))
            except asyncio.QueueFull:
                dead.append(queue)
        for queue in dead:
            self.unsubscribe(queue)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
