from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from app.streamer_analytics import (
    StreamerAnalyticsCohortSnapshotUnavailable,
    build_timo_weekly_cohorts,
    normalize_streamer_app,
)


FIRST_ROI_WEEK_START = date(2026, 7, 6)
ROI_TRACKING_WEEKS = 9
BJ_TZ = timezone(timedelta(hours=8))
SHARED_COST_FIELDS = (
    'admin_cost_usd',
    'customer_service_cost_usd',
    'media_buyer_cost_usd',
    'activity_cost_usd',
)

TIMO_SCORECARD_REFERENCE = {
    'source': 'timo_roi_deep_analysis_2026-07-12',
    'unit_price_max_usd': 2.0,
    'certification_rate_min': 0.65,
    'income_rate_min': 0.70,
    'w1_arpu_min_usd': 1.0,
    'active_cost_target_usd': 0.50,
    'active_cost_redline_usd': 0.60,
}

TIMO_FORECAST_STANDARD = {
    'source': 'timo_weekly_roi_dashboard_2026-07-12',
    'unit_price_usd': 1.84,
    'w1_arpu_usd': 1.82,
    'retention_w2': 0.317,
    'active_cost_per_streamer_usd': 0.45,
}


DEFAULT_POLICIES = (
    # app, country, guild, units/$, CPS (including subsidy), newcomer CPA,
    # non-certified CPA, certified CPA, 7d bonus, 10d bonus
    ('timo', 'Mexico', 'Agency MX somente', 20000.0, 0.30, 0.0, 0.50, 1.80, 0.50, 1.00),
    ('timo', 'Brazil', 'agency of BR somente', 20000.0, 0.25, 0.0, 0.50, 1.80, 0.50, 1.00),
    ('linky', 'Indonesia', 'Nova', 5000.0, 1.20, 0.0, 0.0, 0.0, 0.0, 0.0),
    ('linky', 'Indonesia', 'Carote', 5000.0, 0.25, 3.0, 0.0, 0.0, 0.0, 0.0),
    ('linky', 'Brazil', 'BR-HotSozinha', 5000.0, 0.25, 2.5, 0.0, 0.0, 0.0, 0.0),
    ('linky', 'Brazil', 'BR-EVIAN', 5000.0, 0.90, 3.0, 0.0, 0.0, 0.0, 0.0),
    ('linky', 'Brazil', 'Whisky🍸', 5000.0, 0.70, 1.0, 0.0, 0.0, 0.0, 0.0),
    ('linky', 'Mexico', 'Nova-Spa', 5000.0, 1.70, 0.0, 0.0, 0.0, 0.0, 0.0),
    ('linky', 'Mexico', 'Evian✨', 5000.0, 1.10, 3.0, 0.0, 0.0, 0.0, 0.0),
)


class StreamerAnalyticsSnapshotUnavailable(RuntimeError):
    """The configured analytics snapshot cannot safely serve ROI reads."""


TIMO_INDONESIA_STREAMER_TIERS = (
    (1, 20_000, 2_000, 2_000),
    (2, 30_000, 6_000, 4_000),
    (3, 50_000, 10_000, 4_000),
    (4, 70_000, 14_000, 4_000),
    (5, 100_000, 25_000, 11_000),
    (6, 150_000, 38_000, 13_000),
    (7, 300_000, 75_000, 37_000),
    (8, 500_000, 125_000, 50_000),
    (9, 700_000, 175_000, 50_000),
    (10, 1_000_000, 300_000, 125_000),
)

TIMO_INDONESIA_GUILD_TIERS = (
    (1, 100_000, 10_000, 10_000),
    (2, 200_000, 30_000, 20_000),
    (3, 600_000, 130_000, 100_000),
    (4, 1_000_000, 230_000, 100_000),
    (5, 1_400_000, 330_000, 100_000),
    (6, 2_000_000, 450_000, 120_000),
    (7, 2_600_000, 580_000, 130_000),
    (8, 3_200_000, 720_000, 140_000),
    (9, 4_000_000, 950_000, 230_000),
    (10, 5_000_000, 1_200_000, 250_000),
    (11, 6_000_000, 1_500_000, 300_000),
    (12, 8_000_000, 2_000_000, 500_000),
    (13, 10_000_000, 2_500_000, 500_000),
    (14, 12_000_000, 3_000_000, 500_000),
    (15, 14_000_000, 3_500_000, 500_000),
    (16, 16_000_000, 4_000_000, 500_000),
    (17, 18_000_000, 4_500_000, 500_000),
    (18, 20_000_000, 5_000_000, 500_000),
    (19, 25_000_000, 6_000_000, 1_000_000),
    (20, 30_000_000, 7_000_000, 1_000_000),
    (21, 35_000_000, 8_300_000, 1_300_000),
    (22, 40_000_000, 10_000_000, 1_700_000),
    (23, 50_000_000, 12_500_000, 2_500_000),
    (24, 60_000_000, 16_500_000, 4_000_000),
)


def _now_iso() -> str:
    return datetime.now(BJ_TZ).replace(microsecond=0).isoformat()


def _today_bj() -> date:
    return datetime.now(BJ_TZ).date()


def _dict_rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    cursor = conn.execute(sql, tuple(params))
    columns = [item[0] for item in cursor.description or ()]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _iso_date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value or '')[:10])
    except ValueError:
        return None


def _round_money(value: Optional[float]) -> Optional[float]:
    return round(float(value), 2) if value is not None else None


