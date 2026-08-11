from __future__ import annotations

import sqlite3

import pytest

from app.growth.ad_experiment_cycle_service import AdExperimentCycleService
from app.growth.ad_experiment_cycle_evaluator import AdExperimentCycleEvaluator
from app.growth.ad_experiment_evaluator import AdExperimentEvaluator
from app.growth.ad_experiment_service import AdExperimentService
from app.growth.common import canonical_json, payload_hash
from app.growth.delivery_guardrails import new_account_delivery_guardrails
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
    include_siblings: bool = False,
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
        (experiment_id,experiment_code,target_app,account_id,source_report_id,
         source_campaign_id,source_ad_id,experiment_type,state,control_definition_json,
         stop_rule_json,created_at,updated_at)
        VALUES ('experiment-1','EXP-1','Tugao','2282907019174017',?,?,?,
                'PAUSE_TEST','ADJUSTING',?,?,?,?)""",
        (
            "launch-1" if include_siblings else "", "campaign-1", "ad-1",
            canonical_json({"role": "BASELINE"}),
            canonical_json({"delivery_guardrails": new_account_delivery_guardrails()}),
            "2026-08-11T07:00:00+00:00", "2026-08-11T08:00:00+00:00",
        ),
    )
    if include_siblings:
        for index in (2, 3):
            conn.execute(
                """INSERT INTO ad_experiment
                (experiment_id,experiment_code,target_app,account_id,source_report_id,
                 source_campaign_id,source_ad_id,experiment_type,state,
                 control_definition_json,stop_rule_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,'NEW_AD_TEST','MATURING',?,?,?,?)""",
                (
                    f"experiment-{index}", f"EXP-{index}", "Tugao",
                    "2282907019174017", "launch-1", "campaign-1", f"ad-{index}",
                    canonical_json({"role": "CHALLENGER"}),
                    canonical_json({
                        "delivery_guardrails": new_account_delivery_guardrails(),
                    }),
                    "2026-08-11T07:00:00+00:00", "2026-08-11T08:00:00+00:00",
                ),
            )
    conn.execute(
        """INSERT INTO growth_context_snapshot
        (context_snapshot_id,app_id,snapshot_hash,created_at)
        VALUES ('context-1','Tugao','context-hash-1','2026-08-11T07:00:00+00:00')""",
    )
    conn.execute(
        """INSERT INTO growth_decision
        (decision_id,recommendation_id,context_snapshot_id,selected_action,
         rejected_actions_json,decision_reason_json,confidence,status,target_type,
         target_id,idempotency_key,request_hash,decided_by,created_at,updated_at)
        VALUES ('decision-1','recommendation-1','context-1','PAUSE_AD','[]','{}',
                0.8,'BOUND','AD','ad-1','decision-key','decision-hash','operator',
                '2026-08-11T07:00:00+00:00','2026-08-11T07:00:00+00:00')""",
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


def _performance_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE ad_creative_performance_daily (
        report_date_london TEXT NOT NULL,asset_id TEXT NOT NULL,ad_id TEXT NOT NULL,
        spend REAL NOT NULL,impressions REAL NOT NULL,clicks REAL NOT NULL,
        installs REAL NOT NULL,tugao_real_bind_count REAL NOT NULL,
        data_quality_status TEXT NOT NULL,
        PRIMARY KEY(report_date_london,asset_id,ad_id))""",
    )


def _metric(
    conn: sqlite3.Connection, report_date: str, ad_id: str, *, spend: float,
    impressions: int, clicks: int, installs: int, joins: int, asset_id: str = "",
) -> None:
    conn.execute(
        """INSERT INTO ad_creative_performance_daily
        (report_date_london,asset_id,ad_id,spend,impressions,clicks,installs,
         tugao_real_bind_count,data_quality_status)
        VALUES (?,?,?,?,?,?,?,?, 'PASS')""",
        (
            report_date, asset_id or f"asset-{ad_id}", ad_id, spend, impressions, clicks,
            installs, joins,
        ),
    )


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


def test_verified_pause_backfill_opens_cycle_from_already_paused_state() -> None:
    conn = _db()
    _insert_pause_chain(conn, terminal=True, include_siblings=True)
    conn.execute("UPDATE ad_experiment SET state='PAUSED' WHERE experiment_id='experiment-1'")
    conn.commit()

    cycle = AdExperimentCycleService(conn).reconcile_verified_action(
        "action-1", actor="growth-experiment-evaluator",
    )

    assert cycle["state"] == "WAITING_EVIDENCE"
    assert AdExperimentService(conn).get("experiment-1")["state"] == "EVALUATING_ADJUSTMENT"
    assert cycle["first_complete_date"] == "2026-08-12"
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


