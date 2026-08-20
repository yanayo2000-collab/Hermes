from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ad_daily_report import ensure_ad_daily_report_tables
from app.growth.ad_experiment_service import AdExperimentService, resolve_rebuild_source_budget
from app.growth.api import create_ad_experiment_router
from app.growth.common import utc_now
from app.growth.decision_service import DecisionService
from app.growth.errors import GrowthValidationError
from app.growth.meta_graph_adapter import MetaGraphExecutionAdapter, MetaGraphWritePolicy
from app.growth.schema import ensure_growth_schema

def test_rebuild_prepare_resolves_legacy_identity_and_freezes_verified_meta_plan(tmp_path):
    db_path = tmp_path / "rebuild-prepare.sqlite3"

    class Db:
        def connect(self):
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            return conn

    class MetaResponse:
        headers = {}
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

        def raise_for_status(self):
            return None

    class ReadOnlyMetaSession:
        def __init__(self):
            self.posts = []
            self.gets = []

        def get(self, url, **kwargs):
            self.gets.append((url, kwargs))
            object_id = url.rsplit("/", 1)[-1]
            if object_id == "120000000000001":
                return MetaResponse({
                    "id": object_id, "name": "source-ad", "account_id": "123",
                    "campaign_id": "120000000000003", "adset_id": "120000000000002",
                    "status": "ACTIVE", "effective_status": "ACTIVE",
                    "creative": {"id": "180000000000001", "image_hash": "meta-image-hash-1234", "object_story_spec": {
                        "page_id": "100000000000001", "link_data": {
                            "image_hash": "meta-image-hash-1234", "message": "source body",
                            "name": "source headline", "description": "source description",
                            "link": "http://play.google.com/store/apps/details?id=com.timetrade.duitan",
                            "call_to_action": {"type": "INSTALL_MOBILE_APP"},
                        },
                    }},
                })
            if object_id == "120000000000002":
                return MetaResponse({
                    "id": object_id, "name": "source-adset", "account_id": "123",
                    "campaign_id": "120000000000003", "status": "ACTIVE",
                    "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                    "billing_event": "IMPRESSIONS", "optimization_goal": "APP_INSTALLS",
                    "targeting": {
                        "geo_locations": {"countries": ["BR"], "location_types": ["home", "recent"]},
                        "genders": [2], "age_min": 18, "age_max": 40, "locales": [16],
                        "app_install_state": "not_installed", "user_os": ["Android"],
                        "user_device": ["Android_Smartphone"],
                        "targeting_automation": {"advantage_audience": 0},
                    },
                    "promoted_object": {
                        "application_id": "1684703062404662",
                        "object_store_url": "http://play.google.com/store/apps/details?id=com.timetrade.duitan",
                    },
                    "attribution_spec": [{"event_type": "CLICK_THROUGH", "window_days": 1}],
                })
            if object_id == "120000000000003":
                return MetaResponse({
                    "id": object_id, "name": "source-campaign", "account_id": "123",
                    "status": "ACTIVE", "objective": "OUTCOME_APP_PROMOTION", "buying_type": "AUCTION",
                    "special_ad_categories": [], "daily_budget": "2200",
                })
            return MetaResponse({})

    with Db().connect() as conn:
        ensure_growth_schema(conn)
        ensure_ad_daily_report_tables(conn)
        report_payload = {
            "ad_objects": [{
                "object_id": "legacy-hash", "object_level": "ad", "account_id": "123",
                "campaign": "source-campaign", "ad_group": "source-adset", "ad": "source-ad",
                "country": "BR",
            }],
        }
        conn.execute(
            "INSERT INTO ad_daily_report VALUES (?,?,?,?,?,?,?,?,?)",
            ("report-1", "2026-08-13", "real", "v1", "v1", "", "", utc_now(), json.dumps(report_payload)),
        )
        recommendation_payload = {
            "recommendation_id": "reco-rebuild", "object_id": "legacy-hash",
            "object_level": "ad", "project": "unknown_project", "country": "BR",
            "primary_action": "repair_delivery_config",
            "evidence": {"funnel_metrics": {"target_app": "unknown_project"}},
        }
        conn.execute(
            """INSERT INTO ad_recommendation
            (recommendation_id,report_id,object_id,primary_action,primary_action_zh,confidence,status_tag,payload_json,decision_context_json,data_origin)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("reco-rebuild", "report-1", "legacy-hash", "repair_delivery_config", "重建受控投放", "medium", "under_delivery", json.dumps(recommendation_payload), "{}", "NATIVE_V2"),
        )
        conn.execute(
            """CREATE TABLE ad_creative_asset (
            account_id TEXT,campaign_id TEXT,adset_id TEXT,ad_id TEXT,ad_name TEXT,
            creative_id TEXT,last_seen_at TEXT)"""
        )
        conn.execute(
            "INSERT INTO ad_creative_asset VALUES (?,?,?,?,?,?,?)",
            ("123", "120000000000003", "120000000000002", "120000000000001", "source-ad", "180000000000001", utc_now()),
        )
        decision = DecisionService(conn).create_decision(
            recommendation_id="reco-rebuild", selected_action="CREATE_EXPERIMENT",
            decision_reason={"type": "REBUILD"}, confidence=0.8,
            idempotency_key="decision-rebuild",
        )
        experiment = AdExperimentService(conn).create_draft(
            {
                "target_app": "unknown_project", "country": "BR", "experiment_type": "NEW_AD_TEST",
                "source_recommendation_id": "reco-rebuild", "source_ad_id": "legacy-hash",
                "hypothesis_json": {"recommended_action": "CREATE_EXPERIMENT"},
            }, actor="operator", idempotency_key="experiment-rebuild",
        )
        DecisionService(conn).bind_target(
            decision["decision_id"], target_type="EXPERIMENT",
            target_id=experiment["experiment_id"], actor="operator",
        )
        approved_path = tmp_path / "approved-rebuild.png"
        approved_path.write_bytes(b"approved-rebuild-image")
        conn.executescript(
            """
            CREATE TABLE creative_pro_work_queue (
                job_id TEXT PRIMARY KEY,status TEXT,generation_plan_json TEXT,material_refs_json TEXT
            );
            CREATE TABLE creative_generated_images (
                image_id TEXT PRIMARY KEY,request_id TEXT,image_ref TEXT,image_hash TEXT,
                review_status TEXT,metadata_json TEXT,created_at TEXT
            );
            CREATE TABLE creative_review_records (
                review_id TEXT PRIMARY KEY,image_id TEXT,review_status TEXT,created_at TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO creative_pro_work_queue VALUES (?,?,?,?)",
            (
                "job-rebuild", "completed", json.dumps({"generation_request_id": "request-rebuild"}),
                json.dumps({"growth_experiment_id": experiment["experiment_id"]}),
            ),
        )
        conn.execute(
            "INSERT INTO creative_generated_images VALUES (?,?,?,?,?,?,?)",
            (
                "approved-rebuild", "request-rebuild", str(approved_path), "approved-image-hash",
                "approved", json.dumps({"job_id": "job-rebuild"}), utc_now(),
            ),
        )
        conn.execute(
            "INSERT INTO creative_review_records VALUES (?,?,?,?)",
            ("review-rebuild", "approved-rebuild", "APPROVED", utc_now()),
        )
        conn.commit()

    session = ReadOnlyMetaSession()
    app = FastAPI()
    app.include_router(create_ad_experiment_router(
        db=Db(), require_admin=lambda request: {"user_id": "operator"},
        meta_session=session, meta_access_token="not-logged",
        meta_graph_root="https://graph.facebook.com/v25.0",
    ))
    client = TestClient(app)
    response = client.post(
        f"/api/ops/ad-data-dashboard/experiments/{experiment['experiment_id']}/rebuild-plan/prepare",
        headers={"Idempotency-Key": "prepare-rebuild", "X-Request-ID": "prepare-request"},
        json={"approved_image_id": "approved-rebuild"},
    )
    assert response.status_code == 201, response.json()
    result = response.json()
    assert result["meta_object_writes"] == 0
    assert result["approval"]["status"] == "PROPOSED"
    assert result["plan"]["steps"]["IMAGE_UPLOAD"]["image_id"] == "approved-rebuild"
    assert result["plan"]["steps"]["ADSET_CREATE"]["bid_strategy"] == "COST_CAP"
    assert result["plan"]["steps"]["ADSET_CREATE"]["bid_amount"] == 30
    assert result["plan"]["steps"]["ADSET_CREATE"]["targeting"]["locales"] == [16]
    assert result["plan"]["after_json"]["budget_mode"] == "CBO"
    assert result["plan"]["after_json"]["campaign"]["daily_budget_usd"] == 22
    assert "daily_budget" not in result["plan"]["steps"]["ADSET_CREATE"]
    assert result["plan"]["reuse_campaign_id"] == "120000000000003"
    assert result["plan"]["initial_status"] == "PAUSED"
    assert result["plan"]["max_write_requests"] == 4
    assert "CAMPAIGN_CREATE" not in result["plan"]["steps"]
    assert session.posts == []

    active_response = client.post(
        f"/api/ops/ad-data-dashboard/experiments/{experiment['experiment_id']}/rebuild-plan/prepare",
        headers={"Idempotency-Key": "prepare-rebuild-active", "X-Request-ID": "prepare-active-request"},
        json={
            "creation_scope": "REUSE_CAMPAIGN_NEW_ADSET", "initial_status": "ACTIVE",
            "approved_image_id": "approved-rebuild",
        },
    )
    assert active_response.status_code == 201, active_response.json()
    active_plan = active_response.json()["plan"]
    assert active_plan["initial_status"] == "ACTIVE"
    assert active_plan["steps"]["ADSET_CREATE"]["status"] == "ACTIVE"
    assert active_plan["steps"]["AD_CREATE"]["status"] == "ACTIVE"
    assert "CAMPAIGN_CREATE" not in active_plan["steps"]
    with Db().connect() as conn:
        stored = AdExperimentService(conn).get(experiment["experiment_id"])
        assert stored["state"] == "WAITING_CREATE_APPROVAL"
        assert stored["target_app"] == "tugao"
        assert stored["account_id"] == "123"
        assert stored["source_ad_id"] == "120000000000001"


def test_rebuild_source_budget_distinguishes_abo_and_cbo() -> None:
    assert resolve_rebuild_source_budget(
        {"daily_budget": "4000"}, {},
    ) == {
        "budget_mode": "ABO", "daily_budget_usd": 40.0,
        "adset_daily_budget": 4000, "campaign_daily_budget": None,
    }
    assert resolve_rebuild_source_budget(
        {}, {"daily_budget": "2200"},
    ) == {
        "budget_mode": "CBO", "daily_budget_usd": 22.0,
        "adset_daily_budget": None, "campaign_daily_budget": 2200,
    }


@pytest.mark.parametrize(
    ("adset", "campaign", "error"),
    [
        ({}, {}, "rebuild_source_daily_budget_missing"),
        ({"lifetime_budget": "4000"}, {}, "rebuild_source_lifetime_budget_not_supported"),
        ({"daily_budget": "4000"}, {"daily_budget": "2200"}, "rebuild_source_budget_owner_ambiguous"),
        ({}, {"daily_budget": "200"}, "rebuild_source_daily_budget_out_of_range"),
    ],
)
def test_rebuild_source_budget_fails_closed_for_unsafe_contracts(adset, campaign, error) -> None:
    with pytest.raises(GrowthValidationError, match=error):
        resolve_rebuild_source_budget(adset, campaign)


class _Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class _RecordingSession:
    def __init__(self):
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _Response({"id": "created-object"})

    def get(self, url, **kwargs):
        return _Response({})


def test_cbo_rebuild_does_not_send_an_adset_budget(tmp_path: Path) -> None:
    payload = {
        "account_id": "act_123", "action_type": "CREATE_PAUSED_AD",
        "approval": {"approval_id": "approval-cbo", "status": "APPROVED", "approved_by": "operator", "approved_at": "2026-08-20T00:00:00+00:00"},
        "plan": {
            "reuse_campaign_id": "campaign-cbo", "initial_status": "ACTIVE", "budget_mode": "CBO", "max_write_requests": 4,
            "steps": {"ADSET_CREATE": {"name": "cbo-adset", "status": "ACTIVE"}},
        },
    }
    session = _RecordingSession()
    adapter = MetaGraphExecutionAdapter(
        session=session, access_token="secret-not-logged",
        policy=MetaGraphWritePolicy(enabled=True, allowed_account_ids=frozenset({"123"}), image_root=str(tmp_path)),
    )
    adapter.execute_step("ADSET_CREATE", payload, {"campaign_id": "campaign-cbo"})
    adset_post = next(kwargs for url, kwargs in session.posts if url.endswith("/adsets"))
    assert adset_post["data"]["campaign_id"] == "campaign-cbo"
    assert adset_post["data"]["status"] == "ACTIVE"
    assert "daily_budget" not in adset_post["data"]
