from datetime import datetime, timedelta, timezone
import sqlite3

from app.timo_incremental_materialization import (
    ensure_timo_incremental_schema,
    materialize_timo_revenue_snapshot,
    timo_external_feed_status,
)


def connect_factory(tmp_path):
    db_path = tmp_path / 'timo.sqlite3'

    def connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    conn = connect()
    conn.executescript('''
      CREATE TABLE timo_external_revenue_daily (
        guild_executor_key TEXT NOT NULL, guild_name TEXT NOT NULL, country TEXT NOT NULL,
        stat_date_bj TEXT NOT NULL, timo_id TEXT NOT NULL, user_uuid TEXT NOT NULL DEFAULT '',
        nickname TEXT NOT NULL DEFAULT '', total_income REAL NOT NULL DEFAULT 0,
        qualified_revenue REAL NOT NULL DEFAULT 0, matching_income REAL NOT NULL DEFAULT 0,
        private_message_income REAL NOT NULL DEFAULT 0, private_gift_income REAL NOT NULL DEFAULT 0,
        call_income REAL NOT NULL DEFAULT 0, online_hours REAL NOT NULL DEFAULT 0,
        call_count INTEGER NOT NULL DEFAULT 0, quality_host INTEGER NOT NULL DEFAULT 0,
        quality_revenue REAL NOT NULL DEFAULT 0, provisional INTEGER NOT NULL DEFAULT 1,
        source_payload TEXT NOT NULL DEFAULT '{}', snapshot_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(guild_executor_key,stat_date_bj,timo_id));
    ''')
    ensure_timo_incremental_schema(conn)
    conn.commit()
    conn.close()
    return connect


def materialize(connect, sync_id, snapshot_at):
    return materialize_timo_revenue_snapshot(
        connect, sync_id=sync_id, parent_run_id='parent', guild_executor_key='guild-br',
        guild_name='agency of BR somente', country='Brazil', stat_date_bj='2026-07-23',
        provisional=False, snapshot_at=snapshot_at,
        revenue_rows=[{'timo_id': '1', 'total_income': 10}, {'timo_id': '2', 'total_income': 20}],
    )


def test_manifest_requires_stability_and_fails_closed_on_fact_drift(tmp_path):
    connect = connect_factory(tmp_path)
    stable_now = datetime.now(timezone.utc) + timedelta(hours=1)
    materialize(connect, 'complete-1', '2026-07-23T08:00:00+00:00')
    conn = connect()
    first = timo_external_feed_status(conn, stat_date_bj='2026-07-23', country='Brazil',
        now=stable_now)
    assert first['status'] == 'stale'
    assert first['scope_manifests'][0]['integrity_errors'] == ['scope_not_reobserved']
    conn.close()

    materialize(connect, 'complete-2', '2026-07-23T08:15:00+00:00')
    conn = connect()
    stable = timo_external_feed_status(conn, stat_date_bj='2026-07-23', country='Brazil',
        now=stable_now)
    assert stable['status'] == 'complete'
    assert stable['publication_ready'] is True
    assert stable['scope_manifests'][0]['observation_count'] == 2
    assert stable['scope_manifests'][0]['source_snapshot_at'] == '2026-07-23T08:15:00+00:00'
    conn.execute("DELETE FROM timo_external_revenue_daily WHERE timo_id='2'")
    conn.commit()
    broken = timo_external_feed_status(conn, stat_date_bj='2026-07-23', country='Brazil',
        now=stable_now)
    assert broken['status'] == 'failed'
    assert {'fact_row_count_mismatch','fact_total_income_mismatch','fact_checksum_mismatch'} <= set(
        broken['scope_manifests'][0]['integrity_errors'])
    conn.close()
