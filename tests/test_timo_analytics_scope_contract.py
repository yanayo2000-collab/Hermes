import hashlib
import sqlite3
from datetime import date, timedelta

from app.streamer_analytics import (
    _build_timo_weekly_cohorts_live,
    _timo_materialization_scope_snapshot,
)


def _checksum(rows):
    digest = hashlib.sha256()
    for timo_id, row_hash in sorted(rows):
        digest.update(timo_id.encode('utf-8'))
        digest.update(b'\x1f')
        digest.update(row_hash.encode('ascii'))
        digest.update(b'\n')
    return digest.hexdigest()


def test_partial_day_consumes_complete_guilds_and_cohort_uses_same_snapshot():
    with sqlite3.connect(':memory:') as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE guild_executors (
                guild_name TEXT, app_name TEXT, country TEXT
            );
            CREATE TABLE timo_external_streamers (
                guild_executor_key TEXT, guild_name TEXT, country TEXT,
                timo_id TEXT, nickname TEXT, registered_at_bj TEXT,
                joined_guild_at_bj TEXT, last_active_at_bj TEXT,
                is_real_person INTEGER, source_payload TEXT, updated_at TEXT
            );
            CREATE TABLE timo_external_revenue_daily (
                guild_executor_key TEXT, guild_name TEXT, stat_date_bj TEXT,
                timo_id TEXT, total_income REAL, provisional INTEGER,
                revision_version INTEGER, last_sync_id TEXT, row_hash TEXT
            );
            CREATE TABLE timo_sync_watermark (
                guild_executor_key TEXT, guild_name TEXT, country TEXT,
                stat_date_bj TEXT, checksum TEXT, last_success_sync_id TEXT,
                last_success_time TEXT, row_count INTEGER, total_income REAL,
                data_status TEXT, revision_version INTEGER
            );
            CREATE TABLE timo_external_revenue_weekly (
                guild_executor_key TEXT, guild_name TEXT, country TEXT,
                week_start_bj TEXT, timo_id TEXT, total_income REAL
            );
            CREATE TABLE timo_external_revenue_weekly_coverage (
                guild_executor_key TEXT, guild_name TEXT, country TEXT,
                week_start_bj TEXT, status TEXT
            );
            INSERT INTO guild_executors VALUES
                ('Brazil Guild','timo','Brazil'),
                ('Mexico Guild','timo','Mexico');
            INSERT INTO timo_external_streamers VALUES
                ('br','Brazil Guild','Brazil','br-new','BR','2026-07-13','',
                 '2026-07-19',1,'{"gender":2}','now'),
                ('mx','Mexico Guild','Mexico','mx-new','MX','2026-07-13','',
                 '2026-07-19',1,'{"gender":2}','now');
            """
        )
        for offset in range(7):
            stat_date = (date(2026, 7, 13) + timedelta(days=offset)).isoformat()
            for guild_key, guild_name, country, timo_id, income, status in (
                ('br', 'Brazil Guild', 'Brazil', 'br-new', 100.0, 'complete'),
                ('mx', 'Mexico Guild', 'Mexico', 'mx-new', 300.0, 'provisional'),
            ):
                row_hash = hashlib.sha256(f'{guild_key}:{stat_date}'.encode()).hexdigest()
                sync_id = f'{guild_key}:{stat_date}:v1'
                conn.execute(
                    "INSERT INTO timo_external_revenue_daily VALUES (?,?,?,?,?,0,1,?,?)",
                    (guild_key, guild_name, stat_date, timo_id, income, sync_id, row_hash),
                )
                conn.execute(
                    "INSERT INTO timo_sync_watermark VALUES (?,?,?,?,?,?,?,1,?,?,?)",
                    (
                        guild_key, guild_name, country, stat_date,
                        _checksum([(timo_id, row_hash)]), sync_id, 'now', income,
                        status, 1,
                    ),
                )

        facts, covered = _timo_materialization_scope_snapshot(conn)
        profiles = [dict(row) for row in conn.execute(
            "SELECT guild_executor_key,guild_name,country,timo_id,registered_at_bj,"
            "is_real_person,source_payload FROM timo_external_streamers"
        )]
        cohort = _build_timo_weekly_cohorts_live(
            conn,
            start=date(2026, 7, 13),
            end=date(2026, 7, 19),
            _profiles=profiles,
            _facts=facts,
            _covered_scopes=covered,
            _data_as_of=date(2026, 7, 19),
        )
        assert {row['country'] for row in facts} == {'Brazil'}
        rows_by_country = {row['country']: row for row in cohort['rows']}
        assert rows_by_country['Brazil']['periods'][0]['income_diamonds'] == 700.0
        assert rows_by_country['Mexico']['periods'][0]['status'] == 'incomplete'
        assert rows_by_country['Mexico']['periods'][0]['income_diamonds'] is None

        conn.execute(
            "UPDATE timo_sync_watermark SET data_status='complete' WHERE guild_executor_key='mx'"
        )
        recovered_facts, recovered_covered = _timo_materialization_scope_snapshot(conn)
        assert {row['country'] for row in recovered_facts} == {'Brazil', 'Mexico'}
        assert len(recovered_covered) == 14


def test_guild_id_alias_is_canonicalized_and_normal_official_scope_wins_conflict():
    canonical = 'timo:cms_guild_sid:lvmy210446316420ie3d'
    official_alias = 'timo:cms_guild_sid:22000408'
    with sqlite3.connect(':memory:') as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE guild_executors (
                guild_name TEXT, app_name TEXT, country TEXT, enabled INTEGER,
                cms_guild_id TEXT, cms_guild_sid TEXT
            );
            CREATE TABLE timo_external_streamers (
                guild_executor_key TEXT, guild_name TEXT, country TEXT,
                timo_id TEXT, nickname TEXT, registered_at_bj TEXT,
                joined_guild_at_bj TEXT, last_active_at_bj TEXT,
                is_real_person INTEGER, source_payload TEXT, updated_at TEXT
            );
            CREATE TABLE timo_external_revenue_daily (
                guild_executor_key TEXT, guild_name TEXT, stat_date_bj TEXT,
                timo_id TEXT, total_income REAL, provisional INTEGER,
                revision_version INTEGER, last_sync_id TEXT, row_hash TEXT
            );
            CREATE TABLE timo_sync_watermark (
                guild_executor_key TEXT, guild_name TEXT, country TEXT,
                stat_date_bj TEXT, checksum TEXT, last_success_sync_id TEXT,
                last_success_time TEXT, row_count INTEGER, total_income REAL,
                data_status TEXT, revision_version INTEGER
            );
            CREATE TABLE timo_external_revenue_weekly (
                guild_executor_key TEXT, guild_name TEXT, country TEXT,
                week_start_bj TEXT, timo_id TEXT, total_income REAL
            );
            CREATE TABLE timo_external_revenue_weekly_coverage (
                guild_executor_key TEXT, guild_name TEXT, country TEXT,
                week_start_bj TEXT, status TEXT
            );
            INSERT INTO guild_executors VALUES (
                'Agency MX somente','timo','Mexico',1,
                '22000408','lvmy210446316420ie3d'
            );
            INSERT INTO timo_external_streamers VALUES (
                'timo:cms_guild_sid:lvmy210446316420ie3d',
                'Agency MX somente','Mexico','mx-new','MX',
                '2026-08-17','','2026-08-23',1,'{"gender":2}','now'
            );
            """
        )
        for offset in range(7):
            stat_date = (date(2026, 8, 17) + timedelta(days=offset)).isoformat()
            raw_key = canonical if offset < 3 else official_alias
            sync_id = f'timo_manual_official_{stat_date.replace("-", "")}_22000408_source'
            row_hash = hashlib.sha256(f'official:{stat_date}'.encode()).hexdigest()
            checksum = _checksum([('mx-new', row_hash)])
            conn.execute(
                "INSERT INTO timo_external_revenue_daily VALUES (?,?,?,?,?,0,1,?,?)",
                (raw_key, 'Agency MX somente', stat_date, 'mx-new', 100.0, sync_id, row_hash),
            )
            conn.execute(
                "INSERT INTO timo_sync_watermark VALUES (?,?,?,?,?,?,?,1,100,?,1)",
                (raw_key, 'Agency MX somente', 'Mexico', stat_date, checksum, sync_id, 'now', 'complete'),
            )
            if offset == 6:
                natural_sync = 'timo_revenue_sync_conflicting'
                natural_hash = hashlib.sha256(b'natural-conflict').hexdigest()
                conn.execute(
                    "INSERT INTO timo_external_revenue_daily VALUES (?,?,?,?,?,0,1,?,?)",
                    (canonical, 'Agency MX somente', stat_date, 'mx-new', 9.0, natural_sync, natural_hash),
                )
                conn.execute(
                    "INSERT INTO timo_sync_watermark VALUES (?,?,?,?,?,?,?,1,9,?,1)",
                    (
                        canonical, 'Agency MX somente', 'Mexico', stat_date,
                        _checksum([('mx-new', natural_hash)]), natural_sync, 'now', 'complete',
                    ),
                )

        facts, covered = _timo_materialization_scope_snapshot(conn)
        assert len(facts) == 7
        assert len(covered) == 7
        assert {row['guild_executor_key'] for row in facts} == {canonical}
        assert sum(float(row['total_income']) for row in facts) == 609.0
        assert sum(int(row['is_new']) for row in facts) == 1

        profiles = [dict(row) for row in conn.execute(
            "SELECT guild_executor_key,guild_name,country,timo_id,registered_at_bj,"
            "is_real_person,source_payload FROM timo_external_streamers"
        )]
        cohort = _build_timo_weekly_cohorts_live(
            conn,
            start=date(2026, 8, 17),
            end=date(2026, 8, 23),
            _profiles=profiles,
            _facts=facts,
            _covered_scopes=covered,
            _data_as_of=date(2026, 8, 23),
        )
        assert cohort['rows'][0]['country'] == 'Mexico'
        assert cohort['rows'][0]['periods'][0]['income_diamonds'] == 609.0


