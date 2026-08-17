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
  repairAction: window.__growthDecisionTest.actionFromRecommendation({{action_type: 'generate_repair_creative'}}),
  deliveryRepairAction: window.__growthDecisionTest.actionFromRecommendation({{action_type: 'repair_delivery_config'}}),
  postImAction: window.__growthDecisionTest.actionFromRecommendation({{action_type: 'inspect_post_im_funnel'}})
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
        "deliveryRepairAction": "CREATE_EXPERIMENT",
        "postImAction": "CHECK_DATA",
    }


def test_review_ui_is_automatic_and_surfaces_each_ads_next_step() -> None:
    page = MAIN_PAGES.read_text(encoding="utf-8")
    decision = DECISION_JS.read_text(encoding="utf-8")

    assert "window.dailyRecommendationDecisionAction=row=>dailyRecoDisplayAction(row);" in page
    assert "function gleScopedRecommendationRows" in page
    assert "function gleRecommendationScope" in page
    assert "function dailyRecoHistoryRouting" in page
    assert "function gleCoverageRecommendationIndex" in page
    assert "function gleCoverageNextStep" in page
    assert "function gleOperatingWorkItems" in page
    assert "function renderGleRecommendationWorkbench" in page
    assert "function openGleSystemReviewQueue" in page
    assert "function mergeDailyDecisionStates" in page
    assert "const sourceAdId=String(row&&row.source_ad_id||'').trim()" in page
    assert "gle_scope_verified:true" in page
    assert "data-growth-bulk-confirm" not in page
    assert "查看账户明细" not in page
    assert "系统核对低投放原因" in page
    assert "不会按日历无限延长观察期" not in page
    assert "近7天零交付" in page
    assert "数据同步或初始窗口待就绪" in page
    assert "本轮已经完成评分" in page
    assert "data-gle-open-operating-workbench" in page
    assert "window.showGleOperatingStatus" in page
    assert "&refresh=1" in page
    assert "growth-decision.js?v=20260813-gle-submit-followup-v1" in page
    assert "growth-workspace.js?v=20260816-gle-auto-recovery-loop-v1" in page

    assert "generate_repair_creative: 'CREATE_EXPERIMENT'" in decision
    assert "repair_delivery_config: 'CREATE_EXPERIMENT'" in decision
    assert "inspect_post_im_funnel: 'CHECK_DATA'" in decision
    workspace = (ROOT / "app/static/ops/growth-workspace.js").read_text(encoding="utf-8")
    assert "id=\"growthCostCapUsd\"" in workspace
    assert "['generate_creative','generate_repair_creative'].includes(raw)" in workspace
    assert "cpi_target:Number(recommendation.cpi_target" in workspace
    assert "growthBulkDecisionModal" not in decision
    assert "确认加入复核队列" not in decision
    assert "系统已完成当前只读复核" in decision
    assert "返回 GLE 工作台" in decision
    assert "查看并审批" in decision
    assert "button.dataset.targetExperimentId = experimentId" in decision
    assert "if (result && result.experiment_id) close();" not in decision
    assert "window.refreshGleDecisionSurface=async()=>" in page

    history_start = page.index("function renderDailyRecommendationTable")
    history_end = page.index("function renderDailyReport", history_start)
    history_source = page[history_start:history_end]
    assert "data-growth-decision" not in history_source
    assert "data-creative-from-reco" not in history_source
    assert "data-gle-open-recommendation-queue" in history_source
    assert "routing.label" in history_source
    assert "__gle_history_routing:dailyRecoHistoryRouting(row)" in history_source
    assert "进入 GLE 确认" in page
    assert "未纳入 GLE · 仅供分析" in page
    assert "renderDailyRecommendationTable(currentDailyReport.recommendations)" in page


