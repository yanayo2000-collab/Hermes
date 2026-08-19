from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import sqlite3

import pytest

from scripts.notify_linky_materialization import _read_state, _write_state, load_event


def _databases(tmp_path: Path) -> tuple[Path, Path, str]:
    data_date = (date.today() - timedelta(days=1)).isoformat()
    analytics = tmp_path / 'analytics.db'
    source = tmp_path / 'source.db'
    with sqlite3.connect(analytics) as conn:
        conn.execute('CREATE TABLE streamer_analytics_materialization_state(app_name TEXT,status TEXT,data_as_of TEXT,materialized_at TEXT)')
        conn.execute('INSERT INTO streamer_analytics_materialization_state VALUES(?,?,?,?)',
                     ('linky', 'ready', data_date, '2026-08-17T10:47:49+08:00'))
    with sqlite3.connect(source) as conn:
        conn.execute('CREATE TABLE guild_executors(app_name TEXT,guild_name TEXT,enabled INTEGER)')
        conn.execute('CREATE TABLE streamer_external_sync_runs(run_id TEXT,app_name TEXT,date_from TEXT,date_to TEXT,status TEXT,run_scope TEXT,created_at TEXT)')
        conn.execute('CREATE TABLE streamer_external_revenue_daily(app_name TEXT,guild_executor_key TEXT,guild_name TEXT,country TEXT,stat_date_bj TEXT,total_income REAL)')
        conn.execute('CREATE TABLE streamer_external_guild_revenue_daily(app_name TEXT,guild_executor_key TEXT,guild_name TEXT,country TEXT,stat_date_bj TEXT,total_income REAL,source_row_count INTEGER,snapshot_at TEXT)')
        conn.execute(
            'INSERT INTO streamer_external_sync_runs VALUES(?,?,?,?,?,?,?)',
            ('run-complete', 'linky', data_date, data_date, 'success', 'full', '2026-08-17T02:05:00+00:00'),
        )
        conn.executemany('INSERT INTO guild_executors VALUES(?,?,?)', [
            ('linky', 'Nova', 1),
            ('linky', 'Carote', 1),
        ])
        conn.executemany('INSERT INTO streamer_external_revenue_daily VALUES(?,?,?,?,?,?)', [
            ('linky', 'linky:nova', 'Nova', 'Indonesia', data_date, 100.5),
            ('linky', 'linky:nova', 'Nova', 'Indonesia', data_date, 20.0),
            ('linky', 'linky:carote', 'Carote', 'Indonesia', data_date, 30.0),
        ])
        conn.executemany('INSERT INTO streamer_external_guild_revenue_daily VALUES(?,?,?,?,?,?,?,?)', [
            ('linky', 'linky:nova', 'Nova', 'Indonesia', data_date, 120.5, 40000, '2026-08-17T02:00:00+00:00'),
            ('linky', 'linky:carote', 'Carote', 'Indonesia', data_date, 30.0, 33000, '2026-08-17T02:01:00+00:00'),
        ])
    return analytics, source, data_date


def test_load_event_requires_ready_d1_and_builds_stable_scope_checksum(tmp_path: Path) -> None:
    analytics, source, data_date = _databases(tmp_path)
    event = load_event(analytics, source, expected_date=data_date)
    assert event['eventType'] == 'linky.materialization.completed'
    assert event['dataDate'] == data_date
    assert event['businessDate'] == data_date
    assert event['status'] == 'ready'
    assert event['ready'] is True
    assert event['scopeTotal'] == 2
    assert event['scopeSucceeded'] == 2
    assert event['scopeFailed'] == 0
    assert event['failedScopes'] == []
    assert event['materializedAt'] == '2026-08-17T10:47:49+08:00'
    assert len(event['sourceGeneration']) == 20
    assert event['eventId'].startswith(f'linky:{data_date}:')
    assert len(event['checksum']) == 64
    assert [(scope['guildName'], scope['rowCount'], scope['sourceRowCount'], scope['totalIncome']) for scope in event['scopes']] == [
        ('Carote', 1, 33000, '30.000000'),
        ('Nova', 2, 40000, '120.500000'),
    ]
    assert all(scope['qualityStatus'] == 'passed' for scope in event['scopes'])
    assert all(scope['consumable'] is True for scope in event['scopes'])
    assert all(len(scope['checksum']) == 64 for scope in event['scopes'])
    assert all(len(scope['sourceGeneration']) == 20 for scope in event['scopes'])


def test_load_event_fails_closed_for_stale_analytics(tmp_path: Path) -> None:
    analytics, source, data_date = _databases(tmp_path)
    with sqlite3.connect(analytics) as conn:
        conn.execute("UPDATE streamer_analytics_materialization_state SET data_as_of='2026-01-01'")
    with pytest.raises(ValueError, match='materialization_date_not_ready'):
        load_event(analytics, source, expected_date=data_date)


