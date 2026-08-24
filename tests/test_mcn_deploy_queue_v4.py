from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "scripts/mcn_deploy_queue.py"
    spec = importlib.util.spec_from_file_location("mcn_deploy_queue_v4", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dq = load_module()


def enqueue(**kwargs):
    kwargs.setdefault("restart_policy", "none")
    return dq.enqueue(**kwargs)


def runner(tmp_path: Path) -> Path:
    path = tmp_path / "runner.sh"
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\nphase=test\nexit 0\n", encoding="utf-8")
    return path


def admission(*, ok: bool, reasons: list[str], invocation: str = "inv-before") -> dict:
    return {
        "ok": ok, "checked_at_utc": "2026-07-22T00:00:00+00:00", "reasons": reasons,
        "checks": {"restart_receipt": {"ok": "restart_receipt" not in reasons, "detail": {"invocation_id": invocation}}},
    }


def test_soft_blocked_head_does_not_block_later_independent_job(tmp_path: Path, monkeypatch):
    queue = tmp_path / "queue"
    script = runner(tmp_path)
    first = enqueue(
        release_id="blocked-20260722T000000Z", description="blocked", runner=script, artifacts=[],
        queue_root=queue, required_passes=1, priority_class=1, blocking_queues=["operations"],
    )
    second = enqueue(
        release_id="ready-20260722T000001Z", description="ready", runner=script, artifacts=[],
        queue_root=queue, required_passes=1, priority_class=2,
    )

    def fake_admission(job):
        return admission(ok=False, reasons=["production_queues"]) if job["release_id"].startswith("blocked") else admission(ok=True, reasons=[])

    monkeypatch.setattr(dq, "_collect_admission_window", fake_admission)
    monkeypatch.setattr(
        dq, "_execute_ready_job",
        lambda **kwargs: {"ok": True, "executed": kwargs["job"]["queue_id"]},
    )
    result = dq.run_once(queue_root=queue)
    assert result["executed"] == second["queue_id"]
    blocked_job = json.loads((queue / "queued" / first["queue_id"] / "job.json").read_text())
    assert blocked_job["soft_block_count"] == 1
    assert blocked_job["deferred_until_utc"]


def test_unattributed_restart_is_global_freeze(tmp_path: Path, monkeypatch):
    queue = tmp_path / "queue"
    script = runner(tmp_path)
    first = enqueue(
        release_id="one-20260722T000000Z", description="one", runner=script, artifacts=[],
        queue_root=queue, required_passes=1, priority_class=1,
    )
    enqueue(
        release_id="two-20260722T000001Z", description="two", runner=script, artifacts=[],
        queue_root=queue, required_passes=1, priority_class=2,
    )
    monkeypatch.setattr(dq, "_collect_admission_window", lambda job: admission(ok=False, reasons=["restart_receipt"]))
    result = dq.run_once(queue_root=queue)
    assert result["reason"] == "global_production_freeze"
    assert result["queue_id"] == first["queue_id"]
    assert len(list((queue / "queued").iterdir())) == 2


def test_priority_class_cannot_be_overtaken_by_age(tmp_path: Path):
    queue = tmp_path / "queue"
    script = runner(tmp_path)
    low = enqueue(
        release_id="low-20260722T000000Z", description="low", runner=script, artifacts=[],
        queue_root=queue, required_passes=1, priority_class=4,
    )
    high = enqueue(
        release_id="high-20260722T000001Z", description="high", runner=script, artifacts=[],
        queue_root=queue, required_passes=1, priority_class=1,
    )
    jobs, _ = dq._eligible_jobs(queue)
    assert [dq._load_job(path)["queue_id"] for path in jobs] == [high["queue_id"], low["queue_id"]]


def test_schema_v3_job_persists_scoped_contract(tmp_path: Path):
    queue = tmp_path / "queue"
    result = enqueue(
        release_id="scoped-20260722T000000Z", description="scoped", runner=runner(tmp_path), artifacts=[],
        queue_root=queue, required_passes=2, work_item_id="work_1", priority_class=2,
        restart_policy="none", dependency_units=["a.service"], blocking_units=["b.service"],
        blocking_queues=["operations"], required_resources=["automation_db_writer"], batch_id="batch_1",
        candidate_id="candidate_1", max_production_attempts=2,
    )
    job = json.loads((Path(result["path"]) / "job.json").read_text())
    assert job["schema_version"] == 3
    assert job["restart_policy"] == "none"
    assert job["dependency_units"] == ["a.service"]
    assert job["blocking_queues"] == ["operations"]
    assert job["candidate_id"] == "candidate_1"
    assert job["production_attempt_number"] == 1


def test_single_active_owner_per_work_item(tmp_path: Path):
    queue = tmp_path / "queue"
    script = runner(tmp_path)
    enqueue(
        release_id="owner-a-20260722T000000Z", description="owner-a",
        runner=script, artifacts=[], queue_root=queue, required_passes=1,
        work_item_id="work_same",
    )
    try:
        enqueue(
            release_id="owner-b-20260722T000001Z", description="owner-b",
            runner=script, artifacts=[], queue_root=queue, required_passes=1,
            work_item_id="work_same",
        )
    except RuntimeError as exc:
        assert str(exc) == "deploy_queue_work_item_already_active:work_same"
    else:
        raise AssertionError("duplicate active owner was accepted")


def test_candidate_production_attempt_budget_is_bounded(tmp_path: Path):
    queue = tmp_path / "queue"
    script = runner(tmp_path)
    first = enqueue(
        release_id="attempt-a-20260722T000000Z", description="attempt-a",
        runner=script, artifacts=[], queue_root=queue, required_passes=1,
        candidate_id="candidate_same", max_production_attempts=1,
    )
    source = Path(first["path"])
    job = json.loads((source / "job.json").read_text())
    job["state"] = "failed"
    (source / "job.json").write_text(json.dumps(job), encoding="utf-8")
    source.rename(queue / "failed" / source.name)
    try:
        enqueue(
            release_id="attempt-b-20260722T000001Z", description="attempt-b",
            runner=script, artifacts=[], queue_root=queue, required_passes=1,
            candidate_id="candidate_same", max_production_attempts=1,
        )
    except RuntimeError as exc:
        assert str(exc) == (
            "deploy_queue_candidate_attempt_budget_exhausted:candidate_same:1"
        )
    else:
        raise AssertionError("candidate attempt budget was bypassed")


def test_dispatcher_crash_recovery_moves_running_job_to_manual_review(tmp_path: Path):
    queue = tmp_path / "queue"
    result = enqueue(
        release_id="crash-20260722T000000Z", description="crash", runner=runner(tmp_path), artifacts=[],
        queue_root=queue, required_passes=1,
    )
    source = Path(result["path"])
    target = queue / "running" / source.name
    source.rename(target)
    job = json.loads((target / "job.json").read_text())
    job["state"] = "running"
    (target / "job.json").write_text(json.dumps(job), encoding="utf-8")
    outcome = dq.run_once(queue_root=queue)
    assert outcome["idle"] is True
    recovered = queue / "manual-review" / source.name
    assert recovered.is_dir()
    result_payload = json.loads((recovered / "job.json").read_text())["result"]
    assert result_payload["reason"] == "dispatcher_interrupted_after_runner_start"


def test_corrupted_runner_is_terminal_failed_not_retried(tmp_path: Path, monkeypatch):
    queue = tmp_path / "queue"
    result = enqueue(
        release_id="corrupt-20260722T000000Z", description="corrupt", runner=runner(tmp_path), artifacts=[],
        queue_root=queue, required_passes=1,
    )
    staged = Path(result["path"]) / "runner.sh"
    staged.chmod(0o600)
    staged.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(dq, "_collect_admission_window", lambda job: admission(ok=True, reasons=[]))
    outcome = dq.run_once(queue_root=queue)
    assert outcome["reason"] == "no_job_ready_this_pass"
    assert (queue / "failed" / Path(result["path"]).name).is_dir()


def test_restart_policy_is_explicit_for_cli_and_direct_enqueue(tmp_path: Path):
    try:
        dq._parser().parse_args([
            "enqueue", "--release-id", "explicit-20260722T000000Z",
            "--description", "explicit", "--runner", str(runner(tmp_path)),
        ])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("CLI silently defaulted restart policy")

    try:
        dq.enqueue(
            release_id="explicit-20260722T000000Z", description="explicit",
            runner=runner(tmp_path), artifacts=[], queue_root=tmp_path / "queue",
            required_passes=1,
        )
    except RuntimeError as exc:
        assert str(exc) == "deploy_queue_restart_policy_invalid"
    else:
        raise AssertionError("direct enqueue silently defaulted restart policy")


def _admission_stubs(monkeypatch, failed_output: str = "") -> list[list[str]]:
    commands: list[list[str]] = []

    def fake_run_json(command, **_kwargs):
        commands.append(list(command))
        if "mcn_release_governance.py" in " ".join(command):
            return 0, {
                "ok": True, "classification": "matching_passed_receipt",
                "current_invocation_id": "inv-ok", "matching_receipt": "/receipt.json",
            }
        return 0, {"ok": True, "admission": "allowed", "reasons": []}

    def fake_subprocess_run(command, **_kwargs):
        if command[:2] == ["systemctl", "--failed"]:
            return subprocess.CompletedProcess(command, 0, stdout=failed_output, stderr="")
        return subprocess.CompletedProcess(command, 3, stdout="", stderr="")

    monkeypatch.setattr(dq, "_run_json", fake_run_json)
    monkeypatch.setattr(dq.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(dq, "_lock_available", lambda _path: True)
    return commands


def test_resource_gates_follow_declared_contract(monkeypatch):
    commands = _admission_stubs(monkeypatch)
    plain = dq.collect_admission({"schema_version": 3, "restart_policy": "none"})
    assert plain["ok"] is True
    plain_command = commands[1]
    assert "--backend-health-url" in plain_command
    assert "--max-load1" not in plain_command
    assert "--max-db-locked-count" not in plain_command
    assert "--max-nginx-504-count" not in plain_command

    commands.clear()
    sqlite_job = dq.collect_admission({
        "schema_version": 3, "restart_policy": "none",
        "required_resources": ["automation_db_writer"],
    })
    assert sqlite_job["ok"] is True
    sqlite_command = commands[1]
    assert "--max-db-locked-count" in sqlite_command
    assert str(dq.ETL_LOCK) in sqlite_command
    assert str(dq.WRITER_LOCK) in sqlite_command
    assert "/run/lock/mcn-sqlite-etl.lock" not in sqlite_command
    assert "--max-load1" not in sqlite_command

    commands.clear()
    backend = dq.collect_admission({"schema_version": 3, "restart_policy": "backend"})
    assert backend["ok"] is True
    backend_command = commands[1]
    assert "--max-load1" in backend_command
    assert "--max-db-locked-count" in backend_command
    assert "--max-nginx-504-count" in backend_command


def test_unrelated_failed_unit_is_diagnostic_and_dependency_is_scoped(monkeypatch):
    failed = "mcn-daily-data-completion-notifier.service loaded failed failed notifier\n"
    _admission_stubs(monkeypatch, failed_output=failed)
    unrelated = dq.collect_admission({"schema_version": 3, "restart_policy": "backend"})
    assert unrelated["ok"] is True
    assert unrelated["checks"]["unrelated_failed_units"]["blocking"] is False
    assert "mcn-daily-data-completion-notifier.service" in unrelated["checks"]["unrelated_failed_units"]["detail"]

    dependent = dq.collect_admission({
        "schema_version": 3, "restart_policy": "backend",
        "dependency_units": ["mcn-daily-data-completion-notifier.service"],
    })
    assert dependent["ok"] is False
    assert dependent["reasons"] == ["dependency_failed_units"]


def test_failed_dependency_defers_only_its_candidate(tmp_path: Path, monkeypatch):
    queue = tmp_path / "queue"
    script = runner(tmp_path)
    blocked = enqueue(
        release_id="blocked-dependency-20260722T000000Z", description="blocked",
        runner=script, artifacts=[], queue_root=queue, required_passes=1,
        priority_class=1, restart_policy="backend",
        dependency_units=["mcn-daily-data-completion-notifier.service"],
    )
    ready = enqueue(
        release_id="ready-independent-20260722T000001Z", description="ready",
        runner=script, artifacts=[], queue_root=queue, required_passes=1,
        priority_class=2, restart_policy="none",
    )

    def fake_admission(job):
        if job["queue_id"] == blocked["queue_id"]:
            return admission(ok=False, reasons=["dependency_failed_units"])
        return admission(ok=True, reasons=[])

    monkeypatch.setattr(dq, "_collect_admission_window", fake_admission)
    monkeypatch.setattr(
        dq, "_execute_ready_job",
        lambda **kwargs: {"ok": True, "executed": kwargs["job"]["queue_id"]},
    )
    result = dq.run_once(queue_root=queue)
    assert result["executed"] == ready["queue_id"]
    blocked_job = json.loads((queue / "queued" / blocked["queue_id"] / "job.json").read_text())
    assert blocked_job["soft_block_count"] == 1
