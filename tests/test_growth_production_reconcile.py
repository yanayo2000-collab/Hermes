from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.creative_image_generation import (
    CreativeImageGenerationBrief,
    archive_due_replaced_creatives,
    create_feed_image_generation,
    create_chatgpt_pro_job,
    create_review_record,
    ensure_creative_image_generation_tables,
    latest_generated_images,
    mark_generated_image_adopted,
    mark_replaced_creative_pending_cleanup,
)
from app.ad_creative_intelligence import ensure_creative_intelligence_tables
from app.growth.decision_service import DecisionService
from app.growth.execution_service import ExecutionTaskService
from app.growth.api import create_ad_experiment_router
from app.growth.new_account_launch_meta_delete import (
    NewAccountLaunchMetaDeleteService,
    launch_meta_delete_status,
)
from app.growth.schema import ensure_growth_schema
from app.main import create_app


def _growth_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_growth_schema(conn)
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
        "INSERT OR IGNORE INTO ad_recommendation (recommendation_id, payload_json) VALUES ('recommendation-1', ?)",
        (json.dumps({"project": "tugao", "country": "BR"}),),
    )
    conn.commit()
    return conn


def _api_client(tmp_path: Path) -> tuple[TestClient, Path]:
    db_path = tmp_path / "growth-api.sqlite3"
    client = TestClient(create_app({"DB_PATH": str(db_path), "AUTH_ENABLED": False}))
    return client, db_path


def _launch_payload(*, country: str = "BR") -> dict:
    return {
        "target_app": "tugao",
        "country": country,
        "account_id": "123456789",
        "daily_spend_target": 60,
        "cpi_target": 0.30,
        "page_id": "998877",
        "gender": "female",
        "age_min": 18,
        "age_max": 40,
        "language": "pt_BR" if country == "BR" else "es_419",
        "naming_date": "20260805",
        "creative_directions": [
            {
                "direction_id": "points_reward",
                "key": "points_reward",
                "code": "PR",
                "title": "网赚效率",
                "hypothesis": "A",
                "initial_daily_budget": 20,
            },
            {
                "direction_id": "easy_start",
                "key": "easy_start",
                "code": "ES",
                "title": "流程透明",
                "hypothesis": "B",
                "initial_daily_budget": 20,
            },
        ],
    }


def test_reconciled_growth_import_graph_is_closed() -> None:
    assert callable(create_ad_experiment_router)
    assert callable(NewAccountLaunchMetaDeleteService.enqueue)
    assert callable(NewAccountLaunchMetaDeleteService.run_enqueued)
    assert callable(launch_meta_delete_status)
    assert callable(mark_replaced_creative_pending_cleanup)
    assert callable(archive_due_replaced_creatives)


def test_creative_job_creation_is_idempotent_per_recommendation_and_source_ad() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    brief = CreativeImageGenerationBrief(country="BR", campaign="BR campaign", ad="source ad")
    payload = {
        "recommendation_id": "reco-one",
        "source_ad_id": "source-ad-one",
        "experiment_mode": "new_test",
    }

    first = create_chatgpt_pro_job(conn, brief=brief, payload=payload, created_by="operator")
    second = create_chatgpt_pro_job(conn, brief=brief, payload=payload, created_by="operator")

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["deduplicated"] is True
    assert second["job"]["job_id"] == first["job"]["job_id"]
    assert second["experiment"]["experiment_id"] == first["experiment"]["experiment_id"]
    assert conn.execute("SELECT COUNT(*) FROM creative_pro_work_queue").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM creative_experiment_suggestions").fetchone()[0] == 1