def test_submitted_experiment_stays_visible_and_opens_the_exact_approval_task() -> None:
    source = DECISION_JS.read_text(encoding="utf-8")
    assert source.index("renderAcceptedState(completedDecision);") < source.index(
        "await refreshRecommendationPanel();",
        source.index("renderAcceptedState(completedDecision);"),
    )
    assert "target_type: acceptedResult.experiment_id ? 'EXPERIMENT'" in source
    assert "target_id: acceptedResult.experiment_id || acceptedDecision.target_id" in source
    assert "await window.openGleExperimentTask(experimentId);" in source
    assert "方案已生成，等待你审批并完成 dry-run。" in source


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
      zeroDelivery: gleCoverageNextStep(null, {{ready: false, monitoringStatus: 'NO_DELIVERY_IN_COMPLETE_WINDOW', deliveryDiagnosis: {{same_adset_delivering_ads: 2}}}}),
      zeroDelivery48h: gleCoverageNextStep(null, {{ready: false, monitoringStatus: 'NO_LIFETIME_DELIVERY_AFTER_48H', deliveryDiagnosis: {{same_adset_delivering_ads: 1}}}}),
  scoring: gleCoverageNextStep(null, {{ready: true}}),
  inactive: gleCoverageNextStep(null, {{active: false, ready: false}}),
  task: gleCoverageNextStep(null, {{ready: true, task: true}}),
  actionableTask: gleCoverageNextStep(null, {{ready: true, task: true, taskActionable: true}}),
  taskWithPause: gleCoverageNextStep({{action: 'pause', status: '严重超阈值'}}, {{ready: true, task: true}}),
  actionableTaskWithPause: gleCoverageNextStep({{action: 'pause', status: '严重超阈值'}}, {{ready: true, task: true, taskActionable: true}})
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
    assert payload["review"]["label"] == "系统核对低投放原因"
    assert payload["review"]["bucket"] == "system"
    assert "不再无限等待" in payload["review"]["detail"]
    assert payload["pause"]["label"] == "确认暂停"
    assert payload["pause"]["action"] == "decision"
    assert payload["waiting"]["label"] == "数据同步或初始窗口待就绪"
    assert payload["zeroDelivery"]["label"] == "确认重建投放"
    assert payload["zeroDelivery"]["bucket"] == "confirm"
    assert payload["zeroDelivery"]["action"] == "rebuild"
    assert "同广告组已有 2 条广告获得交付" in payload["zeroDelivery"]["detail"]
    assert payload["zeroDelivery48h"]["label"] == "48小时零消耗，确认重建"
    assert payload["zeroDelivery48h"]["bucket"] == "confirm"
    assert "不能删除共享广告组" in payload["zeroDelivery48h"]["detail"]
    assert payload["scoring"]["label"] == "本轮未形成可评分样本"
    assert payload["inactive"]["label"] == "当前未投放"
    assert payload["task"]["label"] == "查看任务进度"
    assert payload["actionableTask"]["label"] == "处理任务"
    assert payload["actionableTask"]["bucket"] == "confirm"
    assert payload["taskWithPause"]["label"] == "确认暂停"
    assert payload["taskWithPause"]["action"] == "decision"
    assert payload["actionableTaskWithPause"]["label"] == "处理任务"


