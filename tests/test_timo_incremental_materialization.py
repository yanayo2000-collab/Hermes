from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.timo_incremental_materialization import (
    bootstrap_timo_legacy_watermarks,
    TimoCircuitOpen,
    TimoDbSyncLease,
    TimoIncrementalSyncError,
    TimoSyncLockBusy,
    check_timo_circuit_breaker,
    ensure_timo_incremental_schema,
    materialize_timo_revenue_snapshot,
    next_retry_delay_minutes,
    record_timo_circuit_failure,
    record_timo_sync_attempt_failure,
    rollback_timo_revenue_sync,
    schedule_timo_sync_retry,
    timo_external_feed_status,
)
from scripts import timo_incremental_retry_worker as retry_worker
from scripts.timo_incremental_retry_worker import due_retry_dates


def _connect_factory(tmp_path):
    db_path = tmp_path / 'timo_incremental.sqlite3'

    def connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    conn = connect()
    conn.executescript(
        """
        CREATE TABLE timo_external_streamers (
            guild_executor_key TEXT NOT NULL,
            timo_id TEXT NOT NULL,
            joined_guild_at_bj TEXT NOT NULL DEFAULT '',
            timo_registered_at_bj TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(guild_executor_key, timo_id)
        );
        CREATE TABLE timo_external_revenue_daily (
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
            snapshot_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(guild_executor_key, stat_date_bj, timo_id)
        );
        """
    )
    ensure_timo_incremental_schema(conn)
    conn.commit()
    conn.close()
    return connect


def _rows(first_income=10, second_income=20):
    return [
        {'timo_id': '1001', 'nick_name': 'A', 'total_income': first_income},
        {'timo_id': '1002', 'nick_name': 'B', 'total_income': second_income},
    ]


def _materialize(connect, *, sync_id, rows, provisional=True, snapshot_at='2026-07-23T08:00:00+00:00'):
    return materialize_timo_revenue_snapshot(
        connect,
        sync_id=sync_id,
        parent_run_id='parent-1',
        guild_executor_key='guild-br',
        guild_name='agency of BR somente',
        country='Brazil',
        stat_date_bj='2026-07-23',
        provisional=provisional,
        revenue_rows=rows,
        snapshot_at=snapshot_at,
    )


def test_external_feed_requires_reobservation_and_exact_fact_manifest(tmp_path):
    connect = _connect_factory(tmp_path)
    _materialize(
        connect,
        sync_id='complete-1',
        rows=_rows(),
        provisional=False,
        snapshot_at='2026-07-23T08:00:00+00:00',
    )
    conn = connect()
    stable_now = datetime.fromisoformat(conn.execute(
        "SELECT last_success_time FROM timo_sync_watermark WHERE guild_executor_key='guild-br'"
    ).fetchone()[0]) + timedelta(hours=1)
    first = timo_external_feed_status(
        conn,
        stat_date_bj='2026-07-23',
        country='Brazil',
        now=stable_now,
    )
    assert first['publication_ready'] is False
    assert first['status'] == 'stale'
    assert first['scope_manifests'][0]['integrity_errors'] == ['scope_not_reobserved']
    conn.close()

    _materialize(
        connect,
        sync_id='complete-2',
        rows=_rows(),
        provisional=False,
        snapshot_at='2026-07-23T08:15:00+00:00',
    )
    conn = connect()
    stable = timo_external_feed_status(
        conn,
        stat_date_bj='2026-07-23',
        country='Brazil',
        now=stable_now,
    )
    assert stable['publication_ready'] is True
    assert stable['status'] == 'complete'
    manifest = stable['scope_manifests'][0]
    assert manifest['row_count'] == 2
    assert manifest['total_income'] == '30'
    assert manifest['observation_count'] == 2

    conn.execute(
        "DELETE FROM timo_external_revenue_daily WHERE guild_executor_key='guild-br' AND timo_id='1002'"
    )
    conn.commit()
    broken = timo_external_feed_status(
        conn,
        stat_date_bj='2026-07-23',
        country='Brazil',
        now=datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc),
    )
    assert broken['publication_ready'] is False
    assert broken['status'] == 'failed'
    assert {'fact_row_count_mismatch', 'fact_total_income_mismatch', 'fact_checksum_mismatch'} <= set(
        broken['scope_manifests'][0]['integrity_errors']
    )
    conn.close()


