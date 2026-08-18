from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence


TIMO_REVENUE_COLUMNS: tuple[str, ...] = (
    'guild_executor_key',
    'guild_name',
    'country',
    'stat_date_bj',
    'timo_id',
    'user_uuid',
    'nickname',
    'total_income',
    'qualified_revenue',
    'matching_income',
    'private_message_income',
    'private_gift_income',
    'call_income',
    'online_hours',
    'call_count',
    'quality_host',
    'quality_revenue',
    'provisional',
    'source_payload',
    'snapshot_at',
    'updated_at',
)

_MONEY_FIELDS: tuple[str, ...] = (
    'total_income',
    'qualified_revenue',
    'matching_income',
    'private_message_income',
    'private_gift_income',
    'call_income',
    'online_hours',
    'quality_revenue',
)

_ROW_JSON_SQL = """
json_object(
    'guild_executor_key', {alias}.guild_executor_key,
    'guild_name', {alias}.guild_name,
    'country', {alias}.country,
    'stat_date_bj', {alias}.stat_date_bj,
    'timo_id', {alias}.timo_id,
    'user_uuid', {alias}.user_uuid,
    'nickname', {alias}.nickname,
    'total_income', {alias}.total_income,
    'qualified_revenue', {alias}.qualified_revenue,
    'matching_income', {alias}.matching_income,
    'private_message_income', {alias}.private_message_income,
    'private_gift_income', {alias}.private_gift_income,
    'call_income', {alias}.call_income,
    'online_hours', {alias}.online_hours,
    'call_count', {alias}.call_count,
    'quality_host', {alias}.quality_host,
    'quality_revenue', {alias}.quality_revenue,
    'provisional', {alias}.provisional,
    'source_payload', {alias}.source_payload,
    'snapshot_at', {alias}.snapshot_at,
    'updated_at', {alias}.updated_at,
    'revision_version', {alias}.revision_version,
    'last_sync_id', {alias}.last_sync_id,
    'row_hash', {alias}.row_hash
)
"""


class TimoIncrementalSyncError(RuntimeError):
    def __init__(self, code: str, message: str = '', *, evidence: Optional[Dict[str, Any]] = None) -> None:
        self.code = str(code or 'timo_incremental_sync_failed')
        self.evidence = dict(evidence or {})
        super().__init__(message or self.code)


class TimoSyncLockBusy(TimoIncrementalSyncError):
    pass


class TimoCircuitOpen(TimoIncrementalSyncError):
    pass


@dataclass(frozen=True)
class TimoQualityGateResult:
    passed: bool
    error_code: str
    warnings: tuple[str, ...]
    metrics: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return {
            'passed': self.passed,
            'error_code': self.error_code,
            'warnings': list(self.warnings),
            'metrics': dict(self.metrics),
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value or '').replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decimal_text(value: Any) -> str:
    try:
        number = Decimal(str(value if value is not None else '0'))
    except (InvalidOperation, ValueError):
        raise TimoIncrementalSyncError('invalid_numeric_value', f'invalid numeric value: {value!r}')
    if not number.is_finite():
        raise TimoIncrementalSyncError('invalid_numeric_value', f'non-finite numeric value: {value!r}')
    normalized = number.quantize(Decimal('0.000001'))
    text = format(normalized, 'f').rstrip('0').rstrip('.')
    return text or '0'


