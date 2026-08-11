from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from app import ad_dashboard_repository, main_shared, sqlite_write_queue
from app.growth.ad_account_coverage import build_gle_ad_account_coverage
from app.sqlite_write_queue import SQLiteWriteQueueError, apply_sqlite_write_job


def _meta_row(*, ad_id: str, adset_id: str, cost: float, impressions: int, clicks: int):
    return {
        "date": "2026-08-10",
        "data_source": "Meta",
        "platform": "Meta",
        "account_id": "act_1012060198097836",
        "account_name": "自投-MX-TM",
        "app_id": "自投-MX-TM",
        "country": "Mexico",
        "media_source": "Meta",
        "campaign": "共享广告系列",
        "campaign_id": "120250176932910544",
        "ad_group": "共享广告组",
        "adset_id": adset_id,
        "ad": "自投MX-04",
        "ad_name": "自投MX-04",
        "ad_id": ad_id,
        "source_type": "推广量",
        "cost": cost,
        "impressions": impressions,
        "clicks": clicks,
        "link_clicks": clicks,
    }


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _insert_legacy_predecessor(conn: sqlite3.Connection, row: dict, *, cost: float) -> str:
    row_id = ad_dashboard_repository._ad_fact_legacy_row_id(row)
    conn.execute(
        """
        INSERT INTO ad_dashboard_fact_rows(
            row_id,date,data_source,platform,app_id,account_id,account_name,country,
            media_source,campaign,campaign_id,adset_id,ad_id,ad_group,ad,source_type,
            row_count,cost,impressions,clicks,link_clicks,payload_json,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            row_id,
            row["date"],
            row["data_source"],
            row["platform"],
            row["app_id"],
            row["account_id"],
            row["account_name"],
            row["country"],
            row["media_source"],
            row["campaign"],
            row["campaign_id"],
            row["adset_id"],
            row["ad_id"],
            row["ad_group"],
            row["ad"],
            row["source_type"],
            2,
            cost,
            300,
            30,
            30,
            json.dumps({"legacy_fact_grain": "name"}),
            "2026-08-11T00:00:00+00:00",
        ),
    )
    return row_id


@pytest.mark.parametrize("runtime", [ad_dashboard_repository, main_shared])
def test_same_name_meta_ads_materialize_as_exact_id_rows(runtime):
    rows = [
        _meta_row(
            ad_id="120250333715100544",
            adset_id="120250333715110544",
            cost=12.5,
            impressions=100,
            clicks=10,
        ),
        _meta_row(
            ad_id="120250363359310544",
            adset_id="120250363359330544",
            cost=7.5,
            impressions=200,
            clicks=20,
        ),
    ]

    materialized = runtime._ad_materialize_fact_rows(rows)

    assert len(materialized) == 2
    assert {row["ad_id"] for row in materialized} == {
        "120250333715100544",
        "120250363359310544",
    }
    assert {row["fact_grain_version"] for row in materialized} == {"meta_exact_ad_v2"}
    assert sum(row["cost"] for row in materialized) == 20
    assert sum(row["impressions"] for row in materialized) == 300
    assert sum(row["clicks"] for row in materialized) == 30
    assert len({runtime._ad_fact_row_id(row) for row in materialized}) == 2


@pytest.mark.parametrize("runtime", [ad_dashboard_repository, main_shared])
def test_exact_upsert_replaces_only_represented_legacy_name_grain(runtime):
    conn = _connect()
    first = _meta_row(
        ad_id="120250333715100544",
        adset_id="120250333715110544",
        cost=12.5,
        impressions=100,
        clicks=10,
    )
    second = _meta_row(
        ad_id="120250363359310544",
        adset_id="120250363359330544",
        cost=7.5,
        impressions=200,
        clicks=20,
    )
    unrelated = dict(first)
    unrelated.update(
        account_id="act_1250000910496826",
        account_name="自投-ID-TM",
        app_id="自投-ID-TM",
        ad_id="120250999999999999",
        adset_id="120250999999990000",
    )
    try:
        runtime.ensure_ad_dashboard_fact_tables(conn)
        represented_legacy = _insert_legacy_predecessor(conn, first, cost=20)
        unrelated_legacy = _insert_legacy_predecessor(conn, unrelated, cost=3)

        assert runtime.upsert_ad_dashboard_fact_rows(conn, [first, second]) == 2
        assert runtime.upsert_ad_dashboard_fact_rows(conn, [first, second]) == 2

        stored = conn.execute(
            "SELECT row_id,ad_id,cost,impressions,clicks,payload_json "
            "FROM ad_dashboard_fact_rows ORDER BY account_id,ad_id"
        ).fetchall()
        assert conn.execute(
            "SELECT COUNT(*) FROM ad_dashboard_fact_rows WHERE row_id=?",
            (represented_legacy,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ad_dashboard_fact_rows WHERE row_id=?",
            (unrelated_legacy,),
        ).fetchone()[0] == 1
        exact = [row for row in stored if row["ad_id"] in {first["ad_id"], second["ad_id"]}]
        assert len(exact) == 2
        assert sum(row["cost"] for row in exact) == 20
        assert sum(row["impressions"] for row in exact) == 300
        assert sum(row["clicks"] for row in exact) == 30
        assert all(json.loads(row["payload_json"])["fact_grain_version"] == "meta_exact_ad_v2" for row in exact)
    finally:
        conn.close()


def test_exact_fact_rows_make_coverage_readable_by_ad_id():
    conn = _connect()
    rows = [
        _meta_row(
            ad_id="120250333715100544",
            adset_id="120250333715110544",
            cost=12.5,
            impressions=100,
            clicks=10,
        ),
        _meta_row(
            ad_id="120250363359310544",
            adset_id="120250363359330544",
            cost=7.5,
            impressions=200,
            clicks=20,
        ),
    ]
    live_ads = [
        {
            "account_id": "1012060198097836",
            "account_name": "自投-MX-TM",
            "market": "MX",
            "ad_id": row["ad_id"],
            "ad_name": row["ad_name"],
            "campaign_id": row["campaign_id"],
            "adset_id": row["adset_id"],
            "configured_status": "ACTIVE",
            "effective_status": "ACTIVE",
            "created_time": "2026-08-01T00:00:00+0000",
            "updated_time": "2026-08-10T00:00:00+0000",
        }
        for row in rows
    ]
    try:
        ad_dashboard_repository.ensure_ad_dashboard_fact_tables(conn)
        conn.execute(
            """
            CREATE TABLE ad_experiment(
                experiment_id TEXT,account_id TEXT,source_report_id TEXT,
                source_campaign_id TEXT,source_adset_id TEXT,source_ad_id TEXT,
                state TEXT,control_definition_json TEXT
            )
            """
        )
        ad_dashboard_repository.upsert_ad_dashboard_fact_rows(conn, rows)
        ad_dashboard_repository.mark_ad_dashboard_sync_state(
            conn,
            source="all",
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 10),
            status="ok",
            row_count=2,
        )

        result = build_gle_ad_account_coverage(conn, live_ads)

        mx = next(account for account in result["accounts"] if account["account_id"] == "1012060198097836")
        assert mx["effective_active_ads"] == 2
        assert mx["active_ads_with_metric_observation"] == 2
        assert {item["monitoring_status"] for item in mx["items"]} == {"METRIC_OBSERVATION_AVAILABLE"}
        assert {
            item["fact_window"]["row_count"] for item in mx["items"]
        } == {1}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(ad_dashboard_fact_rows)")}
        assert "idx_ad_dashboard_fact_ad_identity" in indexes
    finally:
        conn.close()


def test_meta_row_without_complete_ids_stays_on_legacy_non_authoritative_grain():
    row = _meta_row(
        ad_id="",
        adset_id="120250333715110544",
        cost=1,
        impressions=2,
        clicks=3,
    )

    for runtime in (ad_dashboard_repository, main_shared):
        materialized = runtime._ad_materialize_fact_rows([row])
        assert len(materialized) == 1
        assert "fact_grain_version" not in materialized[0]
        assert runtime._ad_fact_grain_key(materialized[0]) == runtime._ad_fact_legacy_grain_key(materialized[0])


def test_dedicated_writer_commits_only_after_exact_meta_readback(tmp_path):
    db_path = tmp_path / "facts.db"
    rows = [
        _meta_row(
            ad_id="120250333715100544",
            adset_id="120250333715110544",
            cost=12.5,
            impressions=100,
            clicks=10,
        ),
        _meta_row(
            ad_id="120250363359310544",
            adset_id="120250363359330544",
            cost=7.5,
            impressions=200,
            clicks=20,
        ),
    ]
    conn = sqlite3.connect(db_path)
    try:
        ad_dashboard_repository.ensure_ad_dashboard_fact_tables(conn)
        legacy_row_id = _insert_legacy_predecessor(conn, rows[0], cost=20)
        conn.commit()
    finally:
        conn.close()

    result = apply_sqlite_write_job(
        db_path=str(db_path),
        job={
            "type": "ad_dashboard_fact_replace",
            "rows": rows,
            "start_date": "2026-08-10",
            "end_date": "2026-08-10",
            "source": "meta_exact_repair",
            "appsflyer_required": False,
            "tugao_funnel_required": False,
        },
    )

    assert result["exact_meta_readback"] == {
        "stored_rows": 2,
        "superseded_legacy_rows_remaining": 0,
        "cost": 20.0,
        "impressions": 300,
        "clicks": 30,
    }
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM ad_dashboard_fact_rows WHERE row_id=?",
            (legacy_row_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ad_dashboard_fact_rows WHERE ad_id IN (?,?)",
            (rows[0]["ad_id"], rows[1]["ad_id"]),
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_dedicated_writer_rolls_back_legacy_replacement_when_readback_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "facts.db"
    row = _meta_row(
        ad_id="120250333715100544",
        adset_id="120250333715110544",
        cost=12.5,
        impressions=100,
        clicks=10,
    )
    conn = sqlite3.connect(db_path)
    try:
        ad_dashboard_repository.ensure_ad_dashboard_fact_tables(conn)
        legacy_row_id = _insert_legacy_predecessor(conn, row, cost=12.5)
        conn.commit()
    finally:
        conn.close()

    def fail_readback(*_args, **_kwargs):
        raise SQLiteWriteQueueError("forced_readback_failure")

    monkeypatch.setattr(sqlite_write_queue, "_exact_meta_write_readback", fail_readback)
    with pytest.raises(SQLiteWriteQueueError, match="forced_readback_failure"):
        apply_sqlite_write_job(
            db_path=str(db_path),
            job={
                "type": "ad_dashboard_fact_replace",
                "rows": [row],
                "start_date": "2026-08-10",
                "end_date": "2026-08-10",
                "source": "meta_exact_repair",
                "appsflyer_required": False,
                "tugao_funnel_required": False,
            },
        )

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM ad_dashboard_fact_rows WHERE row_id=?",
            (legacy_row_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM ad_dashboard_fact_rows WHERE row_id=?",
            (ad_dashboard_repository._ad_fact_row_id(row),),
        ).fetchone()[0] == 0
    finally:
        conn.close()
