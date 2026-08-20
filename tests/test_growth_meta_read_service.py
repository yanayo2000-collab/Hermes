from __future__ import annotations

import json

import pytest

from app.growth.errors import GrowthValidationError
from app.growth.meta_read_service import MetaGraphReadService


class Response:
    def __init__(self, body, *, headers=None, status_code=200):
        self.body = body
        self.headers = headers or {}
        self.status_code = status_code

    def json(self):
        return self.body

    def raise_for_status(self):
        return None


class Session:
    def __init__(self):
        self.posts = []
        self.gets = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return Response({"report_run_id": "report-1"}, headers={"x-app-usage": '{"call_count":12}'})

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if url.endswith("/120000000000001"):
            return Response({
                "id": "120000000000001", "name": "source-ad", "account_id": "123",
                "campaign_id": "120000000000003", "adset_id": "120000000000002",
                "status": "ACTIVE", "effective_status": "ACTIVE",
                "creative": {"id": "180000000000001", "image_hash": "meta-image-hash-1234", "object_story_spec": {
                    "page_id": "100000000000001", "link_data": {
                        "image_hash": "meta-image-hash-1234", "message": "body", "name": "headline",
                        "description": "description", "link": "https://example.test/app",
                        "call_to_action": {"type": "INSTALL_MOBILE_APP"},
                    },
                }},
            })
        if url.endswith("/120000000000002"):
            return Response({
                "id": "120000000000002", "name": "source-adset", "account_id": "123",
                "campaign_id": "120000000000003", "daily_budget": "4000",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP", "targeting": {"geo_locations": {"countries": ["BR"]}},
            })
        if url.endswith("/120000000000003"):
            return Response({
                "id": "120000000000003", "name": "source-campaign", "account_id": "123",
                "objective": "OUTCOME_APP_PROMOTION", "buying_type": "AUCTION",
                "daily_budget": "2200",
            })
        if url.endswith("/report-1"):
            return Response({"id": "report-1", "async_status": "Job Completed", "async_percent_completion": 100})
        if url.endswith("/report-1/insights"):
            return Response({"data": [{"ad_id": "ad-1", "spend": "10.00"}]})
        if url.endswith("/activities"):
            return Response({"data": [{"id": "activity-1", "event_type": "update_ad_set_budget"}]})
        if url.endswith("/ad-1/previews"):
            return Response({"data": [{"body": "<iframe>preview</iframe>"}]})
        if url.endswith("/adset-1"):
            return Response({
                "id": "adset-1", "status": "ACTIVE", "effective_status": "ACTIVE",
                "learning_stage_info": {"status": "LEARNING"}, "issues_info": [],
            })
        if url.endswith("/act_123"):
            return Response({
                "id": "act_123", "account_id": "123", "name": "MX Main", "account_status": 1,
                "currency": "USD", "user_tasks": ["ADVERTISE", "ANALYZE"], "capabilities": ["CAN_USE_VIDEO"],
            })
        if url.endswith("/study-1/cells"):
            return Response({"data": [{"id": "cell-1", "treatment_percentage": 50}]})
        if url.endswith("/study-1/objectives"):
            return Response({"data": [{"id": "objective-1", "type": "CONVERSIONS"}]})
        if url.endswith("/study-1"):
            return Response({"id": "study-1", "type": "SPLIT_TEST"})
        return Response({})


def _service():
    session = Session()
    service = MetaGraphReadService(
        session=session, access_token="not-logged", base_url="https://graph.facebook.com", api_version="v25.0",
    )
    return service, session


def test_ninety_day_history_uses_async_report_and_captures_usage() -> None:
    service, session = _service()
    submitted = service.submit_async_insights(account_id="act_123", since="2026-05-08", until="2026-08-05")
    assert submitted == {
        "report_run_id": "report-1", "mode": "ASYNC", "since": "2026-05-08", "until": "2026-08-05",
        "level": "ad", "meta_object_writes": 0, "rate_usage": {"x-app-usage": {"call_count": 12}},
    }
    assert session.posts[0][1]["data"]["async"] == "true"
    assert service.read_async_status("report-1")["success"] is True
    assert service.read_async_result("report-1")["data"][0]["ad_id"] == "ad-1"


def test_read_only_activity_capability_and_study_surfaces() -> None:
    service, session = _service()
    assert service.read_activities(account_id="123")["activities"][0]["id"] == "activity-1"
    account = service.read_account_capabilities(account_id="123")
    assert account["eligible_for_write_plan"] is True
    study = service.read_study_result_surface("study-1")
    assert study["study"]["type"] == "SPLIT_TEST"
    assert study["cells"][0]["treatment_percentage"] == 50
    assert study["meta_object_writes"] == 0
    assert session.posts == []


def test_preview_delivery_state_and_activity_drift_are_read_only() -> None:
    service, session = _service()
    preview = service.read_ad_preview(ad_id="ad-1", ad_format="INSTAGRAM_REELS")
    assert preview["previews"][0]["body"].startswith("<iframe")
    delivery = service.read_delivery_state(object_id="adset-1", object_type="adset")
    assert delivery["learning_stage"] == {"status": "LEARNING"}
    drift = service.detect_activity_drift(
        activities=[{
            "id": "activity-2", "object_id": "adset-1", "event_type": "update_ad_set_budget",
            "event_time": "2026-08-06T10:30:00+00:00", "actor_id": "outside", "actor_name": "Operator",
        }],
        target_object_ids=["adset-1"], approved_actor_ids=["worker"],
        cutoff_at="2026-08-06T10:00:00+00:00",
    )
    assert drift["status"] == "DRIFT_DETECTED"
    assert drift["drift_count"] == 1
    assert session.posts == []


def test_insight_range_over_ninety_three_days_is_rejected_before_network() -> None:
    service, session = _service()
    with pytest.raises(GrowthValidationError, match="meta_insights_range_invalid"):
        service.submit_async_insights(account_id="123", since="2026-01-01", until="2026-08-05")
    assert session.posts == []


def test_rebuild_source_freezes_full_hierarchy_without_meta_write() -> None:
    service, session = _service()
    result = service.read_ad_rebuild_source(ad_id="120000000000001")
    assert result["source_ids"] == {
        "campaign_id": "120000000000003", "adset_id": "120000000000002",
        "ad_id": "120000000000001", "creative_id": "180000000000001",
    }
    assert result["creative_contract"]["image_hash"] == "meta-image-hash-1234"
    assert result["creative_contract"]["primary_text"] == "body"
    assert result["campaign"]["daily_budget"] == "2200"
    campaign_get = next(call for call in session.gets if call[0].endswith("/120000000000003"))
    assert "daily_budget" in campaign_get[1]["params"]["fields"]
    assert result["meta_object_writes"] == 0
    assert session.posts == []
