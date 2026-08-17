from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
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


def _live_ad(
    account: dict[str, str], suffix: str, *, effective: str = "ACTIVE",
    lifetime_impressions: int = 1, lifetime_spend: float = 0.01,
) -> dict:
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
        "insights": {"data": [{
            "impressions": str(lifetime_impressions), "spend": str(lifetime_spend),
        }]} if lifetime_impressions or lifetime_spend else {"data": []},
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
            "active_ads_zero_delivery": 0,
            "active_ads_zero_delivery_after_48h": 0,
        "active_ads_waiting_for_facts": 1,
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


def test_coverage_readiness_uses_the_same_seven_day_window_as_operating_scores() -> None:
    account = GLE_AD_ACCOUNT_SCOPE_V1[0]
    current = _live_ad(account, "20")
    stale = _live_ad(account, "21")
    conn = _database()
    conn.executemany(
        "INSERT INTO ad_dashboard_fact_rows VALUES (?,?,?)",
        [
            (current["id"], "2026-08-03", account["account_id"]),
            (stale["id"], "2026-08-02", account["account_id"]),
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
            for item in (current, stale)
        ],
    )

    items = {
        item["ad_id"]: item
        for current_account in result["accounts"]
        for item in current_account["items"]
    }
    assert result["fact_window"] == {
        "start_date": "2026-08-03",
        "cutoff_date": "2026-08-09",
        "days": 7,
        "complete": False,
    }
    assert items[current["id"]]["monitoring_status"] == "METRIC_OBSERVATION_AVAILABLE"
    assert items[stale["id"]]["monitoring_status"] == "WAITING_FOR_DASHBOARD_FACTS"


def test_complete_window_distinguishes_zero_delivery_from_initial_or_sync_waiting() -> None:
    account = GLE_AD_ACCOUNT_SCOPE_V1[0]
    delivered = _live_ad(account, "30")
    zero_with_delivering_sibling = _live_ad(account, "31")
    zero_with_delivering_sibling["adset_id"] = delivered["adset_id"]
    zero_adset = _live_ad(account, "32")
    recent = _live_ad(account, "33")
    recent["created_time"] = "2026-08-04T00:00:00+0000"
    paused = _live_ad(account, "34", effective="PAUSED")
    live_ads = fetch_scoped_meta_ads(
        _MetaSession(
            {
                account["account_id"]: [
                    delivered,
                    zero_with_delivering_sibling,
                    zero_adset,
                    recent,
                    paused,
                ]
            }
        ),
        access_token="secret-not-output",
        graph_root="https://graph.example/v25.0",
    )
    conn = _database()
    conn.executemany(
        "INSERT INTO ad_dashboard_sync_state VALUES ('all',?,'ok',10)",
        [(f"2026-08-{day:02d}",) for day in range(3, 9)],
    )
    conn.execute(
        "INSERT INTO ad_dashboard_fact_rows VALUES (?,?,?)",
        (delivered["id"], "2026-08-09", account["account_id"]),
    )
    conn.commit()

    result = build_gle_ad_account_coverage(conn, live_ads)
    items = {
        item["ad_id"]: item
        for current_account in result["accounts"]
        for item in current_account["items"]
    }

    assert result["fact_window"]["complete"] is True
    assert items[delivered["id"]]["monitoring_status"] == "METRIC_OBSERVATION_AVAILABLE"
    sibling = items[zero_with_delivering_sibling["id"]]
    assert sibling["monitoring_status"] == "NO_DELIVERY_IN_COMPLETE_WINDOW"
    assert sibling["delivery_diagnosis"]["same_adset_delivering_ads"] == 1
    assert sibling["delivery_diagnosis"]["review_focus"] == "AD_DELIVERY_ALLOCATION"
    assert items[zero_adset["id"]]["delivery_diagnosis"]["review_focus"] == (
        "ADSET_DELIVERY_CONFIGURATION"
    )
    assert items[recent["id"]]["monitoring_status"] == "WAITING_FOR_DASHBOARD_FACTS"
    assert items[paused["id"]]["monitoring_status"] == "WAITING_FOR_DASHBOARD_FACTS"
    assert result["summary"]["active_ads_zero_delivery"] == 2
    assert result["summary"]["active_ads_waiting_for_facts"] == 1


