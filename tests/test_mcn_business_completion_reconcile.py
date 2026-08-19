from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from scripts import mcn_business_completion_reconcile as reconciler


def _source(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE streamer_external_sync_runs ("
            "run_id TEXT,app_name TEXT,date_from TEXT,date_to TEXT,status TEXT,guild_count INTEGER,"
            "profile_count INTEGER,revenue_count INTEGER,error_code TEXT,error_message TEXT,"
            "created_at TEXT,updated_at TEXT,run_scope TEXT,scope_key TEXT)"
        )
        connection.execute(
            "INSERT INTO streamer_external_sync_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run-linky", "linky", "2026-08-03", "2026-08-03", "success", 8, 3410, 3410,
                "", "", "2026-08-04T03:46:26+00:00", "2026-08-04T04:20:39+00:00", "full", "",
            ),
        )


def _analytics(path: Path, *, status: str = "ready") -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE streamer_analytics_materialization_state ("
            "app_name TEXT,status TEXT,data_as_of TEXT,profile_count INTEGER,streamer_daily_count INTEGER,"
            "daily_summary_count INTEGER,error_message TEXT,materialized_at TEXT)"
        )
        connection.execute(
            "INSERT INTO streamer_analytics_materialization_state VALUES (?,?,?,?,?,?,?,?)",
            ("linky", status, "2026-08-03", 296822, 1671398, 2016, "", "2026-08-04T12:20:46+08:00"),
        )


def _newcomer_publication(path: Path, *, guild_rows: int = 2) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE newcomer_daily_publications (
              platform TEXT,business_date TEXT,revision INTEGER,status TEXT,
              publication_type TEXT,date_contract TEXT,expected_guild_count INTEGER,
              completed_guild_count INTEGER,summary_count INTEGER,member_count INTEGER,
              unique_member_count INTEGER,checksum TEXT,completed_at TEXT,created_at TEXT
            );
            CREATE TABLE newcomer_daily_publication_guilds (
              platform TEXT,business_date TEXT,revision INTEGER,guild_executor_key TEXT,
              guild_id TEXT,guild_name TEXT,country TEXT,summary_count INTEGER,
              member_count INTEGER,unique_member_count INTEGER,real_person_count INTEGER,
              checksum TEXT
            );
            CREATE TABLE newcomer_publication_events (
              event_id TEXT,event_type TEXT,platform TEXT,business_date TEXT,revision INTEGER,
              checksum TEXT,payload_json TEXT,delivery_status TEXT,attempt_count INTEGER,
              max_attempts INTEGER,next_attempt_at TEXT,last_error TEXT,created_at TEXT,
              delivered_at TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO newcomer_daily_publications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "linky", "2026-08-17", 1, "complete", "complete", "contract", 2, 2,
                2, 2, 2, "checksum", "2026-08-18T01:27:37+00:00",
                "2026-08-18T01:27:37+00:00",
            ),
        )
        for index in range(guild_rows):
            connection.execute(
                "INSERT INTO newcomer_daily_publication_guilds VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "linky", "2026-08-17", 1, f"guild-{index}", str(index),
                    f"Guild {index}", "Indonesia", 1, 1, 1, 0, f"guild-checksum-{index}",
                ),
            )
        connection.execute(
            "INSERT INTO newcomer_publication_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "event-1", "mcn.newcomers.daily.completed", "linky", "2026-08-17", 1,
                "checksum", "{}", "delivered", 1, 8, "", "",
                "2026-08-18T01:27:37+00:00", "2026-08-18T01:29:18+00:00",
            ),
        )