def test_incremental_materialization_is_idempotent_and_uses_sql_diff(tmp_path):
    connect = _connect_factory(tmp_path)
    first = _materialize(connect, sync_id='sync-1', rows=_rows())
    assert first['status'] == 'success'
    assert first['inserted_count'] == 2
    assert first['updated_count'] == 0

    conn = connect()
    before = {
        row['timo_id']: (row['updated_at'], row['revision_version'])
        for row in conn.execute(
            "SELECT timo_id, updated_at, revision_version FROM timo_external_revenue_daily"
        )
    }
    conn.close()

    no_op = _materialize(
        connect,
        sync_id='sync-2',
        rows=_rows(),
        snapshot_at='2026-07-23T08:15:00+00:00',
    )
    assert no_op['status'] == 'no_op'
    assert no_op['unchanged_count'] == 2

    changed = _materialize(
        connect,
        sync_id='sync-3',
        rows=_rows(second_income=25),
        snapshot_at='2026-07-23T08:30:00+00:00',
    )
    assert changed['inserted_count'] == 0
    assert changed['updated_count'] == 1
    assert changed['unchanged_count'] == 1

    conn = connect()
    after = {
        row['timo_id']: (row['updated_at'], row['revision_version'], row['total_income'])
        for row in conn.execute(
            "SELECT timo_id, updated_at, revision_version, total_income "
            "FROM timo_external_revenue_daily"
        )
    }
    assert after['1001'] == ('2026-07-23T08:30:00+00:00', 2, 10.0)
    assert after['1002'] == ('2026-07-23T08:30:00+00:00', 2, 25.0)
    assert conn.execute(
        "SELECT COUNT(*) FROM timo_revenue_changes WHERE sync_id='sync-3'"
    ).fetchone()[0] == 2
    conn.close()


def test_quality_gate_failure_does_not_touch_canonical_rows(tmp_path):
    connect = _connect_factory(tmp_path)
    _materialize(connect, sync_id='sync-1', rows=_rows())

    with pytest.raises(TimoIncrementalSyncError) as exc_info:
        _materialize(
            connect,
            sync_id='sync-empty',
            rows=[],
            snapshot_at='2026-07-23T08:15:00+00:00',
        )
    assert exc_info.value.code == 'quality_gate_empty_snapshot'

    conn = connect()
    assert conn.execute(
        "SELECT COUNT(*) FROM timo_external_revenue_daily"
    ).fetchone()[0] == 2
    run = conn.execute(
        "SELECT status, error_code FROM timo_sync_run_log WHERE sync_id='sync-empty'"
    ).fetchone()
    assert dict(run) == {
        'status': 'quality_failed',
        'error_code': 'quality_gate_empty_snapshot',
    }
    conn.close()


