from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple


TUGAO_BIND_SUCCESS_PATH = '/api/v1/analytics/bind-success-events'
TUGAO_PII_KEYS = {'phone', 'mobile', 'email', 'name', 'real_name', 'whatsapp', 'wa'}
GLE_CANONICAL_IDENTITY_CONTRACT_VERSION = 'gle-canonical-identity-v1'

_PERMANENT_CANONICAL_IDENTITY_REASONS = {
    'AMBIGUOUS_CANONICAL_IDENTITY',
    'CANONICAL_IDENTITY_INVALID',
    'CANONICAL_IDENTITY_SOURCE_UNAVAILABLE',
    'CANONICAL_IDENTITY_STORED_INCOMPLETE',
    'EVENT_IDENTITY_DRIFT',
    'EVENT_IDENTITY_MISSING_AFTER_VALID',
    'LEAD_CUSTOMER_LINK_CONFLICT',
}
_PRE_ACCEPTANCE_PERMANENT_REASONS = {
    'CANONICAL_IDENTITY_INVALID',
    'CANONICAL_IDENTITY_MISSING',
    'IDENTITY_CONTRACT_VERSION_INVALID',
    'IDENTITY_CONTRACT_VERSION_UNSUPPORTED',
}


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


def _exact_identity_value(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _canonical_identity_input(raw: Dict[str, Any]) -> Tuple[str, str, str, str]:
    if 'identity_contract_version' not in raw:
        return '', '', '', 'LEGACY_UNVERIFIED'
    version = raw.get('identity_contract_version')
    if not _exact_identity_value(version):
        return '', '', '', 'IDENTITY_CONTRACT_VERSION_INVALID'
    if version != GLE_CANONICAL_IDENTITY_CONTRACT_VERSION:
        return '', '', '', 'IDENTITY_CONTRACT_VERSION_UNSUPPORTED'
    if 'lead_id' not in raw or 'customer_id' not in raw:
        return '', '', '', 'CANONICAL_IDENTITY_MISSING'
    lead_id = raw.get('lead_id')
    customer_id = raw.get('customer_id')
    if not _exact_identity_value(lead_id) or not _exact_identity_value(customer_id):
        return '', '', '', 'CANONICAL_IDENTITY_INVALID'
    return version, lead_id, customer_id, ''


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
    (
        identity_contract_version,
        canonical_lead_id,
        canonical_customer_id,
        canonical_identity_input_reason,
    ) = _canonical_identity_input(raw)
    if 'identity_contract_version' in raw:
        customer_user_id = _coalesce(raw, 'customer_user_id')
    else:
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
        'identity_contract_version': identity_contract_version,
        'canonical_lead_id': canonical_lead_id,
        'canonical_customer_id': canonical_customer_id,
        'canonical_identity_input_reason': canonical_identity_input_reason,
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
            last_seen_at_utc TEXT NOT NULL,
            identity_contract_version TEXT,
            canonical_lead_id TEXT,
            canonical_customer_id TEXT,
            canonical_identity_status TEXT NOT NULL DEFAULT 'LEGACY_UNVERIFIED',
            canonical_identity_reason TEXT NOT NULL DEFAULT 'LEGACY_UNVERIFIED'
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
    existing_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(tugao_bind_success_raw_events)").fetchall()
    }
    identity_columns = {
        'identity_contract_version': 'TEXT',
        'canonical_lead_id': 'TEXT',
        'canonical_customer_id': 'TEXT',
        'canonical_identity_status': "TEXT NOT NULL DEFAULT 'LEGACY_UNVERIFIED'",
        'canonical_identity_reason': "TEXT NOT NULL DEFAULT 'LEGACY_UNVERIFIED'",
    }
    for name, declaration in identity_columns.items():
        if name not in existing_columns:
            conn.execute(
                f'ALTER TABLE tugao_bind_success_raw_events ADD COLUMN {name} {declaration}'
            )


