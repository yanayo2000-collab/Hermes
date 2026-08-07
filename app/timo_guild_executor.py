from __future__ import annotations

import uuid
from typing import Any, Dict, Iterable, Optional

import requests


TIMO_DEFAULT_API_BASE_URL = 'https://g4bms-v.touchchat.me'
TIMO_GET_HOST_LIST_ENDPOINT = 'website-frontend/v1/officalWebGuild/getHostList'
TIMO_AUTH_EXPIRED_RESULT_CODE = 'timo_ticket_expired'
TIMO_AUTH_EXPIRED_MESSAGE = 'Timo 公会后台凭证已过期，需要重新取 Ticket。'


class TimoGuildExecutor:
    def __init__(
        self,
        *,
        base_url: str = TIMO_DEFAULT_API_BASE_URL,
        ticket: str = '',
        lang: str = 'zh_TW',
        guild_uuid: str = '',
        timeout_seconds: float = 15.0,
        session: Any = requests,
    ) -> None:
        self.base_url = str(base_url or TIMO_DEFAULT_API_BASE_URL).strip().rstrip('/') or TIMO_DEFAULT_API_BASE_URL
        self.ticket = str(ticket or '').strip()
        self.lang = str(lang or '').strip() or 'zh_TW'
        self.guild_uuid = str(guild_uuid or '').strip()
        self.timeout_seconds = max(3.0, float(timeout_seconds or 15.0))
        self.session = session

    def configured(self) -> bool:
        return bool(self.ticket)

    def verify_host_membership(self, *, timo_id: str, guild_uuid: Optional[str] = None) -> Dict[str, Any]:
        normalized_timo_id = ''.join(ch for ch in str(timo_id or '').strip() if ch.isdigit())
        if not normalized_timo_id:
            return {
                'ok': False,
                'verified': False,
                'result_code': 'missing_timo_id',
                'result_reason': 'Timo ID is required.',
            }
        if not self.ticket:
            return {
                'ok': False,
                'verified': False,
                'result_code': 'timo_ticket_not_configured',
                'result_reason': 'Timo ticket is not configured.',
            }
        request_payload = {
            'uuid': str(guild_uuid or self.guild_uuid or '').strip(),
            'pageNum': 1,
            'pageSize': 10,
            'userId': normalized_timo_id,
            'status': '',
            'queryRole': '',
            'gender': '',
            'startTime': '',
            'endTime': '',
            'isRealPerson': '',
        }
        try:
            response = self.session.post(
                f'{self.base_url}/{TIMO_GET_HOST_LIST_ENDPOINT}',
                params={'distinctRequestId': uuid.uuid4().hex[:20]},
                json=request_payload,
                headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'ticket': self.ticket,
                    'lang': self.lang,
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            auth_result = self._auth_error_result_from_exception(exc, request_payload)
            if auth_result:
                return auth_result
            return {
                'ok': False,
                'verified': False,
                'result_code': 'timo_request_failed',
                'result_reason': str(exc),
                'request_payload': self._safe_request_payload(request_payload),
            }

        if isinstance(body, dict) and body.get('success') is False:
            if self._is_auth_error_payload(body):
                return self._auth_error_result(request_payload=request_payload, raw_response=body)
            return {
                'ok': False,
                'verified': False,
                'result_code': str(body.get('code') or 'timo_api_rejected'),
                'result_reason': str(body.get('msg') or body.get('message') or 'Timo API rejected the request.'),
                'request_payload': self._safe_request_payload(request_payload),
                'raw_response': self._safe_response(body),
            }

        rows = list(self._extract_rows(body))
        matched = next((row for row in rows if self._row_user_id(row) == normalized_timo_id), None)
        if not matched:
            return {
                'ok': True,
                'verified': False,
                'result_code': 'timo_member_not_found',
                'result_reason': f'Timo ID {normalized_timo_id} was not found in the guild member list.',
                'request_payload': self._safe_request_payload(request_payload),
                'raw_response': self._safe_response(body),
                'candidate_count': len(rows),
            }

        member = self._normalize_member(matched)
        return {
            'ok': True,
            'verified': True,
            'result_code': 'timo_member_verified',
            'result_reason': f'Timo ID {normalized_timo_id} is already in the guild.',
            'request_payload': self._safe_request_payload(request_payload),
            'member': member,
            'raw_response': self._safe_response(body),
            'candidate_count': len(rows),
        }

    @staticmethod
    def _row_user_id(row: Any) -> str:
        if not isinstance(row, dict):
            return ''
        for key in ('userId', 'user_id', 'timoId', 'timo_id', 'id'):
            value = str(row.get(key) or '').strip()
            if value:
                return ''.join(ch for ch in value if ch.isdigit())
        return ''

    @classmethod
    def _extract_rows(cls, payload: Any) -> Iterable[Dict[str, Any]]:
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    yield item
            return
        if not isinstance(payload, dict):
            return
        candidates = [payload]
        for key in ('data', 'info', 'result'):
            child = payload.get(key)
            if isinstance(child, dict):
                candidates.append(child)
            elif isinstance(child, list):
                for item in child:
                    if isinstance(item, dict):
                        yield item
        for candidate in candidates:
            for key in ('data', 'list', 'rows', 'records', 'items', 'content'):
                child = candidate.get(key)
                if isinstance(child, list):
                    for item in child:
                        if isinstance(item, dict):
                            yield item

    @staticmethod
    def _normalize_member(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            'userId': str(row.get('userId') or row.get('user_id') or row.get('timoId') or '').strip(),
            'userUuid': str(row.get('userUuid') or row.get('user_uuid') or '').strip(),
            'nickName': str(row.get('nickName') or row.get('nickname') or row.get('name') or '').strip(),
            'gender': row.get('gender'),
            'status': row.get('status'),
            'isRealPerson': row.get('isRealPerson'),
            'joinTime': row.get('joinTime'),
            'lastActiveTime': row.get('lastActiveTime'),
            'countryName': str(row.get('countryName') or row.get('country') or '').strip(),
            'inviterUserId': str(row.get('inviterUserId') or '').strip(),
            'bindSource': row.get('bindSource'),
            'appointment': row.get('appointment'),
            'commandoFemale': row.get('commandoFemale'),
        }

    @staticmethod
    def _safe_request_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        return dict(payload or {})

    @staticmethod
    def _safe_response(payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        copied = dict(payload)
        for key in ('ticket', 'pcToken', 'token', 'authorization'):
            if key in copied:
                copied[key] = '***'
        return copied

    @classmethod
    def _auth_error_result_from_exception(cls, exc: Exception, request_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        response = getattr(exc, 'response', None)
        status_code = getattr(response, 'status_code', None)
        if status_code in (401, 403):
            return cls._auth_error_result(request_payload=request_payload, http_status=status_code)
        return None

    @classmethod
    def _auth_error_result(
        cls,
        *,
        request_payload: Dict[str, Any],
        raw_response: Any = None,
        http_status: Optional[int] = None,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            'ok': False,
            'verified': False,
            'result_code': TIMO_AUTH_EXPIRED_RESULT_CODE,
            'result_reason': TIMO_AUTH_EXPIRED_MESSAGE,
            'request_payload': cls._safe_request_payload(request_payload),
        }
        if http_status is not None:
            result['http_status'] = http_status
        if raw_response is not None:
            result['raw_response'] = cls._safe_response(raw_response)
        return result

    @staticmethod
    def _is_auth_error_payload(payload: Dict[str, Any]) -> bool:
        code = str(payload.get('code') or payload.get('status') or '').strip().lower()
        if code in {'401', '403', 'unauthorized', 'forbidden', 'token_expired', 'ticket_expired', 'login_expired'}:
            return True
        text = ' '.join(
            str(payload.get(key) or '')
            for key in ('msg', 'message', 'error', 'reason', 'detail')
        ).strip().lower()
        return any(marker in text for marker in (
            'ticket',
            'token',
            'auth',
            'unauthorized',
            'forbidden',
            'expired',
            'login',
            '登录',
            '登入',
            '失效',
            '过期',
            '過期',
            '未登录',
            '未登入',
            '请登录',
            '請登入',
        ))
