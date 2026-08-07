from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Sequence

import requests


DEFAULT_RESULT_MESSAGE_BASE_URL = 'https://api.timetrade.club'
RESULT_MESSAGE_ENDPOINTS = {
    'deliveries': '/api/v1/analytics/marketing-diagnostics/im-result-message-deliveries',
    'interactions': '/api/v1/analytics/marketing-diagnostics/im-result-message-interactions',
    'daily': '/api/v1/analytics/marketing-diagnostics/im-result-message-daily',
}
MAX_RESULT_MESSAGE_PAGE_SIZE = 500
MAX_RESULT_MESSAGE_QUERY_DAYS = 120
RESULT_MESSAGE_STEPS = ('R101', 'R104', 'R105')


class ImResultMessageApiError(RuntimeError):
    pass


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or '').strip()[:10]
    if not text:
        raise ValueError('date_required')
    return datetime.fromisoformat(text).date().isoformat()


@dataclass(frozen=True)
class ImResultMessagePage:
    rows: List[Dict[str, Any]]
    pages: int
    schema_version: str
    source: str
    timezone: str
    definition: Dict[str, Any]


class ImResultMessageClient:
    def __init__(
        self,
        *,
        token: str,
        base_url: str = DEFAULT_RESULT_MESSAGE_BASE_URL,
        session: Any = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.token = str(token or '').strip()
        self.base_url = str(base_url or DEFAULT_RESULT_MESSAGE_BASE_URL).strip().rstrip('/')
        self.session = session or requests
        self.timeout_seconds = max(3.0, float(timeout_seconds or 30.0))

    def _headers(self) -> Dict[str, str]:
        if not self.token:
            raise ImResultMessageApiError('result_message_token_missing')
        return {'Authorization': f'Bearer {self.token}', 'Accept': 'application/json'}

    def fetch(
        self,
        kind: str,
        *,
        start_date: Any,
        end_date: Any,
        page_size: int = MAX_RESULT_MESSAGE_PAGE_SIZE,
        max_pages: int = 100,
        country: str = '',
        external_app: str = '',
        guild_name: str = '',
        step_code: str = '',
    ) -> ImResultMessagePage:
        endpoint = RESULT_MESSAGE_ENDPOINTS.get(str(kind or '').strip())
        if not endpoint:
            raise ValueError('invalid_result_message_endpoint')
        start = date.fromisoformat(_date_text(start_date))
        end = date.fromisoformat(_date_text(end_date))
        if start > end:
            raise ValueError('start_date_after_end_date')
        if (end - start).days + 1 > MAX_RESULT_MESSAGE_QUERY_DAYS:
            raise ValueError('date_range_exceeds_120_days')
        normalized_page_size = max(1, min(int(page_size or MAX_RESULT_MESSAGE_PAGE_SIZE), MAX_RESULT_MESSAGE_PAGE_SIZE))
        base_params: Dict[str, Any] = {
            'start_date': start.isoformat(),
            'end_date': end.isoformat(),
            'page_size': normalized_page_size,
        }
        for key, value in (
            ('country', country),
            ('external_app', external_app),
            ('guild_name', guild_name),
            ('step_code', step_code),
        ):
            text = str(value or '').strip()
            if text:
                base_params[key] = text

        rows: List[Dict[str, Any]] = []
        cursor = ''
        seen_cursors = set()
        pages = 0
        metadata: Dict[str, Any] = {}
        while pages < max(1, int(max_pages or 1)):
            params = dict(base_params)
            if cursor:
                params['cursor'] = cursor
            response = self.session.get(
                f'{self.base_url}{endpoint}',
                headers=self._headers(),
                params=params,
                timeout=self.timeout_seconds,
            )
            status_code = int(getattr(response, 'status_code', 200) or 200)
            if status_code >= 400:
                raise ImResultMessageApiError(f'result_message_http_{status_code}')
            body = response.json()
            if not isinstance(body, dict) or body.get('ok') is False:
                raise ImResultMessageApiError('result_message_response_not_ok')
            data = body.get('data') or []
            if not isinstance(data, list):
                raise ImResultMessageApiError('result_message_data_not_list')
            rows.extend(dict(item) for item in data if isinstance(item, dict))
            pages += 1
            if not metadata:
                metadata = body
            next_cursor = str(body.get('next_cursor') or '').strip()
            if not body.get('has_more') or not next_cursor:
                cursor = ''
                break
            if next_cursor in seen_cursors:
                raise ImResultMessageApiError('result_message_cursor_loop')
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        if cursor:
            raise ImResultMessageApiError('result_message_max_pages_exceeded')
        return ImResultMessagePage(
            rows=rows,
            pages=pages,
            schema_version=str(metadata.get('schema_version') or ''),
            source=str(metadata.get('source') or ''),
            timezone=str(metadata.get('timezone') or 'UTC+0'),
            definition=dict(metadata.get('definition') or {}),
        )

    def fetch_bundle(
        self,
        *,
        start_date: Any,
        end_date: Any,
        kinds: Sequence[str] = ('deliveries', 'interactions', 'daily'),
        page_size: int = MAX_RESULT_MESSAGE_PAGE_SIZE,
        max_pages: int = 100,
    ) -> Dict[str, ImResultMessagePage]:
        return {
            kind: self.fetch(
                kind,
                start_date=start_date,
                end_date=end_date,
                page_size=page_size,
                max_pages=max_pages,
            )
            for kind in kinds
        }
