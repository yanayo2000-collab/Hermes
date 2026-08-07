from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import requests


DEFAULT_TUGAO_FUNNEL_API_URL = 'https://api.timetrade.club/api/v1/analytics/funnel-daily-metrics'
DEFAULT_GROUP_BY = ('date', 'country', 'media_source', 'campaign_id', 'adset_id', 'ad_id', 'external_app')
ALLOWED_GROUP_BY = set(DEFAULT_GROUP_BY)
QUALIFIED_JOIN_SOURCE_FIELD = 'guild_join_success_users'
QUALIFIED_JOIN_METRIC_CONTRACT = 'tugao_funnel_daily_metrics_api_v1'
MAX_QUERY_DAYS = 90
MAX_PAGE_SIZE = 1000
MAX_EXACT_COUNT = 9_007_199_254_740_991

METRIC_FIELDS = (
    'new_registered_users',
    'high_value_l1_female_18_40_users',
    'auto_apply_message_users',
    'im_user_message_ge_3_users',
    QUALIFIED_JOIN_SOURCE_FIELD,
    'guild_join_success_no_wa_users',
    'guild_join_total_users',
)

FORBIDDEN_PII_KEYS = {
    'phone', 'mobile', 'whatsapp', 'wa', 'email', 'name', 'real_name', 'user_id',
}


class TugaoFunnelApiError(RuntimeError):
    pass


class TugaoFunnelPiiError(TugaoFunnelApiError):
    pass


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or '').strip()[:10]
    if not text:
        raise ValueError('date_required')
    return datetime.fromisoformat(text).date()


def _date_text(value: Any) -> str:
    return _parse_date(value).isoformat()


def _parse_number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if math.isfinite(number) else 0.0
    text = str(value).strip()
    if not text or text in {'-', '—', 'N/A', 'n/a'}:
        return 0.0
    text = text.replace(',', '').replace('$', '').replace('%', '').strip()
    try:
        number = float(text)
    except ValueError:
        return 0.0
    return number if math.isfinite(number) else 0.0


def _parse_observed_count(value: Any, field: str) -> int:
    if isinstance(value, bool) or value is None:
        raise TugaoFunnelApiError(f'tugao_funnel_invalid_count:{field}')
    if isinstance(value, str):
        text = value.strip().replace(',', '')
        if not text or not re.fullmatch(r'[0-9]+(?:\.0+)?', text):
            raise TugaoFunnelApiError(f'tugao_funnel_invalid_count:{field}')
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise TugaoFunnelApiError(f'tugao_funnel_invalid_count:{field}') from None
    elif isinstance(value, (int, float)):
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise TugaoFunnelApiError(f'tugao_funnel_invalid_count:{field}') from None
    else:
        raise TugaoFunnelApiError(f'tugao_funnel_invalid_count:{field}')
    if (
        not number.is_finite()
        or number < 0
        or number > MAX_EXACT_COUNT
        or number != number.to_integral_value()
    ):
        raise TugaoFunnelApiError(f'tugao_funnel_invalid_count:{field}')
    return int(number)


def _clean_text(value: Any) -> str:
    return str(value or '').strip()


def _normalize_key(value: Any) -> str:
    return re.sub(r'[^a-z0-9_]', '', str(value or '').strip().lower())


