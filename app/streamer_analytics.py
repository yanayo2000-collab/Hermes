from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import sys
import threading
import time
from collections import OrderedDict, defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from itertools import groupby, islice
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.streamer_app_fan import (
    APP_FAN_COVERAGE_START,
    app_fan_history_complete,
    classify_app_fan_status,
    confirmed_app_fan_ids,
    reconcile_streamer_app_fans,
)
from app.streamer_data_foundation import (
    ensure_streamer_foundation_tables,
    ensure_streamer_foundation_views,
)
from app.sqlite_observability import connect_observed_sqlite
from app.sqlite_write_window import connect_short_write_sqlite

SUPPORTED_APPS = ('timo', 'linky', 'sugo')
APP_LABELS = {'timo': 'Timo', 'linky': 'Linky', 'sugo': 'Sugo'}
APP_ALIASES = {'timol': 'timo', 'inky': 'linky', 'sogo': 'sugo'}

APP_CAPABILITIES = {
    'timo': {
        'streamer_profile': True,
        'daily_revenue': True,
        'newcomer_revenue': True,
        'revenue_retention': True,
        'source_status': 'ready',
        'source_note': '已接入主播名册与主播日收益，可计算收益和收益活跃留存。',
    },
    'linky': {
        'streamer_profile': True,
        'daily_revenue': True,
        'newcomer_revenue': True,
        'revenue_retention': True,
        'source_status': 'pending',
        'source_note': '已接入 Linky 公会后台 OAuth 主播日收益接口，等待首次历史补采。',
    },
    'sugo': {
        'streamer_profile': True,
        'daily_revenue': False,
        'newcomer_revenue': False,
        'revenue_retention': False,
        'source_status': 'partial',
        'source_note': '已纳入成功绑定主播；平台尚未落库全量主播日收益，收益和收益活跃留存暂不可计算。',
    },
}

PROFILE_VIEW = 'streamer_analytics_profiles_v5'
DAILY_FACT_VIEW = 'streamer_analytics_daily_facts_v11'
TIMO_DIAMONDS_PER_USD = 20000.0
RETENTION_DAY_OFFSETS = {1: 1, 7: 7, 30: 30}
PLATFORM_INCOME_UNITS_PER_USD = {
    'timo': TIMO_DIAMONDS_PER_USD,
    'linky': 5000.0,
    'sugo': None,
}
TIMO_COHORT_PERIOD_COUNT = 12
TIMO_COHORT_DISPLAY_WEEKS = 12


class StreamerAnalyticsCohortSnapshotUnavailable(RuntimeError):
    """A materialized cohort cache is present but cannot be decoded safely."""
STREAMER_ANALYTICS_MAX_RANGE_DAYS = 365
NEWCOMER_COMPLETE_WINDOW_DAYS = 30
LINKY_SQLITE_SOURCE_CACHE_KIB = 32768
LINKY_SQLITE_PUBLISH_CACHE_KIB = 16384
LINKY_SQLITE_TEMP_CACHE_KIB = 16384
LINKY_SQLITE_THREADS = 1
LINKY_OFFLINE_PUBLISH_BATCH_SIZE = 2048
LINKY_PRODUCTION_BATCH_SLICE = 'mcn-batch-linky.slice'
PRODUCTION_PROJECT_ROOT = Path('/opt/mcn-ai-automation')
STREAMER_ANALYTICS_SUPPORT_COPY_BATCH_SIZE = 2048
STREAMER_ANALYTICS_PUBLISH_WRITE_WINDOW_SECONDS = 300.0
STREAMER_ANALYTICS_PAYLOAD_CACHE_TTL_SECONDS = 3600.0
STREAMER_ANALYTICS_PAYLOAD_CACHE_MAX_ENTRIES = 96
STREAMER_SYNC_RUNNING_STALE_SECONDS = 2 * 60 * 60
STREAMER_ANALYTICS_INHERITED_QUEUE_LOCK_PATHS = (
    '/tmp/mcn-ai-automation-sqlite-job-locks/sqlite-etl.lock',
    '/tmp/mcn-ai-automation-sqlite-job-locks/sqlite-writer.lock',
)
_STREAMER_ANALYTICS_PAYLOAD_CACHE: OrderedDict[tuple[Any, ...], tuple[float, Dict[str, Any]]] = OrderedDict()
_STREAMER_ANALYTICS_PAYLOAD_CACHE_LOCK = threading.Lock()
STREAMER_ANALYTICS_STORE_TABLES = (
    'streamer_analytics_profile_summary',
    'streamer_analytics_streamer_daily_summary',
    'streamer_analytics_daily_summary',
    'streamer_analytics_newcomer_summary',
    'streamer_analytics_timo_cohort_summary',
    'streamer_analytics_linky_cohort_summary',
    'streamer_analytics_materialization_state',
)
STREAMER_ANALYTICS_STORE_SUPPORT_TABLES = (
    'streamer_app_fan_identities',
    'guild_executors',
    'guild_anchor_daily_stats',
    'streamer_external_sync_runs',
    'streamer_external_revenue_daily',
    'streamer_external_guild_revenue_daily',
    'timo_external_revenue_daily',
    'timo_external_sync_runs',
)
LINKY_STREAMER_ANALYTICS_SUPPORT_TABLES = (
    'guild_executors',
    'guild_anchor_daily_stats',
    'streamer_external_sync_runs',
    'streamer_external_revenue_daily',
    'streamer_external_guild_revenue_daily',
)
STREAMER_ANALYTICS_STORE_SUPPORT_SELECTS = {
    # The standalone read store needs freshness coverage, not every raw detail row.
    # Keep one representative row per app/guild/day to avoid duplicating millions
    # of raw income records that remain authoritative in automation.db.
    'streamer_external_revenue_daily': (
        'SELECT source.* FROM streamer_external_revenue_daily AS source '
        'JOIN (SELECT MIN(rowid) AS selected_rowid FROM streamer_external_revenue_daily '
        'GROUP BY app_name, stat_date_bj, guild_name) AS selected '
        'ON selected.selected_rowid = source.rowid'
    ),
    'timo_external_revenue_daily': (
        'SELECT source.* FROM timo_external_revenue_daily AS source '
        'JOIN (SELECT MIN(rowid) AS selected_rowid FROM timo_external_revenue_daily '
        'GROUP BY guild_executor_key, guild_name, stat_date_bj, provisional) AS selected '
        'ON selected.selected_rowid = source.rowid'
    ),
}
STREAMER_ANALYTICS_PROFILE_COLUMNS = (
    'app_name', 'guild_executor_key', 'guild_name', 'country', 'streamer_id',
    'display_name', 'registered_date', 'last_active_date', 'is_real_person',
    'is_app_fan', 'app_fan_status', 'app_fan_source',
    'source_updated_at', 'materialized_at',
)
STREAMER_ANALYTICS_STREAMER_DAILY_COLUMNS = (
    'app_name', 'guild_executor_key', 'guild_name', 'country', 'stat_date',
    'streamer_id', 'total_income', 'is_new', 'is_active', 'materialized_at',
)
STREAMER_ANALYTICS_DAILY_COLUMNS = (
    'app_name', 'guild_executor_key', 'guild_name', 'country', 'stat_date',
    'new_streamers', 'active_streamers', 'total_income',
    'streamer_detail_income', 'materialized_at',
)
STREAMER_ANALYTICS_NEWCOMER_COLUMNS = (
    'app_name', 'guild_executor_key', 'guild_name', 'country', 'streamer_id',
    'registered_date', 'data_as_of', 'income_d1', 'income_d7', 'income_d30',
    'mature_income_d1', 'mature_income_d7', 'mature_income_d30',
    'mature_retention_d1', 'mature_retention_d7', 'mature_retention_d30',
    'retained_d1', 'retained_d7', 'retained_d30', 'materialized_at',
)
STREAMER_ANALYTICS_COHORT_COLUMNS = (
    'scope_type', 'scope_key', 'data_as_of', 'payload_json', 'materialized_at',
)
LINKY_ANALYTICS_EXCLUDED_GUILDS = frozenset()
TIMO_SETTLEMENT_RATES = {
    'mexico': (0.5, 1.8),
    'mx': (0.5, 1.8),
    'chile': (0.5, 1.8),
    'cl': (0.5, 1.8),
    'colombia': (0.3, 1.6),
    'co': (0.3, 1.6),
    'venezuela': (0.3, 1.6),
    've': (0.3, 1.6),
    'argentina': (0.3, 1.6),
    'ar': (0.3, 1.6),
}


def normalize_streamer_app(value: object) -> str:
    normalized = str(value or '').strip().lower()
    normalized = APP_ALIASES.get(normalized, normalized)
    if normalized not in SUPPORTED_APPS:
        raise ValueError('unsupported_streamer_app')
    return normalized


def _linky_guild_is_included(guild_name: object) -> bool:
    return str(guild_name or '').strip() not in LINKY_ANALYTICS_EXCLUDED_GUILDS


def _linky_exclusion_scope(*, alias: str = '') -> tuple[str, List[object]]:
    if not LINKY_ANALYTICS_EXCLUDED_GUILDS:
        return '', []
    prefix = f'{alias}.' if alias else ''
    placeholders = ','.join('?' for _ in LINKY_ANALYTICS_EXCLUDED_GUILDS)
    return (
        f' AND {prefix}guild_name NOT IN ({placeholders})',
        sorted(LINKY_ANALYTICS_EXCLUDED_GUILDS),
    )


