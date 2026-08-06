from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from app.im_diagnostics import infer_dropoff_stage


DEFAULT_IM_DIAGNOSTICS_BASE_URL = 'https://api.timetrade.club'
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500
MAX_RECEPTION_MODE_DAILY_PAGE_SIZE = 2000
ENDPOINTS = ('conversations', 'messages', 'events', 'conversions')
LINK_CLICKS_ENDPOINT = '/api/v1/analytics/marketing-diagnostics/im-link-clicks'
RECEPTION_MODE_DAILY_ENDPOINT = '/api/v1/im/diagnostics/reception-mode-daily'
OFFICIAL_RECEPTION_MODES = {
    'robot_only',
    'human_only',
    'mixed',
    'no_effective_reception',
}

FORBIDDEN_PII_KEYS = {'phone', 'mobile', 'whatsapp', 'wa', 'email', 'name', 'real_name', 'user_id'}
IDENTITY_KEYS_TO_HASH = {'user_id', 'customer_user_id', 'external_user_id', 'sender_actor_id', 'current_assignee_id'}
OPS_GROUP_LINK_DOMAINS = {'chat.whatsapp.com'}
OPS_GROUP_SHORT_LINK_DOMAINS = {'ourl.cn'}
# Tugao may report a Linky click as generic_external. Keep these fallback
# signatures narrow and data-driven so unrelated external links stay generic.
LINKY_DOWNLOAD_DOMAINS = {'m.chimmy.ai'}
LINKY_ANDROID_PACKAGE_IDS = {'com.hwsj.chat'}
LINKY_ANDROID_STORE_PATH = '/store/apps/details'
DOWNLOAD_LINK_TYPES = {'linky_download', 'timo_download', 'app_download', 'download_link'}
DOWNLOAD_LINK_APPS = {'linky', 'timo'}
_SHORT_LINK_DOMAIN_CACHE: Dict[str, str] = {}


class ImDiagnosticsApiError(RuntimeError):
    pass


class ImDiagnosticsAuthError(ImDiagnosticsApiError):
    pass


class ImDiagnosticsResponseError(ImDiagnosticsApiError):
    pass


def _clean_text(value: Any) -> str:
    return str(value or '').strip()


def _normalize_key(value: Any) -> str:
    return ''.join(ch for ch in str(value or '').strip().lower() if ch.isalnum() or ch == '_')


def _stable_hash(*parts: Any, prefix: str = '') -> str:
    digest = hashlib.sha256('|'.join(_clean_text(part) for part in parts).encode('utf-8')).hexdigest()[:32]
    return f'{prefix}{digest}' if prefix else digest