def test_load_event_rejects_complete_zero_income_scope(tmp_path: Path) -> None:
    analytics, source, data_date = _databases(tmp_path)
    with sqlite3.connect(source) as conn:
        conn.execute('INSERT INTO guild_executors VALUES(?,?,?)', ('linky', 'Zero Day', 1))
        conn.execute(
            'INSERT INTO streamer_external_revenue_daily VALUES(?,?,?,?,?,?)',
            ('linky', 'linky:zero', 'Zero Day', 'Brazil', data_date, 0),
        )
        conn.execute(
            'INSERT INTO streamer_external_guild_revenue_daily VALUES(?,?,?,?,?,?,?,?)',
            ('linky', 'linky:zero', 'Zero Day', 'Brazil', data_date, 0, 100, '2026-08-17T02:02:00+00:00'),
        )
    with pytest.raises(ValueError, match='materialization_scope_invalid'):
        load_event(analytics, source, expected_date=data_date)


def test_load_event_rejects_negative_income_scope(tmp_path: Path) -> None:
    analytics, source, data_date = _databases(tmp_path)
    with sqlite3.connect(source) as conn:
        conn.execute('INSERT INTO guild_executors VALUES(?,?,?)', ('linky', 'Invalid Negative', 1))
        conn.execute(
            'INSERT INTO streamer_external_revenue_daily VALUES(?,?,?,?,?,?)',
            ('linky', 'linky:negative', 'Invalid Negative', 'Brazil', data_date, -1),
        )
        conn.execute(
            'INSERT INTO streamer_external_guild_revenue_daily VALUES(?,?,?,?,?,?,?,?)',
            ('linky', 'linky:negative', 'Invalid Negative', 'Brazil', data_date, -1, 100, '2026-08-17T02:03:00+00:00'),
        )
    with pytest.raises(ValueError, match='materialization_scope_invalid'):
        load_event(analytics, source, expected_date=data_date)


def test_load_event_rejects_missing_enabled_guild_scope(tmp_path: Path) -> None:
    analytics, source, data_date = _databases(tmp_path)
    with sqlite3.connect(source) as conn:
        conn.execute('INSERT INTO guild_executors VALUES(?,?,?)', ('linky', 'Missing Guild', 1))
    with pytest.raises(ValueError, match='materialization_scope_incomplete'):
        load_event(analytics, source, expected_date=data_date)


def test_load_event_rejects_latest_incomplete_full_source_run(tmp_path: Path) -> None:
    analytics, source, data_date = _databases(tmp_path)
    with sqlite3.connect(source) as conn:
        conn.execute(
            'INSERT INTO streamer_external_sync_runs VALUES(?,?,?,?,?,?,?)',
            ('run-partial', 'linky', data_date, data_date, 'partial', 'full', '2026-08-17T02:10:00+00:00'),
        )
    with pytest.raises(ValueError, match='materialization_source_run_not_complete'):
        load_event(analytics, source, expected_date=data_date)


def test_load_event_accepts_newer_complete_composite_after_partial_full_run(tmp_path: Path) -> None:
    analytics, source, data_date = _databases(tmp_path)
    with sqlite3.connect(source) as conn:
        conn.execute(
            'UPDATE streamer_external_sync_runs SET status=?, created_at=? WHERE run_id=?',
            ('partial', '2026-08-17T02:10:00+00:00', 'run-complete'),
        )
        conn.execute(
            'INSERT INTO streamer_external_sync_runs VALUES(?,?,?,?,?,?,?)',
            ('run-composite', 'linky', data_date, data_date, 'success', 'composite',
             '2026-08-17T02:20:00+00:00'),
        )
    event = load_event(analytics, source, expected_date=data_date)
    assert event['ready'] is True
    assert event['scopeTotal'] == 2


def test_load_event_rejects_source_snapshot_newer_than_analytics(tmp_path: Path) -> None:
    analytics, source, data_date = _databases(tmp_path)
    with sqlite3.connect(source) as conn:
        conn.execute(
            "UPDATE streamer_external_guild_revenue_daily SET snapshot_at='2026-08-17T03:00:00+00:00' "
            "WHERE guild_name='Nova'",
        )
    with pytest.raises(ValueError, match='materialization_source_newer_than_analytics'):
        load_event(analytics, source, expected_date=data_date)


def test_ack_state_is_atomic_and_reusable(tmp_path: Path) -> None:
    state = tmp_path / 'state.json'
    _write_state(state, {'event_id': 'linky:test', 'acknowledged_at': 'now'})
    assert _read_state(state) == {'acknowledged_at': 'now', 'event_id': 'linky:test'}
    assert json.loads(state.read_text())['event_id'] == 'linky:test'
    assert state.stat().st_mode & 0o777 == 0o600
