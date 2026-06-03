from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class BaileysRegistrationGroupApprovalExecutor:
    APPROVE_TIMEOUT_BASE_SECONDS = 15.0
    APPROVE_TIMEOUT_PER_REQUESTER_SECONDS = 3.0
    DEFAULT_ENDPOINT_PATHS = {
        'health': ['/health', '/ops/baileys/health', '/baileys/health'],
        'group_state': ['/group-state', '/ops/baileys/group-state', '/baileys/group-state'],
        'approve': ['/approve', '/ops/baileys/approve', '/baileys/approve'],
        'full_queue_sync': ['/full-queue-sync', '/ops/baileys/full-queue-sync', '/baileys/full-queue-sync'],
        'official_group_approve': [
            '/official-group/approve',
            '/ops/baileys/official-group/approve',
            '/baileys/official-group/approve',
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
                response = self.session.request(
                    method.upper(),
                    f'{self.base_url}{path}',
                    json=payload if method.upper() != 'GET' else None,
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
        body = self._request_json(method='POST', endpoint_key='group_state', payload=payload)
        normalized = dict(body or {})
        normalized.setdefault('group_name', normalized_group)
        normalized.setdefault('group_id', None)
        normalized.setdefault('pending_count', None)
        normalized.setdefault('member_count', None)
        normalized.setdefault('requester_ids', [])
        normalized.setdefault('provider', 'baileys')
        return normalized

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
