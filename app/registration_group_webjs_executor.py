from __future__ import annotations

from typing import Any, Dict, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class WebjsBridgeRegistrationGroupApprovalExecutor:
    APPROVE_TIMEOUT_BASE_SECONDS = 15.0
    APPROVE_TIMEOUT_PER_REQUESTER_SECONDS = 3.0

    def __init__(
        self,
        *,
        base_url: str,
        token: Optional[str] = None,
        session: Any = None,
        timeout_seconds: float = 35.0,
    ) -> None:
        normalized_base_url = str(base_url or '').strip().rstrip('/')
        self.base_url = normalized_base_url
        self.token = str(token or '').strip() or None
        self.timeout_seconds = max(3.0, float(timeout_seconds or 20.0))
        if session is not None:
            self.session = session
        else:
            if requests is None:
                raise RuntimeError('requests is required for webjs bridge registration-group executor runtime')
            self.session = requests.Session()

    def _headers(self) -> Dict[str, str]:
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        return headers

    def _normalize_health(self, body: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(body or {})
        normalized.setdefault('configured', bool(self.base_url))
        normalized.setdefault('provider', 'whatsapp_webjs_bridge')
        normalized.setdefault('base_url', self.base_url)
        normalized.setdefault('timeout_seconds', self.timeout_seconds)
        normalized.setdefault('supports', ['approve', 'strict_queue_and_member_verify', 'crm_batch_writeback_ready'])
        normalized.setdefault('schema_version', 'registration-group-webjs-bridge-v1')
        normalized.setdefault('status', 'healthy' if self.base_url else 'misconfigured')
        return normalized

    def _post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        response = self.session.post(
            f'{self.base_url}{path}',
            json=payload,
            headers=self._headers(),
            timeout=self.timeout_seconds,
        )
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError('webjs bridge returned unexpected payload')
        return body

    def health(self) -> Dict[str, Any]:
        if not self.base_url:
            return self._normalize_health({'configured': False, 'status': 'misconfigured'})
        try:
            response = self.session.get(f'{self.base_url}/health', timeout=self.timeout_seconds)
            body = response.json()
            if not isinstance(body, dict):
                raise RuntimeError('health endpoint returned unexpected payload')
            return self._normalize_health(body)
        except Exception as exc:
            return self._normalize_health({'status': 'degraded', 'last_error': str(exc)})

    def warmup(self) -> Dict[str, Any]:
        if not self.base_url:
            return self._normalize_health({'configured': False, 'status': 'misconfigured'})
        try:
            body = self._post_json('/warmup', {})
            return self._normalize_health(body)
        except Exception as exc:
            return self._normalize_health({'status': 'degraded', 'last_error': str(exc)})

    def group_state(self, registration_group: str) -> Dict[str, Any]:
        normalized_group = str(registration_group or '').strip()
        if not self.base_url:
            return {
                'group_name': normalized_group,
                'group_id': None,
                'pending_count': None,
                'member_count': None,
                'requester_ids': [],
                'status': 'failed',
                'result_code': 'registration_group_webjs_bridge_not_configured',
                'result_reason': 'registration group webjs bridge base_url is not configured',
            }
        body = self._post_json('/group-state', {'registration_group': normalized_group})
        normalized = dict(body or {})
        normalized.setdefault('group_name', normalized_group)
        normalized.setdefault('group_id', None)
        normalized.setdefault('pending_count', None)
        normalized.setdefault('member_count', None)
        normalized.setdefault('requester_ids', [])
        return normalized

    def _approve_timeout_seconds(self, context: Dict[str, Any]) -> float:
        try:
            approved_count = int((context or {}).get('approved_count') or 1)
        except (TypeError, ValueError):
            approved_count = 1
        approved_count = max(1, approved_count)
        scaled_timeout = self.APPROVE_TIMEOUT_BASE_SECONDS + (
            approved_count * self.APPROVE_TIMEOUT_PER_REQUESTER_SECONDS
        )
        return max(self.timeout_seconds, float(scaled_timeout))

    def approve(self, context: Dict[str, Any]) -> Dict[str, Any]:
        if not self.base_url:
            return {
                'status': 'failed',
                'verified': False,
                'result_code': 'registration_group_webjs_bridge_not_configured',
                'result_reason': 'registration group webjs bridge base_url is not configured',
                'approved_count': int((context or {}).get('approved_count') or 1),
                'target_member': {},
                'raw_result': {
                    'approval_run_id': str((context or {}).get('approval_run_id') or '').strip() or None,
                    'execution_disposition': 'failed',
                },
            }
        try:
            response = self.session.post(
                f'{self.base_url}/approve',
                json=dict(context or {}),
                headers=self._headers(),
                timeout=self._approve_timeout_seconds(context),
            )
            body = response.json()
            if not isinstance(body, dict):
                raise RuntimeError('webjs bridge returned unexpected payload')
        except Exception as exc:
            return {
                'status': 'failed',
                'verified': False,
                'result_code': 'registration_group_webjs_bridge_request_failed',
                'result_reason': str(exc),
                'approved_count': int((context or {}).get('approved_count') or 1),
                'target_member': {},
                'raw_result': {
                    'approval_run_id': str((context or {}).get('approval_run_id') or '').strip() or None,
                    'execution_disposition': 'failed',
                },
            }
        normalized = dict(body)
        normalized.setdefault('status', 'failed')
        normalized.setdefault('verified', False)
        normalized.setdefault('result_code', 'registration_group_webjs_bridge_invalid_response')
        normalized.setdefault('result_reason', '')
        normalized.setdefault('approved_count', int((context or {}).get('approved_count') or 1))
        normalized.setdefault('target_member', {})
        raw_result = dict(normalized.get('raw_result') or {})
        raw_result.setdefault('approval_run_id', str((context or {}).get('approval_run_id') or '').strip() or None)
        normalized['raw_result'] = raw_result
        return normalized
