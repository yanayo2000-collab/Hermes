from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.growth.ad_account_coverage import (
    AdAccountCoverageError,
    GLE_AD_ACCOUNT_SCOPE_V1,
    build_gle_ad_account_coverage,
    fetch_scoped_meta_ads,
)
from app.growth.api import create_ad_experiment_router
from app.main_pages import AD_DATA_DASHBOARD_PAGE_HTML


class _Response:
    def __init__(self, body: dict) -> None:
        self.body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.body


class _MetaSession:
    def __init__(self, rows_by_account: dict[str, list[dict]]) -> None:
        self.rows_by_account = rows_by_account
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        account_id = str(url).rsplit("/act_", 1)[-1].split("/", 1)[0]
        self.calls.append({"url": url, **kwargs})
        return _Response({"data": self.rows_by_account.get(account_id, [])})


def _live_ad(account: dict[str, str], suffix: str, *, effective: str = "ACTIVE") -> dict:
    return {
        "id": f"9{account['account_id'][-8:]}{suffix}",
        "name": f"ad-{suffix}",
        "account_id": account["account_id"],
        "campaign_id": f"8{account['account_id'][-8:]}{suffix}",
        "adset_id": f"7{account['account_id'][-8:]}{suffix}",
        "status": "ACTIVE" if effective == "ACTIVE" else "PAUSED",
        "effective_status": effective,
        "created_time": "2026-08-01T00:00:00+0000",
        "updated_time": "2026-08-09T00:00:00+0000",
    }


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE ad_dashboard_sync_state (
          source TEXT,date TEXT,status TEXT,row_count INTEGER
        );
        CREATE TABLE ad_dashboard_fact_rows (
          ad_id TEXT,date TEXT,account_id TEXT
        );
        CREATE TABLE ad_experiment (
          experiment_id TEXT,account_id TEXT,source_report_id TEXT,
          source_campaign_id TEXT,source_adset_id TEXT,source_ad_id TEXT,
          state TEXT,control_definition_json TEXT
        );
        INSERT INTO ad_dashboard_sync_state VALUES ('all','2026-08-09','ok',10);
        """
    )
    return conn


def test_all_five_accounts_and_every_meta_ad_are_rostered_read_only() -> None:
    rows_by_account = {
        account["account_id"]: [_live_ad(account, "1")]
        for account in GLE_AD_ACCOUNT_SCOPE_V1
    }
    first = GLE_AD_ACCOUNT_SCOPE_V1[0]
    rows_by_account[first["account_id"]].append(_live_ad(first, "2"))
    session = _MetaSession(rows_by_account)

    live_ads = fetch_scoped_meta_ads(
        session, access_token="secret-not-output", graph_root="https://graph.example/v25.0"
    )
    assert len(session.calls) == 5
    assert all(call["headers"] == {"Authorization": "Bearer secret-not-output"} for call in session.calls)
    assert all("access_token" not in call["params"] for call in session.calls)

    conn = _database()
    for item in live_ads[:-1]:
        conn.execute(
            "INSERT INTO ad_dashboard_fact_rows VALUES (?,?,?)",
            (item["ad_id"], "2026-08-09", f"act_{item['account_id']}"),
        )
    pair = live_ads[:2]
    for index, item in enumerate(pair):
        conn.execute(
            "INSERT INTO ad_experiment VALUES (?,?,?,?,?,?,?,?)",
            (
                f"exp-{index + 1}", item["account_id"], "launch-1",
                pair[0]["campaign_id"], item["adset_id"], item["ad_id"], "MATURING",
                json.dumps({"role": "BASELINE" if index == 0 else "CHALLENGER"}),
            ),
        )
    conn.commit()

    result = build_gle_ad_account_coverage(conn, live_ads)

    assert result["coverage_status"] == "ALL_META_ADS_ROSTERED_READ_ONLY"
    assert result["summary"] == {
        "account_count": 5,
        "ads_total": 6,
        "effective_active_ads": 6,
        "covered_ads": 6,
        "covered_active_ads": 6,
        "ads_with_metric_observation": 5,
        "active_ads_with_metric_observation": 5,
        "multi_cell_experiment_ads": 2,
        "single_ad_observation_ads": 4,
    }
    items = [item for account in result["accounts"] for item in account["items"]]
    assert {item["coverage_status"] for item in items} == {"COVERED_READ_ONLY"}
    assert sum(item["coverage_mode"] == "MULTI_CELL_EXPERIMENT" for item in items) == 2
    assert sum(item["monitoring_status"] == "WAITING_FOR_DASHBOARD_FACTS" for item in items) == 1
    assert all(item["causal_claim"] is False for item in items)
    assert all(item["meta_write_allowed_by_gate"] is False for item in items)
    assert result["gate"] == {
        "gate0_status": "QUASI_ONLY",
        "gate0_result_effect": "UNCHANGED",
        "gate1_status": "NOT_READY",
        "causal_claim": False,
        "meta_write_allowed_by_gate": False,
    }


def test_meta_roster_rejects_cross_account_identity() -> None:
    rows_by_account = {
        account["account_id"]: [_live_ad(account, "1")]
        for account in GLE_AD_ACCOUNT_SCOPE_V1
    }
    rows_by_account[GLE_AD_ACCOUNT_SCOPE_V1[0]["account_id"]][0]["account_id"] = (
        GLE_AD_ACCOUNT_SCOPE_V1[1]["account_id"]
    )
    with pytest.raises(AdAccountCoverageError, match="META_IDENTITY_CONFLICT"):
        fetch_scoped_meta_ads(
            _MetaSession(rows_by_account),
            access_token="token",
            graph_root="https://graph.example/v25.0",
        )


def test_fact_binding_ignores_blank_source_identity_but_rejects_missing_or_conflicting_identity() -> None:
    account = GLE_AD_ACCOUNT_SCOPE_V1[0]
    exact_with_blank = _live_ad(account, "10")
    blank_only = _live_ad(account, "11")
    conflicting = _live_ad(account, "12")
    conn = _database()
    conn.executemany(
        "INSERT INTO ad_dashboard_fact_rows VALUES (?,?,?)",
        [
            (exact_with_blank["id"], "2026-08-09", f"act_{account['account_id']}"),
            (exact_with_blank["id"], "2026-08-09", ""),
            (blank_only["id"], "2026-08-09", ""),
            (conflicting["id"], "2026-08-09", account["account_id"]),
            (conflicting["id"], "2026-08-09", GLE_AD_ACCOUNT_SCOPE_V1[1]["account_id"]),
        ],
    )
    conn.commit()

    result = build_gle_ad_account_coverage(
        conn,
        [
            {
                "account_id": item["account_id"],
                "account_name": account["account_name"],
                "market": account["market"],
                "ad_id": item["id"],
                "ad_name": item["name"],
                "campaign_id": item["campaign_id"],
                "adset_id": item["adset_id"],
                "configured_status": item["status"],
                "effective_status": item["effective_status"],
                "created_time": item["created_time"],
                "updated_time": item["updated_time"],
            }
            for item in (exact_with_blank, blank_only, conflicting)
        ],
    )

    items = {
        item["ad_id"]: item
        for current_account in result["accounts"]
        for item in current_account["items"]
    }
    assert items[exact_with_blank["id"]]["monitoring_status"] == "METRIC_OBSERVATION_AVAILABLE"
    assert items[blank_only["id"]]["monitoring_status"] == "WAITING_FOR_DASHBOARD_FACTS"
    assert items[conflicting["id"]]["monitoring_status"] == "WAITING_FOR_DASHBOARD_FACTS"
    assert result["summary"]["active_ads_with_metric_observation"] == 1


def test_dashboard_exposes_all_ad_coverage_without_gate_or_meta_write_claims() -> None:
    assert 'id="adGleCoveragePanel"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert "/api/ops/ad-data-dashboard/gle-ad-coverage" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "GLE 全广告经营覆盖" in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'id="adGleCoverageReadiness"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'id="adGleCoverageFilters"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert "在投待数据" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "定位 Meta 明细" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "查看经营建议" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "关键归因数据还没收齐" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "暂时不能判断哪项调整真正带来了效果" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "不会自动停投、扩量或修改 Meta 广告" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "Gate0=QUASI_ONLY" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert "Gate1=NOT_READY" not in AD_DATA_DASHBOARD_PAGE_HTML


def test_dashboard_coverage_flow_is_readonly_filterable_and_keyboard_visible() -> None:
    assert 'role="group" aria-label="筛选 GLE 广告状态"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'data-gle-coverage-filter="${esc(key)}"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'aria-pressed="${key===currentGleCoverageFilter' in AD_DATA_DASHBOARD_PAGE_HTML
    assert "gleCoverageItemMatches" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "focusGleAdInDashboard" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "setPlatformCollapsed('Meta',false,{manual:true})" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "loadDashboard({preserveDailyReport:true,skipDailyReport:true})" in AD_DATA_DASHBOARD_PAGE_HTML
    assert ".ad-gle-filterbar button:focus-visible" in AD_DATA_DASHBOARD_PAGE_HTML
    assert ".ad-gle-panel{padding:16px}" in AD_DATA_DASHBOARD_PAGE_HTML
    assert ".ad-gle-account-table-head{min-height:36px" in AD_DATA_DASHBOARD_PAGE_HTML
    assert ".ad-gle-chip{display:inline-flex;align-items:center;min-height:24px" in AD_DATA_DASHBOARD_PAGE_HTML
    assert ".ad-gle-account-open,.ad-gle-row-action{display:inline-flex;align-items:center;justify-content:center;gap:5px;min-height:34px" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "meta_write_allowed_by_gate" not in AD_DATA_DASHBOARD_PAGE_HTML[
        AD_DATA_DASHBOARD_PAGE_HTML.index('id="adGleCoveragePanel"'):
        AD_DATA_DASHBOARD_PAGE_HTML.index('id="adDailyRecommendationPanel"')
    ]


def test_readonly_api_returns_the_exact_five_account_scope(tmp_path: Path) -> None:
    path = tmp_path / "coverage.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE ad_dashboard_sync_state (
          source TEXT,date TEXT,status TEXT,row_count INTEGER
        );
        CREATE TABLE ad_dashboard_fact_rows (
          ad_id TEXT,date TEXT,account_id TEXT
        );
        CREATE TABLE ad_experiment (
          experiment_id TEXT,account_id TEXT,source_report_id TEXT,
          source_campaign_id TEXT,source_adset_id TEXT,source_ad_id TEXT,
          state TEXT,control_definition_json TEXT
        );
        INSERT INTO ad_dashboard_sync_state VALUES ('all','2026-08-09','ok',5);
        """
    )
    conn.commit()
    conn.close()

    class _Db:
        @contextmanager
        def connect(self):
            current = sqlite3.connect(path)
            current.row_factory = sqlite3.Row
            try:
                yield current
            finally:
                current.close()

    rows_by_account = {
        account["account_id"]: [_live_ad(account, "1")]
        for account in GLE_AD_ACCOUNT_SCOPE_V1
    }
    meta = _MetaSession(rows_by_account)
    app = FastAPI()
    app.include_router(
        create_ad_experiment_router(
            db=_Db(),
            require_admin=lambda _request: {"username": "operator"},
            meta_session=meta,
            meta_access_token="token",
            meta_graph_root="https://graph.example/v25.0",
        )
    )

    response = TestClient(app).get("/api/ops/ad-data-dashboard/gle-ad-coverage")

    assert response.status_code == 200
    body = response.json()
    assert [item["account_name"] for item in body["accounts"]] == [
        item["account_name"] for item in GLE_AD_ACCOUNT_SCOPE_V1
    ]
    assert body["summary"]["covered_ads"] == 5
    assert body["safety"]["meta_writes_performed"] is False
    assert all(call["url"].endswith("/ads") for call in meta.calls)
