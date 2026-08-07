from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class BaileysRegistrationGroupApprovalExecutor:
    APPROVE_TIMEOUT_BASE_SECONDS = 15.0
    APPROVE_TIMEOUT_PER_REQUESTER_SECONDS = 3.0
    DEFAULT_ENDPOINT_PATHS = {
        'health': ['/health', '/ops/baileys/health', '/baileys/health'],
        'group_state': ['/group-state', '/ops/baileys/group-state', '/baileys/group-state', '/tasks/probe'],
        'approve': ['/approve', '/ops/baileys/approve', '/baileys/approve', '/tasks/approve-verify', '/tasks/approve'],
        'full_queue_sync': ['/full-queue-sync', '/ops/baileys/full-queue-sync', '/baileys/full-queue-sync', '/tasks/probe'],
        'official_group_approve': [
            '/official-group/approve',
            '/ops/baileys/official-group/approve',
            '/baileys/official-group/approve',
            '/tasks/approve-verify',
        ],
        'group_member_lookup': [
            '/group-member-lookup',
            '/ops/baileys/group-member-lookup',
            '/baileys/group-member-lookup',
        ],
        'group_metadata': [
            '/group-metadata',
            '/ops/baileys/group-metadata',
            '/baileys/group-metadata',
        ],
    }

    def __init__(
        self,
        *,
        base_url: str,
        token: Optional[str] = None,
        session: Any = None,
        timeout_seconds: float = 35.0,
        endpoint_paths: Optional[Dict[str, Iterable[str]]] = None,
    ) -> None:
        normalized_base_url = str(base_url or '').strip().rstrip('/')
        self.base_url = normalized_base_url
        self.token = str(token or '').strip() or None
        self.timeout_seconds = max(3.0, float(timeout_seconds or 20.0))
        self.endpoint_paths = self._normalize_endpoint_paths(endpoint_paths)
        if session is not None:
            self.session = session
        else:
            if requests is None:
                raise RuntimeError('requests is required for Baileys registration-group executor runtime')
            self.session = requests.Session()

    def _normalize_endpoint_paths(self, endpoint_paths: Optional[Dict[str, Iterable[str]]]) -> Dict[str, List[str]]:
        resolved: Dict[str, List[str]] = {}
        override = endpoint_paths or {}
        for key, default_paths in self.DEFAULT_ENDPOINT_PATHS.items():
            values = override.get(key, default_paths)
            normalized: List[str] = []
            for raw in values or []:
                path = str(raw or '').strip()
                if not path:
                    continue
                if not path.startswith('/'):
                    path = f'/{path}'
                if path not in normalized:
                    normalized.append(path)
            if not normalized:
                normalized = list(default_paths)
            resolved[key] = normalized
        return resolved

    def _headers(self) -> Dict[str, str]:
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        return headers

    @staticmethod
    def _first_text(payload: Dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = str((payload or {}).get(key) or '').strip()
            if value:
                return value
        return ''

    @staticmethod
    def _looks_like_invite_link(value: Any) -> bool:
        return str(value or '').strip().startswith('https://chat.whatsapp.com/')

    @staticmethod
    def _looks_like_group_jid(value: Any) -> bool:
        text = str(value or '').strip()
        return bool(text and text.endswith('@g.us') and ' ' not in text)

    @classmethod
    def _first_group_jid(cls, *payloads: Any) -> str:
        preferred_keys = {
            'resolvedGroupId',
            'resolved_group_id',
            'groupJid',
            'group_jid',
            'groupId',
            'group_id',
            'chatId',
            'chat_id',
            'jid',
        }
        seen: set[int] = set()

        def visit(value: Any) -> str:
            if value is None:
                return ''
            if isinstance(value, str):
                text = value.strip()
                return text if cls._looks_like_group_jid(text) else ''
            if isinstance(value, (int, float, bool)):
                return ''
            marker = id(value)
            if marker in seen:
                return ''
            seen.add(marker)
            if isinstance(value, dict):
                for key in preferred_keys:
                    found = visit(value.get(key))
                    if found:
                        return found
                for nested in value.values():
                    found = visit(nested)
                    if found:
                        return found
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    found = visit(nested)
                    if found:
                        return found
            return ''

        for payload in payloads:
            found = visit(payload)
            if found:
                return found
        return ''

    @classmethod
    def _first_group_name(cls, *payloads: Any) -> str:
        preferred_keys = (
            'groupSubject',
            'group_subject',
            'groupName',
            'group_name',
            'subject',
            'title',
        )
        seen: set[int] = set()

        def usable(value: Any) -> str:
            text = str(value or '').strip()
            if not text or cls._looks_like_invite_link(text) or cls._looks_like_group_jid(text):
                return ''
            return text

        def visit(value: Any) -> str:
            if value is None or isinstance(value, (int, float, bool)):
                return ''
            if isinstance(value, str):
                return ''
            marker = id(value)
            if marker in seen:
                return ''
            seen.add(marker)
            if isinstance(value, dict):
                for key in preferred_keys:
                    found = usable(value.get(key))
                    if found:
                        return found
                for nested in value.values():
                    found = visit(nested)
                    if found:
                        return found
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    found = visit(nested)
                    if found:
                        return found
            return ''

        for payload in payloads:
            found = visit(payload)
            if found:
                return found
        return ''

    @staticmethod
    def _int_or_none(value: Any) -> Optional[int]:
        try:
            if value is None:
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def _poc_probe_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = dict(payload or {})
        group_id = self._first_text(raw, 'groupId', 'group_id', 'registration_group', 'target', 'probe_target')
        group_link = self._first_text(raw, 'groupLink', 'group_link', 'link')
        if self._looks_like_invite_link(group_id):
            group_link = group_link or group_id
            group_id = ''
        account_id = self._first_text(raw, 'accountId', 'baileys_account_id', 'provider_account_id', 'account_id')
        normalized: Dict[str, Any] = {'priority': raw.get('priority') or 'P1'}
        if account_id:
            normalized['accountId'] = account_id
        if group_id:
            normalized['groupId'] = group_id
        if group_link:
            normalized['groupLink'] = group_link
        return normalized

    def _poc_approve_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = dict(payload or {})
        normalized = self._poc_probe_payload(raw)
        requester_ids = raw.get('requesterIds')
        if not isinstance(requester_ids, list):
            requester_ids = raw.get('requester_ids')
        if not isinstance(requester_ids, list):
            requester_ids = raw.get('expected_requester_ids')
        if not isinstance(requester_ids, list):
            before = raw.get('latest_group_state_before_approve')
            requester_ids = (before or {}).get('requester_ids') if isinstance(before, dict) else []
        normalized['requesterIds'] = [str(item).strip() for item in (requester_ids or []) if str(item).strip()]
        normalized['verifyAfterApprove'] = True
        normalized['priority'] = raw.get('priority') or 'P0'
        return normalized

    def _normalize_poc_probe_body(self, body: Dict[str, Any], *, fallback_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raw = dict(body or {})
        fallback = dict(fallback_payload or {})
        snapshot = raw.get('snapshot') if isinstance(raw.get('snapshot'), dict) else raw
        current_truth = dict(snapshot.get('current_truth') or {}) if isinstance(snapshot.get('current_truth'), dict) else {}
        latest_probe = dict(snapshot.get('latest_probe') or {}) if isinstance(snapshot.get('latest_probe'), dict) else {}
        self_participant_found = raw.get('self_participant_found')
        self_is_admin = raw.get('self_is_admin')
        can_manage_requests = raw.get('can_manage_membership_requests')
        permission_status = self._first_text(raw, 'permission_status')
        terminal_state_payload = raw.get('terminalState') if isinstance(raw.get('terminalState'), dict) else {}
        terminal_status = str(
            raw.get('terminal_state')
            or raw.get('terminalStatus')
            or terminal_state_payload.get('state')
            or raw.get('error')
            or ''
        ).strip().lower()
        group_banned = terminal_status == 'group_banned'
        if self_participant_found is False:
            permission_status = 'not_group_member'
        elif self_participant_found is True and self_is_admin is False:
            permission_status = 'not_group_admin'
        permission_denied = permission_status in {'not_group_member', 'not_group_admin'}
        snapshot_stale = bool(raw.get('stale') or current_truth.get('stale'))
        queue_readable = raw.get('queue_readable')
        live_queue_usable = raw.get('ok') is not False and queue_readable is not False and not snapshot_stale
        pending_count = self._int_or_none(
            current_truth.get('pendingCount', current_truth.get('pending_count', latest_probe.get('pendingCount', latest_probe.get('pending_count'))))
        )
        requester_ids = current_truth.get('requesterIds') or current_truth.get('requester_ids') or latest_probe.get('requesterIds') or latest_probe.get('requester_ids') or []
        if not isinstance(requester_ids, list):
            requester_ids = []
        requester_ids = [str(item).strip() for item in requester_ids if str(item).strip()]
        requesters = current_truth.get('requesters') or latest_probe.get('requesters') or []
        if not isinstance(requesters, list):
            requesters = []
        group_id = self._first_group_jid(raw, snapshot, current_truth, latest_probe, fallback)
        if self._looks_like_invite_link(group_id):
            group_id = ''
        group_name = self._first_group_name(raw, snapshot, current_truth, latest_probe) or group_id
        member_count = self._int_or_none(
            current_truth.get('memberCount', current_truth.get('member_count', latest_probe.get('memberCount', latest_probe.get('member_count'))))
        )
        if group_banned or permission_denied or not live_queue_usable:
            # A failed live probe may contain an old snapshot. It is useful for
            # diagnostics, but must never be promoted to current queue truth.
            pending_count = None
            requester_ids = []
            requesters = []
        verified_at = self._first_text(current_truth, 'verifiedAt', 'verified_at') or self._first_text(latest_probe, 'observedAt', 'observed_at')
        source = dict(raw.get('source') or {}) if isinstance(raw.get('source'), dict) else {}
        source.setdefault('provider', 'baileys')
        source.setdefault('mode', 'poc_tasks_probe' if isinstance(raw.get('task'), dict) or raw.get('resolvedGroupId') else 'poc_snapshot')
        if raw.get('provider_endpoint'):
            source.setdefault('provider_endpoint', raw.get('provider_endpoint'))
        if group_banned:
            trust_status = 'GROUP_BANNED'
        elif permission_denied:
            trust_status = 'PERMISSION_DENIED'
        elif pending_count is None:
            trust_status = 'TRUTH_UNKNOWN'
        elif pending_count > 0:
            trust_status = 'TRUSTED_CONFIRMED_PENDING'
        else:
            trust_status = 'TRUSTED_CONFIRMED_EMPTY'
        can_manual_approve = bool(pending_count is not None and pending_count > 0 and not permission_denied and not group_banned)
        raw_error = self._first_text(raw, 'probe_error', 'error', 'reason')
        reason_code = (
            'group_banned'
            if group_banned
            else permission_status
            if permission_denied
            else (
                'poc_baileys_probe_pending_count'
                if pending_count is not None
                else (raw_error or 'poc_baileys_probe_pending_count_missing')
            )
        )
        return {
            'ok': bool(raw.get('ok', pending_count is not None)) and not permission_denied,
            'provider': 'baileys',
            'trust_status': trust_status,
            'reason_code': reason_code,
            'permission_status': permission_status or None,
            'terminal_confirmed': group_banned and bool(raw.get('terminal')),
            'terminal_state': 'group_banned' if group_banned else None,
            'terminal_source': str(terminal_state_payload.get('source') or '').strip() or None,
            'pending_count': pending_count,
            'trusted_pending_count': pending_count,
            'api_pending_count': pending_count,
            'ui_pending_count': pending_count,
            'member_count': member_count,
            'requester_ids': requester_ids,
            'requesters': requesters,
            'group_id': group_id or None,
            'group_name': group_name or None,
            'group_identity_verified': bool(group_id),
            'runtime_identity_match': True if group_id else None,
            'session_authenticated': True,
            'queue_readable': queue_readable,
            'participants_load_status': raw.get('participants_load_status'),
            'self_participant_found': self_participant_found,
            'self_is_admin': self_is_admin,
            'can_manage_membership_requests': can_manage_requests,
            'review_surface_ready': False,
            'can_manual_approve': can_manual_approve,
            'manual_approve_allowed': can_manual_approve,
            'display_trusted': pending_count is not None,
            'fingerprint_quality': 'strong' if pending_count is not None else 'weak',
            'source_ts': verified_at or None,
            'verified_at': verified_at or None,
            'stale': snapshot_stale,
            'source': source,
            'poc_snapshot': snapshot,
        }

    def _normalize_poc_approve_body(self, body: Dict[str, Any], *, fallback_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raw = dict(body or {})
        fallback = dict(fallback_payload or {})
        snapshot = raw.get('snapshot') if isinstance(raw.get('snapshot'), dict) else {}
        operation = raw.get('operation') if isinstance(raw.get('operation'), dict) else {}
        approval = operation.get('approval') if isinstance(operation.get('approval'), dict) else {}
        verification = operation.get('verification') if isinstance(operation.get('verification'), dict) else {}
        current_truth = dict(snapshot.get('current_truth') or {}) if isinstance(snapshot.get('current_truth'), dict) else {}
        latest_before = dict(fallback.get('latest_group_state_before_approve') or {}) if isinstance(fallback.get('latest_group_state_before_approve'), dict) else {}
        requested_ids = self._poc_approve_payload(fallback).get('requesterIds') or []
        approved_ids = approval.get('approvedIds') if isinstance(approval.get('approvedIds'), list) else []
        approved_ids = [str(item).strip() for item in approved_ids if str(item).strip()]
        if not approved_ids and raw.get('ok') and requested_ids:
            remaining = set(str(item).strip() for item in (approval.get('remainingPendingIds') or current_truth.get('requesterIds') or []) if str(item).strip())
            approved_ids = [item for item in requested_ids if item not in remaining]
        remaining_ids = approval.get('remainingPendingIds')
        if not isinstance(remaining_ids, list):
            remaining_ids = verification.get('remainingPendingIds')
        if not isinstance(remaining_ids, list):
            remaining_ids = current_truth.get('requesterIds')
        remaining_ids = [str(item).strip() for item in (remaining_ids or []) if str(item).strip()]
        pending_after = self._int_or_none(current_truth.get('pendingCount', current_truth.get('pending_count')))
        if pending_after is None:
            pending_after = len(remaining_ids) if remaining_ids is not None else None
        pending_before = self._int_or_none(latest_before.get('pending_count'))
        if pending_before is None:
            pending_before = self._int_or_none(fallback.get('approved_count'))
        member_count_before = self._int_or_none(latest_before.get('member_count'))
        member_count_after = self._int_or_none(
            current_truth.get('memberCount', current_truth.get('member_count', approval.get('memberCount', verification.get('memberCount'))))
        )
        expected_requesters = fallback.get('expected_requesters') if isinstance(fallback.get('expected_requesters'), list) else []
        if not expected_requesters and isinstance(latest_before.get('requesters'), list):
            expected_requesters = latest_before.get('requesters') or []
        requesters_by_id = {}
        for requester in expected_requesters:
            if not isinstance(requester, dict):
                continue
            requester_id = self._first_text(requester, 'requesterId', 'requester_id')
            if requester_id:
                requesters_by_id[requester_id] = dict(requester)
        approved_requesters = approval.get('approvedRequesters') if isinstance(approval.get('approvedRequesters'), list) else []
        for requester in approved_requesters:
            if not isinstance(requester, dict):
                continue
            requester_id = self._first_text(requester, 'requesterId', 'requester_id')
            if requester_id:
                merged_requester = dict(requesters_by_id.get(requester_id) or {})
                for key, value in dict(requester).items():
                    if value is None:
                        continue
                    if isinstance(value, str) and not value.strip():
                        continue
                    merged_requester[key] = value
                requesters_by_id[requester_id] = merged_requester or dict(requester)
        approval_results = []
        selected_candidates = []
        for requester_id in approved_ids:
            requester = requesters_by_id.get(requester_id) or {'requesterId': requester_id}
            approval_results.append({
                'requesterId': requester_id,
                'jid': requester_id,
                'error': None,
                'status': 200,
            })
            selected_candidates.append(dict(requester))
        status_ok = bool(raw.get('ok')) and (bool(approved_ids) or pending_after == 0)
        approved_count = len(approved_ids) if approved_ids else self._int_or_none(fallback.get('approved_count')) or 0
        approved_at = self._first_text(current_truth, 'verifiedAt', 'verified_at') or self._first_text(raw, 'finishedAt', 'finished_at')
        return {
            'status': 'success' if status_ok else 'failed',
            'verified': bool(status_ok and (operation.get('verifyOk') is True or pending_after is not None)),
            'verification_pending': False,
            'result_code': 'approved' if status_ok else 'baileys_poc_approval_failed',
            'result_reason': operation.get('message') or raw.get('error') or ('baileys poc approval executed' if status_ok else 'baileys poc approval did not confirm execution'),
            'approved_count': max(0, int(approved_count or 0)),
            'approved_at': approved_at,
            'target_member': {},
            'provider': 'baileys',
            'provider_endpoint': raw.get('provider_endpoint'),
            'raw_result': {
                'approval_run_id': str(fallback.get('approval_run_id') or '').strip() or None,
                'execution_disposition': 'executed' if status_ok else 'failed',
                'pending_before': pending_before,
                'pending_after': pending_after,
                'member_count_before': member_count_before,
                'member_count_after': member_count_after,
                'approved_ids': approved_ids,
                'remaining_pending_ids': remaining_ids,
                'approval_results': approval_results,
                'selected_candidates': selected_candidates,
                'poc_operation': operation,
                'poc_snapshot': snapshot,
            },
        }

    def _normalize_health(self, body: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(body or {})
        normalized.setdefault('configured', bool(self.base_url))
        normalized.setdefault('provider', 'baileys')
        normalized.setdefault('base_url', self.base_url)
        normalized.setdefault('timeout_seconds', self.timeout_seconds)
        normalized.setdefault(
            'supports',
            [
                'approve',
                'strict_queue_and_member_verify',
                'full_queue_sync',
                'official_group_approve',
                'group_member_lookup',
                'group_metadata',
                'assistant_group_runtime',
            ],
        )
        normalized.setdefault('schema_version', 'registration-group-baileys-bridge-v1')
        normalized.setdefault('status', 'healthy' if self.base_url else 'misconfigured')
        return normalized

    def _request_json(
        self,
        *,
        method: str,
        endpoint_key: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self.base_url:
            raise RuntimeError('baileys_runtime_base_url_missing')
        paths = list(self.endpoint_paths.get(endpoint_key) or [])
        if not paths:
            raise RuntimeError(f'baileys_endpoint_paths_missing:{endpoint_key}')
        last_error: Optional[BaseException] = None
        for path in paths:
            try:
                request_payload = payload
                if method.upper() == 'POST' and path == '/tasks/probe':
                    request_payload = self._poc_probe_payload(payload or {})
                elif method.upper() == 'POST' and path in {'/tasks/approve', '/tasks/approve-verify'}:
                    request_payload = self._poc_approve_payload(payload or {})
                response = self.session.request(
                    method.upper(),
                    f'{self.base_url}{path}',
                    json=request_payload if method.upper() != 'GET' else None,
                    headers=self._headers(),
                    timeout=timeout_seconds or self.timeout_seconds,
                )
                if getattr(response, 'status_code', 200) in {404, 405} and path != paths[-1]:
                    continue
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise RuntimeError('baileys bridge returned unexpected payload')
                body.setdefault('provider', 'baileys')
                body.setdefault('provider_endpoint', path)
                return body
            except Exception as exc:
                last_error = exc
                status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
                if status_code in {404, 405} and path != paths[-1]:
                    continue
        if last_error is not None:
            raise last_error
        raise RuntimeError(f'baileys request failed without error: {endpoint_key}')

    def health(self) -> Dict[str, Any]:
        if not self.base_url:
            return self._normalize_health({'configured': False, 'status': 'misconfigured'})
        try:
            body = self._request_json(method='GET', endpoint_key='health')
            return self._normalize_health(body)
        except Exception as exc:
            return self._normalize_health({'status': 'degraded', 'last_error': str(exc)})

    def group_state(self, registration_group: str, *, extra_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        normalized_group = str(registration_group or '').strip()
        if not self.base_url:
            return {
                'group_name': normalized_group,
                'group_id': None,
                'pending_count': None,
                'member_count': None,
                'requester_ids': [],
                'status': 'failed',
                'result_code': 'registration_group_baileys_bridge_not_configured',
                'result_reason': 'registration group Baileys bridge base_url is not configured',
                'provider': 'baileys',
            }
        payload = {'registration_group': normalized_group}
        if isinstance(extra_payload, dict):
            payload.update({k: v for k, v in extra_payload.items() if v is not None})
        group_link = self._first_text(payload, 'groupLink', 'group_link', 'link')
        group_id = self._first_text(payload, 'groupId', 'group_id', 'registration_group')
        if self._looks_like_invite_link(group_id):
            group_link = group_link or group_id
            group_id = ''
        if self._looks_like_invite_link(normalized_group):
            group_link = group_link or normalized_group
            group_id = ''
        if self._looks_like_group_jid(normalized_group):
            group_id = group_id or normalized_group
        for alias in ('registration_group', 'registrationGroup', 'target_group', 'targetGroup', 'group_name', 'groupName'):
            if self._looks_like_invite_link(payload.get(alias)):
                group_link = group_link or str(payload.get(alias) or '').strip()
                payload.pop(alias, None)
        for alias in ('groupId', 'group_id'):
            if self._looks_like_invite_link(payload.get(alias)):
                group_link = group_link or str(payload.get(alias) or '').strip()
                payload.pop(alias, None)
        if group_link:
            payload['groupLink'] = group_link
            payload['link'] = group_link
        if group_id:
            payload['groupId'] = group_id
            payload['group_id'] = group_id
        account_id = self._first_text(
            payload,
            'accountId',
            'account_key',
            'baileys_account_id',
            'provider_account_id',
            'account_id',
        )
        if account_id:
            payload['accountId'] = account_id
        body = self._request_json(method='POST', endpoint_key='group_state', payload=payload)
        normalized = dict(body or {})
        if (
            'snapshot' in normalized
            or 'current_truth' in normalized
            or 'queue_readable' in normalized
            or 'self_participant_found' in normalized
            or 'self_is_admin' in normalized
        ):
            return self._normalize_poc_probe_body(normalized, fallback_payload=payload)
        nested_group_id = self._first_group_jid(normalized)
        nested_group_name = self._first_group_name(normalized)
        top_level_group_id = self._first_text(normalized, 'group_id', 'groupId', 'groupJid')
        if nested_group_id and (not top_level_group_id or not self._looks_like_group_jid(top_level_group_id)):
            normalized['group_id'] = nested_group_id
        top_level_group_name = self._first_text(normalized, 'group_name', 'groupName', 'groupSubject')
        if nested_group_name and (
            not top_level_group_name
            or self._looks_like_invite_link(top_level_group_name)
            or self._looks_like_group_jid(top_level_group_name)
        ):
            normalized['group_name'] = nested_group_name
        normalized.setdefault('group_name', normalized_group)
        normalized.setdefault('group_id', None)
        normalized.setdefault('pending_count', None)
        normalized.setdefault('member_count', None)
        normalized.setdefault('requester_ids', [])
        normalized.setdefault('provider', 'baileys')
        return normalized

    def snapshot_state(self, registration_group: str) -> Dict[str, Any]:
        normalized_group = str(registration_group or '').strip()
        if not self.base_url or not normalized_group:
            return {}
        encoded_group = quote(normalized_group, safe='')
        fallback_path = f'/baileys/groups/{encoded_group}'
        last_error: Optional[BaseException] = None
        for path in (
            f'/groups/{encoded_group}',
            f'/ops/baileys/groups/{encoded_group}',
            fallback_path,
        ):
            try:
                response = self.session.request(
                    'GET',
                    f'{self.base_url}{path}',
                    headers=self._headers(),
                    timeout=self.timeout_seconds,
                )
                if getattr(response, 'status_code', 200) == 404 and path != fallback_path:
                    continue
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise RuntimeError('baileys bridge snapshot returned unexpected payload')
                body.setdefault('provider', 'baileys')
                body.setdefault('provider_endpoint', path)
                return body
            except Exception as exc:
                last_error = exc
                status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
                if status_code == 404 and path != fallback_path:
                    continue
        if last_error is not None:
            raise last_error
        return {}

    def full_queue_sync(self, payload: Dict[str, Any], *, timeout_seconds: Optional[float] = None) -> Dict[str, Any]:
        if not self.base_url:
            return {
                'ok': False,
                'trust_status': 'TRUTH_UNKNOWN',
                'reason_code': 'baileys_bridge_not_configured',
                'provider': 'baileys',
                'source': {'provider': 'baileys', 'mode': 'bridge_not_configured'},
            }
        body = self._request_json(
            method='POST',
            endpoint_key='full_queue_sync',
            payload=dict(payload or {}),
            timeout_seconds=timeout_seconds,
        )
        normalized = dict(body or {})
        if 'snapshot' in normalized or 'current_truth' in normalized:
            return self._normalize_poc_probe_body(normalized, fallback_payload=payload)
        normalized.setdefault('provider', 'baileys')
        source = dict(normalized.get('source') or {}) if isinstance(normalized.get('source'), dict) else {}
        source.setdefault('provider', 'baileys')
        source.setdefault('mode', 'full_queue_sync')
        normalized['source'] = source
        return normalized

    def _approve_timeout_seconds(self, context: Dict[str, Any]) -> float:
        try:
            approved_count = int((context or {}).get('approved_count') or 1)
        except (TypeError, ValueError):
            approved_count = 1
        approved_count = max(1, approved_count)
        scaled_timeout = self.APPROVE_TIMEOUT_BASE_SECONDS + (approved_count * self.APPROVE_TIMEOUT_PER_REQUESTER_SECONDS)
        return max(self.timeout_seconds, float(scaled_timeout))

    def approve(self, context: Dict[str, Any]) -> Dict[str, Any]:
        if not self.base_url:
            return {
                'status': 'failed',
                'verified': False,
                'result_code': 'registration_group_baileys_bridge_not_configured',
                'result_reason': 'registration group Baileys bridge base_url is not configured',
                'approved_count': int((context or {}).get('approved_count') or 1),
                'target_member': {},
                'provider': 'baileys',
                'raw_result': {
                    'approval_run_id': str((context or {}).get('approval_run_id') or '').strip() or None,
                    'execution_disposition': 'failed',
                },
            }
        try:
            body = self._request_json(
                method='POST',
                endpoint_key='approve',
                payload=dict(context or {}),
                timeout_seconds=self._approve_timeout_seconds(context),
            )
        except Exception as exc:
            return {
                'status': 'failed',
                'verified': False,
                'result_code': 'registration_group_baileys_bridge_request_failed',
                'result_reason': str(exc),
                'approved_count': int((context or {}).get('approved_count') or 1),
                'target_member': {},
                'provider': 'baileys',
                'raw_result': {
                    'approval_run_id': str((context or {}).get('approval_run_id') or '').strip() or None,
                    'execution_disposition': 'failed',
                },
            }
        normalized = dict(body or {})
        if 'snapshot' in normalized or 'operation' in normalized or 'task' in normalized:
            return self._normalize_poc_approve_body(normalized, fallback_payload=context)
        normalized.setdefault('status', 'failed')
        normalized.setdefault('verified', False)
        normalized.setdefault('result_code', 'registration_group_baileys_bridge_invalid_response')
        normalized.setdefault('result_reason', '')
        normalized.setdefault('approved_count', int((context or {}).get('approved_count') or 1))
        normalized.setdefault('target_member', {})
        normalized.setdefault('provider', 'baileys')
        raw_result = dict(normalized.get('raw_result') or {})
        raw_result.setdefault('approval_run_id', str((context or {}).get('approval_run_id') or '').strip() or None)
        normalized['raw_result'] = raw_result
        return normalized

    def official_group_approve(self, context: Dict[str, Any]) -> Dict[str, Any]:
        if not self.base_url:
            return {
                'status': 'failed',
                'verified': False,
                'result_code': 'official_group_baileys_bridge_not_configured',
                'result_reason': 'official group Baileys bridge base_url is not configured',
                'approved_count': int((context or {}).get('approved_count') or 1),
                'provider': 'baileys',
                'raw_result': {
                    'approval_run_id': str((context or {}).get('approval_run_id') or '').strip() or None,
                    'execution_disposition': 'failed',
                },
            }
        try:
            body = self._request_json(
                method='POST',
                endpoint_key='official_group_approve',
                payload=dict(context or {}),
                timeout_seconds=self._approve_timeout_seconds(context),
            )
        except Exception as exc:
            return {
                'status': 'failed',
                'verified': False,
                'result_code': 'official_group_baileys_bridge_request_failed',
                'result_reason': str(exc),
                'approved_count': int((context or {}).get('approved_count') or 1),
                'provider': 'baileys',
                'raw_result': {
                    'approval_run_id': str((context or {}).get('approval_run_id') or '').strip() or None,
                    'execution_disposition': 'failed',
                },
            }
        normalized = dict(body or {})
        if 'snapshot' in normalized or 'operation' in normalized or 'task' in normalized:
            return self._normalize_poc_approve_body(normalized, fallback_payload=context)
        normalized.setdefault('status', 'failed')
        normalized.setdefault('verified', False)
        normalized.setdefault('result_code', 'official_group_baileys_bridge_invalid_response')
        normalized.setdefault('result_reason', '')
        normalized.setdefault('approved_count', int((context or {}).get('approved_count') or 1))
        normalized.setdefault('provider', 'baileys')
        raw_result = dict(normalized.get('raw_result') or {})
        raw_result.setdefault('approval_run_id', str((context or {}).get('approval_run_id') or '').strip() or None)
        normalized['raw_result'] = raw_result
        return normalized

    def group_member_lookup(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized_payload = dict(payload or {})
        if not self.base_url:
            return {
                'ok': False,
                'result_code': 'group_member_lookup_baileys_bridge_not_configured',
                'result_reason': 'group member lookup Baileys bridge base_url is not configured',
                'group_id': normalized_payload.get('group_id'),
                'group_name': normalized_payload.get('group_name'),
                'members': [],
                'provider': 'baileys',
            }
        body = self._request_json(
            method='POST',
            endpoint_key='group_member_lookup',
            payload=normalized_payload,
        )
        normalized = dict(body or {})
        normalized.setdefault('ok', True)
        normalized.setdefault('group_id', normalized_payload.get('group_id'))
        normalized.setdefault('group_name', normalized_payload.get('group_name'))
        normalized.setdefault('members', [])
        normalized.setdefault('provider', 'baileys')
        return normalized

    def group_metadata(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized_payload = dict(payload or {})
        if not self.base_url:
            return {
                'ok': False,
                'result_code': 'group_metadata_baileys_bridge_not_configured',
                'result_reason': 'group metadata Baileys bridge base_url is not configured',
                'group_id': normalized_payload.get('group_id'),
                'group_name': normalized_payload.get('group_name'),
                'metadata': {},
                'provider': 'baileys',
            }
        body = self._request_json(
            method='POST',
            endpoint_key='group_metadata',
            payload=normalized_payload,
        )
        normalized = dict(body or {})
        normalized.setdefault('ok', True)
        normalized.setdefault('group_id', normalized_payload.get('group_id'))
        normalized.setdefault('group_name', normalized_payload.get('group_name'))
        normalized.setdefault('metadata', {})
        normalized.setdefault('provider', 'baileys')
        return normalized
