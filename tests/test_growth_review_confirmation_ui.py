from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DECISION_JS = ROOT / "app/static/ops/growth-decision.js"
MAIN_PAGES = ROOT / "app/main_pages.py"


def test_poor_scorecard_maps_to_data_review_and_bulk_candidate() -> None:
    source = DECISION_JS.read_text(encoding="utf-8")
    instrumented = source.rsplit("})();", 1)[0] + "\nwindow.__growthDecisionTest = {actionFromRecommendation, safeBulkCandidates};\n})();\n"
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
    outside = {**row, "recommendation_id": "review-outside", "gle_scope_verified": False}
    harness = f"""
global.window = global;
global.document = {{ addEventListener() {{}} }};
window.dailyRecommendationDecisionAction = row => row.evidence.scorecard.band === 'poor' ? 'manual_review' : 'observe';
window.dailyRecommendationRowsForBulk = () => [{json.dumps(row)}, {json.dumps(outside)}];
{instrumented}
const item = window.dailyRecommendationRowsForBulk()[0];
console.log(JSON.stringify({{
  action: window.__growthDecisionTest.actionFromRecommendation(item),
  bulkCount: window.__growthDecisionTest.safeBulkCandidates().length
}}));
"""
    result = subprocess.run(
        ["node", "-e", harness],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {"action": "CHECK_DATA", "bulkCount": 1}


def test_review_buttons_explain_effect_and_open_bulk_confirmation() -> None:
    page = MAIN_PAGES.read_text(encoding="utf-8")
    decision = DECISION_JS.read_text(encoding="utf-8")

    assert "window.dailyRecommendationDecisionAction=row=>dailyRecoDisplayAction(row);" in page
    assert "function gleScopedRecommendationRows" in page
    assert "const sourceAdId=String(row&&row.source_ad_id||'').trim()" in page
    assert "gle_scope_verified:true" in page
    assert "data-growth-bulk-confirm>确认加入复核队列" in page
    assert "不会暂停、降预算、放量或修改 Meta" in page
    assert "growth-decision.js?v=20260812-review-scope-v1" in page

    assert "actionFromRecommendation(row) === 'CHECK_DATA'" in decision
    assert "row.gle_scope_verified === true" in decision
    assert "String(row.gle_scope_ad_id || '') === String(row.source_ad_id || '')" in decision
    assert "仅限已授权给 GLE 的 5 个广告账户" in decision
    assert "每条广告均已通过 exact ad_id 匹配授权范围" in decision
    assert "确认加入数据复核队列" in decision
    assert "Meta 写入 0" in decision
    assert "确认后将这条表现偏弱广告加入经营数据复核队列" in decision


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