def _coalesce(row: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _clean_text(row.get(key))
        if value:
            return value
    return ''


def _normalize_domain(value: Any) -> str:
    raw = _clean_text(value).lower()
    if not raw:
        return ''
    if '://' in raw:
        parsed = urlparse(raw)
        raw = parsed.netloc or parsed.path
    return raw.split('/')[0].split(':')[0].lstrip('www.')


def _normalize_external_app(value: Any) -> str:
    raw = _clean_text(value)
    lowered = raw.lower()
    if lowered == 'linky':
        return 'Linky'
    if lowered == 'timo':
        return 'Timo'
    return raw


def official_handoff_type(reception_mode: Any) -> str:
    """Map Tugao's official actual-message classification to dashboard segments."""
    mode = _clean_text(reception_mode).lower()
    if mode in {'human_only', 'mixed'}:
        return 'human_assisted'
    if mode == 'robot_only':
        return 'non_human'
    if mode == 'no_effective_reception':
        return 'no_effective_reception'
    return 'unclassified'


def _message_contains_download_link(text: Any, *, external_app: str = '') -> bool:
    raw = _clean_text(text)
    urls = re.findall(r'https?://[^\s<>"\']+', raw, flags=re.IGNORECASE)
    if not urls:
        return False
    app = _normalize_external_app(external_app).lower()
    mentions_app = app in DOWNLOAD_LINK_APPS or any(name in raw.lower() for name in DOWNLOAD_LINK_APPS)
    for url in urls:
        domain = _normalize_domain(url)
        if domain in OPS_GROUP_LINK_DOMAINS or domain in OPS_GROUP_SHORT_LINK_DOMAINS:
            continue
        if domain in LINKY_DOWNLOAD_DOMAINS:
            return True
        if domain == 'play.google.com':
            return mentions_app
        if mentions_app:
            return True
    return False


def _resolve_short_link_domain(link_url: str) -> str:
    url = _clean_text(link_url)
    if not url:
        return ''
    cached = _SHORT_LINK_DOMAIN_CACHE.get(url)
    if cached is not None:
        return cached
    resolved_domain = ''
    try:
        response = requests.get(
            url,
            allow_redirects=True,
            timeout=5,
            stream=True,
            headers={'User-Agent': 'Mozilla/5.0'},
        )
        try:
            resolved_domain = _normalize_domain(getattr(response, 'url', '') or url)
        finally:
            response.close()
    except Exception:
        resolved_domain = ''
    _SHORT_LINK_DOMAIN_CACHE[url] = resolved_domain
    return resolved_domain


def _is_ops_group_link_click(row: Dict[str, Any]) -> bool:
    link_type = _coalesce(row, 'link_url_type')
    link_domain = _normalize_domain(_coalesce(row, 'link_url_domain'))
    if link_type == 'ops_whatsapp_group' or link_domain in OPS_GROUP_LINK_DOMAINS:
        return True
    if link_domain in OPS_GROUP_SHORT_LINK_DOMAINS:
        return _resolve_short_link_domain(_coalesce(row, 'link_url')) in OPS_GROUP_LINK_DOMAINS
    return False


def _is_download_link_click(row: Dict[str, Any], *, external_app: str = '') -> bool:
    """Recognize Linky/Timo download clicks without treating unrelated links as app downloads."""
    link_type = _coalesce(row, 'link_url_type').lower()
    if link_type in DOWNLOAD_LINK_TYPES:
        return True

    link_domain = _normalize_domain(_coalesce(row, 'link_url_domain'))
    if link_domain in LINKY_DOWNLOAD_DOMAINS:
        return True
    if (
        _normalize_external_app(external_app).lower() in DOWNLOAD_LINK_APPS
        and _coalesce(row, 'source_event_name') == 'im_link_click'
        and _coalesce(row, 'entry_source') == 'im_message_link'
    ):
        return True
    if link_domain != 'play.google.com':
        return False

    link_url = _coalesce(row, 'link_url')
    if not link_url:
        return False
    parsed = urlparse(link_url)
    if parsed.path.rstrip('/') != LINKY_ANDROID_STORE_PATH:
        return False
    package_ids = parse_qs(parsed.query).get('id', [])
    return any(package_id in LINKY_ANDROID_PACKAGE_IDS for package_id in package_ids)


def _num(value: Any) -> int:
    try:
        return int(float(_clean_text(value) or 0))
    except Exception:
        return 0


def _float(value: Any) -> float:
    try:
        return float(_clean_text(value) or 0)
    except Exception:
        return 0.0


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = _clean_text(value)[:10]
    if not raw:
        raise ValueError('date_required')
    return datetime.fromisoformat(raw).date().isoformat()


def find_forbidden_pii_keys(payload: Any, *, path: str = '$') -> List[str]:
    found: List[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = _normalize_key(key)
            if normalized in FORBIDDEN_PII_KEYS:
                found.append(f'{path}.{key}')
            found.extend(find_forbidden_pii_keys(value, path=f'{path}.{key}'))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(find_forbidden_pii_keys(value, path=f'{path}[{index}]'))
    return found


def _identity_hash(row: Dict[str, Any], *keys: str) -> str:
    raw = _coalesce(row, *keys)
    return _stable_hash(raw, prefix='im_user_') if raw else ''


def _source_event_name(raw_name: Any) -> str:
    name = _clean_text(raw_name)
    mapping = {
        'im_user_first_manual_reply': 'first_user_reply',
        'im_user_manual_reply_count_3': 'im_message_ge_3',
        'im_deep_conversation': 'im_message_ge_3',
        'im_link_click': 'link_clicked',
        'link_clicked': 'link_clicked',
        'guild_bind_request': 'guild_bind_request',
        'bind_result_success': 'bind_result_success',
        'crm_succeeded': 'crm_succeeded',
        'real_join_succeeded': 'real_join_succeeded',
    }
    return mapping.get(name, name)


def _message_sender_type(raw: Any) -> str:
    sender = _clean_text(raw).lower()
    if sender in {'user', 'customer'}:
        return 'user'
    if sender in {'admin', 'agent', 'agent_manual', 'human'}:
        return 'agent_manual'
    if sender in {'agent_template', 'template'}:
        return 'agent_template'
    if sender in {'system_bot', 'bot', 'bot_auto'}:
        return 'bot'
    if sender in {'system', 'system_event'}:
        return 'system'
    return sender or 'unknown'


def _final_outcome(conversation: Dict[str, Any], conversions: Sequence[Dict[str, Any]]) -> str:
    stage = _clean_text(conversation.get('business_stage')).lower()
    if stage in {'joined', 'success', 'succeed', 'fully_success'}:
        return 'success'
    for row in conversions:
        system_status = _clean_text(row.get('system_status')).lower()
        bind_status = _clean_text(row.get('bind_status')).lower()
        if system_status == 'fully_success' or bind_status == 'fully_success':
            return 'success'
    return 'lost'


def _conversion_events(row: Dict[str, Any], index: int) -> List[Dict[str, Any]]:
    conversation_id = _coalesce(row, 'conversation_id')
    if not conversation_id:
        return []
    occurred_at = _coalesce(row, 'bound_at', 'updated_at', 'created_at')
    event_source = _coalesce(row, 'source_table') or 'timetrade_im_diagnostics_api'
    anonymous_user_id = _identity_hash(row, 'customer_user_id', 'user_id', 'external_user_id')
    link_id_hash = _stable_hash(_coalesce(row, 'external_user_id'), prefix='link_') if _coalesce(row, 'external_user_id') else ''
    bind_status = _clean_text(row.get('bind_status')).lower()
    internal_status = _clean_text(row.get('internal_bind_status')).lower()
    system_status = _clean_text(row.get('system_status')).lower()
    external_app = _normalize_external_app(_coalesce(row, 'external_app'))
    names: List[Tuple[str, str]] = []
    if bind_status in {'bound', 'success', 'fully_success'} or internal_status == 'bound':
        names.append(('guild_bind_request', 'succeed'))
        names.append(('bind_result_success', 'succeed'))
    elif bind_status or internal_status:
        names.append(('guild_bind_request', 'failed' if 'fail' in f'{bind_status} {internal_status}' else bind_status or internal_status))
    if system_status == 'fully_success' or bind_status == 'fully_success':
        names.append(('crm_succeeded', 'succeed'))
        names.append(('real_join_succeeded', 'succeed'))
    events: List[Dict[str, Any]] = []
    for offset, (event_name, status) in enumerate(names):
        events.append({
            'event_id': _coalesce(row, 'conversion_id') + f'_{event_name}' if _coalesce(row, 'conversion_id') else _stable_hash(conversation_id, event_name, occurred_at, index, offset, prefix='im_evt_'),
            'conversation_id': conversation_id,
            'anonymous_user_id': anonymous_user_id,
            'event_name': event_name,
            'event_time': occurred_at,
            'event_status': status,
            'event_source': event_source,
            'external_app': external_app,
            'link_id_hash': link_id_hash,
        })
    return events


@dataclass(frozen=True)
class ImDiagnosticsApiPage:
    rows: List[Dict[str, Any]]
    pages: int
    next_cursor: str
    pii_key_paths: List[str]


class TimeTradeImDiagnosticsClient:
    def __init__(
        self,
        *,
        token: str,
        base_url: str = DEFAULT_IM_DIAGNOSTICS_BASE_URL,
        session: Any = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        auth_header: str = 'authorization',
    ) -> None:
        self.token = _clean_text(token)
        self.base_url = _clean_text(base_url).rstrip('/') or DEFAULT_IM_DIAGNOSTICS_BASE_URL
        self.session = session or requests
        self.timeout = float(timeout or 30.0)
        self.max_retries = max(0, int(max_retries or 0))
        self.sleep = sleep
        self.auth_header = _clean_text(auth_header).lower() or 'authorization'
        if not self.token:
            raise ImDiagnosticsAuthError('missing_token')

    def _headers(self) -> Dict[str, str]:
        if self.auth_header == 'x-im-diagnostics-token':
            return {'x-im-diagnostics-token': self.token}
        return {'Authorization': f'Bearer {self.token}'}

    def _url(self, path: str) -> str:
        normalized = '/' + _clean_text(path).lstrip('/')
        return f'{self.base_url}{normalized}'

    def status(self) -> Dict[str, Any]:
        return self._request('/api/v1/im/diagnostics/status', {})

    def _request(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        attempt = 0
        while True:
            try:
                response = self.session.get(self._url(path), params=params, headers=self._headers(), timeout=self.timeout)
                status_code = int(getattr(response, 'status_code', 200) or 200)
                if status_code in {401, 403}:
                    raise ImDiagnosticsAuthError(f'auth_failed:{status_code}')
                if status_code == 429 or status_code >= 500:
                    if attempt >= self.max_retries:
                        raise ImDiagnosticsResponseError(f'upstream_retry_exhausted:{status_code}')
                    self.sleep(min(2 ** attempt, 8))
                    attempt += 1
                    continue
                if hasattr(response, 'raise_for_status'):
                    response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise ImDiagnosticsResponseError('payload_not_object')
                if body.get('ok') is False:
                    raise ImDiagnosticsResponseError('payload_not_ok')
                data = body.get('data')
                if data is not None and not isinstance(data, list):
                    raise ImDiagnosticsResponseError('data_not_list')
                return body
            except ImDiagnosticsApiError:
                raise
            except Exception as exc:
                if attempt >= self.max_retries:
                    raise ImDiagnosticsResponseError(f'request_failed:{exc.__class__.__name__}') from exc
                self.sleep(min(2 ** attempt, 8))
                attempt += 1

    def fetch_link_clicks(
        self,
        *,
        start_date: str = '',
        end_date: str = '',
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = 100,
        link_url_type: str = '',
        link_url_domain: str = '',
    ) -> ImDiagnosticsApiPage:
        normalized_page_size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
        params: Dict[str, Any] = {'page_size': normalized_page_size}
        if start_date:
            params['start_date'] = _date_text(start_date)
        if end_date:
            params['end_date'] = _date_text(end_date)
        if link_url_type:
            params['link_url_type'] = _clean_text(link_url_type)
        if link_url_domain:
            params['link_url_domain'] = _clean_text(link_url_domain)
        rows: List[Dict[str, Any]] = []
        pii_paths: List[str] = []
        cursor = ''
        seen_cursors = set()
        pages = 0
        while pages < max(1, int(max_pages or 1)):
            request_params = {'cursor': cursor, 'page_size': normalized_page_size} if cursor else dict(params)
            body = self._request_raw(LINK_CLICKS_ENDPOINT, request_params)
            pages += 1
            pii_paths.extend(find_forbidden_pii_keys(body))
            page_rows = _extract_rows(body)
            rows.extend(page_rows)
            next_cursor = _extract_next_cursor(body)
            if not next_cursor:
                cursor = ''
                break
            if next_cursor in seen_cursors:
                raise ImDiagnosticsResponseError('cursor_loop_detected')
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return ImDiagnosticsApiPage(rows=rows, pages=pages, next_cursor=cursor, pii_key_paths=sorted(set(pii_paths))[:50])

    def _request_raw(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        attempt = 0
        while True:
            try:
                response = self.session.get(self._url(path), params=params, headers=self._headers(), timeout=self.timeout)
                status_code = int(getattr(response, 'status_code', 200) or 200)
                if status_code in {401, 403}:
                    raise ImDiagnosticsAuthError(f'auth_failed:{status_code}')
                if status_code == 429 or status_code >= 500:
                    if attempt >= self.max_retries:
                        raise ImDiagnosticsResponseError(f'upstream_retry_exhausted:{status_code}')
                    self.sleep(min(2 ** attempt, 8))
                    attempt += 1
                    continue
                if hasattr(response, 'raise_for_status'):
                    response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise ImDiagnosticsResponseError('payload_not_object')
                if body.get('ok') is False:
                    raise ImDiagnosticsResponseError('payload_not_ok')
                return body
            except ImDiagnosticsApiError:
                raise
            except Exception as exc:
                if attempt >= self.max_retries:
                    raise ImDiagnosticsResponseError(f'request_failed:{exc.__class__.__name__}') from exc
                self.sleep(min(2 ** attempt, 8))
                attempt += 1

    def fetch_endpoint(
        self,
        endpoint: str,
        *,
        snapshot_date: str = '',
        start_date: str = '',
        end_date: str = '',
        conversation_id: str = '',
        event_name: str = '',
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = 100,
    ) -> ImDiagnosticsApiPage:
        if endpoint not in ENDPOINTS:
            raise ValueError('invalid_endpoint')
        normalized_page_size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
        params: Dict[str, Any] = {'page_size': normalized_page_size}
        if snapshot_date:
            params['snapshot_date'] = _date_text(snapshot_date)
        else:
            params['start_date'] = _date_text(start_date)
            params['end_date'] = _date_text(end_date)
        if conversation_id:
            params['conversation_id'] = _clean_text(conversation_id)
        if event_name and endpoint == 'events':
            params['event_name'] = _clean_text(event_name)
        rows: List[Dict[str, Any]] = []
        pii_paths: List[str] = []
        cursor = ''
        seen_cursors = set()
        pages = 0
        while pages < max(1, int(max_pages or 1)):
            request_params = {'cursor': cursor, 'page_size': normalized_page_size} if cursor else dict(params)
            body = self._request(f'/api/v1/im/diagnostics/{endpoint}', request_params)
            pages += 1
            pii_paths.extend(find_forbidden_pii_keys(body))
            rows.extend(dict(item or {}) for item in body.get('data') or [] if isinstance(item, dict))
            next_cursor = _clean_text(body.get('next_cursor'))
            if not body.get('has_more') or not next_cursor:
                cursor = ''
                break
            if next_cursor in seen_cursors:
                raise ImDiagnosticsResponseError('cursor_loop_detected')
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return ImDiagnosticsApiPage(rows=rows, pages=pages, next_cursor=cursor, pii_key_paths=sorted(set(pii_paths))[:50])

    def fetch_reception_mode_daily(
        self,
        *,
        start_date: str,
        end_date: str,
        country: str = '',
        external_app: str = '',
        ab_group_at_entry: str = '',
        reception_mode: str = '',
        page_size: int = MAX_RECEPTION_MODE_DAILY_PAGE_SIZE,
        max_pages: int = 100,
    ) -> ImDiagnosticsApiPage:
        """Read Tugao's official daily reception-mode aggregate without inference."""
        mode = _clean_text(reception_mode).lower()
        if mode and mode not in OFFICIAL_RECEPTION_MODES:
            raise ValueError('invalid_reception_mode')
        normalized_page_size = max(
            1,
            min(int(page_size or MAX_RECEPTION_MODE_DAILY_PAGE_SIZE), MAX_RECEPTION_MODE_DAILY_PAGE_SIZE),
        )
        params: Dict[str, Any] = {
            'start_date': _date_text(start_date),
            'end_date': _date_text(end_date),
            'page_size': normalized_page_size,
        }
        for key, value in (
            ('country', country),
            ('external_app', external_app),
            ('ab_group_at_entry', ab_group_at_entry),
            ('reception_mode', mode),
        ):
            if _clean_text(value):
                params[key] = _clean_text(value)

        rows: List[Dict[str, Any]] = []
        pii_paths: List[str] = []
        cursor = ''
        seen_cursors = set()
        pages = 0
        while pages < max(1, int(max_pages or 1)):
            request_params = dict(params)
            if cursor:
                request_params['cursor'] = cursor
            body = self._request(RECEPTION_MODE_DAILY_ENDPOINT, request_params)
            pages += 1
            pii_paths.extend(find_forbidden_pii_keys(body))
            rows.extend(dict(item or {}) for item in body.get('data') or [] if isinstance(item, dict))
            next_cursor = _clean_text(body.get('next_cursor'))
            if not body.get('has_more') or not next_cursor:
                cursor = ''
                break
            if next_cursor in seen_cursors:
                raise ImDiagnosticsResponseError('cursor_loop_detected')
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return ImDiagnosticsApiPage(
            rows=rows,
            pages=pages,
            next_cursor=cursor,
            pii_key_paths=sorted(set(pii_paths))[:50],
        )


def aggregate_reception_mode_daily(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate official rows by business date and country for BI/reporting."""
    deduped: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw or {})
        mode = _clean_text(row.get('reception_mode')).lower()
        if mode not in OFFICIAL_RECEPTION_MODES:
            continue
        key = (
            _clean_text(row.get('stat_date'))[:10],
            _clean_text(row.get('timezone')),
            _clean_text(row.get('country')),
            _normalize_external_app(row.get('external_app')),
            _clean_text(row.get('ab_group_at_entry')),
            mode,
            _clean_text(row.get('reception_mode_rule_version')),
        )
        deduped[key] = row

    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in deduped.values():
        stat_date = _clean_text(row.get('stat_date'))[:10]
        country = _clean_text(row.get('country'))
        if not stat_date or not country:
            continue
        item = grouped.setdefault((stat_date, country), {
            'stat_date': stat_date,
            'timezone': _clean_text(row.get('timezone')) or 'Asia/Shanghai',
            'country': country,
            'coverage_status': 'available',
            'data_maturity_status': 'final',
            'reception_mode_rule_versions': set(),
            'human_customer_uv': 0,
            'human_success_uv': 0,
            'non_human_customer_uv': 0,
            'non_human_success_uv': 0,
            'no_effective_reception_customer_uv': 0,
            'no_effective_reception_success_uv': 0,
            'total_customer_uv': 0,
            'total_success_uv': 0,
        })
        maturity = _clean_text(row.get('data_maturity_status')).lower()
        if maturity and maturity != 'final':
            item['data_maturity_status'] = maturity
        rule_version = _clean_text(row.get('reception_mode_rule_version'))
        if rule_version:
            item['reception_mode_rule_versions'].add(rule_version)
        customer_uv = _num(row.get('customer_uv'))
        success_uv = _num(row.get('success_uv'))
        item['total_customer_uv'] += customer_uv
        item['total_success_uv'] += success_uv
        handoff = official_handoff_type(row.get('reception_mode'))
        if handoff == 'human_assisted':
            item['human_customer_uv'] += customer_uv
            item['human_success_uv'] += success_uv
        elif handoff == 'non_human':
            item['non_human_customer_uv'] += customer_uv
            item['non_human_success_uv'] += success_uv
        else:
            item['no_effective_reception_customer_uv'] += customer_uv
            item['no_effective_reception_success_uv'] += success_uv

    result: List[Dict[str, Any]] = []
    for item in grouped.values():
        human_base = int(item['human_customer_uv'])
        non_human_base = int(item['non_human_customer_uv'])
        total_base = int(item['total_customer_uv'])
        item['human_conversion_rate'] = round(int(item['human_success_uv']) / human_base, 6) if human_base else 0.0
        item['non_human_conversion_rate'] = round(int(item['non_human_success_uv']) / non_human_base, 6) if non_human_base else 0.0
        item['total_conversion_rate'] = round(int(item['total_success_uv']) / total_base, 6) if total_base else 0.0
        item['reception_mode_rule_versions'] = sorted(item['reception_mode_rule_versions'])
        result.append(item)
    return sorted(result, key=lambda item: (item['stat_date'], item['country']))


def normalize_api_payload(
    *,
    conversations_raw: Sequence[Dict[str, Any]],
    messages_raw: Sequence[Dict[str, Any]],
    events_raw: Sequence[Dict[str, Any]],
    conversions_raw: Sequence[Dict[str, Any]],
    link_clicks_raw: Sequence[Dict[str, Any]] = (),
    snapshot_date: str = '',
) -> Dict[str, Any]:
    conversions_by_conversation: Dict[str, List[Dict[str, Any]]] = {}
    external_app_by_conversation: Dict[str, str] = {}
    for row in conversations_raw:
        cid = _coalesce(row, 'conversation_id')
        external_app = _normalize_external_app(_coalesce(row, 'external_app'))
        if cid and external_app:
            external_app_by_conversation[cid] = external_app
    for row in conversions_raw:
        cid = _coalesce(row, 'conversation_id')
        if cid:
            conversions_by_conversation.setdefault(cid, []).append(dict(row))
            external_app = _normalize_external_app(_coalesce(row, 'external_app'))
            if external_app:
                external_app_by_conversation[cid] = external_app

    normalized_events: List[Dict[str, Any]] = []
    event_names_by_conversation: Dict[str, set[str]] = {}
    for index, row in enumerate(events_raw):
        cid = _coalesce(row, 'conversation_id')
        event_name = _source_event_name(row.get('event_name'))
        if not cid or not event_name:
            continue
        event_names_by_conversation.setdefault(cid, set()).add(event_name)
        normalized_events.append({
            'event_id': _coalesce(row, 'event_id') or _stable_hash(cid, event_name, row.get('occurred_at'), index, prefix='im_evt_'),
            'conversation_id': cid,
            'anonymous_user_id': _identity_hash(row, 'customer_user_id', 'user_id'),
            'event_name': event_name,
            'event_time': _coalesce(row, 'occurred_at', 'received_at'),
            'event_status': _coalesce(row, 'event_status'),
            'event_source': _coalesce(row, 'route_name') or 'timetrade_im_diagnostics_api',
            'external_app': _normalize_external_app(_coalesce(row, 'external_app')) or external_app_by_conversation.get(cid, ''),
            'link_id_hash': _stable_hash(json.dumps(row.get('props') or {}, ensure_ascii=False, sort_keys=True), prefix='link_') if row.get('props') else '',
        })

    for index, row in enumerate(link_clicks_raw):
        cid = _coalesce(row, 'conversation_id')
        if not cid:
            continue
        link_type = _coalesce(row, 'link_url_type')
        link_domain = _normalize_domain(_coalesce(row, 'link_url_domain'))
        external_app = _normalize_external_app(_coalesce(row, 'external_app')) or external_app_by_conversation.get(cid, '')
        if _is_ops_group_link_click(row):
            event_name = 'ops_group_link_clicked'
            link_type = 'ops_whatsapp_group'
        elif _is_download_link_click(row, external_app=external_app):
            event_name = 'link_clicked'
            if external_app:
                link_type = f'{external_app.lower()}_download'
            elif link_domain in LINKY_DOWNLOAD_DOMAINS or link_type == 'linky_download':
                link_type = 'linky_download'
            else:
                link_type = 'app_download'
        else:
            event_name = 'generic_link_clicked'
        event_names_by_conversation.setdefault(cid, set()).add(event_name)
        link_hash = _coalesce(row, 'link_url_hash') or _stable_hash(_coalesce(row, 'link_url'), prefix='link_')
        normalized_events.append({
            'event_id': _coalesce(row, 'event_id') or _stable_hash(cid, event_name, row.get('message_id'), link_hash, index, prefix='im_evt_'),
            'conversation_id': cid,
            'anonymous_user_id': _identity_hash(row, 'customer_user_id', 'user_id', 'anonymous_user_id'),
            'event_name': event_name,
            'event_time': _coalesce(row, 'event_time', 'clicked_at', 'created_at', 'updated_at', 'business_date'),
            'event_status': 'succeed',
            'event_source': _coalesce(row, 'source_event_name') or 'timetrade_im_link_clicks_api',
            'external_app': external_app,
            'campaign_id': _coalesce(row, 'campaign_id'),
            'adset_id': _coalesce(row, 'adset_id'),
            'ad_id': _coalesce(row, 'ad_id'),
            'creative_id': _coalesce(row, 'creative_id'),
            'link_id_hash': link_hash,
            'link_url_domain': link_domain,
            'link_url_type': link_type,
        })

    for index, row in enumerate(conversions_raw):
        for event in _conversion_events(row, index):
            normalized_events.append(event)
            event_names_by_conversation.setdefault(str(event['conversation_id']), set()).add(str(event['event_name']))

    normalized_messages: List[Dict[str, Any]] = []
    per_conversation_index: Dict[str, int] = {}
    link_sent_conversations: set[str] = set()
    link_sent_at_by_conversation: Dict[str, str] = {}
    first_bot_message_at: Dict[str, str] = {}
    for index, row in enumerate(messages_raw):
        cid = _coalesce(row, 'conversation_id')
        if not cid:
            continue
        per_conversation_index[cid] = per_conversation_index.get(cid, 0) + 1
        text = _coalesce(row, 'text', 'message_text_redacted')
        sender_type = _message_sender_type(row.get('sender_type'))
        message_at = _coalesce(row, 'created_at')
        if sender_type in {'bot', 'system', 'agent_template'} and message_at and cid not in first_bot_message_at:
            first_bot_message_at[cid] = message_at
        is_link_message = _message_contains_download_link(
            text,
            external_app=external_app_by_conversation.get(cid, ''),
        )
        if sender_type in {'agent_manual', 'agent_template', 'bot', 'system'} and is_link_message:
            link_sent_conversations.add(cid)
            if message_at and (cid not in link_sent_at_by_conversation or message_at < link_sent_at_by_conversation[cid]):
                link_sent_at_by_conversation[cid] = message_at
        normalized_messages.append({
            'message_id': _coalesce(row, 'message_id', 'client_msg_id') or _stable_hash(cid, row.get('created_at'), index, prefix='im_msg_'),
            'conversation_id': cid,
            'message_index': per_conversation_index[cid],
            'sender_type': sender_type,
            'message_type': _coalesce(row, 'media_kind', 'msg_type') or 'text',
            'message_at': message_at,
            'message_text_redacted': text,
            'language': _coalesce(row, 'language'),
            'template_id': _coalesce(row, 'template_id', 'template_code'),
            'template_name': _coalesce(row, 'template_name', 'template_code'),
            'link_id_hash': _stable_hash(text, prefix='link_') if is_link_message else '',
        })

    normalized_conversations: List[Dict[str, Any]] = []
    for row in conversations_raw:
        cid = _coalesce(row, 'conversation_id')
        if not cid:
            continue
        external_app = _normalize_external_app(_coalesce(row, 'external_app')) or external_app_by_conversation.get(cid, '')
        events = event_names_by_conversation.get(cid, set())
        if 'entered_im' not in events:
            normalized_events.append({
                'event_id': _stable_hash(cid, 'entered_im', row.get('snapshot_date') or snapshot_date, prefix='im_evt_'),
                'conversation_id': cid,
                'anonymous_user_id': _identity_hash(row, 'customer_user_id', 'user_id'),
                'event_name': 'entered_im',
                'event_time': _coalesce(row, 'created_at', 'updated_at', 'snapshot_date') or snapshot_date,
                'event_source': 'timetrade_im_diagnostics_api_synthetic',
                'external_app': external_app,
            })
        if (_num(row.get('system_bot_message_count')) > 0 or cid in first_bot_message_at) and 'auto_apply_message_sent' not in events:
            normalized_events.append({
                'event_id': _stable_hash(cid, 'auto_apply_message_sent', prefix='im_evt_'),
                'conversation_id': cid,
                'anonymous_user_id': _identity_hash(row, 'customer_user_id', 'user_id'),
                'event_name': 'auto_apply_message_sent',
                'event_time': first_bot_message_at.get(cid) or _coalesce(row, 'created_at', 'updated_at', 'snapshot_date') or snapshot_date,
                'event_source': 'timetrade_im_diagnostics_api_synthetic',
                'external_app': external_app,
            })
        if (_num(row.get('system_bot_message_count')) > 0 or cid in first_bot_message_at) and 'message_sent' not in events:
            normalized_events.append({
                'event_id': _stable_hash(cid, 'message_sent', prefix='im_evt_'),
                'conversation_id': cid,
                'anonymous_user_id': _identity_hash(row, 'customer_user_id', 'user_id'),
                'event_name': 'message_sent',
                'event_time': first_bot_message_at.get(cid) or _coalesce(row, 'created_at', 'updated_at', 'snapshot_date') or snapshot_date,
                'event_source': 'im_bot_flow_events',
                'external_app': external_app,
            })
        if cid in link_sent_conversations and 'link_sent' not in events:
            observed_at = link_sent_at_by_conversation.get(cid, '')
            normalized_events.append({
                'event_id': _stable_hash(cid, 'link_sent', prefix='im_evt_'),
                'conversation_id': cid,
                'anonymous_user_id': _identity_hash(row, 'customer_user_id', 'user_id'),
                'event_name': 'link_sent',
                'event_time': observed_at,
                'event_status': 'observed' if observed_at else 'time_missing',
                'event_source': 'timetrade_im_message_link_observed',
                'external_app': external_app,
            })
        final_outcome = _final_outcome(row, conversions_by_conversation.get(cid, []))
        conversation_events = [
            event for event in normalized_events
            if event.get('conversation_id') == cid
        ]
        reception_mode = _clean_text(row.get('reception_mode')).lower()
        normalized_conversations.append({
            'conversation_id': cid,
            'anonymous_user_id': _identity_hash(row, 'customer_user_id', 'user_id'),
            'country': _coalesce(row, 'country'),
            'language': _coalesce(row, 'language'),
            'media_source': _coalesce(row, 'media_source') or 'Tugao',
            'external_app': external_app,
            'campaign_id': _coalesce(row, 'campaign_id'),
            'campaign_name': _coalesce(row, 'campaign_name'),
            'adset_id': _coalesce(row, 'adset_id'),
            'adset_name': _coalesce(row, 'adset_name'),
            'ad_id': _coalesce(row, 'ad_id'),
            'ad_name': _coalesce(row, 'ad_name'),
            'creative_id': _coalesce(row, 'creative_id'),
            'ad_account_id': _coalesce(row, 'ad_account_id'),
            'entered_im_at': _coalesce(row, 'created_at', 'snapshot_date') or snapshot_date,
            'conversation_start_time': _coalesce(row, 'created_at', 'snapshot_date') or snapshot_date,
            'conversation_end_time': _coalesce(row, 'last_message_at', 'updated_at'),
            'first_user_message_at': _coalesce(row, 'first_user_message_at'),
            'first_agent_reply_at': _coalesce(row, 'last_admin_message_at'),
            'first_response_seconds': _float(row.get('first_response_seconds')),
            'final_join_status': _coalesce(row, 'business_stage'),
            'final_outcome': final_outcome,
            'dropoff_stage': infer_dropoff_stage(conversation_events, final_outcome),
            'agent_id_hash': _identity_hash(row, 'current_assignee_id'),
            'agent_team': _coalesce(row, 'current_assignee_type'),
            'agent_shift': _coalesce(row, 'reception_status'),
            # Tugao reception_mode is the official actual-message classification.
            # Missing coverage stays unclassified; never infer it from assignee/status.
            'handoff_type': official_handoff_type(reception_mode),
            'data_quality_status': (
                'timetrade_im_diagnostics_api'
                if reception_mode in OFFICIAL_RECEPTION_MODES
                else 'timetrade_im_reception_mode_uncovered'
            ),
            'pii_scan_status': 'api_filtered',
            'attribution_quality_status': 'attributed' if (_coalesce(row, 'ad_id') or _coalesce(row, 'ad_name')) else 'missing_ad_attribution',
        })

    return {
        'conversations': normalized_conversations,
        'messages': normalized_messages,
        'events': normalized_events,
    }


def fetch_im_diagnostics_payload(
    client: TimeTradeImDiagnosticsClient,
    *,
    link_click_client: Optional[TimeTradeImDiagnosticsClient] = None,
    include_link_click_details: bool = False,
    snapshot_date: str = '',
    start_date: str = '',
    end_date: str = '',
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = 100,
) -> Dict[str, Any]:
    pages: Dict[str, ImDiagnosticsApiPage] = {}
    for endpoint in ENDPOINTS:
        pages[endpoint] = client.fetch_endpoint(
            endpoint,
            snapshot_date=snapshot_date,
            start_date=start_date,
            end_date=end_date,
            page_size=page_size,
            max_pages=max_pages,
        )
    if include_link_click_details:
        link_start_date = start_date or snapshot_date
        link_end_date = end_date or snapshot_date
        if link_start_date and link_end_date:
            pages['link_clicks'] = (link_click_client or client).fetch_link_clicks(
                start_date=link_start_date,
                end_date=link_end_date,
                page_size=page_size,
                max_pages=max_pages,
            )
    normalized = normalize_api_payload(
        conversations_raw=pages['conversations'].rows,
        messages_raw=pages['messages'].rows,
        events_raw=pages['events'].rows,
        conversions_raw=pages['conversions'].rows,
        link_clicks_raw=pages.get('link_clicks', ImDiagnosticsApiPage(rows=[], pages=0, next_cursor='', pii_key_paths=[])).rows,
        snapshot_date=snapshot_date or start_date,
    )
    return {
        'ok': True,
        'source': 'timetrade_im_diagnostics_api',
        'snapshot_date': snapshot_date,
        'start_date': start_date,
        'end_date': end_date,
        'raw_counts': {endpoint: len(page.rows) for endpoint, page in pages.items()},
        'pages': {endpoint: page.pages for endpoint, page in pages.items()},
        'next_cursors': {endpoint: page.next_cursor for endpoint, page in pages.items() if page.next_cursor},
        'pii_key_paths': {endpoint: page.pii_key_paths for endpoint, page in pages.items() if page.pii_key_paths},
        **normalized,
    }


def _extract_rows(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    containers: List[Any] = [body]
    data = body.get('data')
    if isinstance(data, dict):
        containers.insert(0, data)
    elif isinstance(data, list):
        return [dict(item or {}) for item in data if isinstance(item, dict)]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ('rows', 'items', 'records'):
            value = container.get(key)
            if isinstance(value, list):
                return [dict(item or {}) for item in value if isinstance(item, dict)]
    return []


def _extract_next_cursor(body: Dict[str, Any]) -> str:
    for key in ('next_cursor', 'nextCursor', 'cursor'):
        value = _clean_text(body.get(key))
        if value:
            return value
    data = body.get('data')
    if isinstance(data, dict):
        for key in ('next_cursor', 'nextCursor', 'cursor'):
            value = _clean_text(data.get(key))
            if value:
                return value
    return ''
