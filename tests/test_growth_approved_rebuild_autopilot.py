import json
import sqlite3
from pathlib import Path

from app.growth.approved_rebuild_autopilot import ApprovedRebuildAutopilot


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Session:
    def __init__(self, conn):
        self.conn = conn
        self.paths = []

    def post(self, url, *, json, headers, timeout):
        path = url.split("http://internal", 1)[-1]
        self.paths.append(path)
        if path.endswith("/rebuild-plan/prepare"):
            self.conn.execute(
                "INSERT INTO growth_operation_action VALUES (?,?,?,?,?)",
                ("plan-1", "CREATED", "CREATE_PAUSED_AD", '{"experiment_id":"exp-1"}', "2026-08-19T10:00:00+00:00"),
            )
            self.conn.execute(
                "INSERT INTO growth_operation_approval VALUES (?,?,?)",
                ("approval-1", "plan-1", "PROPOSED"),
            )
            self.conn.commit()
            return _Response({"plan_id": "plan-1"}, 201)
        if path.endswith("/approve"):
            self.conn.execute(
                "UPDATE growth_operation_approval SET status='APPROVED' WHERE operation_action_id='plan-1'"
            )
            self.conn.commit()
            return _Response({"status": "APPROVED"})
        if path.endswith("/execute") and json.get("execution_mode") == "dry_run":
            return _Response({"status": "DRY_RUN_VERIFIED"})
        if path.endswith("/execute") and json.get("execution_mode") == "live":
            self.conn.execute(
                "INSERT INTO meta_execution_task VALUES (?,?,?,?,?)",
                ("task-1", "plan-1", "QUEUED", "", "2026-08-19T10:00:00+00:00"),
            )
            self.conn.commit()
            return _Response({"execution_task_id": "task-1"}, 201)
        if path.endswith("/rebuild-source-ad/delete"):
            return _Response({
                "status": "SUCCESS",
                "source_ad_deleted": True,
                "new_ad_id": "new-ad",
                "source_ad_id": "old-ad",
            })
        raise AssertionError(path)


class _Experiments:
    def get(self, experiment_id):
        return {"experiment_id": experiment_id, "hypothesis_json": {}}

    def latest_approved_creative(self, experiment_id):
        return {"image_id": "image-1", "review_status": "APPROVED"}