def test_zero_income_provisional_is_rejected_then_complete_snapshot_is_accepted(tmp_path):
    connect = _connect_factory(tmp_path)
    provisional_rows = [
        {'timo_id': f'host-{index}', 'nick_name': f'Host {index}', 'total_income': 0}
        for index in range(100)
    ]
    with pytest.raises(TimoIncrementalSyncError) as exc_info:
        _materialize(connect, sync_id='provisional-all-hosts', rows=provisional_rows)
    assert exc_info.value.code == 'quality_gate_provisional_zero_income_not_ready'

    conn = connect()
    rejected = conn.execute(
        "SELECT status, error_code FROM timo_sync_run_log WHERE sync_id='provisional-all-hosts'"
    ).fetchone()
    assert dict(rejected) == {
        'status': 'quality_failed',
        'error_code': 'quality_gate_provisional_zero_income_not_ready',
    }
    assert conn.execute(
        "SELECT COUNT(*) FROM timo_sync_watermark WHERE stat_date_bj='2026-07-23'"
    ).fetchone()[0] == 0
    conn.close()

    complete_rows = [
        {'timo_id': f'host-{index}', 'nick_name': f'Host {index}', 'total_income': index + 1}
        for index in range(10)
    ]
    result = _materialize(
        connect,
        sync_id='complete-earners',
        rows=complete_rows,
        provisional=False,
        snapshot_at='2026-07-24T08:00:00+00:00',
    )

    assert result['status'] == 'success'
    assert result['row_count'] == 10
    assert result['deleted_count'] == 0
    assert result['quality_gate']['metrics']['old_data_status'] == 'empty'
    assert result['quality_gate']['metrics']['new_data_status'] == 'complete'
    assert result['quality_gate']['metrics']['comparable_population'] is False

    conn = connect()
    canonical = conn.execute(
        """
        SELECT COUNT(*) AS row_count,
               SUM(CASE WHEN provisional=0 THEN 1 ELSE 0 END) AS complete_count,
               SUM(total_income) AS total_income
        FROM timo_external_revenue_daily
        WHERE guild_executor_key='guild-br' AND stat_date_bj='2026-07-23'
        """
    ).fetchone()
    assert dict(canonical) == {
        'row_count': 10,
        'complete_count': 10,
        'total_income': 55.0,
    }
    watermark = conn.execute(
        """
        SELECT data_status, row_count, total_income
        FROM timo_sync_watermark
        WHERE guild_executor_key='guild-br' AND stat_date_bj='2026-07-23'
        """
    ).fetchone()
    assert dict(watermark) == {
        'data_status': 'complete',
        'row_count': 10,
        'total_income': 55.0,
    }
    conn.close()


@pytest.mark.parametrize(
    ('provisional_count', 'complete_count', 'complete_income'),
    (
        (803, 121, 365620.45),
        (559, 67, 1121712.10),
        (3695, 401, 7917046.90),
    ),
)
def test_production_shaped_provisional_to_complete_transitions(
    tmp_path,
    provisional_count,
    complete_count,
    complete_income,
):
    connect = _connect_factory(tmp_path)
    provisional_rows = [
        {'timo_id': f'host-{index}', 'total_income': 0}
        for index in range(provisional_count)
    ]
    with pytest.raises(TimoIncrementalSyncError) as exc_info:
        _materialize(connect, sync_id='provisional-all-hosts', rows=provisional_rows)
    assert exc_info.value.code == 'quality_gate_provisional_zero_income_not_ready'

    per_host_income = complete_income / complete_count
    complete_rows = [
        {'timo_id': f'host-{index}', 'total_income': per_host_income}
        for index in range(complete_count)
    ]
    result = _materialize(
        connect,
        sync_id='complete-earners',
        rows=complete_rows,
        provisional=False,
        snapshot_at='2026-07-24T08:00:00+00:00',
    )

    assert result['status'] == 'success'
    assert result['row_count'] == complete_count
    assert result['deleted_count'] == 0
    assert result['quality_gate']['metrics']['comparable_population'] is False
    conn = connect()
    materialized = conn.execute(
        """
        SELECT COUNT(*) AS row_count, SUM(total_income) AS total_income,
               SUM(CASE WHEN provisional=0 THEN 1 ELSE 0 END) AS complete_count
        FROM timo_external_revenue_daily
        WHERE guild_executor_key='guild-br' AND stat_date_bj='2026-07-23'
        """
    ).fetchone()
    assert int(materialized['row_count']) == complete_count
    assert int(materialized['complete_count']) == complete_count
    assert float(materialized['total_income']) == pytest.approx(complete_income)
    conn.close()


