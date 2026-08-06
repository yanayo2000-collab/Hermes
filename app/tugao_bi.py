from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


TUGAO_BIND_SUCCESS_PATH = '/api/v1/analytics/bind-success-events'
TUGAO_PII_KEYS = {'phone', 'mobile', 'email', 'name', 'real_name', 'whatsapp', 'wa'}


class TugaoBiClientError(RuntimeError):
    pass


class TugaoBiAuthError(TugaoBiClientError):
    pass


class TugaoBiResponseError(TugaoBiClientError):
    pass


class TugaoBiSafetyError(TugaoBiClientError):
    pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_text(value: Any) -> str:
    text = str(value or '')
    if len(text) > 300:
        return f'{text[:300]}...<truncated>'
    return text.replace('\n', ' ').replace('\r', ' ')


def _normalize_key(key: Any) -> str:
    return str(key or '').strip().lower().replace('-', '_')


def find_forbidden_pii_keys(value: Any) -> List[str]:
    found: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = _normalize_key(key)
            if normalized in TUGAO_PII_KEYS:
                found.append(str(key))
                continue
            found.extend(find_forbidden_pii_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(find_forbidden_pii_keys(item))
    return sorted(set(found))


def _stable_event_id(raw: Dict[str, Any]) -> str:
    for key in ('event_id', 'id', 'bind_success_event_id'):
        value = str(raw.get(key) or '').strip()
        if value:
            return value
    raise TugaoBiResponseError('missing_event_id')


def _parse_bool(value: Any) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    text = str(value or '').strip().lower()
    return 1 if text in {'1', 'true', 'yes', 'y', 'on'} else 0


def _coalesce(raw: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(raw.get(key) or '').strip()
        if value:
            return value
    return ''


def normalize_tugao_bind_event(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise TugaoBiResponseError('event_not_object')
    pii_keys = find_forbidden_pii_keys(raw)
    if pii_keys:
        raise TugaoBiSafetyError(f'forbidden_pii_keys:{",".join(pii_keys)}')
    event_id = _stable_event_id(raw)
    bind_status = _coalesce(raw, 'bind_status', 'status').lower()
    occurred_at = _coalesce(raw, 'bind_success_time', 'occurred_at', 'event_time', 'created_at')
    updated_at = _coalesce(raw, 'updated_at', 'bind_updated_at')
    business_date = _coalesce(raw, 'business_date_jakarta', 'business_date', 'date')
    customer_user_id = _coalesce(raw, 'customer_user_id', 'customer_id', 'user_id')
    bind_id = _coalesce(raw, 'bind_id', 'guild_bind_id')
    user_key = customer_user_id or bind_id or event_id
    raw_payload_json = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {
        'event_id': event_id,
        'bind_status': bind_status,
        'occurred_at_utc': occurred_at,
        'updated_at_utc': updated_at,
        'business_date': business_date,
        'project': _coalesce(raw, 'project'),
        'country': _coalesce(raw, 'country'),
        'media_source': _coalesce(raw, 'media_source'),
        'campaign_id': _coalesce(raw, 'campaign_id'),
        'campaign_name': _coalesce(raw, 'campaign_name'),
        'adset_id': _coalesce(raw, 'adset_id', 'ad_group_id'),
        'adset_name': _coalesce(raw, 'adset_name', 'ad_group_name'),
        'ad_id': _coalesce(raw, 'ad_id'),
        'ad_name': _coalesce(raw, 'ad_name'),
        'bind_id': bind_id,
        'customer_user_id': customer_user_id,
        'user_key': user_key,
        'has_wa': _parse_bool(raw.get('has_wa')),
        'raw_payload_json': raw_payload_json,
        'raw_payload_sha256': hashlib.sha256(raw_payload_json.encode('utf-8')).hexdigest(),
    }


class TugaoBindSuccessClient:
    def __init__(
        self,
        *,
        token: str,
        base_url: str = 'https://servertest.timetrade.club',
        session: Any,
        timeout: float = 30.0,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.token = str(token or '').strip()
        self.base_url = str(base_url or '').rstrip('/') or 'https://servertest.timetrade.club'
        self.session = session
        self.timeout = float(timeout or 30.0)
        self.max_retries = max(0, int(max_retries or 0))
        self.sleep = sleep
        if not self.token:
            raise TugaoBiAuthError('missing_token')

    @property
    def url(self) -> str:
        return f'{self.base_url}{TUGAO_BIND_SUCCESS_PATH}'

    def _request_page(self, params: Dict[str, Any]) -> Dict[str, Any]:
        attempt = 0
        while True:
            try:
                response = self.session.get(
                    self.url,
                    params=params,
                    headers={'Authorization': f'Bearer {self.token}'},
                    timeout=self.timeout,
                )
                status_code = int(getattr(response, 'status_code', 200) or 200)
                if status_code in {401, 403}:
                    raise TugaoBiAuthError(f'auth_failed:{status_code}')
                if status_code == 429 or status_code >= 500:
                    if attempt >= self.max_retries:
                        raise TugaoBiResponseError(f'upstream_retry_exhausted:{status_code}')
                    retry_after = 0.0
                    headers = getattr(response, 'headers', {}) or {}
                    try:
                        retry_after = float(headers.get('Retry-After') or 0)
                    except Exception:
                        retry_after = 0.0
                    self.sleep(max(retry_after, min(2 ** attempt, 8)))
                    attempt += 1
                    continue
                if hasattr(response, 'raise_for_status'):
                    response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TugaoBiResponseError('payload_not_object')
                data = payload.get('data')
                if data is None:
                    data = []
                if not isinstance(data, list):
                    raise TugaoBiResponseError('data_not_list')
                return payload
            except TugaoBiClientError:
                raise
            except Exception as exc:
                if attempt >= self.max_retries:
                    raise TugaoBiResponseError(f'request_failed:{exc.__class__.__name__}') from exc
                self.sleep(min(2 ** attempt, 8))
                attempt += 1

    def iter_bind_success_events(
        self,
        *,
        start_time: str,
        end_time: str,
        project: Optional[str] = 'TUGAO',
        updated_after: Optional[str] = None,
        page_size: int = 500,
        max_pages: int = 20,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        params: Dict[str, Any] = {
            'start_time': start_time,
            'end_time': end_time,
            'page_size': min(max(int(page_size or 500), 1), 500),
        }
        if project:
            params['project'] = str(project).strip()
        if updated_after:
            params['updated_after'] = str(updated_after).strip()
        rows: List[Dict[str, Any]] = []
        cursor = ''
        seen_cursors = set()
        pages = 0
        max_page_count = max(int(max_pages or 1), 1)
        while pages < max_page_count:
            request_params = dict(params)
            if cursor:
                if cursor in seen_cursors:
                    raise TugaoBiResponseError('cursor_loop_detected')
                seen_cursors.add(cursor)
                request_params['cursor'] = cursor
            payload = self._request_page(request_params)
            pages += 1
            rows.extend(item for item in payload.get('data') or [] if isinstance(item, dict))
            next_cursor = str(payload.get('next_cursor') or '').strip()
            if not payload.get('has_more') or not next_cursor:
                cursor = ''
                break
            cursor = next_cursor
        return rows, {'pages': pages, 'next_cursor': cursor, 'truncated': bool(cursor)}


def ensure_tugao_bind_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tugao_bind_success_raw_events (
            event_id TEXT PRIMARY KEY,
            bind_status TEXT NOT NULL,
            occurred_at_utc TEXT,
            updated_at_utc TEXT,
            business_date TEXT,
            project TEXT,
            country TEXT,
            media_source TEXT,
            campaign_id TEXT,
            campaign_name TEXT,
            adset_id TEXT,
            adset_name TEXT,
            ad_id TEXT,
            ad_name TEXT,
            bind_id TEXT,
            customer_user_id TEXT,
            user_key TEXT,
            has_wa INTEGER NOT NULL DEFAULT 0,
            raw_payload_sha256 TEXT NOT NULL,
            raw_payload_json TEXT NOT NULL,
            first_seen_at_utc TEXT NOT NULL,
            last_seen_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tugao_bind_success_sync_audit (
            sync_id TEXT PRIMARY KEY,
            started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT,
            status TEXT NOT NULL,
            project TEXT,
            start_time TEXT,
            end_time TEXT,
            updated_after TEXT,
            pages INTEGER NOT NULL DEFAULT 0,
            rows_seen INTEGER NOT NULL DEFAULT 0,
            rows_inserted INTEGER NOT NULL DEFAULT 0,
            rows_updated INTEGER NOT NULL DEFAULT 0,
            rows_blocked_pii INTEGER NOT NULL DEFAULT 0,
            error_code TEXT,
            error_message TEXT
        );

        CREATE TABLE IF NOT EXISTS tugao_bind_success_sync_state (
            sync_name TEXT PRIMARY KEY,
            project TEXT,
            last_cursor TEXT,
            last_successful_start_time TEXT,
            last_successful_end_time TEXT,
            last_successful_sync_id TEXT,
            updated_at_utc TEXT NOT NULL
        );
        """
    )


def upsert_tugao_bind_event(conn: sqlite3.Connection, normalized: Dict[str, Any]) -> str:
    now = _utc_now_iso()
    existing = conn.execute(
        "SELECT raw_payload_sha256 FROM tugao_bind_success_raw_events WHERE event_id = ?",
        (normalized['event_id'],),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO tugao_bind_success_raw_events (
            event_id, bind_status, occurred_at_utc, updated_at_utc, business_date, project, country,
            media_source, campaign_id, campaign_name, adset_id, adset_name, ad_id, ad_name,
            bind_id, customer_user_id, user_key, has_wa, raw_payload_sha256, raw_payload_json,
            first_seen_at_utc, last_seen_at_utc
        ) VALUES (
            :event_id, :bind_status, :occurred_at_utc, :updated_at_utc, :business_date, :project, :country,
            :media_source, :campaign_id, :campaign_name, :adset_id, :adset_name, :ad_id, :ad_name,
            :bind_id, :customer_user_id, :user_key, :has_wa, :raw_payload_sha256, :raw_payload_json,
            :first_seen_at_utc, :last_seen_at_utc
        )
        ON CONFLICT(event_id) DO UPDATE SET
            bind_status = excluded.bind_status,
            occurred_at_utc = excluded.occurred_at_utc,
            updated_at_utc = excluded.updated_at_utc,
            business_date = excluded.business_date,
            project = excluded.project,
            country = excluded.country,
            media_source = excluded.media_source,
            campaign_id = excluded.campaign_id,
            campaign_name = excluded.campaign_name,
            adset_id = excluded.adset_id,
            adset_name = excluded.adset_name,
            ad_id = excluded.ad_id,
            ad_name = excluded.ad_name,
            bind_id = excluded.bind_id,
            customer_user_id = excluded.customer_user_id,
            user_key = excluded.user_key,
            has_wa = excluded.has_wa,
            raw_payload_sha256 = excluded.raw_payload_sha256,
            raw_payload_json = excluded.raw_payload_json,
            last_seen_at_utc = excluded.last_seen_at_utc
        """,
        {**normalized, 'first_seen_at_utc': now, 'last_seen_at_utc': now},
    )
    return 'updated' if existing else 'inserted'


def sync_tugao_bind_success_events(
    conn: sqlite3.Connection,
    client: TugaoBindSuccessClient,
    *,
    start_time: str,
    end_time: str,
    project: str = 'TUGAO',
    updated_after: Optional[str] = None,
    page_size: int = 500,
    max_pages: int = 20,
) -> Dict[str, Any]:
    ensure_tugao_bind_tables(conn)
    sync_id = f'tugao_bind_{uuid.uuid4().hex[:16]}'
    started_at = _utc_now_iso()
    conn.execute(
        """
        INSERT INTO tugao_bind_success_sync_audit
        (sync_id, started_at_utc, status, project, start_time, end_time, updated_after)
        VALUES (?, ?, 'running', ?, ?, ?, ?)
        """,
        (sync_id, started_at, project, start_time, end_time, updated_after),
    )
    rows_seen = rows_inserted = rows_updated = rows_blocked_pii = 0
    pages = 0
    try:
        rows, meta = client.iter_bind_success_events(
            start_time=start_time,
            end_time=end_time,
            project=project,
            updated_after=updated_after,
            page_size=page_size,
            max_pages=max_pages,
        )
        pages = int(meta.get('pages') or 0)
        for raw in rows:
            rows_seen += 1
            try:
                normalized = normalize_tugao_bind_event(raw)
            except TugaoBiSafetyError:
                rows_blocked_pii += 1
                continue
            result = upsert_tugao_bind_event(conn, normalized)
            if result == 'inserted':
                rows_inserted += 1
            else:
                rows_updated += 1
        finished_at = _utc_now_iso()
        status = 'partial' if rows_blocked_pii else 'success'
        conn.execute(
            """
            UPDATE tugao_bind_success_sync_audit
            SET finished_at_utc = ?, status = ?, pages = ?, rows_seen = ?, rows_inserted = ?,
                rows_updated = ?, rows_blocked_pii = ?
            WHERE sync_id = ?
            """,
            (finished_at, status, pages, rows_seen, rows_inserted, rows_updated, rows_blocked_pii, sync_id),
        )
        conn.execute(
            """
            INSERT INTO tugao_bind_success_sync_state (
                sync_name, project, last_cursor, last_successful_start_time, last_successful_end_time,
                last_successful_sync_id, updated_at_utc
            ) VALUES ('bind_success_events', ?, '', ?, ?, ?, ?)
            ON CONFLICT(sync_name) DO UPDATE SET
                project = excluded.project,
                last_cursor = excluded.last_cursor,
                last_successful_start_time = excluded.last_successful_start_time,
                last_successful_end_time = excluded.last_successful_end_time,
                last_successful_sync_id = excluded.last_successful_sync_id,
                updated_at_utc = excluded.updated_at_utc
            """,
            (project, start_time, end_time, sync_id, finished_at),
        )
        conn.commit()
        return {
            'sync_id': sync_id,
            'status': status,
            'pages': pages,
            'rows_seen': rows_seen,
            'rows_inserted': rows_inserted,
            'rows_updated': rows_updated,
            'rows_blocked_pii': rows_blocked_pii,
        }
    except Exception as exc:
        finished_at = _utc_now_iso()
        conn.execute(
            """
            UPDATE tugao_bind_success_sync_audit
            SET finished_at_utc = ?, status = 'failed', pages = ?, rows_seen = ?,
                rows_inserted = ?, rows_updated = ?, rows_blocked_pii = ?,
                error_code = ?, error_message = ?
            WHERE sync_id = ?
            """,
            (
                finished_at,
                pages,
                rows_seen,
                rows_inserted,
                rows_updated,
                rows_blocked_pii,
                exc.__class__.__name__,
                _safe_text(exc),
                sync_id,
            ),
        )
        conn.commit()
        raise


def query_tugao_bind_success_rows(
    conn: sqlite3.Connection,
    *,
    start_time: str,
    end_time: str,
    project: Optional[str] = None,
    country: Optional[str] = None,
) -> List[sqlite3.Row]:
    ensure_tugao_bind_tables(conn)
    where = ["LOWER(COALESCE(bind_status, '')) = 'success'"]
    params: List[Any] = []
    start_date = start_time[:10]
    end_date = end_time[:10]
    where.append(
        """(
            (COALESCE(occurred_at_utc, updated_at_utc, '') >= ? AND COALESCE(occurred_at_utc, updated_at_utc, '') < ?)
            OR (
                COALESCE(occurred_at_utc, updated_at_utc, '') = ''
                AND COALESCE(business_date, '') >= ?
                AND COALESCE(business_date, '') < ?
            )
        )"""
    )
    params.extend([start_time, end_time, start_date, end_date])
    if project:
        where.append("project = ?")
        params.append(str(project))
    if country:
        where.append("UPPER(country) = UPPER(?)")
        params.append(str(country))
    return list(conn.execute(
        f"""
        SELECT * FROM tugao_bind_success_raw_events
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(occurred_at_utc, updated_at_utc, business_date, event_id)
        """,
        params,
    ))