def verify_tugao_canonical_identity(
    conn: sqlite3.Connection,
    *,
    lead_id: str,
    customer_id: str,
) -> Tuple[str, str]:
    if not _exact_identity_value(lead_id) or not _exact_identity_value(customer_id):
        return 'BLOCKED', 'CANONICAL_IDENTITY_INVALID'
    try:
        lead_rows = conn.execute(
            "SELECT lead_id,matched_customer_id FROM leads WHERE lead_id=? LIMIT 2",
            (lead_id,),
        ).fetchall()
        customer_rows = conn.execute(
            "SELECT customer_id,lead_id FROM customer_projection WHERE customer_id=? LIMIT 2",
            (customer_id,),
        ).fetchall()
        leads_for_customer = conn.execute(
            "SELECT lead_id FROM leads WHERE matched_customer_id=? ORDER BY lead_id LIMIT 2",
            (customer_id,),
        ).fetchall()
        customers_for_lead = conn.execute(
            "SELECT customer_id FROM customer_projection WHERE lead_id=? ORDER BY customer_id LIMIT 2",
            (lead_id,),
        ).fetchall()
        lead_row = lead_rows[0] if len(lead_rows) == 1 else None
        customer_row = customer_rows[0] if len(customer_rows) == 1 else None
        if lead_row is not None and (
            not _exact_identity_value(lead_row[0])
            or not _exact_identity_value(lead_row[1])
        ):
            return 'BLOCKED', 'CANONICAL_IDENTITY_INVALID'
        if customer_row is not None and (
            not _exact_identity_value(customer_row[0])
            or not _exact_identity_value(customer_row[1])
        ):
            return 'BLOCKED', 'CANONICAL_IDENTITY_INVALID'
        if lead_row is not None and (
            lead_row[0] != lead_id or lead_row[1] != customer_id
        ):
            return 'BLOCKED', 'LEAD_CUSTOMER_LINK_CONFLICT'
        if customer_row is not None and (
            customer_row[0] != customer_id or customer_row[1] != lead_id
        ):
            return 'BLOCKED', 'LEAD_CUSTOMER_LINK_CONFLICT'
        reverse_leads = tuple(row[0] for row in leads_for_customer)
        reverse_customers = tuple(row[0] for row in customers_for_lead)
        if not all(
            _exact_identity_value(value)
            for value in (*reverse_leads, *reverse_customers)
        ):
            return 'BLOCKED', 'CANONICAL_IDENTITY_INVALID'
        if len(reverse_leads) > 1 or len(reverse_customers) > 1:
            return 'BLOCKED', 'AMBIGUOUS_CANONICAL_IDENTITY'
        reverse_lead = reverse_leads[0] if reverse_leads else None
        reverse_customer = reverse_customers[0] if reverse_customers else None
        if (
            reverse_lead is not None and reverse_lead != lead_id
        ) or (
            reverse_customer is not None and reverse_customer != customer_id
        ):
            return 'BLOCKED', 'LEAD_CUSTOMER_LINK_CONFLICT'
        if lead_row is None or customer_row is None:
            return 'PENDING_VERIFICATION', 'CANONICAL_IDENTITY_NOT_IN_CRM'
        if (reverse_lead, reverse_customer) != (lead_id, customer_id):
            return 'BLOCKED', 'AMBIGUOUS_CANONICAL_IDENTITY'
        return 'VERIFIED', ''
    except sqlite3.Error:
        return 'BLOCKED', 'CANONICAL_IDENTITY_SOURCE_UNAVAILABLE'