def test_complete_snapshot_still_rejects_true_same_population_row_drop(tmp_path):
    connect = _connect_factory(tmp_path)
    complete_rows = [
        {'timo_id': f'host-{index}', 'total_income': index + 1}
        for index in range(20)
    ]
    _materialize(
        connect,
        sync_id='complete-baseline',
        rows=complete_rows,
        provisional=False,
    )

    with pytest.raises(TimoIncrementalSyncError) as exc_info:
        _materialize(
            connect,
            sync_id='complete-partial',
            rows=complete_rows[:5],
            provisional=False,
            snapshot_at='2026-07-24T08:00:00+00:00',
        )
    assert exc_info.value.code == 'quality_gate_row_count_drop'
    assert exc_info.value.evidence['metrics']['comparable_population'] is True


def test_status_transition_rejects_missing_nonzero_provisional_streamer(tmp_path):
    connect = _connect_factory(tmp_path)
    provisional_rows = [
        {'timo_id': f'host-{index}', 'total_income': 10 if index == 19 else 0}
        for index in range(20)
    ]
    _materialize(connect, sync_id='provisional-with-income', rows=provisional_rows)

    with pytest.raises(TimoIncrementalSyncError) as exc_info:
        _materialize(
            connect,
            sync_id='complete-missing-known-income',
            rows=[{'timo_id': f'host-{index}', 'total_income': index + 1} for index in range(5)],
            provisional=False,
            snapshot_at='2026-07-24T08:00:00+00:00',
        )
    assert exc_info.value.code == 'quality_gate_missing_nonzero_streamer'
    assert exc_info.value.evidence['metrics']['missing_nonzero_rows'] == 1


def test_complete_snapshot_cannot_downgrade_to_provisional(tmp_path):
    connect = _connect_factory(tmp_path)
    _materialize(
        connect,
        sync_id='complete-baseline',
        rows=_rows(),
        provisional=False,
    )

    with pytest.raises(TimoIncrementalSyncError) as exc_info:
        _materialize(
            connect,
            sync_id='provisional-downgrade',
            rows=_rows(),
            provisional=True,
            snapshot_at='2026-07-24T08:00:00+00:00',
        )
    assert exc_info.value.code == 'quality_gate_complete_downgrade'


def test_first_upgraded_run_guards_nontrivial_legacy_scope_without_watermark(tmp_path):
    connect = _connect_factory(tmp_path)
    conn = connect()
    for index in range(20):
        conn.execute(
            """
            INSERT INTO timo_external_revenue_daily(
                guild_executor_key, guild_name, country, stat_date_bj, timo_id,
                total_income, provisional, source_payload, snapshot_at, updated_at
            ) VALUES (
                'guild-br', 'agency of BR somente', 'Brazil', '2026-07-23', ?,
                1, 1, '{}', 'legacy', 'legacy'
            )
            """,
            (f'legacy-{index}',),
        )
    conn.commit()
    conn.close()
    with pytest.raises(TimoIncrementalSyncError) as exc_info:
        _materialize(connect, sync_id='bootstrap-partial', rows=_rows())
    assert exc_info.value.code == 'quality_gate_row_count_drop'
    conn = connect()
    assert conn.execute(
        "SELECT COUNT(*) FROM timo_external_revenue_daily"
    ).fetchone()[0] == 20
    conn.close()


def test_legacy_bootstrap_sets_hashes_and_watermark_without_business_revision_changes(tmp_path):
    connect = _connect_factory(tmp_path)
    conn = connect()
    for timo_id, income in (('legacy-1', 10), ('legacy-2', 20)):
        conn.execute(
            """
            INSERT INTO timo_external_revenue_daily(
                guild_executor_key, guild_name, country, stat_date_bj, timo_id,
                total_income, provisional, source_payload, snapshot_at, updated_at
            ) VALUES (
                'guild-br', 'agency of BR somente', 'Brazil', '2026-07-22', ?,
                ?, 0, '{}', 'legacy-snapshot', 'legacy-updated'
            )
            """,
            (timo_id, income),
        )
    conn.commit()
    result = bootstrap_timo_legacy_watermarks(conn, max_scopes=1)
    assert result['status'] == 'success'
    assert result['processed_scope_count'] == 1
    assert result['processed_row_count'] == 2
    assert conn.execute(
        "SELECT SUM(total_income) FROM timo_external_revenue_daily"
    ).fetchone()[0] == 30
    assert conn.execute(
        "SELECT COUNT(*) FROM timo_external_revenue_daily WHERE row_hash<>''"
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM timo_revenue_changes"
    ).fetchone()[0] == 0
    watermark = conn.execute(
        """
        SELECT row_count, total_income, data_status
        FROM timo_sync_watermark
        WHERE guild_executor_key='guild-br' AND stat_date_bj='2026-07-22'
        """
    ).fetchone()
    assert dict(watermark) == {
        'row_count': 2,
        'total_income': 30.0,
        'data_status': 'complete',
    }
    replay = bootstrap_timo_legacy_watermarks(conn, max_scopes=1)
    assert replay['status'] == 'no_op'
    conn.close()