def test_new_ad_with_zero_lifetime_delivery_becomes_actionable_after_48_hours() -> None:
    account = GLE_AD_ACCOUNT_SCOPE_V1[0]
    dead = _live_ad(account, "35", lifetime_impressions=0, lifetime_spend=0)
    live_ads = fetch_scoped_meta_ads(
        _MetaSession({account["account_id"]: [dead]}),
        access_token="secret-not-output",
        graph_root="https://graph.example/v25.0",
    )
    conn = _database()

    result = build_gle_ad_account_coverage(
        conn, live_ads, now=datetime.fromisoformat("2026-08-03T01:00:00+00:00"),
    )
    item = next(
        item for current_account in result["accounts"] for item in current_account["items"]
    )

    assert result["fact_window"]["complete"] is False
    assert item["monitoring_status"] == "NO_LIFETIME_DELIVERY_AFTER_48H"
    assert item["delivery_diagnosis"]["age_hours"] == 49.0
    assert item["delivery_diagnosis"]["lifetime_impressions"] == 0
    assert item["delivery_diagnosis"]["lifetime_spend"] == 0
    assert result["summary"]["active_ads_zero_delivery_after_48h"] == 1
    assert result["summary"]["active_ads_waiting_for_facts"] == 0


def test_dashboard_exposes_all_ad_coverage_without_gate_or_meta_write_claims() -> None:
    assert 'id="adGleCoveragePanel"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert "/api/ops/ad-data-dashboard/gle-ad-coverage" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "GLE 全广告经营覆盖" in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'id="adGleCoverageReadiness"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'id="adGleCoverageFilters"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'id="adGleRecommendationViewTab"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'id="adGleCoverageViewTab"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'id="adGleTaskViewTab"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'data-gle-view-panel="recommendations"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'data-gle-view-panel="coverage"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'data-gle-view-panel="tasks"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'data-gle-workspace-view="recommendations"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'data-gle-workspace-view="coverage"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'data-gle-workspace-view="tasks"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'id="adGleRecommendationSummary"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'id="adGleRecommendationFilters"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'id="adGleRecommendationRows"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert "在投零交付" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "确认重建投放" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "48小时零消耗，确认重建" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "不能删除共享广告组" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "/gle-ad-coverage/rebuild-recommendations" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "直接重建预算、成本上限、受众与排期配置，不再等待数据" in AD_DATA_DASHBOARD_PAGE_HTML
    rebuild_action_html = AD_DATA_DASHBOARD_PAGE_HTML.split("if(step.action==='rebuild')return", 1)[1].split(";return", 1)[0]
    assert "<small>${esc(step.detail)}</small>" not in rebuild_action_html
    assert "在投待同步" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "查看数据" in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'id="adGleTaskWorkbenchMount"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'class="ad-gle-operations-grid"' in AD_DATA_DASHBOARD_PAGE_HTML
    operations_start = AD_DATA_DASHBOARD_PAGE_HTML.index('class="ad-gle-operations-grid"')
    operations_end = AD_DATA_DASHBOARD_PAGE_HTML.index('id="adGleCoverageViewPanel"')
    assert 'class="ad-gle-viewbar"' in AD_DATA_DASHBOARD_PAGE_HTML[operations_start:operations_end]
    assert "覆盖广告任务工作台" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert "查看任务" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "核对 ${activeZeroDelivery} 条在投零交付" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "优先复核" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "任务待处理" in AD_DATA_DASHBOARD_PAGE_HTML
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


