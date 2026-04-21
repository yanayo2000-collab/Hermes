from __future__ import annotations

from typing import Any, Dict, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class WebhookOfficialGroupApprovalExecutor:
    def __init__(
        self,
        *,
        webhook_url: str,
        token: Optional[str] = None,
        session: Any = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.webhook_url = str(webhook_url or '').strip()
        self.token = str(token or '').strip() or None
        self.timeout_seconds = max(3.0, float(timeout_seconds or 20.0))
        if session is not None:
            self.session = session
        else:
            if requests is None:
                raise RuntimeError('requests is required for webhook official-group executor runtime')
            self.session = requests.Session()

    def health(self) -> Dict[str, Any]:
        return {
            'status': 'healthy' if self.webhook_url else 'misconfigured',
            'provider': 'webhook',
            'supports': ['approve'],
            'webhook_url': self.webhook_url,
            'has_token': bool(self.token),
            'timeout_seconds': self.timeout_seconds,
            'schema_version': 'official-group-webhook-v1',
        }

    def _normalize_body(self, body: Dict[str, Any], *, fallback_target_group: str) -> Dict[str, Any]:
        upstream_status = str(body.get('status') or 'failed').strip().lower() or 'failed'
        raw_result = dict(body.get('raw_result') or {})
        raw_result.setdefault('target_group', fallback_target_group)
        raw_result.setdefault('upstream_status', upstream_status)
        if upstream_status == 'success':
            return {
                'status': 'success',
                'result_code': body.get('result_code') or 'approval_ok',
                'result_reason': body.get('result_reason') or '',
                'raw_result': raw_result,
            }
        if upstream_status == 'retryable_failed':
            raw_result['execution_disposition'] = 'retryable_failed'
            raw_result['retryable'] = True
            return {
                'status': 'failed',
                'result_code': body.get('result_code') or 'official_group_executor_retryable_failed',
                'result_reason': body.get('result_reason') or '',
                'raw_result': raw_result,
            }
        if upstream_status == 'manual_required':
            raw_result['execution_disposition'] = 'manual_required'
            raw_result['requires_human_action'] = True
            return {
                'status': 'failed',
                'result_code': body.get('result_code') or 'official_group_executor_manual_required',
                'result_reason': body.get('result_reason') or '',
                'raw_result': raw_result,
            }
        raw_result['execution_disposition'] = 'failed'
        return {
            'status': 'failed',
            'result_code': body.get('result_code') or 'official_group_executor_failed',
            'result_reason': body.get('result_reason') or '',
            'raw_result': raw_result,
        }

    def approve(self, *, target_group: str, lead: Dict[str, Any], crm_snapshot: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
        if not self.webhook_url:
            return {
                'status': 'failed',
                'result_code': 'official_group_executor_not_configured',
                'result_reason': 'official group webhook url is not configured',
                'raw_result': {'target_group': target_group},
            }
        payload = {
            'target_group': str(target_group or '').strip(),
            'lead': dict(lead or {}),
            'crm_snapshot': dict(crm_snapshot or {}),
            'task': dict(task or {}),
        }
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        response = self.session.post(
            self.webhook_url,
            json=payload,
            headers=headers,
            timeout=self.timeout_seconds,
        )
        try:
            body = response.json()
        except Exception as exc:
            return {
                'status': 'failed',
                'result_code': 'official_group_executor_invalid_response',
                'result_reason': f'Webhook returned non-JSON response: {exc}',
                'raw_result': {'target_group': payload['target_group'], 'execution_disposition': 'failed'},
            }
        if not isinstance(body, dict):
            return {
                'status': 'failed',
                'result_code': 'official_group_executor_invalid_response',
                'result_reason': 'Webhook returned unexpected payload.',
                'raw_result': {'target_group': payload['target_group'], 'body': body, 'execution_disposition': 'failed'},
            }
        return self._normalize_body(body, fallback_target_group=payload['target_group'])
