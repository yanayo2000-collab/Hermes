from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.growth.approval_service import OperationApprovalService
from app.growth.common import canonical_json as db_json, payload_hash, utc_now
from app.growth.errors import GrowthStateConflict, GrowthValidationError
from app.growth.execution_service import ExecutionTaskService
from app.growth.meta_execution_worker import MetaExecutionWorker
from app.growth.meta_graph_adapter import MetaGraphExecutionAdapter, MetaGraphWritePolicy
from app.growth.new_account_autopilot import NewAccountLaunchAutopilot
from app.growth.primary_text_only_compiler import (
    MAX_APPROVED_ASSET_BYTES,
    assert_phase1_live_permission,
    attach_compiler_receipt,
    canonical_json,
    compile_primary_text_only_plan,
    is_primary_text_only_plan,
    require_human_approver,
    verify_compiler_receipt,
)
from app.growth.schema import ensure_growth_schema


def _cell(key: str, role: str, message: str) -> dict:
    return {
        "cell_key": key,
        "experiment_id": f"experiment-{key}",
        "experiment_code": f"EXP-{key}",
        "role": role,
        "creative_direction": {"key": "points_reward"},
        "audience_strategy": {
            "strategy_key": "BROAD", "label": "Broad", "detailed_targeting": {},
            "meta_targeting_ids": [], "verification_status": "VERIFIED_EMPTY_BY_DEFINITION",
        },
        "allocation_percent": 50,
        "study_cell_name": f"Study-{key}",
        "frozen_creative_id": "image-1",
        "copy_version_id": f"copy-{key}",
        "copy_benchmark_version": "benchmark-v1",
        "copy_hypothesis": f"hypothesis-{key}",
        "steps": {
            "IMAGE_UPLOAD": {"image_id": "image-1", "image_path": "/tmp/image.png"},
            "CREATIVE_CREATE": {
                "name": f"Creative-{key}",
                "object_story_spec": {
                    "page_id": "page-1",
                    "link_data": {
                        "link": "https://example.com/app",
                        "message": message,
                        "name": "Same headline",
                        "description": "Same description",
                        "call_to_action": {
                            "type": "INSTALL_MOBILE_APP",
                            "value": {"link": "https://example.com/app"},
                        },
                    },
                },
            },
            "ADSET_CREATE": {
                "name": f"AdSet-{key}",
                "daily_budget": 2000,
                "optimization_goal": "APP_INSTALLS",
                "billing_event": "IMPRESSIONS",
                "bid_strategy": "COST_CAP",
                "bid_amount": 55,
                "targeting": {
                    "geo_locations": {"countries": ["MX"], "location_types": ["home", "recent"]},
                    "genders": [2], "age_min": 18, "age_max": 40, "locales": [23],
                    "app_install_state": "not_installed", "user_os": ["Android_ver_8.0_and_above"],
                    "user_device": ["Android_Smartphone"],
                    "targeting_automation": {"advantage_audience": 0},
                },
                "promoted_object": {"application_id": "app-1", "object_store_url": "https://example.com/app"},
                "attribution_spec": [{"event_type": "CLICK_THROUGH", "window_days": 1}],
                "status": "PAUSED",
            },
            "AD_CREATE": {"name": f"Ad-{key}", "status": "PAUSED"},
        },
        "asset_sha256": "a" * 64,
    }