def test_successful_sync_can_be_rolled_back_once(tmp_path):
    connect = _connect_factory(tmp_path)
    _materialize(connect, sync_id='sync-1', rows=_rows())
    conn = connect()
    before = [tuple(row) for row in conn.execute(
        "SELECT timo_id,total_income,updated_at,revision_version,last_sync_id,row_hash "
        "FROM timo_external_revenue_daily ORDER BY timo_id"
    )]
    conn.close()
    _materialize(
        connect,
        sync_id='sync-2',
        rows=_rows(second_income=99),
        snapshot_at='2026-07-23T08:15:00+00:00',
    )
    conn = connect()
    result = rollback_timo_revenue_sync(
        conn,
        sync_id='sync-2',
        rollback_sync_id='rollback-sync-2',
    )
    assert result['ok'] is True
    assert conn.execute(
        "SELECT total_income FROM timo_external_revenue_daily WHERE timo_id='1002'"
    ).fetchone()[0] == 20
    after = [tuple(row) for row in conn.execute(
        "SELECT timo_id,total_income,updated_at,revision_version,last_sync_id,row_hash "
        "FROM timo_external_revenue_daily ORDER BY timo_id"
    )]
    assert after == before
    replay = rollback_timo_revenue_sync(
        conn,
        sync_id='sync-2',
        rollback_sync_id='rollback-sync-2-replay',
    )
    assert replay['idempotent_replay'] is True
    conn.close()


def test_rollback_refuses_to_overwrite_a_newer_successful_sync(tmp_path):
    connect = _connect_factory(tmp_path)
    _materialize(connect, sync_id='sync-1', rows=_rows())
    _materialize(
        connect,
        sync_id='sync-2',
        rows=_rows(second_income=30),
        snapshot_at='2026-07-23T08:15:00+00:00',
    )
    _materialize(
        connect,
        sync_id='sync-3',
        rows=_rows(second_income=40),
        snapshot_at='2026-07-23T08:30:00+00:00',
    )
    conn = connect()
    with pytest.raises(TimoIncrementalSyncError) as exc_info:
        rollback_timo_revenue_sync(
            conn,
            sync_id='sync-2',
            rollback_sync_id='unsafe-rollback',
        )
    assert exc_info.value.code == 'rollback_source_not_latest'
    assert conn.execute(
        "SELECT total_income FROM timo_external_revenue_daily WHERE timo_id='1002'"
    ).fetchone()[0] == 40
    conn.close()


def test_db_lease_is_exclusive_and_expired_lock_is_recoverable(tmp_path):
    connect = _connect_factory(tmp_path)
    lease = TimoDbSyncLease(
        connect,
        lock_key='timo_sync:guild-br:2026-07-23',
        owner_sync_id='sync-1',
        auto_renew=False,
    ).acquire()
    with pytest.raises(TimoSyncLockBusy):
        TimoDbSyncLease(
            connect,
            lock_key='timo_sync:guild-br:2026-07-23',
            owner_sync_id='sync-2',
            auto_renew=False,
        ).acquire()
    lease.release()

    conn = connect()
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    conn.execute(
        """
        INSERT INTO timo_sync_locks(lock_key, owner_sync_id, acquired_at, heartbeat_at, expires_at)
        VALUES ('timo_sync:guild-br:2026-07-23', 'stale', ?, ?, ?)
        """,
        (expired, expired, expired),
    )
    conn.commit()
    conn.close()
    recovered = TimoDbSyncLease(
        connect,
        lock_key='timo_sync:guild-br:2026-07-23',
        owner_sync_id='sync-3',
        auto_renew=False,
    ).acquire()
    recovered.release()


