from __future__ import annotations

import sqlite3

import pytest

from app.growth.ad_experiment_cycle_service import AdExperimentCycleService
from app.growth.ad_experiment_evaluator import AdExperimentEvaluator
from app.growth.common import canonical_json, payload_hash
from app.growth.errors import GrowthStateConflict
from app.growth.execution_service import ExecutionTaskService
from app.growth.schema import ensure_growth_schema


FINISHED_AT = "2026-08-11T08:30:59+00:00"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_growth_schema(conn)
    return conn


def _insert_pause_chain(
    conn: sqlite3.Connection, *, terminal: bool = False, readback_status: str = "PAUSED",
) -> None:
    plan = {
        "schema_version": "gle-ad-experiment-plan-v1",
        "experiment_id": "experiment-1",
        "action_type": "PAUSE_AD",
        "target_object_type": "AD",
        "target_object_id": "ad-1",
        "before_json": {"status": "ACTIVE"},
        "after_json": {"status": "PAUSED"},
        "steps": {"STATUS_UPDATE": {"target_id": "ad-1", "status": "PAUSED"}},
        "evaluation_window": {"checkpoints": ["D1", "D3", "D7"]},
    }
    action_payload = {
        "experiment_id": "experiment-1",
        "experiment_ids": ["experiment-1"],
        "plan": plan,
    }
    conn.execute(
        """INSERT INTO ad_experiment
        (experiment_id,experiment_code,target_app,account_id,source_ad_id,
         experiment_type,state,created_at,updated_at)
        VALUES ('experiment-1','EXP-1','Tugao','2282907019174017','ad-1',
                'PAUSE_TEST','ADJUSTING',?,?)""",
        ("2026-08-11T07:00:00+00:00", "2026-08-11T08:00:00+00:00"),
    )
    conn.execute(
        """INSERT INTO growth_operation_action
        (operation_action_id,decision_id,action_type,action_scope,target_type,target_id,
         payload_json,status,created_by,created_at,updated_at)
        VALUES ('action-1','decision-1','PAUSE_AD','EXPERIMENT','AD','ad-1',?,?,?,?,?)""",
        (
            canonical_json(action_payload), "VERIFIED" if terminal else "EXECUTING",
            "operator:planner", "2026-08-11T07:30:00+00:00",
            FINISHED_AT if terminal else "2026-08-11T08:05:00+00:00",
        ),
    )
    conn.execute(
        """INSERT INTO growth_operation_approval
        (approval_id,operation_action_id,plan_hash,plan_json,status,proposed_by,approved_by,
         approved_at,expires_at,consumed_at,idempotency_key,request_hash,created_at,updated_at)
        VALUES ('approval-1','action-1',?,?,'APPROVED','operator:planner','operator:owner',
                '2026-08-11T08:00:00+00:00','2026-08-12T08:00:00+00:00',
                '2026-08-11T08:05:00+00:00','approval-key',?,
                '2026-08-11T07:55:00+00:00','2026-08-11T08:05:00+00:00')""",
        (
            payload_hash(plan), canonical_json(plan),
            payload_hash({"operation_action_id": "action-1", "plan": plan}),
        ),
    )
    task_payload = {
        "action_type": "PAUSE_AD", "experiment_id": "experiment-1",
        "plan": plan,
    }
    conn.execute(
        """INSERT INTO meta_execution_task
        (execution_task_id,operation_action_id,idempotency_key,request_hash,status,current_step,
         payload_json,meta_object_ids_json,locked_by,locked_at,heartbeat_at,
         created_at,updated_at,finished_at)
        VALUES ('task-1','action-1','task-key',?,?,?,?,'{"ad_id":"ad-1"}',
                'worker-1','2026-08-11T08:10:00+00:00','2026-08-11T08:20:00+00:00',
                '2026-08-11T08:10:00+00:00',?,?)""",
        (
            payload_hash({"operation_action_id": "action-1", "payload": task_payload}),
            "SUCCESS" if terminal else "VERIFYING", "RECEIPT" if terminal else "VERIFY",
            canonical_json(task_payload), FINISHED_AT if terminal else "2026-08-11T08:20:00+00:00",
            FINISHED_AT if terminal else "",
        ),
    )
    verification = {
        "status": "SUCCESS",
        "meta_object_ids": {"ad_id": "ad-1"},
        "object_statuses": {"ad_id": readback_status},
    }
    receipts = (
        ("receipt-status", "STATUS_UPDATE", "SUCCESS", {"status": "SUCCESS"}, verification,
         "2026-08-11T08:20:00+00:00"),
        ("receipt-verify", "VERIFY", "VERIFIED", {}, verification,
         "2026-08-11T08:21:00+00:00"),
        ("receipt-final", "RECEIPT", "SUCCESS", {"final_status": "SUCCESS"}, verification,
         "2026-08-11T08:22:00+00:00"),
    )
    for receipt_id, step, status, result, verified, created_at in receipts:
        conn.execute(
            """INSERT INTO meta_execution_task_receipt
            (receipt_id,execution_task_id,step_name,step_status,step_result_json,
             meta_object_ids_json,verification_result_json,created_at)
            VALUES (?,'task-1',?,?,?,?,?,?)""",
            (
                receipt_id, step, status, canonical_json(result),
                canonical_json({"ad_id": "ad-1"}), canonical_json(verified), created_at,
            ),
        )
    conn.commit()


