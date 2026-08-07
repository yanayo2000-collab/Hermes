from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence

import requests


DEFAULT_MARKETING_DIAGNOSTICS_DAILY_URL = 'https://api.timetrade.club/api/v1/analytics/marketing-diagnostics/daily'
DEFAULT_MARKETING_DIAGNOSTICS_JOURNEYS_URL = 'https://api.timetrade.club/api/v1/analytics/marketing-diagnostics/journeys'
MAX_QUERY_DAYS = 120
MAX_PAGE_SIZE = 500

FORBIDDEN_PII_KEYS = {
    'phone', 'mobile', 'email', 'name', 'real_name', 'whatsapp', 'wa', 'user_id',
}


class MarketingDiagnosticsApiError(RuntimeError):
    pass


class MarketingDiagnosticsPiiError(MarketingDiagnosticsApiError):
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


def _clean_text(value: Any) -> str:
    return str(value or '').strip()


def _normalize_key(value: Any) -> str:
    return re.sub(r'[^a-z0-9_]', '', str(value or '').strip().lower())


def _parse_number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text in {'-', '—', 'N/A', 'n/a'}:
        return 0.0
    text = text.replace(',', '').replace('$', '').replace('%', '').strip()
    try:
        return float(text)
    except ValueError:
        return 0.0