def test_operating_workbench_assigns_one_next_step_to_every_covered_ad() -> None:
    page = MAIN_PAGES.read_text(encoding="utf-8")
    start = page.index("function gleCoverageNextStep")
    end = page.index("function gleOperatingItemMatches", start)
    function_source = page[start:end]
    coverage = {
        "accounts": [
            {
                "account_id": "account-1",
                "account_name": "授权账户",
                "items": [
                    {
                        "ad_id": f"ad-{index}",
                        "ad_name": f"广告 {index}",
                        "effective_status": "ACTIVE" if index < 76 else "PAUSED",
                        "monitoring_status": (
                            "METRIC_OBSERVATION_AVAILABLE"
                            if index < 70
                            else "WAITING_FOR_DASHBOARD_FACTS"
                        ),
                    }
                    for index in range(93)
                ],
            }
        ]
    }
    harness = f"""
const currentGleAdCoverage = {json.dumps(coverage)};
const currentDailyReport = {{recommendations: []}};
const dailyRecoDisplayAction = row => row.action;
const dailyRecoHumanStatus = row => row.status || '表现一般';
const dailyRecoNeedsOperator = row => ['pause', 'reduce_budget'].includes(row.action);
const dailyRecoHasDecision = row => Boolean(row.decision_state && row.decision_state.decision_id);
const dailyRecoDisplayActionZh = row => row.action;
const gleRecommendationActionLabel = row => '查看并确认';
const gleCoverageRecommendationIndex = () => new Map();
const gleCoverageTask = () => null;
const gleCoverageTaskState = () => ({{actionable: false}});
{function_source}
const work = gleOperatingWorkItems(currentGleAdCoverage, currentDailyReport);
console.log(JSON.stringify({{
  total: work.length,
  unique: new Set(work.map(row => row.item.ad_id)).size,
  withNextStep: work.filter(row => row.nextStep && row.nextStep.label).length,
  buckets: work.reduce((acc,row)=>{{acc[row.nextStep.bucket]=(acc[row.nextStep.bucket]||0)+1;return acc;}},{{}})
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
    assert payload["total"] == 93
    assert payload["unique"] == 93
    assert payload["withNextStep"] == 93
    assert sum(payload["buckets"].values()) == 93


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


def test_six_authorized_pending_rows_share_one_count_and_outside_rows_stay_readonly() -> None:
    page = MAIN_PAGES.read_text(encoding="utf-8")
    scope_start = page.index("function gleReviewScopeIndex")
    scope_end = page.index("function gleCoverageRecommendationIndex", scope_start)
    scope_source = page[scope_start:scope_end]
    summary_start = page.index("function gleRecommendationSummary")
    summary_end = page.index("function openGleRecommendationQueue", summary_start)
    summary_source = page[summary_start:summary_end]
    routing_start = page.index("function dailyRecoHistoryRouting")
    routing_end = page.index("const recoFilterDefs", routing_start)
    routing_source = page[routing_start:routing_end]

    authorized_ids = [f"authorized-{index}" for index in range(6)]
    outside_ids = [f"outside-{index}" for index in range(15)]
    coverage = {
        "accounts": [
            {
                "account_id": "account-authorized",
                "account_name": "GLE 授权账户",
                "items": [{"ad_id": ad_id} for ad_id in authorized_ids],
            }
        ]
    }
    recommendations = [
        {
            "recommendation_id": f"recommendation-{ad_id}",
            "source_ad_id": ad_id,
            "data_origin": "NATIVE_V2",
            "action": "repair_delivery_config",
        }
        for ad_id in authorized_ids + outside_ids
    ]
    work_items = [
        {
            "nextStep": {"bucket": "confirm"},
            "recommendation": row,
        }
        for row in recommendations[:6]
    ]
    harness = f"""
const currentGleAdCoverage = {json.dumps(coverage)};
const dailyRecoNeedsOperator = row => row && row.action === 'repair_delivery_config';
const dailyRecoDisplayAction = row => row && row.action;
const dailyRecoIsSystemReview = () => false;
{scope_source}
{summary_source}
{routing_source}
const recommendations = {json.dumps(recommendations)};
const workItems = {json.dumps(work_items)};
const summary = gleRecommendationSummary({{recommendations}}, workItems);
const routing = recommendations.map(row => dailyRecoHistoryRouting(row, currentGleAdCoverage));
console.log(JSON.stringify({{
  scoped: gleScopedRecommendationRows(recommendations, currentGleAdCoverage).length,
  summaryTotal: summary.total,
  confirmLabels: routing.filter(item => item.actionable).map(item => item.label),
  outsideLabels: routing.filter(item => !item.gleScoped).map(item => item.label)
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
    assert payload["scoped"] == 6
    assert payload["summaryTotal"] == 6
    assert payload["confirmLabels"] == ["进入 GLE 确认"] * 6
    assert payload["outsideLabels"] == ["未纳入 GLE · 仅供分析"] * 15