def test_d1_cycle_creates_immutable_observe_plan_without_meta_write() -> None:
    conn = _db()
    _insert_pause_chain(conn, terminal=True, include_siblings=True)
    cycle = AdExperimentCycleService(conn).reconcile_verified_action(
        "action-1", actor="growth-experiment-evaluator",
    )
    _performance_table(conn)
    _metric(conn, "2026-08-12", "ad-2", spend=0.20, impressions=100, clicks=4, installs=1, joins=0)
    _metric(conn, "2026-08-12", "ad-3", spend=0.25, impressions=120, clicks=5, installs=1, joins=0)
    conn.commit()

    not_due = AdExperimentCycleEvaluator(conn).evaluate_due(as_of_date="2026-08-11")
    result = AdExperimentCycleEvaluator(conn).evaluate_due(as_of_date="2026-08-12")
    repeat = AdExperimentCycleEvaluator(conn).evaluate_due(as_of_date="2026-08-12")
    detail = AdExperimentCycleEvaluator(conn).detail(cycle["cycle_id"])

    assert not_due["count"] == 0
    assert result["count"] == 1
    assert repeat["count"] == 0
    assert detail["cycle"]["state"] == "EVALUATING"
    assert detail["evaluations"][0]["evaluation_status"] == "OBSERVE"
    assert set(detail["evaluations"][0]["metrics_by_experiment"]) == {
        "experiment-2", "experiment-3",
    }
    assert detail["plans"][0]["action_type"] == "OBSERVE"
    assert detail["plans"][0]["status"] == "READY"
    assert detail["plans"][0]["requires_confirmation"] is False
    assert detail["plans"][0]["meta_write_allowed"] is False
    assert conn.execute("SELECT COUNT(*) FROM growth_operation_action").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE ad_experiment_cycle_next_plan SET causal_claim=1 WHERE cycle_id=?",
            (cycle["cycle_id"],),
        )


def test_cycle_detail_uses_pre_action_history_for_immediate_observe_answer() -> None:
    conn = _db()
    _insert_pause_chain(conn, terminal=True, include_siblings=True)
    cycle = AdExperimentCycleService(conn).reconcile_verified_action(
        "action-1", actor="growth-experiment-evaluator",
    )
    _performance_table(conn)
    for day in range(5, 11):
        _metric(
            conn, f"2026-08-{day:02d}", "ad-2",
            spend=0.50, impressions=200, clicks=6, installs=0, joins=0,
        )
        _metric(
            conn, f"2026-08-{day:02d}", "ad-3",
            spend=0.40, impressions=150, clicks=3, installs=0, joins=0,
        )
    conn.commit()
    before = {
        "evaluations": conn.execute(
            "SELECT COUNT(*) FROM ad_experiment_cycle_evaluation",
        ).fetchone()[0],
        "plans": conn.execute(
            "SELECT COUNT(*) FROM ad_experiment_cycle_next_plan",
        ).fetchone()[0],
        "actions": conn.execute("SELECT COUNT(*) FROM growth_operation_action").fetchone()[0],
    }

    detail = AdExperimentCycleEvaluator(conn).detail(cycle["cycle_id"])
    assessment = detail["immediate_assessment"]

    assert assessment["status"] == "NO_INTERVENTION_SUPPORTED"
    assert assessment["recommended_action"] == "OBSERVE"
    assert assessment["source_window"] == {
        "start": "2026-08-05", "end": "2026-08-10", "observed_day_count": 6,
    }
    assert assessment["history_is_post_action"] is False
    assert assessment["creates_cycle_evaluation"] is False
    assert assessment["creates_next_plan"] is False
    assert assessment["causal_claim"] is False
    assert assessment["meta_write_allowed"] is False
    assert assessment["action_candidates"] == []
    assert set(assessment["metrics_by_experiment"]) == {"experiment-2", "experiment-3"}
    assert before == {
        "evaluations": conn.execute(
            "SELECT COUNT(*) FROM ad_experiment_cycle_evaluation",
        ).fetchone()[0],
        "plans": conn.execute(
            "SELECT COUNT(*) FROM ad_experiment_cycle_next_plan",
        ).fetchone()[0],
        "actions": conn.execute("SELECT COUNT(*) FROM growth_operation_action").fetchone()[0],
    }


