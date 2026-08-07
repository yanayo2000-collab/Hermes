from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List, Optional


BI_MART_SCHEMA_VERSION = 'timo_bi_mart_v1'
BI_MART_MAX_PAGE_SIZE = 1000
BI_MART_MAX_OFFSET = 100_000


class TimoBiMartError(RuntimeError):
    pass


class TimoBiMartQueryTimeout(TimoBiMartError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _readonly_uri(path: Path) -> str:
    return f'file:{path.resolve()}?mode=ro'


def ensure_timo_bi_mart_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS bi_timo_revenue_daily (
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            stat_date_bj TEXT NOT NULL,
            timo_id TEXT NOT NULL,
            user_uuid TEXT NOT NULL DEFAULT '',
            nickname TEXT NOT NULL DEFAULT '',
            joined_guild_at_bj TEXT NOT NULL DEFAULT '',
            timo_registered_at_bj TEXT NOT NULL DEFAULT '',
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
            revision_version INTEGER NOT NULL DEFAULT 1,
            last_sync_id TEXT NOT NULL DEFAULT '',
            snapshot_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            mart_synced_at TEXT NOT NULL,
            PRIMARY KEY (guild_executor_key, stat_date_bj, timo_id)
        );
        CREATE INDEX IF NOT EXISTS idx_bi_timo_revenue_country_date
            ON bi_timo_revenue_daily(country, stat_date_bj, guild_name);
        CREATE INDEX IF NOT EXISTS idx_bi_timo_revenue_updated
            ON bi_timo_revenue_daily(updated_at, stat_date_bj);

        CREATE TABLE IF NOT EXISTS bi_timo_scope_watermark (
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '',
            stat_date_bj TEXT NOT NULL,
            source_checksum TEXT NOT NULL,
            source_sync_id TEXT NOT NULL,
            source_revision_version INTEGER NOT NULL DEFAULT 1,
            source_data_status TEXT NOT NULL DEFAULT 'provisional',
            source_row_count INTEGER NOT NULL DEFAULT 0,
            mart_row_count INTEGER NOT NULL DEFAULT 0,
            last_success_run_id TEXT NOT NULL,
            last_success_at TEXT NOT NULL,
            PRIMARY KEY (guild_executor_key, stat_date_bj)
        );

        CREATE TABLE IF NOT EXISTS bi_timo_sync_run_log (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            changed_scope_count INTEGER NOT NULL DEFAULT 0,
            source_row_count INTEGER NOT NULL DEFAULT 0,
            upserted_row_count INTEGER NOT NULL DEFAULT 0,
            deleted_row_count INTEGER NOT NULL DEFAULT 0,
            error_code TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )


def _open_source_snapshot(source_db_path: str, *, timeout_seconds: float) -> sqlite3.Connection:
    path = Path(source_db_path)
    if not path.is_file():
        raise TimoBiMartError(f'source_db_not_found:{path}')
    conn = sqlite3.connect(
        _readonly_uri(path),
        uri=True,
        timeout=max(1.0, float(timeout_seconds)),
    )
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    return conn


def _source_scope_rows(
    source: sqlite3.Connection,
    *,
    guild_executor_key: str,
    stat_date_bj: str,
) -> List[Dict[str, Any]]:
    return [
        dict(row)
        for row in source.execute(
            """
            SELECT
                guild_executor_key, guild_name, country, stat_date_bj, timo_id,
                user_uuid, nickname, COALESCE(joined_guild_at_bj, '') AS joined_guild_at_bj,
                COALESCE(timo_registered_at_bj, '') AS timo_registered_at_bj,
                total_income, qualified_revenue, matching_income,
                private_message_income, private_gift_income, call_income,
                online_hours, call_count, quality_host, quality_revenue,
                provisional, revision_version, last_sync_id, snapshot_at, updated_at
            FROM bi_timo_revenue_view
            WHERE guild_executor_key=? AND stat_date_bj=?
            ORDER BY timo_id
            """,
            (guild_executor_key, stat_date_bj),
        ).fetchall()
    ]


def materialize_timo_bi_mart(
    *,
    source_db_path: str,
    mart_db_path: str,
    run_id: str,
    timeout_seconds: float = 30.0,
    max_changed_scopes: int = 200,
) -> Dict[str, Any]:
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    source = _open_source_snapshot(source_db_path, timeout_seconds=timeout_seconds)
    mart_path = Path(mart_db_path)
    mart_path.parent.mkdir(parents=True, exist_ok=True)
    mart = sqlite3.connect(mart_path, timeout=max(1.0, float(timeout_seconds)))
    mart.row_factory = sqlite3.Row
    mart.execute('PRAGMA busy_timeout=30000')
    mart.execute('PRAGMA journal_mode=WAL')
    mart.execute('PRAGMA synchronous=FULL')
    ensure_timo_bi_mart_schema(mart)
    existing = mart.execute(
        'SELECT * FROM bi_timo_sync_run_log WHERE run_id=?',
        (run_id,),
    ).fetchone()
    if existing:
        result = dict(existing)
        result['ok'] = str(existing['status']) in {'success', 'partial', 'no_op'}
        result['idempotent_replay'] = True
        source.close()
        mart.close()
        return result
    mart.execute(
        """
        INSERT INTO bi_timo_sync_run_log(
            run_id, started_at, status, created_at, updated_at
        ) VALUES (?, ?, 'running', ?, ?)
        """,
        (run_id, started_at, started_at, started_at),
    )
    mart.commit()
    try:
        source_watermarks = source.execute(
            """
            SELECT guild_executor_key, guild_name, country, stat_date_bj, checksum,
                   last_success_sync_id, row_count, data_status, revision_version
            FROM timo_sync_watermark
            ORDER BY stat_date_bj, guild_executor_key
            """
        ).fetchall()
        changed_scopes: List[sqlite3.Row] = []
        for watermark in source_watermarks:
            current = mart.execute(
                """
                SELECT source_checksum, source_sync_id, source_revision_version,
                       source_data_status, source_row_count
                FROM bi_timo_scope_watermark
                WHERE guild_executor_key=? AND stat_date_bj=?
                """,
                (watermark['guild_executor_key'], watermark['stat_date_bj']),
            ).fetchone()
            source_state = (
                str(watermark['checksum']),
                str(watermark['last_success_sync_id']),
                int(watermark['revision_version'] or 1),
                str(watermark['data_status']),
                int(watermark['row_count'] or 0),
            )
            current_state = (
                str(current['source_checksum']),
                str(current['source_sync_id']),
                int(current['source_revision_version'] or 1),
                str(current['source_data_status']),
                int(current['source_row_count'] or 0),
            ) if current else None
            if current_state != source_state:
                changed_scopes.append(watermark)
        pending_scope_count = len(changed_scopes)
        changed_scopes = changed_scopes[: max(1, int(max_changed_scopes))]
        if not changed_scopes:
            finished_at = _utc_now()
            evidence = {
                'schema_version': BI_MART_SCHEMA_VERSION,
                'source_scope_count': len(source_watermarks),
                'changed_scope_count': 0,
                'pending_scope_count': 0,
                'duration_ms': int((time.monotonic() - started_monotonic) * 1000),
            }
            mart.execute(
                """
                UPDATE bi_timo_sync_run_log
                SET status='no_op', finished_at=?, evidence_json=?, updated_at=?
                WHERE run_id=?
                """,
                (
                    finished_at,
                    json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    finished_at,
                    run_id,
                ),
            )
            mart.commit()
            return {'ok': True, 'run_id': run_id, 'status': 'no_op', **evidence}
        source_row_count = upserted_row_count = deleted_row_count = 0
        mart.execute('BEGIN IMMEDIATE')
        for watermark in changed_scopes:
            guild_executor_key = str(watermark['guild_executor_key'])
            stat_date_bj = str(watermark['stat_date_bj'])
            rows = _source_scope_rows(
                source,
                guild_executor_key=guild_executor_key,
                stat_date_bj=stat_date_bj,
            )
            expected_rows = int(watermark['row_count'] or 0)
            if len(rows) != expected_rows:
                raise TimoBiMartError(
                    f'source_watermark_row_count_mismatch:{guild_executor_key}:{stat_date_bj}:'
                    f'{len(rows)}!={expected_rows}'
                )
            old_count = int(
                mart.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM bi_timo_revenue_daily
                    WHERE guild_executor_key=? AND stat_date_bj=?
                    """,
                    (guild_executor_key, stat_date_bj),
                ).fetchone()['n']
                or 0
            )
            mart.execute(
                """
                DELETE FROM bi_timo_revenue_daily
                WHERE guild_executor_key=? AND stat_date_bj=?
                """,
                (guild_executor_key, stat_date_bj),
            )
            mart.executemany(
                """
                INSERT INTO bi_timo_revenue_daily(
                    guild_executor_key, guild_name, country, stat_date_bj, timo_id,
                    user_uuid, nickname, joined_guild_at_bj, timo_registered_at_bj,
                    total_income, qualified_revenue, matching_income,
                    private_message_income, private_gift_income, call_income,
                    online_hours, call_count, quality_host, quality_revenue,
                    provisional, revision_version, last_sync_id, snapshot_at,
                    updated_at, mart_synced_at
                ) VALUES (
                    :guild_executor_key, :guild_name, :country, :stat_date_bj, :timo_id,
                    :user_uuid, :nickname, :joined_guild_at_bj, :timo_registered_at_bj,
                    :total_income, :qualified_revenue, :matching_income,
                    :private_message_income, :private_gift_income, :call_income,
                    :online_hours, :call_count, :quality_host, :quality_revenue,
                    :provisional, :revision_version, :last_sync_id, :snapshot_at,
                    :updated_at, :mart_synced_at
                )
                """,
                [{**row, 'mart_synced_at': started_at} for row in rows],
            )
            mart.execute(
                """
                INSERT INTO bi_timo_scope_watermark(
                    guild_executor_key, guild_name, country, stat_date_bj,
                    source_checksum, source_sync_id, source_revision_version,
                    source_data_status, source_row_count, mart_row_count,
                    last_success_run_id, last_success_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_executor_key, stat_date_bj) DO UPDATE SET
                    guild_name=excluded.guild_name,
                    country=excluded.country,
                    source_checksum=excluded.source_checksum,
                    source_sync_id=excluded.source_sync_id,
                    source_revision_version=excluded.source_revision_version,
                    source_data_status=excluded.source_data_status,
                    source_row_count=excluded.source_row_count,
                    mart_row_count=excluded.mart_row_count,
                    last_success_run_id=excluded.last_success_run_id,
                    last_success_at=excluded.last_success_at
                """,
                (
                    guild_executor_key,
                    str(watermark['guild_name'] or ''),
                    str(watermark['country'] or ''),
                    stat_date_bj,
                    str(watermark['checksum']),
                    str(watermark['last_success_sync_id']),
                    int(watermark['revision_version'] or 1),
                    str(watermark['data_status']),
                    expected_rows,
                    len(rows),
                    run_id,
                    started_at,
                ),
            )
            source_row_count += len(rows)
            upserted_row_count += len(rows)
            deleted_row_count += old_count
        finished_at = _utc_now()
        evidence = {
            'schema_version': BI_MART_SCHEMA_VERSION,
            'source_scope_count': len(source_watermarks),
            'changed_scope_count': len(changed_scopes),
            'pending_scope_count': max(0, pending_scope_count - len(changed_scopes)),
            'source_row_count': source_row_count,
            'upserted_row_count': upserted_row_count,
            'deleted_row_count': deleted_row_count,
            'duration_ms': int((time.monotonic() - started_monotonic) * 1000),
        }
        status = 'partial' if evidence['pending_scope_count'] else 'success'
        mart.execute(
            """
            UPDATE bi_timo_sync_run_log
            SET status=?, finished_at=?, changed_scope_count=?,
                source_row_count=?, upserted_row_count=?, deleted_row_count=?,
                evidence_json=?, updated_at=?
            WHERE run_id=?
            """,
            (
                status,
                finished_at,
                len(changed_scopes),
                source_row_count,
                upserted_row_count,
                deleted_row_count,
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                finished_at,
                run_id,
            ),
        )
        mart.commit()
        return {'ok': True, 'run_id': run_id, 'status': status, **evidence}
    except Exception as exc:
        mart.rollback()
        finished_at = _utc_now()
        mart.execute(
            """
            UPDATE bi_timo_sync_run_log
            SET status='failed', finished_at=?, error_code='bi_mart_sync_failed',
                error=?, updated_at=?
            WHERE run_id=?
            """,
            (finished_at, str(exc)[:1000], finished_at, run_id),
        )
        mart.commit()
        raise
    finally:
        source.rollback()
        source.close()
        mart.close()


def query_timo_bi_mart(
    *,
    mart_db_path: str,
    stat_date_bj: str,
    country: str = '',
    guild_name: str = '',
    updated_since: str = '',
    include_provisional: bool = True,
    limit: int = 500,
    offset: int = 0,
    statement_timeout_ms: int = 2_000,
) -> Dict[str, Any]:
    path = Path(mart_db_path)
    if not path.is_file():
        raise TimoBiMartError('bi_mart_not_ready')
    safe_limit = max(1, min(BI_MART_MAX_PAGE_SIZE, int(limit or 500)))
    safe_offset = max(0, min(BI_MART_MAX_OFFSET, int(offset or 0)))
    deadline = time.monotonic() + max(0.05, int(statement_timeout_ms or 2_000) / 1000.0)
    conn = sqlite3.connect(_readonly_uri(path), uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, 5_000)
    where = ['stat_date_bj=?']
    params: List[Any] = [str(stat_date_bj).strip()]
    scope_where = ['stat_date_bj=?']
    scope_params: List[Any] = [str(stat_date_bj).strip()]
    if str(country or '').strip():
        where.append('country=?')
        params.append(str(country).strip())
        scope_where.append('country=?')
        scope_params.append(str(country).strip())
    if str(guild_name or '').strip():
        where.append('guild_name=?')
        params.append(str(guild_name).strip())
        scope_where.append('guild_name=?')
        scope_params.append(str(guild_name).strip())
    if str(updated_since or '').strip():
        where.append('updated_at>=?')
        params.append(str(updated_since).strip())
    if not include_provisional:
        where.append('provisional=0')
    where_sql = ' AND '.join(where)
    try:
        total = int(
            conn.execute(
                f'SELECT COUNT(*) AS n FROM bi_timo_revenue_daily WHERE {where_sql}',
                tuple(params),
            ).fetchone()['n']
            or 0
        )
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT guild_name, country, stat_date_bj, timo_id, user_uuid,
                       nickname, joined_guild_at_bj, timo_registered_at_bj,
                       total_income, qualified_revenue, matching_income,
                       private_message_income, private_gift_income, call_income,
                       online_hours, call_count, quality_host, quality_revenue,
                       provisional, revision_version, last_sync_id, snapshot_at,
                       updated_at, mart_synced_at
                FROM bi_timo_revenue_daily
                WHERE {where_sql}
                ORDER BY guild_name, timo_id
                LIMIT ? OFFSET ?
                """,
                tuple(params + [safe_limit, safe_offset]),
            ).fetchall()
        ]
        watermark = conn.execute(
            f"""
            SELECT MAX(last_success_at) AS snapshot_at,
                   MAX(source_revision_version) AS revision_version,
                   MIN(source_data_status) AS min_status,
                   MAX(source_data_status) AS max_status,
                   MAX(last_success_run_id) AS last_success_run_id,
                   COUNT(*) AS scope_count
            FROM bi_timo_scope_watermark
            WHERE {' AND '.join(scope_where)}
            """,
            tuple(scope_params),
        ).fetchone()
        scope_count = int(watermark['scope_count'] or 0) if watermark else 0
        if scope_count == 0:
            raise TimoBiMartError('bi_mart_revenue_not_ready:no_successful_scope')
        snapshot_at = str(watermark['snapshot_at'] or '') if watermark else ''
        data_status = (
            'complete'
            if watermark
            and str(watermark['min_status'] or '') == 'complete'
            and str(watermark['max_status'] or '') == 'complete'
            else 'provisional'
        )
        total_income = float(
            conn.execute(
                f'SELECT COALESCE(SUM(total_income), 0) AS amount '
                f'FROM bi_timo_revenue_daily WHERE {" AND ".join(scope_where)}',
                tuple(scope_params),
            ).fetchone()['amount']
            or 0
        )
        if data_status == 'provisional' and abs(total_income) <= 0.000001:
            raise TimoBiMartError(
                'bi_mart_revenue_not_ready:provisional_zero_income_without_upstream_completion'
            )
        return {
            'ok': True,
            'schema_version': BI_MART_SCHEMA_VERSION,
            'status': data_status,
            'snapshot_at': snapshot_at,
            'revision_version': int(watermark['revision_version'] or 0) if watermark else 0,
            'quality_status': 'passed',
            'consumable': True,
            'readiness_reason': 'complete_zero_confirmed' if data_status == 'complete' and abs(total_income) <= 0.000001 else 'revenue_ready',
            'last_success_run_id': str(watermark['last_success_run_id'] or ''),
            'scope_count': scope_count,
            'total_income': total_income,
            'statement_timeout_ms': max(50, int(statement_timeout_ms or 2_000)),
            'max_page_size': BI_MART_MAX_PAGE_SIZE,
            'total': total,
            'limit': safe_limit,
            'offset': safe_offset,
            'rows': rows,
        }
    except sqlite3.OperationalError as exc:
        if 'interrupted' in str(exc).lower():
            raise TimoBiMartQueryTimeout('bi_mart_query_timeout') from exc
        raise
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()
