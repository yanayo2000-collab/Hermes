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
        "evidence": {"scorecard": {"band": "poor"}},
    }
    harness = f"""
global.window = global;
global.document = {{ addEventListener() {{}} }};
window.dailyRecommendationDecisionAction = row => row.evidence.scorecard.band === 'poor' ? 'manual_review' : 'observe';
window.dailyRecommendationRowsForBulk = () => [{json.dumps(row)}];
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
    assert "data-growth-bulk-confirm>确认加入复核队列" in page
    assert "不会暂停、降预算、放量或修改 Meta" in page
    assert "growth-decision.js?v=20260812-review-confirmation-v2" in page

    assert "actionFromRecommendation(row) === 'CHECK_DATA'" in decision
    assert "确认加入数据复核队列" in decision
    assert "Meta 写入 0" in decision
    assert "确认后将这条表现偏弱广告加入经营数据复核队列" in decision