def _canonical_revenue_row(
    row: Dict[str, Any],
    *,
    guild_executor_key: str,
    guild_name: str,
    country: str,
    stat_date_bj: str,
    provisional: bool,
    snapshot_at: str,
) -> Dict[str, Any]:
    timo_id = str(row.get('timo_id') or '').strip()
    if not timo_id:
        raise TimoIncrementalSyncError('invalid_streamer_id', 'timo_id is required')
    canonical: Dict[str, Any] = {
        'guild_executor_key': str(guild_executor_key or '').strip(),
        'guild_name': str(guild_name or '').strip(),
        'country': str(country or '').strip(),
        'stat_date_bj': str(stat_date_bj or '').strip(),
        'timo_id': timo_id,
        'user_uuid': str(row.get('user_uuid') or '').strip(),
        'nickname': str(row.get('nick_name') or row.get('nickname') or '').strip(),
        'call_count': int(row.get('call_count') or 0),
        'quality_host': 1 if row.get('quality_host') in (True, 1, '1', 'true', 'TRUE', 'yes', 'YES') else 0,
        'provisional': 1 if provisional else 0,
        'snapshot_at': snapshot_at,
        'updated_at': snapshot_at,
    }
    for field in _MONEY_FIELDS:
        canonical[field] = float(Decimal(_decimal_text(row.get(field) or 0)))
    source_payload = row.get('source_payload')
    if not isinstance(source_payload, str):
        source_payload = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
    canonical['source_payload'] = source_payload
    hash_payload = {
        field: (_decimal_text(canonical[field]) if field in _MONEY_FIELDS else canonical[field])
        for field in TIMO_REVENUE_COLUMNS
        if field not in {'snapshot_at', 'updated_at', 'source_payload'}
    }
    canonical['row_hash'] = hashlib.sha256(
        json.dumps(hash_payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()
    return canonical


def calculate_snapshot_checksum(rows: Sequence[Dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: (str(item['timo_id']), str(item['row_hash']))):
        digest.update(str(row['timo_id']).encode('utf-8'))
        digest.update(b'\x1f')
        digest.update(str(row['row_hash']).encode('ascii'))
        digest.update(b'\n')
    return digest.hexdigest()


def bootstrap_timo_legacy_watermarks(
    conn: sqlite3.Connection,
    *,
    max_scopes: int = 50,
) -> Dict[str, Any]:
    """Create an auditable incremental baseline without changing business values.

    This migration is intentionally separate from normal change capture: legacy
    rows predate the incremental pipeline, so their first canonical row hashes
    and watermarks are baseline evidence rather than revenue revisions.
    """
    ensure_timo_incremental_schema(conn)
    conn.commit()
    scopes = conn.execute(
        """
        SELECT revenue.guild_executor_key, revenue.guild_name, revenue.country,
               revenue.stat_date_bj
        FROM timo_external_revenue_daily AS revenue
        LEFT JOIN timo_sync_watermark AS watermark
          ON watermark.guild_executor_key=revenue.guild_executor_key
         AND watermark.stat_date_bj=revenue.stat_date_bj
        WHERE watermark.guild_executor_key IS NULL
        GROUP BY revenue.guild_executor_key, revenue.guild_name, revenue.country,
                 revenue.stat_date_bj
        ORDER BY revenue.stat_date_bj, revenue.guild_executor_key
        LIMIT ?
        """,
        (max(1, min(5000, int(max_scopes or 50))),),
    ).fetchall()
    processed: List[Dict[str, Any]] = []
    for scope in scopes:
        guild_executor_key = str(scope['guild_executor_key'])
        guild_name = str(scope['guild_name'] or '')
        country = str(scope['country'] or '')
        stat_date_bj = str(scope['stat_date_bj'])
        source_rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT {', '.join(TIMO_REVENUE_COLUMNS)}, revision_version
                FROM timo_external_revenue_daily
                WHERE guild_executor_key=? AND stat_date_bj=?
                ORDER BY timo_id
                """,
                (guild_executor_key, stat_date_bj),
            ).fetchall()
        ]
        if not source_rows:
            raise TimoIncrementalSyncError(
                'bootstrap_empty_scope',
                f'{guild_executor_key}:{stat_date_bj}',
            )
        provisional = any(bool(row.get('provisional')) for row in source_rows)
        canonical_rows = [
            _canonical_revenue_row(
                row,
                guild_executor_key=guild_executor_key,
                guild_name=guild_name,
                country=country,
                stat_date_bj=stat_date_bj,
                provisional=provisional,
                snapshot_at=str(row.get('snapshot_at') or row.get('updated_at') or utc_now()),
            )
            for row in source_rows
        ]
        checksum = calculate_snapshot_checksum(canonical_rows)
        sync_suffix = hashlib.sha256(
            f'{guild_executor_key}\x1f{stat_date_bj}'.encode('utf-8')
        ).hexdigest()[:16]
        sync_id = f'timo_legacy_bootstrap_{stat_date_bj.replace("-", "")}_{sync_suffix}'
        now = utc_now()
        total_income = sum(float(row.get('total_income') or 0) for row in canonical_rows)
        revision_version = max(
            1,
            max(int(row.get('revision_version') or 1) for row in source_rows),
        )
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.executemany(
                """
                UPDATE timo_external_revenue_daily
                SET row_hash=?,
                    revision_version=CASE
                        WHEN COALESCE(revision_version, 0)<1 THEN 1
                        ELSE revision_version
                    END,
                    last_sync_id=CASE
                        WHEN COALESCE(last_sync_id, '')='' THEN ?
                        ELSE last_sync_id
                    END
                WHERE guild_executor_key=? AND stat_date_bj=? AND timo_id=?
                """,
                [
                    (
                        str(row['row_hash']),
                        sync_id,
                        guild_executor_key,
                        stat_date_bj,
                        str(row['timo_id']),
                    )
                    for row in canonical_rows
                ],
            )
            conn.execute(
                """
                INSERT INTO timo_sync_run_log(
                    sync_id, parent_run_id, idempotency_key, guild_executor_key,
                    guild_name, country, stat_date_bj, data_status, start_time,
                    end_time, status, row_count, old_row_count, unchanged_count,
                    checksum, duration_ms, gate_evidence_json, diff_evidence_json,
                    created_at, updated_at
                ) VALUES (?, 'legacy-bootstrap', ?, ?, ?, ?, ?, ?, ?, ?, 'success',
                          ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    sync_id,
                    sync_id,
                    guild_executor_key,
                    guild_name,
                    country,
                    stat_date_bj,
                    'provisional' if provisional else 'complete',
                    now,
                    now,
                    len(canonical_rows),
                    len(canonical_rows),
                    len(canonical_rows),
                    checksum,
                    json.dumps(
                        {'passed': True, 'mode': 'legacy_baseline'},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    json.dumps(
                        {'mode': 'legacy_baseline', 'business_values_changed': False},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO timo_sync_watermark(
                    guild_executor_key, guild_name, country, stat_date_bj, checksum,
                    last_success_sync_id, last_success_time, row_count, total_income,
                    data_status, revision_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_executor_key,
                    guild_name,
                    country,
                    stat_date_bj,
                    checksum,
                    sync_id,
                    now,
                    len(canonical_rows),
                    total_income,
                    'provisional' if provisional else 'complete',
                    revision_version,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        processed.append(
            {
                'guild_executor_key': guild_executor_key,
                'stat_date_bj': stat_date_bj,
                'row_count': len(canonical_rows),
                'checksum': checksum,
                'sync_id': sync_id,
            }
        )
    remaining = int(
        conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM (
                SELECT revenue.guild_executor_key, revenue.stat_date_bj
                FROM timo_external_revenue_daily AS revenue
                LEFT JOIN timo_sync_watermark AS watermark
                  ON watermark.guild_executor_key=revenue.guild_executor_key
                 AND watermark.stat_date_bj=revenue.stat_date_bj
                WHERE watermark.guild_executor_key IS NULL
                GROUP BY revenue.guild_executor_key, revenue.stat_date_bj
            )
            """
        ).fetchone()['n']
        or 0
    )
    return {
        'ok': True,
        'status': 'partial' if remaining else ('success' if processed else 'no_op'),
        'processed_scope_count': len(processed),
        'processed_row_count': sum(int(item['row_count']) for item in processed),
        'remaining_scope_count': remaining,
        'scopes': processed,
    }


def ensure_timo_incremental_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS timo_revenue_staging (
            sync_id TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            stat_date_bj TEXT NOT NULL,
            timo_id TEXT NOT NULL,
            user_uuid TEXT NOT NULL DEFAULT '',
            nickname TEXT NOT NULL DEFAULT '',
            total_income REAL NOT NULL DEFAULT 0,
            qualified_revenue REAL NOT NULL DEFAULT 0,
            matching_income REAL NOT NULL DEFAULT 0,
            private_message_income REAL NOT NULL DEFAULT 0,
            private_gift_income REAL NOT NULL DEFAULT 0,
            call_income REAL NOT NULL DEFAULT 0,
            online_hours REAL NOT NULL DEFAULT 0,
            call_count INTEGER NOT NULL DEFAULT 0,
            quality_host INTEGER NOT NULL DEFAULT 0,
            quality_revenue REAL NOT NULL DEFAULT 0,
            provisional INTEGER NOT NULL DEFAULT 1,
            source_payload TEXT NOT NULL DEFAULT '{}',
            row_hash TEXT NOT NULL,
            snapshot_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            loaded_at TEXT NOT NULL,
            PRIMARY KEY (sync_id, guild_executor_key, stat_date_bj, timo_id)
        );
        CREATE INDEX IF NOT EXISTS idx_timo_revenue_staging_scope
            ON timo_revenue_staging(sync_id, guild_executor_key, stat_date_bj);

        CREATE TABLE IF NOT EXISTS timo_sync_run_log (
            sync_id TEXT PRIMARY KEY,
            parent_run_id TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '',
            stat_date_bj TEXT NOT NULL,
            data_status TEXT NOT NULL DEFAULT 'provisional',
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            error_code TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            row_count INTEGER NOT NULL DEFAULT 0,
            old_row_count INTEGER NOT NULL DEFAULT 0,
            inserted_count INTEGER NOT NULL DEFAULT 0,
            updated_count INTEGER NOT NULL DEFAULT 0,
            deleted_count INTEGER NOT NULL DEFAULT 0,
            unchanged_count INTEGER NOT NULL DEFAULT 0,
            checksum TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL DEFAULT 0,
            gate_evidence_json TEXT NOT NULL DEFAULT '{}',
            diff_evidence_json TEXT NOT NULL DEFAULT '{}',
            rollback_of_sync_id TEXT NOT NULL DEFAULT '',
            rolled_back_at TEXT NOT NULL DEFAULT '',
            retry_attempt INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_timo_sync_run_log_scope
            ON timo_sync_run_log(guild_executor_key, stat_date_bj, start_time DESC);
        CREATE INDEX IF NOT EXISTS idx_timo_sync_run_log_status
            ON timo_sync_run_log(status, next_retry_at);

        CREATE TABLE IF NOT EXISTS timo_sync_watermark (
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '',
            stat_date_bj TEXT NOT NULL,
            checksum TEXT NOT NULL,
            last_success_sync_id TEXT NOT NULL,
            last_success_time TEXT NOT NULL,
            row_count INTEGER NOT NULL DEFAULT 0,
            total_income REAL NOT NULL DEFAULT 0,
            data_status TEXT NOT NULL DEFAULT 'provisional',
            revision_version INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (guild_executor_key, stat_date_bj)
        );
        CREATE INDEX IF NOT EXISTS idx_timo_sync_watermark_country_date
            ON timo_sync_watermark(country, stat_date_bj);

        CREATE TABLE IF NOT EXISTS timo_live_revenue_aggregate (
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            country TEXT NOT NULL,
            stat_date_bj TEXT NOT NULL,
            active_1v1_hosts INTEGER NOT NULL DEFAULT 0,
            quality_hosts INTEGER NOT NULL DEFAULT 0,
            total_income REAL NOT NULL DEFAULT 0,
            qualified_revenue REAL NOT NULL DEFAULT 0,
            matching_income REAL NOT NULL DEFAULT 0,
            private_message_income REAL NOT NULL DEFAULT 0,
            private_gift_income REAL NOT NULL DEFAULT 0,
            call_income REAL NOT NULL DEFAULT 0,
            quality_revenue REAL NOT NULL DEFAULT 0,
            checksum TEXT NOT NULL,
            revision_version INTEGER NOT NULL DEFAULT 1,
            source_time_type INTEGER NOT NULL DEFAULT 0,
            last_success_run_id TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_executor_key, stat_date_bj)
        );
        CREATE INDEX IF NOT EXISTS idx_timo_live_revenue_aggregate_country_date
            ON timo_live_revenue_aggregate(country, stat_date_bj, fetched_at DESC);

        CREATE TABLE IF NOT EXISTS timo_live_revenue_aggregate_runs (
            run_id TEXT PRIMARY KEY,
            parent_run_id TEXT NOT NULL DEFAULT '',
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            country TEXT NOT NULL,
            stat_date_bj TEXT NOT NULL,
            status TEXT NOT NULL,
            error_code TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            checksum TEXT NOT NULL DEFAULT '',
            revision_version INTEGER NOT NULL DEFAULT 0,
            total_income REAL NOT NULL DEFAULT 0,
            source_time_type INTEGER NOT NULL DEFAULT 0,
            fetched_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_timo_live_revenue_aggregate_runs_scope
            ON timo_live_revenue_aggregate_runs(
                guild_executor_key, stat_date_bj, fetched_at DESC
            );
        CREATE INDEX IF NOT EXISTS idx_timo_live_revenue_aggregate_runs_status
            ON timo_live_revenue_aggregate_runs(status, fetched_at DESC);

        CREATE TABLE IF NOT EXISTS timo_revenue_changes (
            change_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sync_id TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '',
            streamer_id TEXT NOT NULL,
            stat_date_bj TEXT NOT NULL,
            change_type TEXT NOT NULL,
            old_income REAL,
            new_income REAL,
            old_row_json TEXT NOT NULL DEFAULT '',
            new_row_json TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(sync_id, guild_executor_key, stat_date_bj, streamer_id, change_type)
        );
        CREATE INDEX IF NOT EXISTS idx_timo_revenue_changes_scope
            ON timo_revenue_changes(guild_executor_key, stat_date_bj, created_at DESC);

        CREATE TABLE IF NOT EXISTS timo_sync_locks (
            lock_key TEXT PRIMARY KEY,
            owner_sync_id TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS timo_sync_circuit_breakers (
            guild_executor_key TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'closed',
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            opened_at TEXT NOT NULL DEFAULT '',
            cooldown_until TEXT NOT NULL DEFAULT '',
            last_error_code TEXT NOT NULL DEFAULT '',
            probe_sync_id TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE VIEW IF NOT EXISTS bi_timo_revenue_view AS
        SELECT
            revenue.guild_executor_key,
            revenue.guild_name,
            revenue.country,
            revenue.stat_date_bj,
            revenue.timo_id,
            revenue.user_uuid,
            revenue.nickname,
            streamers.joined_guild_at_bj AS joined_guild_at_bj,
            streamers.timo_registered_at_bj AS timo_registered_at_bj,
            revenue.total_income,
            revenue.qualified_revenue,
            revenue.matching_income,
            revenue.private_message_income,
            revenue.private_gift_income,
            revenue.call_income,
            revenue.online_hours,
            revenue.call_count,
            revenue.quality_host,
            revenue.quality_revenue,
            revenue.provisional,
            revenue.revision_version,
            revenue.last_sync_id,
            revenue.snapshot_at,
            revenue.updated_at
        FROM timo_external_revenue_daily AS revenue
        LEFT JOIN timo_external_streamers AS streamers
          ON streamers.guild_executor_key = revenue.guild_executor_key
         AND streamers.timo_id = revenue.timo_id;
        """
    )
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info('timo_external_revenue_daily')").fetchall()
    }
    for statement, column_name in (
        (
            "ALTER TABLE timo_external_revenue_daily ADD COLUMN revision_version INTEGER NOT NULL DEFAULT 1",
            'revision_version',
        ),
        (
            "ALTER TABLE timo_external_revenue_daily ADD COLUMN last_sync_id TEXT NOT NULL DEFAULT ''",
            'last_sync_id',
        ),
        (
            "ALTER TABLE timo_external_revenue_daily ADD COLUMN row_hash TEXT NOT NULL DEFAULT ''",
            'row_hash',
        ),
    ):
        if column_name not in columns:
            conn.execute(statement)
    staging_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info('timo_revenue_staging')").fetchall()
    }
    if 'updated_at' not in staging_columns:
        conn.execute(
            "ALTER TABLE timo_revenue_staging ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
        )


class TimoDbSyncLease(AbstractContextManager['TimoDbSyncLease']):
    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        *,
        lock_key: str,
        owner_sync_id: str,
        ttl_seconds: int = 600,
        auto_renew: bool = True,
    ) -> None:
        self._connect = connect
        self.lock_key = lock_key
        self.owner_sync_id = owner_sync_id
        self.ttl_seconds = max(30, int(ttl_seconds or 600))
        self.auto_renew = bool(auto_renew)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def acquire(self) -> 'TimoDbSyncLease':
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires_at = (now + timedelta(seconds=self.ttl_seconds)).isoformat()
        conn = self._connect()
        conn.execute('BEGIN IMMEDIATE')
        try:
            ensure_timo_incremental_schema(conn)
            conn.execute(
                "DELETE FROM timo_sync_locks WHERE lock_key=? AND expires_at<=?",
                (self.lock_key, now_text),
            )
            try:
                conn.execute(
                    """
                    INSERT INTO timo_sync_locks(lock_key, owner_sync_id, acquired_at, heartbeat_at, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (self.lock_key, self.owner_sync_id, now_text, now_text, expires_at),
                )
            except sqlite3.IntegrityError as exc:
                owner = conn.execute(
                    "SELECT owner_sync_id, expires_at FROM timo_sync_locks WHERE lock_key=?",
                    (self.lock_key,),
                ).fetchone()
                raise TimoSyncLockBusy(
                    'sync_lock_busy',
                    f'{self.lock_key} is owned by {owner[0] if owner else "unknown"}',
                    evidence={'lock_key': self.lock_key, 'owner_sync_id': owner[0] if owner else '', 'expires_at': owner[1] if owner else ''},
                ) from exc
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if self.auto_renew:
            self._thread = threading.Thread(target=self._renew_loop, name=f'timo-sync-lease-{self.owner_sync_id}', daemon=True)
            self._thread.start()
        return self

    def _renew_loop(self) -> None:
        interval = max(10.0, self.ttl_seconds / 3.0)
        while not self._stop.wait(interval):
            try:
                self.renew()
            except Exception:
                # The owner still fails closed at the materialization transaction.
                return

    def renew(self) -> None:
        now = datetime.now(timezone.utc)
        conn = self._connect()
        cursor = conn.execute(
            """
            UPDATE timo_sync_locks
            SET heartbeat_at=?, expires_at=?
            WHERE lock_key=? AND owner_sync_id=?
            """,
            (
                now.isoformat(),
                (now + timedelta(seconds=self.ttl_seconds)).isoformat(),
                self.lock_key,
                self.owner_sync_id,
            ),
        )
        conn.commit()
        if int(cursor.rowcount or 0) != 1:
            raise TimoSyncLockBusy('sync_lock_lost', f'lease lost: {self.lock_key}')

    def release(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        conn = self._connect()
        conn.execute(
            "DELETE FROM timo_sync_locks WHERE lock_key=? AND owner_sync_id=?",
            (self.lock_key, self.owner_sync_id),
        )
        conn.commit()

    def __enter__(self) -> 'TimoDbSyncLease':
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


def check_timo_circuit_breaker(
    conn: sqlite3.Connection,
    *,
    guild_executor_key: str,
    sync_id: str,
    now: Optional[datetime] = None,
) -> str:
    ensure_timo_incremental_schema(conn)
    current = now or datetime.now(timezone.utc)
    row = conn.execute(
        """
        SELECT state, cooldown_until, probe_sync_id
        FROM timo_sync_circuit_breakers
        WHERE guild_executor_key=?
        """,
        (guild_executor_key,),
    ).fetchone()
    if not row or str(row['state'] or 'closed') == 'closed':
        return 'closed'
    state = str(row['state'] or '').lower()
    cooldown_until = _parse_utc(str(row['cooldown_until'])) if str(row['cooldown_until'] or '') else current
    if state == 'open' and current < cooldown_until:
        raise TimoCircuitOpen(
            'circuit_open',
            f'circuit open until {cooldown_until.isoformat()}',
            evidence={'guild_executor_key': guild_executor_key, 'cooldown_until': cooldown_until.isoformat()},
        )
    cursor = conn.execute(
        """
        UPDATE timo_sync_circuit_breakers
        SET state='half_open', probe_sync_id=?, updated_at=?
        WHERE guild_executor_key=? AND state='open'
        """,
        (sync_id, current.isoformat(), guild_executor_key),
    )
    if int(cursor.rowcount or 0) == 1:
        conn.commit()
        return 'half_open'
    if state == 'half_open' and str(row['probe_sync_id'] or '') != sync_id:
        raise TimoCircuitOpen('circuit_half_open_probe_busy', 'another half-open probe is running')
    return state


def record_timo_circuit_success(
    conn: sqlite3.Connection,
    *,
    guild_executor_key: str,
    now_text: Optional[str] = None,
) -> None:
    timestamp = now_text or utc_now()
    conn.execute(
        """
        INSERT INTO timo_sync_circuit_breakers(
            guild_executor_key, state, consecutive_failures, opened_at, cooldown_until,
            last_error_code, probe_sync_id, updated_at
        ) VALUES (?, 'closed', 0, '', '', '', '', ?)
        ON CONFLICT(guild_executor_key) DO UPDATE SET
            state='closed',
            consecutive_failures=0,
            opened_at='',
            cooldown_until='',
            last_error_code='',
            probe_sync_id='',
            updated_at=excluded.updated_at
        """,
        (guild_executor_key, timestamp),
    )
    conn.commit()


def record_timo_circuit_failure(
    conn: sqlite3.Connection,
    *,
    guild_executor_key: str,
    error_code: str,
    failure_threshold: int = 3,
    cooldown_seconds: int = 900,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    ensure_timo_incremental_schema(conn)
    row = conn.execute(
        "SELECT consecutive_failures FROM timo_sync_circuit_breakers WHERE guild_executor_key=?",
        (guild_executor_key,),
    ).fetchone()
    failures = int(row['consecutive_failures'] or 0) + 1 if row else 1
    state = 'open' if failures >= max(1, int(failure_threshold or 3)) else 'closed'
    opened_at = current.isoformat() if state == 'open' else ''
    cooldown_until = (
        current + timedelta(seconds=max(60, int(cooldown_seconds or 900)))
    ).isoformat() if state == 'open' else ''
    conn.execute(
        """
        INSERT INTO timo_sync_circuit_breakers(
            guild_executor_key, state, consecutive_failures, opened_at, cooldown_until,
            last_error_code, probe_sync_id, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, '', ?)
        ON CONFLICT(guild_executor_key) DO UPDATE SET
            state=excluded.state,
            consecutive_failures=excluded.consecutive_failures,
            opened_at=excluded.opened_at,
            cooldown_until=excluded.cooldown_until,
            last_error_code=excluded.last_error_code,
            probe_sync_id='',
            updated_at=excluded.updated_at
        """,
        (
            guild_executor_key,
            state,
            failures,
            opened_at,
            cooldown_until,
            str(error_code or '')[:120],
            current.isoformat(),
        ),
    )
    conn.commit()
    return {'state': state, 'consecutive_failures': failures, 'cooldown_until': cooldown_until}


def _insert_sync_run_start(
    conn: sqlite3.Connection,
    *,
    sync_id: str,
    parent_run_id: str,
    idempotency_key: str,
    guild_executor_key: str,
    guild_name: str,
    country: str,
    stat_date_bj: str,
    provisional: bool,
    start_time: str,
) -> Dict[str, Any]:
    existing = conn.execute(
        "SELECT * FROM timo_sync_run_log WHERE sync_id=? OR idempotency_key=?",
        (sync_id, idempotency_key),
    ).fetchone()
    if existing:
        return dict(existing)
    conn.execute(
        """
        INSERT INTO timo_sync_run_log(
            sync_id, parent_run_id, idempotency_key, guild_executor_key, guild_name, country,
            stat_date_bj, data_status, start_time, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
        """,
        (
            sync_id,
            parent_run_id,
            idempotency_key,
            guild_executor_key,
            guild_name,
            country,
            stat_date_bj,
            'provisional' if provisional else 'complete',
            start_time,
            start_time,
            start_time,
        ),
    )
    conn.commit()
    return {}


def _quality_gate(
    conn: sqlite3.Connection,
    *,
    sync_id: str,
    guild_executor_key: str,
    stat_date_bj: str,
    provisional: bool,
    min_row_ratio: float,
    min_income_ratio: float,
) -> TimoQualityGateResult:
    staged = conn.execute(
        """
        SELECT COUNT(*) AS row_count,
               COUNT(DISTINCT timo_id) AS distinct_count,
               COALESCE(SUM(total_income), 0) AS total_income,
               SUM(CASE WHEN total_income < 0 THEN 1 ELSE 0 END) AS negative_income_count
        FROM timo_revenue_staging
        WHERE sync_id=? AND guild_executor_key=? AND stat_date_bj=?
        """,
        (sync_id, guild_executor_key, stat_date_bj),
    ).fetchone()
    current = conn.execute(
        """
        SELECT COUNT(*) AS row_count, COALESCE(SUM(total_income), 0) AS total_income,
               SUM(CASE WHEN provisional=0 THEN 1 ELSE 0 END) AS complete_count,
               SUM(CASE WHEN provisional=1 THEN 1 ELSE 0 END) AS provisional_count
        FROM timo_external_revenue_daily
        WHERE guild_executor_key=? AND stat_date_bj=?
        """,
        (guild_executor_key, stat_date_bj),
    ).fetchone()
    missing_nonzero = conn.execute(
        """
        SELECT COUNT(*) AS row_count
        FROM timo_external_revenue_daily AS current
        LEFT JOIN timo_revenue_staging AS staged
          ON staged.sync_id=?
         AND staged.guild_executor_key=current.guild_executor_key
         AND staged.stat_date_bj=current.stat_date_bj
         AND staged.timo_id=current.timo_id
        WHERE current.guild_executor_key=?
          AND current.stat_date_bj=?
          AND staged.timo_id IS NULL
          AND ABS(current.total_income) > 0.000001
        """,
        (sync_id, guild_executor_key, stat_date_bj),
    ).fetchone()
    new_rows = int(staged['row_count'] or 0)
    old_rows = int(current['row_count'] or 0)
    new_income = float(staged['total_income'] or 0)
    old_income = float(current['total_income'] or 0)
    complete_count = int(current['complete_count'] or 0)
    provisional_count = int(current['provisional_count'] or 0)
    if not old_rows:
        old_data_status = 'empty'
    elif complete_count == old_rows:
        old_data_status = 'complete'
    elif provisional_count == old_rows:
        old_data_status = 'provisional'
    else:
        old_data_status = 'mixed'
    new_data_status = 'provisional' if provisional else 'complete'
    comparable_population = old_data_status == new_data_status
    historical_complete = conn.execute(
        """
        SELECT stat_date_bj, COUNT(*) AS row_count,
               COALESCE(SUM(total_income), 0) AS total_income
        FROM timo_external_revenue_daily
        WHERE guild_executor_key=?
          AND stat_date_bj < ?
          AND stat_date_bj >= date(?, '-14 days')
          AND provisional=0
        GROUP BY stat_date_bj
        ORDER BY stat_date_bj DESC
        LIMIT 7
        """,
        (guild_executor_key, stat_date_bj, stat_date_bj),
    ).fetchall()
    historical_row_counts = sorted(int(row['row_count'] or 0) for row in historical_complete)
    historical_income_totals = sorted(float(row['total_income'] or 0) for row in historical_complete)

    def _median(values: Sequence[float]) -> Optional[float]:
        if not values:
            return None
        midpoint = len(values) // 2
        if len(values) % 2:
            return float(values[midpoint])
        return (float(values[midpoint - 1]) + float(values[midpoint])) / 2

    historical_row_median = _median(historical_row_counts)
    historical_income_median = _median(historical_income_totals)
    historical_guard = not provisional and len(historical_complete) >= 2
    metrics = {
        'new_row_count': new_rows,
        'old_row_count': old_rows,
        'new_total_income': new_income,
        'old_total_income': old_income,
        'row_ratio': (new_rows / old_rows) if old_rows else None,
        'income_ratio': (new_income / old_income) if old_income else None,
        'missing_nonzero_rows': int(missing_nonzero['row_count'] or 0),
        'old_data_status': old_data_status,
        'new_data_status': new_data_status,
        'comparable_population': comparable_population,
        'historical_complete_days': len(historical_complete),
        'historical_row_median': historical_row_median,
        'historical_income_median': historical_income_median,
    }
    watermark = conn.execute(
        """
        SELECT data_status
        FROM timo_sync_watermark
        WHERE guild_executor_key=? AND stat_date_bj=?
        """,
        (guild_executor_key, stat_date_bj),
    ).fetchone()
    # Existing production data predates the watermark table. Treat a non-trivial
    # legacy scope as a trusted bootstrap baseline so the first upgraded run
    # cannot replace thousands of rows with a small non-empty partial export.
    baseline_guard = bool(watermark) or old_rows >= 20
    error_code = ''
    if new_rows == 0:
        error_code = 'quality_gate_empty_snapshot'
    elif provisional and abs(new_income) <= 0.000001:
        error_code = 'quality_gate_provisional_zero_income_not_ready'
    elif int(staged['distinct_count'] or 0) != new_rows:
        error_code = 'quality_gate_duplicate_streamer'
    elif int(staged['negative_income_count'] or 0) > 0:
        error_code = 'quality_gate_negative_income'
    elif old_data_status == 'mixed':
        error_code = 'quality_gate_mixed_current_status'
    elif watermark and str(watermark['data_status'] or '') == 'complete' and provisional:
        error_code = 'quality_gate_complete_downgrade'
    elif historical_guard and historical_row_median and new_rows < historical_row_median * min_row_ratio:
        error_code = 'quality_gate_historical_row_count_drop'
    elif historical_guard and historical_income_median and new_income < historical_income_median * min_income_ratio:
        error_code = 'quality_gate_historical_income_drop'
    elif baseline_guard and comparable_population and new_rows < old_rows * min_row_ratio:
        error_code = 'quality_gate_row_count_drop'
    elif baseline_guard and comparable_population and old_income > 0 and new_income < old_income * min_income_ratio:
        error_code = 'quality_gate_income_drop'
    elif baseline_guard and int(missing_nonzero['row_count'] or 0) > 0:
        error_code = 'quality_gate_missing_nonzero_streamer'
    warnings: List[str] = []
    if old_rows > 0 and new_rows > old_rows * 1.5:
        warnings.append('row_count_growth_requires_observation')
    if old_income > 0 and new_income > old_income * 5:
        warnings.append('income_growth_requires_observation')
    return TimoQualityGateResult(
        passed=not error_code,
        error_code=error_code,
        warnings=tuple(warnings),
        metrics=metrics,
    )


def materialize_timo_revenue_snapshot(
    connect: Callable[[], sqlite3.Connection],
    *,
    sync_id: str,
    parent_run_id: str,
    guild_executor_key: str,
    guild_name: str,
    country: str,
    stat_date_bj: str,
    provisional: bool,
    revenue_rows: Iterable[Dict[str, Any]],
    snapshot_at: Optional[str] = None,
    idempotency_key: str = '',
    min_row_ratio: float = 0.5,
    min_income_ratio: float = 0.5,
    source_provenance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    started_monotonic = time.monotonic()
    start_time = snapshot_at or utc_now()
    safe_idempotency_key = str(idempotency_key or sync_id).strip()
    conn = connect()
    ensure_timo_incremental_schema(conn)
    existing = _insert_sync_run_start(
        conn,
        sync_id=sync_id,
        parent_run_id=parent_run_id,
        idempotency_key=safe_idempotency_key,
        guild_executor_key=guild_executor_key,
        guild_name=guild_name,
        country=country,
        stat_date_bj=stat_date_bj,
        provisional=provisional,
        start_time=start_time,
    )
    if existing and str(existing.get('status') or '') in {
        'success',
        'no_op',
        'rolled_back',
        'quality_failed',
        'failed',
    }:
        result = dict(existing)
        result['idempotent_replay'] = True
        result['ok'] = str(existing.get('status') or '') in {'success', 'no_op'}
        return result
    canonical_rows = [
        _canonical_revenue_row(
            dict(row),
            guild_executor_key=guild_executor_key,
            guild_name=guild_name,
            country=country,
            stat_date_bj=stat_date_bj,
            provisional=provisional,
            snapshot_at=start_time,
        )
        for row in revenue_rows
    ]
    seen: set[str] = set()
    duplicate_ids: List[str] = []
    for row in canonical_rows:
        timo_id = str(row['timo_id'])
        if timo_id in seen:
            duplicate_ids.append(timo_id)
        seen.add(timo_id)
    if duplicate_ids:
        error = TimoIncrementalSyncError(
            'quality_gate_duplicate_streamer',
            f'duplicate streamer ids: {duplicate_ids[:5]}',
        )
        _finish_sync_failure(conn, sync_id=sync_id, error=error, started_monotonic=started_monotonic)
        raise error
    checksum = calculate_snapshot_checksum(canonical_rows)
    expected_data_status = 'provisional' if provisional else 'complete'
    preflight_watermark = conn.execute(
        """
        SELECT checksum, row_count, revision_version, data_status, last_success_sync_id,
               total_income
        FROM timo_sync_watermark
        WHERE guild_executor_key=? AND stat_date_bj=?
        """,
        (guild_executor_key, stat_date_bj),
    ).fetchone()
    preflight_fact_ready = False
    if preflight_watermark:
        preflight_fact = conn.execute(
            """
            SELECT COUNT(*) AS row_count, COALESCE(SUM(total_income), 0) AS total_income,
                   MIN(revision_version) AS min_revision, MAX(revision_version) AS max_revision,
                   COUNT(DISTINCT last_sync_id) AS sync_id_count,
                   MIN(last_sync_id) AS fact_sync_id
            FROM timo_external_revenue_daily
            WHERE guild_executor_key=? AND stat_date_bj=?
            """,
            (guild_executor_key, stat_date_bj),
        ).fetchone()
        preflight_fact_rows = conn.execute(
            """
            SELECT timo_id, row_hash
            FROM timo_external_revenue_daily
            WHERE guild_executor_key=? AND stat_date_bj=?
            ORDER BY timo_id
            """,
            (guild_executor_key, stat_date_bj),
        ).fetchall()
        preflight_digest = hashlib.sha256()
        for fact_row in preflight_fact_rows:
            preflight_digest.update(str(fact_row['timo_id']).encode('utf-8'))
            preflight_digest.update(b'\x1f')
            preflight_digest.update(str(fact_row['row_hash']).encode('ascii'))
            preflight_digest.update(b'\n')
        preflight_fact_ready = bool(
            int(preflight_fact['row_count'] or 0) == int(preflight_watermark['row_count'] or 0)
            and abs(float(preflight_fact['total_income'] or 0) - float(preflight_watermark['total_income'] or 0)) <= 0.000001
            and preflight_digest.hexdigest() == str(preflight_watermark['checksum'] or '')
            and int(preflight_fact['min_revision'] or 0) == int(preflight_watermark['revision_version'] or 0)
            and int(preflight_fact['max_revision'] or 0) == int(preflight_watermark['revision_version'] or 0)
            and int(preflight_fact['sync_id_count'] or 0) == 1
            and str(preflight_fact['fact_sync_id'] or '') == str(preflight_watermark['last_success_sync_id'] or '')
        )
    # The checksum is calculated from the complete canonical source snapshot.
    # If it matches the last accepted watermark with the same completeness
    # state, no staging/diff transaction is necessary.  Keeping the frequent
    # 15-minute no-op path to two tiny run-log transactions prevents it from
    # contending with unrelated online writes on the shared 13 GiB database.
    if (
        preflight_watermark
        and preflight_fact_ready
        and str(preflight_watermark['checksum'] or '') == checksum
        and str(preflight_watermark['data_status'] or '') == expected_data_status
        and not (
            provisional
            and all(abs(float(row.get('total_income') or 0)) <= 0.000001 for row in canonical_rows)
        )
    ):
        end_time = utc_now()
        preflight_evidence = {
            'passed': True,
            'error_code': '',
            'warnings': [],
            'metrics': {'mode': 'accepted_watermark_checksum_match'},
            'source_provenance': dict(source_provenance or {}),
        }
        conn.execute(
            """
            UPDATE timo_sync_run_log
            SET status='no_op', row_count=?, old_row_count=?, unchanged_count=?,
                checksum=?, gate_evidence_json=?, end_time=?, duration_ms=?, updated_at=?
            WHERE sync_id=?
            """,
            (
                len(canonical_rows),
                int(preflight_watermark['row_count'] or 0),
                len(canonical_rows),
                checksum,
                json.dumps(preflight_evidence, ensure_ascii=False, sort_keys=True),
                end_time,
                int((time.monotonic() - started_monotonic) * 1000),
                end_time,
                sync_id,
            ),
        )
        conn.commit()
        return {
            'ok': True,
            'sync_id': sync_id,
            'status': 'no_op',
            'checksum': checksum,
            'row_count': len(canonical_rows),
            'inserted_count': 0,
            'updated_count': 0,
            'deleted_count': 0,
            'unchanged_count': len(canonical_rows),
            'revision_version': int(preflight_watermark['revision_version'] or 1),
            'quality_gate': preflight_evidence,
        }
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("DELETE FROM timo_revenue_staging WHERE sync_id=?", (sync_id,))
        conn.executemany(
            """
            INSERT INTO timo_revenue_staging(
                sync_id, guild_executor_key, guild_name, country, stat_date_bj, timo_id,
                user_uuid, nickname, total_income, qualified_revenue, matching_income,
                private_message_income, private_gift_income, call_income, online_hours,
                call_count, quality_host, quality_revenue, provisional, source_payload,
                row_hash, snapshot_at, updated_at, loaded_at
            ) VALUES (
                :sync_id, :guild_executor_key, :guild_name, :country, :stat_date_bj, :timo_id,
                :user_uuid, :nickname, :total_income, :qualified_revenue, :matching_income,
                :private_message_income, :private_gift_income, :call_income, :online_hours,
                :call_count, :quality_host, :quality_revenue, :provisional, :source_payload,
                :row_hash, :snapshot_at, :updated_at, :loaded_at
            )
            """,
            [
                {
                    **row,
                    'sync_id': sync_id,
                    'loaded_at': start_time,
                }
                for row in canonical_rows
            ],
        )
        gate = _quality_gate(
            conn,
            sync_id=sync_id,
            guild_executor_key=guild_executor_key,
            stat_date_bj=stat_date_bj,
            provisional=provisional,
            min_row_ratio=max(0.0, min(1.0, float(min_row_ratio))),
            min_income_ratio=max(0.0, min(1.0, float(min_income_ratio))),
        )
        gate_evidence = {
            **gate.as_dict(),
            'source_provenance': dict(source_provenance or {}),
        }
        if not gate.passed:
            conn.execute(
                """
                UPDATE timo_sync_run_log
                SET status='quality_failed', error_code=?, error=?, row_count=?,
                    old_row_count=?, checksum=?, gate_evidence_json=?,
                    end_time=?, duration_ms=?, updated_at=?
                WHERE sync_id=?
                """,
                (
                    gate.error_code,
                    gate.error_code,
                    int(gate.metrics['new_row_count']),
                    int(gate.metrics['old_row_count']),
                    checksum,
                    json.dumps(gate_evidence, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                    int((time.monotonic() - started_monotonic) * 1000),
                    utc_now(),
                    sync_id,
                ),
            )
            conn.execute("DELETE FROM timo_revenue_staging WHERE sync_id=?", (sync_id,))
            conn.commit()
            raise TimoIncrementalSyncError(gate.error_code, gate.error_code, evidence=gate_evidence)
        watermark = conn.execute(
            """
            SELECT checksum, row_count, revision_version, data_status, last_success_sync_id
            FROM timo_sync_watermark
            WHERE guild_executor_key=? AND stat_date_bj=?
            """,
            (guild_executor_key, stat_date_bj),
        ).fetchone()
        if (
            watermark
            and str(watermark['checksum'] or '') == checksum
            and str(watermark['data_status'] or '') == ('provisional' if provisional else 'complete')
        ):
            end_time = utc_now()
            conn.execute(
                """
                UPDATE timo_external_revenue_daily
                SET revision_version=?, last_sync_id=?
                WHERE guild_executor_key=? AND stat_date_bj=?
                """,
                (
                    int(watermark['revision_version'] or 1),
                    str(watermark['last_success_sync_id'] or ''),
                    guild_executor_key,
                    stat_date_bj,
                ),
            )
            conn.execute(
                """
                UPDATE timo_external_revenue_daily
                SET row_hash=(
                    SELECT staged.row_hash
                    FROM timo_revenue_staging AS staged
                    WHERE staged.sync_id=?
                      AND staged.guild_executor_key=timo_external_revenue_daily.guild_executor_key
                      AND staged.stat_date_bj=timo_external_revenue_daily.stat_date_bj
                      AND staged.timo_id=timo_external_revenue_daily.timo_id
                )
                WHERE guild_executor_key=? AND stat_date_bj=?
                  AND EXISTS (
                    SELECT 1
                    FROM timo_revenue_staging AS staged
                    WHERE staged.sync_id=?
                      AND staged.guild_executor_key=timo_external_revenue_daily.guild_executor_key
                      AND staged.stat_date_bj=timo_external_revenue_daily.stat_date_bj
                      AND staged.timo_id=timo_external_revenue_daily.timo_id
                  )
                """,
                (
                    sync_id,
                    guild_executor_key,
                    stat_date_bj,
                    sync_id,
                ),
            )
            conn.execute(
                """
                UPDATE timo_sync_run_log
                SET status='no_op', row_count=?, old_row_count=?, unchanged_count=?,
                    checksum=?, gate_evidence_json=?, end_time=?, duration_ms=?, updated_at=?
                WHERE sync_id=?
                """,
                (
                    len(canonical_rows),
                    int(watermark['row_count'] or 0),
                    len(canonical_rows),
                    checksum,
                    json.dumps(gate_evidence, ensure_ascii=False, sort_keys=True),
                    end_time,
                    int((time.monotonic() - started_monotonic) * 1000),
                    end_time,
                    sync_id,
                ),
            )
            conn.execute("DELETE FROM timo_revenue_staging WHERE sync_id=?", (sync_id,))
            conn.commit()
            return {
                'ok': True,
                'sync_id': sync_id,
                'status': 'no_op',
                'checksum': checksum,
                'row_count': len(canonical_rows),
                'inserted_count': 0,
                'updated_count': 0,
                'deleted_count': 0,
                'unchanged_count': len(canonical_rows),
                'revision_version': int(watermark['revision_version'] or 1),
                'quality_gate': gate_evidence,
            }
        _record_sql_diff(
            conn,
            sync_id=sync_id,
            guild_executor_key=guild_executor_key,
            stat_date_bj=stat_date_bj,
            created_at=start_time,
        )
        counts = _sql_diff_counts(
            conn,
            sync_id=sync_id,
            guild_executor_key=guild_executor_key,
            stat_date_bj=stat_date_bj,
        )
        revision_version = int(watermark['revision_version'] or 0) + 1 if watermark else 1
        _apply_sql_diff(
            conn,
            sync_id=sync_id,
            guild_executor_key=guild_executor_key,
            stat_date_bj=stat_date_bj,
            revision_version=revision_version,
        )
        total_income = float(sum(Decimal(_decimal_text(row['total_income'])) for row in canonical_rows))
        end_time = utc_now()
        conn.execute(
            """
            INSERT INTO timo_sync_watermark(
                guild_executor_key, guild_name, country, stat_date_bj, checksum,
                last_success_sync_id, last_success_time, row_count, total_income,
                data_status, revision_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_executor_key, stat_date_bj) DO UPDATE SET
                guild_name=excluded.guild_name,
                country=excluded.country,
                checksum=excluded.checksum,
                last_success_sync_id=excluded.last_success_sync_id,
                last_success_time=excluded.last_success_time,
                row_count=excluded.row_count,
                total_income=excluded.total_income,
                data_status=excluded.data_status,
                revision_version=excluded.revision_version
            """,
            (
                guild_executor_key,
                guild_name,
                country,
                stat_date_bj,
                checksum,
                sync_id,
                end_time,
                len(canonical_rows),
                total_income,
                'provisional' if provisional else 'complete',
                revision_version,
            ),
        )
        diff_evidence = {
            **counts,
            'sql_diff': True,
            'revision_version': revision_version,
            'checksum': checksum,
        }
        conn.execute(
            """
            UPDATE timo_sync_run_log
            SET status='success', row_count=?, old_row_count=?, inserted_count=?,
                updated_count=?, deleted_count=?, unchanged_count=?, checksum=?,
                gate_evidence_json=?, diff_evidence_json=?, end_time=?, duration_ms=?, updated_at=?
            WHERE sync_id=?
            """,
            (
                len(canonical_rows),
                int(gate.metrics['old_row_count']),
                counts['inserted_count'],
                counts['updated_count'],
                counts['deleted_count'],
                counts['unchanged_count'],
                checksum,
                json.dumps(gate_evidence, ensure_ascii=False, sort_keys=True),
                json.dumps(diff_evidence, ensure_ascii=False, sort_keys=True),
                end_time,
                int((time.monotonic() - started_monotonic) * 1000),
                end_time,
                sync_id,
            ),
        )
        conn.execute("DELETE FROM timo_revenue_staging WHERE sync_id=?", (sync_id,))
        conn.commit()
        return {
            'ok': True,
            'sync_id': sync_id,
            'status': 'success',
            'checksum': checksum,
            'row_count': len(canonical_rows),
            **counts,
            'revision_version': revision_version,
            'quality_gate': gate_evidence,
        }
    except TimoIncrementalSyncError:
        if conn.in_transaction:
            conn.rollback()
        raise
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        raw_error = str(exc)
        resource_conflict = (
            'sqlite_write_window_timeout:' in raw_error
            or 'database is locked' in raw_error.lower()
            or 'database table is locked' in raw_error.lower()
        )
        error = TimoIncrementalSyncError(
            'resource_write_conflict' if resource_conflict else 'materialization_failed',
            raw_error,
            evidence={'retryable': True, 'resource': 'automation_db_writer'}
            if resource_conflict
            else None,
        )
        _finish_sync_failure(conn, sync_id=sync_id, error=error, started_monotonic=started_monotonic)
        raise error from exc


def _finish_sync_failure(
    conn: sqlite3.Connection,
    *,
    sync_id: str,
    error: TimoIncrementalSyncError,
    started_monotonic: float,
) -> None:
    end_time = utc_now()
    conn.execute(
        """
        UPDATE timo_sync_run_log
        SET status='failed', error_code=?, error=?, end_time=?, duration_ms=?, updated_at=?
        WHERE sync_id=?
        """,
        (
            error.code,
            str(error)[:1000],
            end_time,
            int((time.monotonic() - started_monotonic) * 1000),
            end_time,
            sync_id,
        ),
    )
    conn.commit()


def record_timo_sync_attempt_failure(
    conn: sqlite3.Connection,
    *,
    sync_id: str,
    parent_run_id: str,
    guild_executor_key: str,
    guild_name: str,
    country: str,
    stat_date_bj: str,
    provisional: bool,
    error_code: str,
    error: str,
    retry_attempt: int = 1,
    persistent_retry: bool = False,
) -> Dict[str, Any]:
    ensure_timo_incremental_schema(conn)
    started = utc_now()
    _insert_sync_run_start(
        conn,
        sync_id=sync_id,
        parent_run_id=parent_run_id,
        idempotency_key=sync_id,
        guild_executor_key=guild_executor_key,
        guild_name=guild_name,
        country=country,
        stat_date_bj=stat_date_bj,
        provisional=provisional,
        start_time=started,
    )
    normalized_attempt = max(1, int(retry_attempt or 1))
    next_retry_at = (
        datetime.now(timezone.utc)
        + timedelta(minutes=next_retry_delay_minutes(normalized_attempt))
    ).isoformat() if persistent_retry or normalized_attempt <= 4 else ''
    conn.execute(
        """
        UPDATE timo_sync_run_log
        SET status='failed', error_code=?, error=?, end_time=?, duration_ms=0,
            retry_attempt=?, next_retry_at=?, updated_at=?
        WHERE sync_id=?
        """,
        (
            str(error_code or 'upstream_failed')[:120],
            str(error or '')[:1000],
            started,
            normalized_attempt,
            next_retry_at,
            started,
            sync_id,
        ),
    )
    conn.commit()
    return {
        'sync_id': sync_id,
        'status': 'failed',
        'error_code': str(error_code or 'upstream_failed')[:120],
        'next_retry_at': next_retry_at,
    }


def _sql_diff_counts(
    conn: sqlite3.Connection,
    *,
    sync_id: str,
    guild_executor_key: str,
    stat_date_bj: str,
) -> Dict[str, int]:
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN current.timo_id IS NULL THEN 1 ELSE 0 END) AS inserted_count,
            SUM(CASE WHEN current.timo_id IS NOT NULL AND current.row_hash<>staged.row_hash THEN 1 ELSE 0 END) AS updated_count,
            SUM(CASE WHEN current.timo_id IS NOT NULL AND current.row_hash=staged.row_hash THEN 1 ELSE 0 END) AS unchanged_count
        FROM timo_revenue_staging AS staged
        LEFT JOIN timo_external_revenue_daily AS current
          ON current.guild_executor_key=staged.guild_executor_key
         AND current.stat_date_bj=staged.stat_date_bj
         AND current.timo_id=staged.timo_id
        WHERE staged.sync_id=? AND staged.guild_executor_key=? AND staged.stat_date_bj=?
        """,
        (sync_id, guild_executor_key, stat_date_bj),
    ).fetchone()
    deleted = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM timo_external_revenue_daily AS current
        LEFT JOIN timo_revenue_staging AS staged
          ON staged.sync_id=?
         AND staged.guild_executor_key=current.guild_executor_key
         AND staged.stat_date_bj=current.stat_date_bj
         AND staged.timo_id=current.timo_id
        WHERE current.guild_executor_key=? AND current.stat_date_bj=?
          AND staged.timo_id IS NULL
        """,
        (sync_id, guild_executor_key, stat_date_bj),
    ).fetchone()
    return {
        'inserted_count': int(row['inserted_count'] or 0),
        'updated_count': int(row['updated_count'] or 0),
        'deleted_count': int(deleted['n'] or 0),
        'unchanged_count': int(row['unchanged_count'] or 0),
    }


def _record_sql_diff(
    conn: sqlite3.Connection,
    *,
    sync_id: str,
    guild_executor_key: str,
    stat_date_bj: str,
    created_at: str,
) -> None:
    current_json = _ROW_JSON_SQL.format(alias='current')
    staged_json = _ROW_JSON_SQL.replace(
        "'revision_version', {alias}.revision_version,\n    'last_sync_id', {alias}.last_sync_id,\n    'row_hash', {alias}.row_hash",
        "'revision_version', 0,\n    'last_sync_id', '',\n    'row_hash', {alias}.row_hash",
    ).format(alias='staged')
    conn.execute(
        f"""
        INSERT OR IGNORE INTO timo_revenue_changes(
            sync_id, guild_executor_key, guild_name, country, streamer_id, stat_date_bj,
            change_type, old_income, new_income, old_row_json, new_row_json, created_at
        )
        SELECT
            ?, staged.guild_executor_key, staged.guild_name, staged.country, staged.timo_id,
            staged.stat_date_bj,
            CASE WHEN current.timo_id IS NULL THEN 'insert' ELSE 'update' END,
            current.total_income, staged.total_income,
            CASE WHEN current.timo_id IS NULL THEN '' ELSE {current_json} END,
            {staged_json},
            ?
        FROM timo_revenue_staging AS staged
        LEFT JOIN timo_external_revenue_daily AS current
          ON current.guild_executor_key=staged.guild_executor_key
         AND current.stat_date_bj=staged.stat_date_bj
         AND current.timo_id=staged.timo_id
        WHERE staged.sync_id=?
          AND staged.guild_executor_key=?
          AND staged.stat_date_bj=?
          AND (current.timo_id IS NULL OR current.row_hash<>staged.row_hash)
        """,
        (sync_id, created_at, sync_id, guild_executor_key, stat_date_bj),
    )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO timo_revenue_changes(
            sync_id, guild_executor_key, guild_name, country, streamer_id, stat_date_bj,
            change_type, old_income, new_income, old_row_json, new_row_json, created_at
        )
        SELECT
            ?, current.guild_executor_key, current.guild_name, current.country, current.timo_id,
            current.stat_date_bj, 'delete', current.total_income, NULL,
            {current_json}, '', ?
        FROM timo_external_revenue_daily AS current
        LEFT JOIN timo_revenue_staging AS staged
          ON staged.sync_id=?
         AND staged.guild_executor_key=current.guild_executor_key
         AND staged.stat_date_bj=current.stat_date_bj
         AND staged.timo_id=current.timo_id
        WHERE current.guild_executor_key=?
          AND current.stat_date_bj=?
          AND staged.timo_id IS NULL
        """,
        (sync_id, created_at, sync_id, guild_executor_key, stat_date_bj),
    )


def _apply_sql_diff(
    conn: sqlite3.Connection,
    *,
    sync_id: str,
    guild_executor_key: str,
    stat_date_bj: str,
    revision_version: int,
) -> None:
    assignments = ',\n'.join(
        f"{column}=(SELECT staged.{column} FROM timo_revenue_staging AS staged "
        "WHERE staged.sync_id=? "
        "AND staged.guild_executor_key=timo_external_revenue_daily.guild_executor_key "
        "AND staged.stat_date_bj=timo_external_revenue_daily.stat_date_bj "
        "AND staged.timo_id=timo_external_revenue_daily.timo_id)"
        for column in TIMO_REVENUE_COLUMNS
        if column not in {'guild_executor_key', 'stat_date_bj', 'timo_id'}
    )
    set_params: List[Any] = [sync_id] * len(
        [column for column in TIMO_REVENUE_COLUMNS if column not in {'guild_executor_key', 'stat_date_bj', 'timo_id'}]
    )
    conn.execute(
        f"""
        UPDATE timo_external_revenue_daily
        SET {assignments},
            revision_version=?,
            last_sync_id=?,
            row_hash=(
                SELECT staged.row_hash FROM timo_revenue_staging AS staged
                WHERE staged.sync_id=?
                  AND staged.guild_executor_key=timo_external_revenue_daily.guild_executor_key
                  AND staged.stat_date_bj=timo_external_revenue_daily.stat_date_bj
                  AND staged.timo_id=timo_external_revenue_daily.timo_id
            )
        WHERE guild_executor_key=? AND stat_date_bj=?
          AND EXISTS (
              SELECT 1 FROM timo_revenue_staging AS staged
              WHERE staged.sync_id=?
                AND staged.guild_executor_key=timo_external_revenue_daily.guild_executor_key
                AND staged.stat_date_bj=timo_external_revenue_daily.stat_date_bj
                AND staged.timo_id=timo_external_revenue_daily.timo_id
                AND staged.row_hash<>timo_external_revenue_daily.row_hash
          )
        """,
        tuple(set_params + [
            revision_version,
            sync_id,
            sync_id,
            guild_executor_key,
            stat_date_bj,
            sync_id,
        ]),
    )
    select_columns = ', '.join(f'staged.{column}' for column in TIMO_REVENUE_COLUMNS)
    insert_columns = ', '.join(TIMO_REVENUE_COLUMNS)
    conn.execute(
        f"""
        INSERT INTO timo_external_revenue_daily(
            {insert_columns}, revision_version, last_sync_id, row_hash
        )
        SELECT {select_columns}, ?, ?, staged.row_hash
        FROM timo_revenue_staging AS staged
        LEFT JOIN timo_external_revenue_daily AS current
          ON current.guild_executor_key=staged.guild_executor_key
         AND current.stat_date_bj=staged.stat_date_bj
         AND current.timo_id=staged.timo_id
        WHERE staged.sync_id=?
          AND staged.guild_executor_key=?
          AND staged.stat_date_bj=?
          AND current.timo_id IS NULL
        """,
        (revision_version, sync_id, sync_id, guild_executor_key, stat_date_bj),
    )
    conn.execute(
        """
        DELETE FROM timo_external_revenue_daily
        WHERE guild_executor_key=? AND stat_date_bj=?
          AND NOT EXISTS (
              SELECT 1 FROM timo_revenue_staging AS staged
              WHERE staged.sync_id=?
                AND staged.guild_executor_key=timo_external_revenue_daily.guild_executor_key
                AND staged.stat_date_bj=timo_external_revenue_daily.stat_date_bj
                AND staged.timo_id=timo_external_revenue_daily.timo_id
          )
        """,
        (guild_executor_key, stat_date_bj, sync_id),
    )
    # A published scope is one immutable row-set version. Even rows whose
    # business values did not change must carry the revision/sync provenance
    # of that exact accepted set; otherwise facts and watermark disagree.
    conn.execute(
        """
        UPDATE timo_external_revenue_daily
        SET revision_version=?, last_sync_id=?
        WHERE guild_executor_key=? AND stat_date_bj=?
        """,
        (revision_version, sync_id, guild_executor_key, stat_date_bj),
    )


def rollback_timo_revenue_sync(
    conn: sqlite3.Connection,
    *,
    sync_id: str,
    rollback_sync_id: str,
) -> Dict[str, Any]:
    ensure_timo_incremental_schema(conn)
    source = conn.execute(
        "SELECT * FROM timo_sync_run_log WHERE sync_id=?",
        (sync_id,),
    ).fetchone()
    if not source:
        raise TimoIncrementalSyncError('rollback_source_not_found', sync_id)
    existing = conn.execute(
        "SELECT * FROM timo_sync_run_log WHERE rollback_of_sync_id=? AND status='success'",
        (sync_id,),
    ).fetchone()
    if existing:
        result = dict(existing)
        result['ok'] = True
        result['idempotent_replay'] = True
        return result
    if str(source['status'] or '') not in {'success'}:
        raise TimoIncrementalSyncError('rollback_source_not_reversible', str(source['status'] or ''))
    current_watermark = conn.execute(
        """
        SELECT last_success_sync_id
        FROM timo_sync_watermark
        WHERE guild_executor_key=? AND stat_date_bj=?
        """,
        (str(source['guild_executor_key']), str(source['stat_date_bj'])),
    ).fetchone()
    if not current_watermark or str(current_watermark['last_success_sync_id'] or '') != sync_id:
        raise TimoIncrementalSyncError(
            'rollback_source_not_latest',
            'a newer successful sync exists; refusing to overwrite it',
        )
    started = utc_now()
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute(
            """
            INSERT INTO timo_sync_run_log(
                sync_id, parent_run_id, idempotency_key, guild_executor_key, guild_name,
                country, stat_date_bj, data_status, start_time, status, rollback_of_sync_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
            """,
            (
                rollback_sync_id,
                str(source['parent_run_id'] or ''),
                f'rollback:{sync_id}',
                str(source['guild_executor_key']),
                str(source['guild_name'] or ''),
                str(source['country'] or ''),
                str(source['stat_date_bj']),
                str(source['data_status']),
                started,
                sync_id,
                started,
                started,
            ),
        )
        changes = conn.execute(
            """
            SELECT change_type, streamer_id, old_row_json
            FROM timo_revenue_changes
            WHERE sync_id=?
            ORDER BY change_id DESC
            """,
            (sync_id,),
        ).fetchall()
        for change in changes:
            change_type = str(change['change_type'])
            streamer_id = str(change['streamer_id'])
            if change_type == 'insert':
                conn.execute(
                    """
                    DELETE FROM timo_external_revenue_daily
                    WHERE guild_executor_key=? AND stat_date_bj=? AND timo_id=?
                    """,
                    (str(source['guild_executor_key']), str(source['stat_date_bj']), streamer_id),
                )
                continue
            old_row = json.loads(str(change['old_row_json'] or '{}'))
            columns = list(TIMO_REVENUE_COLUMNS) + ['revision_version', 'last_sync_id', 'row_hash']
            placeholders = ', '.join('?' for _ in columns)
            update_sql = ', '.join(
                f'{column}=excluded.{column}'
                for column in columns
                if column not in {'guild_executor_key', 'stat_date_bj', 'timo_id'}
            )
            conn.execute(
                f"""
                INSERT INTO timo_external_revenue_daily({', '.join(columns)})
                VALUES ({placeholders})
                ON CONFLICT(guild_executor_key, stat_date_bj, timo_id) DO UPDATE SET {update_sql}
                """,
                tuple(old_row.get(column) for column in columns),
            )
        previous_watermark = conn.execute(
            """
            SELECT sync_id, checksum, row_count, data_status
            FROM timo_sync_run_log
            WHERE guild_executor_key=? AND stat_date_bj=?
              AND status IN ('success', 'no_op')
              AND start_time < ?
              AND sync_id<>?
            ORDER BY start_time DESC
            LIMIT 1
            """,
            (
                str(source['guild_executor_key']),
                str(source['stat_date_bj']),
                str(source['start_time']),
                sync_id,
            ),
        ).fetchone()
        if previous_watermark:
            restored = conn.execute(
                """
                SELECT COUNT(*) AS row_count, COALESCE(SUM(total_income), 0) AS total_income
                FROM timo_external_revenue_daily
                WHERE guild_executor_key=? AND stat_date_bj=?
                """,
                (str(source['guild_executor_key']), str(source['stat_date_bj'])),
            ).fetchone()
            conn.execute(
                """
                UPDATE timo_sync_watermark
                SET checksum=?, last_success_sync_id=?, last_success_time=?,
                    row_count=?, total_income=?, data_status=?,
                    revision_version=MAX(1, revision_version-1)
                WHERE guild_executor_key=? AND stat_date_bj=?
                """,
                (
                    str(previous_watermark['checksum'] or ''),
                    str(previous_watermark['sync_id']),
                    started,
                    int(restored['row_count'] or 0),
                    float(restored['total_income'] or 0),
                    str(previous_watermark['data_status'] or 'provisional'),
                    str(source['guild_executor_key']),
                    str(source['stat_date_bj']),
                ),
            )
        else:
            conn.execute(
                "DELETE FROM timo_sync_watermark WHERE guild_executor_key=? AND stat_date_bj=?",
                (str(source['guild_executor_key']), str(source['stat_date_bj'])),
            )
        end_time = utc_now()
        conn.execute(
            """
            UPDATE timo_sync_run_log
            SET status='success', end_time=?, row_count=?, updated_at=?
            WHERE sync_id=?
            """,
            (end_time, len(changes), end_time, rollback_sync_id),
        )
        conn.execute(
            "UPDATE timo_sync_run_log SET status='rolled_back', rolled_back_at=?, updated_at=? WHERE sync_id=?",
            (end_time, end_time, sync_id),
        )
        conn.commit()
        return {
            'ok': True,
            'sync_id': rollback_sync_id,
            'status': 'success',
            'rollback_of_sync_id': sync_id,
            'reverted_change_count': len(changes),
        }
    except Exception:
        conn.rollback()
        raise


def next_retry_delay_minutes(attempt: int) -> int:
    schedule = (1, 5, 15, 30)
    index = max(0, min(len(schedule) - 1, int(attempt or 1) - 1))
    return schedule[index]


def schedule_timo_sync_retry(
    conn: sqlite3.Connection,
    *,
    sync_id: str,
    attempt: int,
    persistent_retry: bool = False,
    now: Optional[datetime] = None,
) -> str:
    current = now or datetime.now(timezone.utc)
    normalized_attempt = max(1, int(attempt or 1))
    next_retry_at = (
        current + timedelta(minutes=next_retry_delay_minutes(normalized_attempt))
    ).isoformat() if persistent_retry or normalized_attempt <= 4 else ''
    conn.execute(
        """
        UPDATE timo_sync_run_log
        SET retry_attempt=?, next_retry_at=?, updated_at=?
        WHERE sync_id=?
        """,
        (normalized_attempt, next_retry_at, current.isoformat(), sync_id),
    )
    conn.commit()
    return next_retry_at


def timo_external_feed_status(
    conn: sqlite3.Connection,
    *,
    stat_date_bj: str,
    country: str = '',
    guild_name: str = '',
    stale_after_seconds: int = 1800,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    ensure_timo_incremental_schema(conn)
    where = ['stat_date_bj=?']
    params: List[Any] = [stat_date_bj]
    if str(country or '').strip():
        where.append('country=?')
        params.append(str(country).strip())
    if str(guild_name or '').strip():
        where.append('guild_name=?')
        params.append(str(guild_name).strip())
    rows = conn.execute(
        f"""
        SELECT guild_executor_key, guild_name, country, data_status, checksum,
               last_success_time, last_success_sync_id, row_count, total_income,
               revision_version
        FROM timo_sync_watermark
        WHERE {' AND '.join(where)}
        """,
        tuple(params),
    ).fetchall()
    current = now or datetime.now(timezone.utc)
    scope_manifests: List[Dict[str, Any]] = []
    for watermark in rows:
        scope_where = ['guild_executor_key=?', 'stat_date_bj=?']
        scope_params: List[Any] = [str(watermark['guild_executor_key']), stat_date_bj]
        fact = conn.execute(
            """
            SELECT COUNT(*) AS row_count, COALESCE(SUM(total_income), 0) AS total_income,
                   MIN(revision_version) AS min_revision, MAX(revision_version) AS max_revision,
                   COUNT(DISTINCT last_sync_id) AS sync_id_count,
                   MIN(last_sync_id) AS fact_sync_id,
                   SUM(CASE WHEN provisional<>0 THEN 1 ELSE 0 END) AS provisional_count
            FROM timo_external_revenue_daily
            WHERE guild_executor_key=? AND stat_date_bj=?
            """,
            tuple(scope_params),
        ).fetchone()
        fact_rows = conn.execute(
            """
            SELECT timo_id, row_hash
            FROM timo_external_revenue_daily
            WHERE guild_executor_key=? AND stat_date_bj=?
            ORDER BY timo_id
            """,
            tuple(scope_params),
        ).fetchall()
        digest = hashlib.sha256()
        for fact_row in fact_rows:
            digest.update(str(fact_row['timo_id']).encode('utf-8'))
            digest.update(b'\x1f')
            digest.update(str(fact_row['row_hash']).encode('ascii'))
            digest.update(b'\n')
        fact_checksum = digest.hexdigest()
        expected_rows = int(watermark['row_count'] or 0)
        expected_total = float(watermark['total_income'] or 0)
        revision = int(watermark['revision_version'] or 0)
        last_sync_id = str(watermark['last_success_sync_id'] or '')
        errors: List[str] = []
        if int(fact['row_count'] or 0) != expected_rows:
            errors.append('fact_row_count_mismatch')
        if abs(float(fact['total_income'] or 0) - expected_total) > 0.000001:
            errors.append('fact_total_income_mismatch')
        if fact_checksum != str(watermark['checksum'] or ''):
            errors.append('fact_checksum_mismatch')
        if int(fact['min_revision'] or 0) != revision or int(fact['max_revision'] or 0) != revision:
            errors.append('fact_revision_mismatch')
        if int(fact['sync_id_count'] or 0) != 1 or str(fact['fact_sync_id'] or '') != last_sync_id:
            errors.append('fact_sync_id_mismatch')
        if int(fact['provisional_count'] or 0) != 0 or str(watermark['data_status'] or '') != 'complete':
            errors.append('scope_not_complete')
        observations = conn.execute(
            """
            SELECT COUNT(*) AS observation_count
            FROM timo_sync_run_log
            WHERE guild_executor_key=? AND stat_date_bj=? AND data_status='complete'
              AND status IN ('success','no_op') AND checksum=?
            """,
            (str(watermark['guild_executor_key']), stat_date_bj, str(watermark['checksum'] or '')),
        ).fetchone()
        stability_age_seconds = max(
            0,
            int((current - _parse_utc(str(watermark['last_success_time']))).total_seconds()),
        )
        observation_count = int(observations['observation_count'] or 0)
        if observation_count < 2:
            errors.append('scope_not_reobserved')
        if stability_age_seconds < 2700:
            errors.append('scope_not_stable_45m')
        scope_manifests.append({
            'guild_executor_key': str(watermark['guild_executor_key']),
            'guild_name': str(watermark['guild_name'] or ''),
            'country': str(watermark['country'] or ''),
            'stat_date_bj': stat_date_bj,
            'data_status': str(watermark['data_status'] or ''),
            'row_count': expected_rows,
            'total_income': _decimal_text(expected_total),
            'checksum': str(watermark['checksum'] or ''),
            'revision_version': revision,
            'last_success_sync_id': last_sync_id,
            'observation_count': observation_count,
            'stability_age_seconds': stability_age_seconds,
            'publication_ready': not errors,
            'integrity_errors': errors,
        })
    snapshot_at = max((str(row['last_success_time']) for row in rows), default='')
    cache_age_seconds: Optional[int] = None
    if snapshot_at:
        cache_age_seconds = max(0, int((current - _parse_utc(snapshot_at)).total_seconds()))
    if not rows:
        status = 'failed'
        data_status = 'failed'
    elif scope_manifests and all(bool(item['publication_ready']) for item in scope_manifests):
        status = 'complete'
        data_status = 'complete'
    elif any(
        str(error).startswith('fact_')
        for item in scope_manifests
        for error in item['integrity_errors']
    ):
        status = 'failed'
        data_status = 'failed'
    elif scope_manifests:
        status = 'stale'
        data_status = 'provisional'
    else:
        data_status = 'provisional'
        status = 'stale' if cache_age_seconds is None or cache_age_seconds > stale_after_seconds else 'realtime'
    complete_through = conn.execute(
        """
        SELECT MAX(stat_date_bj) AS complete_through
        FROM timo_sync_watermark
        WHERE data_status='complete'
        """
    ).fetchone()
    return {
        'status': status,
        'data_status': data_status,
        'snapshot_at': snapshot_at,
        'cache_age_seconds': cache_age_seconds,
        'complete_through': str(complete_through['complete_through'] or ''),
        'revision_version': max((int(row['revision_version'] or 1) for row in rows), default=0),
        'last_success_sync_ids': sorted({str(row['last_success_sync_id']) for row in rows}),
        'scope_count': len(rows),
        'publication_ready': bool(scope_manifests) and all(bool(item['publication_ready']) for item in scope_manifests),
        'scope_manifests': scope_manifests,
        'integrity_contract_version': 'timo_scope_manifest_v1',
    }