def require_streamer_analytics_snapshot_ready(
    conn: sqlite3.Connection,
    *,
    app_name: Any,
) -> Dict[str, Any]:
    app = normalize_streamer_app(app_name)
    try:
        rows = _dict_rows(
            conn,
            """
            SELECT status, data_as_of, profile_count, streamer_daily_count,
                   daily_summary_count, newcomer_count, cohort_scope_count,
                   materialized_at
            FROM streamer_analytics_materialization_state
            WHERE app_name = ?
            """,
            (app,),
        )
    except sqlite3.Error as exc:
        raise StreamerAnalyticsSnapshotUnavailable(
            'streamer_analytics_snapshot_unavailable'
        ) from exc
    if len(rows) != 1:
        raise StreamerAnalyticsSnapshotUnavailable(
            'streamer_analytics_snapshot_unavailable'
        )
    state = rows[0]
    if str(state.get('status') or '').strip().lower() != 'ready':
        raise StreamerAnalyticsSnapshotUnavailable(
            'streamer_analytics_snapshot_unavailable'
        )
    if _iso_date(state.get('data_as_of')) is None:
        raise StreamerAnalyticsSnapshotUnavailable(
            'streamer_analytics_snapshot_unavailable'
        )
    try:
        materialized_at = str(state.get('materialized_at') or '').strip()
        datetime.fromisoformat(materialized_at.replace('Z', '+00:00'))
        for field in (
            'profile_count',
            'streamer_daily_count',
            'daily_summary_count',
            'newcomer_count',
            'cohort_scope_count',
        ):
            if int(state.get(field)) < 0:
                raise ValueError(field)
    except (TypeError, ValueError):
        raise StreamerAnalyticsSnapshotUnavailable(
            'streamer_analytics_snapshot_unavailable'
        ) from None
    return state


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {str(row[1]) for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    if column not in columns:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')


def ensure_streamer_roi_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS streamer_roi_policies (
            app_name TEXT NOT NULL,
            country TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            effective_from TEXT NOT NULL,
            income_units_per_usd REAL NOT NULL,
            cps_rate REAL NOT NULL,
            newcomer_cpa_usd REAL NOT NULL DEFAULT 0,
            non_certified_cpa_usd REAL NOT NULL DEFAULT 0,
            certified_cpa_usd REAL NOT NULL DEFAULT 0,
            bonus_7d_usd REAL NOT NULL DEFAULT 0,
            bonus_10d_usd REAL NOT NULL DEFAULT 0,
            source_label TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (app_name, country, guild_name, effective_from)
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_roi_policy_lookup
            ON streamer_roi_policies(app_name, country, guild_name, effective_from DESC);

        CREATE TABLE IF NOT EXISTS streamer_roi_policy_tiers (
            app_name TEXT NOT NULL,
            country TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            effective_from TEXT NOT NULL,
            tier_scope TEXT NOT NULL,
            tier_level INTEGER NOT NULL,
            threshold_income_units REAL NOT NULL,
            cumulative_reward_units REAL NOT NULL,
            incremental_reward_units REAL NOT NULL,
            PRIMARY KEY (app_name, country, guild_name, effective_from, tier_scope, tier_level)
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_roi_policy_tier_lookup
            ON streamer_roi_policy_tiers(app_name, country, guild_name, effective_from, tier_scope, threshold_income_units);

        CREATE TABLE IF NOT EXISTS streamer_roi_policy_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_name TEXT NOT NULL,
            country TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            effective_from TEXT NOT NULL,
            action TEXT NOT NULL,
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            reason TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_roi_policy_audit_scope
            ON streamer_roi_policy_audit(app_name, country, guild_name, effective_from, audit_id DESC);

        CREATE TABLE IF NOT EXISTS streamer_roi_weekly_inputs (
            app_name TEXT NOT NULL,
            country TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            week_start TEXT NOT NULL,
            ad_cost_usd REAL,
            admin_cost_usd REAL,
            customer_service_cost_usd REAL,
            media_buyer_cost_usd REAL,
            activity_cost_usd REAL,
            status TEXT NOT NULL DEFAULT 'draft',
            revision INTEGER NOT NULL DEFAULT 1,
            correction_reason TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            published_by TEXT NOT NULL DEFAULT '',
            published_at TEXT,
            PRIMARY KEY (app_name, country, guild_name, week_start)
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_roi_weekly_scope
            ON streamer_roi_weekly_inputs(app_name, week_start, country, guild_name);

        CREATE TABLE IF NOT EXISTS streamer_roi_weekly_input_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_name TEXT NOT NULL,
            country TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            week_start TEXT NOT NULL,
            action TEXT NOT NULL,
            revision INTEGER NOT NULL,
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            reason TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_streamer_roi_audit_scope
            ON streamer_roi_weekly_input_audit(app_name, week_start, country, guild_name, audit_id DESC);
        """
    )
    _ensure_column(conn, 'streamer_roi_policies', 'calculation_mode', "TEXT NOT NULL DEFAULT 'flat'")
    _ensure_column(conn, 'streamer_roi_policies', 'guild_eligible_host_min_units', 'REAL NOT NULL DEFAULT 0')
    _ensure_column(conn, 'streamer_roi_policies', 'policy_note', "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, 'streamer_roi_policies', 'updated_by', "TEXT NOT NULL DEFAULT ''")
    now = _now_iso()
    conn.executemany(
        """
        INSERT OR IGNORE INTO streamer_roi_policies (
            app_name, country, guild_name, effective_from, income_units_per_usd,
            cps_rate, newcomer_cpa_usd, non_certified_cpa_usd,
            certified_cpa_usd, bonus_7d_usd, bonus_10d_usd,
            source_label, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '项目roi测算.xlsx', ?)
        """,
        [(*row[:3], FIRST_ROI_WEEK_START.isoformat(), *row[3:], now) for row in DEFAULT_POLICIES],
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO streamer_roi_policies (
            app_name, country, guild_name, effective_from, income_units_per_usd,
            cps_rate, newcomer_cpa_usd, non_certified_cpa_usd,
            certified_cpa_usd, bonus_7d_usd, bonus_10d_usd,
            source_label, updated_at, calculation_mode,
            guild_eligible_host_min_units, policy_note, updated_by
        ) VALUES ('timo','Indonesia','TIMO001','2026-06-01',20000,0,0,0,0,0,0,?,?,'timo_tiered_1v1',10000,?,'system')
        """,
        (
            'timo印尼.xlsx', now,
            '1v1主播10档、公会24档；公会有效收入仅累计当周1v1收益达到10000钻石的主播。',
        ),
    )
    tier_rows = [
        ('timo', 'Indonesia', 'TIMO001', '2026-06-01', scope, *tier)
        for scope, tiers in (
            ('streamer', TIMO_INDONESIA_STREAMER_TIERS),
            ('guild', TIMO_INDONESIA_GUILD_TIERS),
        )
        for tier in tiers
    ]
    conn.executemany(
        """
        INSERT OR IGNORE INTO streamer_roi_policy_tiers (
            app_name,country,guild_name,effective_from,tier_scope,tier_level,
            threshold_income_units,cumulative_reward_units,incremental_reward_units
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        tier_rows,
    )
    legacy_linky_policies = _dict_rows(
        conn,
        """
        SELECT * FROM streamer_roi_policies
        WHERE app_name='linky' AND income_units_per_usd=10000
          AND source_label='项目roi测算.xlsx'
        """,
    )
    for before in legacy_linky_policies:
        after = dict(before)
        after['income_units_per_usd'] = 5000.0
        after['updated_at'] = now
        after['updated_by'] = 'system'
        conn.execute(
            """
            UPDATE streamer_roi_policies
            SET income_units_per_usd=5000, updated_at=?, updated_by='system'
            WHERE app_name=? AND country=? AND guild_name=? AND effective_from=?
            """,
            (
                now,
                before['app_name'], before['country'], before['guild_name'],
                before['effective_from'],
            ),
        )
        conn.execute(
            """
            INSERT INTO streamer_roi_policy_audit (
                app_name,country,guild_name,effective_from,action,before_json,
                after_json,reason,actor,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                before['app_name'], before['country'], before['guild_name'],
                before['effective_from'], 'corrected',
                json.dumps(before, ensure_ascii=False, sort_keys=True),
                json.dumps(after, ensure_ascii=False, sort_keys=True),
                'Linky钻石兑美元口径纠正为5000:1', 'system', now,
            ),
        )


def _latest_target_week(today: Optional[date] = None) -> date:
    current = today or _today_bj()
    current_week_start = current - timedelta(days=current.weekday())
    latest_complete = current_week_start - timedelta(days=7)
    return max(FIRST_ROI_WEEK_START, latest_complete)


def _policy_for(
    conn: sqlite3.Connection,
    *,
    app: str,
    country: str,
    guild_name: str,
    week_start: date,
) -> Optional[Dict[str, Any]]:
    rows = _dict_rows(
        conn,
        """
        SELECT * FROM streamer_roi_policies
        WHERE app_name = ? AND country = ? AND guild_name = ? AND effective_from <= ?
        ORDER BY effective_from DESC LIMIT 1
        """,
        (app, country, guild_name, week_start.isoformat()),
    )
    if not rows:
        return None
    policy = rows[0]
    policy['tiers'] = _policy_tiers(
        conn,
        app=app,
        country=country,
        guild_name=guild_name,
        effective_from=str(policy['effective_from']),
    )
    return policy


def _policies_for_guilds(
    conn: sqlite3.Connection,
    *,
    app: str,
    week_start: date,
) -> Dict[tuple[str, str], Dict[str, Any]]:
    policies: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in _dict_rows(
        conn,
        """
        SELECT * FROM streamer_roi_policies
        WHERE app_name = ? AND effective_from <= ?
        ORDER BY country, guild_name, effective_from DESC
        """,
        (app, week_start.isoformat()),
    ):
        key = (str(row.get('country') or ''), str(row.get('guild_name') or ''))
        if key not in policies:
            row['tiers'] = {'streamer': [], 'guild': []}
            policies[key] = row
    if not policies:
        return policies
    for row in _dict_rows(
        conn,
        """
        SELECT country, guild_name, effective_from, tier_scope, tier_level,
               threshold_income_units, cumulative_reward_units,
               incremental_reward_units
        FROM streamer_roi_policy_tiers
        WHERE app_name = ? AND effective_from <= ?
        ORDER BY country, guild_name, effective_from DESC, tier_scope, tier_level
        """,
        (app, week_start.isoformat()),
    ):
        key = (str(row.get('country') or ''), str(row.get('guild_name') or ''))
        policy = policies.get(key)
        if not policy or str(row.get('effective_from') or '') != str(policy.get('effective_from') or ''):
            continue
        scope = str(row.get('tier_scope') or '')
        if scope not in policy['tiers']:
            continue
        policy['tiers'][scope].append({
            'tier_level': int(row.get('tier_level') or 0),
            'threshold_income_units': float(row.get('threshold_income_units') or 0),
            'cumulative_reward_units': float(row.get('cumulative_reward_units') or 0),
            'incremental_reward_units': float(row.get('incremental_reward_units') or 0),
        })
    return policies


def _policy_history_for_guilds(
    conn: sqlite3.Connection,
    *,
    app: str,
    through_week: date,
) -> Dict[tuple[str, str], List[Dict[str, Any]]]:
    """Load every policy version once so calendar aggregation stays O(1) in SQL calls."""
    history: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
    policies = _dict_rows(
        conn,
        """
        SELECT * FROM streamer_roi_policies
        WHERE app_name = ? AND effective_from <= ?
        ORDER BY country, guild_name, effective_from
        """,
        (app, through_week.isoformat()),
    )
    by_version: Dict[tuple[str, str, str], Dict[str, Any]] = {}
    for policy in policies:
        key = (str(policy.get('country') or ''), str(policy.get('guild_name') or ''))
        policy['tiers'] = {'streamer': [], 'guild': []}
        history.setdefault(key, []).append(policy)
        by_version[(*key, str(policy.get('effective_from') or ''))] = policy
    if not policies:
        return history
    for tier in _dict_rows(
        conn,
        """
        SELECT country, guild_name, effective_from, tier_scope, tier_level,
               threshold_income_units, cumulative_reward_units,
               incremental_reward_units
        FROM streamer_roi_policy_tiers
        WHERE app_name = ? AND effective_from <= ?
        ORDER BY country, guild_name, effective_from, tier_scope, tier_level
        """,
        (app, through_week.isoformat()),
    ):
        policy = by_version.get((
            str(tier.get('country') or ''),
            str(tier.get('guild_name') or ''),
            str(tier.get('effective_from') or ''),
        ))
        scope = str(tier.get('tier_scope') or '')
        if not policy or scope not in policy['tiers']:
            continue
        policy['tiers'][scope].append({
            'tier_level': int(tier.get('tier_level') or 0),
            'threshold_income_units': float(tier.get('threshold_income_units') or 0),
            'cumulative_reward_units': float(tier.get('cumulative_reward_units') or 0),
            'incremental_reward_units': float(tier.get('incremental_reward_units') or 0),
        })
    return history


def _policy_from_history(
    history: Dict[tuple[str, str], List[Dict[str, Any]]],
    *,
    country: str,
    guild_name: str,
    cohort_week: date,
) -> Optional[Dict[str, Any]]:
    versions = history.get((country, guild_name), [])
    return next(
        (
            policy for policy in reversed(versions)
            if str(policy.get('effective_from') or '') <= cohort_week.isoformat()
        ),
        None,
    )


def _policy_tiers(
    conn: sqlite3.Connection,
    *,
    app: str,
    country: str,
    guild_name: str,
    effective_from: str,
) -> Dict[str, List[Dict[str, Any]]]:
    result: Dict[str, List[Dict[str, Any]]] = {'streamer': [], 'guild': []}
    for row in _dict_rows(
        conn,
        """
        SELECT tier_scope, tier_level, threshold_income_units,
               cumulative_reward_units, incremental_reward_units
        FROM streamer_roi_policy_tiers
        WHERE app_name=? AND country=? AND guild_name=? AND effective_from=?
        ORDER BY tier_scope, tier_level
        """,
        (app, country, guild_name, effective_from),
    ):
        scope = str(row.pop('tier_scope') or '')
        if scope in result:
            result[scope].append(row)
    return result


def _policy_snapshot(conn: sqlite3.Connection, row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not row:
        return {}
    payload = dict(row)
    payload['tiers'] = _policy_tiers(
        conn,
        app=str(row['app_name']),
        country=str(row['country']),
        guild_name=str(row['guild_name']),
        effective_from=str(row['effective_from']),
    )
    return payload


def list_streamer_roi_policies(
    conn: sqlite3.Connection,
    *,
    app_name: Any,
    country: str = '',
    guild_name: str = '',
    ensure_schema: bool = True,
) -> Dict[str, Any]:
    if ensure_schema:
        ensure_streamer_roi_tables(conn)
    app = normalize_streamer_app(app_name)
    configured = _configured_guilds(conn, app=app, country=country, guild_name=guild_name)
    clauses = ['app_name=?']
    params: List[Any] = [app]
    if country:
        clauses.append('country=?')
        params.append(country)
    if guild_name:
        clauses.append('guild_name=?')
        params.append(guild_name)
    policies = [
        _policy_snapshot(conn, row)
        for row in _dict_rows(
            conn,
            f"SELECT * FROM streamer_roi_policies WHERE {' AND '.join(clauses)} ORDER BY country,guild_name,effective_from DESC",
            params,
        )
    ]
    return {'app': app, 'guilds': configured, 'policies': policies}


def _normalized_policy_tiers(raw_tiers: Any, scope: str) -> List[Dict[str, Any]]:
    tiers: List[Dict[str, Any]] = []
    previous_threshold = -1.0
    previous_cumulative = 0.0
    for index, raw in enumerate(raw_tiers or (), start=1):
        level = int(raw.get('tier_level') or index)
        threshold = float(raw.get('threshold_income_units') or 0)
        cumulative = float(raw.get('cumulative_reward_units') or 0)
        incremental = float(raw.get('incremental_reward_units') or 0)
        if level != index or threshold <= previous_threshold or threshold <= 0:
            raise ValueError(f'streamer_roi_{scope}_tiers_must_increase')
        if cumulative < previous_cumulative or incremental < 0:
            raise ValueError(f'streamer_roi_{scope}_reward_must_increase')
        if abs((cumulative - previous_cumulative) - incremental) > 0.01:
            raise ValueError(f'streamer_roi_{scope}_incremental_reward_mismatch')
        tiers.append({
            'tier_level': level,
            'threshold_income_units': round(threshold, 2),
            'cumulative_reward_units': round(cumulative, 2),
            'incremental_reward_units': round(incremental, 2),
        })
        previous_threshold = threshold
        previous_cumulative = cumulative
    return tiers


def save_streamer_roi_policy(
    conn: sqlite3.Connection,
    *,
    payload: Dict[str, Any],
    actor: str,
) -> Dict[str, Any]:
    ensure_streamer_roi_tables(conn)
    app = normalize_streamer_app(payload.get('app'))
    country = str(payload.get('country') or '').strip()
    guild_name = str(payload.get('guild_name') or '').strip()
    effective = _iso_date(payload.get('effective_from'))
    if not country or not guild_name:
        raise ValueError('streamer_roi_policy_scope_required')
    if not effective or effective.weekday() != 0:
        raise ValueError('streamer_roi_policy_effective_date_must_be_monday')
    configured = {(row['country'], row['guild_name']) for row in _configured_guilds(conn, app=app)}
    if (country, guild_name) not in configured:
        raise ValueError('streamer_roi_guild_not_configured')
    mode = str(payload.get('calculation_mode') or 'flat').strip().lower()
    if mode not in {'flat', 'timo_tiered_1v1'}:
        raise ValueError('streamer_roi_policy_mode_invalid')
    units_per_usd = float(payload.get('income_units_per_usd') or 0)
    # Timo 1v1 uses the weekly guild tier table as the complete settlement
    # rule. A percentage CPS would double-count the same policy income.
    cps_rate = 0.0 if mode == 'timo_tiered_1v1' else float(payload.get('cps_rate') or 0)
    if not math.isfinite(units_per_usd) or units_per_usd <= 0:
        raise ValueError('streamer_roi_policy_units_per_usd_invalid')
    if not math.isfinite(cps_rate) or cps_rate < 0:
        raise ValueError('streamer_roi_policy_cps_rate_invalid')
    streamer_tiers = _normalized_policy_tiers(payload.get('streamer_tiers'), 'streamer')
    guild_tiers = _normalized_policy_tiers(payload.get('guild_tiers'), 'guild')
    if mode == 'timo_tiered_1v1' and (app != 'timo' or not guild_tiers):
        raise ValueError('streamer_roi_tiered_policy_requires_guild_tiers')
    reason = str(payload.get('change_reason') or '').strip()
    if not reason:
        raise ValueError('streamer_roi_policy_change_reason_required')
    key = (app, country, guild_name, effective.isoformat())
    existing_rows = _dict_rows(
        conn,
        'SELECT * FROM streamer_roi_policies WHERE app_name=? AND country=? AND guild_name=? AND effective_from=?',
        key,
    )
    before = _policy_snapshot(conn, existing_rows[0]) if existing_rows else {}
    now = _now_iso()
    values = {
        'app_name': app,
        'country': country,
        'guild_name': guild_name,
        'effective_from': effective.isoformat(),
        'income_units_per_usd': round(units_per_usd, 4),
        'cps_rate': round(cps_rate, 6),
        'newcomer_cpa_usd': round(float(payload.get('newcomer_cpa_usd') or 0), 4),
        'non_certified_cpa_usd': round(float(payload.get('non_certified_cpa_usd') or 0), 4),
        'certified_cpa_usd': round(float(payload.get('certified_cpa_usd') or 0), 4),
        'bonus_7d_usd': round(float(payload.get('bonus_7d_usd') or 0), 4),
        'bonus_10d_usd': round(float(payload.get('bonus_10d_usd') or 0), 4),
        'source_label': str(payload.get('source_label') or '运营后台配置').strip(),
        'updated_at': now,
        'calculation_mode': mode,
        'guild_eligible_host_min_units': round(float(payload.get('guild_eligible_host_min_units') or 0), 2),
        'policy_note': str(payload.get('policy_note') or '').strip(),
        'updated_by': actor,
    }
    for key_name in (
        'newcomer_cpa_usd', 'non_certified_cpa_usd', 'certified_cpa_usd',
        'bonus_7d_usd', 'bonus_10d_usd', 'guild_eligible_host_min_units',
    ):
        if not math.isfinite(float(values[key_name])) or float(values[key_name]) < 0:
            raise ValueError('streamer_roi_policy_value_must_be_non_negative')
    conn.execute(
        """
        INSERT INTO streamer_roi_policies (
            app_name,country,guild_name,effective_from,income_units_per_usd,cps_rate,
            newcomer_cpa_usd,non_certified_cpa_usd,certified_cpa_usd,bonus_7d_usd,
            bonus_10d_usd,source_label,updated_at,calculation_mode,
            guild_eligible_host_min_units,policy_note,updated_by
        ) VALUES (
            :app_name,:country,:guild_name,:effective_from,:income_units_per_usd,:cps_rate,
            :newcomer_cpa_usd,:non_certified_cpa_usd,:certified_cpa_usd,:bonus_7d_usd,
            :bonus_10d_usd,:source_label,:updated_at,:calculation_mode,
            :guild_eligible_host_min_units,:policy_note,:updated_by
        )
        ON CONFLICT(app_name,country,guild_name,effective_from) DO UPDATE SET
            income_units_per_usd=excluded.income_units_per_usd,cps_rate=excluded.cps_rate,
            newcomer_cpa_usd=excluded.newcomer_cpa_usd,
            non_certified_cpa_usd=excluded.non_certified_cpa_usd,
            certified_cpa_usd=excluded.certified_cpa_usd,bonus_7d_usd=excluded.bonus_7d_usd,
            bonus_10d_usd=excluded.bonus_10d_usd,source_label=excluded.source_label,
            updated_at=excluded.updated_at,calculation_mode=excluded.calculation_mode,
            guild_eligible_host_min_units=excluded.guild_eligible_host_min_units,
            policy_note=excluded.policy_note,updated_by=excluded.updated_by
        """,
        values,
    )
    conn.execute(
        'DELETE FROM streamer_roi_policy_tiers WHERE app_name=? AND country=? AND guild_name=? AND effective_from=?',
        key,
    )
    tier_rows = [
        (*key, scope, tier['tier_level'], tier['threshold_income_units'], tier['cumulative_reward_units'], tier['incremental_reward_units'])
        for scope, tiers in (('streamer', streamer_tiers), ('guild', guild_tiers))
        for tier in tiers
    ]
    if tier_rows:
        conn.executemany(
            """
            INSERT INTO streamer_roi_policy_tiers (
                app_name,country,guild_name,effective_from,tier_scope,tier_level,
                threshold_income_units,cumulative_reward_units,incremental_reward_units
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            tier_rows,
        )
    after = _policy_snapshot(conn, values)
    conn.execute(
        """
        INSERT INTO streamer_roi_policy_audit (
            app_name,country,guild_name,effective_from,action,before_json,after_json,reason,actor,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (*key, 'corrected' if before else 'created', json.dumps(before, ensure_ascii=False, sort_keys=True),
         json.dumps(after, ensure_ascii=False, sort_keys=True), reason, actor, now),
    )
    conn.commit()
    return list_streamer_roi_policies(conn, app_name=app, country=country, guild_name=guild_name)


def _configured_guilds(
    conn: sqlite3.Connection,
    *,
    app: str,
    country: str = '',
    guild_name: str = '',
) -> List[Dict[str, str]]:
    aliases = ('sugo', 'sogo') if app == 'sugo' else (app,)
    placeholders = ','.join('?' for _ in aliases)
    clauses = [f"enabled = 1 AND lower(app_name) IN ({placeholders})"]
    params: List[Any] = list(aliases)
    if country:
        clauses.append('country = ?')
        params.append(country)
    if guild_name:
        clauses.append('guild_name = ?')
        params.append(guild_name)
    return [
        {'country': str(row.get('country') or ''), 'guild_name': str(row.get('guild_name') or '')}
        for row in _dict_rows(
            conn,
            f"SELECT COALESCE(country, '') AS country, guild_name FROM guild_executors WHERE {' AND '.join(clauses)} ORDER BY country, guild_name",
            params,
        )
    ]


def _cohort_periods(
    conn: sqlite3.Connection,
    *,
    app: str,
    country: str,
    guild_name: str,
    week_start: date,
    units_per_usd: Optional[float],
) -> tuple[int, int, List[Dict[str, Any]], Optional[date]]:
    week_end = week_start + timedelta(days=6)
    profile_rows = _dict_rows(
        conn,
        """
        SELECT streamer_id, is_real_person
        FROM streamer_analytics_profile_summary
        WHERE app_name = ? AND country = ? AND guild_name = ?
          AND registered_date BETWEEN ? AND ?
        """,
        (app, country, guild_name, week_start.isoformat(), week_end.isoformat()),
    )
    streamer_ids = [str(row['streamer_id']) for row in profile_rows]
    certified = sum(1 for row in profile_rows if int(row.get('is_real_person') or 0) == 1)
    state_rows = _dict_rows(
        conn,
        "SELECT data_as_of FROM streamer_analytics_materialization_state WHERE app_name = ? AND status = 'ready'",
        (app,),
    )
    data_as_of = _iso_date(state_rows[0].get('data_as_of')) if state_rows else None
    income_by_streamer_date: Dict[tuple[str, str], float] = {}
    active_by_streamer_date: Dict[tuple[str, str], bool] = {}
    if streamer_ids:
        last_period_end = week_start + timedelta(days=ROI_TRACKING_WEEKS * 7 - 1)
        for row in _dict_rows(
            conn,
            """
            SELECT daily.streamer_id, daily.stat_date,
                   SUM(daily.total_income) AS total_income,
                   MAX(daily.is_active) AS is_active
            FROM streamer_analytics_streamer_daily_summary AS daily
            WHERE daily.app_name = ? AND daily.country = ? AND daily.guild_name = ?
              AND daily.stat_date BETWEEN ? AND ?
              AND EXISTS (
                  SELECT 1
                  FROM streamer_analytics_profile_summary AS profile
                  WHERE profile.app_name = daily.app_name
                    AND profile.country = daily.country
                    AND profile.guild_name = daily.guild_name
                    AND profile.streamer_id = daily.streamer_id
                    AND profile.registered_date BETWEEN ? AND ?
              )
            GROUP BY daily.streamer_id, daily.stat_date
            """,
            (
                app, country, guild_name,
                week_start.isoformat(), last_period_end.isoformat(),
                week_start.isoformat(), week_end.isoformat(),
            ),
        ):
            fact_key = (str(row['streamer_id']), str(row['stat_date']))
            income_by_streamer_date[fact_key] = float(row.get('total_income') or 0)
            active_by_streamer_date[fact_key] = int(row.get('is_active') or 0) == 1
    periods: List[Dict[str, Any]] = []
    for index in range(ROI_TRACKING_WEEKS):
        period_start = week_start + timedelta(days=index * 7)
        period_end = period_start + timedelta(days=6)
        complete = bool(data_as_of and period_end <= data_as_of)
        member_income: List[float] = []
        member_active: List[bool] = []
        if complete:
            for streamer_id in streamer_ids:
                total = sum(
                    income_by_streamer_date.get((streamer_id, (period_start + timedelta(days=offset)).isoformat()), 0.0)
                    for offset in range(7)
                )
                member_income.append(total)
                member_active.append(any(
                    active_by_streamer_date.get(
                        (streamer_id, (period_start + timedelta(days=offset)).isoformat()),
                        False,
                    )
                    for offset in range(7)
                ))
        income_units = sum(member_income) if complete else None
        active_streamers = sum(1 for value in member_active if value) if complete else None
        periods.append({
            'week': index + 1,
            'date_from': period_start.isoformat(),
            'date_to': period_end.isoformat(),
            'status': 'complete' if complete else 'incomplete',
            'active_streamers': active_streamers,
            'income_units': round(income_units, 2) if income_units is not None else None,
            'income_usd': _round_money(income_units / units_per_usd)
            if income_units is not None and units_per_usd else None,
        })
    return len(profile_rows), certified, periods, data_as_of


def _weekly_roi_fact_bundle(
    conn: sqlite3.Connection,
    *,
    app: str,
    week_start: date,
    configured: List[Dict[str, str]],
) -> Dict[str, Any]:
    week_end = week_start + timedelta(days=6)
    tracking_end = week_start + timedelta(days=ROI_TRACKING_WEEKS * 7 - 1)
    state_rows = _dict_rows(
        conn,
        "SELECT data_as_of FROM streamer_analytics_materialization_state WHERE app_name = ? AND status = 'ready'",
        (app,),
    )
    data_as_of = _iso_date(state_rows[0].get('data_as_of')) if state_rows else None
    scope_values = ','.join('(?, ?)' for _item in configured) or "('', '')"
    scope_params = [
        value
        for item in configured
        for value in (str(item.get('country') or ''), str(item.get('guild_name') or ''))
    ]
    cohort_counts = {
        (str(row.get('country') or ''), str(row.get('guild_name') or '')): {
            'new_streamers': int(row.get('new_streamers') or 0),
            'certified_streamers': int(row.get('certified_streamers') or 0),
        }
        for row in _dict_rows(
            conn,
            """
            WITH configured(country, guild_name) AS (VALUES {scope_values})
            SELECT profile.country, profile.guild_name, COUNT(*) AS new_streamers,
                   SUM(CASE WHEN is_real_person = 1 THEN 1 ELSE 0 END) AS certified_streamers
            FROM configured AS scope
            JOIN streamer_analytics_profile_summary AS profile INDEXED BY idx_streamer_analytics_profile_scope
              ON profile.app_name = ?
             AND profile.country = scope.country
             AND profile.guild_name = scope.guild_name
             AND profile.registered_date BETWEEN ? AND ?
            GROUP BY profile.country, profile.guild_name
            """.format(scope_values=scope_values),
            (*scope_params, app, week_start.isoformat(), week_end.isoformat()),
        )
    }
    cohort_periods = {
        (
            str(row.get('country') or ''),
            str(row.get('guild_name') or ''),
            int(row.get('period_index') or 0),
        ): {
            'income_units': float(row.get('income_units') or 0),
            'active_streamers': int(row.get('active_streamers') or 0),
        }
        for row in _dict_rows(
            conn,
            """
            WITH configured(country, guild_name) AS (VALUES {scope_values}),
            cohort AS (
                SELECT profile.guild_executor_key, profile.streamer_id,
                       profile.country, profile.guild_name
                FROM configured AS scope
                JOIN streamer_analytics_profile_summary AS profile INDEXED BY idx_streamer_analytics_profile_scope
                  ON profile.app_name = ?
                 AND profile.country = scope.country
                 AND profile.guild_name = scope.guild_name
                 AND profile.registered_date BETWEEN ? AND ?
            )
            SELECT cohort.country, cohort.guild_name,
                   CAST((julianday(daily.stat_date) - julianday(?)) / 7 AS INTEGER) AS period_index,
                   SUM(daily.total_income) AS income_units,
                   COUNT(DISTINCT CASE WHEN daily.is_active = 1 THEN daily.streamer_id END) AS active_streamers
            FROM cohort
            JOIN streamer_analytics_streamer_daily_summary AS daily INDEXED BY idx_streamer_analytics_streamer_daily_rank
              ON daily.app_name = ?
             AND daily.guild_executor_key = cohort.guild_executor_key
             AND daily.streamer_id = cohort.streamer_id
             AND daily.stat_date BETWEEN ? AND ?
            GROUP BY cohort.country, cohort.guild_name, period_index
            """.format(scope_values=scope_values),
            (
                *scope_params, app,
                week_start.isoformat(), week_end.isoformat(),
                week_start.isoformat(), app,
                week_start.isoformat(), tracking_end.isoformat(),
            ),
        )
        if 0 <= int(row.get('period_index') or 0) < ROI_TRACKING_WEEKS
    }
    whole_week_income = {
        (str(row.get('country') or ''), str(row.get('guild_name') or '')): float(row.get('total_income') or 0)
        for row in _dict_rows(
            conn,
            """
            WITH configured(country, guild_name) AS (VALUES {scope_values})
            SELECT daily.country, daily.guild_name, SUM(daily.total_income) AS total_income
            FROM configured AS scope
            JOIN streamer_analytics_daily_summary AS daily
              ON daily.app_name = ?
             AND daily.country = scope.country
             AND daily.guild_name = scope.guild_name
             AND daily.stat_date BETWEEN ? AND ?
            GROUP BY daily.country, daily.guild_name
            """.format(scope_values=scope_values),
            (*scope_params, app, week_start.isoformat(), week_end.isoformat()),
        )
    }
    all_active_streamers = {
        (
            str(row.get('country') or ''),
            str(row.get('guild_name') or ''),
            int(row.get('period_index') or 0),
        ): int(row.get('active_streamers') or 0)
        for row in _dict_rows(
            conn,
            """
            WITH configured(country, guild_name) AS (VALUES {scope_values})
            SELECT daily.country, daily.guild_name,
                   CAST((julianday(stat_date) - julianday(?)) / 7 AS INTEGER) AS period_index,
                   COUNT(DISTINCT CASE WHEN is_active = 1 THEN streamer_id END) AS active_streamers
            FROM configured AS scope
            JOIN streamer_analytics_streamer_daily_summary AS daily INDEXED BY idx_streamer_analytics_streamer_daily_scope
              ON daily.app_name = ?
             AND daily.country = scope.country
             AND daily.guild_name = scope.guild_name
             AND daily.stat_date BETWEEN ? AND ?
            GROUP BY daily.country, daily.guild_name, period_index
            """.format(scope_values=scope_values),
            (*scope_params, week_start.isoformat(), app, week_start.isoformat(), tracking_end.isoformat()),
        )
        if 0 <= int(row.get('period_index') or 0) < ROI_TRACKING_WEEKS
    }
    tiered_host_income: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
    if app == 'timo':
        for row in _dict_rows(
            conn,
            """
            WITH configured(country, guild_name) AS (VALUES {scope_values})
            SELECT daily.country, daily.guild_name, daily.streamer_id,
                   SUM(daily.total_income) AS income_units
            FROM configured AS scope
            JOIN streamer_analytics_streamer_daily_summary AS daily INDEXED BY idx_streamer_analytics_streamer_daily_scope
              ON daily.app_name = 'timo'
             AND daily.country = scope.country
             AND daily.guild_name = scope.guild_name
             AND daily.stat_date BETWEEN ? AND ?
            GROUP BY daily.country, daily.guild_name, daily.streamer_id
            """.format(scope_values=scope_values),
            (*scope_params, week_start.isoformat(), week_end.isoformat()),
        ):
            tiered_host_income.setdefault(
                (str(row.get('country') or ''), str(row.get('guild_name') or '')),
                [],
            ).append(row)
    return {
        'data_as_of': data_as_of,
        'cohort_counts': cohort_counts,
        'cohort_periods': cohort_periods,
        'whole_week_income': whole_week_income,
        'all_active_streamers': all_active_streamers,
        'tiered_host_income': tiered_host_income,
    }


def _cohort_periods_from_bundle(
    bundle: Dict[str, Any],
    *,
    country: str,
    guild_name: str,
    week_start: date,
    units_per_usd: Optional[float],
) -> tuple[int, int, List[Dict[str, Any]], Optional[date]]:
    key = (country, guild_name)
    counts = bundle['cohort_counts'].get(key, {})
    data_as_of = bundle.get('data_as_of')
    periods: List[Dict[str, Any]] = []
    for index in range(ROI_TRACKING_WEEKS):
        period_start = week_start + timedelta(days=index * 7)
        period_end = period_start + timedelta(days=6)
        complete = bool(data_as_of and period_end <= data_as_of)
        aggregate = bundle['cohort_periods'].get((*key, index), {})
        income_units = float(aggregate.get('income_units') or 0) if complete else None
        active_streamers = int(aggregate.get('active_streamers') or 0) if complete else None
        periods.append({
            'week': index + 1,
            'date_from': period_start.isoformat(),
            'date_to': period_end.isoformat(),
            'status': 'complete' if complete else 'incomplete',
            'active_streamers': active_streamers,
            'income_units': round(income_units, 2) if income_units is not None else None,
            'income_usd': _round_money(income_units / units_per_usd)
            if income_units is not None and units_per_usd else None,
        })
    return (
        int(counts.get('new_streamers') or 0),
        int(counts.get('certified_streamers') or 0),
        periods,
        data_as_of,
    )


def _timo_settlement(
    conn: sqlite3.Connection,
    *,
    country: str,
    guild_name: str,
    week_start: date,
    new_streamers: int,
    certified_streamers: int,
    policy: Dict[str, Any],
    allow_live_fallback: bool = True,
) -> Dict[str, Any]:
    non_certified = max(new_streamers - certified_streamers, 0)
    base = (
        non_certified * float(policy.get('non_certified_cpa_usd') or 0)
        + certified_streamers * float(policy.get('certified_cpa_usd') or 0)
    )
    try:
        cohort_payload = build_timo_weekly_cohorts(
            conn,
            start=week_start,
            end=week_start + timedelta(days=6),
            country=country,
            guild_name=guild_name,
            allow_live_fallback=allow_live_fallback,
        )
    except StreamerAnalyticsCohortSnapshotUnavailable as exc:
        raise StreamerAnalyticsSnapshotUnavailable(
            'streamer_analytics_snapshot_unavailable'
        ) from exc
    matching = next(
        (row for row in cohort_payload.get('rows') or [] if str(row.get('week_start')) == week_start.isoformat()),
        None,
    )
    return _timo_settlement_from_cohort_row(
        matching,
        new_streamers=new_streamers,
        certified_streamers=certified_streamers,
        policy=policy,
    )


def _timo_settlement_from_cohort_row(
    matching: Optional[Dict[str, Any]],
    *,
    new_streamers: int,
    certified_streamers: int,
    policy: Dict[str, Any],
) -> Dict[str, Any]:
    non_certified = max(new_streamers - certified_streamers, 0)
    base = (
        non_certified * float(policy.get('non_certified_cpa_usd') or 0)
        + certified_streamers * float(policy.get('certified_cpa_usd') or 0)
    )
    bonus_7 = (matching or {}).get('settlement', {}).get('bonus_7d', {})
    bonus_10 = (matching or {}).get('settlement', {}).get('bonus_10d', {})
    bonus_7_amount = float(bonus_7.get('amount_usd') or 0) if bonus_7.get('status') == 'complete' else 0.0
    bonus_10_amount = float(bonus_10.get('amount_usd') or 0) if bonus_10.get('status') == 'complete' else 0.0
    complete = bonus_7.get('status') == 'complete' and bonus_10.get('status') == 'complete'
    return {
        'status': 'complete' if complete else 'pending',
        'base_usd': _round_money(base),
        'bonus_7d_usd': _round_money(bonus_7_amount) if bonus_7.get('status') == 'complete' else None,
        'bonus_10d_usd': _round_money(bonus_10_amount) if bonus_10.get('status') == 'complete' else None,
        'total_usd': _round_money(base + bonus_7_amount + bonus_10_amount),
    }


def _highest_tier_reward(tiers: List[Dict[str, Any]], income_units: float) -> tuple[int, float]:
    matched_level = 0
    reward_units = 0.0
    for tier in tiers:
        if income_units < float(tier.get('threshold_income_units') or 0):
            break
        matched_level = int(tier.get('tier_level') or 0)
        reward_units = float(tier.get('cumulative_reward_units') or 0)
    return matched_level, reward_units


def _timo_tiered_settlement(
    conn: sqlite3.Connection,
    *,
    country: str,
    guild_name: str,
    week_start: date,
    policy: Dict[str, Any],
    complete: bool,
) -> Dict[str, Any]:
    if not complete:
        return {'status': 'pending', 'total_usd': None}
    week_end = week_start + timedelta(days=6)
    host_rows = _dict_rows(
        conn,
        """
        SELECT streamer_id, SUM(total_income) AS income_units
        FROM streamer_analytics_streamer_daily_summary
        WHERE app_name='timo' AND country=? AND guild_name=? AND stat_date BETWEEN ? AND ?
        GROUP BY streamer_id
        """,
        (country, guild_name, week_start.isoformat(), week_end.isoformat()),
    )
    return _timo_tiered_settlement_from_rows(
        host_rows,
        policy=policy,
    )


def _timo_tiered_settlement_from_rows(
    host_rows: List[Dict[str, Any]],
    *,
    policy: Dict[str, Any],
) -> Dict[str, Any]:
    minimum = float(policy.get('guild_eligible_host_min_units') or 0)
    guild_effective_income = sum(
        float(row.get('income_units') or 0)
        for row in host_rows
        if float(row.get('income_units') or 0) >= minimum
    )
    tiers = policy.get('tiers') or {}
    guild_level, guild_reward_units = _highest_tier_reward(tiers.get('guild') or [], guild_effective_income)
    streamer_reward_units = sum(
        _highest_tier_reward(tiers.get('streamer') or [], float(row.get('income_units') or 0))[1]
        for row in host_rows
    )
    units_per_usd = float(policy.get('income_units_per_usd') or 0)
    return {
        'status': 'complete',
        'mode': 'timo_tiered_1v1',
        'base_usd': _round_money(guild_reward_units / units_per_usd) if units_per_usd else None,
        'bonus_7d_usd': 0.0,
        'bonus_10d_usd': 0.0,
        'total_usd': _round_money(guild_reward_units / units_per_usd) if units_per_usd else None,
        'guild_effective_income_units': round(guild_effective_income, 2),
        'guild_tier_level': guild_level,
        'guild_reward_units': round(guild_reward_units, 2),
        'streamer_reward_units': round(streamer_reward_units, 2),
        'eligible_streamers': sum(1 for row in host_rows if float(row.get('income_units') or 0) >= minimum),
    }


def _input_total(row: Optional[Dict[str, Any]]) -> Optional[float]:
    if not row:
        return None
    fields = (
        'ad_cost_usd', 'admin_cost_usd', 'customer_service_cost_usd',
        'media_buyer_cost_usd', 'activity_cost_usd',
    )
    values = [row.get(field) for field in fields]
    if any(value is None for value in values):
        return None
    return round(sum(float(value) for value in values), 2)


def _shared_cost_total(row: Optional[Dict[str, Any]]) -> Optional[float]:
    if not row or str(row.get('status') or '') != 'published':
        return None
    values = [row.get(field) for field in SHARED_COST_FIELDS]
    if any(value is None for value in values):
        return None
    return round(sum(float(value) for value in values), 2)


def _weekly_income_active_streamers(
    conn: sqlite3.Connection,
    *,
    app: str,
    country: str,
    guild_name: str,
    week_start: date,
) -> int:
    week_end = week_start + timedelta(days=6)
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM (
            SELECT streamer_id
            FROM streamer_analytics_streamer_daily_summary
            WHERE app_name = ? AND country = ? AND guild_name = ?
              AND stat_date BETWEEN ? AND ?
            GROUP BY streamer_id
            HAVING MAX(is_active) > 0
        )
        """,
        (app, country, guild_name, week_start.isoformat(), week_end.isoformat()),
    ).fetchone()
    return int((row or [0])[0] or 0)


def _portfolio_lifecycle(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    def period_for(row: Dict[str, Any], week: int) -> Dict[str, Any]:
        return next(
            (item for item in row.get('periods') or [] if int(item.get('week') or 0) == week),
            {},
        )

    latest_scope_week = next(
        (
            week
            for week in range(ROI_TRACKING_WEEKS, 0, -1)
            if any(period_for(row, week).get('lifecycle_status') == 'complete' for row in rows)
        ),
        None,
    )
    scope_rows = [
        row for row in rows
        if latest_scope_week is not None
        and period_for(row, latest_scope_week).get('lifecycle_status') == 'complete'
    ]
    covered_row_count = len(scope_rows)
    total_row_count = len(rows)
    periods: List[Dict[str, Any]] = []
    break_even_week: Optional[int] = None
    latest_complete: Optional[Dict[str, Any]] = None
    for week in range(1, ROI_TRACKING_WEEKS + 1):
        all_items = [period_for(row, week) for row in rows]
        data_complete = bool(rows) and all(item.get('status') == 'complete' for item in all_items)
        items = [period_for(row, week) for row in scope_rows]
        cost_complete = bool(items) and all(
            item.get('lifecycle_status') == 'complete' for item in items
        )
        if cost_complete:
            cumulative_income = sum(float(item.get('cumulative_income_usd') or 0) for item in items)
            incremental_income = sum(float(item.get('incremental_income_usd') or 0) for item in items)
            acquisition_cost = sum(float(item.get('acquisition_cost_usd') or 0) for item in items)
            weekly_shared = sum(float(item.get('allocated_shared_cost_usd') or 0) for item in items)
            cumulative_shared = sum(float(item.get('cumulative_shared_cost_usd') or 0) for item in items)
            lifecycle_cost = acquisition_cost + cumulative_shared
            roi = cumulative_income / lifecycle_cost if lifecycle_cost else None
            profit = cumulative_income - lifecycle_cost
            gap = max(lifecycle_cost - cumulative_income, 0)
            active_streamers = sum(int(item.get('active_streamers') or 0) for item in items)
        else:
            cumulative_income = incremental_income = acquisition_cost = weekly_shared = None
            cumulative_shared = lifecycle_cost = roi = profit = gap = None
            active_streamers = None
        lifecycle_status = (
            ('complete' if covered_row_count == total_row_count else 'partial')
            if cost_complete else
            ('cost_incomplete' if data_complete else 'incomplete')
        )
        period = {
            'week': week,
            'label': f'W{week}',
            'status': 'complete' if data_complete else 'incomplete',
            'lifecycle_status': lifecycle_status,
            'covered_row_count': covered_row_count if cost_complete else 0,
            'total_row_count': total_row_count,
            'active_streamers': active_streamers,
            'incremental_income_usd': _round_money(incremental_income),
            'cumulative_income_usd': _round_money(cumulative_income),
            'acquisition_cost_usd': _round_money(acquisition_cost),
            'allocated_shared_cost_usd': _round_money(weekly_shared),
            'cumulative_shared_cost_usd': _round_money(cumulative_shared),
            'lifecycle_cost_usd': _round_money(lifecycle_cost),
            'roi': round(roi, 6) if roi is not None else None,
            'profit_usd': _round_money(profit),
            'break_even_gap_usd': _round_money(gap),
        }
        periods.append(period)
        if cost_complete:
            latest_complete = period
            if break_even_week is None and roi is not None and roi >= 1:
                break_even_week = week
    if latest_complete:
        conclusion = (
            f'W{break_even_week} 达到 100% 回本'
            if break_even_week is not None
            else f"截至 W{latest_complete['week']} 尚未回本，差额 ${latest_complete['break_even_gap_usd']:.2f}"
        )
        status = 'ready'
    else:
        conclusion = '待发布完整周成本'
        status = 'cost_incomplete' if rows else 'empty'
    return {
        'status': status,
        'coverage_status': (
            'complete' if covered_row_count == total_row_count and total_row_count else
            ('partial' if covered_row_count else 'missing')
        ),
        'row_count': total_row_count,
        'covered_row_count': covered_row_count,
        'missing_row_count': max(total_row_count - covered_row_count, 0),
        'break_even_week': break_even_week,
        'latest_complete_week': latest_complete['week'] if latest_complete else None,
        'latest': latest_complete,
        'conclusion': conclusion,
        'periods': periods,
    }


def _calendar_profit_growth(
    conn: sqlite3.Connection,
    facts_conn: sqlite3.Connection,
    *,
    app: str,
    configured: List[Dict[str, str]],
    data_as_of: Optional[date],
    require_ready_snapshot: bool,
) -> Dict[str, Any]:
    """Aggregate every tracked cohort on calendar weeks with a fixed query count."""
    if not configured or not data_as_of:
        return {'status': 'insufficient', 'scope': 'all_tracked_cohorts_calendar_week', 'periods': []}
    latest_week = data_as_of - timedelta(days=data_as_of.weekday())
    if latest_week + timedelta(days=6) > data_as_of:
        latest_week -= timedelta(days=7)
    if latest_week < FIRST_ROI_WEEK_START:
        return {'status': 'insufficient', 'scope': 'all_tracked_cohorts_calendar_week', 'periods': []}

    range_end = latest_week + timedelta(days=6)
    scope_values = ','.join('(?, ?)' for _item in configured)
    scope_params = [
        value
        for item in configured
        for value in (str(item.get('country') or ''), str(item.get('guild_name') or ''))
    ]
    monday_sql = (
        "date({column}, '-' || "
        "((CAST(strftime('%w', {column}) AS INTEGER) + 6) % 7) || ' days')"
    )
    registered_week_sql = monday_sql.format(column='profile.registered_date')
    stat_week_sql = monday_sql.format(column='daily.stat_date')

    cohort_counts = {
        (
            str(row.get('country') or ''),
            str(row.get('guild_name') or ''),
            str(row.get('cohort_week') or ''),
        ): {
            'new_streamers': int(row.get('new_streamers') or 0),
            'certified_streamers': int(row.get('certified_streamers') or 0),
        }
        for row in _dict_rows(
            facts_conn,
            f"""
            WITH configured(country, guild_name) AS (VALUES {scope_values})
            SELECT profile.country, profile.guild_name,
                   {registered_week_sql} AS cohort_week,
                   COUNT(*) AS new_streamers,
                   SUM(CASE WHEN profile.is_real_person = 1 THEN 1 ELSE 0 END)
                       AS certified_streamers
            FROM configured AS scope
            JOIN streamer_analytics_profile_summary AS profile
              INDEXED BY idx_streamer_analytics_profile_scope
              ON profile.app_name = ?
             AND profile.country = scope.country
             AND profile.guild_name = scope.guild_name
             AND profile.registered_date BETWEEN ? AND ?
            GROUP BY profile.country, profile.guild_name, cohort_week
            """,
            (*scope_params, app, FIRST_ROI_WEEK_START.isoformat(), range_end.isoformat()),
        )
    }
    cohort_facts: Dict[tuple[str, str, str, str], Dict[str, Any]] = {}
    for row in _dict_rows(
        facts_conn,
        f"""
        WITH configured(country, guild_name) AS (VALUES {scope_values}),
        cohort AS (
            SELECT profile.guild_executor_key, profile.streamer_id,
                   profile.country, profile.guild_name,
                   {registered_week_sql} AS cohort_week,
                   profile.registered_date
            FROM configured AS scope
            JOIN streamer_analytics_profile_summary AS profile
              INDEXED BY idx_streamer_analytics_profile_scope
              ON profile.app_name = ?
             AND profile.country = scope.country
             AND profile.guild_name = scope.guild_name
             AND profile.registered_date BETWEEN ? AND ?
        )
        SELECT cohort.country, cohort.guild_name, cohort.cohort_week,
               {stat_week_sql} AS calendar_week,
               SUM(daily.total_income) AS income_units,
               COUNT(DISTINCT CASE WHEN daily.is_active = 1 THEN daily.streamer_id END)
                   AS active_streamers
        FROM cohort
        JOIN streamer_analytics_streamer_daily_summary AS daily
          INDEXED BY idx_streamer_analytics_streamer_daily_rank
          ON daily.app_name = ?
         AND daily.guild_executor_key = cohort.guild_executor_key
         AND daily.streamer_id = cohort.streamer_id
         AND daily.stat_date BETWEEN ? AND ?
         AND daily.stat_date >= cohort.registered_date
        GROUP BY cohort.country, cohort.guild_name, cohort.cohort_week, calendar_week
        """,
        (
            *scope_params, app, FIRST_ROI_WEEK_START.isoformat(), range_end.isoformat(),
            app, FIRST_ROI_WEEK_START.isoformat(), range_end.isoformat(),
        ),
    ):
        cohort_facts[(
            str(row.get('country') or ''),
            str(row.get('guild_name') or ''),
            str(row.get('cohort_week') or ''),
            str(row.get('calendar_week') or ''),
        )] = {
            'income_units': float(row.get('income_units') or 0),
            'active_streamers': int(row.get('active_streamers') or 0),
        }
    all_active = {
        (
            str(row.get('country') or ''),
            str(row.get('guild_name') or ''),
            str(row.get('calendar_week') or ''),
        ): int(row.get('active_streamers') or 0)
        for row in _dict_rows(
            facts_conn,
            f"""
            WITH configured(country, guild_name) AS (VALUES {scope_values})
            SELECT daily.country, daily.guild_name, {stat_week_sql} AS calendar_week,
                   COUNT(DISTINCT CASE WHEN daily.is_active = 1 THEN daily.streamer_id END)
                       AS active_streamers
            FROM configured AS scope
            JOIN streamer_analytics_streamer_daily_summary AS daily
              INDEXED BY idx_streamer_analytics_streamer_daily_scope
              ON daily.app_name = ?
             AND daily.country = scope.country
             AND daily.guild_name = scope.guild_name
             AND daily.stat_date BETWEEN ? AND ?
            GROUP BY daily.country, daily.guild_name, calendar_week
            """,
            (*scope_params, app, FIRST_ROI_WEEK_START.isoformat(), range_end.isoformat()),
        )
    }
    tiered_host_income: Dict[tuple[str, str, str], List[Dict[str, Any]]] = {}
    if app == 'timo':
        for row in _dict_rows(
            facts_conn,
            f"""
            WITH configured(country, guild_name) AS (VALUES {scope_values})
            SELECT daily.country, daily.guild_name, {stat_week_sql} AS calendar_week,
                   daily.streamer_id, SUM(daily.total_income) AS income_units
            FROM configured AS scope
            JOIN streamer_analytics_streamer_daily_summary AS daily
              INDEXED BY idx_streamer_analytics_streamer_daily_scope
              ON daily.app_name = 'timo'
             AND daily.country = scope.country
             AND daily.guild_name = scope.guild_name
             AND daily.stat_date BETWEEN ? AND ?
            GROUP BY daily.country, daily.guild_name, calendar_week, daily.streamer_id
            """,
            (*scope_params, FIRST_ROI_WEEK_START.isoformat(), range_end.isoformat()),
        ):
            tiered_host_income.setdefault((
                str(row.get('country') or ''),
                str(row.get('guild_name') or ''),
                str(row.get('calendar_week') or ''),
            ), []).append(row)

    input_rows = _dict_rows(
        conn,
        """
        SELECT * FROM streamer_roi_weekly_inputs
        WHERE app_name = ? AND week_start BETWEEN ? AND ?
        """,
        (app, FIRST_ROI_WEEK_START.isoformat(), latest_week.isoformat()),
    )
    inputs = {
        (str(row.get('country') or ''), str(row.get('guild_name') or ''), str(row.get('week_start') or '')): row
        for row in input_rows
    }
    policy_history = _policy_history_for_guilds(conn, app=app, through_week=latest_week)
    flat_timo_rows: Dict[tuple[str, str, str], Dict[str, Any]] = {}
    if app == 'timo' and any(
        str(policy.get('calculation_mode') or 'flat') != 'timo_tiered_1v1'
        for versions in policy_history.values() for policy in versions
    ):
        try:
            cohort_payload = build_timo_weekly_cohorts(
                facts_conn,
                start=FIRST_ROI_WEEK_START,
                end=range_end,
                allow_live_fallback=not require_ready_snapshot,
            )
        except StreamerAnalyticsCohortSnapshotUnavailable as exc:
            raise StreamerAnalyticsSnapshotUnavailable(
                'streamer_analytics_snapshot_unavailable'
            ) from exc
        flat_timo_rows = {
            (
                str(row.get('country') or ''),
                str(row.get('guild_name') or ''),
                str(row.get('week_start') or ''),
            ): row
            for row in cohort_payload.get('rows') or []
        }

    periods: List[Dict[str, Any]] = []
    week = FIRST_ROI_WEEK_START
    while week <= latest_week:
        week_key = week.isoformat()
        total_income = total_cost = tail_income = tail_cost = 0.0
        total_active = tail_active = 0
        missing: List[str] = []
        cohort_details: List[Dict[str, Any]] = []
        for guild in configured:
            country = str(guild.get('country') or '')
            guild_name = str(guild.get('guild_name') or '')
            input_row = inputs.get((country, guild_name, week_key))
            guild_cost = _input_total(input_row) if str((input_row or {}).get('status') or '') == 'published' else None
            shared_cost = _shared_cost_total(input_row)
            if guild_cost is None or shared_cost is None:
                missing.append(f'{guild_name}:cost')
            else:
                total_cost += guild_cost
            guild_all_active = all_active.get((country, guild_name, week_key), 0)
            total_active += guild_all_active
            guild_cohorts = [
                (key, facts) for key, facts in cohort_facts.items()
                if key[0] == country and key[1] == guild_name and key[3] == week_key
            ]
            guild_raw_income = sum(float(facts.get('income_units') or 0) for _key, facts in guild_cohorts)
            guild_tail_raw_income = 0.0
            guild_tail_active = 0
            guild_cps_income = 0.0
            for key, facts in guild_cohorts:
                cohort_week = _iso_date(key[2])
                if not cohort_week:
                    continue
                policy = _policy_from_history(
                    policy_history, country=country, guild_name=guild_name, cohort_week=cohort_week,
                )
                if not policy:
                    missing.append(f'{guild_name}:{key[2]}:policy')
                    continue
                tiered = app == 'timo' and str(policy.get('calculation_mode') or 'flat') == 'timo_tiered_1v1'
                units_per_usd = float(policy.get('income_units_per_usd') or 0)
                cps_income = 0.0 if tiered else (
                    float(facts.get('income_units') or 0) / units_per_usd * float(policy.get('cps_rate') or 0)
                    if units_per_usd else 0.0
                )
                age_week = ((week - cohort_week).days // 7) + 1
                active_streamers = int(facts.get('active_streamers') or 0)
                guild_cps_income += cps_income
                if age_week >= 5:
                    guild_tail_raw_income += float(facts.get('income_units') or 0)
                    guild_tail_active += active_streamers
                    tail_income += cps_income
                cohort_details.append({
                    'country': country,
                    'guild_name': guild_name,
                    'cohort_week': cohort_week.isoformat(),
                    'age_week': age_week,
                    'income_usd': _round_money(cps_income),
                    'active_streamers': active_streamers,
                })
            acquisition_policy = _policy_from_history(
                policy_history, country=country, guild_name=guild_name, cohort_week=week,
            )
            settlement: Dict[str, Any]
            counts = cohort_counts.get((country, guild_name, week_key), {})
            if not acquisition_policy:
                settlement = {'status': 'unconfigured', 'total_usd': None}
            elif app == 'timo' and str(acquisition_policy.get('calculation_mode') or 'flat') == 'timo_tiered_1v1':
                settlement = _timo_tiered_settlement_from_rows(
                    tiered_host_income.get((country, guild_name, week_key), []),
                    policy=acquisition_policy,
                )
            elif app == 'timo':
                settlement = _timo_settlement_from_cohort_row(
                    flat_timo_rows.get((country, guild_name, week_key)),
                    new_streamers=int(counts.get('new_streamers') or 0),
                    certified_streamers=int(counts.get('certified_streamers') or 0),
                    policy=acquisition_policy,
                )
            else:
                settlement = {
                    'status': 'complete',
                    'total_usd': _round_money(
                        int(counts.get('new_streamers') or 0)
                        * float(acquisition_policy.get('newcomer_cpa_usd') or 0)
                    ),
                }
            if settlement.get('status') != 'complete' or settlement.get('total_usd') is None:
                missing.append(f'{guild_name}:settlement')
                settlement_income = 0.0
            else:
                settlement_income = float(settlement.get('total_usd') or 0)
            total_income += guild_cps_income + settlement_income
            tiered_week = bool(
                acquisition_policy
                and app == 'timo'
                and str(acquisition_policy.get('calculation_mode') or 'flat') == 'timo_tiered_1v1'
            )
            if tiered_week and guild_raw_income > 0:
                tail_income += settlement_income * guild_tail_raw_income / guild_raw_income
            if shared_cost is not None:
                if guild_all_active > 0:
                    allocated_tail_cost = shared_cost * guild_tail_active / guild_all_active
                elif shared_cost == 0 and guild_tail_active == 0:
                    allocated_tail_cost = 0.0
                else:
                    missing.append(f'{guild_name}:active_cost_allocation')
                    allocated_tail_cost = 0.0
                tail_cost += allocated_tail_cost
            tail_active += guild_tail_active
        complete = not missing
        periods.append({
            'week_start': week_key,
            'week_end': (week + timedelta(days=6)).isoformat(),
            'status': 'complete' if complete else 'cost_incomplete',
            'income_usd': _round_money(total_income) if complete else None,
            'cost_usd': _round_money(total_cost) if complete else None,
            'net_profit_usd': _round_money(total_income - total_cost) if complete else None,
            'active_streamers': total_active,
            'w5_plus_income_usd': _round_money(tail_income) if complete else None,
            'w5_plus_cost_usd': _round_money(tail_cost) if complete else None,
            'w5_plus_profit_usd': _round_money(tail_income - tail_cost) if complete else None,
            'w5_plus_active_streamers': tail_active,
            'missing': sorted(set(missing)),
            'cohorts': cohort_details,
        })
        week += timedelta(days=7)

    recent_four = periods[-4:]
    rolling_ready = bool(recent_four) and all(
        period['status'] == 'complete' for period in recent_four
    )
    latest_complete = periods[-1] if periods and periods[-1]['status'] == 'complete' else None
    return {
        'status': 'ready' if latest_complete else 'insufficient',
        'scope': 'all_tracked_cohorts_calendar_week',
        'tracked_from': FIRST_ROI_WEEK_START.isoformat(),
        'rolling_4w_profit_usd': _round_money(sum(
            float(period.get('net_profit_usd') or 0) for period in recent_four
        )) if rolling_ready else None,
        'rolling_4w_weeks': [period['week_start'] for period in recent_four] if rolling_ready else [],
        'w5_plus_profit_usd': latest_complete.get('w5_plus_profit_usd') if latest_complete else None,
        'w5_plus_active_streamers': latest_complete.get('w5_plus_active_streamers') if latest_complete else None,
        'w5_plus_latest_week': latest_complete.get('week_start') if latest_complete else None,
        'periods': periods,
        'query_strategy': 'batch_calendar_aggregation',
        'tiered_tail_allocation': 'income_share',
    }


def _growth_decision_layer(
    rows: List[Dict[str, Any]],
    portfolio: Dict[str, Any],
    *,
    app: str,
) -> Dict[str, Any]:
    def row_period(row: Dict[str, Any], week: int) -> Dict[str, Any]:
        return next(
            (item for item in row.get('periods') or [] if int(item.get('week') or 0) == week),
            {},
        )

    complete_periods: List[Dict[str, Any]] = []
    for period in portfolio.get('periods') or []:
        if period.get('lifecycle_status') not in {'complete', 'partial'}:
            continue
        week = int(period.get('week') or 0)
        acquisition_cost = float(period.get('acquisition_cost_usd') or 0) if week == 1 else 0.0
        marginal_profit = (
            float(period.get('incremental_income_usd') or 0)
            - float(period.get('allocated_shared_cost_usd') or 0)
            - acquisition_cost
        )
        complete_periods.append({
            'week': week,
            'label': f'W{week}',
            'incremental_income_usd': _round_money(period.get('incremental_income_usd')),
            'marginal_cost_usd': _round_money(
                float(period.get('allocated_shared_cost_usd') or 0) + acquisition_cost
            ),
            'marginal_profit_usd': _round_money(marginal_profit),
            'active_streamers': period.get('active_streamers'),
            'is_long_tail': week >= 5,
        })

    recent_four = complete_periods[-4:]
    tail_periods = [item for item in complete_periods if item['is_long_tail']]
    latest_tail = tail_periods[-1] if tail_periods else None
    new_streamers = sum(int(row.get('new_streamers') or 0) for row in rows)
    certified_values = [row.get('certified_streamers') for row in rows]
    certified_streamers = (
        sum(int(value or 0) for value in certified_values)
        if any(value is not None for value in certified_values) else None
    )

    def cohort_metric(week: int, field: str) -> float:
        return sum(
            float(row_period(row, week).get(field) or 0)
            for row in rows
            if row_period(row, week).get('status') == 'complete'
        )

    w1_active = cohort_metric(1, 'active_streamers')
    w1_income = cohort_metric(1, 'income_usd')
    acquisition_rows = [
        row for row in rows
        if (row.get('lifecycle') or {}).get('acquisition_cost_usd') is not None
    ]
    acquisition_new = sum(int(row.get('new_streamers') or 0) for row in acquisition_rows)
    acquisition_cost = sum(
        float((row.get('lifecycle') or {}).get('acquisition_cost_usd') or 0)
        for row in acquisition_rows
    )

    retention: Dict[str, Optional[float]] = {}
    for week in (2, 4, 8):
        measurable = bool(new_streamers) and any(
            row_period(row, week).get('status') == 'complete' for row in rows
        )
        retention[f'w{week}'] = (
            cohort_metric(week, 'active_streamers') / new_streamers
            if measurable else None
        )

    latest_portfolio = portfolio.get('latest') or {}
    latest_active = int(latest_portfolio.get('active_streamers') or 0)
    latest_shared_cost = latest_portfolio.get('allocated_shared_cost_usd')
    active_cost = (
        float(latest_shared_cost) / latest_active
        if latest_shared_cost is not None and latest_active else None
    )
    scorecard = {
        'new_streamers': new_streamers,
        'unit_price_usd': _round_money(
            acquisition_cost / acquisition_new if acquisition_new else None
        ),
        'certification_rate': (
            certified_streamers / new_streamers
            if certified_streamers is not None and new_streamers else None
        ),
        'income_rate': w1_active / new_streamers if new_streamers else None,
        'w1_arpu_usd': _round_money(w1_income / new_streamers if new_streamers else None),
        'retention_w2': retention['w2'],
        'retention_w4': retention['w4'],
        'retention_w8': retention['w8'],
        'active_cost_per_streamer_usd': _round_money(active_cost),
        'reference': dict(TIMO_SCORECARD_REFERENCE) if app == 'timo' else None,
    }

    latest_growth = complete_periods[-1] if complete_periods else None
    reference_failures: List[str] = []
    if app == 'timo':
        if scorecard['unit_price_usd'] is not None and scorecard['unit_price_usd'] > TIMO_SCORECARD_REFERENCE['unit_price_max_usd']:
            reference_failures.append('主播单价高于参考线')
        if scorecard['certification_rate'] is not None and scorecard['certification_rate'] < TIMO_SCORECARD_REFERENCE['certification_rate_min']:
            reference_failures.append('认证率低于参考线')
        if scorecard['income_rate'] is not None and scorecard['income_rate'] < TIMO_SCORECARD_REFERENCE['income_rate_min']:
            reference_failures.append('收益率低于参考线')
        if scorecard['w1_arpu_usd'] is not None and scorecard['w1_arpu_usd'] < TIMO_SCORECARD_REFERENCE['w1_arpu_min_usd']:
            reference_failures.append('首周 ARPU 低于参考线')
        if scorecard['active_cost_per_streamer_usd'] is not None and scorecard['active_cost_per_streamer_usd'] > TIMO_SCORECARD_REFERENCE['active_cost_redline_usd']:
            reference_failures.append('单活跃成本超过红线')

    if portfolio.get('status') != 'ready' or portfolio.get('coverage_status') != 'complete' or not latest_growth:
        scale_status = 'insufficient'
        scale_reason = '成本覆盖或成熟周数据不完整'
    elif reference_failures:
        scale_status = 'hold'
        scale_reason = '；'.join(reference_failures)
    elif float((portfolio.get('latest') or {}).get('roi') or 0) < 1:
        scale_status = 'hold'
        scale_reason = '生命周期 ROI 尚未回本'
    elif float(latest_growth.get('marginal_profit_usd') or 0) <= 0:
        scale_status = 'hold'
        scale_reason = '最新成熟周边际利润未转正'
    elif int(latest_growth.get('week') or 0) < 4:
        scale_status = 'validate'
        scale_reason = '已回本，等待至少 W4 的持续性验证'
    else:
        scale_status = 'scale'
        scale_reason = '已回本且最新成熟周边际利润为正'
    scorecard['scale_status'] = scale_status
    scorecard['scale_reason'] = scale_reason

    actual_baseline = {
        'new_streamers': new_streamers,
        'unit_price_usd': scorecard['unit_price_usd'],
        'w1_arpu_usd': scorecard['w1_arpu_usd'],
        'retention_w2': scorecard['retention_w2'],
        'active_cost_per_streamer_usd': scorecard['active_cost_per_streamer_usd'],
    }
    return {
        'status': 'ready' if complete_periods else 'insufficient',
        'scope': 'selected_registration_cohort',
        'rolling_4w_profit_usd': _round_money(sum(
            float(item.get('marginal_profit_usd') or 0) for item in recent_four
        )) if recent_four else None,
        'rolling_4w_weeks': [item['week'] for item in recent_four],
        'w5_plus_profit_usd': _round_money(sum(
            float(item.get('marginal_profit_usd') or 0) for item in tail_periods
        )) if tail_periods else None,
        'w5_plus_active_streamers': latest_tail.get('active_streamers') if latest_tail else None,
        'w5_plus_latest_week': latest_tail.get('week') if latest_tail else None,
        'periods': complete_periods,
        'scorecard': scorecard,
        'forecast': {
            'actual_baseline': actual_baseline,
            'standard_baseline': dict(TIMO_FORECAST_STANDARD) if app == 'timo' else None,
            'is_prediction': True,
        },
    }


def build_streamer_weekly_roi_payload(
    conn: sqlite3.Connection,
    *,
    app_name: Any,
    week_start: Any = None,
    country: str = '',
    guild_name: str = '',
    today: Optional[date] = None,
    analytics_conn: Optional[sqlite3.Connection] = None,
    ensure_schema: bool = True,
    require_ready_snapshot: bool = False,
) -> Dict[str, Any]:
    if ensure_schema:
        ensure_streamer_roi_tables(conn)
    facts_conn = analytics_conn or conn
    app = normalize_streamer_app(app_name)
    if require_ready_snapshot:
        require_streamer_analytics_snapshot_ready(facts_conn, app_name=app)
    requested_week = _iso_date(week_start) or _latest_target_week(today)
    if requested_week.weekday() != 0:
        raise ValueError('streamer_roi_week_must_start_monday')
    if requested_week < FIRST_ROI_WEEK_START:
        raise ValueError('streamer_roi_history_not_enabled')
    week_end = requested_week + timedelta(days=6)
    available_from = week_end + timedelta(days=2)
    current = today or _today_bj()
    editable = current >= available_from
    configured = _configured_guilds(
        facts_conn,
        app=app,
        country=country,
        guild_name=guild_name,
    )
    inputs = {
        (str(row['country']), str(row['guild_name'])): row
        for row in _dict_rows(
            conn,
            "SELECT * FROM streamer_roi_weekly_inputs WHERE app_name = ? AND week_start = ?",
            (app, requested_week.isoformat()),
        )
    }
    lifecycle_input_rows = _dict_rows(
        conn,
        """
        SELECT * FROM streamer_roi_weekly_inputs
        WHERE app_name = ? AND week_start BETWEEN ? AND ?
        """,
        (
            app,
            requested_week.isoformat(),
            (requested_week + timedelta(days=(ROI_TRACKING_WEEKS - 1) * 7)).isoformat(),
        ),
    )
    lifecycle_inputs = {
        (str(row['country']), str(row['guild_name']), str(row['week_start'])): row
        for row in lifecycle_input_rows
    }
    policies = _policies_for_guilds(
        conn,
        app=app,
        week_start=requested_week,
    )
    fact_bundle = _weekly_roi_fact_bundle(
        facts_conn,
        app=app,
        week_start=requested_week,
        configured=configured,
    )
    timo_cohort_rows: Dict[tuple[str, str], Dict[str, Any]] = {}
    if app == 'timo' and any(
        str(policy.get('calculation_mode') or 'flat') != 'timo_tiered_1v1'
        for policy in policies.values()
    ):
        try:
            timo_cohort_payload = build_timo_weekly_cohorts(
                facts_conn,
                start=requested_week,
                end=week_end,
                allow_live_fallback=not require_ready_snapshot,
            )
        except StreamerAnalyticsCohortSnapshotUnavailable as exc:
            raise StreamerAnalyticsSnapshotUnavailable(
                'streamer_analytics_snapshot_unavailable'
            ) from exc
        timo_cohort_rows = {
            (str(row.get('country') or ''), str(row.get('guild_name') or '')): row
            for row in timo_cohort_payload.get('rows') or []
            if str(row.get('week_start') or '') == requested_week.isoformat()
        }
    rows: List[Dict[str, Any]] = []
    max_data_as_of: Optional[date] = None
    for guild in configured:
        guild_country = guild['country']
        name = guild['guild_name']
        policy = policies.get((guild_country, name))
        units_per_usd = float(policy['income_units_per_usd']) if policy else None
        new_count, certified, periods, data_as_of = _cohort_periods_from_bundle(
            fact_bundle,
            country=guild_country,
            guild_name=name,
            week_start=requested_week,
            units_per_usd=units_per_usd,
        )
        if data_as_of and (not max_data_as_of or data_as_of > max_data_as_of):
            max_data_as_of = data_as_of
        whole_week_complete = bool(data_as_of and week_end <= data_as_of)
        whole_income_units = (
            float(fact_bundle['whole_week_income'].get((guild_country, name), 0))
            if whole_week_complete else None
        )
        whole_income_usd = (
            whole_income_units / units_per_usd
            if whole_income_units is not None and units_per_usd else None
        )
        if not policy:
            settlement = {'status': 'unconfigured', 'total_usd': None}
        elif app == 'timo' and str(policy.get('calculation_mode') or 'flat') == 'timo_tiered_1v1':
            settlement = _timo_tiered_settlement_from_rows(
                fact_bundle['tiered_host_income'].get((guild_country, name), []),
                policy=policy,
            ) if whole_week_complete else {'status': 'pending', 'total_usd': None}
        elif app == 'timo':
            settlement = _timo_settlement_from_cohort_row(
                timo_cohort_rows.get((guild_country, name)),
                new_streamers=new_count,
                certified_streamers=certified,
                policy=policy,
            )
        else:
            settlement = {
                'status': 'complete',
                'base_usd': _round_money(new_count * float(policy.get('newcomer_cpa_usd') or 0)),
                'bonus_7d_usd': 0.0,
                'bonus_10d_usd': 0.0,
                'total_usd': _round_money(new_count * float(policy.get('newcomer_cpa_usd') or 0)),
            }
        input_row = inputs.get((guild_country, name))
        total_cost = _input_total(input_row)
        input_status = str((input_row or {}).get('status') or 'missing')
        tiered_policy = bool(
            policy
            and app == 'timo'
            and str(policy.get('calculation_mode') or 'flat') == 'timo_tiered_1v1'
        )
        cps_rate = None if tiered_policy else (float(policy.get('cps_rate') or 0) if policy else None)
        platform_settlement = settlement.get('total_usd')
        if tiered_policy:
            overall_income = float(platform_settlement) if platform_settlement is not None else None
        else:
            overall_income = (
                float(platform_settlement) + float(whole_income_usd) * float(cps_rate)
                if platform_settlement is not None and whole_income_usd is not None and cps_rate is not None else None
            )
        overall_roi = overall_income / total_cost if overall_income is not None and total_cost and input_status == 'published' else None
        cumulative_new_income = 0.0
        previous_cumulative_income = 0.0
        cumulative_shared_cost = 0.0
        lifecycle_cost_available = bool(
            input_status == 'published'
            and input_row
            and input_row.get('ad_cost_usd') is not None
        )
        acquisition_cost = float(input_row.get('ad_cost_usd') or 0) if lifecycle_cost_available else None
        break_even_week: Optional[int] = None
        roi_periods: List[Dict[str, Any]] = []
        for period in periods:
            if period['status'] != 'complete' or period['income_usd'] is None:
                roi_periods.append({
                    **period,
                    'cumulative_income_usd': None,
                    'incremental_income_usd': None,
                    'roi': None,
                    'profit_usd': None,
                    'lifecycle_status': 'incomplete',
                    'acquisition_cost_usd': _round_money(acquisition_cost),
                    'shared_cost_pool_usd': None,
                    'all_active_streamers': None,
                    'shared_cost_per_active_usd': None,
                    'allocated_shared_cost_usd': None,
                    'cumulative_shared_cost_usd': None,
                    'lifecycle_cost_usd': None,
                    'lifecycle_roi': None,
                    'lifecycle_profit_usd': None,
                    'break_even_gap_usd': None,
                })
                continue
            cumulative_new_income += float(period['income_usd'])
            if tiered_policy:
                cumulative_income = float(platform_settlement) if platform_settlement is not None else None
            else:
                cumulative_income = (
                    float(platform_settlement) + cumulative_new_income * float(cps_rate)
                    if platform_settlement is not None and cps_rate is not None else None
                )
            roi = cumulative_income / total_cost if cumulative_income is not None and total_cost and input_status == 'published' else None
            incremental_income = (
                cumulative_income - previous_cumulative_income
                if cumulative_income is not None else None
            )
            if cumulative_income is not None:
                previous_cumulative_income = cumulative_income
            period_start = _iso_date(period.get('date_from')) or requested_week
            lifecycle_input = lifecycle_inputs.get((guild_country, name, period_start.isoformat()))
            shared_cost_pool = _shared_cost_total(lifecycle_input)
            all_active_streamers = int(
                fact_bundle['all_active_streamers'].get(
                    (guild_country, name, int(period.get('week') or 1) - 1),
                    0,
                )
            )
            cohort_active = int(period.get('active_streamers') or 0)
            allocated_shared_cost: Optional[float] = None
            if lifecycle_cost_available and shared_cost_pool is not None:
                if all_active_streamers > 0:
                    allocated_shared_cost = shared_cost_pool * cohort_active / all_active_streamers
                elif shared_cost_pool == 0 and cohort_active == 0:
                    allocated_shared_cost = 0.0
                else:
                    lifecycle_cost_available = False
            else:
                lifecycle_cost_available = False
            if lifecycle_cost_available and allocated_shared_cost is not None and acquisition_cost is not None:
                cumulative_shared_cost += allocated_shared_cost
                lifecycle_cost = acquisition_cost + cumulative_shared_cost
                lifecycle_roi = cumulative_income / lifecycle_cost if cumulative_income is not None and lifecycle_cost else None
                lifecycle_profit = cumulative_income - lifecycle_cost if cumulative_income is not None else None
                break_even_gap = max(lifecycle_cost - cumulative_income, 0) if cumulative_income is not None else None
                lifecycle_status = 'complete'
                if break_even_week is None and lifecycle_roi is not None and lifecycle_roi >= 1:
                    break_even_week = int(period.get('week') or 0)
            else:
                lifecycle_cost = lifecycle_roi = lifecycle_profit = break_even_gap = None
                lifecycle_status = 'cost_incomplete'
            roi_periods.append({
                **period,
                'cumulative_income_usd': _round_money(cumulative_income),
                'incremental_income_usd': _round_money(incremental_income),
                'roi': round(roi, 6) if roi is not None else None,
                'profit_usd': _round_money(cumulative_income - total_cost)
                if cumulative_income is not None and total_cost is not None else None,
                'lifecycle_status': lifecycle_status,
                'acquisition_cost_usd': _round_money(acquisition_cost),
                'shared_cost_pool_usd': _round_money(shared_cost_pool),
                'all_active_streamers': all_active_streamers,
                'shared_cost_per_active_usd': _round_money(
                    shared_cost_pool / all_active_streamers
                    if shared_cost_pool is not None and all_active_streamers else None
                ),
                'allocated_shared_cost_usd': _round_money(allocated_shared_cost),
                'cumulative_shared_cost_usd': _round_money(cumulative_shared_cost)
                if lifecycle_status == 'complete' else None,
                'lifecycle_cost_usd': _round_money(lifecycle_cost),
                'lifecycle_roi': round(lifecycle_roi, 6) if lifecycle_roi is not None else None,
                'lifecycle_profit_usd': _round_money(lifecycle_profit),
                'break_even_gap_usd': _round_money(break_even_gap),
            })
        row_state = 'policy_missing' if not policy else input_status
        if not editable and input_status == 'missing':
            row_state = 'not_open'
        elif input_status == 'published' and settlement.get('status') != 'complete':
            row_state = 'pending_settlement'
        elif input_status == 'published' and any(item['status'] == 'complete' for item in periods):
            row_state = 'complete' if settlement.get('status') == 'complete' else 'pending_settlement'
        rows.append({
            'country': guild_country,
            'guild_name': name,
            'state': row_state,
            'input_status': input_status,
            'revision': int((input_row or {}).get('revision') or 0),
            'policy': {
                'configured': bool(policy),
                'income_units_per_usd': units_per_usd,
                'cps_rate': cps_rate,
                'newcomer_cpa_usd': float(policy.get('newcomer_cpa_usd') or 0) if policy else None,
                'includes_subsidy': bool(policy and float(policy.get('cps_rate') or 0) > 1),
                'calculation_mode': str(policy.get('calculation_mode') or 'flat') if policy else None,
                'settlement_basis': 'weekly_tier' if tiered_policy else 'fixed_rate',
                'effective_from': str(policy.get('effective_from') or '') if policy else None,
            },
            'input': {
                field: (input_row or {}).get(field)
                for field in (
                    'ad_cost_usd', 'admin_cost_usd', 'customer_service_cost_usd',
                    'media_buyer_cost_usd', 'activity_cost_usd',
                )
            },
            'correction_reason': str((input_row or {}).get('correction_reason') or ''),
            'updated_by': str((input_row or {}).get('updated_by') or ''),
            'updated_at': (input_row or {}).get('updated_at'),
            'new_streamers': new_count,
            'certified_streamers': certified if app == 'timo' else None,
            'whole_week_income_units': round(whole_income_units, 2)
            if whole_income_units is not None else None,
            'whole_week_income_usd': _round_money(whole_income_usd),
            'platform_settlement': settlement,
            'total_cost_usd': total_cost,
            'cost_per_new_streamer_usd': _round_money(total_cost / new_count) if total_cost is not None and new_count else None,
            'overall_income_usd': _round_money(overall_income),
            'overall_roi': round(overall_roi, 6) if overall_roi is not None else None,
            'overall_profit_usd': _round_money(overall_income - total_cost)
            if overall_income is not None and total_cost is not None else None,
            'lifecycle': {
                'status': next(
                    (
                        item['lifecycle_status']
                        for item in reversed(roi_periods)
                        if item.get('status') == 'complete'
                    ),
                    'incomplete',
                ),
                'acquisition_cost_usd': _round_money(acquisition_cost),
                'break_even_week': break_even_week,
            },
            'periods': roi_periods,
        })
    portfolio = _portfolio_lifecycle(rows)
    growth = _growth_decision_layer(rows, portfolio, app=app)
    growth_configured = _configured_guilds(facts_conn, app=app)
    calendar_growth = _calendar_profit_growth(
        conn,
        facts_conn,
        app=app,
        configured=growth_configured,
        data_as_of=fact_bundle.get('data_as_of'),
        require_ready_snapshot=require_ready_snapshot,
    )
    growth.update(calendar_growth)
    scorecard = growth.get('scorecard') or {}
    complete_calendar_periods = [
        period for period in calendar_growth.get('periods') or []
        if period.get('status') == 'complete'
    ]
    if calendar_growth.get('status') != 'ready':
        scorecard['scale_status'] = 'insufficient'
        scorecard['scale_reason'] = '跨 Cohort 自然周成本或结算未完整'
    elif complete_calendar_periods and float(complete_calendar_periods[-1].get('net_profit_usd') or 0) <= 0:
        scorecard['scale_status'] = 'hold'
        scorecard['scale_reason'] = '最新完整自然周净利润未转正'
    return {
        'available': app in {'timo', 'linky'},
        'app': app,
        'week_start': requested_week.isoformat(),
        'week_end': week_end.isoformat(),
        'available_from': available_from.isoformat(),
        'editable': editable and app in {'timo', 'linky'},
        'data_as_of': max_data_as_of.isoformat() if max_data_as_of else None,
        'rows': rows,
        'portfolio': portfolio,
        'growth': growth,
        'definitions': {
            'cost_scope': '广告费计入注册周获客成本；其余成本按每周收益活跃主播占比分摊并逐周累计。',
            'cps_rate': '固定比例政策的公会CPS结算比例；Timo 1v1梯度政策不使用该字段。',
            'cohort': 'W1为注册所在A周，W2为A+1周，依次类推。',
            'growth_scope': '利润增长按全部已启用公会、全部可追踪 Cohort 的自然周汇总；最近4周只取成本与结算均完整的自然周。',
            'tail_profit': 'W5+ 为最新完整自然周中注册满5周 Cohort 的收入，扣除其当周共享成本份额，不重复扣注册周获客成本。梯度结算按 Cohort 收入占比分配。',
            'forecast': '绝对参数演算沿用当前实值生命周期曲线，只改变主播单价、首周ARPU、W2留存和单活跃成本；结果为预测，不写入真实收益、成本或结算数据。',
        },
    }


INPUT_FIELDS = (
    'ad_cost_usd', 'admin_cost_usd', 'customer_service_cost_usd',
    'media_buyer_cost_usd', 'activity_cost_usd',
)


def save_streamer_weekly_roi_inputs(
    conn: sqlite3.Connection,
    *,
    app_name: Any,
    week_start: Any,
    rows: List[Dict[str, Any]],
    status: str,
    actor: str,
    today: Optional[date] = None,
    analytics_conn: Optional[sqlite3.Connection] = None,
    require_ready_snapshot: bool = False,
) -> Dict[str, Any]:
    facts_conn = analytics_conn or conn
    app = normalize_streamer_app(app_name)
    if require_ready_snapshot:
        require_streamer_analytics_snapshot_ready(facts_conn, app_name=app)
    ensure_streamer_roi_tables(conn)
    target = _iso_date(week_start)
    if not target or target.weekday() != 0:
        raise ValueError('streamer_roi_week_must_start_monday')
    if target < FIRST_ROI_WEEK_START:
        raise ValueError('streamer_roi_history_not_enabled')
    available_from = target + timedelta(days=8)
    if (today or _today_bj()) < available_from:
        raise ValueError('streamer_roi_week_not_open')
    normalized_status = str(status or '').strip().lower()
    if normalized_status not in {'draft', 'published'}:
        raise ValueError('streamer_roi_invalid_status')
    configured = {
        (item['country'], item['guild_name'])
        for item in _configured_guilds(facts_conn, app=app)
    }
    now = _now_iso()
    saved = 0
    for incoming in rows:
        country = str(incoming.get('country') or '').strip()
        guild_name = str(incoming.get('guild_name') or '').strip()
        if (country, guild_name) not in configured:
            raise ValueError('streamer_roi_guild_not_configured')
        values: Dict[str, Optional[float]] = {}
        for field in INPUT_FIELDS:
            raw = incoming.get(field)
            if raw in (None, ''):
                values[field] = None
                continue
            value = float(raw)
            if not math.isfinite(value) or value < 0:
                raise ValueError('streamer_roi_cost_must_be_non_negative')
            values[field] = round(value, 2)
        if normalized_status == 'published' and any(values[field] is None for field in INPUT_FIELDS):
            raise ValueError('streamer_roi_publish_requires_all_costs')
        before_rows = _dict_rows(
            conn,
            "SELECT * FROM streamer_roi_weekly_inputs WHERE app_name=? AND country=? AND guild_name=? AND week_start=?",
            (app, country, guild_name, target.isoformat()),
        )
        before = before_rows[0] if before_rows else None
        changed = not before or any(before.get(field) != values[field] for field in INPUT_FIELDS) or str(before.get('status')) != normalized_status
        if not changed:
            continue
        correction_reason = str(incoming.get('correction_reason') or '').strip()
        if before and str(before.get('status')) == 'published' and not correction_reason:
            raise ValueError('streamer_roi_correction_reason_required')
        revision = int((before or {}).get('revision') or 0) + 1
        created_by = str((before or {}).get('created_by') or actor)
        created_at = str((before or {}).get('created_at') or now)
        published_by = actor if normalized_status == 'published' else str((before or {}).get('published_by') or '')
        published_at = now if normalized_status == 'published' else (before or {}).get('published_at')
        after = {
            'app_name': app, 'country': country, 'guild_name': guild_name,
            'week_start': target.isoformat(), **values, 'status': normalized_status,
            'revision': revision, 'correction_reason': correction_reason,
            'created_by': created_by, 'created_at': created_at,
            'updated_by': actor, 'updated_at': now,
            'published_by': published_by, 'published_at': published_at,
        }
        conn.execute(
            """
            INSERT INTO streamer_roi_weekly_inputs (
                app_name,country,guild_name,week_start,ad_cost_usd,admin_cost_usd,
                customer_service_cost_usd,media_buyer_cost_usd,activity_cost_usd,
                status,revision,correction_reason,created_by,created_at,updated_by,
                updated_at,published_by,published_at
            ) VALUES (
                :app_name,:country,:guild_name,:week_start,:ad_cost_usd,:admin_cost_usd,
                :customer_service_cost_usd,:media_buyer_cost_usd,:activity_cost_usd,
                :status,:revision,:correction_reason,:created_by,:created_at,:updated_by,
                :updated_at,:published_by,:published_at
            )
            ON CONFLICT(app_name,country,guild_name,week_start) DO UPDATE SET
                ad_cost_usd=excluded.ad_cost_usd,admin_cost_usd=excluded.admin_cost_usd,
                customer_service_cost_usd=excluded.customer_service_cost_usd,
                media_buyer_cost_usd=excluded.media_buyer_cost_usd,
                activity_cost_usd=excluded.activity_cost_usd,status=excluded.status,
                revision=excluded.revision,correction_reason=excluded.correction_reason,
                updated_by=excluded.updated_by,updated_at=excluded.updated_at,
                published_by=excluded.published_by,published_at=excluded.published_at
            """,
            after,
        )
        action = 'corrected' if before and str(before.get('status')) == 'published' else normalized_status
        conn.execute(
            """
            INSERT INTO streamer_roi_weekly_input_audit (
                app_name,country,guild_name,week_start,action,revision,before_json,
                after_json,reason,actor,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                app, country, guild_name, target.isoformat(), action, revision,
                json.dumps(before or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(after, ensure_ascii=False, sort_keys=True),
                correction_reason, actor, now,
            ),
        )
        saved += 1
    conn.commit()
    payload = build_streamer_weekly_roi_payload(
        conn,
        app_name=app,
        week_start=target,
        today=today,
        analytics_conn=facts_conn,
        ensure_schema=False,
        require_ready_snapshot=require_ready_snapshot,
    )
    payload['saved_count'] = saved
    return payload