def scan_forbidden_pii_keys(payload: Any, *, path: str = '$') -> List[str]:
    found: List[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = _normalize_key(key)
            if normalized in FORBIDDEN_PII_KEYS:
                found.append(f'{path}.{key}')
            found.extend(scan_forbidden_pii_keys(value, path=f'{path}.{key}'))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(scan_forbidden_pii_keys(value, path=f'{path}[{index}]'))
    return found


def assert_no_forbidden_pii_keys(payload: Any) -> None:
    found = scan_forbidden_pii_keys(payload)
    if found:
        raise MarketingDiagnosticsPiiError('forbidden_pii_keys:' + ','.join(found[:20]))


def validate_date_window(start_date: date, end_date: date) -> None:
    if start_date > end_date:
        raise ValueError('start_date_after_end_date')
    if (end_date - start_date).days + 1 > MAX_QUERY_DAYS:
        raise ValueError('date_range_exceeds_120_days')


def normalize_platform(value: Any) -> str:
    text = _clean_text(value)
    lower = text.lower()
    if lower in {'', 'unknown', 'internal', 'organic', 'natural', '自然'}:
        return 'Internal'
    if any(token in lower for token in ('meta', 'facebook', 'fb')):
        return 'Meta'
    if 'tiktok' in lower or 'tik tok' in lower:
        return 'TikTok'
    if 'google' in lower:
        return 'Google'
    return text or 'Unknown'


def _metric(metrics: Dict[str, Any], *keys: str) -> float:
    return max(_parse_number(metrics.get(key)) for key in keys)


def marketing_diagnostics_daily_row_to_fact(row: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(row, dict):
        raise MarketingDiagnosticsApiError('row_not_object')
    dataset = _clean_text(row.get('dataset'))
    dimensions = row.get('dimensions') if isinstance(row.get('dimensions'), dict) else {}
    metrics = row.get('metrics') if isinstance(row.get('metrics'), dict) else {}
    business_date = _parse_date(row.get('business_date') or row.get('date')).isoformat()
    media_source = _clean_text(dimensions.get('media_source') or dimensions.get('media_family'))
    platform = normalize_platform(media_source)
    is_organic = platform == 'Internal' or dataset == 'natural_im_funnel_daily'
    country = _clean_text(dimensions.get('country')) or 'Unknown'
    account_id = _clean_text(dimensions.get('account_id'))
    account_name = _clean_text(dimensions.get('account_name'))
    campaign = _clean_text(dimensions.get('campaign')) or _clean_text(dimensions.get('campaign_id')) or '未命名'
    ad_group = _clean_text(dimensions.get('adset')) or _clean_text(dimensions.get('adset_id'))
    ad = _clean_text(dimensions.get('ad')) or _clean_text(dimensions.get('ad_id'))
    im_entries = _metric(metrics, 'imEntry', 'autoApplyMessage')
    im_first_reply = _metric(metrics, 'imFirstManualReply', 'afImFirstReply')
    im_reply_3 = _metric(metrics, 'imReply3', 'afReply3')
    im_reply_5 = _metric(metrics, 'imReply5')
    user_engaged = max(im_first_reply, im_reply_3, im_reply_5)
    im_link_click = _metric(metrics, 'imLinkClick', 'afLinkClick')
    r2_triggered = _metric(metrics, 'imR2Triggered', 'afR2Triggered')
    l1_completed = _metric(metrics, 'l1Completed')
    l1_high_value = _metric(metrics, 'l1HighValue', 'afHighValue')
    high_intent = max(im_link_click, r2_triggered, l1_completed, l1_high_value)
    guild_joins = _metric(metrics, 'dailyGuildJoin')
    cost = _metric(metrics, 'metaSpend')
    return {
        'date': business_date,
        'data_source': 'MarketingDiagnostics',
        'platform': platform,
        'account_id': account_id,
        'app_id': account_name or account_id,
        'appsflyer_app_id': '',
        'country': country,
        'media_source': media_source or platform,
        'campaign': campaign,
        'ad_group': ad_group,
        'ad': ad,
        'source_type': '自然量' if is_organic else '推广量',
        'row_count': 1,
        'cost': cost,
        'impressions': _metric(metrics, 'metaImpressions'),
        'reach': _metric(metrics, 'metaReach'),
        'clicks': _metric(metrics, 'metaClicks'),
        'link_clicks': _metric(metrics, 'metaLinkClicks'),
        'installs': _metric(metrics, 'metaInstalls', 'afInstalls', 'appActivations'),
        'meta_installs': _metric(metrics, 'metaInstalls'),
        'af_installs': _metric(metrics, 'afInstalls'),
        'registrations': _metric(metrics, 'metaRegistrations', 'afRegistrations', 'appRegistrations'),
        'meta_registrations': _metric(metrics, 'metaRegistrations'),
        'af_registrations': _metric(metrics, 'afRegistrations'),
        'onsite_registrations': _metric(metrics, 'appRegistrations', 'afRegistrations'),
        'high_value_users': l1_high_value,
        'im_entries': im_entries,
        'auto_apply_message_users': _metric(metrics, 'autoApplyMessage'),
        'im_first_replies': im_first_reply,
        'im_manual_reply_3': im_reply_3,
        'im_user_message_ge_5_users': im_reply_5,
        'link_click_users': im_link_click,
        'im_link_click_users': im_link_click,
        'im_link_clicks': im_link_click,
        'im_step2_triggers': r2_triggered,
        'linky_register_users': l1_completed,
        'bind_success_users': guild_joins,
        'crm_succeed_users': guild_joins,
        'high_intent_im_users': high_intent,
        'guild_joins': guild_joins,
        'promotion_guild_joins': 0.0 if is_organic else guild_joins,
        'organic_guild_joins': guild_joins if is_organic else 0.0,
        'source_metric_contract': 'marketing_diagnostics_daily_v1',
    }


@dataclass(frozen=True)
class MarketingDiagnosticsFetchResult:
    rows: List[Dict[str, Any]]
    pages: int
    raw_row_count: int
    datasets: Dict[str, int]


class MarketingDiagnosticsDailyClient:
    def __init__(
        self,
        *,
        token: str,
        base_url: str = DEFAULT_MARKETING_DIAGNOSTICS_DAILY_URL,
        session: Any = None,
        page_size: int = MAX_PAGE_SIZE,
    ) -> None:
        self.token = str(token or '').strip()
        self.base_url = str(base_url or DEFAULT_MARKETING_DIAGNOSTICS_DAILY_URL).strip()
        self.session = session or requests
        self.page_size = max(1, min(int(page_size or MAX_PAGE_SIZE), MAX_PAGE_SIZE))

    def _headers(self) -> Dict[str, str]:
        if not self.token:
            return {}
        return {'Authorization': f'Bearer {self.token}', 'Accept': 'application/json'}

    def fetch(
        self,
        *,
        start_date: date,
        end_date: date,
        page_size: Optional[int] = None,
        datasets: Optional[Sequence[str]] = None,
    ) -> MarketingDiagnosticsFetchResult:
        if not self.token:
            raise MarketingDiagnosticsApiError('marketing_diagnostics_token_missing')
        validate_date_window(start_date, end_date)
        normalized_page_size = max(1, min(int(page_size or self.page_size), MAX_PAGE_SIZE))
        rows: List[Dict[str, Any]] = []
        dataset_counts: Dict[str, int] = {}
        cursor = ''
        pages = 0
        wanted_datasets = {str(item or '').strip() for item in (datasets or []) if str(item or '').strip()}
        while True:
            params: Dict[str, Any] = (
                {'cursor': cursor, 'page_size': normalized_page_size}
                if cursor else
                {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat(), 'page_size': normalized_page_size}
            )
            response = self.session.get(self.base_url, params=params, headers=self._headers(), timeout=30)
            status_code = int(getattr(response, 'status_code', 200) or 200)
            if status_code >= 400:
                raise MarketingDiagnosticsApiError(f'marketing_diagnostics_http_{status_code}')
            body = response.json()
            assert_no_forbidden_pii_keys(body)
            if body.get('ok') is False:
                raise MarketingDiagnosticsApiError('marketing_diagnostics_not_ok')
            data = body.get('data') or []
            if not isinstance(data, list):
                raise MarketingDiagnosticsApiError('marketing_diagnostics_data_not_list')
            for item in data:
                if not isinstance(item, dict):
                    continue
                dataset = _clean_text(item.get('dataset'))
                dataset_counts[dataset] = dataset_counts.get(dataset, 0) + 1
                if wanted_datasets and dataset not in wanted_datasets:
                    continue
                rows.append(marketing_diagnostics_daily_row_to_fact(item))
            pages += 1
            cursor = str(body.get('next_cursor') or '').strip()
            if not cursor:
                break
        return MarketingDiagnosticsFetchResult(
            rows=rows,
            pages=pages,
            raw_row_count=sum(dataset_counts.values()),
            datasets=dataset_counts,
        )