def _canonical_identity_state(
    conn: sqlite3.Connection,
    *,
    normalized: Dict[str, Any],
    existing: Optional[Mapping[str, Any]],
) -> Tuple[Optional[str], Optional[str], Optional[str], str, str]:
    incoming_version = str(normalized.get('identity_contract_version') or '')
    incoming_lead = str(normalized.get('canonical_lead_id') or '')
    incoming_customer = str(normalized.get('canonical_customer_id') or '')
    input_reason = str(normalized.get('canonical_identity_input_reason') or '')
    incoming_pair_valid = bool(incoming_version and incoming_lead and incoming_customer)

    if existing is None:
        stored_version = stored_lead = stored_customer = None
        stored_status = stored_reason = ''
    else:
        stored_version = existing['identity_contract_version']
        stored_lead = existing['canonical_lead_id']
        stored_customer = existing['canonical_customer_id']
        stored_status = str(existing['canonical_identity_status'] or '')
        stored_reason = str(existing['canonical_identity_reason'] or '')

    stored_identity_values = (stored_version, stored_lead, stored_customer)
    if any(stored_identity_values) and (
        not all(stored_identity_values)
        or stored_version != GLE_CANONICAL_IDENTITY_CONTRACT_VERSION
        or not _exact_identity_value(stored_lead)
        or not _exact_identity_value(stored_customer)
    ):
        return (
            stored_version,
            stored_lead,
            stored_customer,
            'BLOCKED',
            'CANONICAL_IDENTITY_STORED_INCOMPLETE',
        )
    if all(stored_identity_values) and (
        (stored_status == 'VERIFIED' and stored_reason != '')
        or (
            stored_status == 'PENDING_VERIFICATION'
            and stored_reason != 'CANONICAL_IDENTITY_NOT_IN_CRM'
        )
        or (
            stored_status == 'BLOCKED'
            and stored_reason not in _PERMANENT_CANONICAL_IDENTITY_REASONS
        )
        or stored_status not in {'VERIFIED', 'PENDING_VERIFICATION', 'BLOCKED'}
    ):
        return (
            stored_version,
            stored_lead,
            stored_customer,
            'BLOCKED',
            'CANONICAL_IDENTITY_STORED_INCOMPLETE',
        )
    if existing is not None and not any(stored_identity_values):
        if stored_status == 'BLOCKED' and stored_reason in _PRE_ACCEPTANCE_PERMANENT_REASONS:
            return None, None, None, 'BLOCKED', stored_reason
        if (stored_status, stored_reason) != ('LEGACY_UNVERIFIED', 'LEGACY_UNVERIFIED'):
            return None, None, None, 'BLOCKED', 'CANONICAL_IDENTITY_STORED_INCOMPLETE'
    if all(stored_identity_values):
        if stored_reason in _PERMANENT_CANONICAL_IDENTITY_REASONS:
            return stored_version, stored_lead, stored_customer, 'BLOCKED', stored_reason
        if not incoming_pair_valid:
            return (
                stored_version,
                stored_lead,
                stored_customer,
                'BLOCKED',
                'EVENT_IDENTITY_MISSING_AFTER_VALID',
            )
        if incoming_lead != stored_lead or incoming_customer != stored_customer:
            return stored_version, stored_lead, stored_customer, 'BLOCKED', 'EVENT_IDENTITY_DRIFT'
        if stored_status == 'VERIFIED':
            return stored_version, stored_lead, stored_customer, 'VERIFIED', ''
        if stored_status == 'PENDING_VERIFICATION' and stored_reason == 'CANONICAL_IDENTITY_NOT_IN_CRM':
            status, reason = verify_tugao_canonical_identity(
                conn,
                lead_id=stored_lead,
                customer_id=stored_customer,
            )
            return stored_version, stored_lead, stored_customer, status, reason
        return stored_version, stored_lead, stored_customer, 'BLOCKED', stored_reason

    if incoming_pair_valid:
        status, reason = verify_tugao_canonical_identity(
            conn,
            lead_id=incoming_lead,
            customer_id=incoming_customer,
        )
        return incoming_version, incoming_lead, incoming_customer, status, reason
    if input_reason == 'LEGACY_UNVERIFIED':
        return None, None, None, 'LEGACY_UNVERIFIED', 'LEGACY_UNVERIFIED'
    return None, None, None, 'BLOCKED', input_reason