def test_dashboard_coverage_surfaces_existing_governed_recommendations() -> None:
    assert "function gleRecommendationSummary(report,workItems=null)" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "dailyRecoNeedsOperator(row)" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "String(row&&row.data_origin||'LEGACY').toUpperCase()!=='LEGACY'" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "function gleOperatingWorkItems" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "function renderGleRecommendationWorkbench" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "只有会改变广告的方案才需要你确认" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "当前无需修改 Meta；系统继续经营跟踪" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "确认 ${recommendation.hard} 条止损调整" in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'data-gle-open-operating-workbench' in AD_DATA_DASHBOARD_PAGE_HTML
    assert "function openGleRecommendationQueue(filter='confirm')" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "function openGleSystemReviewQueue()" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "currentGleRecommendationFilter=filter" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "setGleWorkspaceView('recommendations',{focus:true})" in AD_DATA_DASHBOARD_PAGE_HTML
    queue_start = AD_DATA_DASHBOARD_PAGE_HTML.index("function openGleRecommendationQueue")
    queue_end = AD_DATA_DASHBOARD_PAGE_HTML.index("function renderGleAdCoverage", queue_start)
    assert "adDailyRecommendationPanel" not in AD_DATA_DASHBOARD_PAGE_HTML[queue_start:queue_end]
    assert "if(currentGleAdCoverage)renderGleAdCoverage(currentGleAdCoverage)" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "执行前仍需你逐条确认" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "function gleCoverageRecommendationIndex" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "function gleCoverageNextStep" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "等待本轮评分" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert "本轮未形成可评分样本" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "近7天零交付" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "数据同步或初始窗口待就绪" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "系统核对低投放原因" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "不会按日历无限延长观察期" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert "保持投放，准备放量" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "查看账户明细" not in AD_DATA_DASHBOARD_PAGE_HTML
    assert "data-growth-bulk-confirm" not in AD_DATA_DASHBOARD_PAGE_HTML