def plan() -> dict:
    checked_at = datetime.now(timezone.utc)
    preflight_expires_at = checked_at + timedelta(hours=1)
    study_start = checked_at + timedelta(hours=2)
    study_end = study_start + timedelta(days=7)
    return {
        "plan_id": "plan-1",
        "plan_version": "NEW_ACCOUNT_COPY_BATCH_V1",
        "launch_id": "launch-1",
        "experiment_id": "experiment-C1",
        "experiment_ids": ["experiment-C1", "experiment-C2"],
        "experiment_type": "COPY_ONLY",
        "unique_variable": "PRIMARY_TEXT",
        "action_type": "CREATE_PAUSED_AD",
        "target_account_id": "account-1",
        "target_object_type": "LAUNCH",
        "target_object_id": "launch-1",
        "campaign": {
            "name": "Campaign", "objective": "OUTCOME_APP_PROMOTION",
            "buying_type": "AUCTION", "special_ad_categories": [], "status": "PAUSED",
        },
        "cells": [_cell("C1", "BASELINE", "Baseline text"), _cell("C2", "CHALLENGER", "Challenger text")],
        "baseline_experiment_id": "experiment-C1",
        "test_variable": "copy_variant",
        "sdk_contract_version": "gle-meta-sdk-v1",
        "copy_benchmark_versions": ["benchmark-v1"],
        "frozen_creative_id": "image-1",
        "study": {
            "business_id": "business-1", "name": "Study", "type": "SPLIT_TEST",
            "start_time": study_start.isoformat(), "end_time": study_end.isoformat(),
        },
        "audience_preflight": {
            "preflight_id": "preflight-1", "launch_id": "launch-1",
            "source": "meta_graph_read_only", "status": "VERIFIED",
            "account_id": "account-1", "account_name": "Account 1",
            "business_id": "business-1", "country": "MX",
            "test_variable": "copy_variant", "strategy_keys": ["BROAD", "BROAD"],
            "targeting_ids": [], "delivery_estimates": {"C1": {}, "C2": {}},
            "intersection_estimate": {}, "overlap_ratio": 0.0,
            "checked_at": checked_at.isoformat(),
            "expires_at": preflight_expires_at.isoformat(),
            "start_time": study_start.isoformat(), "end_time": study_end.isoformat(),
            "meta_writes_performed": False,
        },
        "invariants": {
            "base_conditions": {
                "country": "MX", "country_label": "Mexico", "gender": "female",
                "gender_label": "Female", "age_min": 18, "age_max": 40,
                "language": "es_419", "language_label": "Spanish",
            },
            "audience_strategy": {
                "strategy_key": "BROAD", "label": "Broad", "detailed_targeting": {},
                "meta_targeting_ids": [], "verification_status": "VERIFIED_EMPTY_BY_DEFINITION",
            },
            "advantage_audience": "DISABLED", "gender_as_suggestion": False,
            "age_as_suggestion": False, "frozen_creative_id": "image-1",
            "single_variable": "copy_variant", "randomization": "META_SPLIT_TEST_REQUIRED",
            "copy_versions": ["copy-C1", "copy-C2"], "budget_mode": "ABO",
            "equal_daily_budget_usd": 20.0, "bid_strategy": "COST_CAP",
            "cost_cap_usd": 0.55, "optimization_goal": "APP_INSTALLS",
            "placement": "ADVANTAGE_PLUS",
            "attribution": "1d_click_1d_view_1d_engaged_video_view",
        },
        "delivery_guardrails": {
            "version": "v1",
            "ctr_floor": {"minimum_impressions": 800, "minimum_ctr": 0.012, "action": "PAUSE_AD"},
            "zero_install_spend": {
                "minimum_attribution_hours": 24, "spend_limit_usd": 1.2,
                "maximum_installs": 0, "action": "PAUSE_AD",
            },
            "high_cpi": {"minimum_installs": 10, "maximum_cpi_usd": 0.55, "action": "PAUSE_AD"},
        },
        "max_write_requests": 10,
        "execution_policy": {
            "live_creation_allowed": True, "blocked_reason": "",
            "required_readback": ["study_id", "cell_ids", "adset_ids", "ads", "strict_targeting"],
        },
        "evaluation_window": {"checkpoints": ["D1", "D3", "D7"]},
        "market_profile": {},
        "expires_at": "2099-01-01T00:00:00+00:00",
    }


def test_passes_when_only_primary_text_and_identity_names_differ() -> None:
    receipt = compile_primary_text_only_plan(plan())
    assert receipt["status"] == "PASS"
    assert receipt["reason_codes"] == []
    assert len(receipt["changed_paths"]) == 2
    assert len(set(receipt["cell_primary_text_hashes"].values())) == 2


def test_creative_direction_is_invariant_configuration() -> None:
    value = plan()
    value["cells"][1]["creative_direction"] = {
        "key": "copy_variant", "code": "CV2", "title": "challenger",
    }
    assert compile_primary_text_only_plan(value)["reason_codes"] == ["INVARIANT_PROJECTION_MISMATCH"]


def test_receipt_is_stable_across_object_key_order() -> None:
    value = plan()
    reordered = {key: value[key] for key in reversed(list(value))}
    assert compile_primary_text_only_plan(value) == compile_primary_text_only_plan(reordered)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda value: value["cells"][1]["steps"]["CREATIVE_CREATE"]["object_story_spec"]["link_data"].update(message="Baseline text"), "PRIMARY_TEXT_NOT_DISTINCT"),
        (lambda value: value["cells"][1]["steps"]["CREATIVE_CREATE"]["object_story_spec"]["link_data"].update(name="Different headline"), "INVARIANT_PROJECTION_MISMATCH"),
        (lambda value: value["cells"][1]["steps"]["ADSET_CREATE"].update(daily_budget=2100), "INVARIANT_PROJECTION_MISMATCH"),
        (lambda value: value["cells"][1]["steps"]["IMAGE_UPLOAD"].update(image_id="image-2"), "FROZEN_CREATIVE_ID_MISMATCH"),
        (lambda value: value.update(cells=value["cells"][:1]), "EXACTLY_TWO_CELLS_REQUIRED"),
        (lambda value: value["cells"][1].update(allocation_percent=40), "ALLOCATION_NOT_50_50"),
        (lambda value: value["campaign"].update(status="ACTIVE"), "OBJECTS_NOT_PAUSED"),
        (lambda value: value.update(expires_at=""), "APPROVAL_TTL_REQUIRED"),
        (lambda value: (value["study"].update(start_time="not-a-time"), value["audience_preflight"].update(start_time="not-a-time")), "STUDY_TIME_INVALID"),
        (lambda value: value["audience_preflight"].update(delivery_estimates="bad"), "AUDIENCE_PREFLIGHT_SCHEMA_INVALID"),
        (lambda value: value.update(market_profile={"country": "CO"}), "MARKET_PROFILE_SCHEMA_INVALID"),
        (lambda value: value.update(extra_field=True), "UNKNOWN_OR_MISSING_PLAN_FIELD"),
        (lambda value: value["cells"][0].update(extra_field=True), "UNKNOWN_OR_MISSING_CELL_FIELD"),
    ],
)
def test_rejects_non_golden_path_differences(mutate, reason: str) -> None:
    value = plan()
    mutate(value)
    receipt = compile_primary_text_only_plan(value)
    assert receipt["status"] == "FAIL"
    assert receipt["reason_codes"] == [reason]


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda value: value["cells"][1].update(cell_key="c1"), "CELL_KEY_ROLE_BINDING_INVALID"),
        (lambda value: value["cells"][1].update(copy_version_id="copy-C1"), "COPY_VERSION_ID_INVALID"),
        (lambda value: value["cells"][1].update(study_cell_name="Study-C1"), "STUDY_CELL_NAME_INVALID"),
        (lambda value: value["cells"][1].update(experiment_code="EXP-C1"), "CELL_EXPERIMENT_CODE_INVALID"),
        (lambda value: value["invariants"].update(frozen_creative_id="other"), "FROZEN_CREATIVE_ID_MISMATCH"),
        (lambda value: value["cells"][1].update(frozen_creative_id="other"), "FROZEN_CREATIVE_ID_MISMATCH"),
        (lambda value: value["invariants"].update(copy_versions=["copy-C2", "copy-C1"]), "COPY_VERSIONS_MISMATCH"),
        (lambda value: value.update(copy_benchmark_versions=["other"]), "COPY_BENCHMARK_VERSIONS_MISMATCH"),
    ],
)
def test_rejects_ambiguous_cell_and_alias_identities(mutate, reason: str) -> None:
    value = plan()
    mutate(value)
    assert compile_primary_text_only_plan(value)["reason_codes"] == [reason]