def test_pre_action_history_can_flag_review_but_never_create_an_operation() -> None:
    conn = _db()
    _insert_pause_chain(conn, terminal=True, include_siblings=True)
    cycle = AdExperimentCycleService(conn).reconcile_verified_action(
        "action-1", actor="growth-experiment-evaluator",
    )
    _performance_table(conn)
    for day in range(5, 11):
        _metric(
            conn, f"2026-08-{day:02d}", "ad-2",
            spend=0.50, impressions=150, clicks=0, installs=0, joins=0,
        )
        _metric(
            conn, f"2026-08-{day:02d}", "ad-3",
            spend=0.40, impressions=150, clicks=5, installs=0, joins=0,
        )
    conn.commit()

    assessment = AdExperimentCycleEvaluator(conn).detail(
        cycle["cycle_id"],
    )["immediate_assessment"]

    assert assessment["status"] == "INTERVENTION_REVIEW_SUPPORTED"
    assert assessment["recommended_action"] == "REVIEW_PAUSE_CANDIDATE"
    assert assessment["operator_review_required"] is True
    assert assessment["action_candidates"][0]["ad_id"] == "ad-2"
    assert assessment["creates_next_plan"] is False
    assert assessment["meta_write_allowed"] is False
    assert conn.execute("SELECT COUNT(*) FROM growth_operation_action").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM ad_experiment_cycle_next_plan").fetchone()[0] == 0


def test_legacy_launch_freezes_compat_guardrails_without_claiming_authority() -> None:
    conn = _db()
    _insert_pause_chain(conn, terminal=True, include_siblings=True)
    conn.execute("UPDATE ad_experiment SET stop_rule_json='{}'")
    conn.commit()

    cycle = AdExperimentCycleService(conn).reconcile_verified_action(
        "action-1", actor="growth-experiment-evaluator",
    )

    assert len(cycle["evaluation_subject"]["cells"]) == 3
    for cell in cycle["evaluation_subject"]["cells"]:
        assert cell["delivery_guardrails"]["version"] == "mx_cold_start_stop_v1"
        assert cell["delivery_guardrails_source"] == (
            "LEGACY_NEW_ACCOUNT_POLICY_COMPAT_MX_COLD_START_STOP_V1"
        )


def test_cycle_dedupes_asset_projection_and_never_treats_installs_as_zero() -> None:
    conn = _db()
    _insert_pause_chain(conn, terminal=True, include_siblings=True)
    cycle = AdExperimentCycleService(conn).reconcile_verified_action(
        "action-1", actor="growth-experiment-evaluator",
    )
    _performance_table(conn)
    for day in range(12, 15):
        report_date = f"2026-08-{day:02d}"
        for ad_id in ("ad-2", "ad-3"):
            for suffix in ("a", "b"):
                _metric(
                    conn, report_date, ad_id, spend=0.75, impressions=1000,
                    clicks=20, installs=0, joins=0,
                    asset_id=f"asset-{ad_id}-{suffix}",
                )
    conn.commit()

    result = AdExperimentCycleEvaluator(conn).evaluate_due(as_of_date="2026-08-14")
    detail = AdExperimentCycleEvaluator(conn).detail(cycle["cycle_id"])
    d3 = detail["evaluations"][-1]

    assert result["count"] == 2
    assert d3["checkpoint"] == "D3"
    assert d3["evaluation_status"] == "OBSERVE"
    assert d3["action_candidates"] == []
    assert d3["metrics_by_experiment"]["experiment-2"]["spend"] == pytest.approx(2.25)
    assert d3["metrics_by_experiment"]["experiment-2"]["installs"] is None
    assert d3["metrics_by_experiment"]["experiment-2"][
        "duplicate_projection_rows_collapsed"
    ] == 3
    assert conn.execute("SELECT COUNT(*) FROM growth_operation_action").fetchone()[0] == 1


