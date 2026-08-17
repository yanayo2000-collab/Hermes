from __future__ import annotations

from pathlib import Path

from app.growth.meta_read_service import MetaGraphReadService


ROOT = Path(__file__).resolve().parents[1]


class _Response:
    headers = {}
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body

    def raise_for_status(self):
        return None


class _ReadOnlySession:
    def __init__(self):
        self.posts = []

    def get(self, url, **kwargs):
        object_id = url.rsplit("/", 1)[-1]
        if object_id == "120000000000001":
            return _Response({
                "id": object_id, "name": "source-ad", "account_id": "123",
                "campaign_id": "120000000000003", "adset_id": "120000000000002",
                "creative": {
                    "id": "180000000000001", "image_hash": "meta-image-hash",
                    "object_story_spec": {"page_id": "100000000000001", "link_data": {
                        "image_hash": "meta-image-hash", "message": "body", "name": "headline",
                        "description": "description", "link": "https://example.test/app",
                        "call_to_action": {"type": "INSTALL_MOBILE_APP"},
                    }},
                },
            })
        if object_id == "120000000000002":
            return _Response({
                "id": object_id, "name": "source-adset", "account_id": "123",
                "campaign_id": "120000000000003", "daily_budget": "4000",
                "targeting": {"geo_locations": {"countries": ["BR"]}},
            })
        return _Response({
            "id": "120000000000003", "name": "source-campaign", "account_id": "123",
        })


def test_exact_rebuild_source_is_get_only_and_freezes_hierarchy() -> None:
    session = _ReadOnlySession()
    result = MetaGraphReadService(
        session=session, access_token="not-logged",
        base_url="https://graph.facebook.com", api_version="v25.0",
    ).read_ad_rebuild_source(ad_id="120000000000001")

    assert result["source_ids"] == {
        "campaign_id": "120000000000003", "adset_id": "120000000000002",
        "ad_id": "120000000000001", "creative_id": "180000000000001",
    }
    assert result["creative_contract"]["image_hash"] == "meta-image-hash"
    assert result["meta_object_writes"] == 0
    assert session.posts == []


def test_bulk_rebuild_ui_is_scoped_persistent_and_never_enables_delivery() -> None:
    page = (ROOT / "app/main_pages.py").read_text()
    workspace = (ROOT / "app/static/ops/growth-workspace.js").read_text()

    assert "批量审批重建投放" in page
    assert "dailyRecoDisplayAction(work.recommendation)==='repair_delivery_config'" in page
    assert "growth-bulk-rebuild-approval-v1" in workspace
    assert "gle-bulk-rebuild:${batchId}:${recommendationId}:${phase}" in workspace
    assert "confirmation:'CREATE_PAUSED_OBJECTS'" in workspace
    bulk_block = workspace[workspace.index("async function executeBulkRebuildItem"):workspace.index("async function runBulkRebuildBatch")]
    assert "ENABLE_DELIVERY" not in bulk_block
    assert "MANUAL_REVIEW" in workspace


def test_bulk_rebuild_opens_a_top_level_modal_without_switching_tabs() -> None:
    page = (ROOT / "app/main_pages.py").read_text()
    workspace = (ROOT / "app/static/ops/growth-workspace.js").read_text()
    start = page.index("function openGleBulkRebuildApproval")
    end = page.index("function renderGleRecommendationWorkbench", start)
    helper = page[start:end]

    assert "setGleWorkspaceView('tasks'" not in helper
    assert "window.GrowthWorkspace.openBulkRebuildApproval(prepared)" in helper
    assert "/gle-ad-coverage/rebuild-recommendations" in helper
    assert "node.id='growthBulkRebuildModal'" in workspace
    assert "document.body.appendChild(node)" in workspace
    assert ".growth-bulk-modal-layer" in workspace
    assert "position:fixed;z-index:1900" in workspace
    assert "addEventListener('click',()=>openGleBulkRebuildApproval(" in page


def test_bulk_rebuild_storage_is_compact_and_fails_closed() -> None:
    workspace = (ROOT / "app/static/ops/growth-workspace.js").read_text()

    assert "function bulkRebuildRecommendationSnapshot" in workspace
    assert "snapshot.objective={cpi_target:source.objective.cpi_target}" in workspace
    assert "window.sessionStorage.setItem(BULK_REBUILD_STORAGE_KEY,serialized)" in workspace
    assert "showBulkRebuildModal(batch,{allowPending:true,persisted})" in workspace
    assert "persisted&&allowPending&&pending" in workspace
    assert "本条尚未发起，批次已安全停止" in workspace


def test_bulk_rebuild_modal_separates_confirmation_from_progress() -> None:
    workspace = (ROOT / "app/static/ops/growth-workspace.js").read_text()

    assert "确认批量重建" in workspace
    assert "批量重建进度" in workspace
    assert "确认后系统会做什么" in workspace
    assert "开始重建 ${pending} 条广告" in workspace
    assert "总体进度 ${progress}%" in workspace
    assert "优先处理" in workspace
    assert "等待处理的广告" in workspace
    assert "role=\"progressbar\"" in workspace
    assert "role=\"status\" aria-live=\"polite\"" in workspace


def test_bulk_rebuild_modal_humanizes_exceptions_and_keeps_raw_detail_collapsed() -> None:
    workspace = (ROOT / "app/static/ops/growth-workspace.js").read_text()

    assert "function bulkRebuildErrorGuidance" in workspace
    assert "语言定向需要核对" in workspace
    assert "系统已停止该条，不会自动重试" in workspace
    assert "<summary>技术详情</summary>" in workspace
    assert "查看并处理" in workspace
    assert "查看重建结果" in workspace
    assert "growth-bulk-priority" in workspace


def test_bulk_rebuild_modal_is_bounded_and_keeps_actions_visible() -> None:
    workspace = (ROOT / "app/static/ops/growth-workspace.js").read_text()

    assert "max-height:min(760px,calc(100vh - 36px))" in workspace
    assert ".growth-bulk-modal-layer .growth-modal-body{min-height:0;overflow:auto" in workspace
    assert ".growth-bulk-modal-layer .growth-modal-foot{flex:0 0 auto" in workspace
    assert "growth-bulk-group-list{max-height:250px;overflow:auto" in workspace
    assert "requestAnimationFrame(()=>{const target=node.querySelector('#growthConfirmBulkRebuild:not([hidden])')" in workspace
