from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DECISION_JS = ROOT / "app/static/ops/growth-decision.js"
MAIN_PAGES = ROOT / "app/main_pages.py"


def test_poor_scorecard_maps_to_readonly_review_and_repair_creative_maps_to_experiment() -> None:
    source = DECISION_JS.read_text(encoding="utf-8")
    instrumented = source.rsplit("})();", 1)[0] + "\nwindow.__growthDecisionTest = {actionFromRecommendation};\n})();\n"
    row = {
        "recommendation_id": "review-1",
        "data_origin": "NATIVE_V2",
        "action_type": "observe",
        "primary_action": "observe",
        "source_ad_id": "ad-1",
        "gle_scope_verified": True,
        "gle_scope_ad_id": "ad-1",
        "gle_scope_account_id": "act-1",
        "gle_scope_account_name": "授权账户",
        "evidence": {"scorecard": {"band": "poor"}},
    }
    harness = f"""
global.window = global;
global.document = {{ addEventListener() {{}} }};
window.dailyRecommendationDecisionAction = row => row && row.evidence && row.evidence.scorecard && row.evidence.scorecard.band === 'poor' ? 'manual_review' : (row.action_type || 'observe');
{instrumented}
console.log(JSON.stringify({{
  reviewAction: window.__growthDecisionTest.actionFromRecommendation({json.dumps(row)}),
  repairAction: window.__growthDecisionTest.actionFromRecommendation({{action_type: 'generate_repair_creative'}})
}}));
"""
    result = subprocess.run(
        ["node", "-e", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "reviewAction": "CHECK_DATA",
        "repairAction": "CREATE_EXPERIMENT",
    }


def test_review_ui_is_automatic_and_surfaces_each_ads_next_step() -> None:
    page = MAIN_PAGES.read_text(encoding="utf-8")
    decision = DECISION_JS.read_text(encoding="utf-8")

    assert "window.dailyRecommendationDecisionAction=row=>dailyRecoDisplayAction(row);" in page
    assert "function gleScopedRecommendationRows" in page
    assert "function gleCoverageRecommendationIndex" in page
    assert "function gleCoverageNextStep" in page
    assert "function openGleSystemReviewQueue" in page
    assert "function mergeDailyDecisionStates" in page
    assert "const sourceAdId=String(row&&row.source_ad_id||'').trim()" in page
    assert "gle_scope_verified:true" in page
    assert "data-growth-bulk-confirm" not in page
    assert "查看账户明细" not in page
    assert "系统已复核 ${recommendation.systemReview} 条表现偏弱广告" in page
    assert "数据更新后自动重算" in page
    assert "data-gle-open-system-reviews" in page
    assert "&refresh=1" in page
    assert "growth-decision.js?v=20260812-review-followup-v2" in page

    assert "generate_repair_creative: 'CREATE_EXPERIMENT'" in decision
    assert "growthBulkDecisionModal" not in decision
    assert "确认加入复核队列" not in decision
    assert "系统已完成当前只读复核" in decision


def test_fresh_report_cannot_erase_an_accepted_local_decision() -> None:
    page = MAIN_PAGES.read_text(encoding="utf-8")
    start = page.index("function mergeDailyDecisionStates")
    end = page.index("function dailyReportCachedAtText", start)
    harness = f"""
{page[start:end]}
const prior = {{recommendations: [{{recommendation_id: 'r1', decision_state: {{decision_id: 'd1', status: 'CREATED'}}}}]}};
const stale = {{recommendations: [{{recommendation_id: 'r1'}}, {{recommendation_id: 'r2'}}]}};
const current = {{recommendations: [{{recommendation_id: 'r1', decision_state: {{decision_id: 'd2', status: 'SUCCEEDED'}}}}]}};
console.log(JSON.stringify({{
  retained: mergeDailyDecisionStates(stale, prior).recommendations[0].decision_state,
  serverWins: mergeDailyDecisionStates(current, prior).recommendations[0].decision_state
}}));
"""
    result = subprocess.run(
        ["node", "-e", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "retained": {"decision_id": "d1", "status": "CREATED"},
        "serverWins": {"decision_id": "d2", "status": "SUCCEEDED"},
    }


def test_each_covered_ad_gets_a_truthful_next_step() -> None:
    page = MAIN_PAGES.read_text(encoding="utf-8")
    start = page.index("function gleCoverageNextStep")
    end = page.index("function gleCoverageFilterDefinitions", start)
    function_source = page[start:end]
    harness = f"""
const dailyRecoDisplayAction = row => row.action;
const dailyRecoHumanStatus = row => row.status || '表现一般';
const dailyRecoIsSystemReview = row => row.action === 'manual_review';
const dailyRecoNeedsOperator = row => ['pause', 'reduce_budget'].includes(row.action);
const dailyRecoHasDecision = row => Boolean(row.decision_state && row.decision_state.decision_id);
const dailyRecoDisplayActionZh = row => row.action === 'pause' ? '暂停' : row.action;
const gleRecommendationActionLabel = row => row.action === 'pause' ? '确认暂停' : '查看并确认';
{function_source}
console.log(JSON.stringify({{
  review: gleCoverageNextStep({{action: 'manual_review', status: '表现偏弱'}}, {{ready: true}}),
  pause: gleCoverageNextStep({{action: 'pause', status: '严重超阈值'}}, {{ready: true}}),
  waiting: gleCoverageNextStep(null, {{ready: false}}),
  scoring: gleCoverageNextStep(null, {{ready: true}}),
  inactive: gleCoverageNextStep(null, {{active: false, ready: false}}),
  task: gleCoverageNextStep(null, {{ready: true, task: true}})
}}));
"""
    result = subprocess.run(
        ["node", "-e", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["review"]["label"] == "继续观察"
    assert "自动重算" in payload["review"]["detail"]
    assert payload["pause"]["label"] == "确认暂停"
    assert payload["pause"]["action"] == "decision"
    assert payload["waiting"]["label"] == "系统补数中"
    assert payload["scoring"]["label"] == "等待下一轮评分"
    assert payload["inactive"]["label"] == "当前未投放"
    assert payload["task"]["label"] == "查看任务"


def test_bulk_review_scope_requires_exact_covered_ad_id() -> None:
    page = MAIN_PAGES.read_text(encoding="utf-8")
    start = page.index("function gleReviewScopeIndex")
    end = page.index("function gleCoverageFilterDefinitions", start)
    scope_functions = page[start:end]
    coverage = {
        "accounts": [
            {
                "account_id": "account-1",
                "account_name": "授权账户",
                "items": [{"ad_id": "ad-inside"}],
            }
        ]
    }
    rows = [
        {"recommendation_id": "inside", "source_ad_id": "ad-inside"},
        {"recommendation_id": "outside", "source_ad_id": "ad-outside"},
        {"recommendation_id": "missing"},
    ]
    harness = f"""
const currentGleAdCoverage = {json.dumps(coverage)};
{scope_functions}
const scoped = gleScopedRecommendationRows({json.dumps(rows)});
console.log(JSON.stringify(scoped));
"""
    result = subprocess.run(
        ["node", "-e", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == [
        {
            "recommendation_id": "inside",
            "source_ad_id": "ad-inside",
            "gle_scope_verified": True,
            "gle_scope_account_id": "account-1",
            "gle_scope_account_name": "授权账户",
            "gle_scope_ad_id": "ad-inside",
        }
    ]