def ensure_streamer_analytics_views(conn: sqlite3.Connection) -> None:
    """Create the read-only unified App -> guild -> streamer -> day layer."""
    ensure_streamer_foundation_tables(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS streamer_external_sync_runs (
            run_id TEXT PRIMARY KEY,
            app_name TEXT NOT NULL,
            date_from TEXT NOT NULL,
            date_to TEXT NOT NULL,
            status TEXT NOT NULL,
            run_scope TEXT NOT NULL DEFAULT 'legacy',
            scope_key TEXT NOT NULL DEFAULT '',
            guild_count INTEGER NOT NULL DEFAULT 0,
            profile_count INTEGER NOT NULL DEFAULT 0,
            revenue_count INTEGER NOT NULL DEFAULT 0,
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS timo_external_revenue_weekly (
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            week_start_bj TEXT NOT NULL,
            week_end_bj TEXT NOT NULL,
            timo_id TEXT NOT NULL,
            user_uuid TEXT NOT NULL DEFAULT '',
            nickname TEXT NOT NULL DEFAULT '',
            total_income REAL NOT NULL DEFAULT 0,
            source_payload TEXT NOT NULL DEFAULT '{}',
            snapshot_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_executor_key, week_start_bj, timo_id)
        );

        CREATE TABLE IF NOT EXISTS timo_external_revenue_weekly_coverage (
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            week_start_bj TEXT NOT NULL,
            week_end_bj TEXT NOT NULL,
            status TEXT NOT NULL,
            row_count INTEGER NOT NULL DEFAULT 0,
            filename TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_executor_key, week_start_bj)
        );

        CREATE INDEX IF NOT EXISTS idx_timo_external_revenue_weekly_country_week
            ON timo_external_revenue_weekly(country, week_start_bj);
        CREATE INDEX IF NOT EXISTS idx_streamer_external_sync_runs_app_created
            ON streamer_external_sync_runs(app_name, created_at DESC);

        CREATE TABLE IF NOT EXISTS streamer_external_profiles (
            app_name TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            streamer_id TEXT NOT NULL,
            platform_user_id TEXT NOT NULL DEFAULT '',
            platform_character_id TEXT NOT NULL DEFAULT '',
            nickname TEXT NOT NULL DEFAULT '',
            registered_at_bj TEXT NOT NULL DEFAULT '',
            last_active_at_bj TEXT NOT NULL DEFAULT '',
            is_real_person INTEGER NOT NULL DEFAULT 1,
            source_name TEXT NOT NULL,
            source_payload TEXT NOT NULL DEFAULT '{}',
            snapshot_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(app_name, guild_executor_key, streamer_id)
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_external_profiles_app_guild
            ON streamer_external_profiles(app_name, guild_name, registered_at_bj);

        CREATE TABLE IF NOT EXISTS streamer_external_revenue_daily (
            app_name TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            stat_date_bj TEXT NOT NULL,
            streamer_id TEXT NOT NULL,
            nickname TEXT NOT NULL DEFAULT '',
            total_income REAL NOT NULL DEFAULT 0,
            chat_income REAL NOT NULL DEFAULT 0,
            voice_room_income REAL NOT NULL DEFAULT 0,
            video_income REAL NOT NULL DEFAULT 0,
            gift_income REAL NOT NULL DEFAULT 0,
            other_income REAL NOT NULL DEFAULT 0,
            agency_income REAL NOT NULL DEFAULT 0,
            active_days INTEGER NOT NULL DEFAULT 0,
            source_name TEXT NOT NULL,
            source_payload TEXT NOT NULL DEFAULT '{}',
            snapshot_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(app_name, guild_executor_key, stat_date_bj, streamer_id)
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_external_revenue_app_date
            ON streamer_external_revenue_daily(app_name, stat_date_bj, guild_name);

        CREATE TABLE IF NOT EXISTS streamer_external_guild_revenue_daily (
            app_name TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            stat_date_bj TEXT NOT NULL,
            total_income REAL NOT NULL DEFAULT 0,
            chat_income REAL NOT NULL DEFAULT 0,
            voice_room_income REAL NOT NULL DEFAULT 0,
            platform_total_income REAL NOT NULL DEFAULT 0,
            streamer_detail_income REAL NOT NULL DEFAULT 0,
            reconciliation_delta REAL NOT NULL DEFAULT 0,
            source_row_count INTEGER NOT NULL DEFAULT 0,
            streamer_count INTEGER NOT NULL DEFAULT 0,
            source_name TEXT NOT NULL,
            source_payload TEXT NOT NULL DEFAULT '{}',
            snapshot_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(app_name, guild_executor_key, stat_date_bj)
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_external_guild_revenue_app_date
            ON streamer_external_guild_revenue_daily(app_name, stat_date_bj, guild_name);

        CREATE TABLE IF NOT EXISTS streamer_analytics_profile_summary (
            app_name TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            streamer_id TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            registered_date TEXT NOT NULL DEFAULT '',
            last_active_date TEXT NOT NULL DEFAULT '',
            is_real_person INTEGER NOT NULL DEFAULT 0,
            is_app_fan INTEGER,
            app_fan_status TEXT NOT NULL DEFAULT 'historical_unknown',
            app_fan_source TEXT NOT NULL DEFAULT '',
            source_updated_at TEXT NOT NULL DEFAULT '',
            materialized_at TEXT NOT NULL,
            PRIMARY KEY(app_name, guild_executor_key, streamer_id)
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_analytics_profile_scope
            ON streamer_analytics_profile_summary(app_name, country, guild_name, registered_date);

        CREATE TABLE IF NOT EXISTS streamer_analytics_streamer_daily_summary (
            app_name TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            stat_date TEXT NOT NULL,
            streamer_id TEXT NOT NULL,
            total_income REAL NOT NULL DEFAULT 0,
            is_new INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 0,
            materialized_at TEXT NOT NULL,
            PRIMARY KEY(app_name, guild_executor_key, stat_date, streamer_id)
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_analytics_streamer_daily_scope
            ON streamer_analytics_streamer_daily_summary(app_name, country, guild_name, stat_date);
        CREATE INDEX IF NOT EXISTS idx_streamer_analytics_streamer_daily_rank
            ON streamer_analytics_streamer_daily_summary(app_name, guild_executor_key, streamer_id, stat_date);

        CREATE TABLE IF NOT EXISTS streamer_analytics_daily_summary (
            app_name TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            stat_date TEXT NOT NULL,
            new_streamers INTEGER NOT NULL DEFAULT 0,
            active_streamers INTEGER NOT NULL DEFAULT 0,
            total_income REAL NOT NULL DEFAULT 0,
            streamer_detail_income REAL NOT NULL DEFAULT 0,
            materialized_at TEXT NOT NULL,
            PRIMARY KEY(app_name, guild_executor_key, stat_date)
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_analytics_daily_scope
            ON streamer_analytics_daily_summary(app_name, country, guild_name, stat_date);

        CREATE TABLE IF NOT EXISTS streamer_analytics_newcomer_summary (
            app_name TEXT NOT NULL,
            guild_executor_key TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            streamer_id TEXT NOT NULL,
            registered_date TEXT NOT NULL,
            data_as_of TEXT NOT NULL DEFAULT '',
            income_d1 REAL,
            income_d7 REAL,
            income_d30 REAL,
            mature_income_d1 INTEGER NOT NULL DEFAULT 0,
            mature_income_d7 INTEGER NOT NULL DEFAULT 0,
            mature_income_d30 INTEGER NOT NULL DEFAULT 0,
            mature_retention_d1 INTEGER NOT NULL DEFAULT 0,
            mature_retention_d7 INTEGER NOT NULL DEFAULT 0,
            mature_retention_d30 INTEGER NOT NULL DEFAULT 0,
            retained_d1 INTEGER NOT NULL DEFAULT 0,
            retained_d7 INTEGER NOT NULL DEFAULT 0,
            retained_d30 INTEGER NOT NULL DEFAULT 0,
            materialized_at TEXT NOT NULL,
            PRIMARY KEY(app_name, guild_executor_key, streamer_id)
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_analytics_newcomer_scope
            ON streamer_analytics_newcomer_summary(app_name, country, guild_name, registered_date);

        CREATE TABLE IF NOT EXISTS streamer_analytics_timo_cohort_summary (
            scope_type TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            data_as_of TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            materialized_at TEXT NOT NULL,
            PRIMARY KEY(scope_type, scope_key)
        );

        CREATE TABLE IF NOT EXISTS streamer_analytics_linky_cohort_summary (
            scope_type TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            data_as_of TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            materialized_at TEXT NOT NULL,
            PRIMARY KEY(scope_type, scope_key)
        );

        CREATE TABLE IF NOT EXISTS streamer_analytics_materialization_state (
            app_name TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            data_as_of TEXT NOT NULL DEFAULT '',
            profile_count INTEGER NOT NULL DEFAULT 0,
            streamer_daily_count INTEGER NOT NULL DEFAULT 0,
            daily_summary_count INTEGER NOT NULL DEFAULT 0,
            newcomer_count INTEGER NOT NULL DEFAULT 0,
            cohort_scope_count INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            materialized_at TEXT NOT NULL
        );

        CREATE VIEW IF NOT EXISTS streamer_analytics_profiles_v1 AS
        SELECT
            'timo' AS app_name,
            guild_executor_key,
            guild_name,
            timo_id AS streamer_id,
            nickname AS display_name,
            CASE WHEN length(COALESCE(NULLIF(joined_guild_at_bj, ''), registered_at_bj)) >= 10
                 THEN substr(COALESCE(NULLIF(joined_guild_at_bj, ''), registered_at_bj), 1, 10) ELSE '' END AS registered_date,
            CASE WHEN length(last_active_at_bj) >= 10 THEN substr(last_active_at_bj, 1, 10) ELSE '' END AS last_active_date,
            is_real_person,
            updated_at AS source_updated_at
        FROM timo_external_streamers

        UNION ALL

        SELECT
            'linky' AS app_name,
            guild_anchor_seen.guild_executor_key,
            guild_anchor_seen.guild_name,
            guild_anchor_seen.anchor_id AS streamer_id,
            guild_anchor_seen.anchor_name AS display_name,
            guild_anchor_seen.created_date_bj AS registered_date,
            CASE WHEN length(guild_anchor_seen.last_seen_at) >= 10 THEN substr(guild_anchor_seen.last_seen_at, 1, 10) ELSE '' END AS last_active_date,
            guild_anchor_seen.is_real_person,
            guild_anchor_seen.last_seen_at AS source_updated_at
        FROM guild_anchor_seen

        UNION ALL

        SELECT
            'sugo' AS app_name,
            COALESCE(NULLIF(guild_name, ''), 'sugo') AS guild_executor_key,
            COALESCE(NULLIF(guild_name, ''), '未分配公会') AS guild_name,
            parsed_account_id AS streamer_id,
            parsed_account_id AS display_name,
            substr(MIN(created_at), 1, 10) AS registered_date,
            substr(MAX(COALESCE(processed_at, created_at)), 1, 10) AS last_active_date,
            1 AS is_real_person,
            MAX(COALESCE(processed_at, created_at)) AS source_updated_at
        FROM ops_intake_items
        WHERE lower(COALESCE(parsed_app, '')) IN ('sugo', 'sogo')
          AND COALESCE(parsed_account_id, '') <> ''
          AND lower(COALESCE(system_status, '')) IN ('fully_success', 'success', 'verified_success', 'crm_success')
        GROUP BY COALESCE(NULLIF(guild_name, ''), 'sugo'), parsed_account_id;

        CREATE VIEW IF NOT EXISTS streamer_analytics_daily_facts_v1 AS
        SELECT
            'timo' AS app_name,
            revenue.guild_executor_key,
            revenue.guild_name,
            revenue.timo_id AS streamer_id,
            revenue.stat_date_bj AS stat_date,
            CASE WHEN substr(COALESCE(NULLIF(profile.joined_guild_at_bj, ''), profile.registered_at_bj), 1, 10)
                           = revenue.stat_date_bj THEN 1 ELSE 0 END AS is_new,
            CASE WHEN revenue.total_income > 0 OR revenue.online_hours > 0 OR revenue.call_count > 0 THEN 1 ELSE 0 END AS is_active,
            revenue.total_income,
            revenue.qualified_revenue,
            revenue.matching_income,
            revenue.private_message_income,
            revenue.private_gift_income,
            revenue.call_income,
            revenue.online_hours,
            revenue.call_count,
            'timo_external_revenue_daily' AS source_name,
            revenue.updated_at AS source_updated_at
        FROM timo_external_revenue_daily AS revenue
        LEFT JOIN timo_external_streamers AS profile
          ON profile.guild_executor_key = revenue.guild_executor_key
         AND profile.timo_id = revenue.timo_id

        UNION ALL

        SELECT
            profile.app_name,
            profile.guild_executor_key,
            profile.guild_name,
            profile.streamer_id,
            profile.registered_date AS stat_date,
            1 AS is_new,
            NULL AS is_active,
            NULL AS total_income,
            NULL AS qualified_revenue,
            NULL AS matching_income,
            NULL AS private_message_income,
            NULL AS private_gift_income,
            NULL AS call_income,
            NULL AS online_hours,
            NULL AS call_count,
            CASE WHEN profile.app_name = 'linky' THEN 'guild_anchor_seen' ELSE 'ops_intake_items' END AS source_name,
            profile.source_updated_at
        FROM streamer_analytics_profiles_v1 AS profile
        WHERE profile.app_name IN ('linky', 'sugo')
          AND profile.registered_date <> '';

        CREATE VIEW IF NOT EXISTS streamer_analytics_profiles_v3 AS
        SELECT
            'timo' AS app_name, guild_executor_key, guild_name, timo_id AS streamer_id,
            nickname AS display_name,
            CASE WHEN length(COALESCE(NULLIF(joined_guild_at_bj, ''), registered_at_bj)) >= 10
                 THEN substr(COALESCE(NULLIF(joined_guild_at_bj, ''), registered_at_bj), 1, 10) ELSE '' END AS registered_date,
            CASE WHEN length(last_active_at_bj) >= 10 THEN substr(last_active_at_bj, 1, 10) ELSE '' END AS last_active_date,
            is_real_person, updated_at AS source_updated_at
        FROM timo_external_streamers
        UNION ALL
        SELECT
            app_name, guild_executor_key, guild_name, streamer_id, nickname AS display_name,
            CASE WHEN length(registered_at_bj) >= 10 THEN substr(registered_at_bj, 1, 10) ELSE '' END,
            CASE WHEN length(last_active_at_bj) >= 10 THEN substr(last_active_at_bj, 1, 10) ELSE '' END,
            is_real_person, updated_at
        FROM streamer_external_profiles
        WHERE app_name IN ('linky', 'sugo')
        UNION ALL
        SELECT
            'linky', seen.guild_executor_key, seen.guild_name, seen.anchor_id, seen.anchor_name,
            seen.created_date_bj,
            CASE WHEN length(seen.last_seen_at) >= 10 THEN substr(seen.last_seen_at, 1, 10) ELSE '' END,
            seen.is_real_person, seen.last_seen_at
        FROM guild_anchor_seen AS seen
        JOIN guild_executors AS executor
          ON executor.guild_name = seen.guild_name
         AND lower(executor.app_name) = 'linky'
        WHERE NOT EXISTS (
            SELECT 1 FROM streamer_external_profiles AS external
            WHERE external.app_name = 'linky' AND external.guild_executor_key = seen.guild_executor_key
              AND (external.streamer_id = seen.anchor_id OR external.platform_user_id = seen.anchor_id)
        )
        UNION ALL
        SELECT * FROM streamer_analytics_profiles_v1 AS legacy
        WHERE legacy.app_name = 'sugo'
          AND NOT EXISTS (SELECT 1 FROM streamer_external_profiles WHERE app_name = 'sugo');

        CREATE VIEW IF NOT EXISTS streamer_analytics_daily_facts_v3 AS
        SELECT
            'timo' AS app_name, revenue.guild_executor_key, revenue.guild_name,
            revenue.timo_id AS streamer_id, revenue.stat_date_bj AS stat_date,
            CASE WHEN substr(COALESCE(NULLIF(profile.joined_guild_at_bj, ''), profile.registered_at_bj), 1, 10)
                           = revenue.stat_date_bj THEN 1 ELSE 0 END AS is_new,
            CASE WHEN revenue.total_income > 0 OR revenue.online_hours > 0 OR revenue.call_count > 0 THEN 1 ELSE 0 END AS is_active,
            revenue.total_income, revenue.qualified_revenue, revenue.matching_income,
            revenue.private_message_income, revenue.private_gift_income, revenue.call_income,
            revenue.online_hours, revenue.call_count,
            'timo_external_revenue_daily' AS source_name, revenue.updated_at AS source_updated_at
        FROM timo_external_revenue_daily AS revenue
        LEFT JOIN timo_external_streamers AS profile
          ON profile.guild_executor_key = revenue.guild_executor_key AND profile.timo_id = revenue.timo_id
        UNION ALL
        SELECT
            revenue.app_name, revenue.guild_executor_key, revenue.guild_name, revenue.streamer_id,
            revenue.stat_date_bj,
            CASE WHEN substr(profile.registered_at_bj, 1, 10) = revenue.stat_date_bj THEN 1 ELSE 0 END,
            CASE WHEN revenue.total_income <> 0 OR revenue.active_days > 0 THEN 1 ELSE 0 END,
            revenue.total_income, 0, 0, revenue.chat_income, revenue.gift_income,
            revenue.video_income, NULL, NULL, revenue.source_name, revenue.updated_at
        FROM streamer_external_revenue_daily AS revenue
        LEFT JOIN streamer_external_profiles AS profile
          ON profile.app_name = revenue.app_name
         AND profile.guild_executor_key = revenue.guild_executor_key
         AND profile.streamer_id = revenue.streamer_id
        UNION ALL
        SELECT
            profile.app_name, profile.guild_executor_key, profile.guild_name, profile.streamer_id,
            profile.registered_date, 1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
            CASE WHEN profile.app_name = 'linky' THEN 'guild_anchor_seen' ELSE 'ops_intake_items' END,
            profile.source_updated_at
        FROM streamer_analytics_profiles_v3 AS profile
        WHERE profile.app_name IN ('linky', 'sugo') AND profile.registered_date <> ''
          AND NOT EXISTS (
              SELECT 1 FROM streamer_external_revenue_daily AS revenue
              WHERE revenue.app_name = profile.app_name
                AND revenue.guild_executor_key = profile.guild_executor_key
                AND revenue.streamer_id = profile.streamer_id
                AND revenue.stat_date_bj = profile.registered_date
          );

        CREATE VIEW IF NOT EXISTS streamer_analytics_daily_facts_v4 AS
        SELECT facts.*
        FROM streamer_analytics_daily_facts_v3 AS facts
        WHERE facts.app_name <> 'timo'
           OR EXISTS (
                SELECT 1
                FROM timo_external_revenue_daily AS timo_revenue
                WHERE timo_revenue.guild_executor_key = facts.guild_executor_key
                  AND timo_revenue.stat_date_bj = facts.stat_date
                  AND timo_revenue.timo_id = facts.streamer_id
                  AND timo_revenue.provisional = 0
           );

        CREATE VIEW IF NOT EXISTS streamer_analytics_profiles_v4 AS
        SELECT
            profile.app_name,
            profile.guild_executor_key,
            profile.guild_name,
            profile.streamer_id,
            profile.display_name,
            profile.registered_date,
            profile.last_active_date,
            profile.is_real_person,
            COALESCE(
                NULLIF(timo_profile.country, ''),
                NULLIF(external_profile.country, ''),
                NULLIF(executor.country, ''),
                ''
            ) AS country,
            profile.source_updated_at
        FROM streamer_analytics_profiles_v3 AS profile
        LEFT JOIN timo_external_streamers AS timo_profile
          ON profile.app_name = 'timo'
         AND timo_profile.guild_executor_key = profile.guild_executor_key
         AND timo_profile.timo_id = profile.streamer_id
        LEFT JOIN streamer_external_profiles AS external_profile
          ON profile.app_name = external_profile.app_name
         AND external_profile.guild_executor_key = profile.guild_executor_key
         AND external_profile.streamer_id = profile.streamer_id
        LEFT JOIN guild_executors AS executor
          ON executor.guild_name = profile.guild_name
         AND lower(executor.app_name) IN (
              profile.app_name,
              CASE WHEN profile.app_name = 'sugo' THEN 'sogo' ELSE profile.app_name END
         );

        CREATE VIEW IF NOT EXISTS streamer_analytics_daily_facts_v5 AS
        SELECT
            facts.*,
            COALESCE(NULLIF(profile.country, ''), NULLIF(executor.country, ''), '') AS country
        FROM streamer_analytics_daily_facts_v4 AS facts
        LEFT JOIN streamer_analytics_profiles_v4 AS profile
          ON profile.app_name = facts.app_name
         AND profile.guild_executor_key = facts.guild_executor_key
         AND profile.streamer_id = facts.streamer_id
        LEFT JOIN guild_executors AS executor
          ON executor.guild_name = facts.guild_name
         AND lower(executor.app_name) IN (
              facts.app_name,
              CASE WHEN facts.app_name = 'sugo' THEN 'sogo' ELSE facts.app_name END
         );

        CREATE VIEW IF NOT EXISTS streamer_analytics_profiles_v5 AS
        WITH linky_profiles AS (
            SELECT
                profile.*,
                COALESCE(NULLIF(external_profile.platform_character_id, ''), profile.streamer_id)
                    AS canonical_streamer_id
            FROM streamer_analytics_profiles_v4 AS profile
            LEFT JOIN streamer_external_profiles AS external_profile
              ON profile.app_name = external_profile.app_name
             AND profile.guild_executor_key = external_profile.guild_executor_key
             AND profile.streamer_id = external_profile.streamer_id
            WHERE profile.app_name = 'linky'
        ), ranked_linky AS (
            SELECT
                linky_profiles.*,
                ROW_NUMBER() OVER (
                    PARTITION BY app_name, canonical_streamer_id
                    ORDER BY source_updated_at DESC, streamer_id = canonical_streamer_id DESC
                ) AS canonical_rank
            FROM linky_profiles
        )
        SELECT
            app_name, guild_executor_key, guild_name, canonical_streamer_id AS streamer_id,
            display_name, registered_date, last_active_date, is_real_person, country, source_updated_at
        FROM ranked_linky
        WHERE canonical_rank = 1
        UNION ALL
        SELECT
            app_name, guild_executor_key, guild_name, streamer_id,
            display_name, registered_date, last_active_date, is_real_person, country, source_updated_at
        FROM streamer_analytics_profiles_v4
        WHERE app_name <> 'linky';

        CREATE VIEW IF NOT EXISTS streamer_analytics_daily_facts_v6 AS
        WITH canonical_facts AS (
            SELECT
                facts.*,
                CASE
                    WHEN facts.app_name = 'linky'
                    THEN COALESCE(NULLIF(external_profile.platform_character_id, ''), facts.streamer_id)
                    ELSE facts.streamer_id
                END AS canonical_streamer_id,
                NULLIF(external_profile.platform_character_id, '') AS linky_sid
            FROM streamer_analytics_daily_facts_v5 AS facts
            LEFT JOIN streamer_external_profiles AS external_profile
              ON facts.app_name = 'linky'
             AND external_profile.app_name = facts.app_name
             AND external_profile.guild_executor_key = facts.guild_executor_key
             AND external_profile.streamer_id = facts.streamer_id
        ), ranked_facts AS (
            SELECT
                canonical_facts.*,
                MAX(COALESCE(is_new, 0)) OVER (
                    PARTITION BY app_name, guild_executor_key, canonical_streamer_id, stat_date
                ) AS canonical_is_new,
                ROW_NUMBER() OVER (
                    PARTITION BY app_name, guild_executor_key, canonical_streamer_id, stat_date
                    ORDER BY total_income IS NOT NULL DESC,
                             COALESCE(total_income, 0) DESC,
                             source_updated_at DESC
                ) AS canonical_rank
            FROM canonical_facts
        )
        SELECT
            app_name, guild_executor_key, guild_name, canonical_streamer_id AS streamer_id,
            stat_date, canonical_is_new AS is_new,
            CASE
                WHEN app_name = 'linky' THEN CASE WHEN COALESCE(private_message_income, 0) > 0 THEN 1 ELSE 0 END
                WHEN COALESCE(total_income, 0) > 0 THEN 1
                ELSE 0
            END AS is_active,
            total_income, qualified_revenue, matching_income, private_message_income,
            private_gift_income, call_income, online_hours, call_count,
            source_name, source_updated_at, country
        FROM ranked_facts
        WHERE canonical_rank = 1
          AND (app_name <> 'linky' OR linky_sid IS NOT NULL);

        CREATE VIEW IF NOT EXISTS streamer_analytics_daily_facts_v7 AS
        SELECT
            app_name, guild_executor_key, guild_name, streamer_id, stat_date, is_new,
            CASE
                WHEN app_name = 'linky' THEN CASE WHEN COALESCE(private_message_income, 0) > 0 THEN 1 ELSE 0 END
                ELSE is_active
            END AS is_active,
            total_income, qualified_revenue, matching_income, private_message_income,
            private_gift_income, call_income, online_hours, call_count,
            source_name, source_updated_at, country
        FROM streamer_analytics_daily_facts_v6;

        CREATE VIEW IF NOT EXISTS streamer_analytics_daily_facts_v8 AS
        SELECT facts.*
        FROM streamer_analytics_daily_facts_v7 AS facts
        WHERE facts.app_name <> 'timo'
           OR NOT EXISTS (
                SELECT 1
                FROM timo_external_sync_runs AS any_run
                WHERE any_run.data_date_bj = facts.stat_date
           )
           OR EXISTS (
                SELECT 1
                FROM timo_external_sync_runs AS successful_run
                WHERE successful_run.data_date_bj = facts.stat_date
                  AND successful_run.status = 'success'
                  AND successful_run.created_at = (
                      SELECT MAX(latest_run.created_at)
                      FROM timo_external_sync_runs AS latest_run
                      WHERE latest_run.data_date_bj = facts.stat_date
                  )
           );

        CREATE VIEW IF NOT EXISTS streamer_analytics_daily_facts_v9 AS
        SELECT
            app_name, guild_executor_key, guild_name, streamer_id, stat_date, is_new,
            CASE
                WHEN app_name = 'linky' THEN CASE WHEN COALESCE(total_income, 0) > 0 THEN 1 ELSE 0 END
                ELSE is_active
            END AS is_active,
            total_income, qualified_revenue, matching_income, private_message_income,
            private_gift_income, call_income, online_hours, call_count,
            source_name, source_updated_at, country
        FROM streamer_analytics_daily_facts_v8;

        CREATE VIEW IF NOT EXISTS streamer_analytics_daily_facts_v10 AS
        SELECT
            app_name, guild_executor_key, guild_name, streamer_id, stat_date, is_new,
            CASE
                WHEN app_name = 'linky' AND lower(trim(COALESCE(country, ''))) <> 'indonesia'
                THEN CASE WHEN COALESCE(private_message_income, 0) > 0 THEN 1 ELSE 0 END
                ELSE is_active
            END AS is_active,
            CASE
                WHEN app_name = 'linky' AND lower(trim(COALESCE(country, ''))) <> 'indonesia'
                THEN COALESCE(private_message_income, 0)
                ELSE total_income
            END AS total_income,
            qualified_revenue, matching_income, private_message_income,
            private_gift_income, call_income, online_hours, call_count,
            source_name, source_updated_at, country
        FROM streamer_analytics_daily_facts_v9;

        CREATE VIEW IF NOT EXISTS streamer_analytics_daily_facts_v11 AS
        SELECT
            facts.app_name, facts.guild_executor_key, facts.guild_name,
            facts.streamer_id, facts.stat_date,
            CASE
                WHEN facts.app_name = 'timo'
                THEN CASE
                    WHEN substr(
                        COALESCE(NULLIF(profile.joined_guild_at_bj, ''), profile.registered_at_bj),
                        1, 10
                    ) = facts.stat_date THEN 1 ELSE 0
                END
                ELSE facts.is_new
            END AS is_new,
            facts.is_active, facts.total_income, facts.qualified_revenue,
            facts.matching_income, facts.private_message_income,
            facts.private_gift_income, facts.call_income, facts.online_hours,
            facts.call_count, facts.source_name, facts.source_updated_at, facts.country
        FROM streamer_analytics_daily_facts_v10 AS facts
        LEFT JOIN timo_external_streamers AS profile
          ON facts.app_name = 'timo'
         AND profile.guild_executor_key = facts.guild_executor_key
         AND profile.timo_id = facts.streamer_id;
        """
    )
    sync_run_columns = {
        str(row[1]) for row in conn.execute('PRAGMA table_info(streamer_external_sync_runs)').fetchall()
    }
    if 'run_scope' not in sync_run_columns:
        conn.execute(
            "ALTER TABLE streamer_external_sync_runs "
            "ADD COLUMN run_scope TEXT NOT NULL DEFAULT 'legacy'"
        )
    if 'scope_key' not in sync_run_columns:
        conn.execute(
            "ALTER TABLE streamer_external_sync_runs "
            "ADD COLUMN scope_key TEXT NOT NULL DEFAULT ''"
        )
    revenue_columns = {
        str(row[1]) for row in conn.execute('PRAGMA table_info(streamer_external_revenue_daily)').fetchall()
    }
    if 'voice_room_income' not in revenue_columns:
        conn.execute(
            "ALTER TABLE streamer_external_revenue_daily "
            "ADD COLUMN voice_room_income REAL NOT NULL DEFAULT 0"
        )
    profile_summary_columns = {
        str(row[1])
        for row in conn.execute(
            'PRAGMA table_info(streamer_analytics_profile_summary)'
        ).fetchall()
    }
    for column_name, column_sql in (
        ('is_app_fan', 'INTEGER'),
        ('app_fan_status', "TEXT NOT NULL DEFAULT 'historical_unknown'"),
        ('app_fan_source', "TEXT NOT NULL DEFAULT ''"),
    ):
        if column_name not in profile_summary_columns:
            conn.execute(
                f'ALTER TABLE streamer_analytics_profile_summary '
                f'ADD COLUMN {column_name} {column_sql}'
            )
    guild_revenue_columns = {
        str(row[1]) for row in conn.execute('PRAGMA table_info(streamer_external_guild_revenue_daily)').fetchall()
    }
    for column in ('chat_income', 'voice_room_income', 'platform_total_income'):
        if column not in guild_revenue_columns:
            conn.execute(
                f"ALTER TABLE streamer_external_guild_revenue_daily "
                f"ADD COLUMN {column} REAL NOT NULL DEFAULT 0"
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_streamer_external_sync_runs_app_scope_created "
        "ON streamer_external_sync_runs(app_name, run_scope, created_at DESC)"
    )
    ensure_streamer_foundation_views(conn)


def _iso_date(value: object) -> Optional[date]:
    try:
        return date.fromisoformat(str(value or '')[:10])
    except ValueError:
        return None


def _date_window(date_from: object, date_to: object) -> tuple[date, date]:
    default_end = date.today() - timedelta(days=1)
    end = _iso_date(date_to) or default_end
    start = _iso_date(date_from) or (end - timedelta(days=29))
    if start > end:
        raise ValueError('invalid_date_range')
    if (end - start).days > STREAMER_ANALYTICS_MAX_RANGE_DAYS:
        raise ValueError('streamer_analytics_range_too_large')
    return start, end


def _newcomer_analysis_window(
    start: date,
    end: date,
    data_as_of: Optional[date],
) -> tuple[date, date]:
    """Use the first 30 days of a complete 60-day window for rolling 30-day dashboards."""
    selected_days = (end - start).days + 1
    if selected_days != NEWCOMER_COMPLETE_WINDOW_DAYS or not data_as_of:
        return start, end
    complete_end = min(end, data_as_of)
    if end + timedelta(days=RETENTION_DAY_OFFSETS[30]) <= data_as_of:
        return start, end
    return (
        complete_end - timedelta(days=(NEWCOMER_COMPLETE_WINDOW_DAYS * 2) - 1),
        complete_end - timedelta(days=NEWCOMER_COMPLETE_WINDOW_DAYS),
    )


def _rows(conn: sqlite3.Connection, sql: str, params: Iterable[object] = ()) -> List[Dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def _timo_joined_guild_at_expression(conn: sqlite3.Connection) -> str:
    columns = {
        str(row[1]) for row in conn.execute('PRAGMA table_info(timo_external_streamers)').fetchall()
    }
    if 'joined_guild_at_bj' in columns:
        return "COALESCE(NULLIF(joined_guild_at_bj, ''), registered_at_bj)"
    return 'registered_at_bj'


def _timo_materialization_profiles(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Read Timo profiles without expanding the cross-platform UNION view."""
    joined_at = _timo_joined_guild_at_expression(conn)
    return _rows(
        conn,
        f"""
        SELECT
            'timo' AS app_name,
            profile.guild_executor_key,
            profile.guild_name,
            COALESCE(NULLIF(profile.country, ''), NULLIF(executor.country, ''), '') AS country,
            profile.timo_id AS streamer_id,
            profile.nickname AS display_name,
            CASE WHEN length({joined_at}) >= 10 THEN substr({joined_at}, 1, 10) ELSE '' END
                AS registered_date,
            CASE WHEN length(profile.last_active_at_bj) >= 10
                 THEN substr(profile.last_active_at_bj, 1, 10) ELSE '' END AS last_active_date,
            profile.is_real_person,
            profile.source_payload,
            profile.updated_at AS source_updated_at
        FROM timo_external_streamers AS profile
        LEFT JOIN guild_executors AS executor
          ON executor.guild_name = profile.guild_name
         AND lower(executor.app_name) = 'timo'
        """,
    )


def _timo_legacy_materialization_daily_facts(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Preserve the pre-watermark all-market gate for legacy fixtures only."""
    joined_at = _timo_joined_guild_at_expression(conn)
    return _rows(
        conn,
        f"""
        WITH latest_sync AS (
            SELECT data_date_bj, MAX(created_at) AS created_at
            FROM timo_external_sync_runs
            GROUP BY data_date_bj
        ), latest_sync_status AS (
            SELECT run.data_date_bj,
                   MAX(CASE WHEN run.status = 'success' THEN 1 ELSE 0 END) AS succeeded
            FROM timo_external_sync_runs AS run
            JOIN latest_sync AS latest
              ON latest.data_date_bj = run.data_date_bj
             AND latest.created_at = run.created_at
            GROUP BY run.data_date_bj
        )
        SELECT
            'timo' AS app_name,
            revenue.guild_executor_key,
            revenue.guild_name,
            COALESCE(NULLIF(profile.country, ''), NULLIF(executor.country, ''), '') AS country,
            revenue.timo_id AS streamer_id,
            revenue.stat_date_bj AS stat_date,
            CASE WHEN substr({joined_at}, 1, 10) = revenue.stat_date_bj THEN 1 ELSE 0 END
                AS is_new,
            CASE WHEN COALESCE(revenue.total_income, 0) > 0 THEN 1 ELSE 0 END AS is_active,
            revenue.total_income
        FROM timo_external_revenue_daily AS revenue
        LEFT JOIN timo_external_streamers AS profile
          ON profile.guild_executor_key = revenue.guild_executor_key
         AND profile.timo_id = revenue.timo_id
        LEFT JOIN guild_executors AS executor
          ON executor.guild_name = revenue.guild_name
         AND lower(executor.app_name) = 'timo'
        LEFT JOIN latest_sync_status AS sync
          ON sync.data_date_bj = revenue.stat_date_bj
        WHERE revenue.provisional = 0
          AND COALESCE(sync.succeeded, 1) = 1
        """,
    )


def _timo_snapshot_checksum(rows: Iterable[Dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: (str(item['timo_id']), str(item['row_hash']))):
        digest.update(str(row['timo_id']).encode('utf-8'))
        digest.update(b'\x1f')
        digest.update(str(row['row_hash']).encode('ascii'))
        digest.update(b'\n')
    return digest.hexdigest()


def _timo_materialization_scope_snapshot(
    conn: sqlite3.Connection,
) -> tuple[List[Dict[str, Any]], set[tuple[str, date]]]:
    """Return only guild-day facts that match a complete scope watermark.

    A Timo sync run is a three-market orchestration result.  It may be partial
    while an individual guild has already published a complete, immutable
    scope.  Analytics therefore consumes the guild-day watermark and validates
    its full fact lineage instead of using the day-level run status.
    """
    watermark_columns = {
        str(row[1]) for row in conn.execute('PRAGMA table_info(timo_sync_watermark)').fetchall()
    }
    revenue_columns = {
        str(row[1]) for row in conn.execute('PRAGMA table_info(timo_external_revenue_daily)').fetchall()
    }
    required_watermark_columns = {
        'guild_executor_key', 'stat_date_bj', 'checksum', 'last_success_sync_id',
        'row_count', 'total_income', 'data_status', 'revision_version',
    }
    required_revenue_columns = {'revision_version', 'last_sync_id', 'row_hash'}
    if not required_watermark_columns.issubset(watermark_columns) or not required_revenue_columns.issubset(revenue_columns):
        legacy_facts = _timo_legacy_materialization_daily_facts(conn)
        return legacy_facts, {
            (str(row.get('guild_executor_key') or ''), parsed_date)
            for row in legacy_facts
            if (parsed_date := _iso_date(row.get('stat_date')))
        }

    watermarks = _rows(
        conn,
        """
        SELECT guild_executor_key, stat_date_bj, checksum, last_success_sync_id,
               row_count, total_income, revision_version
        FROM timo_sync_watermark
        WHERE lower(COALESCE(data_status, '')) = 'complete'
        ORDER BY stat_date_bj, guild_executor_key
        """,
    )
    if not watermarks:
        legacy_facts = _timo_legacy_materialization_daily_facts(conn)
        return legacy_facts, {
            (str(row.get('guild_executor_key') or ''), parsed_date)
            for row in legacy_facts
            if (parsed_date := _iso_date(row.get('stat_date')))
        }

    joined_at = _timo_joined_guild_at_expression(conn)
    fact_rows = _rows(
        conn,
        f"""
        SELECT
            'timo' AS app_name,
            revenue.guild_executor_key,
            revenue.guild_name,
            COALESCE(NULLIF(profile.country, ''), NULLIF(executor.country, ''), '') AS country,
            revenue.timo_id AS streamer_id,
            revenue.timo_id,
            revenue.stat_date_bj AS stat_date,
            CASE WHEN substr({joined_at}, 1, 10) = revenue.stat_date_bj THEN 1 ELSE 0 END
                AS is_new,
            CASE WHEN COALESCE(revenue.total_income, 0) > 0 THEN 1 ELSE 0 END AS is_active,
            revenue.total_income,
            revenue.revision_version,
            revenue.last_sync_id,
            revenue.row_hash
        FROM timo_external_revenue_daily AS revenue
        JOIN timo_sync_watermark AS watermark
          ON watermark.guild_executor_key = revenue.guild_executor_key
         AND watermark.stat_date_bj = revenue.stat_date_bj
         AND lower(COALESCE(watermark.data_status, '')) = 'complete'
        LEFT JOIN timo_external_streamers AS profile
          ON profile.guild_executor_key = revenue.guild_executor_key
         AND profile.timo_id = revenue.timo_id
        LEFT JOIN guild_executors AS executor
          ON executor.guild_name = revenue.guild_name
         AND lower(executor.app_name) = 'timo'
        WHERE revenue.provisional = 0
        ORDER BY revenue.stat_date_bj, revenue.guild_executor_key, revenue.timo_id
        """,
    )
    facts_by_scope: Dict[tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in fact_rows:
        facts_by_scope[(str(row['guild_executor_key']), str(row['stat_date']))].append(row)

    accepted: List[Dict[str, Any]] = []
    covered_scopes: set[tuple[str, date]] = set()
    integrity_errors: List[str] = []
    for watermark in watermarks:
        guild_key = str(watermark['guild_executor_key'])
        stat_date_text = str(watermark['stat_date_bj'])
        scope_rows = facts_by_scope.get((guild_key, stat_date_text), [])
        expected_rows = int(watermark['row_count'] or 0)
        expected_total = float(watermark['total_income'] or 0)
        expected_revision = int(watermark['revision_version'] or 0)
        expected_sync_id = str(watermark['last_success_sync_id'] or '')
        expected_checksum = str(watermark['checksum'] or '')
        actual_total = sum(float(row.get('total_income') or 0) for row in scope_rows)
        revisions = {int(row.get('revision_version') or 0) for row in scope_rows}
        sync_ids = {str(row.get('last_sync_id') or '') for row in scope_rows}
        valid_row_hashes = all(len(str(row.get('row_hash') or '')) == 64 for row in scope_rows)
        actual_checksum = _timo_snapshot_checksum(scope_rows)
        mismatches = []
        if len(scope_rows) != expected_rows:
            mismatches.append('row_count')
        if abs(actual_total - expected_total) > 0.000001:
            mismatches.append('total_income')
        if expected_rows and revisions != {expected_revision}:
            mismatches.append('revision')
        if expected_rows and sync_ids != {expected_sync_id}:
            mismatches.append('sync_id')
        if not valid_row_hashes:
            mismatches.append('row_hash')
        if actual_checksum != expected_checksum:
            mismatches.append('checksum')
        parsed_date = _iso_date(stat_date_text)
        if not parsed_date:
            mismatches.append('stat_date')
        if mismatches:
            integrity_errors.append(f'{guild_key}:{stat_date_text}:{",".join(mismatches)}')
            continue
        accepted.extend({
            key: value for key, value in row.items()
            if key not in {'timo_id', 'revision_version', 'last_sync_id', 'row_hash'}
        } for row in scope_rows)
        covered_scopes.add((guild_key, parsed_date))

    if integrity_errors:
        raise RuntimeError('timo_analytics_scope_integrity_mismatch:' + ';'.join(integrity_errors[:20]))
    return accepted, covered_scopes


def _timo_materialization_daily_facts(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    facts, _covered_scopes = _timo_materialization_scope_snapshot(conn)
    return facts


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


def _week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _date_span(start: date, end: date) -> List[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _source_value(payload: object, key: str) -> object:
    try:
        parsed = json.loads(str(payload or '{}'))
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed.get(key)


def _is_timo_female(payload: object) -> bool:
    value = _source_value(payload, 'gender')
    return str(value or '').strip() == '2'


def _timo_settlement_rates(country: str) -> tuple[float, float]:
    return TIMO_SETTLEMENT_RATES.get(str(country or '').strip().lower(), (0.0, 0.0))


def _timo_cohort_display_window(_start: date, end: date) -> tuple[date, date]:
    cohort_end = end - timedelta(days=(end.weekday() + 1) % 7)
    cohort_start = _week_start(cohort_end) - timedelta(weeks=TIMO_COHORT_DISPLAY_WEEKS - 1)
    return cohort_start, cohort_end


def _build_timo_weekly_cohorts_live(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    guild_name: str = '',
    country: str = '',
    _profiles: Optional[Iterable[Dict[str, Any]]] = None,
    _facts: Optional[Iterable[Dict[str, Any]]] = None,
    _covered_scopes: Optional[set[tuple[str, date]]] = None,
    _data_as_of: Optional[date] = None,
) -> Dict[str, Any]:
    cohort_start, cohort_end = _timo_cohort_display_window(start, end)
    strict_daily_scope = _facts is not None or _covered_scopes is not None
    if _facts is None or _covered_scopes is None:
        source_facts, covered_scopes = _timo_materialization_scope_snapshot(conn)
    else:
        source_facts = [dict(row) for row in _facts]
        covered_scopes = set(_covered_scopes)
    data_as_of = _data_as_of or max(
        (scope_date for _guild_key, scope_date in covered_scopes),
        default=None,
    )
    if cohort_start > cohort_end:
        return {
            'available': True,
            'data_as_of': data_as_of.isoformat() if data_as_of else None,
            'diamonds_per_usd': int(TIMO_DIAMONDS_PER_USD),
            'rows': [],
        }
    source_profiles = (
        [dict(row) for row in _profiles]
        if _profiles is not None else _timo_materialization_profiles(conn)
    )
    profiles = []
    for source_profile in source_profiles:
        profile = dict(source_profile)
        profile['timo_id'] = str(profile.get('timo_id') or profile.get('streamer_id') or '')
        profile['registered_at_bj'] = str(
            profile.get('registered_at_bj') or profile.get('registered_date') or ''
        )
        registered = _iso_date(profile['registered_at_bj'])
        if not registered or not cohort_start <= registered <= cohort_end:
            continue
        if guild_name and str(profile.get('guild_name') or '') != guild_name:
            continue
        if country and str(profile.get('country') or '') != country:
            continue
        profiles.append(profile)
    if not profiles:
        return {
            'available': True,
            'data_as_of': data_as_of.isoformat() if data_as_of else None,
            'diamonds_per_usd': int(TIMO_DIAMONDS_PER_USD),
            'rows': [],
        }

    facts: List[Dict[str, Any]] = []
    for source_fact in source_facts:
        fact = dict(source_fact)
        fact['timo_id'] = str(fact.get('timo_id') or fact.get('streamer_id') or '')
        fact['stat_date_bj'] = str(fact.get('stat_date_bj') or fact.get('stat_date') or '')
        stat_date = _iso_date(fact['stat_date_bj'])
        if not stat_date or not data_as_of or not cohort_start <= stat_date <= data_as_of:
            continue
        if guild_name and str(fact.get('guild_name') or '') != guild_name:
            continue
        if country and str(fact.get('country') or '') != country:
            continue
        facts.append(fact)

    weekly_facts: List[Dict[str, Any]] = []
    weekly_coverage: List[Dict[str, Any]] = []
    if data_as_of and not strict_daily_scope:
        filter_clause = ''
        filter_params: List[object] = []
        if guild_name:
            filter_clause += ' AND guild_name = ?'
            filter_params.append(guild_name)
        if country:
            filter_clause += ' AND country = ?'
            filter_params.append(country)
        weekly_facts = _rows(
            conn,
            f"""
            SELECT guild_executor_key, guild_name, country, week_start_bj, timo_id, total_income
            FROM timo_external_revenue_weekly
            WHERE week_start_bj BETWEEN ? AND ?{filter_clause}
            """,
            [cohort_start.isoformat(), data_as_of.isoformat(), *filter_params],
        )
        weekly_coverage = _rows(
            conn,
            f"""
            SELECT guild_executor_key, guild_name, country, week_start_bj
            FROM timo_external_revenue_weekly_coverage
            WHERE status = 'success' AND week_start_bj BETWEEN ? AND ?{filter_clause}
            """,
            [cohort_start.isoformat(), data_as_of.isoformat(), *filter_params],
        )

    cohort_groups: Dict[tuple[date, str], List[Dict[str, Any]]] = defaultdict(list)
    profile_country: Dict[tuple[str, str], str] = {}
    income_by_streamer_date: Dict[tuple[str, str, date], float] = defaultdict(float)
    covered_dates: Dict[str, set[date]] = defaultdict(set)
    platform_income: Dict[tuple[str, date], float] = defaultdict(float)
    weekly_income_by_streamer: Dict[tuple[str, str, date], float] = defaultdict(float)
    weekly_covered_guilds: set[tuple[str, date]] = set()
    weekly_platform_income: Dict[tuple[str, date], float] = defaultdict(float)
    for profile in profiles:
        registered = _iso_date(profile.get('registered_at_bj'))
        if not registered:
            continue
        country = str(profile.get('country') or '未标注').strip() or '未标注'
        cohort_groups[(_week_start(registered), country)].append({
            **profile,
            'registered_date': registered,
            'country': country,
            'is_female': _is_timo_female(profile.get('source_payload')),
        })
        profile_country[(str(profile['guild_executor_key']), str(profile['timo_id']))] = country
    for fact in facts:
        stat_date = _iso_date(fact.get('stat_date_bj'))
        if not stat_date:
            continue
        key = (str(fact['guild_executor_key']), str(fact['timo_id']))
        country = profile_country.get(key) or str(fact.get('country') or '未标注').strip() or '未标注'
        income = float(fact.get('total_income') or 0)
        income_by_streamer_date[(key[0], key[1], stat_date)] += income
        covered_dates[country].add(stat_date)
        platform_income[(country, _week_start(stat_date))] += income
    for fact in weekly_facts:
        week_start = _iso_date(fact.get('week_start_bj'))
        if not week_start:
            continue
        executor_key = str(fact.get('guild_executor_key') or '')
        timo_id = str(fact.get('timo_id') or '')
        fact_country = str(fact.get('country') or '未标注').strip() or '未标注'
        income = float(fact.get('total_income') or 0)
        weekly_income_by_streamer[(executor_key, timo_id, week_start)] += income
        weekly_platform_income[(fact_country, week_start)] += income
    for coverage in weekly_coverage:
        week_start = _iso_date(coverage.get('week_start_bj'))
        if week_start:
            weekly_covered_guilds.add((str(coverage.get('guild_executor_key') or ''), week_start))

    rows: List[Dict[str, Any]] = []
    for (week, country), members in sorted(
        cohort_groups.items(),
        key=lambda item: (-item[0][0].toordinal(), item[0][1]),
    ):
        week_end = week + timedelta(days=6)
        certified = sum(1 for member in members if int(member.get('is_real_person') or 0) == 1)
        non_certified = len(members) - certified
        non_certified_rate, certified_rate = _timo_settlement_rates(country)
        base_settlement = non_certified * non_certified_rate + certified * certified_rate
        periods = []
        member_guild_keys = {str(member['guild_executor_key']) for member in members}
        for week_no in range(TIMO_COHORT_PERIOD_COUNT):
            display_week = week_no + 1
            period_start = week + timedelta(days=week_no * 7)
            period_end = period_start + timedelta(days=6)
            expected_dates = _date_span(period_start, period_end)
            daily_complete = bool(
                data_as_of
                and period_end <= data_as_of
                and (
                    all(
                        (guild_key, day) in covered_scopes
                        for guild_key in member_guild_keys
                        for day in expected_dates
                    )
                    if strict_daily_scope else
                    all(day in covered_dates[country] for day in expected_dates)
                )
            )
            weekly_complete = bool(
                data_as_of
                and period_end <= data_as_of
                and member_guild_keys
                and all((guild_key, period_start) in weekly_covered_guilds for guild_key in member_guild_keys)
            )
            complete = daily_complete or weekly_complete
            source = 'daily' if daily_complete else ('weekly' if weekly_complete else '')
            if complete:
                status_reason = 'observed'
            elif not data_as_of or period_end > data_as_of:
                status_reason = 'window_not_complete'
            else:
                status_reason = 'data_coverage_incomplete'
            if complete:
                member_income = []
                for member in members:
                    if daily_complete:
                        income = sum(
                            income_by_streamer_date[(
                                str(member['guild_executor_key']),
                                str(member['timo_id']),
                                day,
                            )]
                            for day in expected_dates
                        )
                    else:
                        income = weekly_income_by_streamer[(
                            str(member['guild_executor_key']),
                            str(member['timo_id']),
                            period_start,
                        )]
                    member_income.append(income)
                income_diamonds = sum(member_income)
                active_streamers = sum(1 for income in member_income if income > 0)
                denominator = len(members) if display_week == 1 else active_streamers
                per_user_usd = income_diamonds / TIMO_DIAMONDS_PER_USD / denominator if denominator else 0.0
            else:
                income_diamonds = None
                active_streamers = None
                per_user_usd = None
            periods.append({
                'week': display_week,
                'label': f'W{display_week}',
                'date_from': period_start.isoformat(),
                'date_to': period_end.isoformat(),
                'status': 'complete' if complete else 'incomplete',
                'status_reason': status_reason,
                'source': source,
                'active_streamers': active_streamers,
                'income_diamonds': round(income_diamonds, 2) if income_diamonds is not None else None,
                'income_usd': round(income_diamonds / TIMO_DIAMONDS_PER_USD, 2) if income_diamonds is not None else None,
                'per_user_usd': round(per_user_usd, 2) if per_user_usd is not None else None,
                'per_user_metric': 'ARPU' if display_week == 1 else 'ARPPU',
            })

        def bonus(days: int, threshold_usd: float, reward_usd: float) -> Dict[str, Any]:
            eligible_members = [member for member in members if member['is_female']]
            matured = 0
            observed = 0
            qualified = 0
            for member in eligible_members:
                registered = member['registered_date']
                expected_dates = [registered + timedelta(days=offset) for offset in range(days)]
                if not data_as_of or expected_dates[-1] > data_as_of:
                    continue
                matured += 1
                if not (
                    all(
                        (str(member['guild_executor_key']), day) in covered_scopes
                        for day in expected_dates
                    )
                    if strict_daily_scope else
                    all(day in covered_dates[country] for day in expected_dates)
                ):
                    continue
                observed += 1
                income = sum(
                    income_by_streamer_date[(
                        str(member['guild_executor_key']),
                        str(member['timo_id']),
                        day,
                    )]
                    for day in expected_dates
                )
                if income / TIMO_DIAMONDS_PER_USD > threshold_usd:
                    qualified += 1
            if not eligible_members:
                status = 'complete'
                status_reason = 'observed'
            elif observed == len(eligible_members):
                status = 'complete'
                status_reason = 'observed'
            elif observed:
                status = 'partial'
                status_reason = 'data_coverage_incomplete' if matured == len(eligible_members) else 'window_not_complete'
            else:
                status = 'incomplete'
                status_reason = 'data_coverage_incomplete' if matured == len(eligible_members) else 'window_not_complete'
            return {
                'days': days,
                'status': status,
                'status_reason': status_reason,
                'eligible_streamers': len(eligible_members),
                'observed_streamers': observed,
                'qualified_streamers': qualified if observed or status == 'complete' else None,
                'reward_per_streamer_usd': reward_usd,
                'amount_usd': round(qualified * reward_usd, 2) if observed or status == 'complete' else None,
            }

        bonus_7d = bonus(7, 0.5, 0.5)
        bonus_10d = bonus(10, 1.0, 1.0)
        settlement_complete = bonus_7d['status'] == 'complete' and bonus_10d['status'] == 'complete'
        historical_coverage_incomplete = (
            any(period['status_reason'] == 'data_coverage_incomplete' for period in periods)
            or bonus_7d['status_reason'] == 'data_coverage_incomplete'
            or bonus_10d['status_reason'] == 'data_coverage_incomplete'
        )
        if historical_coverage_incomplete:
            continue
        rows.append({
            'week_start': week.isoformat(),
            'week_end': week_end.isoformat(),
            'country': country,
            'new_streamers': len(members),
            'certified_streamers': certified,
            'non_certified_streamers': non_certified,
            'platform_week_income_diamonds': (
                round(
                    platform_income[(country, week)]
                    if periods[0]['source'] == 'daily'
                    else weekly_platform_income[(country, week)],
                    2,
                )
                if periods and periods[0]['status'] == 'complete' else None
            ),
            'periods': periods,
            'settlement': {
                'status': 'complete' if settlement_complete else 'incomplete',
                'status_reason': 'observed' if settlement_complete else 'window_not_complete',
                'non_certified_rate_usd': non_certified_rate,
                'certified_rate_usd': certified_rate,
                'base_usd': round(base_settlement, 2),
                'bonus_7d': bonus_7d,
                'bonus_10d': bonus_10d,
                'total_usd': round(base_settlement + bonus_7d['amount_usd'] + bonus_10d['amount_usd'], 2)
                if settlement_complete else None,
            },
        })
    return {
        'available': True,
        'data_as_of': data_as_of.isoformat() if data_as_of else None,
        'diamonds_per_usd': int(TIMO_DIAMONDS_PER_USD),
        'week_starts_on': 'monday',
        'cohort_date_from': cohort_start.isoformat(),
        'cohort_date_to': cohort_end.isoformat(),
        'rows': rows,
        'definitions': {
            'active': '统计周内累计收益大于 0 的主播。',
            'arpu': '首周收益美元除以该周全部新增主播。',
            'arppu': '后续周收益美元除以该周收益活跃主播。',
            'certified': '真人认证主播，即 is_real_person=1。',
            'extra_policy': '新女7日与10日奖励叠加，全部地区通用。',
            'date_window': '最多展示截至筛选结束日最近 12 个完整的周一至周日；没有新增主播的国家周不生成 cohort 行。',
            'zero_vs_unavailable': '完整且覆盖充分的观察周按实际值展示，零收益为 0；窗口未结束或覆盖不足时不可统计。',
        },
    }


def _build_linky_weekly_cohorts_live(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    guild_name: str = '',
    country: str = '',
    _profiles: Optional[Iterable[Dict[str, Any]]] = None,
    _facts: Optional[Iterable[Dict[str, Any]]] = None,
    _platform_facts: Optional[Iterable[Dict[str, Any]]] = None,
    _data_as_of: Optional[date] = None,
    _observed_dates_by_country: Optional[Dict[str, set[str]]] = None,
) -> Dict[str, Any]:
    cohort_start, cohort_end = _timo_cohort_display_window(start, end)
    filter_clause, filter_params = _linky_exclusion_scope()
    if guild_name:
        filter_clause += ' AND guild_name = ?'
        filter_params.append(guild_name)
    if country:
        filter_clause += ' AND country = ?'
        filter_params.append(country)
    data_as_of = (
        _data_as_of
        if _observed_dates_by_country is not None
        else _revenue_data_as_of(
            conn,
            app='linky',
            guild_name=guild_name,
            country=country,
        )
    )
    empty_payload = {
        'available': True,
        'data_as_of': data_as_of.isoformat() if data_as_of else None,
        'diamonds_per_usd': int(PLATFORM_INCOME_UNITS_PER_USD['linky'] or 0),
        'week_starts_on': 'monday',
        'cohort_date_from': cohort_start.isoformat(),
        'cohort_date_to': cohort_end.isoformat(),
        'rows': [],
    }
    if cohort_start > cohort_end:
        return empty_payload
    if _profiles is None:
        profiles: Iterable[Dict[str, Any]] = (
            dict(row) for row in conn.execute(
            f"""
            SELECT guild_executor_key, guild_name, country, streamer_id,
                   registered_date, is_real_person
            FROM {PROFILE_VIEW}
            WHERE app_name = 'linky' AND length(registered_date) >= 10
              AND registered_date BETWEEN ? AND ?{filter_clause}
            """,
            [cohort_start.isoformat(), cohort_end.isoformat(), *filter_params],
            )
        )
    else:
        profiles = (
            row for row in _profiles
            if cohort_start.isoformat() <= str(row.get('registered_date') or '') <= cohort_end.isoformat()
            and _linky_guild_is_included(row.get('guild_name'))
            and (not guild_name or str(row.get('guild_name') or '') == guild_name)
            and (not country or str(row.get('country') or '') == country)
        )

    cohort_groups: Dict[tuple[date, str], List[Dict[str, Any]]] = defaultdict(list)
    income_by_streamer_date: Dict[tuple[str, str, date], float] = defaultdict(float)
    active_by_streamer_date: Dict[tuple[str, str, date], bool] = defaultdict(bool)
    platform_income: Dict[tuple[str, date], float] = defaultdict(float)
    for profile in profiles:
        registered = _iso_date(profile.get('registered_date'))
        if not registered:
            continue
        profile_country = str(profile.get('country') or '未标注').strip() or '未标注'
        cohort_groups[(_week_start(registered), profile_country)].append({
            **profile,
            'registered_date': registered,
            'country': profile_country,
        })
    if not cohort_groups:
        return empty_payload

    if not data_as_of:
        facts: Iterable[Dict[str, Any]] = ()
        platform_facts: Iterable[Dict[str, Any]] = ()
    elif _facts is None:
        facts = (
            dict(row) for row in conn.execute(
                f"""
                SELECT guild_executor_key, guild_name, country, streamer_id, stat_date,
                       total_income, is_active
                FROM {DAILY_FACT_VIEW}
                WHERE app_name = 'linky' AND stat_date BETWEEN ? AND ?{filter_clause}
                """,
                [cohort_start.isoformat(), data_as_of.isoformat(), *filter_params],
            )
        )
    else:
        facts = (
            row for row in _facts
            if cohort_start.isoformat() <= str(row.get('stat_date') or '') <= data_as_of.isoformat()
            and _linky_guild_is_included(row.get('guild_name'))
            and (not guild_name or str(row.get('guild_name') or '') == guild_name)
            and (not country or str(row.get('country') or '') == country)
        )
    if not data_as_of:
        platform_facts = ()
    elif _platform_facts is None:
        platform_facts = (
            dict(row) for row in conn.execute(
                f"""
                SELECT guild_executor_key, guild_name, country, stat_date_bj, total_income
                FROM streamer_external_guild_revenue_daily
                WHERE app_name = 'linky' AND stat_date_bj BETWEEN ? AND ?{filter_clause}
                """,
                [cohort_start.isoformat(), data_as_of.isoformat(), *filter_params],
            )
        )
    else:
        platform_facts = (
            row for row in _platform_facts
            if cohort_start.isoformat() <= str(row.get('stat_date_bj') or '') <= data_as_of.isoformat()
            and _linky_guild_is_included(row.get('guild_name'))
            and (not guild_name or str(row.get('guild_name') or '') == guild_name)
            and (not country or str(row.get('country') or '') == country)
        )
    for fact in facts:
        stat_date = _iso_date(fact.get('stat_date'))
        if not stat_date:
            continue
        fact_key = (
            str(fact.get('guild_executor_key') or ''),
            str(fact.get('streamer_id') or ''),
            stat_date,
        )
        income_by_streamer_date[fact_key] += float(fact.get('total_income') or 0)
        active_by_streamer_date[fact_key] = bool(
            active_by_streamer_date[fact_key] or int(fact.get('is_active') or 0) == 1
        )
    for fact in platform_facts:
        stat_date = _iso_date(fact.get('stat_date_bj'))
        if not stat_date:
            continue
        fact_country = str(fact.get('country') or '未标注').strip() or '未标注'
        platform_income[(fact_country, _week_start(stat_date))] += float(fact.get('total_income') or 0)

    if _observed_dates_by_country is None:
        observed_dates_by_country = {
            row_country: {
                parsed
                for value in _revenue_observed_dates(
                    conn,
                    app='linky',
                    start=cohort_start,
                    end=data_as_of or cohort_end,
                    guild_name=guild_name,
                    country=row_country if row_country != '未标注' else '',
                )
                if (parsed := _iso_date(value))
            }
            for _, row_country in cohort_groups
        }
    else:
        observed_dates_by_country = {
            row_country: {
                parsed
                for value in _observed_dates_by_country.get(row_country, set())
                if (parsed := _iso_date(value))
            }
            for _, row_country in cohort_groups
        }
    diamonds_per_usd = float(PLATFORM_INCOME_UNITS_PER_USD['linky'] or 0)
    rows: List[Dict[str, Any]] = []
    for (week, row_country), members in sorted(
        cohort_groups.items(),
        key=lambda item: (-item[0][0].toordinal(), item[0][1]),
    ):
        periods = []
        covered_dates = observed_dates_by_country.get(row_country, set())
        for week_no in range(TIMO_COHORT_PERIOD_COUNT):
            display_week = week_no + 1
            period_start = week + timedelta(days=week_no * 7)
            period_end = period_start + timedelta(days=6)
            expected_dates = _date_span(period_start, period_end)
            complete = bool(
                data_as_of
                and period_end <= data_as_of
                and all(day in covered_dates for day in expected_dates)
            )
            if complete:
                member_income = [
                    sum(
                        income_by_streamer_date[(
                            str(member.get('guild_executor_key') or ''),
                            str(member.get('streamer_id') or ''),
                            day,
                        )]
                        for day in expected_dates
                    )
                    for member in members
                ]
                income_diamonds = sum(member_income)
                active_streamers = sum(
                    1
                    for member in members
                    if any(
                        active_by_streamer_date[(
                            str(member.get('guild_executor_key') or ''),
                            str(member.get('streamer_id') or ''),
                            day,
                        )]
                        for day in expected_dates
                    )
                )
                denominator = len(members) if display_week == 1 else active_streamers
                per_user_usd = (
                    income_diamonds / diamonds_per_usd / denominator
                    if diamonds_per_usd and denominator else 0.0
                )
                status_reason = 'observed'
            else:
                income_diamonds = None
                active_streamers = None
                per_user_usd = None
                status_reason = (
                    'window_not_complete'
                    if not data_as_of or period_end > data_as_of
                    else 'data_coverage_incomplete'
                )
            periods.append({
                'week': display_week,
                'label': f'W{display_week}',
                'date_from': period_start.isoformat(),
                'date_to': period_end.isoformat(),
                'status': 'complete' if complete else 'incomplete',
                'status_reason': status_reason,
                'source': 'daily' if complete else '',
                'active_streamers': active_streamers,
                'income_diamonds': round(income_diamonds, 2) if income_diamonds is not None else None,
                'income_usd': round(income_diamonds / diamonds_per_usd, 2)
                if income_diamonds is not None and diamonds_per_usd else None,
                'per_user_usd': round(per_user_usd, 2) if per_user_usd is not None else None,
                'per_user_metric': 'ARPU' if display_week == 1 else 'ARPPU',
            })
        if any(period['status_reason'] == 'data_coverage_incomplete' for period in periods):
            continue
        certified = sum(1 for member in members if int(member.get('is_real_person') or 0) == 1)
        rows.append({
            'week_start': week.isoformat(),
            'week_end': (week + timedelta(days=6)).isoformat(),
            'country': row_country,
            'new_streamers': len(members),
            'certified_streamers': certified,
            'non_certified_streamers': len(members) - certified,
            'platform_week_income_diamonds': round(platform_income[(row_country, week)], 2)
            if periods and periods[0]['status'] == 'complete' else None,
            'periods': periods,
        })
    return {
        **empty_payload,
        'rows': rows,
        'definitions': {
            'active': '统计周内累计收益大于 0 的主播。',
            'arpu': '首周收益美元除以该周全部新增主播。',
            'arppu': '后续周收益美元除以该周收益活跃主播。',
            'certified': '真人认证主播，即 is_real_person=1。',
            'date_window': '最多展示截至筛选结束日最近 12 个完整的周一至周日；没有新增主播的国家周不生成 cohort 行。',
            'zero_vs_unavailable': '完整且覆盖充分的观察周按实际值展示，零收益为 0；窗口未结束或覆盖不足时不可统计。',
        },
    }


def _source_linky_revenue_observed_dates(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    guild_name: str = '',
    country: str = '',
) -> set[str]:
    """Resolve complete Linky dates only from the captured source snapshot."""
    scope, params = _linky_exclusion_scope()
    if guild_name:
        scope += ' AND guild_name = ?'
        params.append(guild_name)
    if country:
        scope += ' AND country = ?'
        params.append(country)
    expected_row = conn.execute(
        f"SELECT COUNT(DISTINCT guild_name) FROM _source_linky_enabled_guild "
        f"WHERE 1 = 1{scope}",
        params,
    ).fetchone()
    expected_guilds = int(expected_row[0] or 0) if expected_row else 0
    if not expected_guilds:
        return set()
    rows = conn.execute(
        f"""
        SELECT stat_date
        FROM _source_linky_official_daily
        WHERE stat_date BETWEEN ? AND ?{scope}
        GROUP BY stat_date
        HAVING COUNT(DISTINCT guild_name) >= ?
        """,
        [start.isoformat(), end.isoformat(), *params, expected_guilds],
    ).fetchall()
    return {str(row[0]) for row in rows if row[0]}


def _build_linky_weekly_cohorts_from_source_temp(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    guild_name: str = '',
    country: str = '',
) -> Dict[str, Any]:
    """Build one cohort payload without retaining every Linky fact in Python."""
    cohort_start, cohort_end = _timo_cohort_display_window(start, end)
    scope, params = _linky_exclusion_scope()
    if guild_name:
        scope += ' AND guild_name = ?'
        params.append(guild_name)
    if country:
        scope += ' AND country = ?'
        params.append(country)
    normalized_country_sql = "COALESCE(NULLIF(TRIM(country), ''), '未标注')"
    profile_sql = f"""
        SELECT guild_executor_key, guild_name, country, streamer_id,
               registered_date, is_real_person,
               date(
                   registered_date,
                   '-' || ((CAST(strftime('%w', registered_date) AS INTEGER) + 6) % 7) || ' days'
               ) AS cohort_week,
               {normalized_country_sql} AS normalized_country
        FROM _source_linky_profile
        WHERE length(registered_date) >= 10
          AND registered_date BETWEEN ? AND ?{scope}
        ORDER BY cohort_week DESC, normalized_country,
                 guild_executor_key, streamer_id
    """
    profile_params = [cohort_start.isoformat(), cohort_end.isoformat(), *params]
    complete_dates = _source_linky_revenue_observed_dates(
        conn,
        start=date(1970, 1, 1),
        end=date.today() + timedelta(days=1),
        guild_name=guild_name,
        country=country,
    )
    data_as_of = max((_iso_date(value) for value in complete_dates), default=None)
    observed_dates_by_country: Dict[str, set[str]] = {}
    combined_payload: Optional[Dict[str, Any]] = None
    combined_rows: List[Dict[str, Any]] = []
    profile_rows = _iter_query_dicts(conn, profile_sql, profile_params)
    for (cohort_week_value, row_country), member_rows in groupby(
        profile_rows,
        key=lambda row: (
            str(row.get('cohort_week') or ''),
            str(row.get('normalized_country') or '未标注'),
        ),
    ):
        cohort_week = _iso_date(cohort_week_value)
        if not cohort_week:
            continue
        members = list(member_rows)
        member_week_end = cohort_week + timedelta(days=6)
        if row_country not in observed_dates_by_country:
            observed_dates_by_country[row_country] = _source_linky_revenue_observed_dates(
                conn,
                start=cohort_start,
                end=data_as_of or cohort_end,
                guild_name=guild_name,
                country=row_country if row_country != '未标注' else '',
            )
        if data_as_of:
            fact_end = min(data_as_of, cohort_week + timedelta(days=83))
            facts: Iterable[Dict[str, Any]] = _iter_query_dicts(
                conn,
                f"""
                SELECT daily.guild_executor_key, daily.guild_name, daily.country,
                       daily.streamer_id, daily.stat_date,
                       daily.total_income, daily.is_active
                FROM _source_linky_daily AS daily
                JOIN (
                    SELECT guild_executor_key, streamer_id
                    FROM _source_linky_profile
                    WHERE length(registered_date) >= 10
                      AND registered_date BETWEEN ? AND ?{scope}
                      AND {normalized_country_sql} = ?
                ) AS cohort_members
                  ON cohort_members.guild_executor_key = daily.guild_executor_key
                 AND cohort_members.streamer_id = daily.streamer_id
                WHERE daily.stat_date BETWEEN ? AND ?
                """,
                [
                    cohort_week.isoformat(), member_week_end.isoformat(),
                    *params, row_country,
                    cohort_week.isoformat(), fact_end.isoformat(),
                ],
            )
            platform_country_scope = ''
            platform_country_params: List[object] = []
            if row_country != '未标注':
                platform_country_scope = f' AND {normalized_country_sql} = ?'
                platform_country_params.append(row_country)
            platform_facts: Iterable[Dict[str, Any]] = _iter_query_dicts(
                conn,
                f"""
                SELECT guild_executor_key, guild_name, country,
                       stat_date AS stat_date_bj, total_income
                FROM _source_linky_official_daily
                WHERE stat_date BETWEEN ? AND ?{scope}{platform_country_scope}
                """,
                [
                    cohort_week.isoformat(),
                    min(data_as_of, member_week_end).isoformat(),
                    *params, *platform_country_params,
                ],
            )
        else:
            facts = ()
            platform_facts = ()
        payload = _build_linky_weekly_cohorts_live(
            conn,
            start=start,
            end=end,
            guild_name=guild_name,
            country=country,
            _profiles=members,
            _facts=facts,
            _platform_facts=platform_facts,
            _data_as_of=data_as_of,
            _observed_dates_by_country={
                row_country: observed_dates_by_country[row_country],
            },
        )
        if combined_payload is None:
            combined_payload = {**payload, 'rows': []}
        combined_rows.extend(payload.get('rows') or [])

    if combined_payload is None:
        return _build_linky_weekly_cohorts_live(
            conn,
            start=start,
            end=end,
            guild_name=guild_name,
            country=country,
            _profiles=(),
            _facts=(),
            _platform_facts=(),
            _data_as_of=data_as_of,
            _observed_dates_by_country={},
        )
    combined_payload['rows'] = combined_rows
    return combined_payload


def _build_streamer_analytics_payload_live(
    conn: sqlite3.Connection,
    *,
    app_name: object = 'timo',
    date_from: object = None,
    date_to: object = None,
    guild_name: object = '',
    country: object = '',
    limit: int = 20,
) -> Dict[str, Any]:
    app = normalize_streamer_app(app_name)
    start, end = _date_window(date_from, date_to)
    guild = str(guild_name or '').strip()
    country_name = str(country or '').strip()
    limit = max(1, min(int(limit or 20), 100))
    where_scope = ''
    scope_params: List[object] = []
    if app == 'linky':
        exclusion_clause, exclusion_params = _linky_exclusion_scope()
        where_scope += exclusion_clause
        scope_params.extend(exclusion_params)
    if guild:
        where_scope += ' AND guild_name = ?'
        scope_params.append(guild)
    if country_name:
        where_scope += ' AND country = ?'
        scope_params.append(country_name)
    guild_params: List[object] = [app]
    guild_params.extend(scope_params)

    profiles = _rows(
        conn,
        f"SELECT * FROM {PROFILE_VIEW} WHERE app_name = ?{where_scope}",
        guild_params,
    )
    facts = _rows(
        conn,
        f"""
        SELECT * FROM {DAILY_FACT_VIEW}
        WHERE app_name = ?{where_scope} AND stat_date BETWEEN ? AND ?
        """,
        [*guild_params, start.isoformat(), end.isoformat()],
    )
    aliases = ('sugo', 'sogo') if app == 'sugo' else (app,)
    placeholders = ','.join('?' for _ in aliases)
    configured_scope = ''
    configured_params: List[object] = list(aliases)
    if app == 'linky':
        configured_scope, configured_exclusion_params = _linky_exclusion_scope()
        configured_params.extend(configured_exclusion_params)
    configured_all = _rows(
        conn,
        f"SELECT guild_name, COALESCE(country, '') AS country FROM guild_executors WHERE enabled = 1 AND lower(app_name) IN ({placeholders}){configured_scope} ORDER BY guild_name",
        configured_params,
    )
    configured = [
        row for row in configured_all
        if not country_name or str(row.get('country') or '').strip() == country_name
    ]
    country_scope = ''
    country_params: List[object] = [app]
    if app == 'linky':
        country_scope, country_exclusion_params = _linky_exclusion_scope()
        country_params.extend(country_exclusion_params)
    country_rows = _rows(
        conn,
        f"SELECT DISTINCT country FROM {PROFILE_VIEW} WHERE app_name = ? AND trim(country) <> ''{country_scope}",
        country_params,
    )
    countries = sorted({
        str(row.get('country') or '').strip()
        for row in [*configured_all, *country_rows]
        if str(row.get('country') or '').strip()
    })
    revenue_observed_dates = _revenue_observed_dates(
        conn,
        app=app,
        start=start,
        end=end,
        guild_name=guild,
        country=country_name,
    )
    revenue_data_as_of = _revenue_data_as_of(
        conn,
        app=app,
        guild_name=guild,
        country=country_name,
    )
    newcomer_start, newcomer_end = _newcomer_analysis_window(start, end, revenue_data_as_of)

    profile_by_key = {
        (str(row['guild_executor_key']), str(row['streamer_id'])): row for row in profiles
    }
    profile_count_by_guild: Dict[str, int] = defaultdict(int)
    new_count_by_guild: Dict[str, int] = defaultdict(int)
    for row in profiles:
        profile_count_by_guild[str(row['guild_name'])] += 1
        registered = _iso_date(row.get('registered_date'))
        if registered and start <= registered <= end:
            new_count_by_guild[str(row['guild_name'])] += 1

    guild_names = {str(row['guild_name']) for row in configured}
    guild_names.update(str(row['guild_name']) for row in profiles)
    revenue_by_guild: Dict[str, float] = defaultdict(float)
    active_by_guild: Dict[str, set[str]] = defaultdict(set)
    revenue_by_streamer: Dict[tuple[str, str], float] = defaultdict(float)
    active_streamers: set[str] = set()
    trend: Dict[str, Dict[str, Any]] = defaultdict(lambda: {'revenue': 0.0, 'active': set(), 'new': 0})
    for row in facts:
        guild_value = str(row['guild_name'])
        streamer_key = (str(row['guild_executor_key']), str(row['streamer_id']))
        income = float(row['total_income'] or 0)
        revenue_by_guild[guild_value] += income
        revenue_by_streamer[streamer_key] += income
        trend[str(row['stat_date'])]['revenue'] += income
        trend[str(row['stat_date'])]['new'] += int(row['is_new'] or 0)
        if row['is_active'] == 1:
            active_streamers.add(str(row['streamer_id']))
            active_by_guild[guild_value].add(str(row['streamer_id']))
            trend[str(row['stat_date'])]['active'].add(str(row['streamer_id']))

    if app == 'linky':
        new_count_by_guild, new_count_by_date = _linky_new_streamer_counts(
            conn,
            start=start,
            end=end,
            guild_name=guild,
            country=country_name,
        )
        for values in trend.values():
            values['new'] = 0
        for stat_date, count in new_count_by_date.items():
            trend[stat_date]['new'] = count
        official_rows = _rows(
            conn,
            f"""
            SELECT guild_name, stat_date_bj, total_income
            FROM streamer_external_guild_revenue_daily
            WHERE app_name = 'linky'{where_scope} AND stat_date_bj BETWEEN ? AND ?
            """,
            [*scope_params, start.isoformat(), end.isoformat()],
        )
        if official_rows:
            revenue_by_guild = defaultdict(float)
            official_by_date: Dict[str, float] = defaultdict(float)
            for row in official_rows:
                revenue_by_guild[str(row['guild_name'])] += float(row['total_income'] or 0)
                official_by_date[str(row['stat_date_bj'])] += float(row['total_income'] or 0)
            for stat_date, total_income in official_by_date.items():
                trend[stat_date]['revenue'] = total_income

    external_revenue_count = 0
    if app in {'linky', 'sugo'}:
        external_revenue_count = int(conn.execute(
            "SELECT COUNT(*) FROM streamer_external_revenue_daily WHERE app_name = ?",
            (app,),
        ).fetchone()[0] or 0)
    revenue_available = bool(revenue_observed_dates)
    capabilities = dict(APP_CAPABILITIES[app])
    if app == 'sugo':
        capabilities.update({
            'daily_revenue': True,
            'newcomer_revenue': True,
            'revenue_retention': True,
            'source_status': 'ready' if external_revenue_count else 'pending',
            'source_note': '已接入 Sugo 主播日收益历史表。' if external_revenue_count else 'Sugo 收益接口已接通，等待首次历史补采。',
        })
        _apply_external_sync_status(
            capabilities,
            app=app,
            latest_sync=_latest_authoritative_external_sync(conn, app),
            external_revenue_count=external_revenue_count,
        )
    elif app == 'linky':
        _apply_external_sync_status(
            capabilities,
            app=app,
            latest_sync=_latest_authoritative_external_sync(conn, app),
            external_revenue_count=external_revenue_count,
        )
    capabilities['syncing'] = _streamer_sync_running(conn, app)
    _apply_revenue_freshness(capabilities, app=app, end=end, data_as_of=revenue_data_as_of)
    guild_rows = [
        {
            'guild_name': name,
            'streamer_count': profile_count_by_guild[name],
            'new_streamers': new_count_by_guild[name],
            'active_streamers': len(active_by_guild[name]) if revenue_available else None,
            'total_income': round(revenue_by_guild[name], 2) if revenue_available else None,
        }
        for name in sorted(guild_names, key=lambda item: revenue_by_guild[item], reverse=True)
    ]

    ranking = []
    for key, row in profile_by_key.items():
        registered = _iso_date(row.get('registered_date'))
        if not revenue_available and not (registered and start <= registered <= end):
            continue
        ranking.append({
            'guild_name': row['guild_name'],
            'streamer_id': row['streamer_id'],
            'display_name': row['display_name'],
            'registered_date': row['registered_date'],
            'total_income': round(revenue_by_streamer[key], 2) if revenue_available else None,
        })
    ranking.sort(key=lambda row: (row['total_income'] or 0, row['registered_date'] or ''), reverse=True)

    newcomer_data_as_of = revenue_data_as_of or end
    if app == 'linky' and revenue_data_as_of is not None:
        newcomer_data_as_of = _linky_newcomer_complete_data_as_of(
            conn,
            requested_data_as_of=revenue_data_as_of,
            observation_start=newcomer_start,
            guild_name=guild,
            country=country_name,
        )
        if newcomer_data_as_of < revenue_data_as_of:
            capabilities['newcomer_source_status'] = 'fallback'
            capabilities['newcomer_source_note'] = (
                f'新人指标已回退到最近连续完整日 '
                f'{newcomer_data_as_of.isoformat()}；平台总收益仍使用 '
                f'{revenue_data_as_of.isoformat()}。'
            )
    newcomer_revenue = []
    retention = []
    newcomer_metric_ranges: Dict[str, Dict[str, str]] = {}
    if revenue_available:
        data_as_of = newcomer_data_as_of
        cohort = [
            row for row in profiles
            if (d := _iso_date(row.get('registered_date'))) and newcomer_start <= d <= min(end, data_as_of)
        ]
        cohort_facts = _rows(
            conn,
            f"""
            SELECT * FROM {DAILY_FACT_VIEW}
            WHERE app_name = ?{where_scope} AND stat_date BETWEEN ? AND ?
            """,
            [
                app,
                *scope_params,
                newcomer_start.isoformat(),
                min(data_as_of, end + timedelta(days=29)).isoformat(),
            ],
        )
        facts_by_streamer: Dict[tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        for row in cohort_facts:
            facts_by_streamer[(str(row['guild_executor_key']), str(row['streamer_id']))].append(row)
        linky_observed_dates_by_guild: Dict[str, set[str]] = defaultdict(set)
        if app == 'linky':
            observation_end = min(data_as_of, end + timedelta(days=30))
            observed_rows = _rows(
                conn,
                f"""
                SELECT guild_executor_key, stat_date_bj, reconciliation_delta
                FROM streamer_external_guild_revenue_daily
                WHERE app_name = 'linky'{where_scope}
                  AND stat_date_bj BETWEEN ? AND ?
                """,
                [
                    *scope_params,
                    newcomer_start.isoformat(),
                    observation_end.isoformat(),
                ],
            )
            for row in observed_rows:
                if abs(float(row.get('reconciliation_delta') or 0)) <= 0.000001:
                    linky_observed_dates_by_guild[str(row['guild_executor_key'])].add(
                        str(row['stat_date_bj'])
                    )
        for days in (1, 7, 30):
            metric_end = min(
                end,
                data_as_of - timedelta(days=days - 1),
            )
            mature = [
                row for row in cohort
                if newcomer_start <= (_iso_date(row['registered_date']) or end) <= metric_end
                and (
                    app != 'linky'
                    or _date_window_is_covered(
                        linky_observed_dates_by_guild.get(str(row['guild_executor_key']), set()),
                        _iso_date(row['registered_date']) or end,
                        (_iso_date(row['registered_date']) or end) + timedelta(days=days - 1),
                    )
                )
            ] if metric_end >= newcomer_start else []
            if app == 'linky' and metric_end >= newcomer_start:
                newcomer_count_by_guild, _ = _linky_new_streamer_counts(
                    conn,
                    start=newcomer_start,
                    end=metric_end,
                    guild_name=guild,
                    country=country_name,
                )
                official_cohort_count = sum(newcomer_count_by_guild.values())
                # The displayed denominator must be the same frozen identity
                # set used by the numerator.  The daily aggregate is only an
                # independent completeness guard; it is never a substitute
                # denominator.
                cohort_count = len(mature)
            else:
                official_cohort_count = len(mature)
                cohort_count = len(mature)
            metric_complete = app != 'linky' or _linky_newcomer_metric_is_complete(
                conn,
                newcomer_start=newcomer_start,
                newcomer_end=metric_end,
                observation_start_offset=0,
                observation_end_offset=days - 1,
                profile_count=len(mature),
                official_count=official_cohort_count,
                guild_name=guild,
                country=country_name,
            ) if metric_end >= newcomer_start else False
            total = 0.0
            for row in mature:
                registered = _iso_date(row['registered_date'])
                key = (str(row['guild_executor_key']), str(row['streamer_id']))
                total += sum(
                    float(fact['total_income'] or 0)
                    for fact in facts_by_streamer[key]
                    if registered and 0 <= ((_iso_date(fact['stat_date']) or registered) - registered).days < days
                )
            newcomer_metric_ranges[f'income_d{days}'] = {
                'date_from': newcomer_start.isoformat(),
                'date_to': metric_end.isoformat(),
            } if metric_end >= newcomer_start else {}
            newcomer_revenue.append({
                'days': days,
                'cohort_count': cohort_count,
                'total_income': round(total, 2) if metric_complete else None,
                'avg_income': round(total / cohort_count, 2) if cohort_count and metric_complete else None,
            })
        for offset in (1, 7, 30):
            day_offset = RETENTION_DAY_OFFSETS[offset]
            metric_end = min(
                end,
                data_as_of - timedelta(days=day_offset),
                newcomer_end if offset == 30 else end,
            )
            mature = [
                row for row in cohort
                if newcomer_start <= (_iso_date(row.get('registered_date')) or data_as_of) <= metric_end
                and (
                    app != 'linky'
                    or (
                        (_iso_date(row.get('registered_date')) or data_as_of)
                        + timedelta(days=day_offset)
                    ).isoformat() in linky_observed_dates_by_guild.get(
                        str(row['guild_executor_key']), set()
                    )
                )
            ] if metric_end >= newcomer_start else []
            if app == 'linky' and metric_end >= newcomer_start:
                newcomer_count_by_guild, _ = _linky_new_streamer_counts(
                    conn,
                    start=newcomer_start,
                    end=metric_end,
                    guild_name=guild,
                    country=country_name,
                )
                official_cohort_count = sum(newcomer_count_by_guild.values())
                retention_cohort_count = len(mature)
            else:
                official_cohort_count = len(mature)
                retention_cohort_count = len(mature)
            metric_complete = app != 'linky' or _linky_newcomer_metric_is_complete(
                conn,
                newcomer_start=newcomer_start,
                newcomer_end=metric_end,
                observation_start_offset=day_offset,
                observation_end_offset=day_offset,
                profile_count=len(mature),
                official_count=official_cohort_count,
                guild_name=guild,
                country=country_name,
            ) if metric_end >= newcomer_start else False
            measurable = retention_cohort_count > 0 and bool(mature) and metric_complete
            retained = None
            if measurable:
                retained = 0
                for row in mature:
                    registered = _iso_date(row['registered_date'])
                    key = (str(row['guild_executor_key']), str(row['streamer_id']))
                    if any(
                        fact['is_active'] == 1
                        and registered
                        and ((_iso_date(fact['stat_date']) or registered) - registered).days == day_offset
                        for fact in facts_by_streamer[key]
                    ):
                        retained += 1
            newcomer_metric_ranges[f'retention_d{offset}'] = {
                'date_from': newcomer_start.isoformat(),
                'date_to': metric_end.isoformat(),
            } if metric_end >= newcomer_start else {}
            retention.append({
                'day': offset,
                'eligible': retention_cohort_count,
                'retained': retained,
                'rate': _ratio(retained, retention_cohort_count) if retained is not None else None,
            })

    if app == 'linky' and revenue_data_as_of is not None and newcomer_data_as_of < revenue_data_as_of:
        capabilities['newcomer_source_status'] = 'fallback'
        capabilities['newcomer_source_note'] = (
            f'新人指标已回退到最近连续完整日 '
            f'{newcomer_data_as_of.isoformat()}；平台总收益仍使用 '
            f'{revenue_data_as_of.isoformat()}。'
        )

    new_streamers = sum(new_count_by_guild.values())
    if app == 'timo':
        weekly_cohorts = build_timo_weekly_cohorts(
            conn, start=start, end=end, guild_name=guild, country=country_name,
        )
    elif app == 'linky':
        weekly_cohorts = build_linky_weekly_cohorts(
            conn, start=start, end=end, guild_name=guild, country=country_name,
        )
    else:
        weekly_cohorts = {'available': False, 'data_as_of': None, 'rows': []}
    return {
        'app': app,
        'app_label': APP_LABELS[app],
        'income_units_per_usd': PLATFORM_INCOME_UNITS_PER_USD[app],
        'date_from': start.isoformat(),
        'date_to': end.isoformat(),
        'country': country_name,
        'guild_name': guild,
        'capabilities': capabilities,
        'summary': {
            'guild_count': len(guild_names),
            'streamer_count': len(profiles),
            'new_streamers': new_streamers,
            'active_streamers': len(active_streamers) if revenue_available else None,
            'total_income': round(sum(revenue_by_guild.values()), 2) if revenue_available else None,
        },
        'newcomer_revenue': newcomer_revenue,
        'retention': retention,
        'newcomer_metric_ranges': newcomer_metric_ranges,
        'newcomer_data_as_of': newcomer_data_as_of.isoformat(),
        'newcomer_cohort_date_from': newcomer_start.isoformat(),
        'newcomer_cohort_date_to': newcomer_end.isoformat(),
        'weekly_cohorts': weekly_cohorts,
        'countries': countries,
        'guild_options': [
            {
                'guild_name': str(row.get('guild_name') or ''),
                'country': str(row.get('country') or ''),
            }
            for row in configured_all
            if str(row.get('guild_name') or '').strip()
        ],
        'guilds': guild_rows,
        'streamers': ranking[:limit],
        'trend': [
            {
                'date': stat_date,
                'new_streamers': values['new'],
                'active_streamers': len(values['active']) if stat_date in revenue_observed_dates else None,
                'total_income': round(values['revenue'], 2) if stat_date in revenue_observed_dates else None,
            }
            for stat_date, values in sorted(trend.items())
        ],
        'definitions': {
            'newcomer_revenue': '各指标使用从新人样本起始日至其最新成熟注册日的全部主播。首 N 日收益为注册日开始连续 N 个自然日累计收益。',
            'revenue_retention': 'D1/D7/D30 收益活跃留存分别统计注册后第 1/7/30 天收益大于 0 的主播。各指标按自己的成熟注册范围计算分母。',
        },
        'generated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'data_as_of': revenue_data_as_of.isoformat() if revenue_data_as_of else '',
    }


def _materialized_scope(country: str = '', guild_name: str = '', *, alias: str = '') -> tuple[str, List[object]]:
    prefix = f'{alias}.' if alias else ''
    clause = ''
    params: List[object] = []
    if guild_name:
        clause += f' AND {prefix}guild_name = ?'
        params.append(guild_name)
    if country:
        clause += f' AND {prefix}country = ?'
        params.append(country)
    return clause, params


def _linky_new_streamer_counts(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    guild_name: str = '',
    country: str = '',
) -> tuple[Dict[str, int], Dict[str, int]]:
    conditions = ['stats.stat_date BETWEEN ? AND ?']
    params: List[object] = [start.isoformat(), end.isoformat()]
    if LINKY_ANALYTICS_EXCLUDED_GUILDS:
        placeholders = ','.join('?' for _ in LINKY_ANALYTICS_EXCLUDED_GUILDS)
        conditions.append(f'stats.guild_name NOT IN ({placeholders})')
        params.extend(sorted(LINKY_ANALYTICS_EXCLUDED_GUILDS))
    if guild_name:
        conditions.append('stats.guild_name = ?')
        params.append(guild_name)
    if country:
        conditions.append('executors.country = ?')
        params.append(country)
    rows = _rows(
        conn,
        f"""
        SELECT stats.guild_name, stats.stat_date,
               MAX(COALESCE(stats.joined_count, 0)) AS new_streamers
        FROM guild_anchor_daily_stats AS stats
        JOIN (
            SELECT guild_name, MAX(COALESCE(country, '')) AS country
            FROM guild_executors
            WHERE lower(COALESCE(app_name, 'linky')) = 'linky'
            GROUP BY guild_name
        ) AS executors
          ON executors.guild_name = stats.guild_name
        WHERE {' AND '.join(conditions)}
        GROUP BY stats.guild_name, stats.stat_date
        """,
        params,
    )
    by_guild: Dict[str, int] = defaultdict(int)
    by_date: Dict[str, int] = defaultdict(int)
    for row in rows:
        count = int(row.get('new_streamers') or 0)
        by_guild[str(row['guild_name'])] += count
        by_date[str(row['stat_date'])] += count
    return by_guild, by_date


def _revenue_source_scope(guild_name: str = '', country: str = '') -> tuple[str, List[object]]:
    clause = ''
    params: List[object] = []
    if guild_name:
        clause += ' AND guild_name = ?'
        params.append(guild_name)
    if country:
        clause += ' AND country = ?'
        params.append(country)
    return clause, params


def _revenue_observed_dates(
    conn: sqlite3.Connection,
    *,
    app: str,
    start: date,
    end: date,
    guild_name: str = '',
    country: str = '',
) -> set[str]:
    if app == 'linky':
        executor_scope, params = _linky_exclusion_scope(alias='executors')
        if guild_name:
            executor_scope += ' AND executors.guild_name = ?'
            params.append(guild_name)
        if country:
            executor_scope += ' AND executors.country = ?'
            params.append(country)
        rows = conn.execute(
            f"""
            WITH expected_guilds AS (
                SELECT DISTINCT executors.guild_name
                FROM guild_executors AS executors
                WHERE executors.enabled = 1
                  AND lower(COALESCE(executors.app_name, '')) = 'linky'
                  {executor_scope}
            )
            SELECT revenue.stat_date_bj
            FROM streamer_external_guild_revenue_daily AS revenue
            JOIN expected_guilds AS expected
              ON expected.guild_name = revenue.guild_name
            WHERE revenue.app_name = 'linky'
              AND revenue.stat_date_bj BETWEEN ? AND ?
            GROUP BY revenue.stat_date_bj
            HAVING COUNT(DISTINCT revenue.guild_name) = (
                SELECT COUNT(*) FROM expected_guilds
            )
            """,
            [*params, start.isoformat(), end.isoformat()],
        ).fetchall()
        return {str(row[0]) for row in rows if row[0]}
    scope, params = _revenue_source_scope(guild_name, country)
    if app == 'sugo':
        table = 'streamer_external_revenue_daily'
        date_column = 'stat_date_bj'
        base = "app_name = 'sugo'"
    else:
        table = 'timo_external_revenue_daily'
        date_column = 'stat_date_bj'
        base = 'provisional = 0'
    rows = conn.execute(
        f"SELECT DISTINCT {date_column} FROM {table} "
        f"WHERE {base}{scope} AND {date_column} BETWEEN ? AND ?",
        [*params, start.isoformat(), end.isoformat()],
    ).fetchall()
    return {str(row[0]) for row in rows if row[0]}


def _date_window_is_covered(observed_dates: set[str], start: date, end: date) -> bool:
    return start <= end and all(day.isoformat() in observed_dates for day in _date_span(start, end))


def _linky_newcomer_metric_is_complete(
    conn: sqlite3.Connection,
    *,
    newcomer_start: date,
    newcomer_end: date,
    observation_start_offset: int,
    observation_end_offset: int,
    profile_count: int,
    official_count: int,
    guild_name: str = '',
    country: str = '',
) -> bool:
    """Return true only when Linky cohort identities and every observation day exist."""
    if official_count <= 0 or profile_count < official_count:
        return False
    observation_start = newcomer_start + timedelta(days=observation_start_offset)
    observation_end = newcomer_end + timedelta(days=observation_end_offset)
    observed_dates = _revenue_observed_dates(
        conn,
        app='linky',
        start=observation_start,
        end=observation_end,
        guild_name=guild_name,
        country=country,
    )
    return _date_window_is_covered(observed_dates, observation_start, observation_end)


def _revenue_data_as_of(
    conn: sqlite3.Connection,
    *,
    app: str,
    guild_name: str = '',
    country: str = '',
) -> Optional[date]:
    if app == 'linky':
        observed = _revenue_observed_dates(
            conn,
            app=app,
            start=date(1970, 1, 1),
            end=date.today() + timedelta(days=1),
            guild_name=guild_name,
            country=country,
        )
        return max((_iso_date(value) for value in observed), default=None)
    scope, params = _revenue_source_scope(guild_name, country)
    if app == 'sugo':
        table = 'streamer_external_revenue_daily'
        base = "app_name = 'sugo'"
    else:
        table = 'timo_external_revenue_daily'
        base = 'provisional = 0'
    row = conn.execute(
        f"SELECT MAX(stat_date_bj) FROM {table} WHERE {base}{scope}",
        params,
    ).fetchone()
    observed_max = _iso_date(row[0] if row else None)
    if app != 'timo' or not observed_max:
        return observed_max
    try:
        run_row = conn.execute(
            """
            SELECT run.data_date_bj
            FROM timo_external_sync_runs AS run
            WHERE run.status = 'success'
              AND run.data_date_bj <= ?
              AND run.created_at = (
                  SELECT MAX(latest.created_at)
                  FROM timo_external_sync_runs AS latest
                  WHERE latest.data_date_bj = run.data_date_bj
              )
            ORDER BY run.data_date_bj DESC
            LIMIT 1
            """,
            (_timo_revenue_latest_complete_day_bj().isoformat(),),
        ).fetchone()
        run_count = int(conn.execute('SELECT COUNT(*) FROM timo_external_sync_runs').fetchone()[0] or 0)
    except sqlite3.OperationalError:
        run_row = None
        run_count = 0
    latest_success = _iso_date(run_row[0] if run_row else None)
    if latest_success:
        return min(observed_max, latest_success)
    return None if run_count else observed_max


def _materialized_revenue_data_as_of(
    conn: sqlite3.Connection,
    *,
    app: str,
    guild_name: str = '',
    country: str = '',
) -> Optional[date]:
    """Keep materialized reads inside the currently published generation."""
    source_data_as_of = _revenue_data_as_of(
        conn,
        app=app,
        guild_name=guild_name,
        country=country,
    )
    state = conn.execute(
        "SELECT data_as_of FROM streamer_analytics_materialization_state "
        "WHERE app_name = ? AND status = 'ready'",
        (app,),
    ).fetchone()
    published_data_as_of = _iso_date(state[0]) if state else None
    if published_data_as_of is None:
        return source_data_as_of
    if source_data_as_of is None:
        return published_data_as_of
    return min(source_data_as_of, published_data_as_of)


def _assert_linky_support_coverage(
    conn: sqlite3.Connection,
    *,
    expected_data_as_of: str,
) -> None:
    support_data_as_of = _revenue_data_as_of(conn, app='linky')
    support_text = (
        support_data_as_of.isoformat()
        if support_data_as_of
        else ''
    )
    if support_text != str(expected_data_as_of or ''):
        raise RuntimeError(
            'linky_support_coverage_mismatch:'
            f'expected={expected_data_as_of}:actual={support_text}'
        )


def _timo_revenue_latest_complete_day_bj(now: Optional[datetime] = None) -> date:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current_bj = current.astimezone(ZoneInfo('Asia/Shanghai'))
    complete_day_offset = 1 if current_bj.hour >= 16 else 2
    return current_bj.date() - timedelta(days=complete_day_offset)


def _apply_revenue_freshness(
    capabilities: Dict[str, Any],
    *,
    app: str,
    end: date,
    data_as_of: Optional[date],
    now: Optional[datetime] = None,
) -> None:
    expected_complete_end = end
    if app == 'timo':
        expected_complete_end = min(end, _timo_revenue_latest_complete_day_bj(now))
    if not data_as_of:
        capabilities['source_status'] = 'pending'
        capabilities['source_note'] = '收益数据尚未同步。'
    elif data_as_of < expected_complete_end and capabilities.get('source_status') != 'partial':
        capabilities['source_status'] = 'partial'
        capabilities['source_note'] = f'收益数据截至 {data_as_of.isoformat()}。'


def _parse_sync_timestamp(value: object) -> Optional[datetime]:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _streamer_sync_running(
    conn: sqlite3.Connection,
    app: str,
    *,
    now: Optional[datetime] = None,
) -> bool:
    try:
        if app == 'timo':
            rows = conn.execute(
                "SELECT COALESCE(NULLIF(updated_at, ''), created_at) "
                "FROM timo_external_sync_runs WHERE status = 'running'"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT COALESCE(NULLIF(updated_at, ''), created_at) "
                "FROM streamer_external_sync_runs "
                "WHERE app_name = ? AND status = 'running'",
                (app,),
            ).fetchall()
    except sqlite3.OperationalError:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    return any(
        timestamp is not None
        and timedelta(0) <= current - timestamp <= timedelta(seconds=STREAMER_SYNC_RUNNING_STALE_SECONDS)
        for row in rows
        if (timestamp := _parse_sync_timestamp(row[0])) is not None
    )


def _linky_newcomer_complete_data_as_of(
    conn: sqlite3.Connection,
    *,
    requested_data_as_of: date,
    observation_start: date,
    guild_name: str = '',
    country: str = '',
) -> date:
    """Pin newcomer metrics before the first missing or unreconciled Linky scope."""
    if observation_start > requested_data_as_of:
        return requested_data_as_of
    observed_dates = _revenue_observed_dates(
        conn,
        app='linky',
        start=observation_start,
        end=requested_data_as_of,
        guild_name=guild_name,
        country=country,
    )
    first_incomplete = next(
        (
            day for day in _date_span(observation_start, requested_data_as_of)
            if day.isoformat() not in observed_dates
        ),
        None,
    )
    scope, params = _linky_exclusion_scope(alias='revenue')
    if guild_name:
        scope += ' AND revenue.guild_name = ?'
        params.append(guild_name)
    if country:
        scope += ' AND revenue.country = ?'
        params.append(country)
    delta_row = conn.execute(
        "SELECT MIN(revenue.stat_date_bj) "
        "FROM streamer_external_guild_revenue_daily AS revenue "
        "WHERE revenue.app_name = 'linky' "
        "AND revenue.stat_date_bj BETWEEN ? AND ? "
        "AND ABS(COALESCE(revenue.reconciliation_delta, 0)) > 0.000001"
        f"{scope}",
        [observation_start.isoformat(), requested_data_as_of.isoformat(), *params],
    ).fetchone()
    delta_date = _iso_date(delta_row[0] if delta_row else None)
    if delta_date is not None and (first_incomplete is None or delta_date < first_incomplete):
        first_incomplete = delta_date
    return min(requested_data_as_of, first_incomplete - timedelta(days=1)) if first_incomplete else requested_data_as_of


def _latest_authoritative_external_sync(
    conn: sqlite3.Connection,
    app: str,
) -> Optional[sqlite3.Row]:
    """Return the latest full/composite run; a guild repair alone is never authoritative."""
    try:
        row = conn.execute(
            """
            SELECT status, error_code, run_scope
            FROM streamer_external_sync_runs
            WHERE app_name = ? AND run_scope IN ('full', 'composite')
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (app,),
        ).fetchone()
        if row:
            return row
        return conn.execute(
            """
            SELECT status, error_code, run_scope
            FROM streamer_external_sync_runs
            WHERE app_name = ? AND run_scope = 'legacy'
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (app,),
        ).fetchone()
    except sqlite3.OperationalError:
        return conn.execute(
            """
            SELECT status, error_code
            FROM streamer_external_sync_runs
            WHERE app_name = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (app,),
        ).fetchone()


def _apply_external_sync_status(
    capabilities: Dict[str, Any],
    *,
    app: str,
    latest_sync: Optional[sqlite3.Row],
    external_revenue_count: int,
) -> None:
    latest_status = str(latest_sync[0] or '') if latest_sync else ''
    if latest_status in {'partial', 'failed'}:
        capabilities.update({
            'source_status': 'partial',
            'source_note': f'{APP_LABELS[app]} 全量收益同步未完成，仍有数据需要重试。',
        })
    elif external_revenue_count:
        capabilities.update({
            'source_status': 'ready',
            'source_note': (
                '已接入 Linky 公会后台主播日收益，并按主播和公会落库。'
                if app == 'linky'
                else '已接入 Sugo 主播日收益历史表。'
            ),
        })


def _materialized_capabilities(conn: sqlite3.Connection, app: str) -> tuple[Dict[str, Any], bool]:
    capabilities = dict(APP_CAPABILITIES[app])
    external_revenue_count = 0
    if app in {'linky', 'sugo'}:
        external_revenue_count = int(conn.execute(
            'SELECT COUNT(*) FROM streamer_external_revenue_daily WHERE app_name = ?',
            (app,),
        ).fetchone()[0] or 0)
    revenue_available = bool(capabilities['daily_revenue'] or external_revenue_count)
    if app == 'sugo':
        capabilities.update({
            'daily_revenue': True,
            'newcomer_revenue': True,
            'revenue_retention': True,
            'source_status': 'ready' if external_revenue_count else 'pending',
            'source_note': '已接入 Sugo 主播日收益历史表。' if external_revenue_count else 'Sugo 收益接口已接通，等待首次历史补采。',
        })
        _apply_external_sync_status(
            capabilities,
            app=app,
            latest_sync=_latest_authoritative_external_sync(conn, app),
            external_revenue_count=external_revenue_count,
        )
        revenue_available = True
    elif app == 'linky':
        _apply_external_sync_status(
            capabilities,
            app=app,
            latest_sync=_latest_authoritative_external_sync(conn, app),
            external_revenue_count=external_revenue_count,
        )
    capabilities['syncing'] = _streamer_sync_running(conn, app)
    return capabilities, revenue_available


def _assert_streamer_materialization_parity(
    conn: sqlite3.Connection,
    *,
    app: str,
    expected_profile_count: int,
    expected_streamer_daily_count: int,
    expected_daily_summary_count: int,
    expected_newcomer_count: int,
    expected_streamer_income: float,
    expected_platform_income: float,
    expected_newcomer_income: Dict[int, float],
) -> Dict[str, Any]:
    counts = {
        'profile_count': conn.execute(
            'SELECT COUNT(*) FROM streamer_analytics_profile_summary WHERE app_name = ?', (app,),
        ).fetchone()[0],
        'streamer_daily_count': conn.execute(
            'SELECT COUNT(*) FROM streamer_analytics_streamer_daily_summary WHERE app_name = ?', (app,),
        ).fetchone()[0],
        'daily_summary_count': conn.execute(
            'SELECT COUNT(*) FROM streamer_analytics_daily_summary WHERE app_name = ?', (app,),
        ).fetchone()[0],
        'newcomer_count': conn.execute(
            'SELECT COUNT(*) FROM streamer_analytics_newcomer_summary WHERE app_name = ?', (app,),
        ).fetchone()[0],
    }
    expected_counts = {
        'profile_count': expected_profile_count,
        'streamer_daily_count': expected_streamer_daily_count,
        'daily_summary_count': expected_daily_summary_count,
        'newcomer_count': expected_newcomer_count,
    }
    income_row = conn.execute(
        """
        SELECT
            COALESCE((SELECT SUM(total_income) FROM streamer_analytics_streamer_daily_summary WHERE app_name = ?), 0),
            COALESCE((SELECT SUM(total_income) FROM streamer_analytics_daily_summary WHERE app_name = ?), 0),
            COALESCE((SELECT SUM(income_d1) FROM streamer_analytics_newcomer_summary WHERE app_name = ? AND mature_income_d1 = 1), 0),
            COALESCE((SELECT SUM(income_d7) FROM streamer_analytics_newcomer_summary WHERE app_name = ? AND mature_income_d7 = 1), 0),
            COALESCE((SELECT SUM(income_d30) FROM streamer_analytics_newcomer_summary WHERE app_name = ? AND mature_income_d30 = 1), 0)
        """,
        (app, app, app, app, app),
    ).fetchone()
    actual_income = {
        'streamer': float(income_row[0] or 0),
        'platform': float(income_row[1] or 0),
        'newcomer_d1': float(income_row[2] or 0),
        'newcomer_d7': float(income_row[3] or 0),
        'newcomer_d30': float(income_row[4] or 0),
    }
    expected_income = {
        'streamer': expected_streamer_income,
        'platform': expected_platform_income,
        'newcomer_d1': expected_newcomer_income[1],
        'newcomer_d7': expected_newcomer_income[7],
        'newcomer_d30': expected_newcomer_income[30],
    }
    count_mismatch = {key: (expected_counts[key], counts[key]) for key in counts if expected_counts[key] != counts[key]}
    income_mismatch = {
        key: (expected_income[key], actual_income[key])
        for key in actual_income
        if not math.isclose(
            expected_income[key],
            actual_income[key],
            rel_tol=1e-12,
            abs_tol=0.000001,
        )
    }
    if count_mismatch or income_mismatch:
        detail = json.dumps(
            {'app': app, 'count_mismatch': count_mismatch, 'income_mismatch': income_mismatch},
            ensure_ascii=False,
            separators=(',', ':'),
        )
        raise RuntimeError(f'streamer_materialization_parity_failed:{detail}')
    return {'counts': counts, 'income': actual_income}


def _stage_materialized_rows(
    conn: sqlite3.Connection,
    *,
    stage_table: str,
    source_table: str,
    columns: tuple[str, ...],
    rows: Iterable[tuple[object, ...]],
) -> None:
    """Build a connection-local snapshot without holding the main DB write lock."""
    column_sql = ', '.join(columns)
    placeholders = ', '.join('?' for _ in columns)
    conn.execute(f'DROP TABLE IF EXISTS temp.{stage_table}')
    conn.execute(
        f'CREATE TEMP TABLE {stage_table} AS '
        f'SELECT {column_sql} FROM {source_table} WHERE 0'
    )
    conn.executemany(
        f'INSERT INTO {stage_table} ({column_sql}) VALUES ({placeholders})',
        rows,
    )


def _iter_query_tuples(
    conn: sqlite3.Connection,
    sql: str,
    params: Iterable[object] = (),
    *,
    batch_size: int = 2048,
) -> Iterator[tuple[object, ...]]:
    """Stream a SELECT into sqlite executemany without a Python-sized fetchall."""
    cursor = conn.execute(sql, tuple(params))
    try:
        while True:
            batch = cursor.fetchmany(max(1, int(batch_size or 2048)))
            if not batch:
                return
            for row in batch:
                yield tuple(row)
    finally:
        cursor.close()


def _iter_query_dicts(
    conn: sqlite3.Connection,
    sql: str,
    params: Iterable[object] = (),
    *,
    batch_size: int = 2048,
) -> Iterator[Dict[str, Any]]:
    """Stream sqlite rows as dictionaries and defer opening the cursor."""
    cursor = conn.execute(sql, tuple(params))
    while True:
        batch = cursor.fetchmany(max(1, int(batch_size or 2048)))
        if not batch:
            return
        for row in batch:
            yield dict(row)


def _stage_materialized_query(
    source_conn: sqlite3.Connection,
    publish_conn: sqlite3.Connection,
    *,
    stage_table: str,
    source_table: str,
    columns: tuple[str, ...],
    query: str,
    params: Iterable[object] = (),
) -> None:
    _stage_materialized_rows(
        publish_conn,
        stage_table=stage_table,
        source_table=source_table,
        columns=columns,
        rows=_iter_query_tuples(source_conn, query, params),
    )


def _replace_offline_materialized_rows_in_batches(
    conn: sqlite3.Connection,
    *,
    target_table: str,
    columns: tuple[str, ...],
    rows: Iterable[tuple[object, ...]],
    delete_where: str,
    delete_params: Iterable[object] = (),
    batch_size: int = LINKY_OFFLINE_PUBLISH_BATCH_SIZE,
    batch_logger: Optional[Callable[[str, int, int], None]] = None,
) -> Dict[str, int]:
    """Replace one offline-candidate slice using bounded durable transactions."""
    bounded_batch_size = max(1, int(batch_size or LINKY_OFFLINE_PUBLISH_BATCH_SIZE))
    delete_values = tuple(delete_params)
    deleted_rows = 0
    delete_batches = 0
    while True:
        try:
            conn.execute('BEGIN IMMEDIATE')
            cursor = conn.execute(
                f'DELETE FROM {target_table} WHERE rowid IN ('
                f'SELECT rowid FROM {target_table} WHERE {delete_where} LIMIT ?)',
                (*delete_values, bounded_batch_size),
            )
            deleted = max(0, int(cursor.rowcount or 0))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if not deleted:
            break
        deleted_rows += deleted
        delete_batches += 1
        if batch_logger is not None:
            batch_logger('delete', delete_batches, deleted)

    column_sql = ', '.join(columns)
    placeholders = ', '.join('?' for _ in columns)
    row_iterator = iter(rows)
    inserted_rows = 0
    insert_batches = 0
    while True:
        batch = list(islice(row_iterator, bounded_batch_size))
        if not batch:
            break
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.executemany(
                f'INSERT INTO {target_table} ({column_sql}) VALUES ({placeholders})',
                batch,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        inserted_rows += len(batch)
        insert_batches += 1
        if batch_logger is not None:
            batch_logger('insert', insert_batches, len(batch))
    conn.execute('PRAGMA shrink_memory')
    return {
        'deleted_rows': deleted_rows,
        'delete_batches': delete_batches,
        'inserted_rows': inserted_rows,
        'insert_batches': insert_batches,
    }


def _assert_offline_linky_candidate_target(
    conn: sqlite3.Connection,
    candidate_path: Path,
) -> int:
    """Fail closed before allowing non-atomic, batch-committed candidate writes."""
    if conn.in_transaction:
        raise RuntimeError('offline_linky_candidate_target_transaction_active')
    row = conn.execute("PRAGMA database_list").fetchone()
    actual_path = Path(str(row[2] or '')).resolve() if row and row[2] else None
    if actual_path != candidate_path.resolve():
        raise RuntimeError('offline_linky_candidate_target_path_mismatch')
    if 'candidate' not in candidate_path.name.lower():
        raise RuntimeError('offline_linky_candidate_target_name_invalid')
    configured_active = str(os.getenv('STREAMER_ANALYTICS_DB_PATH') or '').strip()
    if configured_active and Path(configured_active).resolve() == candidate_path.resolve():
        raise RuntimeError('offline_linky_candidate_target_is_active_store')
    journal_mode = str(conn.execute('PRAGMA journal_mode').fetchone()[0] or '').lower()
    if journal_mode != 'delete':
        raise RuntimeError(
            f'offline_linky_candidate_requires_delete_journal:{journal_mode or "unknown"}'
        )
    existing_sidecars = [
        str(Path(f'{candidate_path}{suffix}'))
        for suffix in ('-journal', '-wal', '-shm')
        if Path(f'{candidate_path}{suffix}').exists()
    ]
    if existing_sidecars:
        raise RuntimeError(
            'offline_linky_candidate_sidecar_present:'
            + json.dumps(existing_sidecars, separators=(',', ':'))
        )
    state = conn.execute(
        "SELECT status FROM streamer_analytics_materialization_state "
        "WHERE app_name = 'linky'"
    ).fetchone()
    if not state or str(state[0] or '') != 'ready':
        raise RuntimeError('offline_linky_candidate_state_not_ready')
    generation = conn.execute(
        "SELECT value FROM streamer_analytics_store_meta "
        "WHERE key = 'active_generation:linky'"
    ).fetchone()
    if not generation or not str(generation[0] or ''):
        raise RuntimeError('offline_linky_candidate_generation_missing')
    original_synchronous = int(conn.execute('PRAGMA main.synchronous').fetchone()[0])
    conn.execute('PRAGMA main.synchronous=FULL')
    return original_synchronous


def _begin_offline_linky_candidate_refresh(
    conn: sqlite3.Connection,
    candidate_path: Path,
) -> int:
    original_synchronous = _assert_offline_linky_candidate_target(
        conn,
        candidate_path,
    )
    try:
        cursor = conn.execute(
            "UPDATE streamer_analytics_materialization_state "
            "SET status = 'building', error_message = ?, materialized_at = ? "
            "WHERE app_name = 'linky' AND status = 'ready'",
            (
                'offline_linky_candidate_refresh_in_progress',
                datetime.now().astimezone().isoformat(timespec='seconds'),
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError('offline_linky_candidate_state_transition_failed')
        conn.commit()
    except Exception:
        conn.rollback()
        conn.execute(f'PRAGMA main.synchronous={original_synchronous}')
        raise
    return original_synchronous


def _assert_offline_linky_candidate_refresh_started(
    conn: sqlite3.Connection,
) -> None:
    state = conn.execute(
        "SELECT status FROM streamer_analytics_materialization_state "
        "WHERE app_name = 'linky'"
    ).fetchone()
    if not state or str(state[0] or '') != 'building':
        raise RuntimeError('offline_linky_candidate_refresh_not_started')
    if int(conn.execute('PRAGMA main.synchronous').fetchone()[0]) != 2:
        raise RuntimeError('offline_linky_candidate_full_sync_not_enabled')


def _same_sqlite_database(left: sqlite3.Connection, right: sqlite3.Connection) -> bool:
    if left is right:
        return True
    def main_path(conn: sqlite3.Connection) -> str:
        row = conn.execute("PRAGMA database_list").fetchone()
        return str(row[2] or '') if row else ''
    left_path = main_path(left)
    right_path = main_path(right)
    return bool(left_path and right_path and Path(left_path).resolve() == Path(right_path).resolve())


def _validate_streamer_analytics_source_schema(conn: sqlite3.Connection) -> None:
    """Validate the established analytics schema without mutating the source DB."""
    required_types = {
        PROFILE_VIEW: 'view',
        DAILY_FACT_VIEW: 'view',
        **{name: 'table' for name in STREAMER_ANALYTICS_STORE_TABLES},
        **{name: 'table' for name in STREAMER_ANALYTICS_STORE_SUPPORT_TABLES},
        'streamer_app_fan_coverage': 'table',
    }
    names = tuple(required_types)
    placeholders = ','.join('?' for _ in names)
    actual_types = {
        str(row[0]): str(row[1])
        for row in conn.execute(
            f"SELECT name, type FROM sqlite_master WHERE name IN ({placeholders})",
            names,
        ).fetchall()
    }
    invalid = [
        f'{name}:{actual_types.get(name, "missing")}!={expected}'
        for name, expected in required_types.items()
        if actual_types.get(name) != expected
    ]
    if invalid:
        raise RuntimeError(
            f'streamer_analytics_source_schema_invalid:{",".join(invalid)}'
        )

    required_view_columns = {
        PROFILE_VIEW: {
            'app_name', 'guild_executor_key', 'guild_name', 'country',
            'streamer_id', 'display_name', 'registered_date', 'last_active_date',
            'is_real_person', 'source_updated_at',
        },
        DAILY_FACT_VIEW: {
            'app_name', 'guild_executor_key', 'guild_name', 'country',
            'streamer_id', 'stat_date', 'is_new', 'is_active', 'total_income',
        },
    }
    for view_name, required_columns in required_view_columns.items():
        try:
            cursor = conn.execute(f'SELECT * FROM {view_name} LIMIT 0')
            actual_columns = {str(column[0]) for column in cursor.description or ()}
            cursor.close()
        except sqlite3.Error as exc:
            raise RuntimeError(
                f'streamer_analytics_source_view_invalid:{view_name}:{exc}'
            ) from exc
        missing_columns = sorted(required_columns - actual_columns)
        if missing_columns:
            raise RuntimeError(
                f'streamer_analytics_source_view_columns_missing:'
                f'{view_name}:{",".join(missing_columns)}'
            )


def _emit_streamer_analytics_phase(
    phase_logger: Optional[Callable[[str], None]],
    phase: str,
) -> None:
    if phase_logger is not None:
        phase_logger(phase)


def _ensure_streamer_analytics_store_schema(
    source_conn: sqlite3.Connection,
    target_conn: sqlite3.Connection,
) -> None:
    if _same_sqlite_database(source_conn, target_conn):
        return
    store_tables = (*STREAMER_ANALYTICS_STORE_TABLES, *STREAMER_ANALYTICS_STORE_SUPPORT_TABLES)
    placeholders = ','.join('?' for _ in store_tables)
    table_rows = source_conn.execute(
        f"SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name IN ({placeholders})",
        store_tables,
    ).fetchall()
    table_sql = {str(row[0]): str(row[1] or '') for row in table_rows}
    missing = [name for name in store_tables if not table_sql.get(name)]
    if missing:
        raise RuntimeError(f'streamer_analytics_store_schema_missing:{",".join(missing)}')
    existing_tables = {
        str(row[0])
        for row in target_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    for name in store_tables:
        if name not in existing_tables:
            target_conn.execute(table_sql[name])
            continue
        if name == 'streamer_analytics_profile_summary':
            target_columns = {
                str(row[1])
                for row in target_conn.execute(f'PRAGMA table_info({name})').fetchall()
            }
            for column_name, column_sql in (
                ('is_app_fan', 'INTEGER'),
                ('app_fan_status', "TEXT NOT NULL DEFAULT 'historical_unknown'"),
                ('app_fan_source', "TEXT NOT NULL DEFAULT ''"),
            ):
                if column_name not in target_columns:
                    target_conn.execute(
                        f'ALTER TABLE {name} ADD COLUMN {column_name} {column_sql}'
                    )
        if name not in STREAMER_ANALYTICS_STORE_SUPPORT_TABLES:
            continue
        source_columns = [
            tuple(row[1:6])
            for row in source_conn.execute(f'PRAGMA table_info({name})').fetchall()
        ]
        target_columns = [
            tuple(row[1:6])
            for row in target_conn.execute(f'PRAGMA table_info({name})').fetchall()
        ]
        if source_columns != target_columns:
            # Support tables are reproducible caches. Rebuild drifted schemas
            # before positional copy rather than failing every future run.
            target_conn.execute(f'DROP TABLE {name}')
            target_conn.execute(table_sql[name])
    index_rows = source_conn.execute(
        f"SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name IN ({placeholders}) "
        "AND sql IS NOT NULL ORDER BY name",
        store_tables,
    ).fetchall()
    existing_indexes = {
        str(row[0])
        for row in target_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    for row in index_rows:
        if str(row[0]) not in existing_indexes:
            target_conn.execute(str(row[1]))
    target_conn.execute(
        "CREATE TABLE IF NOT EXISTS streamer_analytics_store_meta ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    target_conn.execute(
        "INSERT INTO streamer_analytics_store_meta (key, value, updated_at) VALUES "
        "('schema_version', '2', ?) ON CONFLICT(key) DO UPDATE SET "
        "value=excluded.value, updated_at=excluded.updated_at",
        (datetime.now(timezone.utc).isoformat(),),
    )
    target_conn.commit()


def _refresh_streamer_analytics_support_tables(
    source_conn: sqlite3.Connection,
    target_conn: sqlite3.Connection,
) -> None:
    if _same_sqlite_database(source_conn, target_conn):
        return
    if source_conn.in_transaction:
        raise RuntimeError('streamer_analytics_support_source_transaction_active')
    if target_conn.in_transaction:
        raise RuntimeError('streamer_analytics_support_target_transaction_active')
    source_started = False
    target_started = False
    try:
        # Keep a stable source snapshot without attaching it to the target write
        # transaction. ATTACH + BEGIN IMMEDIATE also reserves the source writer.
        source_conn.execute('BEGIN')
        source_started = True
        source_conn.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone()
        target_conn.execute('BEGIN IMMEDIATE')
        target_started = True
        for table in STREAMER_ANALYTICS_STORE_SUPPORT_TABLES:
            target_conn.execute(f'DELETE FROM {table}')
            select_sql = STREAMER_ANALYTICS_STORE_SUPPORT_SELECTS.get(
                table,
                f'SELECT * FROM {table}',
            )
            cursor = source_conn.execute(select_sql)
            try:
                placeholders = ','.join('?' for _ in cursor.description or ())
                if not placeholders:
                    raise RuntimeError(
                        f'streamer_analytics_support_columns_missing:{table}'
                    )
                insert_sql = f'INSERT INTO {table} VALUES ({placeholders})'
                while True:
                    rows = cursor.fetchmany(STREAMER_ANALYTICS_SUPPORT_COPY_BATCH_SIZE)
                    if not rows:
                        break
                    target_conn.executemany(
                        insert_sql,
                        (tuple(row) for row in rows),
                    )
            finally:
                cursor.close()
        target_conn.commit()
        target_started = False
    except Exception:
        if target_started and target_conn.in_transaction:
            target_conn.rollback()
            target_started = False
        raise
    finally:
        if target_started and target_conn.in_transaction:
            target_conn.rollback()
        if source_started and source_conn.in_transaction:
            source_conn.rollback()


def _connect_streamer_analytics_store(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_short_write_sqlite(
        path,
        source='streamer-analytics-store',
        timeout=60.0,
        write_window_timeout_seconds=STREAMER_ANALYTICS_PUBLISH_WRITE_WINDOW_SECONDS,
    )
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def _connect_streamer_analytics_store_with_inherited_queue_locks(
    path: Path,
) -> sqlite3.Connection:
    """Open the publish DB without recursively taking queue-owned SQLite locks."""
    inherited = {
        value
        for value in str(os.getenv('MCN_DEPLOY_EXTRA_LOCK_PATHS') or '').split(':')
        if value
    }
    required = set(STREAMER_ANALYTICS_INHERITED_QUEUE_LOCK_PATHS)
    if os.getenv('MCN_DEPLOY_QUEUE_ACTIVE') != '1' or not required.issubset(inherited):
        raise RuntimeError('streamer_analytics_inherited_queue_locks_not_proven')
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_observed_sqlite(
        str(path),
        source='streamer-analytics-store-inherited-queue-locks',
        timeout=5.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def refresh_streamer_app_fan_support_table(
    source_conn: sqlite3.Connection,
    target_conn: sqlite3.Connection,
    *,
    app_names: Iterable[object] = SUPPORTED_APPS,
    phase_logger: Optional[Callable[[str], None]] = None,
    performance_budget_seconds: float = 60.0,
) -> Dict[str, Any]:
    """Publish only App-fan identity/profile fields without rebuilding revenue facts."""
    started = time.monotonic()
    apps = tuple(dict.fromkeys(normalize_streamer_app(value) for value in app_names))

    def phase(name: str, **details: Any) -> None:
        elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
        suffix = ' '.join(f'{key}={value}' for key, value in sorted(details.items()))
        _emit_streamer_analytics_phase(
            phase_logger,
            f'app_fan_targeted.{name} elapsed_ms={elapsed_ms}' + (f' {suffix}' if suffix else ''),
        )
        if time.monotonic() - started > max(float(performance_budget_seconds), 0.1):
            raise RuntimeError(f'app_fan_targeted_performance_budget_exceeded:{name}:{elapsed_ms}ms')

    if source_conn.in_transaction:
        raise RuntimeError('app_fan_targeted_source_transaction_active')
    if target_conn.in_transaction:
        raise RuntimeError('app_fan_targeted_target_transaction_active')
    phase('start', apps=','.join(apps))
    _validate_streamer_analytics_source_schema(source_conn)
    phase('source_schema_validated')
    _ensure_streamer_analytics_store_schema(source_conn, target_conn)
    phase('target_schema_ready')

    placeholders = ','.join('?' for _ in apps)
    source_started = False
    target_started = False
    try:
        source_conn.execute('BEGIN')
        source_started = True
        identity_rows = source_conn.execute(
            "SELECT app_name,streamer_id,is_app_fan,identity_status,acquisition_source,"
            "source_record_type,first_source_record_id,last_source_record_id,"
            "first_confirmed_at,last_confirmed_at,evidence_version,created_at,updated_at "
            f"FROM streamer_app_fan_identities WHERE app_name IN ({placeholders})",
            apps,
        ).fetchall()
        history_complete = {
            app: app_fan_history_complete(
                source_conn,
                app_name=app,
                ensure_schema=False,
            )
            for app in apps
        }
        phase('source_snapshot_ready', identity_rows=len(identity_rows))

        target_conn.execute('BEGIN IMMEDIATE')
        target_started = True
        for app in apps:
            target_conn.execute(
                'DELETE FROM streamer_app_fan_identities WHERE app_name=?',
                (app,),
            )
        if identity_rows:
            target_conn.executemany(
                "INSERT INTO streamer_app_fan_identities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tuple(row) for row in identity_rows),
            )
        updated_profiles: Dict[str, int] = {}
        for app in apps:
            before = target_conn.total_changes
            coverage_start = APP_FAN_COVERAGE_START.get(app, '')
            target_conn.execute(
                """
                UPDATE streamer_analytics_profile_summary AS profile
                SET is_app_fan = CASE
                        WHEN EXISTS (
                            SELECT 1 FROM streamer_app_fan_identities AS fan
                            WHERE fan.app_name=profile.app_name
                              AND fan.streamer_id=profile.streamer_id
                              AND fan.is_app_fan=1 AND fan.identity_status='confirmed'
                        ) THEN 1
                        WHEN ?=1 THEN 0
                        WHEN registered_date>=? THEN 0
                        ELSE NULL
                    END,
                    app_fan_status = CASE
                        WHEN EXISTS (
                            SELECT 1 FROM streamer_app_fan_identities AS fan
                            WHERE fan.app_name=profile.app_name
                              AND fan.streamer_id=profile.streamer_id
                              AND fan.is_app_fan=1 AND fan.identity_status='confirmed'
                        ) THEN 'app_fan'
                        WHEN ?=1 OR registered_date>=? THEN 'non_app_fan'
                        ELSE 'historical_unknown'
                    END,
                    app_fan_source = CASE
                        WHEN EXISTS (
                            SELECT 1 FROM streamer_app_fan_identities AS fan
                            WHERE fan.app_name=profile.app_name
                              AND fan.streamer_id=profile.streamer_id
                              AND fan.is_app_fan=1 AND fan.identity_status='confirmed'
                        ) THEN 'tugao_app' ELSE '' END
                WHERE profile.app_name=?
                """,
                (
                    int(history_complete[app]), coverage_start,
                    int(history_complete[app]), coverage_start, app,
                ),
            )
            updated_profiles[app] = target_conn.total_changes - before
        target_conn.commit()
        target_started = False
        phase(
            'target_committed',
            identity_rows=len(identity_rows),
            profile_rows=sum(updated_profiles.values()),
        )
    except Exception:
        if target_started and target_conn.in_transaction:
            target_conn.rollback()
            target_started = False
        raise
    finally:
        if target_started and target_conn.in_transaction:
            target_conn.rollback()
        if source_started and source_conn.in_transaction:
            source_conn.rollback()

    elapsed_seconds = round(time.monotonic() - started, 3)
    return {
        'status': 'ready',
        'apps': list(apps),
        'identity_rows': len(identity_rows),
        'profile_rows_updated': updated_profiles,
        'history_complete': history_complete,
        'elapsed_seconds': elapsed_seconds,
        'performance_budget_seconds': float(performance_budget_seconds),
        'scope': 'app_fan_identity_and_profile_fields_only',
    }


def _configure_linky_sqlite_workload(
    conn: sqlite3.Connection,
    *,
    publish: bool = False,
) -> None:
    """Bound Linky's per-connection SQLite memory and worker footprint."""
    cache_kib = LINKY_SQLITE_PUBLISH_CACHE_KIB if publish else LINKY_SQLITE_SOURCE_CACHE_KIB
    conn.execute('PRAGMA temp_store=FILE')
    conn.execute(f'PRAGMA cache_size=-{cache_kib}')
    conn.execute(f'PRAGMA temp.cache_size=-{LINKY_SQLITE_TEMP_CACHE_KIB}')
    conn.execute('PRAGMA mmap_size=0')
    conn.execute(f'PRAGMA threads={LINKY_SQLITE_THREADS}')


def _drop_linky_temp_tables(
    conn: sqlite3.Connection,
    temp_tables: Iterable[str],
) -> None:
    """Attempt every cleanup without replacing an in-flight materialization error."""
    preserve_error = sys.exc_info()[0] is not None
    had_transaction = conn.in_transaction
    cleanup_error: Optional[Exception] = None
    for temp_table in temp_tables:
        for attempt in range(3):
            try:
                conn.execute(f'DROP TABLE IF EXISTS temp.{temp_table}')
                break
            except sqlite3.OperationalError as exc:
                if 'database schema has changed' in str(exc).lower() and attempt < 2:
                    continue
                cleanup_error = cleanup_error or exc
                break
            except Exception as exc:
                cleanup_error = cleanup_error or exc
                break
    if not had_transaction and conn.in_transaction:
        try:
            if cleanup_error is None:
                conn.commit()
            else:
                conn.rollback()
        except Exception as exc:
            cleanup_error = cleanup_error or exc
            if conn.in_transaction:
                try:
                    conn.rollback()
                except Exception as rollback_exc:
                    cleanup_error = cleanup_error or rollback_exc
    if cleanup_error is not None and not preserve_error:
        raise cleanup_error


def _reset_empty_linky_temp_store(
    conn: sqlite3.Connection,
    *,
    publish: bool = False,
) -> None:
    """Release an empty TEMP store before Linky's next file-backed phase."""
    if conn.in_transaction:
        raise RuntimeError('linky_temp_store_reset_in_transaction')
    remaining = conn.execute(
        "SELECT type, name FROM sqlite_temp_master ORDER BY type, name"
    ).fetchall()
    if remaining:
        detail = ','.join(f'{row[0]}:{row[1]}' for row in remaining)
        raise RuntimeError(f'linky_temp_store_reset_not_empty:{detail}')
    conn.execute('PRAGMA temp_store=MEMORY')
    _configure_linky_sqlite_workload(conn, publish=publish)
    conn.execute('PRAGMA shrink_memory')


def _publish_staged_streamer_analytics_app(
    publish_conn: sqlite3.Connection,
    *,
    app: str,
    publish_tables: Iterable[tuple[str, str, tuple[str, ...]]],
    cohort_table: str,
    include_cohorts: bool,
    data_as_of: str,
    profile_count: int,
    streamer_daily_count: int,
    daily_summary_count: int,
    newcomer_count: int,
    cohort_scope_count: int,
    expected_streamer_income: float,
    expected_platform_income: float,
    expected_newcomer_income: Dict[int, float],
    materialized_at: str,
) -> tuple[Dict[str, Any], str]:
    """Atomically publish one staged app snapshot or leave the old one untouched."""
    publication_id = uuid4().hex
    tables = tuple(publish_tables)
    try:
        publish_conn.execute('BEGIN IMMEDIATE')
        for table, stage_table, columns in tables:
            column_sql = ', '.join(columns)
            publish_conn.execute(f'DELETE FROM {table} WHERE app_name = ?', (app,))
            publish_conn.execute(
                f'INSERT INTO {table} ({column_sql}) '
                f'SELECT {column_sql} FROM {stage_table}'
            )
        if include_cohorts:
            cohort_column_sql = ', '.join(STREAMER_ANALYTICS_COHORT_COLUMNS)
            publish_conn.execute(f'DELETE FROM {cohort_table}')
            publish_conn.execute(
                f'INSERT INTO {cohort_table} ({cohort_column_sql}) '
                f'SELECT {cohort_column_sql} FROM _stage_streamer_analytics_cohort'
            )
        parity = _assert_streamer_materialization_parity(
            publish_conn,
            app=app,
            expected_profile_count=profile_count,
            expected_streamer_daily_count=streamer_daily_count,
            expected_daily_summary_count=daily_summary_count,
            expected_newcomer_count=newcomer_count,
            expected_streamer_income=expected_streamer_income,
            expected_platform_income=expected_platform_income,
            expected_newcomer_income=expected_newcomer_income,
        )
        if app == 'linky':
            _assert_linky_support_coverage(
                publish_conn,
                expected_data_as_of=data_as_of,
            )
        publish_conn.execute(
            """
            INSERT INTO streamer_analytics_materialization_state (
                app_name, status, data_as_of, profile_count, streamer_daily_count,
                daily_summary_count, newcomer_count, cohort_scope_count,
                error_message, materialized_at
            ) VALUES (?, 'ready', ?, ?, ?, ?, ?, ?, '', ?)
            ON CONFLICT(app_name) DO UPDATE SET
                status='ready', data_as_of=excluded.data_as_of,
                profile_count=excluded.profile_count,
                streamer_daily_count=excluded.streamer_daily_count,
                daily_summary_count=excluded.daily_summary_count,
                newcomer_count=excluded.newcomer_count,
                cohort_scope_count=excluded.cohort_scope_count,
                error_message='', materialized_at=excluded.materialized_at
            """,
            (
                app, data_as_of, profile_count, streamer_daily_count,
                daily_summary_count, newcomer_count, cohort_scope_count,
                materialized_at,
            ),
        )
        has_store_meta = publish_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='streamer_analytics_store_meta'"
        ).fetchone()
        if has_store_meta:
            active_key = f'active_generation:{app}'
            previous_row = publish_conn.execute(
                'SELECT value FROM streamer_analytics_store_meta WHERE key = ?',
                (active_key,),
            ).fetchone()
            if previous_row and str(previous_row[0] or ''):
                publish_conn.execute(
                    "INSERT INTO streamer_analytics_store_meta (key, value, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value, updated_at=excluded.updated_at",
                    (f'previous_generation:{app}', str(previous_row[0]), materialized_at),
                )
            publish_conn.execute(
                "INSERT INTO streamer_analytics_store_meta (key, value, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (active_key, publication_id, materialized_at),
            )
        publish_conn.commit()
    except Exception:
        publish_conn.rollback()
        raise
    return parity, publication_id


def _materialize_linky_streamed(
    conn: sqlite3.Connection,
    *,
    publish_conn: sqlite3.Connection,
    offline_candidate_path: Optional[Path] = None,
    source_overlay: Optional[Callable[[sqlite3.Connection], None]] = None,
    source_overlay_cleanup: Optional[Callable[[sqlite3.Connection], None]] = None,
    phase_logger: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Build Linky's large summaries in SQLite and stream only final rows."""
    app = 'linky'
    offline_candidate = offline_candidate_path is not None
    if offline_candidate:
        if publish_conn is conn:
            raise RuntimeError('offline_linky_candidate_requires_separate_target')
        _assert_offline_linky_candidate_refresh_started(publish_conn)
    materialized_at = datetime.now().astimezone().isoformat(timespec='seconds')
    data_as_of_date: Optional[date] = None
    cohort_payloads: List[tuple[str, str, str, str, str]] = []
    source_temp_tables = (
        '_source_linky_context',
        '_source_linky_enabled_guild',
        '_source_linky_profile',
        '_source_linky_snapshot_day',
        '_source_linky_newcomer_identity',
        '_source_linky_daily',
        '_source_linky_official_daily',
        '_source_linky_daily_summary',
        '_source_linky_observed_date',
        '_source_linky_newcomer',
    )
    stage_temp_tables = (
        '_stage_streamer_analytics_profile',
        '_stage_streamer_analytics_daily',
        '_stage_streamer_analytics_platform_daily',
        '_stage_streamer_analytics_newcomer',
        '_stage_streamer_analytics_cohort',
    )
    try:
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.configure.start')
        _configure_linky_sqlite_workload(conn)
        if publish_conn is not conn:
            _configure_linky_sqlite_workload(publish_conn, publish=True)
        if source_overlay is not None:
            source_overlay(conn)
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.configure.done')
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.source_snapshot.start')
        _drop_linky_temp_tables(conn, source_temp_tables)
        conn.execute('BEGIN')
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.source_metadata.start')
        data_as_of_date = _revenue_data_as_of(conn, app=app)
        data_as_of = data_as_of_date.isoformat() if data_as_of_date else ''
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.source_metadata.done')
        conn.execute(
            """
            CREATE TEMP TABLE _source_linky_context (
                data_as_of TEXT NOT NULL,
                materialized_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            'INSERT INTO _source_linky_context VALUES (?, ?)',
            (data_as_of, materialized_at),
        )
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.source_enabled_guild.start')
        conn.execute(
            """
            CREATE TEMP TABLE _source_linky_enabled_guild AS
            SELECT DISTINCT guild_name, COALESCE(country, '') AS country
            FROM guild_executors
            WHERE enabled = 1 AND lower(COALESCE(app_name, '')) = 'linky'
            """
        )
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.source_enabled_guild.done')
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.source_profile.start')
        conn.execute(
            """
            CREATE TEMP TABLE _source_linky_profile (
                app_name TEXT NOT NULL,
                guild_executor_key TEXT NOT NULL,
                guild_name TEXT NOT NULL,
                country TEXT NOT NULL,
                streamer_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                registered_date TEXT NOT NULL,
                last_active_date TEXT NOT NULL,
                is_real_person INTEGER NOT NULL,
                is_app_fan INTEGER,
                app_fan_status TEXT NOT NULL,
                app_fan_source TEXT NOT NULL,
                source_updated_at TEXT NOT NULL,
                materialized_at TEXT NOT NULL,
                raw_streamer_id TEXT NOT NULL,
                is_direct_canonical INTEGER NOT NULL,
                PRIMARY KEY (app_name, streamer_id)
            ) WITHOUT ROWID
            """
        )
        conn.execute(
            """
            INSERT INTO _source_linky_profile (
                app_name, guild_executor_key, guild_name, country, streamer_id,
                display_name, registered_date, last_active_date, is_real_person,
                is_app_fan, app_fan_status, app_fan_source,
                source_updated_at, materialized_at, raw_streamer_id,
                is_direct_canonical
            )
            SELECT profile.app_name, profile.guild_executor_key,
                   profile.guild_name,
                   COALESCE(NULLIF(executor.country, ''), profile.country, '') AS country,
                   profile.streamer_id,
                   profile.display_name, profile.registered_date,
                   profile.last_active_date, profile.is_real_person,
                   CASE WHEN fan.streamer_id IS NOT NULL THEN 1
                        WHEN profile.registered_date >= '2026-05-17' THEN 0
                        ELSE NULL END,
                   CASE WHEN fan.streamer_id IS NOT NULL THEN 'app_fan'
                        WHEN profile.registered_date >= '2026-05-17' THEN 'non_app_fan'
                        ELSE 'historical_unknown' END,
                   CASE WHEN fan.streamer_id IS NOT NULL THEN 'tugao_app' ELSE '' END,
                   profile.source_updated_at, ?, profile.streamer_id, 1
            FROM streamer_analytics_profiles_v5 AS profile
            LEFT JOIN _source_linky_enabled_guild AS executor
              ON executor.guild_name = profile.guild_name
            LEFT JOIN streamer_app_fan_identities AS fan
              ON fan.app_name = 'linky'
             AND fan.streamer_id = profile.streamer_id
             AND fan.is_app_fan = 1
            WHERE profile.app_name = 'linky'
            """,
            (materialized_at,),
        )
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.source_profile.done')
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.source_newcomer_identity.start',
        )
        conn.execute(
            """
            CREATE TEMP TABLE _source_linky_snapshot_day AS
            SELECT guild_executor_key, stat_date
            FROM guild_anchor_newcomer_snapshot_runs
            UNION
            SELECT guild_executor_key, stat_date
            FROM guild_anchor_newcomer_identity_snapshots
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX temp._idx_source_linky_snapshot_day
            ON _source_linky_snapshot_day(guild_executor_key, stat_date)
            """
        )
        conn.execute(
            """
            CREATE TEMP TABLE _source_linky_newcomer_identity AS
            WITH resolved_snapshot_members AS (
                SELECT 'linky' AS app_name,
                       snapshot.guild_executor_key,
                       snapshot.guild_name,
                       COALESCE(NULLIF(executor.country, ''), '') AS country,
                       NULLIF(snapshot.streamer_sid, '') AS streamer_id,
                       snapshot.anchor_id AS identity_key,
                       snapshot.stat_date AS registered_date,
                       snapshot.snapshot_refreshed_at AS source_updated_at,
                       ? AS materialized_at
                FROM guild_anchor_newcomer_identity_snapshots AS snapshot
                LEFT JOIN guild_executors AS executor
                  ON executor.guild_name = snapshot.guild_name
                 AND lower(COALESCE(executor.app_name, '')) = 'linky'
            ), snapshot_members AS (
                SELECT app_name, guild_executor_key, guild_name, country,
                       streamer_id, identity_key, registered_date, source_updated_at,
                       materialized_at
                FROM resolved_snapshot_members
                WHERE NULLIF(streamer_id, '') IS NOT NULL
                  AND NULLIF(identity_key, '') IS NOT NULL
                  AND NULLIF(guild_executor_key, '') IS NOT NULL
                  AND NULLIF(guild_name, '') IS NOT NULL
            ), legacy_members AS (
                SELECT profile.app_name, profile.guild_executor_key,
                       profile.guild_name, profile.country, profile.streamer_id,
                       profile.streamer_id AS identity_key,
                       profile.registered_date, profile.source_updated_at,
                       profile.materialized_at
                FROM _source_linky_profile AS profile
                WHERE length(COALESCE(profile.registered_date, '')) >= 10
                  AND NOT EXISTS (
                      SELECT 1 FROM _source_linky_snapshot_day AS snapshot_day
                      WHERE snapshot_day.guild_executor_key = profile.guild_executor_key
                        AND snapshot_day.stat_date = substr(profile.registered_date, 1, 10)
                  )
            )
            SELECT * FROM snapshot_members
            UNION ALL
            SELECT * FROM legacy_members
            """,
            (materialized_at,),
        )
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.source_newcomer_identity.done',
        )
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.source_daily.start')
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.source_daily.schema.start')
        conn.execute(
            """
            CREATE TEMP TABLE _source_linky_daily (
                app_name TEXT NOT NULL,
                guild_executor_key TEXT NOT NULL,
                guild_name TEXT NOT NULL,
                country TEXT NOT NULL,
                stat_date TEXT NOT NULL,
                streamer_id TEXT NOT NULL,
                total_income REAL NOT NULL,
                is_new INTEGER NOT NULL,
                is_active INTEGER NOT NULL,
                materialized_at TEXT NOT NULL,
                source_updated_at TEXT NOT NULL,
                has_revenue INTEGER NOT NULL,
                PRIMARY KEY (app_name, guild_executor_key, stat_date, streamer_id)
            ) WITHOUT ROWID
            """
        )
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.source_daily.schema.done')
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.source_daily.revenue_upsert.start',
        )
        conn.execute(
            """
            INSERT INTO _source_linky_daily (
                app_name, guild_executor_key, guild_name, country, stat_date,
                streamer_id, total_income, is_new, is_active, materialized_at,
                source_updated_at, has_revenue
            )
            SELECT revenue.app_name,
                   revenue.guild_executor_key,
                   revenue.guild_name,
                   COALESCE(
                       NULLIF(profile_executor.country, ''),
                       NULLIF(revenue_executor.country, ''),
                       NULLIF(profile.country, ''),
                       ''
                   ) AS country,
                   substr(revenue.stat_date_bj, 1, 10) AS stat_date,
                   profile.platform_character_id AS streamer_id,
                   CASE
                       WHEN lower(trim(COALESCE(
                           NULLIF(profile_executor.country, ''),
                           NULLIF(revenue_executor.country, ''),
                           NULLIF(profile.country, ''),
                           ''
                       ))) = 'indonesia'
                       THEN revenue.total_income
                       ELSE revenue.chat_income
                   END AS total_income,
                   CASE WHEN substr(profile.registered_at_bj, 1, 10)
                                  = revenue.stat_date_bj
                        THEN 1 ELSE 0 END AS is_new,
                   CASE
                       WHEN (
                           CASE
                               WHEN lower(trim(COALESCE(
                                   NULLIF(profile_executor.country, ''),
                                   NULLIF(revenue_executor.country, ''),
                                   NULLIF(profile.country, ''),
                                   ''
                               ))) = 'indonesia'
                               THEN revenue.total_income
                               ELSE revenue.chat_income
                           END
                       ) > 0 THEN 1 ELSE 0
                   END AS is_active,
                   ? AS materialized_at,
                   revenue.updated_at AS source_updated_at,
                   1 AS has_revenue
            FROM streamer_external_revenue_daily AS revenue
            JOIN streamer_external_profiles AS profile
              ON profile.app_name = revenue.app_name
             AND profile.guild_executor_key = revenue.guild_executor_key
             AND profile.streamer_id = revenue.streamer_id
             AND NULLIF(profile.platform_character_id, '') IS NOT NULL
            LEFT JOIN guild_executors AS profile_executor
              ON profile_executor.guild_name = profile.guild_name
             AND lower(profile_executor.app_name) = 'linky'
            LEFT JOIN guild_executors AS revenue_executor
              ON revenue_executor.guild_name = revenue.guild_name
             AND lower(revenue_executor.app_name) = 'linky'
            WHERE revenue.app_name = 'linky'
              AND length(COALESCE(revenue.stat_date_bj, '')) >= 10
            ON CONFLICT (app_name, guild_executor_key, stat_date, streamer_id)
            DO UPDATE SET
                guild_name = CASE
                    WHEN excluded.total_income > _source_linky_daily.total_income
                      OR (excluded.total_income = _source_linky_daily.total_income
                          AND excluded.source_updated_at > _source_linky_daily.source_updated_at)
                    THEN excluded.guild_name ELSE _source_linky_daily.guild_name END,
                country = CASE
                    WHEN excluded.total_income > _source_linky_daily.total_income
                      OR (excluded.total_income = _source_linky_daily.total_income
                          AND excluded.source_updated_at > _source_linky_daily.source_updated_at)
                    THEN excluded.country ELSE _source_linky_daily.country END,
                total_income = MAX(
                    _source_linky_daily.total_income,
                    excluded.total_income
                ),
                is_new = MAX(_source_linky_daily.is_new, excluded.is_new),
                is_active = CASE
                    WHEN MAX(_source_linky_daily.total_income, excluded.total_income) > 0
                    THEN 1 ELSE 0 END,
                materialized_at = excluded.materialized_at,
                source_updated_at = CASE
                    WHEN excluded.total_income > _source_linky_daily.total_income
                      OR (excluded.total_income = _source_linky_daily.total_income
                          AND excluded.source_updated_at > _source_linky_daily.source_updated_at)
                    THEN excluded.source_updated_at
                    ELSE _source_linky_daily.source_updated_at END,
                has_revenue = 1
            """,
            (materialized_at,),
        )
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.source_daily.revenue_upsert.done',
        )
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.source_daily.registration_upsert.start',
        )
        conn.execute(
            """
            INSERT INTO _source_linky_daily (
                app_name, guild_executor_key, guild_name, country, stat_date,
                streamer_id, total_income, is_new, is_active, materialized_at,
                source_updated_at, has_revenue
            )
            SELECT profile.app_name,
                   profile.guild_executor_key,
                   profile.guild_name,
                   COALESCE(
                       NULLIF(executor.country, ''),
                       NULLIF(profile.country, ''),
                       ''
                   ) AS country,
                   substr(profile.registered_at_bj, 1, 10) AS stat_date,
                   profile.platform_character_id AS streamer_id,
                   0 AS total_income,
                   1 AS is_new,
                   0 AS is_active,
                   ? AS materialized_at,
                   profile.updated_at AS source_updated_at,
                   0 AS has_revenue
            FROM streamer_external_profiles AS profile
            LEFT JOIN guild_executors AS executor
              ON executor.guild_name = profile.guild_name
             AND lower(executor.app_name) = 'linky'
            WHERE profile.app_name = 'linky'
              AND NULLIF(profile.platform_character_id, '') IS NOT NULL
              AND length(COALESCE(profile.registered_at_bj, '')) >= 10
            ON CONFLICT (app_name, guild_executor_key, stat_date, streamer_id)
            DO UPDATE SET
                guild_name = CASE
                    WHEN _source_linky_daily.has_revenue = 0
                     AND excluded.source_updated_at > _source_linky_daily.source_updated_at
                    THEN excluded.guild_name ELSE _source_linky_daily.guild_name END,
                country = CASE
                    WHEN _source_linky_daily.has_revenue = 0
                     AND excluded.source_updated_at > _source_linky_daily.source_updated_at
                    THEN excluded.country ELSE _source_linky_daily.country END,
                is_new = 1,
                materialized_at = excluded.materialized_at,
                source_updated_at = CASE
                    WHEN _source_linky_daily.has_revenue = 0
                     AND excluded.source_updated_at > _source_linky_daily.source_updated_at
                    THEN excluded.source_updated_at
                    ELSE _source_linky_daily.source_updated_at END
            """,
            (materialized_at,),
        )
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.source_daily.registration_upsert.done',
        )
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.source_daily.done')
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.source_official_daily.start')
        conn.execute(
            """
            CREATE TEMP TABLE _source_linky_official_daily AS
            SELECT official.guild_executor_key,
                   MAX(COALESCE(official.guild_name, '')) AS guild_name,
                   COALESCE(
                       NULLIF(MAX(executor.country), ''),
                       NULLIF(MAX(official.country), ''),
                       ''
                   ) AS country,
                   official.stat_date_bj AS stat_date,
                   SUM(CASE
                       WHEN lower(trim(COALESCE(
                           NULLIF(executor.country, ''),
                           NULLIF(official.country, ''),
                           ''
                       ))) = 'indonesia'
                       THEN COALESCE(official.total_income, 0)
                       ELSE COALESCE(official.chat_income, 0)
                   END) AS total_income
            FROM streamer_external_guild_revenue_daily AS official
            LEFT JOIN guild_executors AS executor
              ON executor.guild_name = official.guild_name
             AND lower(COALESCE(executor.app_name, '')) = 'linky'
             AND executor.enabled = 1
            WHERE official.app_name = 'linky'
            GROUP BY official.guild_executor_key, official.stat_date_bj
            """
        )
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.source_official_daily.done')
        country_mismatch = conn.execute(
            """
            WITH fact_country AS (
                SELECT guild_name, country FROM _source_linky_profile
                UNION
                SELECT guild_name, country FROM _source_linky_daily
                UNION
                SELECT guild_name, country FROM _source_linky_official_daily
            )
            SELECT fact.guild_name, fact.country, enabled.country
            FROM fact_country AS fact
            JOIN _source_linky_enabled_guild AS enabled
              ON enabled.guild_name = fact.guild_name
            WHERE NULLIF(trim(enabled.country), '') IS NOT NULL
              AND lower(trim(fact.country)) <> lower(trim(enabled.country))
            LIMIT 1
            """
        ).fetchone()
        if country_mismatch is not None:
            raise RuntimeError(
                'linky_guild_country_dimension_mismatch:'
                f'{country_mismatch[0]}:{country_mismatch[1]}:{country_mismatch[2]}'
            )
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.source_observed_date.start')
        conn.execute(
            """
            CREATE TEMP TABLE _source_linky_observed_date AS
            SELECT DISTINCT guild_executor_key, stat_date_bj AS stat_date
            FROM streamer_external_guild_revenue_daily
            WHERE app_name = 'linky'
              AND ABS(COALESCE(reconciliation_delta, 0)) <= 0.000001
            """
        )
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.source_observed_date.done')
        # The rest of the build reads only connection-local temp tables. Release
        # the main database snapshot before indexes, aggregates, staging and cohorts.
        conn.commit()
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.source_snapshot.done')
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.temp_indexes.start')
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.temp_index.profile_cohort.start',
        )
        conn.execute(
            'CREATE INDEX temp._idx_source_linky_profile_cohort '
            'ON _source_linky_profile(guild_name, country, registered_date)'
        )
        conn.execute(
            'CREATE INDEX temp._idx_source_linky_newcomer_identity_cohort '
            'ON _source_linky_newcomer_identity('
            'guild_name, country, registered_date)'
        )
        conn.execute(
            'CREATE UNIQUE INDEX temp._idx_source_linky_canonical_identity '
            'ON _source_linky_newcomer_identity('
            'guild_executor_key, streamer_id, identity_key, registered_date)'
        )
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.temp_index.profile_cohort.done',
        )
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.temp_index.daily_member.start',
        )
        conn.execute(
            'CREATE INDEX temp._idx_source_linky_daily_member '
            'ON _source_linky_daily(guild_executor_key, streamer_id, stat_date)'
        )
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.temp_index.daily_member.done',
        )
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.temp_index.official_daily.start',
        )
        conn.execute(
            'CREATE UNIQUE INDEX temp._idx_source_linky_official_daily '
            'ON _source_linky_official_daily(guild_executor_key, stat_date)'
        )
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.temp_index.official_daily.done',
        )
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.temp_index.official_cohort.start',
        )
        conn.execute(
            'CREATE INDEX temp._idx_source_linky_official_cohort '
            'ON _source_linky_official_daily(guild_name, country, stat_date)'
        )
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.temp_index.official_cohort.done',
        )
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.temp_index.observed_date.start',
        )
        conn.execute(
            'CREATE UNIQUE INDEX temp._idx_source_linky_observed_date '
            'ON _source_linky_observed_date(guild_executor_key, stat_date)'
        )
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.temp_index.observed_date.done',
        )
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.temp_indexes.done')
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.daily_summary.start')
        conn.execute(
            """
            CREATE TEMP TABLE _source_linky_daily_summary AS
            SELECT d.app_name, d.guild_executor_key,
                   MAX(d.guild_name) AS guild_name,
                   MAX(d.country) AS country,
                   d.stat_date,
                   SUM(d.is_new) AS new_streamers,
                   SUM(d.is_active) AS active_streamers,
                   COALESCE(o.total_income, SUM(d.total_income)) AS total_income,
                   SUM(d.total_income) AS streamer_detail_income,
                   MAX(d.materialized_at) AS materialized_at
            FROM _source_linky_daily AS d
            LEFT JOIN _source_linky_official_daily AS o
              ON o.guild_executor_key = d.guild_executor_key
             AND o.stat_date = d.stat_date
            GROUP BY d.app_name, d.guild_executor_key, d.stat_date
            """
        )
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.daily_summary.done')
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.newcomer.start')
        conn.execute(
            """
            CREATE TEMP TABLE _source_linky_newcomer AS
            WITH maturity AS (
                SELECT p.*, c.data_as_of,
                    CASE WHEN c.data_as_of <> ''
                              AND date(p.registered_date) <= date(c.data_as_of)
                              AND (SELECT COUNT(*) FROM _source_linky_observed_date o
                                   WHERE o.guild_executor_key = p.guild_executor_key
                                     AND o.stat_date = date(p.registered_date)) = 1
                         THEN 1 ELSE 0 END AS mature_income_d1,
                    CASE WHEN c.data_as_of <> ''
                              AND date(p.registered_date, '+6 days') <= date(c.data_as_of)
                              AND (SELECT COUNT(*) FROM _source_linky_observed_date o
                                   WHERE o.guild_executor_key = p.guild_executor_key
                                     AND o.stat_date BETWEEN date(p.registered_date)
                                                         AND date(p.registered_date, '+6 days')) = 7
                         THEN 1 ELSE 0 END AS mature_income_d7,
                    CASE WHEN c.data_as_of <> ''
                              AND date(p.registered_date, '+29 days') <= date(c.data_as_of)
                              AND (SELECT COUNT(*) FROM _source_linky_observed_date o
                                   WHERE o.guild_executor_key = p.guild_executor_key
                                     AND o.stat_date BETWEEN date(p.registered_date)
                                                         AND date(p.registered_date, '+29 days')) = 30
                         THEN 1 ELSE 0 END AS mature_income_d30,
                    CASE WHEN c.data_as_of <> ''
                              AND date(p.registered_date, '+1 day') <= date(c.data_as_of)
                              AND EXISTS (SELECT 1 FROM _source_linky_observed_date o
                                          WHERE o.guild_executor_key = p.guild_executor_key
                                            AND o.stat_date = date(p.registered_date, '+1 day'))
                         THEN 1 ELSE 0 END AS mature_retention_d1,
                    CASE WHEN c.data_as_of <> ''
                              AND date(p.registered_date, '+7 days') <= date(c.data_as_of)
                              AND EXISTS (SELECT 1 FROM _source_linky_observed_date o
                                          WHERE o.guild_executor_key = p.guild_executor_key
                                            AND o.stat_date = date(p.registered_date, '+7 days'))
                         THEN 1 ELSE 0 END AS mature_retention_d7,
                    CASE WHEN c.data_as_of <> ''
                              AND date(p.registered_date, '+30 days') <= date(c.data_as_of)
                              AND EXISTS (SELECT 1 FROM _source_linky_observed_date o
                                          WHERE o.guild_executor_key = p.guild_executor_key
                                            AND o.stat_date = date(p.registered_date, '+30 days'))
                         THEN 1 ELSE 0 END AS mature_retention_d30
                FROM _source_linky_newcomer_identity AS p
                CROSS JOIN _source_linky_context AS c
                WHERE length(COALESCE(p.registered_date, '')) >= 10
                  AND date(p.registered_date) IS NOT NULL
            )
            SELECT m.app_name, m.guild_executor_key, m.guild_name, m.country,
                   m.streamer_id, substr(m.registered_date, 1, 10) AS registered_date,
                   m.data_as_of,
                   CASE WHEN m.mature_income_d1 = 1 THEN
                        COALESCE(SUM(CASE WHEN d.stat_date = substr(m.registered_date, 1, 10)
                                          THEN d.total_income ELSE 0 END), 0) END AS income_d1,
                   CASE WHEN m.mature_income_d7 = 1 THEN
                        COALESCE(SUM(CASE WHEN d.stat_date BETWEEN substr(m.registered_date, 1, 10)
                                                             AND date(m.registered_date, '+6 days')
                                          THEN d.total_income ELSE 0 END), 0) END AS income_d7,
                   CASE WHEN m.mature_income_d30 = 1 THEN
                        COALESCE(SUM(CASE WHEN d.stat_date BETWEEN substr(m.registered_date, 1, 10)
                                                             AND date(m.registered_date, '+29 days')
                                          THEN d.total_income ELSE 0 END), 0) END AS income_d30,
                   m.mature_income_d1, m.mature_income_d7, m.mature_income_d30,
                   m.mature_retention_d1, m.mature_retention_d7, m.mature_retention_d30,
                   CASE WHEN m.mature_retention_d1 = 1 THEN
                        MAX(CASE WHEN d.stat_date = date(m.registered_date, '+1 day')
                                      AND d.is_active = 1 THEN 1 ELSE 0 END)
                        ELSE 0 END AS retained_d1,
                   CASE WHEN m.mature_retention_d7 = 1 THEN
                        MAX(CASE WHEN d.stat_date = date(m.registered_date, '+7 days')
                                      AND d.is_active = 1 THEN 1 ELSE 0 END)
                        ELSE 0 END AS retained_d7,
                   CASE WHEN m.mature_retention_d30 = 1 THEN
                        MAX(CASE WHEN d.stat_date = date(m.registered_date, '+30 days')
                                      AND d.is_active = 1 THEN 1 ELSE 0 END)
                        ELSE 0 END AS retained_d30,
                   m.materialized_at
            FROM maturity AS m
            LEFT JOIN _source_linky_daily AS d
              ON d.guild_executor_key = m.guild_executor_key
             AND d.streamer_id = m.streamer_id
             AND d.stat_date BETWEEN substr(m.registered_date, 1, 10)
                                 AND date(m.registered_date, '+30 days')
            GROUP BY m.app_name, m.guild_executor_key, m.streamer_id
            """
        )
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.newcomer.done')

        _emit_streamer_analytics_phase(phase_logger, 'app.linky.metrics.start')
        profile_count = int(conn.execute('SELECT COUNT(*) FROM _source_linky_profile').fetchone()[0])
        streamer_daily_count = int(conn.execute('SELECT COUNT(*) FROM _source_linky_daily').fetchone()[0])
        daily_summary_count = int(conn.execute('SELECT COUNT(*) FROM _source_linky_daily_summary').fetchone()[0])
        newcomer_count = int(conn.execute('SELECT COUNT(*) FROM _source_linky_newcomer').fetchone()[0])
        expected_streamer_income = float(
            conn.execute('SELECT COALESCE(SUM(total_income), 0) FROM _source_linky_daily').fetchone()[0] or 0
        )
        expected_platform_income = float(
            conn.execute('SELECT COALESCE(SUM(total_income), 0) FROM _source_linky_daily_summary').fetchone()[0] or 0
        )
        newcomer_income_row = conn.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN mature_income_d1 = 1 THEN income_d1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN mature_income_d7 = 1 THEN income_d7 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN mature_income_d30 = 1 THEN income_d30 ELSE 0 END), 0)
            FROM _source_linky_newcomer
            """
        ).fetchone()
        expected_newcomer_income = {
            1: float(newcomer_income_row[0] or 0),
            7: float(newcomer_income_row[1] or 0),
            30: float(newcomer_income_row[2] or 0),
        }
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.metrics.done')

        _emit_streamer_analytics_phase(phase_logger, 'app.linky.cohorts.start')
        if profile_count and data_as_of_date:
            min_registered_row = conn.execute(
                "SELECT MIN(registered_date) FROM _source_linky_profile "
                "WHERE length(COALESCE(registered_date, '')) >= 10"
            ).fetchone()
            cohort_start = _iso_date(min_registered_row[0] if min_registered_row else None)
            if cohort_start:
                _emit_streamer_analytics_phase(phase_logger, 'app.linky.cohort.all.start')
                all_payload = _build_linky_weekly_cohorts_from_source_temp(
                    conn, start=cohort_start, end=data_as_of_date,
                )
                _emit_streamer_analytics_phase(phase_logger, 'app.linky.cohort.all.done')
                cohort_payloads.append((
                    'all', '', str(all_payload.get('data_as_of') or ''),
                    json.dumps(all_payload, ensure_ascii=False, separators=(',', ':')),
                    materialized_at,
                ))
                guilds = (
                    str(row[0])
                    for row in conn.execute(
                        "SELECT DISTINCT guild_name FROM _source_linky_profile "
                        "WHERE trim(COALESCE(guild_name, '')) <> '' ORDER BY guild_name"
                    )
                )
                guild_index = 0
                for guild in guilds:
                    if not _linky_guild_is_included(guild):
                        continue
                    guild_index += 1
                    _emit_streamer_analytics_phase(
                        phase_logger,
                        f'app.linky.cohort.guild.{guild_index}.start',
                    )
                    payload = _build_linky_weekly_cohorts_from_source_temp(
                        conn, start=cohort_start, end=data_as_of_date, guild_name=guild,
                    )
                    _emit_streamer_analytics_phase(
                        phase_logger,
                        f'app.linky.cohort.guild.{guild_index}.done',
                    )
                    cohort_payloads.append((
                        'guild', guild, str(payload.get('data_as_of') or ''),
                        json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
                        materialized_at,
                    ))
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.cohorts.done')
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.temp_index.daily_member_drop.start',
        )
        conn.execute('DROP INDEX temp._idx_source_linky_daily_member')
        _emit_streamer_analytics_phase(
            phase_logger, 'app.linky.temp_index.daily_member_drop.done',
        )
        stage_specs = (
            (
                'streamer_analytics_profile_summary', '_stage_streamer_analytics_profile',
                STREAMER_ANALYTICS_PROFILE_COLUMNS, '_source_linky_profile',
            ),
            (
                'streamer_analytics_streamer_daily_summary', '_stage_streamer_analytics_daily',
                STREAMER_ANALYTICS_STREAMER_DAILY_COLUMNS, '_source_linky_daily',
            ),
            (
                'streamer_analytics_daily_summary', '_stage_streamer_analytics_platform_daily',
                STREAMER_ANALYTICS_DAILY_COLUMNS, '_source_linky_daily_summary',
            ),
            (
                'streamer_analytics_newcomer_summary', '_stage_streamer_analytics_newcomer',
                STREAMER_ANALYTICS_NEWCOMER_COLUMNS, '_source_linky_newcomer',
            ),
        )
        for target_table, stage_table, columns, source_table in stage_specs:
            stage_name = source_table.removeprefix('_source_linky_')
            column_sql = ', '.join(columns)
            if offline_candidate:
                _emit_streamer_analytics_phase(
                    phase_logger,
                    f'app.linky.offline_replace.{stage_name}.start',
                )
                batch_result = _replace_offline_materialized_rows_in_batches(
                    publish_conn,
                    target_table=target_table,
                    columns=columns,
                    rows=_iter_query_tuples(
                        conn,
                        f'SELECT {column_sql} FROM {source_table}',
                    ),
                    delete_where='app_name = ?',
                    delete_params=(app,),
                    batch_logger=lambda operation, index, row_count: (
                        _emit_streamer_analytics_phase(
                            phase_logger,
                            f'app.linky.offline_replace.{stage_name}.'
                            f'{operation}.batch.{index}.rows.{row_count}',
                        )
                    ),
                )
                _emit_streamer_analytics_phase(
                    phase_logger,
                    f'app.linky.offline_replace.{stage_name}.batches.'
                    f'{batch_result["delete_batches"]}.'
                    f'{batch_result["insert_batches"]}',
                )
                _emit_streamer_analytics_phase(
                    phase_logger,
                    f'app.linky.offline_replace.{stage_name}.done',
                )
            else:
                _emit_streamer_analytics_phase(
                    phase_logger,
                    f'app.linky.stage.{stage_name}.start',
                )
                _stage_materialized_query(
                    conn,
                    publish_conn,
                    stage_table=stage_table,
                    source_table=target_table,
                    columns=columns,
                    query=f'SELECT {column_sql} FROM {source_table}',
                )
                _emit_streamer_analytics_phase(
                    phase_logger,
                    f'app.linky.stage.{stage_name}.done',
                )
            conn.execute(f'DROP TABLE temp.{source_table}')
        for temp_table in (
            '_source_linky_context',
            '_source_linky_enabled_guild',
            '_source_linky_official_daily',
            '_source_linky_observed_date',
        ):
            conn.execute(f'DROP TABLE temp.{temp_table}')
        if offline_candidate:
            _emit_streamer_analytics_phase(
                phase_logger, 'app.linky.offline_replace.cohort.start',
            )
            _replace_offline_materialized_rows_in_batches(
                publish_conn,
                target_table='streamer_analytics_linky_cohort_summary',
                columns=STREAMER_ANALYTICS_COHORT_COLUMNS,
                rows=iter(cohort_payloads),
                delete_where='1 = 1',
                batch_logger=lambda operation, index, row_count: (
                    _emit_streamer_analytics_phase(
                        phase_logger,
                        f'app.linky.offline_replace.cohort.'
                        f'{operation}.batch.{index}.rows.{row_count}',
                    )
                ),
            )
            _emit_streamer_analytics_phase(
                phase_logger, 'app.linky.offline_replace.cohort.done',
            )
        else:
            _emit_streamer_analytics_phase(phase_logger, 'app.linky.stage.cohort.start')
            _stage_materialized_rows(
                publish_conn,
                stage_table='_stage_streamer_analytics_cohort',
                source_table='streamer_analytics_linky_cohort_summary',
                columns=STREAMER_ANALYTICS_COHORT_COLUMNS,
                rows=iter(cohort_payloads),
            )
            _emit_streamer_analytics_phase(phase_logger, 'app.linky.stage.cohort.done')
        conn.commit()
        if publish_conn is not conn:
            publish_conn.commit()
    except Exception:
        conn.rollback()
        if publish_conn is not conn:
            publish_conn.rollback()
        _drop_linky_temp_tables(publish_conn, stage_temp_tables)
        raise
    finally:
        try:
            _drop_linky_temp_tables(conn, source_temp_tables)
            if source_overlay_cleanup is not None:
                source_overlay_cleanup(conn)
        except Exception:
            _drop_linky_temp_tables(publish_conn, stage_temp_tables)
            raise

    publish_tables = (
        ('streamer_analytics_profile_summary', '_stage_streamer_analytics_profile', STREAMER_ANALYTICS_PROFILE_COLUMNS),
        ('streamer_analytics_streamer_daily_summary', '_stage_streamer_analytics_daily', STREAMER_ANALYTICS_STREAMER_DAILY_COLUMNS),
        ('streamer_analytics_daily_summary', '_stage_streamer_analytics_platform_daily', STREAMER_ANALYTICS_DAILY_COLUMNS),
        ('streamer_analytics_newcomer_summary', '_stage_streamer_analytics_newcomer', STREAMER_ANALYTICS_NEWCOMER_COLUMNS),
    )
    try:
        if publish_conn is not conn:
            _emit_streamer_analytics_phase(
                phase_logger, 'app.linky.source_temp_store_reset.start',
            )
            _reset_empty_linky_temp_store(conn)
            _emit_streamer_analytics_phase(
                phase_logger, 'app.linky.source_temp_store_reset.done',
            )
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.publish.start')
        parity, publication_id = _publish_staged_streamer_analytics_app(
            publish_conn,
            app=app,
            publish_tables=() if offline_candidate else publish_tables,
            cohort_table='streamer_analytics_linky_cohort_summary',
            include_cohorts=not offline_candidate,
            data_as_of=data_as_of_date.isoformat() if data_as_of_date else '',
            profile_count=profile_count,
            streamer_daily_count=streamer_daily_count,
            daily_summary_count=daily_summary_count,
            newcomer_count=newcomer_count,
            cohort_scope_count=len(cohort_payloads),
            expected_streamer_income=expected_streamer_income,
            expected_platform_income=expected_platform_income,
            expected_newcomer_income=expected_newcomer_income,
            materialized_at=materialized_at,
        )
        _emit_streamer_analytics_phase(phase_logger, 'app.linky.publish.done')
    finally:
        _drop_linky_temp_tables(publish_conn, stage_temp_tables)
    return {
        'status': 'ready',
        'data_as_of': data_as_of_date.isoformat() if data_as_of_date else None,
        'profile_count': profile_count,
        'streamer_daily_count': streamer_daily_count,
        'daily_summary_count': daily_summary_count,
        'newcomer_count': newcomer_count,
        'cohort_scope_count': len(cohort_payloads),
        'materialized_at': materialized_at,
        'publication_id': publication_id,
        'source_processing': (
            'sqlite_streamed_offline_batched'
            if offline_candidate else 'sqlite_streamed'
        ),
        'parity': parity,
    }


def materialize_streamer_analytics_tables(
    conn: sqlite3.Connection,
    *,
    app_names: Iterable[object] = SUPPORTED_APPS,
    include_timo_cohorts: bool = True,
    target_conn: Optional[sqlite3.Connection] = None,
    analytics_db_path: object = None,
    validate_source_schema_only: bool = False,
    refresh_support_tables: bool = True,
    offline_linky_candidate_path: object = None,
    linky_source_overlay: Optional[Callable[[sqlite3.Connection], None]] = None,
    linky_source_overlay_cleanup: Optional[Callable[[sqlite3.Connection], None]] = None,
    phase_logger: Optional[Callable[[str], None]] = None,
    inherited_queue_write_locks: bool = False,
) -> Dict[str, Any]:
    apps = tuple(dict.fromkeys(normalize_streamer_app(value) for value in app_names))
    _assert_production_linky_materialization_slice(apps)
    resolved_offline_candidate: Optional[Path] = None
    if offline_linky_candidate_path is not None:
        if target_conn is None or apps != ('linky',):
            raise RuntimeError(
                'offline_linky_candidate_requires_explicit_linky_only_target'
            )
        resolved_offline_candidate = Path(offline_linky_candidate_path).resolve()
    configured_path = str(
        analytics_db_path
        or os.getenv('STREAMER_ANALYTICS_DB_PATH')
        or ''
    ).strip()
    if target_conn is not None:
        original_synchronous: Optional[int] = None
        if resolved_offline_candidate is not None:
            original_synchronous = _begin_offline_linky_candidate_refresh(
                target_conn,
                resolved_offline_candidate,
            )
        try:
            return _materialize_streamer_analytics_tables(
                conn,
                publish_conn=target_conn,
                app_names=apps,
                include_timo_cohorts=include_timo_cohorts,
                validate_source_schema_only=validate_source_schema_only,
                refresh_support_tables=refresh_support_tables,
                offline_linky_candidate_path=resolved_offline_candidate,
                linky_source_overlay=linky_source_overlay,
                linky_source_overlay_cleanup=linky_source_overlay_cleanup,
                phase_logger=phase_logger,
            )
        finally:
            if original_synchronous is not None:
                if target_conn.in_transaction:
                    target_conn.rollback()
                target_conn.execute(
                    f'PRAGMA main.synchronous={original_synchronous}'
                )
    if configured_path:
        connector = (
            _connect_streamer_analytics_store_with_inherited_queue_locks
            if inherited_queue_write_locks
            else _connect_streamer_analytics_store
        )
        with connector(Path(configured_path)) as store_conn:
            return _materialize_streamer_analytics_tables(
                conn,
                publish_conn=store_conn,
                app_names=apps,
                include_timo_cohorts=include_timo_cohorts,
                validate_source_schema_only=validate_source_schema_only,
                refresh_support_tables=refresh_support_tables,
                linky_source_overlay=linky_source_overlay,
                linky_source_overlay_cleanup=linky_source_overlay_cleanup,
                phase_logger=phase_logger,
            )
    return _materialize_streamer_analytics_tables(
        conn,
        publish_conn=conn,
        app_names=apps,
        include_timo_cohorts=include_timo_cohorts,
        validate_source_schema_only=validate_source_schema_only,
        refresh_support_tables=refresh_support_tables,
        linky_source_overlay=linky_source_overlay,
        linky_source_overlay_cleanup=linky_source_overlay_cleanup,
        phase_logger=phase_logger,
    )


def _assert_production_linky_materialization_slice(
    app_names: Iterable[str],
    *,
    project_root: Optional[Path] = None,
    cgroup_text: Optional[str] = None,
) -> None:
    if 'linky' not in app_names:
        return
    resolved_root = (project_root or Path(__file__).resolve().parents[1]).resolve()
    if resolved_root != PRODUCTION_PROJECT_ROOT:
        return
    if os.getenv('MCN_BATCH_LAUNCHER_ACTIVE') != '1':
        raise RuntimeError('linky_materialization_requires_mcn_batch_launcher')
    if cgroup_text is None:
        try:
            cgroup_text = Path('/proc/self/cgroup').read_text(encoding='utf-8')
        except OSError as exc:
            raise RuntimeError(
                'linky_materialization_cgroup_unreadable'
            ) from exc
    expected = f'/{LINKY_PRODUCTION_BATCH_SLICE}/'
    if expected not in cgroup_text:
        raise RuntimeError(
            'linky_materialization_requires_mcn_batch_linky_slice'
        )


def _materialize_streamer_analytics_tables(
    conn: sqlite3.Connection,
    *,
    publish_conn: sqlite3.Connection,
    app_names: Iterable[object] = SUPPORTED_APPS,
    include_timo_cohorts: bool = True,
    validate_source_schema_only: bool = False,
    refresh_support_tables: bool = True,
    offline_linky_candidate_path: Optional[Path] = None,
    linky_source_overlay: Optional[Callable[[sqlite3.Connection], None]] = None,
    linky_source_overlay_cleanup: Optional[Callable[[sqlite3.Connection], None]] = None,
    phase_logger: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Rebuild the dashboard read models after raw feed synchronization.

    Raw profile and revenue tables remain the source of truth. Each app is
    refreshed in one transaction so readers either see the previous complete
    snapshot or the new complete snapshot, never a half-built dashboard.
    """
    conn.row_factory = sqlite3.Row
    publish_conn.row_factory = sqlite3.Row
    apps = tuple(dict.fromkeys(normalize_streamer_app(value) for value in app_names))
    source_schema_phase = (
        'source_schema.validate' if validate_source_schema_only else 'source_schema.ensure'
    )
    _emit_streamer_analytics_phase(phase_logger, f'{source_schema_phase}.start')
    app_fan_reconcile: Dict[str, Any] = {}
    if validate_source_schema_only:
        _validate_streamer_analytics_source_schema(conn)
    else:
        ensure_streamer_analytics_views(conn)
        app_fan_reconcile = reconcile_streamer_app_fans(conn, app_names=apps)
        conn.commit()
    _emit_streamer_analytics_phase(phase_logger, f'{source_schema_phase}.done')
    _emit_streamer_analytics_phase(phase_logger, 'analytics_store_schema.start')
    _ensure_streamer_analytics_store_schema(conn, publish_conn)
    _emit_streamer_analytics_phase(phase_logger, 'analytics_store_schema.done')
    if linky_source_overlay is not None:
        linky_source_overlay(conn)
    if refresh_support_tables:
        _emit_streamer_analytics_phase(phase_logger, 'analytics_support_refresh.start')
        _refresh_streamer_analytics_support_tables(conn, publish_conn)
        _emit_streamer_analytics_phase(phase_logger, 'analytics_support_refresh.done')
    else:
        _emit_streamer_analytics_phase(phase_logger, 'analytics_support_refresh.skipped')
    results: Dict[str, Any] = {}
    for app in apps:
        _emit_streamer_analytics_phase(phase_logger, f'app.{app}.start')
        if app == 'linky':
            results[app] = _materialize_linky_streamed(
                conn,
                publish_conn=publish_conn,
                offline_candidate_path=offline_linky_candidate_path,
                source_overlay=linky_source_overlay,
                source_overlay_cleanup=linky_source_overlay_cleanup,
                phase_logger=phase_logger,
            )
            _emit_streamer_analytics_phase(phase_logger, f'app.{app}.done')
            continue
        materialized_at = datetime.now().astimezone().isoformat(timespec='seconds')
        # Pin every source SELECT for this app to one SQLite read snapshot.
        conn.execute('BEGIN')
        profiles = (
            _timo_materialization_profiles(conn)
            if app == 'timo'
            else _rows(
                conn,
                f"SELECT * FROM {PROFILE_VIEW} WHERE app_name = ?",
                (app,),
            )
        )
        app_fan_ids = confirmed_app_fan_ids(
            conn,
            app_name=app,
            ensure_schema=False,
        )
        app_fan_complete = app_fan_history_complete(
            conn,
            app_name=app,
            ensure_schema=False,
        )
        for profile in profiles:
            profile['app_fan_status'] = classify_app_fan_status(
                app_name=app,
                streamer_id=str(profile.get('streamer_id') or ''),
                registered_date=str(profile.get('registered_date') or ''),
                confirmed_ids=app_fan_ids,
                history_complete=app_fan_complete,
            )
            profile['is_app_fan'] = (
                1 if profile['app_fan_status'] == 'app_fan'
                else 0 if profile['app_fan_status'] == 'non_app_fan'
                else None
            )
            profile['app_fan_source'] = (
                'tugao_app' if profile['app_fan_status'] == 'app_fan' else ''
            )
        timo_covered_scopes: set[tuple[str, date]] = set()
        if app == 'timo':
            raw_facts, timo_covered_scopes = _timo_materialization_scope_snapshot(conn)
        else:
            raw_facts = _rows(
                conn,
                f"""
                SELECT app_name, guild_executor_key, guild_name, country, streamer_id,
                       stat_date, is_new, is_active, total_income
                FROM {DAILY_FACT_VIEW}
                WHERE app_name = ?
                """,
                (app,),
            )
        profile_by_key = {
            (str(row['guild_executor_key']), str(row['streamer_id'])): row
            for row in profiles
        }
        daily_by_key: Dict[tuple[str, str, str], Dict[str, Any]] = {}
        for row in raw_facts:
            stat_date = str(row.get('stat_date') or '')[:10]
            if not stat_date:
                continue
            guild_key = str(row.get('guild_executor_key') or '')
            streamer_id = str(row.get('streamer_id') or '')
            key = (guild_key, stat_date, streamer_id)
            current = daily_by_key.setdefault(key, {
                'guild_executor_key': guild_key,
                'guild_name': str(row.get('guild_name') or ''),
                'country': str(row.get('country') or ''),
                'stat_date': stat_date,
                'streamer_id': streamer_id,
                'total_income': 0.0,
                'is_new': 0,
                'is_active': 0,
            })
            current['total_income'] += float(row.get('total_income') or 0)
            current['is_new'] = max(current['is_new'], int(row.get('is_new') or 0))
            current['is_active'] = max(current['is_active'], int(row.get('is_active') or 0))
        data_as_of_date = (
            max((scope_date for _guild_key, scope_date in timo_covered_scopes), default=None)
            if app == 'timo' and timo_covered_scopes else
            _revenue_data_as_of(conn, app=app)
        )
        data_as_of = data_as_of_date.isoformat() if data_as_of_date else ''

        linky_observed_dates_by_guild: Dict[str, set[str]] = defaultdict(set)
        if app == 'linky':
            for row in conn.execute(
                "SELECT guild_executor_key, stat_date_bj, reconciliation_delta "
                "FROM streamer_external_guild_revenue_daily WHERE app_name = 'linky'"
            ).fetchall():
                # A date with an unexplained guild/detail delta is not safe for
                # per-streamer newcomer revenue, even when the official guild
                # total itself is available.
                if abs(float(row[2] or 0)) <= 0.000001:
                    linky_observed_dates_by_guild[str(row[0])].add(str(row[1]))

        official_daily: Dict[tuple[str, str], float] = {}
        official_daily_rows: List[Dict[str, Any]] = []
        if app == 'linky':
            official_daily_rows = _rows(
                conn,
                """
                SELECT guild_executor_key, guild_name, country, stat_date_bj,
                       SUM(total_income) AS total_income
                FROM streamer_external_guild_revenue_daily
                WHERE app_name = 'linky'
                GROUP BY guild_executor_key, guild_name, country, stat_date_bj
                """,
            )
            for row in official_daily_rows:
                official_daily[(str(row['guild_executor_key']), str(row['stat_date_bj']))] = float(row['total_income'] or 0)

        daily_summary: Dict[tuple[str, str], Dict[str, Any]] = {}
        facts_by_streamer: Dict[tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        for row in daily_by_key.values():
            summary_key = (row['guild_executor_key'], row['stat_date'])
            summary = daily_summary.setdefault(summary_key, {
                'guild_executor_key': row['guild_executor_key'],
                'guild_name': row['guild_name'],
                'country': row['country'],
                'stat_date': row['stat_date'],
                'new_streamers': 0,
                'active_streamers': 0,
                'streamer_detail_income': 0.0,
            })
            summary['new_streamers'] += row['is_new']
            summary['active_streamers'] += row['is_active']
            summary['streamer_detail_income'] += row['total_income']
            facts_by_streamer[(row['guild_executor_key'], row['streamer_id'])].append(row)

        newcomer_rows: List[tuple[object, ...]] = []
        for profile in profiles:
            registered = _iso_date(profile.get('registered_date'))
            if not registered:
                continue
            key = (str(profile['guild_executor_key']), str(profile['streamer_id']))
            member_facts = facts_by_streamer.get(key, [])
            values: Dict[str, object] = {}
            for days in (1, 7, 30):
                retention_offset = RETENTION_DAY_OFFSETS[days]
                income_mature = bool(data_as_of_date and registered + timedelta(days=days - 1) <= data_as_of_date)
                retention_mature = bool(data_as_of_date and registered + timedelta(days=retention_offset) <= data_as_of_date)
                if app == 'linky':
                    observed_dates = linky_observed_dates_by_guild.get(str(profile['guild_executor_key']), set())
                    income_mature = income_mature and _date_window_is_covered(
                        observed_dates, registered, registered + timedelta(days=days - 1),
                    )
                    retention_mature = retention_mature and (
                        registered + timedelta(days=retention_offset)
                    ).isoformat() in observed_dates
                income = sum(
                    float(fact['total_income'] or 0)
                    for fact in member_facts
                    if 0 <= ((_iso_date(fact['stat_date']) or registered) - registered).days < days
                ) if income_mature else None
                retained = int(any(
                    fact['is_active'] == 1
                    and ((_iso_date(fact['stat_date']) or registered) - registered).days == retention_offset
                    for fact in member_facts
                )) if retention_mature else 0
                values[f'income_d{days}'] = income
                values[f'mature_income_d{days}'] = int(income_mature)
                values[f'mature_retention_d{days}'] = int(retention_mature)
                values[f'retained_d{days}'] = retained
            newcomer_rows.append((
                app, profile['guild_executor_key'], profile['guild_name'], profile['country'],
                profile['streamer_id'], registered.isoformat(), data_as_of,
                values['income_d1'], values['income_d7'], values['income_d30'],
                values['mature_income_d1'], values['mature_income_d7'], values['mature_income_d30'],
                values['mature_retention_d1'], values['mature_retention_d7'], values['mature_retention_d30'],
                values['retained_d1'], values['retained_d7'], values['retained_d30'], materialized_at,
            ))

        cohort_payloads: List[tuple[str, str, str, str, str]] = []
        should_materialize_cohorts = (
            (app == 'timo' and include_timo_cohorts)
            or app == 'linky'
        )
        if should_materialize_cohorts and profiles and data_as_of_date:
            registered_dates = [
                parsed for parsed in (_iso_date(row.get('registered_date')) for row in profiles) if parsed
            ]
            if registered_dates:
                cohort_start = _week_start(min(registered_dates))
                cohort_builder = (
                    _build_timo_weekly_cohorts_live
                    if app == 'timo' else _build_linky_weekly_cohorts_live
                )
                if app == 'linky':
                    builder_kwargs = {
                        '_profiles': profiles,
                        '_facts': list(daily_by_key.values()),
                        '_platform_facts': official_daily_rows,
                    }
                else:
                    builder_kwargs = {
                        '_profiles': profiles,
                        '_facts': list(daily_by_key.values()),
                        '_covered_scopes': timo_covered_scopes,
                        '_data_as_of': data_as_of_date,
                    }
                all_payload = cohort_builder(
                    conn, start=cohort_start, end=data_as_of_date, **builder_kwargs,
                )
                cohort_payloads.append((
                    'all', '', str(all_payload.get('data_as_of') or ''),
                    json.dumps(all_payload, ensure_ascii=False, separators=(',', ':')), materialized_at,
                ))
                for guild in sorted({str(row.get('guild_name') or '') for row in profiles if str(row.get('guild_name') or '')}):
                    if not _linky_guild_is_included(guild) and app == 'linky':
                        continue
                    payload = cohort_builder(
                        conn, start=cohort_start, end=data_as_of_date,
                        guild_name=guild, **builder_kwargs,
                    )
                    cohort_payloads.append((
                        'guild', guild, str(payload.get('data_as_of') or ''),
                        json.dumps(payload, ensure_ascii=False, separators=(',', ':')), materialized_at,
                    ))

        conn.commit()
        expected_streamer_income = sum(float(row['total_income'] or 0) for row in daily_by_key.values())
        expected_platform_income = sum(
            official_daily.get((row['guild_executor_key'], row['stat_date']), row['streamer_detail_income'])
            for row in daily_summary.values()
        )
        expected_newcomer_income = {
            days: sum(
                float(row[{1: 7, 7: 8, 30: 9}[days]] or 0)
                for row in newcomer_rows
                if int(row[{1: 10, 7: 11, 30: 12}[days]] or 0) == 1
            )
            for days in (1, 7, 30)
        }

        profile_columns = STREAMER_ANALYTICS_PROFILE_COLUMNS
        streamer_daily_columns = STREAMER_ANALYTICS_STREAMER_DAILY_COLUMNS
        daily_columns = STREAMER_ANALYTICS_DAILY_COLUMNS
        newcomer_columns = STREAMER_ANALYTICS_NEWCOMER_COLUMNS
        publish_tables = (
            (
                'streamer_analytics_profile_summary', '_stage_streamer_analytics_profile', profile_columns,
                (
                    (
                        app, row['guild_executor_key'], row['guild_name'], row['country'], row['streamer_id'],
                        row['display_name'], row['registered_date'], row['last_active_date'],
                        int(row.get('is_real_person') or 0), row.get('is_app_fan'),
                        row.get('app_fan_status') or 'historical_unknown',
                        row.get('app_fan_source') or '', row['source_updated_at'], materialized_at,
                    )
                    for row in profiles
                ),
            ),
            (
                'streamer_analytics_streamer_daily_summary', '_stage_streamer_analytics_daily', streamer_daily_columns,
                (
                    (
                        app, row['guild_executor_key'], row['guild_name'], row['country'], row['stat_date'],
                        row['streamer_id'], row['total_income'], row['is_new'], row['is_active'], materialized_at,
                    )
                    for row in daily_by_key.values()
                ),
            ),
            (
                'streamer_analytics_daily_summary', '_stage_streamer_analytics_platform_daily', daily_columns,
                (
                    (
                        app, row['guild_executor_key'], row['guild_name'], row['country'], row['stat_date'],
                        row['new_streamers'], row['active_streamers'],
                        official_daily.get((row['guild_executor_key'], row['stat_date']), row['streamer_detail_income']),
                        row['streamer_detail_income'], materialized_at,
                    )
                    for row in daily_summary.values()
                ),
            ),
            (
                'streamer_analytics_newcomer_summary', '_stage_streamer_analytics_newcomer', newcomer_columns,
                iter(newcomer_rows),
            ),
        )
        for table, stage_table, columns, rows in publish_tables:
            _stage_materialized_rows(
                publish_conn,
                stage_table=stage_table,
                source_table=table,
                columns=columns,
                rows=rows,
            )
        cohort_table = f'streamer_analytics_{app}_cohort_summary'
        cohort_columns = STREAMER_ANALYTICS_COHORT_COLUMNS
        if should_materialize_cohorts:
            _stage_materialized_rows(
                publish_conn,
                stage_table='_stage_streamer_analytics_cohort',
                source_table=cohort_table,
                columns=cohort_columns,
                rows=iter(cohort_payloads),
            )
        publish_conn.commit()

        parity, publication_id = _publish_staged_streamer_analytics_app(
            publish_conn,
            app=app,
            publish_tables=tuple(
                (table, stage_table, columns)
                for table, stage_table, columns, _rows_to_stage in publish_tables
            ),
            cohort_table=cohort_table,
            include_cohorts=should_materialize_cohorts,
            data_as_of=data_as_of,
            profile_count=len(profiles),
            streamer_daily_count=len(daily_by_key),
            daily_summary_count=len(daily_summary),
            newcomer_count=len(newcomer_rows),
            cohort_scope_count=len(cohort_payloads),
            expected_streamer_income=expected_streamer_income,
            expected_platform_income=expected_platform_income,
            expected_newcomer_income=expected_newcomer_income,
            materialized_at=materialized_at,
        )
        results[app] = {
            'status': 'ready',
            'data_as_of': data_as_of or None,
            'profile_count': len(profiles),
            'streamer_daily_count': len(daily_by_key),
            'daily_summary_count': len(daily_summary),
            'newcomer_count': len(newcomer_rows),
            'cohort_scope_count': len(cohort_payloads),
            'materialized_at': materialized_at,
            'publication_id': publication_id,
            'parity': parity,
        }
        _emit_streamer_analytics_phase(phase_logger, f'app.{app}.done')
    return {
        'ok': True,
        'apps': results,
        'app_fan_reconcile': app_fan_reconcile,
    }


def build_timo_weekly_cohorts(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    guild_name: str = '',
    country: str = '',
    allow_live_fallback: bool = True,
) -> Dict[str, Any]:
    try:
        scope_type, scope_key = ('guild', guild_name) if guild_name else ('all', '')
        cached = conn.execute(
            """
            SELECT payload_json
            FROM streamer_analytics_timo_cohort_summary
            WHERE scope_type = ? AND scope_key = ?
            """,
            (scope_type, scope_key),
        ).fetchone()
    except sqlite3.OperationalError:
        if not allow_live_fallback:
            raise
        cached = None
    if not cached:
        if allow_live_fallback:
            return _build_timo_weekly_cohorts_live(
                conn, start=start, end=end, guild_name=guild_name, country=country,
            )
        cohort_start, cohort_end = _timo_cohort_display_window(start, end)
        return {
            'available': True,
            'data_as_of': None,
            'diamonds_per_usd': int(TIMO_DIAMONDS_PER_USD),
            'week_starts_on': 'monday',
            'cohort_date_from': cohort_start.isoformat(),
            'cohort_date_to': cohort_end.isoformat(),
            'rows': [],
        }
    try:
        payload = json.loads(str(cached[0]))
    except (TypeError, ValueError) as exc:
        raise StreamerAnalyticsCohortSnapshotUnavailable(
            'streamer_analytics_timo_cohort_snapshot_unavailable'
        ) from exc
    if not isinstance(payload, dict):
        raise StreamerAnalyticsCohortSnapshotUnavailable(
            'streamer_analytics_timo_cohort_snapshot_unavailable'
        )
    payload_rows = payload.get('rows', [])
    if not isinstance(payload_rows, list) or any(
        not isinstance(row, dict) for row in payload_rows
    ):
        raise StreamerAnalyticsCohortSnapshotUnavailable(
            'streamer_analytics_timo_cohort_snapshot_unavailable'
        )
    cohort_start, cohort_end = _timo_cohort_display_window(start, end)
    rows = [
        row for row in payload_rows
        if cohort_start.isoformat() <= str(row.get('week_start') or '') <= cohort_end.isoformat()
        and (not country or str(row.get('country') or '') == country)
    ] if cohort_start <= cohort_end else []
    return {
        **payload,
        'cohort_date_from': cohort_start.isoformat(),
        'cohort_date_to': cohort_end.isoformat(),
        'rows': rows,
    }


def build_linky_weekly_cohorts(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    guild_name: str = '',
    country: str = '',
) -> Dict[str, Any]:
    try:
        scope_type, scope_key = ('guild', guild_name) if guild_name else ('all', '')
        cached = conn.execute(
            """
            SELECT payload_json
            FROM streamer_analytics_linky_cohort_summary
            WHERE scope_type = ? AND scope_key = ?
            """,
            (scope_type, scope_key),
        ).fetchone()
    except sqlite3.OperationalError:
        cached = None
    if not cached:
        return _build_linky_weekly_cohorts_live(
            conn, start=start, end=end, guild_name=guild_name, country=country,
        )
    payload = json.loads(str(cached[0]))
    cohort_start, cohort_end = _timo_cohort_display_window(start, end)
    rows = [
        row for row in payload.get('rows', [])
        if cohort_start.isoformat() <= str(row.get('week_start') or '') <= cohort_end.isoformat()
        and (not country or str(row.get('country') or '') == country)
    ] if cohort_start <= cohort_end else []
    return {
        **payload,
        'cohort_date_from': cohort_start.isoformat(),
        'cohort_date_to': cohort_end.isoformat(),
        'rows': rows,
    }


def _build_streamer_analytics_payload_materialized(
    conn: sqlite3.Connection,
    *,
    app_name: object = 'timo',
    date_from: object = None,
    date_to: object = None,
    guild_name: object = '',
    country: object = '',
    limit: int = 20,
) -> Dict[str, Any]:
    app = normalize_streamer_app(app_name)
    start, end = _date_window(date_from, date_to)
    guild = str(guild_name or '').strip()
    country_name = str(country or '').strip()
    limit = max(1, min(int(limit or 20), 100))
    scope_clause, scope_params = _materialized_scope(country_name, guild)
    if app == 'linky':
        exclusion_clause, exclusion_params = _linky_exclusion_scope()
        scope_clause = exclusion_clause + scope_clause
        scope_params = [*exclusion_params, *scope_params]
    profile_summary_rows = _rows(
        conn,
        f"""
        SELECT guild_name,
               COUNT(*) AS streamer_count,
               SUM(CASE WHEN registered_date BETWEEN ? AND ? THEN 1 ELSE 0 END) AS new_streamers
        FROM streamer_analytics_profile_summary
        WHERE app_name = ?{scope_clause}
        GROUP BY guild_name
        """,
        [start.isoformat(), end.isoformat(), app, *scope_params],
    )
    aliases = ('sugo', 'sogo') if app == 'sugo' else (app,)
    placeholders = ','.join('?' for _ in aliases)
    configured_scope = ''
    configured_params: List[object] = list(aliases)
    if app == 'linky':
        configured_scope, configured_exclusion_params = _linky_exclusion_scope()
        configured_params.extend(configured_exclusion_params)
    configured_all = _rows(
        conn,
        f"SELECT guild_name, COALESCE(country, '') AS country FROM guild_executors WHERE enabled = 1 AND lower(app_name) IN ({placeholders}){configured_scope} ORDER BY guild_name",
        configured_params,
    )
    configured = [
        row for row in configured_all
        if (not country_name or str(row.get('country') or '').strip() == country_name)
        and (not guild or str(row.get('guild_name') or '') == guild)
    ]
    countries = sorted({
        str(row.get('country') or '').strip()
        for row in [*configured_all, *_rows(
            conn,
            'SELECT DISTINCT country FROM streamer_analytics_profile_summary '
            f"WHERE app_name = ? AND trim(country) <> ''{configured_scope}",
            [app, *configured_exclusion_params] if app == 'linky' else [app],
        )]
        if str(row.get('country') or '').strip()
    })
    capabilities, revenue_available = _materialized_capabilities(conn, app)
    revenue_data_as_of = _materialized_revenue_data_as_of(
        conn,
        app=app,
        guild_name=guild,
        country=country_name,
    )
    observed_end = min(end, revenue_data_as_of) if revenue_data_as_of else end
    revenue_observed_dates = _revenue_observed_dates(
        conn,
        app=app,
        start=start,
        end=observed_end,
        guild_name=guild,
        country=country_name,
    )
    newcomer_start, newcomer_end = _newcomer_analysis_window(start, end, revenue_data_as_of)
    revenue_available = bool(revenue_observed_dates)
    _apply_revenue_freshness(capabilities, app=app, end=end, data_as_of=revenue_data_as_of)

    profile_count_by_guild: Dict[str, int] = defaultdict(int)
    new_count_by_guild: Dict[str, int] = defaultdict(int)
    for row in profile_summary_rows:
        name = str(row['guild_name'])
        profile_count_by_guild[name] = int(row.get('streamer_count') or 0)
        new_count_by_guild[name] = int(row.get('new_streamers') or 0)

    daily_rows = _rows(
        conn,
        f"""
        SELECT guild_name, stat_date,
               SUM(new_streamers) AS new_streamers,
               SUM(active_streamers) AS active_streamers,
               SUM(total_income) AS total_income
        FROM streamer_analytics_daily_summary
        WHERE app_name = ?{scope_clause} AND stat_date BETWEEN ? AND ?
        GROUP BY guild_name, stat_date
        ORDER BY stat_date
        """,
        [app, *scope_params, start.isoformat(), end.isoformat()],
    )
    guild_income: Dict[str, float] = defaultdict(float)
    trend: Dict[str, Dict[str, Any]] = defaultdict(lambda: {'new': 0, 'active': 0, 'revenue': 0.0})
    for row in daily_rows:
        guild_value = str(row['guild_name'])
        income = float(row.get('total_income') or 0)
        guild_income[guild_value] += income
        trend[str(row['stat_date'])]['new'] += int(row.get('new_streamers') or 0)
        trend[str(row['stat_date'])]['active'] += int(row.get('active_streamers') or 0)
        trend[str(row['stat_date'])]['revenue'] += income
    if app == 'linky':
        new_count_by_guild, new_count_by_date = _linky_new_streamer_counts(
            conn,
            start=start,
            end=end,
            guild_name=guild,
            country=country_name,
        )
        for values in trend.values():
            values['new'] = 0
        for stat_date, count in new_count_by_date.items():
            trend[stat_date]['new'] = count
    active_rows = _rows(
        conn,
        f"""
        SELECT guild_name, COUNT(DISTINCT guild_executor_key || char(31) || streamer_id) AS active_streamers
        FROM streamer_analytics_streamer_daily_summary
        WHERE app_name = ?{scope_clause} AND stat_date BETWEEN ? AND ? AND is_active = 1
        GROUP BY guild_name
        """,
        [app, *scope_params, start.isoformat(), end.isoformat()],
    )
    active_by_guild = {str(row['guild_name']): int(row['active_streamers'] or 0) for row in active_rows}
    active_by_date = {
        str(row['stat_date']): int(row['active_streamers'] or 0)
        for row in _rows(
            conn,
            f"""
            SELECT stat_date, COUNT(DISTINCT streamer_id) AS active_streamers
            FROM streamer_analytics_streamer_daily_summary
            WHERE app_name = ?{scope_clause} AND stat_date BETWEEN ? AND ? AND is_active = 1
            GROUP BY stat_date
            """,
            [app, *scope_params, start.isoformat(), end.isoformat()],
        )
    }
    active_total_row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT streamer_id)
        FROM streamer_analytics_streamer_daily_summary
        WHERE app_name = ?{scope_clause} AND stat_date BETWEEN ? AND ? AND is_active = 1
        """,
        [app, *scope_params, start.isoformat(), end.isoformat()],
    ).fetchone()
    active_streamer_count = int(active_total_row[0] or 0) if active_total_row else 0
    guild_names = {str(row['guild_name']) for row in configured}
    guild_names.update(profile_count_by_guild)
    guild_rows = [
        {
            'guild_name': name,
            'streamer_count': profile_count_by_guild[name],
            'new_streamers': new_count_by_guild[name],
            'active_streamers': active_by_guild.get(name, 0) if revenue_available else None,
            'total_income': round(guild_income[name], 2) if revenue_available else None,
        }
        for name in sorted(guild_names, key=lambda value: guild_income[value], reverse=True)
    ]

    ranking_scope, ranking_params = _materialized_scope(country_name, guild, alias='p')
    if app == 'linky':
        ranking_exclusion, ranking_exclusion_params = _linky_exclusion_scope(alias='p')
        ranking_scope = ranking_exclusion + ranking_scope
        ranking_params = [*ranking_exclusion_params, *ranking_params]
    ranking = _rows(
        conn,
        f"""
        SELECT p.guild_name, p.streamer_id, p.display_name, p.registered_date,
               p.is_app_fan, p.app_fan_status, p.app_fan_source,
               COALESCE(SUM(d.total_income), 0) AS total_income
        FROM streamer_analytics_profile_summary AS p
        LEFT JOIN streamer_analytics_streamer_daily_summary AS d
          ON d.app_name = p.app_name
         AND d.guild_executor_key = p.guild_executor_key
         AND d.streamer_id = p.streamer_id
         AND d.stat_date BETWEEN ? AND ?
        WHERE p.app_name = ?{ranking_scope}
        GROUP BY p.app_name, p.guild_executor_key, p.streamer_id
        ORDER BY total_income DESC, p.registered_date DESC
        LIMIT ?
        """,
        [start.isoformat(), end.isoformat(), app, *ranking_params, limit],
    )
    if not revenue_available:
        ranking = [
            {
                'guild_name': row['guild_name'], 'streamer_id': row['streamer_id'],
                'display_name': row['display_name'], 'registered_date': row['registered_date'],
                'total_income': None,
            }
            for row in ranking
            if (registered := _iso_date(row.get('registered_date'))) and start <= registered <= end
        ]
    else:
        for row in ranking:
            row['total_income'] = round(float(row.get('total_income') or 0), 2)

    app_fan_revenue_comparison = _rows(
        conn,
        f"""
        SELECT p.app_fan_status,
               COUNT(DISTINCT p.guild_executor_key || char(31) || p.streamer_id)
                   AS streamer_count,
               COUNT(DISTINCT CASE WHEN d.is_active = 1
                    THEN p.guild_executor_key || char(31) || p.streamer_id END)
                   AS active_streamers,
               COALESCE(SUM(d.total_income), 0) AS total_income
        FROM streamer_analytics_profile_summary AS p
        LEFT JOIN streamer_analytics_streamer_daily_summary AS d
          ON d.app_name = p.app_name
         AND d.guild_executor_key = p.guild_executor_key
         AND d.streamer_id = p.streamer_id
         AND d.stat_date BETWEEN ? AND ?
        WHERE p.app_name = ?{ranking_scope}
        GROUP BY p.app_fan_status
        ORDER BY CASE p.app_fan_status
            WHEN 'app_fan' THEN 1 WHEN 'non_app_fan' THEN 2 ELSE 3 END
        """,
        [start.isoformat(), end.isoformat(), app, *ranking_params],
    )
    for row in app_fan_revenue_comparison:
        row['streamer_count'] = int(row.get('streamer_count') or 0)
        row['active_streamers'] = int(row.get('active_streamers') or 0)
        row['total_income'] = round(float(row.get('total_income') or 0), 2)

    newcomer_data_as_of = revenue_data_as_of or end
    if app == 'linky' and revenue_data_as_of is not None:
        newcomer_data_as_of = _linky_newcomer_complete_data_as_of(
            conn,
            requested_data_as_of=revenue_data_as_of,
            observation_start=newcomer_start,
            guild_name=guild,
            country=country_name,
        )
        if newcomer_data_as_of < revenue_data_as_of:
            capabilities['newcomer_source_status'] = 'fallback'
            capabilities['newcomer_source_note'] = (
                f'新人指标已回退到最近连续完整日 '
                f'{newcomer_data_as_of.isoformat()}；平台总收益仍使用 '
                f'{revenue_data_as_of.isoformat()}。'
            )
    newcomer_revenue: List[Dict[str, Any]] = []
    retention: List[Dict[str, Any]] = []
    newcomer_metric_ranges: Dict[str, Dict[str, str]] = {}
    if revenue_available:
        newcomer_scope, newcomer_params = _materialized_scope(country_name, guild)
        if app == 'linky':
            newcomer_exclusion, newcomer_exclusion_params = _linky_exclusion_scope()
            newcomer_scope = newcomer_exclusion + newcomer_scope
            newcomer_params = [*newcomer_exclusion_params, *newcomer_params]
        data_as_of = newcomer_data_as_of
        for days in (1, 7, 30):
            metric_end = min(
                end,
                data_as_of - timedelta(days=days - 1),
            )
            while True:
                row = conn.execute(
                    f"""
                    SELECT COUNT(*) AS cohort_count, COALESCE(SUM(income_d{days}), 0) AS total_income
                    FROM streamer_analytics_newcomer_summary
                    WHERE app_name = ?{newcomer_scope}
                      AND registered_date BETWEEN ? AND ? AND mature_income_d{days} = 1
                    """,
                    [app, *newcomer_params, newcomer_start.isoformat(), metric_end.isoformat()],
                ).fetchone()
                newcomer_profile_count = int(row[0] or 0)
                total = float(row[1] or 0)
                if app == 'linky' and metric_end >= newcomer_start:
                    newcomer_count_by_guild, _ = _linky_new_streamer_counts(
                        conn,
                        start=newcomer_start,
                        end=metric_end,
                        guild_name=guild,
                        country=country_name,
                    )
                    official_cohort_count = sum(newcomer_count_by_guild.values())
                else:
                    official_cohort_count = newcomer_profile_count
                metric_complete = app != 'linky' or _linky_newcomer_metric_is_complete(
                    conn,
                    newcomer_start=newcomer_start,
                    newcomer_end=metric_end,
                    observation_start_offset=0,
                    observation_end_offset=days - 1,
                    profile_count=newcomer_profile_count,
                    official_count=official_cohort_count,
                    guild_name=guild,
                    country=country_name,
                ) if metric_end >= newcomer_start else False
                if app != 'linky' or metric_complete or metric_end < newcomer_start:
                    break
                metric_end -= timedelta(days=1)
            # Keep numerator and denominator on the exact same immutable
            # ledger identities. Aggregate counts only prove completeness.
            count = newcomer_profile_count
            if app == 'linky' and metric_complete:
                newcomer_data_as_of = min(
                    newcomer_data_as_of,
                    metric_end + timedelta(days=days - 1),
                )
            newcomer_metric_ranges[f'income_d{days}'] = {
                'date_from': newcomer_start.isoformat(),
                'date_to': metric_end.isoformat(),
            } if metric_end >= newcomer_start else {}
            newcomer_revenue.append({
                'days': days, 'cohort_count': count,
                'total_income': round(total, 2) if metric_complete else None,
                'avg_income': round(total / count, 2) if count and metric_complete else None,
            })
            retention_end = min(
                end,
                data_as_of - timedelta(days=RETENTION_DAY_OFFSETS[days]),
                newcomer_end if days == 30 else end,
            )
            day_offset = RETENTION_DAY_OFFSETS[days]
            while True:
                retained_row = conn.execute(
                    f"""
                    SELECT COUNT(*) AS mature_count, COALESCE(SUM(retained_d{days}), 0) AS retained
                    FROM streamer_analytics_newcomer_summary
                    WHERE app_name = ?{newcomer_scope}
                      AND registered_date BETWEEN ? AND ? AND mature_retention_d{days} = 1
                    """,
                    [
                        app,
                        *newcomer_params,
                        newcomer_start.isoformat(),
                        retention_end.isoformat(),
                    ],
                ).fetchone()
                newcomer_profile_count = int(retained_row[0] or 0)
                if app == 'linky' and retention_end >= newcomer_start:
                    newcomer_count_by_guild, _ = _linky_new_streamer_counts(
                        conn,
                        start=newcomer_start,
                        end=retention_end,
                        guild_name=guild,
                        country=country_name,
                    )
                    official_cohort_count = sum(newcomer_count_by_guild.values())
                else:
                    official_cohort_count = newcomer_profile_count
                metric_complete = app != 'linky' or _linky_newcomer_metric_is_complete(
                    conn,
                    newcomer_start=newcomer_start,
                    newcomer_end=retention_end,
                    observation_start_offset=day_offset,
                    observation_end_offset=day_offset,
                    profile_count=newcomer_profile_count,
                    official_count=official_cohort_count,
                    guild_name=guild,
                    country=country_name,
                ) if retention_end >= newcomer_start else False
                if app != 'linky' or metric_complete or retention_end < newcomer_start:
                    break
                retention_end -= timedelta(days=1)
            retention_cohort_count = newcomer_profile_count
            if app == 'linky' and metric_complete:
                newcomer_data_as_of = min(
                    newcomer_data_as_of,
                    retention_end + timedelta(days=day_offset),
                )
            measurable = retention_cohort_count > 0 and newcomer_profile_count > 0 and metric_complete
            retained = int(retained_row[1] or 0) if measurable else None
            newcomer_metric_ranges[f'retention_d{days}'] = {
                'date_from': newcomer_start.isoformat(),
                'date_to': retention_end.isoformat(),
            } if retention_end >= newcomer_start else {}
            retention.append({
                'day': days, 'eligible': retention_cohort_count, 'retained': retained,
                'rate': _ratio(retained, retention_cohort_count) if retained is not None else None,
            })

    if app == 'linky' and revenue_data_as_of is not None and newcomer_data_as_of < revenue_data_as_of:
        capabilities['newcomer_source_status'] = 'fallback'
        capabilities['newcomer_source_note'] = (
            f'新人指标已回退到最近连续完整日 '
            f'{newcomer_data_as_of.isoformat()}；平台总收益仍使用 '
            f'{revenue_data_as_of.isoformat()}。'
        )

    state = conn.execute(
        "SELECT data_as_of, materialized_at FROM streamer_analytics_materialization_state WHERE app_name = ? AND status = 'ready'",
        (app,),
    ).fetchone()
    if app == 'timo':
        weekly_cohorts = build_timo_weekly_cohorts(
            conn, start=start, end=end, guild_name=guild, country=country_name,
        )
    elif app == 'linky':
        weekly_cohorts = build_linky_weekly_cohorts(
            conn, start=start, end=end, guild_name=guild, country=country_name,
        )
    else:
        weekly_cohorts = {'available': False, 'data_as_of': None, 'rows': []}
    return {
        'app': app,
        'app_label': APP_LABELS[app],
        'income_units_per_usd': PLATFORM_INCOME_UNITS_PER_USD[app],
        'date_from': start.isoformat(),
        'date_to': end.isoformat(),
        'country': country_name,
        'guild_name': guild,
        'capabilities': capabilities,
        'summary': {
            'guild_count': len(guild_names),
            'streamer_count': sum(profile_count_by_guild.values()),
            'new_streamers': sum(new_count_by_guild.values()),
            'active_streamers': active_streamer_count if revenue_available else None,
            'total_income': round(sum(guild_income.values()), 2) if revenue_available else None,
        },
        'newcomer_revenue': newcomer_revenue,
        'retention': retention,
        'newcomer_metric_ranges': newcomer_metric_ranges,
        'newcomer_data_as_of': newcomer_data_as_of.isoformat(),
        'newcomer_cohort_date_from': newcomer_start.isoformat(),
        'newcomer_cohort_date_to': newcomer_end.isoformat(),
        'weekly_cohorts': weekly_cohorts,
        'app_fan_revenue_comparison': app_fan_revenue_comparison,
        'countries': countries,
        'guild_options': [
            {
                'guild_name': str(row.get('guild_name') or ''),
                'country': str(row.get('country') or ''),
            }
            for row in configured_all
            if str(row.get('guild_name') or '').strip()
        ],
        'guilds': guild_rows,
        'streamers': ranking,
        'trend': [
            {
                'date': stat_date,
                'new_streamers': values['new'],
                'active_streamers': active_by_date.get(stat_date, 0) if stat_date in revenue_observed_dates else None,
                'total_income': round(values['revenue'], 2) if stat_date in revenue_observed_dates else None,
            }
            for stat_date, values in sorted(trend.items())
        ],
        'definitions': {
            'newcomer_revenue': '各指标使用从新人样本起始日至其最新成熟注册日的全部主播。首 N 日收益为注册日开始连续 N 个自然日累计收益。',
            'revenue_retention': 'D1/D7/D30 收益活跃留存分别统计注册后第 1/7/30 天收益大于 0 的主播。各指标按自己的成熟注册范围计算分母。',
        },
        'generated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'materialized_at': str(state[1] or '') if state else '',
        'data_as_of': revenue_data_as_of.isoformat() if revenue_data_as_of else '',
    }


def _streamer_analytics_connection_identity(conn: sqlite3.Connection) -> str:
    try:
        rows = conn.execute('PRAGMA database_list').fetchall()
    except sqlite3.Error:
        return f'connection:{id(conn)}'
    for row in rows:
        if str(row[1] or '') != 'main':
            continue
        path = str(row[2] or '').strip()
        return str(Path(path).resolve()) if path else f'connection:{id(conn)}'
    return f'connection:{id(conn)}'


def _streamer_analytics_generation(
    conn: sqlite3.Connection,
    *,
    app: str,
    materialized_at: str,
) -> str:
    try:
        row = conn.execute(
            "SELECT value FROM streamer_analytics_store_meta WHERE key = ?",
            (f'active_generation:{app}',),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    return str(row[0] or '').strip() if row else str(materialized_at or '').strip()


def _streamer_analytics_payload_cache_get(key: tuple[Any, ...]) -> Optional[Dict[str, Any]]:
    now = time.monotonic()
    with _STREAMER_ANALYTICS_PAYLOAD_CACHE_LOCK:
        cached = _STREAMER_ANALYTICS_PAYLOAD_CACHE.get(key)
        if not cached:
            return None
        expires_at, payload = cached
        if expires_at <= now:
            _STREAMER_ANALYTICS_PAYLOAD_CACHE.pop(key, None)
            return None
        _STREAMER_ANALYTICS_PAYLOAD_CACHE.move_to_end(key)
        return deepcopy(payload)


def _streamer_analytics_payload_cache_set(
    key: tuple[Any, ...],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    with _STREAMER_ANALYTICS_PAYLOAD_CACHE_LOCK:
        _STREAMER_ANALYTICS_PAYLOAD_CACHE[key] = (
            time.monotonic() + STREAMER_ANALYTICS_PAYLOAD_CACHE_TTL_SECONDS,
            deepcopy(payload),
        )
        _STREAMER_ANALYTICS_PAYLOAD_CACHE.move_to_end(key)
        while len(_STREAMER_ANALYTICS_PAYLOAD_CACHE) > STREAMER_ANALYTICS_PAYLOAD_CACHE_MAX_ENTRIES:
            _STREAMER_ANALYTICS_PAYLOAD_CACHE.popitem(last=False)
    return payload


def build_streamer_analytics_metadata(conn: sqlite3.Connection) -> Dict[str, Any]:
    try:
        rows = _rows(
            conn,
            """
            SELECT app_name, status, data_as_of, materialized_at
            FROM streamer_analytics_materialization_state
            WHERE app_name IN ('timo', 'linky', 'sugo')
            """,
        )
    except sqlite3.OperationalError:
        rows = []
    apps: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        app = normalize_streamer_app(row.get('app_name'))
        apps[app] = {
            'status': str(row.get('status') or ''),
            'data_as_of': str(row.get('data_as_of') or ''),
            'materialized_at': str(row.get('materialized_at') or ''),
            'generation': _streamer_analytics_generation(
                conn,
                app=app,
                materialized_at=str(row.get('materialized_at') or ''),
            ),
        }
    return {'apps': apps}


def build_streamer_analytics_payload(
    conn: sqlite3.Connection,
    *,
    app_name: object = 'timo',
    date_from: object = None,
    date_to: object = None,
    guild_name: object = '',
    country: object = '',
    limit: int = 20,
) -> Dict[str, Any]:
    app = normalize_streamer_app(app_name)
    try:
        state = conn.execute(
            "SELECT status, materialized_at FROM streamer_analytics_materialization_state WHERE app_name = ?",
            (app,),
        ).fetchone()
    except sqlite3.OperationalError:
        state = None
    if state and str(state[0] or '') == 'ready':
        start, end = _date_window(date_from, date_to)
        normalized_guild = str(guild_name or '').strip()
        normalized_country = str(country or '').strip()
        normalized_limit = max(1, min(int(limit or 20), 100))
        generation = _streamer_analytics_generation(
            conn,
            app=app,
            materialized_at=str(state[1] or ''),
        )
        cache_key = (
            _streamer_analytics_connection_identity(conn),
            generation,
            app,
            start.isoformat(),
            end.isoformat(),
            normalized_country,
            normalized_guild,
            normalized_limit,
        )
        cached = _streamer_analytics_payload_cache_get(cache_key)
        if cached is not None:
            return cached
        payload = _build_streamer_analytics_payload_materialized(
            conn,
            app_name=app,
            date_from=start,
            date_to=end,
            guild_name=normalized_guild,
            country=normalized_country,
            limit=normalized_limit,
        )
        return _streamer_analytics_payload_cache_set(cache_key, payload)
    return _build_streamer_analytics_payload_live(
        conn,
        app_name=app,
        date_from=date_from,
        date_to=date_to,
        guild_name=guild_name,
        country=country,
        limit=limit,
    )