def test_d3_stop_loss_compiles_second_round_plan_for_confirmation() -> None:
    conn = _db()
    _insert_pause_chain(conn, terminal=True, include_siblings=True)
    cycle = AdExperimentCycleService(conn).reconcile_verified_action(
        "action-1", actor="growth-experiment-evaluator",
    )
    _performance_table(conn)
    for report_date, impressions, clicks in (
        ("2026-08-12", 100, 4),
        ("2026-08-13", 400, 1),
        ("2026-08-14", 400, 1),
    ):
        _metric(
            conn, report_date, "ad-2", spend=0.60, impressions=impressions,
            clicks=clicks, installs=1, joins=0,
        )
        _metric(
            conn, report_date, "ad-3", spend=0.40, impressions=500,
            clicks=20, installs=2, joins=1,
        )
    conn.commit()

    result = AdExperimentCycleEvaluator(conn).evaluate_due(as_of_date="2026-08-14")
    detail = AdExperimentCycleEvaluator(conn).detail(cycle["cycle_id"])
    latest = detail["plans"][-1]

    assert result["count"] == 2
    assert detail["evaluations"][-1]["checkpoint"] == "D3"
    assert detail["evaluations"][-1]["evaluation_status"] == "ACTION_RECOMMENDED"
    assert latest["action_type"] == "PAUSE_AD"
    assert latest["target_experiment_id"] == "experiment-2"
    assert latest["target_id"] == "ad-2"
    assert latest["status"] == "AWAITING_CONFIRMATION"
    assert latest["requires_confirmation"] is True
    assert latest["operation_action_id"]
    assert latest["meta_write_allowed"] is False
    proposed = conn.execute(
        "SELECT status,target_id FROM growth_operation_action WHERE operation_action_id=?",
        (latest["operation_action_id"],),
    ).fetchone()
    approval = conn.execute(
        "SELECT status FROM growth_operation_approval WHERE operation_action_id=?",
        (latest["operation_action_id"],),
    ).fetchone()
    assert tuple(proposed) == ("CREATED", "ad-2")
    assert approval["status"] == "PROPOSED"
    assert conn.execute(
        "SELECT COUNT(*) FROM meta_execution_task WHERE operation_action_id=?",
        (latest["operation_action_id"],),
    ).fetchone()[0] == 0
    assert detail["cycle"]["state"] == "NEXT_PLAN_READY"
    assert AdExperimentService(conn).get("experiment-1")["state"] == "PAUSED"


def test_cycle_evaluation_rejects_frozen_subject_identity_drift() -> None:
    conn = _db()
    _insert_pause_chain(conn, terminal=True, include_siblings=True)
    AdExperimentCycleService(conn).reconcile_verified_action(
        "action-1", actor="growth-experiment-evaluator",
    )
    _performance_table(conn)
    _metric(conn, "2026-08-12", "ad-2", spend=0.2, impressions=100, clicks=4, installs=1, joins=0)
    _metric(conn, "2026-08-12", "ad-3", spend=0.2, impressions=100, clicks=4, installs=1, joins=0)
    conn.execute(
        "UPDATE ad_experiment SET source_ad_id='forged-ad' WHERE experiment_id='experiment-2'",
    )
    conn.commit()

    result = AdExperimentCycleEvaluator(conn).evaluate_due(as_of_date="2026-08-12")

    assert result["count"] == 0
    assert result["deferred"][0]["reason"] == "cycle_evaluation_experiment_binding_drift"
    assert conn.execute("SELECT COUNT(*) FROM ad_experiment_cycle_evaluation").fetchone()[0] == 0


def test_d7_cycle_closes_with_no_change_plan_when_no_guardrail_breaches() -> None:
    conn = _db()
    _insert_pause_chain(conn, terminal=True, include_siblings=True)
    cycle = AdExperimentCycleService(conn).reconcile_verified_action(
        "action-1", actor="growth-experiment-evaluator",
    )
    _performance_table(conn)
    for day in range(12, 19):
        report_date = f"2026-08-{day:02d}"
        for ad_id in ("ad-2", "ad-3"):
            _metric(
                conn, report_date, ad_id, spend=0.20, impressions=500,
                clicks=20, installs=2, joins=1,
            )
    conn.commit()

    result = AdExperimentCycleEvaluator(conn).evaluate_due(as_of_date="2026-08-18")
    detail = AdExperimentCycleEvaluator(conn).detail(cycle["cycle_id"])

    assert result["count"] == 3
    assert [item["checkpoint"] for item in detail["evaluations"]] == ["D1", "D3", "D7"]
    assert detail["evaluations"][-1]["evaluation_status"] == "CYCLE_COMPLETE_NO_CHANGE"
    assert detail["cycle"]["state"] == "EVALUATED"
    assert [item["status"] for item in detail["plans"]] == [
        "SUPERSEDED", "SUPERSEDED", "READY",
    ]
    assert detail["plans"][-1]["plan"]["next_checkpoint"] == ""
    assert detail["plans"][-1]["plan"]["requires_confirmation"] is False
    assert conn.execute("SELECT COUNT(*) FROM growth_operation_action").fetchone()[0] == 1
    assert AdExperimentService(conn).get("experiment-1")["state"] == "PAUSED"