def upsert_tugao_bind_event(conn: sqlite3.Connection, normalized: Dict[str, Any]) -> str:
    now = _utc_now_iso()
    for _ in range(4):
        existing_row = conn.execute(
            """SELECT raw_payload_sha256,identity_contract_version,canonical_lead_id,
                      canonical_customer_id,canonical_identity_status,canonical_identity_reason
               FROM tugao_bind_success_raw_events WHERE event_id = ?""",
            (normalized['event_id'],),
        ).fetchone()
        existing = None
        if existing_row is not None:
            existing = {
                'raw_payload_sha256': existing_row[0],
                'identity_contract_version': existing_row[1],
                'canonical_lead_id': existing_row[2],
                'canonical_customer_id': existing_row[3],
                'canonical_identity_status': existing_row[4],
                'canonical_identity_reason': existing_row[5],
            }
        (
            identity_contract_version,
            canonical_lead_id,
            canonical_customer_id,
            canonical_identity_status,
            canonical_identity_reason,
        ) = _canonical_identity_state(conn, normalized=normalized, existing=existing)
        values = {
            **normalized,
            'identity_contract_version': identity_contract_version,
            'canonical_lead_id': canonical_lead_id,
            'canonical_customer_id': canonical_customer_id,
            'canonical_identity_status': canonical_identity_status,
            'canonical_identity_reason': canonical_identity_reason,
            'first_seen_at_utc': now,
            'last_seen_at_utc': now,
        }
        if existing is None:
            cursor = conn.execute(
                """
        INSERT INTO tugao_bind_success_raw_events (
            event_id, bind_status, occurred_at_utc, updated_at_utc, business_date, project, country,
            media_source, campaign_id, campaign_name, adset_id, adset_name, ad_id, ad_name,
            bind_id, customer_user_id, user_key, has_wa, raw_payload_sha256, raw_payload_json,
            first_seen_at_utc, last_seen_at_utc, identity_contract_version, canonical_lead_id,
            canonical_customer_id, canonical_identity_status, canonical_identity_reason
        ) VALUES (
            :event_id, :bind_status, :occurred_at_utc, :updated_at_utc, :business_date, :project, :country,
            :media_source, :campaign_id, :campaign_name, :adset_id, :adset_name, :ad_id, :ad_name,
            :bind_id, :customer_user_id, :user_key, :has_wa, :raw_payload_sha256, :raw_payload_json,
            :first_seen_at_utc, :last_seen_at_utc, :identity_contract_version, :canonical_lead_id,
            :canonical_customer_id, :canonical_identity_status, :canonical_identity_reason
        )
        ON CONFLICT(event_id) DO NOTHING
        """,
                values,
            )
            if cursor.rowcount == 1:
                return 'inserted'
            continue
        values.update(
            {
                'expected_identity_contract_version': existing['identity_contract_version'],
                'expected_canonical_lead_id': existing['canonical_lead_id'],
                'expected_canonical_customer_id': existing['canonical_customer_id'],
                'expected_canonical_identity_status': existing['canonical_identity_status'],
                'expected_canonical_identity_reason': existing['canonical_identity_reason'],
            }
        )
        cursor = conn.execute(
            """
        UPDATE tugao_bind_success_raw_events SET
            bind_status = :bind_status,
            occurred_at_utc = :occurred_at_utc,
            updated_at_utc = :updated_at_utc,
            business_date = :business_date,
            project = :project,
            country = :country,
            media_source = :media_source,
            campaign_id = :campaign_id,
            campaign_name = :campaign_name,
            adset_id = :adset_id,
            adset_name = :adset_name,
            ad_id = :ad_id,
            ad_name = :ad_name,
            bind_id = :bind_id,
            customer_user_id = :customer_user_id,
            user_key = :user_key,
            has_wa = :has_wa,
            raw_payload_sha256 = :raw_payload_sha256,
            raw_payload_json = :raw_payload_json,
            identity_contract_version = :identity_contract_version,
            canonical_lead_id = :canonical_lead_id,
            canonical_customer_id = :canonical_customer_id,
            canonical_identity_status = :canonical_identity_status,
            canonical_identity_reason = :canonical_identity_reason,
            last_seen_at_utc = :last_seen_at_utc
        WHERE event_id = :event_id
          AND identity_contract_version IS :expected_identity_contract_version
          AND canonical_lead_id IS :expected_canonical_lead_id
          AND canonical_customer_id IS :expected_canonical_customer_id
          AND canonical_identity_status IS :expected_canonical_identity_status
          AND canonical_identity_reason IS :expected_canonical_identity_reason
        """,
            values,
        )
        if cursor.rowcount == 1:
            return 'updated'
    raise TugaoBiResponseError('canonical_identity_concurrent_update')


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