def test_strict_daily_gap_uses_complete_weekly_fallback_without_hiding_cohort():
    with sqlite3.connect(':memory:') as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE guild_executors (
                guild_name TEXT, app_name TEXT, country TEXT
            );
            CREATE TABLE timo_external_streamers (
                guild_executor_key TEXT, guild_name TEXT, country TEXT,
                timo_id TEXT, nickname TEXT, registered_at_bj TEXT,
                joined_guild_at_bj TEXT, last_active_at_bj TEXT,
                is_real_person INTEGER, source_payload TEXT, updated_at TEXT
            );
            CREATE TABLE timo_external_revenue_weekly (
                guild_executor_key TEXT, guild_name TEXT, country TEXT,
                week_start_bj TEXT, timo_id TEXT, total_income REAL
            );
            CREATE TABLE timo_external_revenue_weekly_coverage (
                guild_executor_key TEXT, guild_name TEXT, country TEXT,
                week_start_bj TEXT, status TEXT, row_count INTEGER
            );
            INSERT INTO guild_executors VALUES ('Mexico Guild','timo','Mexico');
            INSERT INTO timo_external_streamers VALUES (
                'mx','Mexico Guild','Mexico','mx-new','MX','2026-07-06','',
                '2026-07-12',1,'{"gender":2}','now'
            );
            INSERT INTO timo_external_revenue_weekly VALUES
                ('mx','Mexico Guild','Mexico','2026-07-06','mx-new',700),
                ('mx','Mexico Guild','Mexico','2026-07-13','mx-new',350);
            INSERT INTO timo_external_revenue_weekly_coverage VALUES
                ('mx','Mexico Guild','Mexico','2026-07-06','success',1),
                ('mx','Mexico Guild','Mexico','2026-07-13','success',1);
            """
        )
        profiles = [dict(row) for row in conn.execute(
            "SELECT guild_executor_key,guild_name,country,timo_id,registered_at_bj,"
            "is_real_person,source_payload FROM timo_external_streamers"
        )]
        cohort = _build_timo_weekly_cohorts_live(
            conn,
            start=date(2026, 7, 6),
            end=date(2026, 7, 19),
            _profiles=profiles,
            _facts=[],
            _covered_scopes=set(),
            _data_as_of=date(2026, 7, 19),
        )

        assert len(cohort['rows']) == 1
        row = cohort['rows'][0]
        assert row['week_start'] == '2026-07-06'
        assert row['periods'][0]['status'] == 'complete'
        assert row['periods'][0]['source'] == 'weekly'
        assert row['periods'][0]['income_diamonds'] == 700.0
        assert row['periods'][1]['status'] == 'complete'
        assert row['periods'][1]['income_diamonds'] == 350.0
        assert row['settlement']['bonus_7d']['status'] == 'incomplete'

        conn.execute(
            "UPDATE timo_external_revenue_weekly_coverage "
            "SET row_count=2 WHERE week_start_bj='2026-07-06'"
        )
        invalid = _build_timo_weekly_cohorts_live(
            conn,
            start=date(2026, 7, 6),
            end=date(2026, 7, 19),
            _profiles=profiles,
            _facts=[],
            _covered_scopes=set(),
            _data_as_of=date(2026, 7, 19),
        )
        assert invalid['rows'][0]['periods'][0]['status'] == 'incomplete'
        assert invalid['rows'][0]['periods'][0]['income_diamonds'] is None