def test_dashboard_coverage_flow_is_readonly_filterable_and_keyboard_visible() -> None:
    assert 'role="group" aria-label="筛选 GLE 广告状态"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'data-gle-coverage-filter="${esc(key)}"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'aria-pressed="${key===currentGleCoverageFilter' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'role="group" aria-label="筛选 GLE 经营建议"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'data-gle-recommendation-filter="${esc(key)}"' in AD_DATA_DASHBOARD_PAGE_HTML
    assert 'aria-pressed="${key===currentGleRecommendationFilter' in AD_DATA_DASHBOARD_PAGE_HTML
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
    assert "正在打开任务…" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "button.setAttribute('aria-busy','true')" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "await window.GrowthWorkspace.openExperiment(id)" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "syncGleCoverageTaskScope" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "setCoverageScope(experimentIds,tasks)" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "setGleWorkspaceView('tasks',{focus:true})" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "setGleWorkspaceView('recommendations'" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "showQueue({scroll:false})" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "需你处理" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "AI 处理中" in AD_DATA_DASHBOARD_PAGE_HTML
    assert "观察中" in AD_DATA_DASHBOARD_PAGE_HTML
    assert ".ad-gle-filterbar button:focus-visible" in AD_DATA_DASHBOARD_PAGE_HTML
    assert ".ad-gle-panel{padding:16px}" in AD_DATA_DASHBOARD_PAGE_HTML
    assert ".ad-gle-operations-grid{display:block;min-width:0;overflow:hidden;border:1px solid var(--ad-line)" in AD_DATA_DASHBOARD_PAGE_HTML
    assert '.ad-gle-view-tabs button[aria-selected="true"] span{background:#17233d!important;color:#fff!important}' in AD_DATA_DASHBOARD_PAGE_HTML
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
    assert "async function openWorkspace(experimentId, options={})" in workspace_js
    assert "const loaded = await loadList({select:experimentId})" in workspace_js
    assert "const requestedIds=new Set(state.coverageScope);" in workspace_js
    assert "if(selectedId)requestedIds.add(selectedId);" in workspace_js
    assert "Promise.allSettled([...requestedIds]" in workspace_js
    assert "state.coverageScope.add(selectedId);" in workspace_js
    assert "mount.dataset.experimentIds=JSON.stringify([...state.coverageScope])" in workspace_js
    assert "任务详情暂时无法读取，请重试；系统未执行任何 Meta 操作。" in workspace_js
    assert "async function openExperiment(id) { return openWorkspace(id); }" in workspace_js
    assert "if (isEmbeddedWorkspace()) {\n      showEmbeddedQueue();\n      return;\n    }" in workspace_js
    assert "if(isEmbeddedWorkspace())setWorkspaceReturn({kind:'embeddedQueue'});" in workspace_js
    assert "event.stopPropagation();\n      backWorkspace();" in workspace_js
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
    assert "#growthWorkspacePanel.growth-layer-embedded.has-inline-action{display:grid" in workspace_js
    assert ".growth-layer-embedded.has-inline-action .growth-modal-layer{position:sticky" in workspace_js
    assert "background:rgba(19,31,56,.3)" not in workspace_js[workspace_js.index("embeddedShell.textContent"):]
    assert "const inline=node.id==='growthModal'&&isEmbeddedWorkspace()" in workspace_js
    assert "workspace?.classList.add('has-inline-action')" in workspace_js
    assert "dialog?.setAttribute('role','region')" in workspace_js
    assert "dialog?.removeAttribute('aria-modal')" in workspace_js
    assert "当前任务的下一步" in workspace_js
    assert "任务列表和当前上下文会保留" in workspace_js
    assert "if(activeModal){closeModal();return;}" in workspace_js
    assert "button.addEventListener('click', () => { closeModal();state.workBucket" in workspace_js
    assert "document.getElementById('growthWorkspacePanel')?.classList.remove('has-inline-action')" in workspace_js
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
    assert "immediate_assessment" in workspace_js
    assert "立即经营判断" in workspace_js
    assert "现在不用再停广告" in workspace_js
    assert "这不是暂停后的效果评价" in workspace_js
    assert "继续观察，暂不干预" in workspace_js


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


def test_zero_delivery_rebuild_materialization_revalidates_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "zero-delivery-rebuild.db"
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
        """
    )
    for day in range(3, 10):
        conn.execute(
            "INSERT INTO ad_dashboard_sync_state VALUES ('all',?,'ok',5)",
            (f"2026-08-{day:02d}",),
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
    target = rows_by_account[GLE_AD_ACCOUNT_SCOPE_V1[0]["account_id"]][0]
    target["insights"] = {"data": []}
    meta = _MetaSession(rows_by_account)
    app = FastAPI()
    app.include_router(
        create_ad_experiment_router(
            db=_Db(), require_admin=lambda _request: {"username": "operator"},
            meta_session=meta, meta_access_token="token",
            meta_graph_root="https://graph.example/v25.0",
        )
    )
    client = TestClient(app)

    first = client.post(
        "/api/ops/ad-data-dashboard/gle-ad-coverage/rebuild-recommendations",
        json={"ad_ids": [target["id"]]},
    )
    second = client.post(
        "/api/ops/ad-data-dashboard/gle-ad-coverage/rebuild-recommendations",
        json={"ad_ids": [target["id"]]},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    recommendation = first.json()["recommendations"][0]
    assert recommendation["action_type"] == "repair_delivery_config"
    assert recommendation["source_ad_id"] == target["id"]
    assert recommendation["decision_context"]["rebuild_mode"] == "CREATE_PAUSED_OBJECTS"
    assert recommendation["decision_context"]["source_adset_delete_allowed"] is False
    assert first.json()["meta_writes_performed"] is False
    assert second.json()["recommendations"][0]["recommendation_id"] == recommendation["recommendation_id"]
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT COUNT(*) FROM ad_recommendation").fetchone()[0] == 1