def _control(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE work_items (
              work_id TEXT PRIMARY KEY,kind TEXT,title TEXT,state TEXT,priority_class INTEGER,
              owner_thread_id TEXT,idempotency_key TEXT,deadline_at_utc TEXT,not_before_utc TEXT,
              restart_policy TEXT,release_family TEXT,baseline_sha256 TEXT,artifact_sha256 TEXT,
              runner_sha256 TEXT,block_reason TEXT,metadata_json TEXT,created_at_utc TEXT,
              updated_at_utc TEXT,version INTEGER
            );
            CREATE TABLE work_stages (
              stage_id TEXT PRIMARY KEY,work_id TEXT,stage_key TEXT,ordinal INTEGER,lane TEXT,state TEXT,
              resource_claims_json TEXT,dependency_stage_ids_json TEXT,dependency_units_json TEXT,
              command_json TEXT,idempotent INTEGER,max_attempts INTEGER,attempt_count INTEGER,
              soft_block_count INTEGER,not_before_utc TEXT,lease_owner TEXT,lease_expires_at_utc TEXT,
              started_at_utc TEXT,finished_at_utc TEXT,result_json TEXT,created_at_utc TEXT,
              updated_at_utc TEXT,version INTEGER
            );
            CREATE TABLE resource_leases(resource_name TEXT,slot INTEGER,stage_id TEXT,owner TEXT,acquired_at_utc TEXT,expires_at_utc TEXT);
            CREATE TABLE work_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,work_id TEXT,stage_id TEXT,event_type TEXT,from_state TEXT,to_state TEXT,detail_json TEXT,created_at_utc TEXT);
            """
        )
        metadata = json.dumps({"task_id": "linky-daily-incremental", "target": "2026-08-03"})
        connection.execute(
            "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "work-linky", "business_batch", "linky", "escalated", 1, "", "key",
                "2026-08-04T03:00:00+00:00", "", "none", "", "", "", "", "", metadata,
                "2026-08-04T02:00:00+00:00", "2026-08-04T03:14:36+00:00", 7,
            ),
        )
        connection.execute(
            "INSERT INTO work_stages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "stage-linky", "work-linky", "run_service", 10, "heavy_compute", "manual_review",
                "[]", "[]", "[]", "[]", 1, 5, 5, 61, "", "", "", "", "",
                json.dumps({"reason": "soft_retry_budget_exhausted", "returncode": 1}),
                "2026-08-04T02:00:00+00:00", "2026-08-04T03:14:36+00:00", 12,
            ),
        )


def test_requires_source_success_and_ready_publication(tmp_path: Path) -> None:
    source = tmp_path / "automation.db"
    analytics = tmp_path / "analytics.db"
    _source(source)
    _analytics(analytics, status="running")

    assert reconciler.collect_publication_evidence(source, analytics) == []


def test_newcomer_evidence_requires_complete_exact_guild_publication(tmp_path: Path) -> None:
    complete = tmp_path / "complete.db"
    incomplete = tmp_path / "incomplete.db"
    _newcomer_publication(complete)
    _newcomer_publication(incomplete, guild_rows=1)

    evidence = reconciler.collect_newcomer_publication_evidence(complete)
    assert len(evidence) == 1
    assert evidence[0]["task_id"] == "linky-daily-newcomers"
    assert evidence[0]["guild_count"] == 2
    assert evidence[0]["completion_event_id"] == "event-1"
    assert reconciler.collect_newcomer_publication_evidence(incomplete) == []


def test_newcomer_manual_review_reconciles_without_replaying_service(tmp_path: Path) -> None:
    source = tmp_path / "automation.db"
    control = tmp_path / "control.db"
    _newcomer_publication(source)
    _control(control)
    with sqlite3.connect(control) as connection:
        metadata = json.dumps({
            "task_id": "linky-daily-newcomers",
            "target": "2026-08-17",
            "service_unit": "mcn-linky-daily-newcomers.service",
        })
        command = json.dumps([
            "/opt/mcn-ai-automation/.venv/bin/python",
            "run_governed_systemd_task.py",
            "--unit",
            "mcn-linky-daily-newcomers.service",
        ])
        dependency_units = json.dumps(["mcn-linky-daily-newcomers.service"])
        connection.execute(
            "UPDATE work_items SET metadata_json=?,state='manual_review',"
            "deadline_at_utc='2026-08-18T03:00:00+00:00'",
            (metadata,),
        )
        connection.execute(
            "UPDATE work_stages SET dependency_units_json=?,command_json=?,"
            "result_json=?,state='manual_review'",
            (
                dependency_units,
                command,
                json.dumps({"reason": "expired_running_lease_uncertain_outcome"}),
            ),
        )

    evidence = reconciler.collect_newcomer_publication_evidence(source)
    result = reconciler.reconcile_control_plane(
        control, evidence, now=datetime(2026, 8, 18, 2, 0, tzinfo=timezone.utc)
    )

    assert result["changed"][0]["contract"] == "complete_newcomer_publication_v1"
    assert result["changed"][0]["deadline_missed"] is False
    with sqlite3.connect(control) as connection:
        assert connection.execute("SELECT state FROM work_items").fetchone()[0] == "accepted"
        stage_state, stage_result = connection.execute(
            "SELECT state,result_json FROM work_stages"
        ).fetchone()
    assert stage_state == "succeeded"
    recovered = json.loads(stage_result)
    assert recovered["recovered_from"] == "durable_business_completion_evidence"
    assert recovered["completion_reconciliation"]["durable_evidence"]["guild_count"] == 2


def test_newcomer_evidence_refuses_mismatched_service_unit(tmp_path: Path) -> None:
    source = tmp_path / "automation.db"
    control = tmp_path / "control.db"
    _newcomer_publication(source)
    _control(control)
    with sqlite3.connect(control) as connection:
        connection.execute(
            "UPDATE work_items SET metadata_json=?,state='manual_review'",
            (json.dumps({
                "task_id": "linky-daily-newcomers",
                "target": "2026-08-17",
                "service_unit": "mcn-other.service",
            }),),
        )
        connection.execute(
            "UPDATE work_stages SET dependency_units_json=?,command_json=?,state='manual_review'",
            (json.dumps(["mcn-other.service"]), json.dumps(["mcn-other.service"])),
        )

    result = reconciler.reconcile_control_plane(
        control, reconciler.collect_newcomer_publication_evidence(source), dry_run=True
    )

    assert result["changed"] == []
    assert result["refused"] == [{
        "work_id": "work-linky",
        "reason": "service_unit_evidence_mismatch",
    }]


def test_reconciles_manual_review_and_preserves_late_failure_history(tmp_path: Path) -> None:
    source = tmp_path / "automation.db"
    analytics = tmp_path / "analytics.db"
    control = tmp_path / "control.db"
    _source(source)
    _analytics(analytics)
    _control(control)
    evidence = reconciler.collect_publication_evidence(source, analytics)

    result = reconciler.reconcile_control_plane(
        control, evidence, now=datetime(2026, 8, 4, 6, 0, tzinfo=timezone.utc)
    )

    assert result["ok"] is True
    assert result["changed"][0]["deadline_missed"] is True
    with sqlite3.connect(control) as connection:
        work = connection.execute(
            "SELECT state,block_reason,metadata_json FROM work_items"
        ).fetchone()
        stage = connection.execute("SELECT state,result_json FROM work_stages").fetchone()
        event = connection.execute(
            "SELECT event_type,from_state,to_state FROM work_events"
        ).fetchone()
    assert work[0:2] == ("accepted", "accepted_after_deadline")
    assert json.loads(work[2])["freshness_slo_missed"] is True
    assert stage[0] == "succeeded"
    stage_result = json.loads(stage[1])
    assert stage_result["recovered_from"] == "durable_business_completion_evidence"
    assert stage_result["completion_reconciliation"]["original_stage_result"]["returncode"] == 1
    assert event == ("business_completion_reconciled", "escalated", "accepted")

    second = reconciler.reconcile_control_plane(control, evidence)
    assert second["changed"] == []


def test_incident_generation_refuses_completion_evidence_created_before_work(tmp_path: Path) -> None:
    source = tmp_path / "automation.db"
    analytics = tmp_path / "analytics.db"
    control = tmp_path / "control.db"
    _source(source)
    _analytics(analytics)
    _control(control)
    with sqlite3.connect(control) as connection:
        metadata = {
            "task_id": "linky-daily-incremental",
            "target": "2026-08-03",
            "source_generation": "incident-recovery-test",
        }
        connection.execute(
            "UPDATE work_items SET metadata_json=?,created_at_utc=?",
            (json.dumps(metadata), "2026-08-04T05:00:00+00:00"),
        )

    result = reconciler.reconcile_control_plane(
        control, reconciler.collect_publication_evidence(source, analytics), dry_run=True
    )

    assert result["changed"] == []
    assert result["refused"] == [{
        "work_id": "work-linky",
        "reason": "evidence_predates_non_timer_work",
    }]


def test_shadow_fallback_reads_only_exact_target_from_atomic_snapshot(tmp_path: Path) -> None:
    snapshot = tmp_path / "latest.json"
    evidence = [{
        "task_id": "linky-daily-incremental",
        "target": "2026-08-03",
        "evidence_id": "proof",
        "source_run_id": "run-linky",
        "publication_data_as_of": "2026-08-03",
        "publication_materialized_at_utc": "2026-08-04T04:20:46+00:00",
    }]
    reconciler.write_snapshot(
        snapshot,
        reconciler.build_snapshot(evidence, generated_at=datetime(2026, 8, 4, 5, 0, tzinfo=timezone.utc)),
    )

    result = reconciler.publication_completion_for_shadow(
        {"id": "linky-daily-incremental", "cadence": "daily_previous_day"},
        datetime(2026, 8, 4, 6, 0, tzinfo=timezone.utc),
        "Asia/Shanghai",
        snapshot_path=snapshot,
    )

    assert result["completed"] is True
    assert result["source_run_id"] == "run-linky"


def test_systemd_timer_is_bounded_and_does_not_start_business_units() -> None:
    root = Path(__file__).resolve().parents[1]
    service = (root / "scripts/systemd/mcn-business-completion-reconcile.service").read_text()
    timer = (root / "scripts/systemd/mcn-business-completion-reconcile.timer").read_text()

    assert "TimeoutStartSec=45s" in service
    assert "mcn_business_completion_reconcile.py" in service
    assert "OnUnitActiveSec=5min" in timer
    assert "mcn-linky-external-feed.service" not in service + timer
