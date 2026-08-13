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