def test_circuit_breaker_and_external_status_contract(tmp_path):
    connect = _connect_factory(tmp_path)
    conn = connect()
    now = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)
    for _ in range(3):
        evidence = record_timo_circuit_failure(
            conn,
            guild_executor_key='guild-br',
            error_code='upstream_failed',
            now=now,
        )
    assert evidence['state'] == 'open'
    with pytest.raises(TimoCircuitOpen):
        check_timo_circuit_breaker(
            conn,
            guild_executor_key='guild-br',
            sync_id='sync-probe',
            now=now + timedelta(minutes=1),
        )
    conn.close()

    _materialize(connect, sync_id='sync-1', rows=_rows())
    conn = connect()
    last_success_time = datetime.fromisoformat(
        conn.execute(
            "SELECT last_success_time FROM timo_sync_watermark WHERE guild_executor_key='guild-br'"
        ).fetchone()[0]
    )
    status = timo_external_feed_status(
        conn,
        stat_date_bj='2026-07-23',
        country='Brazil',
        now=last_success_time + timedelta(minutes=10),
    )
    assert status['status'] == 'stale'
    assert status['data_status'] == 'provisional'
    assert status['cache_age_seconds'] == 600
    assert status['revision_version'] == 1
    assert [next_retry_delay_minutes(attempt) for attempt in range(1, 6)] == [1, 5, 15, 30, 30]
    conn.close()


def test_manifest_drift_forces_new_atomic_revision_instead_of_false_no_op(tmp_path):
    connect = _connect_factory(tmp_path)
    _materialize(
        connect,
        sync_id='sync-1',
        rows=_rows(),
        provisional=False,
        snapshot_at='2026-07-23T08:00:00+00:00',
    )
    conn = connect()
    conn.execute(
        "UPDATE timo_external_revenue_daily SET revision_version=99, last_sync_id='manual-drift' "
        "WHERE timo_id='1001'"
    )
    conn.commit()
    conn.close()

    repaired = _materialize(
        connect,
        sync_id='sync-2',
        rows=_rows(),
        provisional=False,
        snapshot_at='2026-07-23T08:15:00+00:00',
    )
    assert repaired['status'] == 'success'
    assert repaired['revision_version'] == 2
    assert repaired['updated_count'] == 0
    assert repaired['unchanged_count'] == 2

    conn = connect()
    lineage = conn.execute(
        """
        SELECT COUNT(*) AS rows, COUNT(DISTINCT revision_version) AS revisions,
               MIN(revision_version) AS revision, COUNT(DISTINCT last_sync_id) AS syncs,
               MIN(last_sync_id) AS sync_id
        FROM timo_external_revenue_daily
        """
    ).fetchone()
    assert dict(lineage) == {
        'rows': 2,
        'revisions': 1,
        'revision': 2,
        'syncs': 1,
        'sync_id': 'sync-2',
    }
    assert conn.execute(
        "SELECT COUNT(*) FROM timo_revenue_changes WHERE sync_id='sync-2' AND change_type='lineage'"
    ).fetchone()[0] == 2
    conn.close()


