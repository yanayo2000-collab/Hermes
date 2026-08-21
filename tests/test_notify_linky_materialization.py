from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from scripts.notify_linky_materialization import (
    _acknowledgements,
    _candidate_dates,
    canonical_json,
    _read_state,
    _write_state,
    load_event,
    main,
)


def test_scope_checksum_matches_nova_node_canonical_contract() -> None:
    scopes = [{
        'guildName': 'BR-EVIAN',
        'country': 'Brazil',
        'rowCount': 37893,
        'totalIncome': '826591.000000',
        'sourceRowCount': 37893,
        'sourceTotalIncome': '826591.000000',
        'qualityStatus': 'passed',
        'consumable': True,
        'materializedAt': '2026-08-19T16:06:09+08:00',
        'sourceGeneration': '0123456789abcdefabcd',
        'checksum': 'a' * 64,
    }]
    assert hashlib.sha256(canonical_json(scopes).encode('utf-8')).hexdigest() == (
        '3848ee5a67da1e8266c3cde02809aa1abbe5417bba9a4f376142cd668150b155'
    )


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
    assert event['checksum'] == hashlib.sha256(
        canonical_json(event['scopes']).encode('utf-8')
    ).hexdigest()


def test_load_event_accepts_history_covered_by_a_newer_ready_generation(tmp_path: Path) -> None:
    analytics, source, latest_date = _databases(tmp_path)
    historical_date = (date.fromisoformat(latest_date) - timedelta(days=1)).isoformat()
    with sqlite3.connect(source) as conn:
        conn.execute(
            'INSERT INTO streamer_external_sync_runs VALUES(?,?,?,?,?,?,?)',
            ('run-historical', 'linky', historical_date, historical_date, 'success', 'full',
             '2026-08-16T02:05:00+00:00'),
        )
        conn.executemany('INSERT INTO streamer_external_revenue_daily VALUES(?,?,?,?,?,?)', [
            ('linky', 'linky:nova', 'Nova', 'Indonesia', historical_date, 80.0),
            ('linky', 'linky:carote', 'Carote', 'Indonesia', historical_date, 20.0),
        ])
        conn.executemany('INSERT INTO streamer_external_guild_revenue_daily VALUES(?,?,?,?,?,?,?,?)', [
            ('linky', 'linky:nova', 'Nova', 'Indonesia', historical_date, 80.0, 39000,
             '2026-08-16T02:00:00+00:00'),
            ('linky', 'linky:carote', 'Carote', 'Indonesia', historical_date, 20.0, 32000,
             '2026-08-16T02:01:00+00:00'),
        ])
    assert load_event(analytics, source, expected_date=historical_date)['dataDate'] == historical_date


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


def test_ack_state_migrates_the_legacy_last_ack_without_assuming_contiguous_days() -> None:
    acknowledgements = _acknowledgements({
        'event_id': 'linky:2026-08-20:run',
        'data_date': '2026-08-20',
        'acknowledged_at': '2026-08-21T02:37:36+00:00',
        'duplicate': False,
    })
    assert list(acknowledgements) == ['2026-08-20']
    assert acknowledgements['2026-08-20']['event_id'] == 'linky:2026-08-20:run'
    assert '2026-08-19' not in acknowledgements


def test_candidate_dates_are_bounded_and_oldest_first() -> None:
    assert _candidate_dates(date(2026, 8, 21), 3) == [
        '2026-08-18', '2026-08-19', '2026-08-20',
    ]
    with pytest.raises(ValueError, match='invalid_catchup_window'):
        _candidate_dates(date(2026, 8, 21), 0)
    with pytest.raises(ValueError, match='invalid_catchup_window'):
        _candidate_dates(date(2026, 8, 21), 32)


def test_main_scans_missing_dates_and_persists_each_ack(monkeypatch: pytest.MonkeyPatch,
                                                        tmp_path: Path) -> None:
    analytics, source, data_date = _databases(tmp_path)
    secret = tmp_path / 'secret'
    secret.write_text('x' * 48)
    state = tmp_path / 'state.json'
    calls: list[str] = []

    def fake_send(event: dict[str, object], **_kwargs: object) -> dict[str, object]:
        calls.append(str(event['dataDate']))
        return {'ok': True, 'event_id': event['eventId'], 'duplicate': False}

    monkeypatch.setattr('scripts.notify_linky_materialization.send_event', fake_send)
    monkeypatch.setattr(sys, 'argv', [
        'notify_linky_materialization.py',
        '--analytics-path', str(analytics),
        '--source-db-path', str(source),
        '--secret-file', str(secret),
        '--state-path', str(state),
        '--lookback-days', '2',
    ])
    assert main() == 0
    assert calls == [data_date]
    stored = _read_state(state)
    assert stored['schema_version'] == 2
    assert list(stored['acknowledgements']) == [data_date]

    assert main() == 0
    assert calls == [data_date]
