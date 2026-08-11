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
    assert 'id="adGleCoverageViewTab"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'id="adGleTaskViewTab"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'data-gle-view-panel="coverage"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'data-gle-view-panel="tasks"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'data-gle-workspace-view="coverage"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'data-gle-workspace-view="tasks"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert "在投待数据" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "查看数据" in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'id="adGleTaskWorkbenchMount"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'class="ad-gle-operations-grid"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert "覆盖广告任务工作台" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert "查看任务" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "核对 ${activeWaiting} 条在投数据" in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'id="adOpenGleRecommendations"' not in AD_DATA_DASHBOARD_PAGE_HTML
    assert "归因未收齐不判定因果赢家" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "只读分析 · 调整需确认" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "可用于经营判断" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert "查看观察进度" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert "当前没有需要确认的 GLE 任务" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert "缺失值保持为空" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert "当前筛选显示" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert "可以切换上方状态继续查看" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert "先处理需确认任务，再查看数据与广告" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert "当前最重要的工作" not in AD_DATA_DASHBOARD_PAGE_HTML
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
    assert "/api/ops/ad-data-dashboard/experiments?limit=200" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert "gleBoundExperimentIds" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "Promise.allSettled(experimentIds.map" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "/api/ops/ad-data-dashboard/experiments/${encodeURIComponent(experimentId)}" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "normalizeGleTaskDetail" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "currentGleTaskByExperiment" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "data-gle-open-task" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "openGleExperimentTask" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "syncGleCoverageTaskScope" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "setCoverageScope(experimentIds,tasks)" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "setGleWorkspaceView('tasks')" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "showQueue({scroll:false})" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "需你处理" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "AI 处理中" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "观察中" in AD_DATA_DASHBOARD_PAGE_HTML
    assert ".ad-gle-filterbar button:focus-visible" in AD_DATA_DASHBOARD_PAGE_HTML
    assert ".ad-gle-panel{padding:16px}" in AD_DATA_DASHBOARD_PAGE_HTML
    assert ".ad-gle-operations-grid{display:block;min-width:0}" in AD_DATA_DASHBOARD_PAGE_HTML
    assert '.ad-gle-operations-grid [data-gle-view-panel][hidden]{display:none!important}' in AD_DATA_DASHBOARD_PAGE_HTML
    assert "grid-template-columns:minmax(0,1.55fr) minmax(340px,.65fr)" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert ".ad-gle-operations-grid.is-task-detail" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert "#adGleTaskWorkbenchMount{position:sticky" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert ".ad-gle-account-table-head{min-height:36px" in AD_DATA_DASHBOARD_PAGE_HTML
    assert ".ad-gle-chip{display:inline-flex;align-items:center;min-height:24px" in AD_DATA_DASHBOARD_PAGE_HTML
    assert ".ad-gle-account-open,.ad-gle-row-action{display:inline-flex;align-items:center;justify-content:center;gap:5px;min-height:34px" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "meta_write_allowed_by_gate" not in AD_DATA_DASHBOARD_PAGE_HTML[
        AD_DATA_DASHBOARD_PAGE_HTML.index('id="adGleCoveragePanel"'):
        AD_DATA_DASHBOARD_PAGE_HTML.index('id="adDailyRecommendationPanel"')
    ]


def test_dashboard_coverage_opens_the_existing_governed_task_flow() -> None:
    workspace_js = (
        Path(__file__).resolve().parents[1] / "app/static/ops/growth-workspace.js"
    ).read_text(encoding="utf-8")

    assert "openTasks:openLaunchWorkspace" in workspace_js
    assert "setCoverageScope" in workspace_js
    assert "coverageScope:new Set()" in workspace_js
    assert "scopedExperiments()" in workspace_js
    assert "growth-layer growth-layer-embedded" in workspace_js
    assert 'role="region" aria-label="覆盖广告关联任务"' in workspace_js
    assert "growth-embedded-refresh" in workspace_js
    assert "growth-layer-embedded.is-detail-open" in workspace_js
    assert "showQueue:showEmbeddedQueue" in workspace_js
    assert "function showEmbeddedQueue({scroll=true}={})" in workspace_js
    assert "classList.toggle('is-task-detail',Boolean(active))" not in workspace_js
    assert ".growth-layer-embedded .growth-task-list{grid-template-columns:repeat(2,minmax(0,1fr))" in workspace_js
    assert ".growth-layer-embedded .growth-task-group-row>span{display:block" in workspace_js
    assert ".growth-layer-embedded .growth-task-group-copy{display:grid!important}" in workspace_js
    assert ".growth-layer-embedded .growth-task-group-row>span:not(.growth-task-group-copy){display:none}" in workspace_js
    assert ".growth-layer-embedded.is-detail-open{min-height:0}" in workspace_js
    assert ".growth-layer-embedded.is-detail-open .growth-detail{display:block;overflow:visible" in workspace_js
    assert ".growth-layer-embedded.is-detail-open .growth-detail.has-autonomy-panel{display:grid" in workspace_js
    assert ".growth-layer-embedded.is-detail-open{min-height:560px}" not in workspace_js
    assert ".growth-layer-embedded.is-detail-open .growth-detail{flex:1 1 auto;overflow:auto" not in workspace_js
    assert 'id="growthTaskSearch"' not in workspace_js[
        workspace_js.index("panel.innerHTML = embeddedMount ?"):
        workspace_js.index(" : `\n      <div class=\"growth-backdrop\"")
    ]
    assert "if (embeddedMount) embeddedMount.replaceChildren(panel);" in workspace_js
    assert "else document.body.appendChild(panel);" in workspace_js
    assert "if (!embeddedMount)" in workspace_js
    assert "openExperiment(id)" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "只读分析 · 调整需确认" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "现在能做" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert "既有计划、明确确认与回读流程" not in AD_DATA_DASHBOARD_PAGE_HTML


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