def test_older_source_snapshot_cannot_overwrite_newer_accepted_revision(tmp_path):
    connect = _connect_factory(tmp_path)
    _materialize(
        connect,
        sync_id='newer-sync',
        rows=_rows(),
        provisional=False,
        snapshot_at='2026-07-23T08:15:00+00:00',
    )

    with pytest.raises(TimoIncrementalSyncError) as exc_info:
        _materialize(
            connect,
            sync_id='older-sync',
            rows=_rows(second_income=99),
            provisional=False,
            snapshot_at='2026-07-23T08:00:00+00:00',
        )
    assert exc_info.value.code == 'stale_source_snapshot'

    conn = connect()
    assert conn.execute(
        "SELECT total_income FROM timo_external_revenue_daily WHERE timo_id='1002'"
    ).fetchone()[0] == 20
    assert dict(conn.execute(
        "SELECT status,error_code FROM timo_sync_run_log WHERE sync_id='older-sync'"
    ).fetchone()) == {'status': 'failed', 'error_code': 'stale_source_snapshot'}
    assert conn.execute(
        "SELECT source_snapshot_at FROM timo_sync_watermark"
    ).fetchone()[0] == '2026-07-23T08:15:00+00:00'
    conn.close()


def test_bi_view_is_read_only_projection_with_join_time(tmp_path):
    connect = _connect_factory(tmp_path)
    conn = connect()
    conn.execute(
        """
        INSERT INTO timo_external_streamers(
            guild_executor_key, timo_id, joined_guild_at_bj, timo_registered_at_bj
        ) VALUES ('guild-br', '1001', '2026-07-23 10:00:00', '')
        """
    )
    conn.commit()
    conn.close()
    _materialize(connect, sync_id='sync-view', rows=_rows())
    conn = connect()
    row = conn.execute(
        """
        SELECT timo_id, joined_guild_at_bj, total_income, revision_version, last_sync_id
        FROM bi_timo_revenue_view
        WHERE timo_id='1001'
        """
    ).fetchone()
    assert dict(row) == {
        'timo_id': '1001',
        'joined_guild_at_bj': '2026-07-23 10:00:00',
        'total_income': 10.0,
        'revision_version': 1,
        'last_sync_id': 'sync-view',
    }
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO bi_timo_revenue_view(timo_id) VALUES ('x')")
    conn.close()


def test_retry_worker_only_returns_latest_due_failed_scope(tmp_path):
    connect = _connect_factory(tmp_path)
    conn = connect()
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    for sync_id, start_time, status, attempt, next_retry_at in (
        ('failed-1', '2026-07-23T08:00:00+00:00', 'failed', 1, past),
        ('failed-2', '2026-07-23T08:05:00+00:00', 'failed', 2, past),
    ):
        conn.execute(
            """
            INSERT INTO timo_sync_run_log(
                sync_id, parent_run_id, idempotency_key, guild_executor_key, guild_name,
                country, stat_date_bj, data_status, start_time, status, retry_attempt,
                next_retry_at, created_at, updated_at
            ) VALUES (?, '', ?, 'guild-br', 'BR', 'Brazil', '2026-07-23',
                      'provisional', ?, ?, ?, ?, ?, ?)
            """,
            (
                sync_id,
                sync_id,
                start_time,
                status,
                attempt,
                next_retry_at,
                start_time,
                start_time,
            ),
        )
    conn.commit()
    conn.close()
    service = SimpleNamespace(db=SimpleNamespace(connect=connect))
    assert due_retry_dates(service, max_dates=1) == ['2026-07-23']

    conn = connect()
    conn.execute(
        """
        INSERT INTO timo_sync_run_log(
            sync_id, parent_run_id, idempotency_key, guild_executor_key, guild_name,
            country, stat_date_bj, data_status, start_time, status, created_at, updated_at
        ) VALUES (
            'success-3', '', 'success-3', 'guild-br', 'BR', 'Brazil', '2026-07-23',
            'provisional', '2026-07-23T08:10:00+00:00', 'success',
            '2026-07-23T08:10:00+00:00', '2026-07-23T08:10:00+00:00'
        )
        """
    )
    conn.commit()
    conn.close()
    assert due_retry_dates(service, max_dates=1) == []


