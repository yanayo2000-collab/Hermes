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
        conn.execute('CREATE TABLE streamer_external_revenue_daily(app_name TEXT,guild_name TEXT,country TEXT,stat_date_bj TEXT,total_income REAL)')
        conn.executemany('INSERT INTO streamer_external_revenue_daily VALUES(?,?,?,?,?)', [
            ('linky', 'Nova', 'Indonesia', data_date, 100.5),
            ('linky', 'Nova', 'Indonesia', data_date, 20.0),
            ('linky', 'Carote', 'Indonesia', data_date, 30.0),
        ])
    return analytics, source, data_date


def test_load_event_requires_ready_d1_and_builds_stable_scope_checksum(tmp_path: Path) -> None:
    analytics, source, data_date = _databases(tmp_path)
    event = load_event(analytics, source, expected_date=data_date)
    assert event['eventType'] == 'linky.materialization.completed'
    assert event['dataDate'] == data_date
    assert event['eventId'].startswith(f'linky:{data_date}:')
    assert len(event['checksum']) == 64
    assert event['scopes'] == [
        {'guildName': 'Carote', 'country': 'Indonesia', 'rowCount': 1, 'totalIncome': '30.000000', 'qualityStatus': 'passed', 'consumable': True},
        {'guildName': 'Nova', 'country': 'Indonesia', 'rowCount': 2, 'totalIncome': '120.500000', 'qualityStatus': 'passed', 'consumable': True},
    ]


def test_load_event_fails_closed_for_stale_analytics(tmp_path: Path) -> None:
    analytics, source, data_date = _databases(tmp_path)
    with sqlite3.connect(analytics) as conn:
        conn.execute("UPDATE streamer_analytics_materialization_state SET data_as_of='2026-01-01'")
    with pytest.raises(ValueError, match='materialization_date_not_ready'):
        load_event(analytics, source, expected_date=data_date)


def test_load_event_accepts_complete_zero_income_scope(tmp_path: Path) -> None:
    analytics, source, data_date = _databases(tmp_path)
    with sqlite3.connect(source) as conn:
        conn.execute(
            'INSERT INTO streamer_external_revenue_daily VALUES(?,?,?,?,?)',
            ('linky', 'Zero Day', 'Brazil', data_date, 0),
        )
    event = load_event(analytics, source, expected_date=data_date)
    zero_scope = next(scope for scope in event['scopes'] if scope['guildName'] == 'Zero Day')
    assert zero_scope == {
        'guildName': 'Zero Day', 'country': 'Brazil', 'rowCount': 1,
        'totalIncome': '0.000000', 'qualityStatus': 'passed', 'consumable': True,
    }


def test_load_event_rejects_negative_income_scope(tmp_path: Path) -> None:
    analytics, source, data_date = _databases(tmp_path)
    with sqlite3.connect(source) as conn:
        conn.execute(
            'INSERT INTO streamer_external_revenue_daily VALUES(?,?,?,?,?)',
            ('linky', 'Invalid Negative', 'Brazil', data_date, -1),
        )
    with pytest.raises(ValueError, match='materialization_scope_invalid'):
        load_event(analytics, source, expected_date=data_date)


def test_ack_state_is_atomic_and_reusable(tmp_path: Path) -> None:
    state = tmp_path / 'state.json'
    _write_state(state, {'event_id': 'linky:test', 'acknowledged_at': 'now'})
    assert _read_state(state) == {'acknowledged_at': 'now', 'event_id': 'linky:test'}
    assert json.loads(state.read_text())['event_id'] == 'linky:test'
    assert state.stat().st_mode & 0o777 == 0o600