def _scan_forbidden_pii_keys(payload: Any, *, path: str = '$') -> List[str]:
    found: List[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = _normalize_key(key)
            if normalized in FORBIDDEN_PII_KEYS:
                found.append(f'{path}.{key}')
            found.extend(_scan_forbidden_pii_keys(value, path=f'{path}.{key}'))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(_scan_forbidden_pii_keys(value, path=f'{path}[{index}]'))
    return found


def assert_no_forbidden_pii_keys(payload: Any) -> None:
    found = _scan_forbidden_pii_keys(payload)
    if found:
        raise TugaoFunnelPiiError('forbidden_pii_keys:' + ','.join(found[:20]))


def validate_group_by(group_by: Sequence[str]) -> List[str]:
    fields = [str(item or '').strip() for item in group_by if str(item or '').strip()]
    invalid = [item for item in fields if item not in ALLOWED_GROUP_BY]
    if invalid:
        raise ValueError('invalid_group_by:' + ','.join(invalid))
    normalized = fields or list(DEFAULT_GROUP_BY)
    if tuple(normalized) != DEFAULT_GROUP_BY:
        raise ValueError('qualified_join_group_by_must_be_exact')
    return normalized


def validate_date_window(start_date: date, end_date: date) -> None:
    if start_date > end_date:
        raise ValueError('start_date_after_end_date')
    if (end_date - start_date).days + 1 > MAX_QUERY_DAYS:
        raise ValueError('date_range_exceeds_90_days')


def _is_organic_media_source(value: Any) -> bool:
    text = _clean_text(value).lower()
    return text in {'', 'unknown', '未知'} or any(token in text for token in ('internal', 'organic', 'natural', '自然'))


def normalize_platform(media_source: Any) -> str:
    text = _clean_text(media_source)
    lower = text.lower()
    if _is_organic_media_source(text):
        return 'Internal'
    if any(token in lower for token in ('meta', 'facebook', 'fb')):
        return 'Meta'
    if 'tiktok' in lower or 'tik tok' in lower:
        return 'TikTok'
    if 'google' in lower:
        return 'Google'
    return text or 'Unknown'


def tugao_funnel_api_row_to_fact(row: Dict[str, Any]) -> Dict[str, Any]:
    media_source = _clean_text(row.get('media_source'))
    platform = normalize_platform(media_source)
    is_organic = platform == 'Internal' or _is_organic_media_source(media_source)
    qualified = _parse_observed_count(row.get(QUALIFIED_JOIN_SOURCE_FIELD), QUALIFIED_JOIN_SOURCE_FIELD)
    qualified_no_wa = _parse_observed_count(
        row.get('guild_join_success_no_wa_users'), 'guild_join_success_no_wa_users',
    )
    total_joins = _parse_number(row.get('guild_join_total_users'))
    if not total_joins:
        total_joins = float(qualified)
    exact_identity = tuple(_clean_text(row.get(key)) for key in (
        'campaign_id', 'adset_id', 'ad_id', 'external_app',
    ))
    return {
        'date': _date_text(row.get('date')),
        'data_source': 'TugaoFunnel',
        'platform': platform,
        'app_id': '',
        'external_app': _clean_text(row.get('external_app')),
        'appsflyer_app_id': '',
        'country': _clean_text(row.get('country')) or 'Unknown',
        'media_source': media_source or platform,
        'campaign': _clean_text(row.get('campaign_name')) or _clean_text(row.get('campaign_id')) or '未命名',
        'campaign_id': _clean_text(row.get('campaign_id')),
        'ad_group': _clean_text(row.get('adset_name')) or _clean_text(row.get('adset_id')),
        'adset_id': _clean_text(row.get('adset_id')),
        'ad': _clean_text(row.get('ad_name')) or _clean_text(row.get('ad_id')),
        'ad_id': _clean_text(row.get('ad_id')),
        'source_type': '自然量' if is_organic else '推广量',
        'row_count': 1,
        'onsite_registrations': _parse_number(row.get('new_registered_users')),
        'high_value_users': _parse_number(row.get('high_value_l1_female_18_40_users')),
        'im_entries': _parse_number(row.get('auto_apply_message_users')),
        'im_manual_reply_3': _parse_number(row.get('im_user_message_ge_3_users')),
        'guild_joins': total_joins,
        'promotion_guild_joins': 0.0 if is_organic else total_joins,
        'organic_guild_joins': total_joins if is_organic else 0.0,
        'tugao_join_success_users': float(qualified),
        'tugao_join_success_no_wa_users': float(qualified_no_wa),
        'qualified_join_metric_observed': True,
        'qualified_join_exact_attribution': all(exact_identity),
        'qualified_join_attribution_status': 'exact' if all(exact_identity) else 'identity_missing',
        'qualified_join_source_field': QUALIFIED_JOIN_SOURCE_FIELD,
        'source_metric_contract': QUALIFIED_JOIN_METRIC_CONTRACT,
    }


def _qualified_tuple(row: Dict[str, Any]) -> Tuple[str, ...]:
    return tuple(_clean_text(row.get(key)) for key in DEFAULT_GROUP_BY)


@dataclass(frozen=True)
class TugaoFunnelApiResult:
    rows: List[Dict[str, Any]]
    metrics_definition: Dict[str, Any]
    pages: int
    raw_row_count: int


class TugaoFunnelDailyMetricsClient:
    def __init__(
        self,
        *,
        token: str = '',
        base_url: str = DEFAULT_TUGAO_FUNNEL_API_URL,
        session: Any = None,
        auth_header: str = 'authorization',
        page_size: int = 1000,
    ) -> None:
        self.token = str(token or '').strip()
        self.base_url = str(base_url or DEFAULT_TUGAO_FUNNEL_API_URL).strip()
        self.session = session or requests
        self.auth_header = str(auth_header or 'authorization').strip().lower()
        self.page_size = max(1, min(int(page_size or MAX_PAGE_SIZE), MAX_PAGE_SIZE))

    def _headers(self) -> Dict[str, str]:
        if not self.token:
            return {}
        if self.auth_header == 'x-bi-api-token':
            return {'x-bi-api-token': self.token}
        return {'Authorization': f'Bearer {self.token}'}

    def fetch(
        self,
        *,
        start_date: date,
        end_date: date,
        group_by: Sequence[str] = DEFAULT_GROUP_BY,
        page_size: Optional[int] = None,
    ) -> TugaoFunnelApiResult:
        if not self.token:
            raise TugaoFunnelApiError('tugao_funnel_api_token_missing')
        validate_date_window(start_date, end_date)
        group_fields = validate_group_by(group_by)
        normalized_page_size = max(1, min(int(page_size or self.page_size), MAX_PAGE_SIZE))
        params: Dict[str, Any] = {
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat(),
            'group_by': ','.join(group_fields),
            'page_size': normalized_page_size,
        }
        rows: List[Dict[str, Any]] = []
        metrics_definition: Dict[str, Any] = {}
        pages = 0
        cursor: Optional[str] = None
        seen_cursors: Set[str] = set()
        seen_tuples: Set[Tuple[str, ...]] = set()
        while True:
            request_params = {'cursor': cursor, 'page_size': normalized_page_size} if cursor else params
            response = self.session.get(self.base_url, params=request_params, headers=self._headers(), timeout=30)
            status_code = int(getattr(response, 'status_code', 200) or 200)
            if status_code >= 400:
                raise TugaoFunnelApiError(f'tugao_funnel_api_http_{status_code}')
            body = response.json()
            assert_no_forbidden_pii_keys(body)
            if body.get('ok') is False:
                raise TugaoFunnelApiError('tugao_funnel_api_not_ok')
            data = body.get('data') or []
            if not isinstance(data, list):
                raise TugaoFunnelApiError('tugao_funnel_api_data_not_list')
            for raw in data:
                item = dict(raw or {})
                missing = [field for field in (*DEFAULT_GROUP_BY, *METRIC_FIELDS) if field not in item]
                if missing:
                    raise TugaoFunnelApiError(
                        'tugao_funnel_api_missing_fields:' + ','.join(sorted(set(missing))),
                    )
                identity = _qualified_tuple(item)
                if identity in seen_tuples:
                    raise TugaoFunnelApiError('tugao_funnel_api_duplicate_qualified_tuple')
                seen_tuples.add(identity)
                rows.append(item)
            if isinstance(body.get('metrics_definition'), dict):
                metrics_definition = body.get('metrics_definition') or metrics_definition
            pages += 1
            if not body.get('has_more'):
                break
            cursor = str(body.get('next_cursor') or '').strip()
            if not cursor:
                raise TugaoFunnelApiError('tugao_funnel_api_missing_next_cursor')
            if cursor in seen_cursors:
                raise TugaoFunnelApiError('tugao_funnel_api_cursor_loop')
            seen_cursors.add(cursor)
        return TugaoFunnelApiResult(
            rows=[tugao_funnel_api_row_to_fact(row) for row in rows],
            metrics_definition=metrics_definition,
            pages=pages,
            raw_row_count=len(rows),
        )
