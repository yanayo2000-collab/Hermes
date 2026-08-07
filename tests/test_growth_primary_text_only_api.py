from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.growth.common import canonical_json, payload_hash


def _enable_governance(path: Path) -> None:
    digest = "a" * 64
    owner = {"name": "Owner", "signed_at": "2026-08-07T00:00:00+00:00", "signature_hash": digest}
    path.write_text(json.dumps({
        "baseline": "FINAL_EXECUTION_PLAN_v1.1",
        "contract_version": "gle-phase1-governance-v1",
        "global_enabled": True,
        "mode": "LIVE_SHADOW",
        "golden_path": {"experiment_type": "COPY_ONLY", "unique_variable": "PRIMARY_TEXT"},
        "canary": {"account_ids": ["123456789"], "markets": ["MX"]},
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
            "block_all_actions": False, "block_all_meta_writes": False,
            "block_account_writes": False, "block_action_writes": False,
            "disable_evaluation_scheduler": False,
            "block_new_experiment_activation": True,
            "force_manual_review_for_uncertain_post": True,
        },
        "owners": {
            "gate_owner": owner, "business_signer": owner,
            "technical_signer": owner, "data_signer": owner,
        },
    }), encoding="utf-8")


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    db_path = tmp_path / "gle-g0-03.sqlite3"
    client = TestClient(create_app({"DB_PATH": str(db_path), "AUTH_ENABLED": False}))
    image_path = tmp_path / "winner.png"
    image_path.write_bytes(b"same-frozen-image")
    image_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    now = "2026-08-07T00:00:00+00:00"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO creative_generated_images
            (image_id,request_id,surface,image_size,market,brand,image_ref,thumbnail_ref,
             prompt_hash,risk_status,risk_tags_json,review_status,provider,metadata_json,
             created_at,image_hash)
            VALUES ('winner','request-1','feed_static','1080x1350','Mexico','Tugao',?,?,'hash',
                    'safe','[]','approved','fixture','{}',?,?)""",
            (str(image_path), str(image_path), now, image_hash),
        )
        conn.execute(
            """INSERT INTO creative_review_records
            (review_id,image_id,request_id,review_status,review_status_zh,reviewer,
             checks_json,decision_reason,created_at)
            VALUES ('review-1','winner','request-1','APPROVED','已通过','operator',
                    '{}','winner',?)""",
            (now,),
        )
        conn.commit()
    return client, db_path


def _variants(*, second_headline: str = "Same headline") -> list[dict]:
    return [
        {
            "primary_text": "Baseline primary text",
            "headline": "Same headline",
            "description": "Same description",
            "hypothesis": "baseline wording",
            "benchmark_version": "gle_copy_benchmark_v1_20260803",
        },
        {
            "primary_text": "Challenger primary text",
            "headline": second_headline,
            "description": "Same description",
            "hypothesis": "challenger wording",
            "benchmark_version": "gle_copy_benchmark_v1_20260803",
        },
    ]


def test_copy_launch_rejects_headline_as_a_second_variable(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    response = client.post(
        "/api/ops/ad-data-dashboard/new-account-launches/copy",
        headers={"Idempotency-Key": "bad-copy", "X-Request-ID": "bad-copy-request"},
        json={
            "country": "MX", "account_id": "123456789", "page_id": "998877",
            "daily_spend_target": 100, "cpi_target": 0.55,
            "frozen_creative_id": "winner",
            "copy_variants": _variants(second_headline="Changed headline"),
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["message"] == "copy_experiment_only_primary_text_may_differ"


def test_copy_plan_is_compiled_before_action_and_approval(tmp_path: Path, monkeypatch) -> None:
    governance_path = tmp_path / "governance.json"
    _enable_governance(governance_path)
    monkeypatch.setenv("GLE_PHASE1_GOVERNANCE_PATH", str(governance_path))
    client, db_path = _client(tmp_path)
    created = client.post(
        "/api/ops/ad-data-dashboard/new-account-launches/copy",
        headers={"Idempotency-Key": "copy-launch", "X-Request-ID": "copy-launch-request"},
        json={
            "country": "MX", "account_id": "123456789", "page_id": "998877",
            "daily_spend_target": 100, "cpi_target": 0.55,
            "frozen_creative_id": "winner", "copy_variants": _variants(),
        },
    )
    assert created.status_code == 201, created.text
    launch = created.json()
    checked_at = datetime.now(timezone.utc)
    preflight_expires_at = checked_at + timedelta(hours=1)
    study_start = checked_at + timedelta(hours=2)
    study_end = study_start + timedelta(days=7)
    evidence = {
        "preflight_id": "copy-preflight", "launch_id": launch["launch_id"],
        "source": "meta_graph_read_only", "status": "VERIFIED",
        "account_id": "123456789", "account_name": "Test account",
        "business_id": "business-1", "country": "MX",
        "test_variable": "copy_variant", "strategy_keys": ["BROAD", "BROAD"],
        "targeting_ids": [], "delivery_estimates": {"C1": {}, "C2": {}},
        "intersection_estimate": {}, "overlap_ratio": 0.0,
        "checked_at": checked_at.isoformat(), "expires_at": preflight_expires_at.isoformat(),
        "start_time": study_start.isoformat(), "end_time": study_end.isoformat(),
        "meta_writes_performed": False,
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO ad_audience_preflight
            (preflight_id,launch_id,account_id,business_id,country,strategy_keys_json,
             evidence_json,evidence_hash,status,checked_at,expires_at)
            VALUES (?,?,?,?,?,?,?,?, 'VERIFIED',?,?)""",
            (
                "copy-preflight", launch["launch_id"], "123456789", "business-1", "MX",
                canonical_json(["BROAD", "BROAD"]), canonical_json(evidence),
                payload_hash(evidence), checked_at.isoformat(), preflight_expires_at.isoformat(),
            ),
        )
        conn.commit()
    cells = []
    for variant in launch["variants"]:
        copy = variant["copy_variant"]
        cells.append({
            "experiment_id": variant["experiment_id"], "role": variant["role"],
            "audience_strategy": "BROAD", "adset_name": variant["meta_names"]["adset"],
            "daily_budget_usd": 20, "ad_name": variant["meta_names"]["ad"],
            "primary_text": copy["primary_text"], "headline": copy["headline"],
            "description": copy["description"], "copy_hypothesis": copy["hypothesis"],
            "copy_benchmark_version": copy["benchmark_version"],
        })
    preview = client.post(
        f"/api/ops/ad-data-dashboard/new-account-launches/{launch['launch_id']}/create-plan/preview",
        headers={"Idempotency-Key": "copy-plan", "X-Request-ID": "copy-plan-request"},
        json={
            "campaign_name": launch["variants"][0]["meta_names"]["campaign"],
            "test_variable": "copy_variant", "frozen_creative_id": "winner",
            "audience_preflight_id": "copy-preflight",
            "cells": cells,
        },
    )
    assert preview.status_code == 201, preview.text
    body = preview.json()
    plan = body["plan"]
    assert plan["experiment_type"] == "COPY_ONLY"
    assert plan["unique_variable"] == "PRIMARY_TEXT"
    assert plan["compiler_receipt"]["status"] == "PASS"
    assert body["approval"]["status"] == "PROPOSED"
    before_approval = client.post(
        f"/api/ops/ad-data-dashboard/meta-plans/{body['plan_id']}/execute",
        headers={"Idempotency-Key": "strict-dry-before-approval", "X-Request-ID": "strict-dry-before-approval-request"},
        json={"execution_mode": "dry_run"},
    )
    assert before_approval.status_code == 409
    assert before_approval.json()["detail"]["message"] == "gle_primary_text_only_approval_required_before_dry_run"
    approved = client.post(
        f"/api/ops/ad-data-dashboard/meta-plans/{body['plan_id']}/approve",
        headers={"Idempotency-Key": "strict-approval", "X-Request-ID": "strict-approval-request"},
        json={"confirmation": "APPROVE_EXACT_PLAN"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
    dry_run = client.post(
        f"/api/ops/ad-data-dashboard/meta-plans/{body['plan_id']}/execute",
        headers={"Idempotency-Key": "strict-dry", "X-Request-ID": "strict-dry-request"},
        json={"execution_mode": "dry_run"},
    )
    assert dry_run.status_code == 201, dry_run.text
    assert dry_run.json()["compiler_status"] == "PASS"
    assert dry_run.json()["compiler_receipt_hash"] == plan["compiler_receipt"]["receipt_hash"]
    with sqlite3.connect(db_path) as conn:
        action_plan, approval_plan = conn.execute(
            """SELECT json_extract(a.payload_json,'$.plan'),p.plan_json
            FROM growth_operation_action a JOIN growth_operation_approval p
              ON p.operation_action_id=a.operation_action_id
            WHERE a.operation_action_id=?""",
            (body["plan_id"],),
        ).fetchone()
    assert action_plan == approval_plan