def test_retry_worker_schedules_latest_unacknowledged_publication_once(tmp_path, monkeypatch):
    connect = _connect_factory(tmp_path)
    conn = connect()
    conn.execute(
        """
        INSERT INTO timo_sync_watermark(
            guild_executor_key, guild_name, country, stat_date_bj, data_status,
            last_success_time, last_success_sync_id, row_count, total_income,
            checksum, revision_version, source_snapshot_at
        ) VALUES (
            'guild-id', 'TIMO001', 'Indonesia', '2026-07-24', 'complete',
            '2026-07-24T08:00:00+00:00', 'sync-id', 1, 10,
            ?, 1, '2026-07-24T08:00:00+00:00'
        )
        """,
        ('a' * 64,),
    )
    conn.commit()
    conn.close()
    service = SimpleNamespace(db=SimpleNamespace(connect=connect))
    monkeypatch.setattr(
        retry_worker,
        '_publication_lineage',
        lambda conn, data_date: {
            'TIMO001': {
                'checksum': 'a' * 64,
                'revision': 1,
                'source_generation': 'sync-id',
            },
        },
    )
    ack_path = tmp_path / 'notification-ack.json'

    assert due_retry_dates(
        service,
        max_dates=1,
        notification_ack_path=ack_path,
    ) == ['2026-07-24']

    ack_path.write_text(
        json.dumps({'scope_lineage': {
            'TIMO001': {
                'checksum': 'a' * 64,
                'revision': 1,
                'source_generation': 'sync-id',
            },
        }}),
        encoding='utf-8',
    )
    assert due_retry_dates(
        service,
        max_dates=1,
        notification_ack_path=ack_path,
    ) == []


def test_retry_worker_due_check_matches_batch_runner_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(
        retry_worker,
        'due_retry_dates',
        lambda service, max_dates, notification_ack_path: ['2026-07-24'],
    )
    args = SimpleNamespace(
        db_path=str(tmp_path / 'automation.db'),
        max_dates=1,
        notification_ack_path=tmp_path / 'ack.json',
    )
    assert retry_worker._check_due(args) == {
        'ok': True,
        'status': 'due',
        'due_dates': ['2026-07-24'],
    }

    monkeypatch.setattr(
        retry_worker,
        'due_retry_dates',
        lambda service, max_dates, notification_ack_path: [],
    )
    assert retry_worker._check_due(args) == {
        'ok': True,
        'status': 'idle',
        'due_dates': [],
    }


def test_source_not_ready_keeps_cross_window_retry_after_normal_limit(tmp_path):
    connect = _connect_factory(tmp_path)
    conn = connect()
    result = record_timo_sync_attempt_failure(
        conn,
        sync_id='source-not-ready-5',
        parent_run_id='parent-5',
        guild_executor_key='guild-id',
        guild_name='TIMO001',
        country='Indonesia',
        stat_date_bj='2026-07-26',
        provisional=False,
        error_code='source_not_ready',
        error='source_not_ready:TIMO001:2026-07-26:empty_effective_revenue',
        retry_attempt=5,
        persistent_retry=True,
    )
    assert result['next_retry_at']
    due_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    conn.execute(
        "UPDATE timo_sync_run_log SET next_retry_at=? WHERE sync_id='source-not-ready-5'",
        (due_at,),
    )
    conn.commit()
    service = SimpleNamespace(db=SimpleNamespace(connect=connect))
    assert due_retry_dates(service, max_dates=1) == ['2026-07-26']

    ordinary = record_timo_sync_attempt_failure(
        conn,
        sync_id='ordinary-5',
        parent_run_id='parent-5',
        guild_executor_key='guild-br',
        guild_name='BR',
        country='Brazil',
        stat_date_bj='2026-07-26',
        provisional=False,
        error_code='upstream_failed',
        error='upstream_failed',
        retry_attempt=5,
    )
    assert ordinary['next_retry_at'] == ''

    scheduled = schedule_timo_sync_retry(
        conn,
        sync_id='source-not-ready-5',
        attempt=6,
        persistent_retry=True,
        now=datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc),
    )
    assert scheduled == '2026-07-27T09:30:00+00:00'
    conn.close()
