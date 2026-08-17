from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.creative_image_generation import ensure_creative_image_generation_tables
from app.growth.decision_service import DecisionService
from app.growth.errors import GrowthStateConflict
from app.growth.execution_service import ExecutionTaskService
from app.growth.schema import ensure_growth_schema


def _connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_growth_schema(conn)
    ensure_creative_image_generation_tables(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_recommendation (
            recommendation_id TEXT PRIMARY KEY,
            data_origin TEXT NOT NULL DEFAULT 'NATIVE_V2',
            payload_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO ad_recommendation (recommendation_id,payload_json) VALUES ('reco-1','{}')"
    )
    conn.commit()
    return conn


def _seed_experiment_and_image(conn: sqlite3.Connection, *, image_id: str = "pro_img_new") -> None:
    now = "2026-08-17T00:00:00+00:00"
    conn.execute(
        """
        INSERT INTO ad_experiment
        (experiment_id,experiment_code,target_app,country,platform,account_id,
         experiment_type,state,created_at,updated_at)
        VALUES ('adexp-1','EXP-BR-1','tugao','BR','meta','123',
                'NEW_AD_TEST','CREATING_PAUSED_OBJECTS',?,?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO creative_generated_images
        (image_id,request_id,surface,image_size,market,brand,image_ref,thumbnail_ref,
         prompt_hash,risk_status,risk_tags_json,review_status,provider,metadata_json,
         created_at,file_path)
        VALUES (?,?,'feed','1024x1024','BR','Premiou',?,'','hash','passed','[]',
                'approved','test','{}',?,?)
        """,
        (image_id, f"request-{image_id}", f"/tmp/{image_id}.png", now, f"/tmp/{image_id}.png"),
    )
    conn.commit()


def _verified_creation_action(
    conn: sqlite3.Connection, *, image_id: str = "pro_img_new",
) -> tuple[ExecutionTaskService, dict, dict]:
    decision = DecisionService(conn).create_decision(
        recommendation_id="reco-1",
        selected_action="CREATE_PAUSED_AD",
        decision_reason={"type": "ZERO_DELIVERY_REBUILD"},
        confidence=1,
        idempotency_key=f"decision-{image_id}",
    )
    tasks = ExecutionTaskService(conn)
    action = tasks.create_operation_action(
        decision_id=decision["decision_id"],
        episode_id=decision["episode_id"],
        action_type="CREATE_PAUSED_AD",
        target_type="CAMPAIGN_REBUILD",
        target_id="source-ad",
        payload={
            "experiment_id": "adexp-1",
            "plan": {
                "target_account_id": "act_123",
                "after_json": {"creative": {"image_id": image_id}},
                "steps": {"IMAGE_UPLOAD": {"image_id": image_id}},
            },
        },
    )
    task = tasks.enqueue_task(
        action["operation_action_id"], idempotency_key=f"task-{image_id}", payload={},
    )
    object_ids = {
        "campaign_id": "campaign-new",
        "adset_id": "adset-new",
        "creative_id": "creative-new",
        "ad_id": "ad-new",
    }
    now = "2026-08-17T00:01:00+00:00"
    conn.execute(
        "UPDATE meta_execution_task SET status='RUNNING' WHERE execution_task_id=?",
        (task["execution_task_id"],),
    )
    conn.execute(
        "UPDATE meta_execution_task SET status='VERIFYING' WHERE execution_task_id=?",
        (task["execution_task_id"],),
    )
    conn.execute(
        """
        UPDATE meta_execution_task
        SET status='SUCCESS',meta_object_ids_json=?,finished_at=?
        WHERE execution_task_id=?
        """,
        (json.dumps(object_ids), now, task["execution_task_id"]),
    )
    conn.execute(
        "UPDATE growth_operation_action SET status='VERIFIED' WHERE operation_action_id=?",
        (action["operation_action_id"],),
    )
    conn.commit()
    return tasks, action, object_ids


def test_verified_creation_reconciliation_binds_frozen_image_once(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "growth.sqlite3")
    _seed_experiment_and_image(conn)
    tasks, action, _ = _verified_creation_action(conn)

    first = tasks.reconcile_verified_replacement_bindings()
    second = tasks.reconcile_verified_replacement_bindings()

    assert first["repaired"] == [action["operation_action_id"]]
    assert second == {"scanned": 0, "repaired": [], "skipped": []}
    binding = conn.execute(
        """
        SELECT image_id,experiment_id,campaign_id,adset_id,creative_id,ad_id,
               binding_method,binding_status
        FROM creative_adoption_records
        """
    ).fetchone()
    assert dict(binding) == {
        "image_id": "pro_img_new",
        "experiment_id": "adexp-1",
        "campaign_id": "campaign-new",
        "adset_id": "adset-new",
        "creative_id": "creative-new",
        "ad_id": "ad-new",
        "binding_method": "META_EXECUTION_RECEIPT_MATCH",
        "binding_status": "confirmed",
    }


def test_verified_creation_binding_fails_closed_without_frozen_image(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "growth-missing.sqlite3")
    _seed_experiment_and_image(conn)
    tasks, action, object_ids = _verified_creation_action(conn, image_id="")

    with pytest.raises(GrowthStateConflict, match="verified_meta_binding_incomplete:single:image_id"):
        tasks._bind_experiment_meta_objects(
            action["operation_action_id"], object_ids, require_complete=True,
        )
