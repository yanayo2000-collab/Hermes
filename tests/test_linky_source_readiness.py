from datetime import date
import sqlite3

import pytest

from app.streamer_external_sync import _assert_linky_streamer_stat_ready


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.execute(
        """
        CREATE TABLE streamer_external_guild_revenue_daily (
            app_name TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL,
            stat_date_bj TEXT NOT NULL,
            source_row_count INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        'INSERT INTO streamer_external_guild_revenue_daily VALUES(?,?,?,?)',
        ('linky', 'linky:cms_guild_sid:42569347', '2026-08-17', 37893),
    )
    return conn


def test_established_linky_guild_rejects_catastrophically_incomplete_source() -> None:
    with _connection() as conn:
        with pytest.raises(RuntimeError, match=r'linky_guild_source_not_ready:current=0:previous=37893'):
            _assert_linky_streamer_stat_ready(
                conn, 'linky:cms_guild_sid:42569347', date(2026, 8, 18), 0,
            )


def test_established_linky_guild_accepts_complete_source_with_small_natural_drift() -> None:
    with _connection() as conn:
        _assert_linky_streamer_stat_ready(
            conn, 'linky:cms_guild_sid:42569347', date(2026, 8, 18), 37893,
        )


def test_new_linky_guild_does_not_invent_a_historical_baseline() -> None:
    with _connection() as conn:
        _assert_linky_streamer_stat_ready(
            conn, 'linky:cms_guild_sid:new', date(2026, 8, 18), 0,
        )