def _connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE ad_experiment (
            experiment_id TEXT PRIMARY KEY,
            hypothesis_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE creative_pro_work_queue (
            job_id TEXT PRIMARY KEY,
            material_refs_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE growth_decision (
            decision_id TEXT PRIMARY KEY,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE growth_idempotency_record (
            route_key TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE growth_operation_action (
            operation_action_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            action_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE growth_operation_approval (
            approval_id TEXT PRIMARY KEY,
            operation_action_id TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE meta_execution_task (
            execution_task_id TEXT PRIMARY KEY,
            operation_action_id TEXT NOT NULL,
            status TEXT NOT NULL,
            error_message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    return conn


def _autopilot(conn, session):
    autopilot = ApprovedRebuildAutopilot.__new__(ApprovedRebuildAutopilot)
    autopilot.conn = conn
    autopilot.session = session
    autopilot.base_url = "http://internal"
    autopilot.internal_token = "secret"
    autopilot.legacy_batch_prefixes = frozenset({
        "gle-bulk-rebuild:gle-bulk-1-attempt-0"
    })
    autopilot.timeout_seconds = 25.0
    return autopilot


def _insert_batch_member(conn, experiment_id, decision_id, recommendation_id, initial_status=""):
    hypothesis = {"rebuild_initial_status": initial_status} if initial_status else {}
    conn.execute(
        "INSERT INTO ad_experiment VALUES (?,?,?)",
        (experiment_id, json.dumps(hypothesis), "2026-08-19T09:15:00+00:00"),
    )
    conn.execute(
        "INSERT INTO growth_decision VALUES (?, 'EXPERIMENT', ?, '2026-08-19T09:15:00+00:00')",
        (decision_id, experiment_id),
    )
    conn.execute(
        "INSERT INTO growth_idempotency_record VALUES ('decision.create',?,?,?)",
        (
            f"gle-bulk-rebuild:gle-bulk-1-attempt-0:{recommendation_id}:decision",
            json.dumps({"decision_id": decision_id}),
            "2026-08-19T09:15:00+00:00",
        ),
    )
    conn.commit()


def test_legacy_batch_infers_one_explicit_initial_status():
    conn = _connection()
    _insert_batch_member(conn, "exp-missing", "decision-missing", "rec-1")
    _insert_batch_member(conn, "exp-explicit", "decision-explicit", "rec-2", "ACTIVE")
    autopilot = _autopilot(conn, _Session(conn))

    status, source = autopilot._authorized_initial_status(
        "exp-missing", {"hypothesis_json": {}}
    )

    assert status == "ACTIVE"
    assert source == "legacy_batch_sibling_status"


def test_legacy_batch_refuses_mixed_initial_statuses():
    conn = _connection()
    _insert_batch_member(conn, "exp-missing", "decision-missing", "rec-1")
    _insert_batch_member(conn, "exp-active", "decision-active", "rec-2", "ACTIVE")
    _insert_batch_member(conn, "exp-paused", "decision-paused", "rec-3", "PAUSED")
    autopilot = _autopilot(conn, _Session(conn))

    assert autopilot._authorized_initial_status(
        "exp-missing", {"hypothesis_json": {}}
    ) == ("", "")


def test_legacy_batch_requires_explicit_recovery_allowlist():
    conn = _connection()
    _insert_batch_member(conn, "exp-missing", "decision-missing", "rec-1")
    _insert_batch_member(conn, "exp-explicit", "decision-explicit", "rec-2", "ACTIVE")
    autopilot = _autopilot(conn, _Session(conn))
    autopilot.legacy_batch_prefixes = frozenset()

    assert autopilot._authorized_initial_status(
        "exp-missing", {"hypothesis_json": {}}
    ) == ("", "")


def test_approved_rebuild_is_durable_and_deletes_only_after_verified(monkeypatch):
    conn = _connection()
    session = _Session(conn)
    autopilot = _autopilot(conn, session)
    autopilot.experiments = _Experiments()
    monkeypatch.setattr(
        autopilot, "_authorized_initial_status", lambda experiment_id, experiment: ("ACTIVE", "test")
    )
    monkeypatch.setattr(autopilot, "_persist_intent", lambda *args: None)

    queued = autopilot._advance_one("exp-1")

    assert queued["status"] == "EXECUTION_QUEUED"
    assert session.paths == [
        "/api/ops/ad-data-dashboard/experiments/exp-1/rebuild-plan/prepare",
        "/api/ops/ad-data-dashboard/meta-plans/plan-1/approve",
        "/api/ops/ad-data-dashboard/meta-plans/plan-1/execute",
        "/api/ops/ad-data-dashboard/meta-plans/plan-1/execute",
    ]
    assert not any(path.endswith("/rebuild-source-ad/delete") for path in session.paths)

    conn.execute("UPDATE growth_operation_action SET status='VERIFIED' WHERE operation_action_id='plan-1'")
    conn.execute("UPDATE meta_execution_task SET status='SUCCESS' WHERE execution_task_id='task-1'")
    conn.commit()
    completed = autopilot._advance_one("exp-1")

    assert completed == {
        "experiment_id": "exp-1",
        "status": "SUCCESS",
        "plan_id": "plan-1",
        "new_ad_id": "new-ad",
        "source_ad_id": "old-ad",
    }
    assert session.paths[-1].endswith("/rebuild-source-ad/delete")


def test_latest_manual_review_action_is_not_bypassed_by_an_older_verified_plan():
    conn = _connection()
    conn.execute(
        "INSERT INTO growth_operation_action VALUES (?,?,?,?,?)",
        ("old-verified", "VERIFIED", "CREATE_PAUSED_AD", '{"experiment_id":"exp-1"}', "2026-08-19T09:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO growth_operation_action VALUES (?,?,?,?,?)",
        ("new-manual", "MANUAL_REVIEW", "CREATE_PAUSED_AD", '{"experiment_id":"exp-1"}', "2026-08-19T10:00:00+00:00"),
    )
    conn.commit()
    autopilot = _autopilot(conn, _Session(conn))

    assert autopilot._latest_create_action("exp-1")["operation_action_id"] == "new-manual"


def test_non_retryable_prepare_error_becomes_durable_parameter_confirmation(monkeypatch):
    conn = _connection()
    conn.execute(
        "INSERT INTO ad_experiment VALUES (?,?,?)",
        ("exp-1", '{}', "2026-08-19T10:00:00+00:00"),
    )
    conn.commit()
    autopilot = _autopilot(conn, _Session(conn))
    autopilot.experiments = _Experiments()
    monkeypatch.setattr(
        autopilot, "_authorized_initial_status", lambda experiment_id, experiment: ("ACTIVE", "test")
    )
    monkeypatch.setattr(autopilot, "_persist_intent", lambda *args: None)
    monkeypatch.setattr(
        autopilot,
        "_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("internal_api_400:rebuild_source_daily_budget_out_of_range")
        ),
    )

    result = autopilot._advance_one("exp-1")
    hypothesis = json.loads(conn.execute(
        "SELECT hypothesis_json FROM ad_experiment WHERE experiment_id='exp-1'"
    ).fetchone()["hypothesis_json"])

    assert result == {
        "experiment_id": "exp-1",
        "status": "NEEDS_PARAMETER_CONFIRMATION",
        "reason": "rebuild_source_daily_budget_out_of_range",
    }
    assert hypothesis["rebuild_auto_blocked_reason"] == "rebuild_source_daily_budget_out_of_range"
    assert hypothesis["rebuild_auto_next_step"] == "CONFIRM_REBUILD_PARAMETERS"


def test_bulk_generation_persists_authorization_and_worker_owns_continuation():
    root = Path(__file__).resolve().parents[1]
    workspace = (root / "app/static/ops/growth-workspace.js").read_text()
    worker = (root / "scripts/run_growth_meta_worker.py").read_text()

    assert "auto_rebuild_on_approval:true" in workspace
    assert "rebuild_initial_status:initialStatus" in workspace
    assert "rebuild_authorized_at:String(item.authorized_at" in workspace
    assert "bulkRebuildCreativePayload(item,batch)" in workspace
    assert "ApprovedRebuildAutopilot" in worker
    assert "approved_rebuilds.advance(" in worker
    assert "GROWTH_APPROVED_REBUILD_RECOVERY_BATCH_PREFIXES" in worker
