from __future__ import annotations

import sqlite3
from datetime import date

from app.linky_source_readiness import persisted_linky_scope_ready


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE guild_executors (
            guild_name TEXT NOT NULL,
            cms_guild_sid TEXT,
            cms_guild_id TEXT,
            enabled INTEGER NOT NULL,
            app_name TEXT NOT NULL
        );
        CREATE TABLE streamer_external_sync_runs (
            run_id TEXT PRIMARY KEY,
            app_name TEXT NOT NULL,
            date_from TEXT NOT NULL,
            date_to TEXT NOT NULL,
            status TEXT NOT NULL,
            run_scope TEXT NOT NULL,
            guild_count INTEGER,
            profile_count INTEGER,
            revenue_count INTEGER,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE streamer_ingestion_run_scopes (
            run_id TEXT NOT NULL,
            app_name TEXT NOT NULL,
            dataset TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL,
            business_date TEXT,
            status TEXT NOT NULL,
            updated_at TEXT
        );
        CREATE TABLE streamer_external_guild_revenue_daily (
            app_name TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL,
            stat_date_bj TEXT NOT NULL,
            source_row_count INTEGER NOT NULL
        );
        """
    )
    return conn


def _seed_scope(conn: sqlite3.Connection, *, current_count: int) -> None:
    executor_key = 'linky:cms_guild_sid:br-evian'
    conn.execute(
        "INSERT INTO guild_executors VALUES ('BR-EVIAN','br-evian','',1,'linky')"
    )
    conn.execute(
        "INSERT INTO streamer_external_sync_runs VALUES "
        "('run-1','linky','2026-08-18','2026-08-18','success','full',1,1,1,'2026-08-19T02:16:47+00:00')"
    )
    conn.executemany(
        "INSERT INTO streamer_ingestion_run_scopes VALUES (?,?,?,?,?,?,?)",
        [
            ('run-1', 'linky', 'anchor_directory', executor_key, '', 'success', '2026-08-19T02:16:00+00:00'),
            ('run-1', 'linky', 'streamer_stat', executor_key, '2026-08-18', 'success', '2026-08-19T02:16:47+00:00'),
        ],
    )
    conn.executemany(
        "INSERT INTO streamer_external_guild_revenue_daily VALUES (?,?,?,?)",
        [
            ('linky', executor_key, '2026-08-17', 47000),
            ('linky', executor_key, '2026-08-18', current_count),
        ],
    )


def test_collapsed_persisted_scope_is_not_reusable() -> None:
    conn = _connection()
    _seed_scope(conn, current_count=0)

    assert persisted_linky_scope_ready(
        conn,
        executor_key='linky:cms_guild_sid:br-evian',
        target_date=date(2026, 8, 18),
    ) is False


def test_persisted_scope_above_readiness_floor_is_reusable() -> None:
    conn = _connection()
    _seed_scope(conn, current_count=37893)

    assert persisted_linky_scope_ready(
        conn,
        executor_key='linky:cms_guild_sid:br-evian',
        target_date=date(2026, 8, 18),
    ) is True