def test_replaced_image_is_retained_then_archived(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    old_result = create_feed_image_generation(
        conn,
        CreativeImageGenerationBrief(country="Brazil", campaign="BR campaign", ad="old"),
        output_dir=tmp_path / "old",
    )
    new_result = create_feed_image_generation(
        conn,
        CreativeImageGenerationBrief(country="Brazil", campaign="BR campaign", ad="new"),
        output_dir=tmp_path / "new",
    )
    old_image_id = old_result["image"]["image_id"]
    new_image_id = new_result["image"]["image_id"]
    for image_id in (old_image_id, new_image_id):
        create_review_record(
            conn,
            image_id=image_id,
            review_status="APPROVED",
            reviewer="operator",
            checks={"feed_static_ad_structure": True},
        )
    mark_generated_image_adopted(
        conn,
        image_id=old_image_id,
        ad_id="ad-1",
        creative_id="creative-old",
    )

    pending = mark_replaced_creative_pending_cleanup(
        conn,
        ad_id="ad-1",
        old_creative_id="creative-old",
        replacement_image_id=new_image_id,
        replacement_creative_id="creative-new",
    )
    mark_generated_image_adopted(
        conn,
        image_id=new_image_id,
        ad_id="ad-1",
        creative_id="creative-new",
    )

    assert pending["updated"] == 1
    listed = {item["image_id"]: item for item in latest_generated_images(conn, limit=10)}
    assert listed[old_image_id]["latest_adoption"]["binding_status"] == "pending_cleanup"
    assert listed[old_image_id]["latest_adoption"]["cleanup_after"]
    assert listed[new_image_id]["latest_adoption"]["binding_status"] == "confirmed"

    archived = archive_due_replaced_creatives(
        conn,
        now=datetime.now(timezone.utc) + timedelta(days=8),
    )
    assert archived["archived"] == [old_image_id]
    assert conn.execute(
        "SELECT review_status FROM creative_generated_images WHERE image_id=?",
        (old_image_id,),
    ).fetchone()["review_status"] == "archived"


def test_verified_replacement_binding_reconcile_is_idempotent(tmp_path: Path) -> None:
    conn = _growth_connection(tmp_path / "growth-replacement-binding.sqlite3")
    ensure_creative_image_generation_tables(conn)
    now = "2026-08-06T00:00:00+00:00"
    conn.execute(
        """INSERT INTO ad_experiment
        (experiment_id,experiment_code,target_app,country,platform,account_id,source_report_id,
         source_ad_id,source_creative_id,experiment_type,state,created_at,updated_at)
        VALUES ('experiment-replacement','BR-REPLACE-1','tugao','BR','meta','123','launch-replacement',
                'ad-1','creative-old','CREATIVE_REPLACEMENT','DATA_INCOMPLETE',?,?)""",
        (now, now),
    )
    for image_id in ("pro_img_old", "pro_img_new"):
        conn.execute(
            """INSERT INTO creative_generated_images
            (image_id,request_id,surface,image_size,market,brand,image_ref,thumbnail_ref,prompt_hash,
             risk_status,risk_tags_json,review_status,provider,metadata_json,created_at,file_path)
            VALUES (?,?,'feed','1024x1024','BR','Premiou',?,'','hash','passed','[]','approved',
                    'test','{}',?,?)""",
            (image_id, f"request-{image_id}", f"/tmp/{image_id}.png", now, f"/tmp/{image_id}.png"),
        )
    conn.execute(
        """INSERT INTO creative_adoption_records
        (adoption_id,image_id,request_id,adoption_type,ad_id,creative_id,status,binding_method,
         binding_confidence,binding_status,payload_json,adopted_by,adopted_at)
        VALUES ('adoption-old','pro_img_old','request-pro_img_old','manual','ad-1','creative-old',
                'USED_IN_AD','MANUAL_CONFIRMED','HIGH','confirmed','{}','operator',?)""",
        (now,),
    )
    conn.execute(
        """INSERT INTO ad_creative_group_evaluation
        (group_evaluation_id,launch_id,checkpoint,window_json,metrics_by_experiment_json,
         ranking_json,winner_experiment_id,decision_status,actual_days,data_quality_status,
         evidence_json,evaluated_at)
        VALUES ('evaluation-before-replacement','launch-replacement','D1','{}','{}','[]','',
                'OBSERVE',1,'PASS','{}',?)""",
        (now,),
    )
    decision = DecisionService(conn).create_decision(
        recommendation_id="recommendation-1",
        selected_action="REPLACE_CREATIVE",
        decision_reason={"type": "META_REJECTION"},
        confidence=1,
        idempotency_key="decision-replacement-binding",
    )
    tasks = ExecutionTaskService(conn)
    action = tasks.create_operation_action(
        decision_id=decision["decision_id"],
        episode_id=decision["episode_id"],
        action_type="REPLACE_CREATIVE",
        target_type="AD",
        target_id="ad-1",
        payload={
            "experiment_id": "experiment-replacement",
            "plan": {
                "before_json": {"creative_id": "creative-old"},
                "after_json": {"creative": {"image_id": "pro_img_new"}},
            },
        },
    )
    task = tasks.enqueue_task(
        action["operation_action_id"],
        idempotency_key="replacement-binding-task",
        payload={},
    )
    object_ids = {"ad_id": "ad-1", "creative_id": "creative-new"}
    conn.execute(
        "UPDATE meta_execution_task SET status='RUNNING' WHERE execution_task_id=?",
        (task["execution_task_id"],),
    )
    conn.execute(
        "UPDATE meta_execution_task SET status='VERIFYING' WHERE execution_task_id=?",
        (task["execution_task_id"],),
    )
    conn.execute(
        """UPDATE meta_execution_task SET status='SUCCESS',meta_object_ids_json=?,finished_at=?
        WHERE execution_task_id=?""",
        (json.dumps(object_ids), now, task["execution_task_id"]),
    )
    conn.execute(
        "UPDATE growth_operation_action SET status='EXECUTING' WHERE operation_action_id=?",
        (action["operation_action_id"],),
    )
    conn.execute(
        "UPDATE growth_operation_action SET status='VERIFIED' WHERE operation_action_id=?",
        (action["operation_action_id"],),
    )
    conn.commit()

    first = tasks.reconcile_verified_replacement_bindings()
    old_deadline = json.loads(
        conn.execute(
            "SELECT payload_json FROM creative_adoption_records WHERE adoption_id='adoption-old'",
        ).fetchone()[0]
    )["cleanup_after"]
    second = tasks.reconcile_verified_replacement_bindings()

    assert first["repaired"] == [action["operation_action_id"]]
    assert second["repaired"] == []
    old_binding = conn.execute(
        "SELECT binding_status,payload_json FROM creative_adoption_records WHERE adoption_id='adoption-old'",
    ).fetchone()
    assert old_binding["binding_status"] == "pending_cleanup"
    assert json.loads(old_binding["payload_json"])["cleanup_after"] == old_deadline
    new_binding = conn.execute(
        """SELECT status,binding_status FROM creative_adoption_records
        WHERE image_id='pro_img_new' AND ad_id='ad-1' AND creative_id='creative-new'""",
    ).fetchone()
    assert tuple(new_binding) == ("USED_IN_AD", "confirmed")


def test_successful_launch_creation_blocks_duplicate_plan(tmp_path: Path) -> None:
    client, db_path = _api_client(tmp_path)
    created = client.post(
        "/api/ops/ad-data-dashboard/new-account-launches",
        headers={"Idempotency-Key": "created-launch", "X-Request-ID": "created-launch-request"},
        json=_launch_payload(country="MX"),
    ).json()
    launch_id = created["launch_id"]
    now = "2026-08-07T03:00:00+00:00"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO growth_operation_action
            (operation_action_id,decision_id,action_type,target_type,target_id,payload_json,status,created_at,updated_at)
            VALUES ('created-action','fixture-decision','CREATE_PAUSED_AD','LAUNCH',?,?,'VERIFIED',?,?)""",
            (launch_id, json.dumps({"launch_id": launch_id}), now, now),
        )
        conn.execute(
            """INSERT INTO meta_execution_task
            (execution_task_id,operation_action_id,idempotency_key,request_hash,status,current_step,
             payload_json,meta_object_ids_json,created_at,updated_at,finished_at)
            VALUES ('created-task','created-action','created-task-key','created-task-hash','SUCCESS','RECEIPT',
                    '{}','{}',?,?,?)""",
            (now, now, now),
        )
        conn.commit()

    experiment_ids = [item["experiment_id"] for item in created["variants"]]
    blocked = client.post(
        f"/api/ops/ad-data-dashboard/new-account-launches/{launch_id}/create-plan/preview",
        headers={"Idempotency-Key": "second-plan", "X-Request-ID": "second-plan-request"},
        json={
            "campaign_name": "TG_MX_INS_CS_260807",
            "cells": [
                {
                    "experiment_id": experiment_id,
                    "role": "BASELINE" if index == 0 else "CHALLENGER",
                    "adset_name": f"adset-{index}",
                    "daily_budget_usd": 10,
                    "ad_name": f"ad-{index}",
                    "primary_text": f"copy-{index}",
                    "headline": f"headline-{index}",
                }
                for index, experiment_id in enumerate(experiment_ids)
            ],
        },
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["message"] == "launch_already_created"


def test_delivery_projection_deduplicates_ad_day_and_marks_t_plus_one(tmp_path: Path) -> None:
    client, db_path = _api_client(tmp_path)
    created = client.post(
        "/api/ops/ad-data-dashboard/new-account-launches",
        headers={"Idempotency-Key": "analysis-launch", "X-Request-ID": "analysis-launch-request"},
        json=_launch_payload(),
    ).json()
    launch_id = created["launch_id"]
    with sqlite3.connect(db_path) as conn:
        ensure_creative_intelligence_tables(conn)
        experiments = conn.execute(
            "SELECT experiment_id FROM ad_experiment WHERE source_report_id=? ORDER BY experiment_code",
            (launch_id,),
        ).fetchall()
        for index, experiment in enumerate(experiments, start=1):
            conn.execute(
                """UPDATE ad_experiment SET state='RUNNING',source_campaign_id='campaign-analysis',
                   source_adset_id=?,source_ad_id=? WHERE experiment_id=?""",
                (f"adset-{index}", f"ad-{index}", experiment[0]),
            )
        for asset_id, ad_id, adset_id, spend, impressions, clicks, installs in (
            ("asset-1", "ad-1", "adset-1", 2.12, 319, 7, 1),
            ("asset-1-copy", "ad-1", "adset-1", 2.12, 319, 7, 1),
            ("asset-2", "ad-2", "adset-2", 3.08, 681, 13, 1),
        ):
            conn.execute(
                """INSERT INTO ad_creative_performance_daily
                (report_date_london,asset_id,creative_id,ad_id,adset_id,campaign_id,country,project,
                 spend,impressions,clicks,ctr,cpm,installs,cpi,af_model_join_events,
                 tugao_real_bind_count,real_bind_cpa,af_to_real_bind_rate,data_quality_status,
                 attribution_level,creative_grain,is_dynamic_creative,grain_warning)
                VALUES ('2026-08-05',?,NULL,?,?,'campaign-analysis','BR','Tugao',
                        ?,?,?,?,0,?,?,0,0,NULL,NULL,'ad_id_with_downstream_text_match',
                        '广告级','STATIC',0,NULL)""",
                (
                    asset_id,
                    ad_id,
                    adset_id,
                    spend,
                    impressions,
                    clicks,
                    clicks / impressions,
                    installs,
                    spend / installs,
                ),
            )
        conn.commit()

    response = client.get(f"/api/ops/ad-data-dashboard/new-account-launches/{launch_id}")
    assert response.status_code == 200
    performance = response.json()["delivery_performance"]
    assert performance["spend"] == 5.2
    assert performance["installs"] == 2
    assert performance["deduplication_key"] == "report_date_london+ad_id"
    assert performance["duplicate_rows_removed"] == 1
    assert performance["statistics_cutoff_date"] == "2026-08-05"
    assert performance["freshness_mode"] == "T_PLUS_1_DAILY"
    assert performance["is_realtime"] is False
    assert performance["delivery_status_is_realtime"] is True