def test_array_order_is_significant() -> None:
    value = plan()
    extra = {"event_type": "VIEW_THROUGH", "window_days": 1}
    for cell in value["cells"]:
        cell["steps"]["ADSET_CREATE"]["attribution_spec"].append(deepcopy(extra))
    value["cells"][1]["steps"]["ADSET_CREATE"]["attribution_spec"].reverse()
    assert compile_primary_text_only_plan(value)["reason_codes"] == ["INVARIANT_PROJECTION_MISMATCH"]


def test_production_shaped_multifield_copy_plan_is_red_fixture() -> None:
    value = plan()
    value["cells"][1]["steps"]["CREATIVE_CREATE"]["object_story_spec"]["link_data"].update({
        "name": "Production candidate changed headline",
        "description": "Production candidate changed description",
    })
    assert compile_primary_text_only_plan(value)["reason_codes"] == ["INVARIANT_PROJECTION_MISMATCH"]


def test_attached_receipt_detects_plan_and_receipt_tampering() -> None:
    compiled = attach_compiler_receipt(plan())
    assert verify_compiler_receipt(compiled)["status"] == "PASS"
    tampered = deepcopy(compiled)
    tampered["cells"][1]["steps"]["CREATIVE_CREATE"]["object_story_spec"]["link_data"]["message"] = "Third text"
    with pytest.raises(GrowthStateConflict, match="receipt_mismatch"):
        verify_compiler_receipt(tampered)
    tampered = deepcopy(compiled)
    tampered["compiler_receipt"]["receipt_hash"] = "0" * 64
    with pytest.raises(GrowthStateConflict, match="receipt_mismatch"):
        verify_compiler_receipt(tampered)


def test_invalid_strings_and_nonfinite_numbers_fail_closed() -> None:
    value = plan()
    value["campaign"]["name"] = " Campaign "
    assert compile_primary_text_only_plan(value)["reason_codes"] == ["CANONICAL_STRING_INVALID"]
    with pytest.raises(Exception):
        canonical_json({"value": float("nan")})
    assert compile_primary_text_only_plan([])["reason_codes"] == ["PLAN_NOT_OBJECT"]


def test_attach_rejects_invalid_plan() -> None:
    value = plan()
    value["cells"][1]["steps"]["AD_CREATE"]["status"] = "ACTIVE"
    with pytest.raises(GrowthValidationError, match="OBJECTS_NOT_PAUSED"):
        attach_compiler_receipt(value)


@pytest.mark.parametrize(
    "actor",
    ["", "internal-system", "growth-autopilot", "growth-autopilot-recovery",
     "bot", "cron", "worker-1", "growth-agent", "service-account", "system:foo"],
)
def test_automated_actors_cannot_approve(actor: str) -> None:
    with pytest.raises(GrowthStateConflict, match="human_approval_required"):
        require_human_approver(actor)


def test_named_operator_can_approve() -> None:
    require_human_approver("operator:nana")


