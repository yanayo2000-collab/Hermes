from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional


APP_FAN_SOURCE = 'tugao_app'
APP_FAN_EVIDENCE_VERSION = 'tugao-success-v1'
APP_FAN_COVERAGE_START = {
    'linky': '2026-05-17',
    'timo': '2026-07-03',
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def ensure_streamer_app_fan_table(conn: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS streamer_app_fan_identities (
            app_name TEXT NOT NULL,
            streamer_id TEXT NOT NULL,
            is_app_fan INTEGER NOT NULL DEFAULT 1 CHECK(is_app_fan = 1),
            identity_status TEXT NOT NULL DEFAULT 'confirmed',
            acquisition_source TEXT NOT NULL DEFAULT 'tugao_app',
            source_record_type TEXT NOT NULL,
            first_source_record_id TEXT NOT NULL,
            last_source_record_id TEXT NOT NULL,
            first_confirmed_at TEXT NOT NULL,
            last_confirmed_at TEXT NOT NULL,
            evidence_version TEXT NOT NULL DEFAULT 'tugao-success-v1',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(app_name, streamer_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_streamer_app_fan_source
            ON streamer_app_fan_identities(acquisition_source, app_name, last_confirmed_at)
        """,
        """
        CREATE TABLE IF NOT EXISTS streamer_app_fan_coverage (
            app_name TEXT PRIMARY KEY,
            history_complete INTEGER NOT NULL DEFAULT 0,
            complete_through TEXT NOT NULL DEFAULT '',
            source_name TEXT NOT NULL DEFAULT '',
            source_snapshot_id TEXT NOT NULL DEFAULT '',
            source_id_count INTEGER NOT NULL DEFAULT 0,
            confirmed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    for statement in statements:
        conn.execute(statement)


def begin_immediate_with_retry(
    conn: sqlite3.Connection,
    *,
    timeout_seconds: float = 60.0,
    retry_interval_seconds: float = 0.1,
) -> int:
    """Acquire the one SQLite writer slot before starting the identity mutation."""
    deadline = time.monotonic() + max(float(timeout_seconds), 0.0)
    attempts = 0
    while True:
        attempts += 1
        try:
            conn.execute('BEGIN IMMEDIATE')
            return attempts
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if 'locked' not in message and 'busy' not in message:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(max(float(retry_interval_seconds), 0.01), remaining))


def import_complete_app_fan_history(
    conn: sqlite3.Connection,
    *,
    app_name: str,
    streamer_ids: Iterable[str],
    snapshot_id: str,
    complete_through: str,
    observed_at: Optional[str] = None,
) -> Dict[str, Any]:
    ensure_streamer_app_fan_table(conn)
    normalized_app = str(app_name or '').strip().lower()
    if normalized_app not in {'linky', 'timo'}:
        raise ValueError('unsupported_app_fan_app')
    normalized_ids = {
        str(value or '').strip().lstrip('\ufeff')
        for value in streamer_ids
        if str(value or '').strip().lstrip('\ufeff')
    }
    if not normalized_ids or any(not value.isdigit() for value in normalized_ids):
        raise ValueError('invalid_app_fan_history_ids')
    normalized_snapshot = str(snapshot_id or '').strip().lower()
    if len(normalized_snapshot) != 64 or any(char not in '0123456789abcdef' for char in normalized_snapshot):
        raise ValueError('invalid_app_fan_snapshot_id')
    now = str(observed_at or _now())
    record_type = 'tugao_complete_history_file'
    for streamer_id in sorted(normalized_ids):
        conn.execute(
            """
            INSERT INTO streamer_app_fan_identities (
                app_name,streamer_id,is_app_fan,identity_status,acquisition_source,
                source_record_type,first_source_record_id,last_source_record_id,
                first_confirmed_at,last_confirmed_at,evidence_version,created_at,updated_at
            ) VALUES (?, ?, 1, 'confirmed', 'tugao_app', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(app_name,streamer_id) DO UPDATE SET
                is_app_fan=1,identity_status='confirmed',acquisition_source='tugao_app',
                last_source_record_id=excluded.last_source_record_id,
                last_confirmed_at=excluded.last_confirmed_at,
                evidence_version=excluded.evidence_version,updated_at=excluded.updated_at
            """,
            (
                normalized_app, streamer_id, record_type, normalized_snapshot,
                normalized_snapshot, now, now, APP_FAN_EVIDENCE_VERSION, now, now,
            ),
        )
    conn.execute(
        """
        INSERT INTO streamer_app_fan_coverage (
            app_name,history_complete,complete_through,source_name,
            source_snapshot_id,source_id_count,confirmed_at,updated_at
        ) VALUES (?,1,?,'tugao_complete_history_file',?,?,?,?)
        ON CONFLICT(app_name) DO UPDATE SET
            history_complete=1,complete_through=excluded.complete_through,
            source_name=excluded.source_name,source_snapshot_id=excluded.source_snapshot_id,
            source_id_count=excluded.source_id_count,confirmed_at=excluded.confirmed_at,
            updated_at=excluded.updated_at
        """,
        (
            normalized_app, str(complete_through or '')[:10], normalized_snapshot,
            len(normalized_ids), now, now,
        ),
    )
    return {
        'app_name': normalized_app,
        'imported_ids': len(normalized_ids),
        'snapshot_id': normalized_snapshot,
        'history_complete': True,
        'complete_through': str(complete_through or '')[:10],
    }


def _confirmed_source_rows(
    conn: sqlite3.Connection,
    *,
    app_names: Optional[Iterable[str]] = None,
) -> Iterable[tuple[str, str, str, str, str]]:
    selected = {
        str(value or '').strip().lower()
        for value in (app_names or ('linky', 'timo'))
        if str(value or '').strip().lower() in {'linky', 'timo'}
    }
    if 'linky' in selected and _table_exists(conn, 'ops_intake_items'):
        yield from (
            ('linky', str(row[1]).strip(), 'ops_intake_items', str(row[0]), str(row[2]))
            for row in conn.execute(
                """
                SELECT item_id, parsed_account_id, created_at
                FROM ops_intake_items
                WHERE source = 'tugao_app'
                  AND lower(COALESCE(system_status, '')) = 'fully_success'
                  AND trim(COALESCE(parsed_account_id, '')) <> ''
                ORDER BY created_at, item_id
                """
            )
        )
    if 'timo' in selected and _table_exists(conn, 'ops_timo_intake_items'):
        yield from (
            ('timo', str(row[1]).strip(), 'ops_timo_intake_items', str(row[0]), str(row[2]))
            for row in conn.execute(
                """
                SELECT item_id, timo_id, COALESCE(timo_verified_at, created_at)
                FROM ops_timo_intake_items
                WHERE source = 'tugao_app'
                  AND lower(COALESCE(timo_verify_status, '')) IN ('success', 'verified')
                  AND trim(COALESCE(timo_id, '')) <> ''
                ORDER BY COALESCE(timo_verified_at, created_at), item_id
                """
            )
        )


def reconcile_streamer_app_fans(
    conn: sqlite3.Connection,
    *,
    observed_at: Optional[str] = None,
    app_names: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Upsert confirmed Tugao-origin streamer identities without guessing negatives."""
    ensure_streamer_app_fan_table(conn)
    now = str(observed_at or _now())
    source_rows = 0
    app_ids: Dict[str, set[str]] = {'linky': set(), 'timo': set()}
    for app_name, streamer_id, record_type, record_id, confirmed_at in _confirmed_source_rows(
        conn,
        app_names=app_names,
    ):
        source_rows += 1
        app_ids.setdefault(app_name, set()).add(streamer_id)
        effective_confirmed_at = str(confirmed_at or now)
        conn.execute(
            """
            INSERT INTO streamer_app_fan_identities (
                app_name, streamer_id, is_app_fan, identity_status,
                acquisition_source, source_record_type,
                first_source_record_id, last_source_record_id,
                first_confirmed_at, last_confirmed_at, evidence_version,
                created_at, updated_at
            ) VALUES (?, ?, 1, 'confirmed', 'tugao_app', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(app_name, streamer_id) DO UPDATE SET
                is_app_fan = 1,
                identity_status = 'confirmed',
                acquisition_source = 'tugao_app',
                source_record_type = excluded.source_record_type,
                last_source_record_id = excluded.last_source_record_id,
                last_confirmed_at = CASE
                    WHEN excluded.last_confirmed_at > streamer_app_fan_identities.last_confirmed_at
                    THEN excluded.last_confirmed_at
                    ELSE streamer_app_fan_identities.last_confirmed_at
                END,
                evidence_version = excluded.evidence_version,
                updated_at = excluded.updated_at
            """,
            (
                app_name, streamer_id, record_type, record_id, record_id,
                effective_confirmed_at, effective_confirmed_at,
                APP_FAN_EVIDENCE_VERSION, now, now,
            ),
        )
    return {
        'source_rows': source_rows,
        'app_fan_ids': {app: len(ids) for app, ids in app_ids.items()},
        'total_app_fan_ids': sum(len(ids) for ids in app_ids.values()),
        'evidence_version': APP_FAN_EVIDENCE_VERSION,
    }


def confirmed_app_fan_ids(
    conn: sqlite3.Connection,
    *,
    app_name: str,
    ensure_schema: bool = True,
) -> set[str]:
    if ensure_schema:
        ensure_streamer_app_fan_table(conn)
    elif not _table_exists(conn, 'streamer_app_fan_identities'):
        raise RuntimeError('streamer_app_fan_identity_schema_missing')
    normalized_app = str(app_name or '').strip().lower()
    return {
        str(row[0])
        for row in conn.execute(
            """
            SELECT streamer_id
            FROM streamer_app_fan_identities
            WHERE app_name = ? AND is_app_fan = 1 AND identity_status = 'confirmed'
            """,
            (normalized_app,),
        )
    }


def app_fan_history_complete(
    conn: sqlite3.Connection,
    *,
    app_name: str,
    ensure_schema: bool = True,
) -> bool:
    if ensure_schema:
        ensure_streamer_app_fan_table(conn)
    elif not _table_exists(conn, 'streamer_app_fan_coverage'):
        raise RuntimeError('streamer_app_fan_coverage_schema_missing')
    row = conn.execute(
        'SELECT history_complete FROM streamer_app_fan_coverage WHERE app_name=?',
        (str(app_name or '').strip().lower(),),
    ).fetchone()
    return bool(row and int(row[0] or 0) == 1)


def classify_app_fan_status(
    *,
    app_name: str,
    streamer_id: str,
    registered_date: str = '',
    confirmed_ids: Optional[Mapping[str, bool] | set[str]] = None,
    history_complete: bool = False,
) -> str:
    normalized_app = str(app_name or '').strip().lower()
    normalized_id = str(streamer_id or '').strip()
    if isinstance(confirmed_ids, set):
        confirmed = normalized_id in confirmed_ids
    else:
        confirmed = bool((confirmed_ids or {}).get(normalized_id))
    if confirmed:
        return 'app_fan'
    if history_complete:
        return 'non_app_fan'
    coverage_start = APP_FAN_COVERAGE_START.get(normalized_app, '')
    normalized_registered = str(registered_date or '')[:10]
    if not coverage_start or not normalized_registered or normalized_registered < coverage_start:
        return 'historical_unknown'
    return 'non_app_fan'