def test_verified_pause_opens_one_evidence_bound_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _db()
    _insert_pause_chain(conn)
    monkeypatch.setattr("app.growth.execution_service.utc_now", lambda: FINISHED_AT)

    result = ExecutionTaskService(conn).transition(
        "task-1", "SUCCESS", worker_id="worker-1", current_step="RECEIPT",
        meta_object_ids={"ad_id": "ad-1"},
    )
    cycle = AdExperimentCycleService(conn).list_for_experiment("experiment-1")["items"][0]

    assert result["evaluation_cycle"]["status"] == "OPENED"
    assert cycle["source_operation_action_id"] == "action-1"
    assert cycle["evaluation_checkpoints"] == ["D1", "D3", "D7"]
    assert cycle["window_opened_at"] == FINISHED_AT
    assert cycle["first_complete_date"] == "2026-08-12"
    assert cycle["causal_claim"] is False
    assert cycle["meta_write_allowed"] is False
    assert conn.execute(
        "SELECT state FROM ad_experiment WHERE experiment_id='experiment-1'",
    ).fetchone()[0] == "EVALUATING_ADJUSTMENT"

    same = AdExperimentCycleService(conn).reconcile_verified_action(
        "action-1", actor="growth-experiment-evaluator",
    )
    assert same["cycle_id"] == cycle["cycle_id"]
    assert conn.execute("SELECT COUNT(*) FROM ad_experiment_cycle").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM ad_experiment_events WHERE event_type='EVALUATION_WINDOW_OPENED'",
    ).fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE ad_experiment_cycle SET causal_claim=1")


def test_invalid_readback_never_opens_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _db()
    _insert_pause_chain(conn, readback_status="ACTIVE")
    monkeypatch.setattr("app.growth.execution_service.utc_now", lambda: FINISHED_AT)

    result = ExecutionTaskService(conn).transition(
        "task-1", "SUCCESS", worker_id="worker-1", current_step="RECEIPT",
        meta_object_ids={"ad_id": "ad-1"},
    )

    assert result["evaluation_cycle"] == {
        "status": "PENDING_RECONCILIATION",
        "reason": "closed_loop_cycle_readback_invalid",
        "meta_writes_performed": False,
    }
    assert conn.execute("SELECT COUNT(*) FROM ad_experiment_cycle").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM ad_experiment_events WHERE event_type='EVALUATION_CYCLE_RECONCILIATION_PENDING'",
    ).fetchone()[0] == 1


def test_reconcile_pending_backfills_verified_history_and_rejects_source_drift() -> None:
    conn = _db()
    _insert_pause_chain(conn, terminal=True)
    service = AdExperimentCycleService(conn)

    first = service.reconcile_pending(actor="growth-experiment-evaluator")
    second = service.reconcile_pending(actor="growth-experiment-evaluator")

    assert first["opened_count"] == 1
    assert first["rejected_count"] == 0
    assert second["opened_count"] == 0
    assert conn.execute("SELECT COUNT(*) FROM ad_experiment_cycle").fetchone()[0] == 1

    conn.execute(
        "UPDATE meta_execution_task_receipt SET step_result_json=? WHERE receipt_id='receipt-final'",
        (canonical_json({"final_status": "SUCCESS", "tampered": True}),),
    )
    conn.commit()
    with pytest.raises(GrowthStateConflict, match="closed_loop_cycle_source_drift"):
        service.reconcile_verified_action("action-1", actor="auditor")


def test_manual_review_action_cannot_open_cycle() -> None:
    conn = _db()
    _insert_pause_chain(conn, terminal=True)
    conn.execute("UPDATE growth_operation_action SET status='MANUAL_REVIEW'")
    conn.commit()

    with pytest.raises(GrowthStateConflict, match="closed_loop_cycle_action_not_verified"):
        AdExperimentCycleService(conn).reconcile_verified_action("action-1", actor="auditor")


def test_legacy_evaluator_never_reuses_original_window_for_cycle() -> None:
    conn = _db()
    _insert_pause_chain(conn, terminal=True)
    AdExperimentCycleService(conn).reconcile_pending(actor="growth-experiment-evaluator")
    conn.execute(
        """CREATE TABLE ad_creative_performance_daily (
        report_date_london TEXT,ad_id TEXT,spend REAL,impressions REAL,clicks REAL,
        installs REAL,tugao_real_bind_count REAL,real_bind_cpa REAL,data_quality_status TEXT)""",
    )
    conn.execute(
        """INSERT INTO ad_creative_performance_daily
        VALUES ('2026-08-12','ad-1',10,1000,20,10,2,5,'PASS')""",
    )
    conn.commit()

    result = AdExperimentEvaluator(conn).evaluate_due(as_of_date="2026-08-20")

    assert result["count"] == 0
    assert conn.execute("SELECT COUNT(*) FROM ad_experiment_evaluation").fetchone()[0] == 0
