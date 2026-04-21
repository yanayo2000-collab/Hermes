from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class LiveCrmAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        session: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip('/')
        self.username = username
        self.password = password
        if session is not None:
            self.session = session
        else:
            if requests is None:
                raise RuntimeError('requests is required for live CRM adapter runtime')
            self.session = requests.Session()
        self.token: Optional[str] = None
        self.login_retry_cooldown_seconds = 15
        self.last_login_attempt_at: Optional[float] = None
        self.last_login_ok_at: Optional[float] = None
        self.last_login_error: Optional[str] = None

    def _auth_headers(self) -> Dict[str, str]:
        if not self.token:
            raise RuntimeError('CRM token is not initialized')
        return {'token': self.token}

    def _parse_json_body(self, response: Any, *, action: str, tolerate_invalid_json: bool = False) -> Dict[str, Any]:
        try:
            body = response.json()
        except Exception:
            if tolerate_invalid_json:
                return {}
            text = str(getattr(response, 'text', '') or '').strip()
            status = getattr(response, 'status_code', 'unknown')
            raise RuntimeError(f'CRM {action} returned non-JSON response: status={status} body={text[:300]}')
        if not isinstance(body, dict):
            if tolerate_invalid_json:
                return {}
            raise RuntimeError(f'CRM {action} returned unexpected JSON payload: {body!r}')
        return body

    def _ensure_logged_in(self) -> str:
        if self.token:
            return self.token
        now = time.time()
        if (
            self.last_login_error
            and self.last_login_attempt_at is not None
            and (now - self.last_login_attempt_at) < self.login_retry_cooldown_seconds
        ):
            remaining = round(self.login_retry_cooldown_seconds - (now - self.last_login_attempt_at), 2)
            raise RuntimeError(
                f'CRM login temporarily throttled after recent failure; retry in {remaining}s. '
                f'Last error: {self.last_login_error}'
            )
        return self.login()

    def health_snapshot(self) -> Dict[str, Any]:
        status = 'healthy' if self.token and not self.last_login_error else 'degraded' if self.last_login_error else 'idle'
        return {
            'status': status,
            'token_ready': bool(self.token),
            'login_error': self.last_login_error,
            'last_login_attempt_at': self.last_login_attempt_at,
            'last_login_ok_at': self.last_login_ok_at,
            'login_retry_cooldown_seconds': self.login_retry_cooldown_seconds,
        }

    def _looks_like_auth_error(self, body: Dict[str, Any]) -> bool:
        code = body.get('code')
        msg = str(body.get('msg') or '').lower()
        if code in {401, 403}:
            return True
        return any(keyword in msg for keyword in ['token', 'login', 'expired', 'unauthorized', 'forbidden'])

    def _request_with_login_retry(self, *, action: str, request_fn, tolerate_invalid_json: bool = False) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(2):
            self._ensure_logged_in()
            try:
                response = request_fn(self._auth_headers())
                body = self._parse_json_body(response, action=action, tolerate_invalid_json=tolerate_invalid_json)
            except RuntimeError as exc:
                last_error = exc
                if attempt == 0 and ('CRM token is not initialized' in str(exc) or 'status=401' in str(exc) or 'status=403' in str(exc)):
                    self.token = None
                    continue
                if attempt == 0 and not tolerate_invalid_json and ('non-JSON response' in str(exc)):
                    raise
                raise
            if attempt == 0 and self._looks_like_auth_error(body):
                self.token = None
                continue
            return body
        if last_error:
            raise last_error
        raise RuntimeError(f'CRM {action} failed after login retry')

    def login(self) -> str:
        self.last_login_attempt_at = time.time()
        response = self.session.post(
            f'{self.base_url}/login',
            json={'username': self.username, 'password': self.password},
            timeout=12,
        )
        try:
            body = self._parse_json_body(response, action='login')
        except Exception as exc:
            self.token = None
            self.last_login_error = str(exc)
            raise
        if body.get('code') != 0 or not ((body.get('data') or {}).get('token')):
            self.token = None
            self.last_login_error = f'CRM login failed: {body}'
            raise RuntimeError(self.last_login_error)
        self.token = body['data']['token']
        self.last_login_error = None
        self.last_login_ok_at = self.last_login_attempt_at
        return self.token

    def get_apps(self) -> list[dict[str, Any]]:
        body = self._request_with_login_retry(
            action='get_apps',
            request_fn=lambda headers: self.session.get(
                f'{self.base_url}/customer/ywapps/allList',
                headers=headers,
                timeout=12,
            ),
        )
        return body.get('data') or []

    def get_depts(self) -> list[dict[str, Any]]:
        body = self._request_with_login_retry(
            action='get_depts',
            request_fn=lambda headers: self.session.get(
                f'{self.base_url}/sys/dept/allList',
                headers=headers,
                timeout=12,
            ),
        )
        return body.get('data') or []

    def find_customer(self, *, yw_id: Optional[str] = None, mobile: Optional[str] = None) -> Optional[dict[str, Any]]:
        params: Dict[str, str] = {}
        if yw_id:
            params['ywId'] = yw_id
        if mobile:
            params['mobile'] = mobile
        body = self._request_with_login_retry(
            action='find_customer',
            request_fn=lambda headers: self.session.get(
                f'{self.base_url}/customer/ywcustomer/page',
                headers=headers,
                params=params,
                timeout=15,
            ),
            tolerate_invalid_json=True,
        )
        rows = ((body.get('data') or {}).get('list') or [])
        return rows[0] if rows else None

    def create_customer(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request_with_login_retry(
            action='create_customer',
            request_fn=lambda headers: self.session.post(
                f'{self.base_url}/customer/ywcustomer',
                headers=headers,
                json=payload,
                timeout=20,
            ),
        )

    def update_customer(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request_with_login_retry(
            action='update_customer',
            request_fn=lambda headers: self.session.put(
                f'{self.base_url}/customer/ywcustomer',
                headers=headers,
                json=payload,
                timeout=20,
            ),
        )

    def upload_voucher(self, *, customer_id: str, image_path: str) -> str:
        with Path(image_path).open('rb') as f:
            body = self._request_with_login_retry(
                action='upload_voucher',
                request_fn=lambda headers: self.session.post(
                    f'{self.base_url}/sys/oss/upload?id={customer_id}',
                    headers=headers,
                    files={'file': (Path(image_path).name, f, 'image/png')},
                    timeout=40,
                ),
            )
        src = ((body.get('data') or {}).get('src'))
        if body.get('code') != 0 or not src:
            raise RuntimeError(f'CRM voucher upload failed: {body}')
        return src

    def attach_voucher(self, record: Dict[str, Any], image_url: str, *, remark_suffix: Optional[str] = None) -> Dict[str, Any]:
        payload = dict(record)
        payload['fileUrl'] = image_url
        payload['pzStatus'] = 1
        remark = str(payload.get('remark') or '').strip()
        if remark_suffix:
            payload['remark'] = f'{remark} | {remark_suffix}'.strip(' |')
        return self.update_customer(payload)

    def create_registration_group_batch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request_with_login_retry(
            action='create_registration_group_batch',
            request_fn=lambda headers: self.session.post(
                f'{self.base_url}/customer/ywruquninfo',
                headers=headers,
                json=payload,
                timeout=20,
            ),
        )