def test_legacy_copy_variant_without_explicit_contract_is_fail_closed_candidate() -> None:
    assert is_primary_text_only_plan({"test_variable": "copy_variant"}) is True


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_growth_schema(conn)
    conn.executescript(
        """
        CREATE TABLE creative_generated_images (
            image_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            surface TEXT NOT NULL,
            image_size TEXT NOT NULL,
            market TEXT NOT NULL,
            brand TEXT NOT NULL,
            image_ref TEXT NOT NULL,
            thumbnail_ref TEXT NOT NULL,
            prompt_hash TEXT NOT NULL,
            risk_status TEXT NOT NULL,
            risk_tags_json TEXT NOT NULL,
            review_status TEXT NOT NULL,
            provider TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            image_hash TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE creative_review_records (
            review_id TEXT PRIMARY KEY,
            image_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            review_status TEXT NOT NULL,
            review_status_zh TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            checks_json TEXT NOT NULL,
            decision_reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    now = utc_now()
    conn.execute(
        """INSERT INTO growth_context_snapshot
        (context_snapshot_id,app_id,snapshot_hash,created_at)
        VALUES ('context-1','tugao','context-hash',?)""",
        (now,),
    )
    conn.execute(
        """INSERT INTO growth_decision
        (decision_id,recommendation_id,context_snapshot_id,selected_action,
         decision_reason_json,confidence,idempotency_key,request_hash,created_at,updated_at)
        VALUES ('decision-1','recommendation-1','context-1','CREATE_PAUSED_AD',
                '{}',1.0,'decision-key','decision-hash',?,?)""",
        (now, now),
    )
    conn.commit()
    return conn


def _materialize_runtime_evidence(conn: sqlite3.Connection, candidate: dict) -> None:
    asset = tempfile.NamedTemporaryFile(prefix="gle-g0-03-asset-", suffix=".bin", delete=False)
    try:
        asset.write(b"approved-frozen-image")
        asset.flush()
    finally:
        asset.close()
    asset_path = Path(asset.name)
    asset_hash = hashlib.sha256(asset_path.read_bytes()).hexdigest()
    for cell in candidate["cells"]:
        cell["asset_sha256"] = asset_hash
        cell["steps"]["IMAGE_UPLOAD"]["image_path"] = str(asset_path)
    now = utc_now()
    conn.execute(
        """INSERT OR REPLACE INTO creative_generated_images
        (image_id,request_id,surface,image_size,market,brand,image_ref,thumbnail_ref,
         prompt_hash,risk_status,risk_tags_json,review_status,provider,metadata_json,
         created_at,image_hash)
        VALUES ('image-1','request-g0-03','feed_static','1080x1350','Mexico','Tugao',
                ?,?,'fixture-hash','safe','[]','approved','fixture','{}',?,?)""",
        (str(asset_path), str(asset_path), now, asset_hash),
    )
    conn.execute(
        """INSERT OR REPLACE INTO creative_review_records
        (review_id,image_id,request_id,review_status,review_status_zh,reviewer,
         checks_json,decision_reason,created_at)
        VALUES ('review-g0-03','image-1','request-g0-03','APPROVED','已通过',
                'operator:fixture','{}','frozen fixture',?)""",
        (now,),
    )
    evidence = candidate["audience_preflight"]
    conn.execute(
        """INSERT OR REPLACE INTO ad_audience_preflight
        (preflight_id,launch_id,account_id,business_id,country,strategy_keys_json,
         evidence_json,evidence_hash,status,checked_at,expires_at)
        VALUES (?,?,?,?,?,?,?,?, 'VERIFIED',?,?)""",
        (
            evidence["preflight_id"], evidence["launch_id"], evidence["account_id"],
            evidence["business_id"], evidence["country"], db_json(evidence["strategy_keys"]),
            db_json(evidence), payload_hash(evidence), evidence["checked_at"], evidence["expires_at"],
        ),
    )
    conn.commit()


@pytest.mark.parametrize("mutation", ["missing_review", "empty_registry_hash"])
def test_action_requires_approved_asset_registry_provenance(
    monkeypatch, tmp_path, mutation: str,
) -> None:
    config_path = tmp_path / "governance.json"
    _enabled_governance(config_path)
    monkeypatch.setenv("GLE_PHASE1_GOVERNANCE_PATH", str(config_path))
    conn = _db()
    candidate = plan()
    _materialize_runtime_evidence(conn, candidate)
    if mutation == "missing_review":
        conn.execute("DELETE FROM creative_review_records WHERE image_id='image-1'")
    else:
        conn.execute("UPDATE creative_generated_images SET image_hash='' WHERE image_id='image-1'")
    conn.commit()
    compiled = attach_compiler_receipt(candidate)
    with pytest.raises(GrowthStateConflict, match="asset_provenance_mismatch"):
        ExecutionTaskService(conn).create_operation_action(
            decision_id="decision-1", action_type="CREATE_PAUSED_AD",
            target_type="LAUNCH", target_id="launch-1", payload={"plan": compiled},
            action_scope="EXPERIMENT", idempotency_key="missing-asset-provenance",
        )
    assert conn.execute("SELECT COUNT(*) FROM growth_operation_action").fetchone()[0] == 0


def test_action_rejects_oversized_approved_asset_without_buffering(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "governance.json"
    _enabled_governance(config_path)
    monkeypatch.setenv("GLE_PHASE1_GOVERNANCE_PATH", str(config_path))
    conn = _db()
    candidate = plan()
    _materialize_runtime_evidence(conn, candidate)
    asset_path = Path(candidate["cells"][0]["steps"]["IMAGE_UPLOAD"]["image_path"])
    with asset_path.open("r+b") as handle:
        handle.truncate(MAX_APPROVED_ASSET_BYTES + 1)
    oversized_hash = "b" * 64
    for cell in candidate["cells"]:
        cell["asset_sha256"] = oversized_hash
    conn.execute(
        "UPDATE creative_generated_images SET image_hash=? WHERE image_id='image-1'",
        (oversized_hash,),
    )
    conn.commit()
    compiled = attach_compiler_receipt(candidate)
    with pytest.raises(GrowthStateConflict, match="asset_too_large"):
        ExecutionTaskService(conn).create_operation_action(
            decision_id="decision-1", action_type="CREATE_PAUSED_AD",
            target_type="LAUNCH", target_id="launch-1", payload={"plan": compiled},
            action_scope="EXPERIMENT", idempotency_key="oversized-asset",
        )
    assert conn.execute("SELECT COUNT(*) FROM growth_operation_action").fetchone()[0] == 0


def test_actual_autopilot_advance_stops_strict_plan_at_human_approval() -> None:
    strict_plan = attach_compiler_receipt(plan())

    class _Rows:
        def __init__(self, *, rows=None, row=None) -> None:
            self._rows = rows or []
            self._row = row

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return self._row

    class _Conn:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _sql, _params=()):
            self.calls += 1
            if self.calls == 1:
                return _Rows(rows=[{"id": "C1"}, {"id": "C2"}])
            return _Rows(row={"operation_action_id": "plan-1"})

    class _Experiments:
        @staticmethod
        def _serialize(row):
            key = row["id"]
            return {
                "experiment_id": f"experiment-{key}", "experiment_code": key,
                "country": "MX", "state": "PLANNED", "hypothesis_json": {
                    "experiment_mode": "creative_direction",
                },
            }

        @staticmethod
        def plan_detail(_plan_id):
            return {"plan_id": "plan-1", "plan": strict_plan, "approval": {"status": "PROPOSED"}}

    autopilot = NewAccountLaunchAutopilot.__new__(NewAccountLaunchAutopilot)
    autopilot.conn = _Conn()
    autopilot.experiments = _Experiments()
    autopilot.meta_adapter = None
    autopilot._ensure_creatives = lambda _launch_id, _experiments: {
        "exhausted": [], "waiting": [],
    }
    autopilot._account_recovery = lambda _launch_id: {}
    result = autopilot.advance("launch-1", allow_live=True)
    assert result == {
        "launch_id": "launch-1", "status": "WAITING_HUMAN_APPROVAL", "plan_id": "plan-1",
    }


def _action_and_approval(
    conn: sqlite3.Connection, *, expires_at: str | None = None,
) -> tuple[dict, dict, dict]:
    candidate = plan()
    if expires_at is not None:
        candidate["expires_at"] = expires_at
    _materialize_runtime_evidence(conn, candidate)
    compiled = attach_compiler_receipt(candidate)
    action = ExecutionTaskService(conn).create_operation_action(
        decision_id="decision-1",
        action_type="CREATE_PAUSED_AD",
        target_type="LAUNCH",
        target_id="launch-1",
        payload={"plan": compiled},
        action_scope="EXPERIMENT",
        created_by="operator:planner",
        idempotency_key="action-key",
    )
    approval = OperationApprovalService(conn).propose(
        action["operation_action_id"],
        compiled,
        proposed_by="operator:planner",
        idempotency_key="approval-key",
        expires_at=compiled["expires_at"],
    )
    return compiled, action, approval


def test_approval_propose_rejects_action_plan_mismatch() -> None:
    conn = _db()
    candidate = plan()
    _materialize_runtime_evidence(conn, candidate)
    compiled = attach_compiler_receipt(candidate)
    action = ExecutionTaskService(conn).create_operation_action(
        decision_id="decision-1", action_type="CREATE_PAUSED_AD",
        target_type="LAUNCH", target_id="launch-1", payload={"plan": compiled},
        action_scope="EXPERIMENT", idempotency_key="action-key",
    )
    changed = deepcopy(compiled)
    changed["cells"][1]["steps"]["CREATIVE_CREATE"]["object_story_spec"]["link_data"]["message"] = "Changed"
    changed = attach_compiler_receipt(changed)
    with pytest.raises(GrowthStateConflict, match="action_plan_mismatch"):
        OperationApprovalService(conn).propose(
            action["operation_action_id"], changed,
            proposed_by="operator:planner", idempotency_key="approval-key",
            expires_at=changed["expires_at"],
        )


def test_action_creation_rejects_confused_deputy_tuple_and_legacy_candidate() -> None:
    conn = _db()
    compiled = attach_compiler_receipt(plan())
    with pytest.raises(GrowthStateConflict, match="action_binding_mismatch"):
        ExecutionTaskService(conn).create_operation_action(
            decision_id="decision-1", action_type="REACTIVATE_AD",
            target_type="AD", target_id="ad-999", payload={"plan": compiled},
            action_scope="EXPERIMENT", idempotency_key="confused-deputy",
        )


def test_default_off_blocks_plan_gate_and_approval_proposal(monkeypatch, tmp_path) -> None:
    conn = _db()
    candidate = plan()
    _materialize_runtime_evidence(conn, candidate)
    compiled = attach_compiler_receipt(candidate)
    action = ExecutionTaskService(conn).create_operation_action(
        decision_id="decision-1", action_type="CREATE_PAUSED_AD",
        target_type="LAUNCH", target_id="launch-1", payload={"plan": compiled},
        action_scope="EXPERIMENT", idempotency_key="enabled-approval-action",
    )
    disabled = tmp_path / "disabled-plan-approval-governance.json"
    _enabled_governance(disabled, block_all_meta_writes=True)
    monkeypatch.setenv("GLE_PHASE1_GOVERNANCE_PATH", str(disabled))
    with pytest.raises(GrowthStateConflict, match="gate_not_pass"):
        assert_phase1_live_permission(compiled)
    with pytest.raises(GrowthStateConflict, match="gate_not_pass"):
        ExecutionTaskService(conn).create_operation_action(
            decision_id="decision-1", action_type="CREATE_PAUSED_AD",
            target_type="LAUNCH", target_id="launch-1", payload={"plan": compiled},
            action_scope="EXPERIMENT", idempotency_key="default-off-action",
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM growth_idempotency_record WHERE route_key='operation_action.create' AND idempotency_key='default-off-action'"
    ).fetchone()[0] == 0
    with pytest.raises(GrowthStateConflict, match="gate_not_pass"):
        OperationApprovalService(conn).propose(
            action["operation_action_id"], compiled,
            proposed_by="operator:planner", idempotency_key="default-off-approval",
            expires_at=compiled["expires_at"],
        )
    with pytest.raises(GrowthStateConflict, match="compiler_receipt_missing"):
        ExecutionTaskService(conn).create_operation_action(
            decision_id="decision-1", action_type="CREATE_PAUSED_AD",
            target_type="LAUNCH", target_id="launch-1",
            payload={"plan": {"test_variable": "copy_variant"}},
            action_scope="EXPERIMENT", idempotency_key="legacy-copy",
        )


def test_strict_approval_requires_unexpired_human_actor() -> None:
    conn = _db()
    _, _, approval = _action_and_approval(conn)
    with pytest.raises(GrowthStateConflict, match="human_approval_required"):
        OperationApprovalService(conn).transition(
            approval["approval_id"], "APPROVED", actor="growth-autopilot",
        )
    accepted = OperationApprovalService(conn).transition(
        approval["approval_id"], "APPROVED", actor="operator:reviewer",
    )
    assert accepted["status"] == "APPROVED"

    expired = _db()
    with pytest.raises(GrowthStateConflict, match="approval_expired"):
        _action_and_approval(expired, expires_at="2000-01-01T00:00:00+00:00")

def _insert_matching_dry_run(conn: sqlite3.Connection, action_id: str, compiled: dict) -> None:
    receipt = compiled["compiler_receipt"]
    response = {
        "plan_id": action_id,
        "status": "DRY_RUN_VERIFIED",
        "execution_mode": "dry_run",
        "plan_hash": payload_hash(compiled),
        "approval_status": "APPROVED",
        "approval_id": conn.execute(
            "SELECT approval_id FROM growth_operation_approval WHERE operation_action_id=?",
            (action_id,),
        ).fetchone()[0],
        "approved_by": conn.execute(
            "SELECT approved_by FROM growth_operation_approval WHERE operation_action_id=?",
            (action_id,),
        ).fetchone()[0],
        "compiler_receipt_hash": receipt["receipt_hash"],
        "compiler_plan_core_hash": receipt["plan_core_hash"],
    }
    conn.execute(
        """INSERT INTO growth_idempotency_record
        (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
        VALUES ('ad_experiment.plan_dry_run','dry-key','dry-hash',200,?,?)""",
        (db_json(response), utc_now()),
    )
    conn.commit()


def test_default_off_gate_blocks_enqueue_without_consuming_approval(monkeypatch, tmp_path) -> None:
    conn = _db()
    compiled, action, approval = _action_and_approval(conn)
    approval = OperationApprovalService(conn).transition(
        approval["approval_id"], "APPROVED", actor="operator:reviewer",
    )
    _insert_matching_dry_run(conn, action["operation_action_id"], compiled)
    config_path = tmp_path / "disabled-governance.json"
    _enabled_governance(config_path, block_all_meta_writes=True)
    monkeypatch.setenv("GLE_PHASE1_GOVERNANCE_PATH", str(config_path))
    with pytest.raises(GrowthStateConflict, match="gate_not_pass"):
        ExecutionTaskService(conn).enqueue_task(
            action["operation_action_id"],
            idempotency_key="live-key",
            payload={
                "execution_mode": "live", "approval_id": approval["approval_id"],
                "plan": compiled, "account_id": "account-1",
            },
        )
    stored = OperationApprovalService(conn).get(approval["approval_id"])
    assert stored["consumed_at"] == ""
    assert conn.execute("SELECT COUNT(*) FROM meta_execution_task").fetchone()[0] == 0


def test_dry_run_compiler_hash_mismatch_blocks_before_consumption(monkeypatch, tmp_path) -> None:
    conn = _db()
    compiled, action, approval = _action_and_approval(conn)
    approval = OperationApprovalService(conn).transition(
        approval["approval_id"], "APPROVED", actor="operator:reviewer",
    )
    _insert_matching_dry_run(conn, action["operation_action_id"], compiled)
    conn.execute(
        "UPDATE growth_idempotency_record SET response_json=json_set(response_json,'$.compiler_receipt_hash',?)",
        ("0" * 64,),
    )
    conn.commit()
    with pytest.raises(GrowthStateConflict, match="matching_dry_run_required"):
        ExecutionTaskService(conn).enqueue_task(
            action["operation_action_id"], idempotency_key="live-key",
            payload={
                "execution_mode": "live", "approval_id": approval["approval_id"],
                "plan": compiled, "account_id": "account-1",
            },
        )
    assert OperationApprovalService(conn).get(approval["approval_id"])["consumed_at"] == ""


def test_fake_receipt_and_unapproved_continuation_cannot_unlock_live(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "governance.json"
    _enabled_governance(config_path)
    monkeypatch.setenv("GLE_PHASE1_GOVERNANCE_PATH", str(config_path))
    conn = _db()
    compiled, action, approval = _action_and_approval(conn)
    approval = OperationApprovalService(conn).transition(
        approval["approval_id"], "APPROVED", actor="operator:reviewer",
    )
    _insert_matching_dry_run(conn, action["operation_action_id"], compiled)
    conn.execute(
        "UPDATE growth_idempotency_record SET response_json=json_set(response_json,'$.execution_mode','fake')"
    )
    conn.commit()
    base_payload = {
        "execution_mode": "live", "approval_id": approval["approval_id"],
        "plan": compiled, "account_id": "account-1",
    }
    with pytest.raises(GrowthStateConflict, match="matching_dry_run_required"):
        ExecutionTaskService(conn).enqueue_task(
            action["operation_action_id"], idempotency_key="fake-unlock", payload=base_payload,
        )
    conn.execute(
        "UPDATE growth_idempotency_record SET response_json=json_set(response_json,'$.execution_mode','dry_run')"
    )
    conn.commit()
    with pytest.raises(GrowthStateConflict, match="execution_payload_not_allowed"):
        ExecutionTaskService(conn).enqueue_task(
            action["operation_action_id"], idempotency_key="continuation-injection",
            payload={
                **base_payload,
                "continuation": {
                    "plan_hash": payload_hash(compiled),
                    "completed_steps": ["CAMPAIGN_CREATE"],
                    "meta_object_ids": {"campaign_id": "arbitrary-existing"},
                },
            },
        )


@pytest.mark.parametrize("mutation", ["missing", "expired", "hash_mismatch"])
def test_central_plan_gate_requires_current_server_owned_preflight(mutation: str) -> None:
    conn = _db()
    candidate = plan()
    _materialize_runtime_evidence(conn, candidate)
    if mutation == "missing":
        conn.execute("DELETE FROM ad_audience_preflight")
    elif mutation == "expired":
        conn.execute("UPDATE ad_audience_preflight SET status='EXPIRED'")
    else:
        conn.execute("UPDATE ad_audience_preflight SET evidence_hash=?", ("0" * 64,))
    conn.commit()
    compiled = attach_compiler_receipt(candidate)
    with pytest.raises(GrowthStateConflict, match="server_preflight_mismatch"):
        ExecutionTaskService(conn).create_operation_action(
            decision_id="decision-1", action_type="CREATE_PAUSED_AD",
            target_type="LAUNCH", target_id="launch-1", payload={"plan": compiled},
            action_scope="EXPERIMENT", idempotency_key=f"server-preflight-{mutation}",
        )
    assert conn.execute("SELECT COUNT(*) FROM growth_operation_action").fetchone()[0] == 0


class _CountingAdapter:
    live_writes_enabled = True

    def __init__(self) -> None:
        self.execute_calls = 0

    def execute_step(self, step, payload, object_ids):
        self.execute_calls += 1
        return {"status": "SUCCESS"}

    def verify_step(self, step, payload, object_ids):
        return {"status": "SUCCESS"}


class _NoPostSession:
    def __init__(self) -> None:
        self.post_calls = 0

    def post(self, *args, **kwargs):
        self.post_calls += 1
        raise AssertionError("POST must not occur for a changed approved image")


def test_adapter_rehashes_immutable_image_bytes_before_any_post(tmp_path) -> None:
    image_path = tmp_path / "approved.png"
    approved_bytes = b"approved-image-bytes"
    image_path.write_bytes(approved_bytes)
    candidate = plan()
    approved_hash = hashlib.sha256(approved_bytes).hexdigest()
    for cell in candidate["cells"]:
        cell["asset_sha256"] = approved_hash
        cell["steps"]["IMAGE_UPLOAD"]["image_path"] = str(image_path)
    compiled = attach_compiler_receipt(candidate)
    image_path.write_bytes(b"replaced-after-approval")
    session = _NoPostSession()
    adapter = MetaGraphExecutionAdapter(
        session=session, access_token="test-token",
        policy=MetaGraphWritePolicy(enabled=True, allowed_account_ids=frozenset({"account-1"})),
    )
    payload = {
        "account_id": "account-1", "action_type": "CREATE_PAUSED_AD", "plan": compiled,
        "approval": {
            "status": "APPROVED", "approval_id": "approval-1",
            "approved_by": "operator:reviewer", "approved_at": "2099-01-01T00:00:00+00:00",
        },
    }
    with pytest.raises(GrowthValidationError, match="asset_sha256_mismatch"):
        adapter.execute_step("C1_IMAGE_UPLOAD", payload, {})
    assert session.post_calls == 0


def test_worker_checks_all_assets_before_first_adapter_call(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "governance.json"
    _enabled_governance(config_path)
    monkeypatch.setenv("GLE_PHASE1_GOVERNANCE_PATH", str(config_path))
    conn = _db()
    compiled, action, approval = _action_and_approval(conn)
    approval = OperationApprovalService(conn).transition(
        approval["approval_id"], "APPROVED", actor="operator:reviewer",
    )
    _insert_matching_dry_run(conn, action["operation_action_id"], compiled)
    task = ExecutionTaskService(conn).enqueue_task(
        action["operation_action_id"], idempotency_key="asset-live",
        payload={
            "execution_mode": "live", "approval_id": approval["approval_id"],
            "account_id": "account-1", "plan": compiled,
        },
    )
    assert task["status"] == "QUEUED"
    first_asset = Path(compiled["cells"][0]["steps"]["IMAGE_UPLOAD"]["image_path"])
    first_asset.write_bytes(b"changed-after-approval")
    adapter = _CountingAdapter()
    result = MetaExecutionWorker(
        ExecutionTaskService(conn), adapter, worker_id="worker-asset", execution_mode="live",
    ).run_once()
    assert result["status"] == "MANUAL_REVIEW"
    assert result["error_code"] == "gle_primary_text_only_write_blocked"
    assert adapter.execute_calls == 0


def test_worker_rechecks_default_off_gate_before_every_adapter_write(monkeypatch, tmp_path) -> None:
    conn = _db()
    compiled, action, approval = _action_and_approval(conn)
    approval = OperationApprovalService(conn).transition(
        approval["approval_id"], "APPROVED", actor="operator:reviewer",
    )
    config_path = tmp_path / "disabled-worker-governance.json"
    _enabled_governance(config_path, block_all_meta_writes=True)
    monkeypatch.setenv("GLE_PHASE1_GOVERNANCE_PATH", str(config_path))
    consumed_at = utc_now()
    conn.execute(
        "UPDATE growth_operation_approval SET consumed_at=? WHERE approval_id=?",
        (consumed_at, approval["approval_id"]),
    )
    conn.execute(
        "UPDATE growth_operation_action SET status='QUEUED' WHERE operation_action_id=?",
        (action["operation_action_id"],),
    )
    payload = {
        "execution_mode": "live", "action_type": "CREATE_PAUSED_AD", "plan": compiled,
        "account_id": "account-1",
        "approval": {
            "approval_id": approval["approval_id"], "status": "APPROVED",
            "approved_by": "operator:reviewer", "consumed_at": consumed_at,
        },
    }
    conn.execute(
        """INSERT INTO meta_execution_task
        (execution_task_id,operation_action_id,idempotency_key,request_hash,status,payload_json,created_at,updated_at)
        VALUES ('task-1',?,'task-key','task-hash','QUEUED',?,?,?)""",
        (action["operation_action_id"], db_json(payload), utc_now(), utc_now()),
    )
    conn.commit()
    adapter = _CountingAdapter()
    result = MetaExecutionWorker(
        ExecutionTaskService(conn), adapter, worker_id="worker-1", execution_mode="live",
    ).run_once()
    assert result["status"] == "MANUAL_REVIEW"
    assert result["error_code"] == "gle_primary_text_only_write_blocked"
    assert adapter.execute_calls == 0
    blocked = conn.execute(
        "SELECT step_status,step_result_json FROM meta_execution_task_receipt WHERE execution_task_id='task-1'"
    ).fetchone()
    assert blocked["step_status"] == "FAILED"
    assert json.loads(blocked["step_result_json"])["write_performed"] is False


def _enabled_governance(path, *, block_all_meta_writes: bool = False) -> None:
    digest = "a" * 64
    owner = {
        "name": "Named Owner", "signed_at": "2026-08-07T00:00:00+00:00",
        "signature_hash": digest,
    }
    path.write_text(json.dumps({
        "baseline": "FINAL_EXECUTION_PLAN_v1.1",
        "contract_version": "gle-phase1-governance-v1",
        "global_enabled": True,
        "mode": "LIVE_SHADOW",
        "golden_path": {"experiment_type": "COPY_ONLY", "unique_variable": "PRIMARY_TEXT"},
        "canary": {"account_ids": ["account-1"], "markets": ["MX"]},
        "action_allowlist": ["CREATE_CANARY_PAUSED"],
        "gates": {
            "gate_0": {"status": "PASS", "receipt_hash": digest},
            "gate_1": {"status": "PASS", "receipt_hash": digest},
            "gate_2": {"status": "NOT_STARTED", "receipt_hash": None},
            "gate_3": {"status": "NOT_STARTED", "receipt_hash": None},
        },
        "canonical_versions": {
            key: {"version": f"{key}-v1", "hash": digest}
            for key in ("schema", "evaluator", "policy", "dataset")
        },
        "kill_switches": {
            "block_all_actions": False,
            "block_all_meta_writes": block_all_meta_writes,
            "block_account_writes": False,
            "block_action_writes": False,
            "disable_evaluation_scheduler": False,
            "block_new_experiment_activation": True,
            "force_manual_review_for_uncertain_post": True,
        },
        "owners": {
            "gate_owner": owner, "business_signer": owner,
            "technical_signer": owner, "data_signer": owner,
        },
    }), encoding="utf-8")


@pytest.fixture(autouse=True)
def _enable_e00_for_strict_service_tests(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "autouse-governance.json"
    _enabled_governance(config_path)
    monkeypatch.setenv("GLE_PHASE1_GOVERNANCE_PATH", str(config_path))


def test_central_gate_allows_only_matching_contract_and_worker_rechecks_kill_switch(
    monkeypatch, tmp_path,
) -> None:
    config_path = tmp_path / "governance.json"
    _enabled_governance(config_path)
    monkeypatch.setenv("GLE_PHASE1_GOVERNANCE_PATH", str(config_path))
    conn = _db()
    compiled, action, approval = _action_and_approval(conn)
    approval = OperationApprovalService(conn).transition(
        approval["approval_id"], "APPROVED", actor="operator:reviewer",
    )
    _insert_matching_dry_run(conn, action["operation_action_id"], compiled)
    task = ExecutionTaskService(conn).enqueue_task(
        action["operation_action_id"], idempotency_key="live-key",
        payload={
            "execution_mode": "live", "approval_id": approval["approval_id"],
            "plan": compiled, "account_id": "account-1",
        },
    )
    assert task["status"] == "QUEUED"
    assert OperationApprovalService(conn).get(approval["approval_id"])["consumed_at"]

    class TripKillSwitchAdapter(_CountingAdapter):
        def execute_step(self, step, payload, object_ids):
            result = super().execute_step(step, payload, object_ids)
            _enabled_governance(config_path, block_all_meta_writes=True)
            return result

    adapter = TripKillSwitchAdapter()
    result = MetaExecutionWorker(
        ExecutionTaskService(conn), adapter, worker_id="worker-1", execution_mode="live",
    ).run_once()
    assert result["status"] == "MANUAL_REVIEW"
    assert result["error_code"] == "gle_primary_text_only_write_blocked"
    assert adapter.execute_calls == 1
