#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(os.getenv("MCN_PROJECT_ROOT") or Path(__file__).resolve().parents[1]).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.linky_phase_admission import linky_source_phase_soft_reasons

from mcn_business_scheduler_shadow import _read_history, collect_telemetry, decide, load_contracts
from mcn_control_plane import (
    DEFAULT_DB,
    DEFAULT_RESOURCES,
    add_stage,
    canonical_json,
    claim_ready_stage,
    connect,
    migrate,
    reconcile_systemd_terminal_evidence,
    register_work,
    run_claimed_stage,
    transition_work,
)


DEFAULT_CONTRACTS = ROOT / "config/mcn_business_task_contracts.json"
DEFAULT_SHADOW_STATE = Path("/var/lib/mcn-ai-automation/business-scheduler-shadow")
PYTHON = str(ROOT / ".venv/bin/python")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def target_for(
    task: dict[str, Any], now: datetime, timezone_name: str, source_generation: str = "",
) -> tuple[str, str]:
    local = now.astimezone(ZoneInfo(timezone_name))
    cadence = str(task["cadence"])
    if cadence == "event_generation":
        generation = str(source_generation or "").strip()
        if not generation:
            raise RuntimeError("event_generation_source_required")
        target = generation
    elif cadence == "four_hour":
        slot = (local.hour // 4) * 4
        target = f"{local.date().isoformat()}T{slot:02d}:00"
    elif cadence == "daily_previous_day":
        target = (local.date() - timedelta(days=1)).isoformat()
    else:
        target = local.date().isoformat()
    deadline = ""
    deadline_local = str(task.get("deadline_local") or "")
    if deadline_local:
        hour, minute = (int(value) for value in deadline_local.split(":"))
        deadline_date = local.date()
        deadline = local.replace(
            year=deadline_date.year, month=deadline_date.month, day=deadline_date.day,
            hour=hour, minute=minute, second=0, microsecond=0,
        ).astimezone(timezone.utc).isoformat()
    return target, deadline


def idempotency_key(task: dict[str, Any], target: str, source_generation: str) -> str:
    return "|".join((
        str(task["task_type"]), str(task["app"]), target, "default",
        str(task["contract_version"]), source_generation,
    ))


def resource_claims(task: dict[str, Any]) -> list[str]:
    explicit = task.get("resource_claims")
    if isinstance(explicit, list):
        return sorted({str(value).strip() for value in explicit if str(value).strip()})
    claims: set[str] = set()
    phases = set(task.get("phases") or [])
    databases = set(task.get("target_databases") or [])
    if "network_pull" in phases:
        claims.add("network_fetch")
    if databases and phases.intersection({"sqlite_merge", "atomic_publish"}):
        claims.add("automation_db_writer")
    if "filesystem_maintenance" in phases:
        claims.add("maintenance_io")
    if float(task.get("estimated_temporary_gb") or 0) >= 3:
        claims.add("data_disk_heavy_io")
    if int(task.get("estimated_runtime_minutes") or 0) >= 30:
        claims.add("heavy_compute")
    return sorted(claims)


def transaction_scoped_resource_claims(task: dict[str, Any]) -> list[str]:
    explicit = task.get('transaction_scoped_resource_claims')
    if isinstance(explicit, list):
        return sorted({str(value).strip() for value in explicit if str(value).strip()})
    return []


def _supersede_older_generation(
    conn: sqlite3.Connection, *, task_id: str, target: str, current_work_id: str,
) -> list[str]:
    changed: list[str] = []
    rows = conn.execute(
        "SELECT work_id,state,metadata_json FROM work_items WHERE kind='business_batch' AND work_id<>? "
        "AND state IN ('developing','validated','package_ready','queued','blocked_soft','blocked_hard')",
        (current_work_id,),
    ).fetchall()
    for row in rows:
        metadata = json.loads(row["metadata_json"] or "{}")
        if metadata.get("task_id") != task_id or metadata.get("target") != target:
            continue
        conn.execute(
            "UPDATE work_stages SET state='superseded',updated_at_utc=? WHERE work_id=? "
            "AND state IN ('queued','blocked_soft','blocked_hard')",
            (utc_now().isoformat(), row["work_id"]),
        )
        conn.execute(
            "UPDATE work_items SET state='superseded',block_reason='newer_source_generation',updated_at_utc=?,version=version+1 "
            "WHERE work_id=?",
            (utc_now().isoformat(), row["work_id"]),
        )
        changed.append(str(row["work_id"]))
    return changed


def enqueue_task(
    *, task_id: str, source_generation: str, contracts_path: Path,
    db_path: Path, now: datetime | None = None, owner_thread_id: str = "",
) -> dict[str, Any]:
    contracts = load_contracts(contracts_path)
    task = next((row for row in contracts["tasks"] if row["id"] == task_id), None)
    if task is None:
        raise RuntimeError(f"business_task_unknown:{task_id}")
    generation = str(source_generation or "timer").strip()
    if not generation:
        raise RuntimeError("source_generation_required")
    target, deadline = target_for(
        task, now or utc_now(), str(contracts["timezone"]), generation,
    )
    key = idempotency_key(task, target, generation)
    migrate(db_path)
    with connect(db_path) as conn:
        owned = []
        for row in conn.execute(
            "SELECT * FROM work_items WHERE kind='business_batch' "
            "AND state NOT IN ('superseded','cancelled','failed') "
            "ORDER BY created_at_utc ASC"
        ).fetchall():
            metadata = json.loads(row["metadata_json"] or "{}")
            if metadata.get("task_id") == task_id and metadata.get("target") == target:
                # A no-op observation proves that the natural reconciliation
                # ran, but must never own or suppress a later real publication.
                if metadata.get("execution_mode") == "noop_observation":
                    continue
                # Ownership is generation-scoped.  In particular, an accepted
                # incident-recovery generation must not suppress the later
                # natural timer generation for the same task and target.
                if str(metadata.get("source_generation") or "").strip() != generation:
                    continue
                owned.append(row)
        if owned:
            # An accepted generation is authoritative only for the same
            # source generation. Repeated timer delivery remains idempotent,
            # while governed recovery and natural execution stay independent.
            work = dict(owned[0])
            return {
                "ok": True,
                "deduplicated": True,
                "target_owner_preserved": True,
                "work": work,
                "owner_thread_id": str(work.get("owner_thread_id") or ""),
                "subscription": {
                    "command": [
                        PYTHON, str(Path(__file__).resolve()), "--db", str(db_path),
                        "status-task", "--task-id", task_id, "--target", target,
                    ],
                    "mode": "durable_one_shot",
                },
                "stage": None,
                "superseded_work_ids": [],
            }
        registered = register_work(
            conn, kind="business_batch", title=f"{task_id} {target}", idempotency_key=key,
            priority_class=int(task["priority_class"]), owner_thread_id=owner_thread_id,
            deadline_at_utc=deadline,
            restart_policy="none", release_family="", initial_state="queued",
            metadata={
                "task_id": task_id, "target": target, "source_generation": generation,
                "freshness_slo": task.get("freshness_slo"), "service_unit": task["service_unit"],
                "timer_unit": task["timer_unit"], "contract_version": task["contract_version"],
                "supports_chunking": bool(task.get("supports_chunking")),
                "supports_resume": bool(task.get("supports_resume")),
                "retryable_exit_codes": [
                    int(code) for code in task.get("retryable_exit_codes") or []
                ],
                "retry_backoff_seconds": max(
                    30, min(int(task.get("retry_backoff_seconds") or 300), 3600)
                ),
                "transaction_scoped_resource_claims": transaction_scoped_resource_claims(task),
            },
        )
        work = registered["work"]
        if registered.get("registered"):
            claims = resource_claims(task)
            stage = add_stage(
                conn, work_id=work["work_id"], stage_key="run_service", ordinal=10,
                lane="maintenance" if "maintenance_io" in claims else "heavy_compute",
                resource_claims=claims, dependencies=[], dependency_units=[task["service_unit"]],
                command=[
                    PYTHON,
                    str(ROOT / "scripts" / "run_governed_systemd_task.py"),
                    "--unit",
                    task["service_unit"],
                ],
                idempotent=bool(task.get("supports_resume")), max_attempts=5 if task.get("supports_resume") else 1,
            )["stage"]
            superseded = _supersede_older_generation(
                conn, task_id=task_id, target=target, current_work_id=work["work_id"],
            )
        else:
            stage_row = conn.execute(
                "SELECT * FROM work_stages WHERE work_id=? AND stage_key='run_service'", (work["work_id"],)
            ).fetchone()
            stage = dict(stage_row) if stage_row else None
            superseded = []
    return {
        "ok": True, "deduplicated": bool(registered.get("deduplicated")), "work": work,
        "stage": stage, "superseded_work_ids": superseded,
        "owner_thread_id": str(work.get("owner_thread_id") or ""),
    }


def status_task(*, db_path: Path, task_id: str, target: str = "") -> dict[str, Any]:
    migrate(db_path)
    matches: list[dict[str, Any]] = []
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM work_items WHERE kind='business_batch' ORDER BY created_at_utc DESC"
        ).fetchall()
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            if metadata.get("task_id") != task_id:
                continue
            if target and metadata.get("target") != target:
                continue
            item = dict(row)
            item["metadata"] = metadata
            item.pop("metadata_json", None)
            item["stages"] = [
                dict(stage) for stage in conn.execute(
                    "SELECT stage_id,stage_key,state,attempt_count,soft_block_count,max_attempts,"
                    "lease_owner,lease_expires_at_utc,"
                    "not_before_utc,result_json,updated_at_utc FROM work_stages "
                    "WHERE work_id=? ORDER BY ordinal,stage_id",
                    (row["work_id"],),
                ).fetchall()
            ]
            matches.append(item)
            if target or len(matches) >= 20:
                break
    return {"ok": True, "task_id": task_id, "target": target, "work": matches}


def reconcile_verifying_business_work(
    conn: sqlite3.Connection, *, work_id: str = "",
) -> list[str]:
    params: tuple[Any, ...] = ()
    where = "kind='business_batch' AND state='verifying'"
    if work_id:
        where += " AND work_id=?"
        params = (work_id,)
    accepted: list[str] = []
    for work in conn.execute(f"SELECT work_id,version FROM work_items WHERE {where}", params).fetchall():
        stages = conn.execute(
            "SELECT state,result_json FROM work_stages WHERE work_id=? ORDER BY ordinal,stage_id",
            (work["work_id"],),
        ).fetchall()
        if not stages or any(stage["state"] != "succeeded" for stage in stages):
            continue
        results = [json.loads(stage["result_json"] or "{}") for stage in stages]
        if any(int(result.get("returncode", -1)) != 0 for result in results):
            continue
        transition_work(
            conn, work_id=work["work_id"], to_state="accepted",
            expected_version=int(work["version"]), reason="all_stages_succeeded",
            metadata_patch={"verification": "persisted_stage_results_passed"},
        )
        accepted.append(str(work["work_id"]))
    return accepted


def dispatch_once(*, db_path: Path, resources_path: Path, owner: str) -> dict[str, Any]:
    migrate(db_path)
    with connect(db_path) as conn:
        claimed = claim_ready_stage(conn, owner=owner, resources_path=resources_path)
        if not claimed.get("claimed"):
            return claimed
        stage = claimed["stage"]
        work = conn.execute(
            "SELECT metadata_json FROM work_items WHERE work_id=?",
            (stage["work_id"],),
        ).fetchone()
        metadata = json.loads(work["metadata_json"] or "{}") if work else {}
        result = run_claimed_stage(
            conn, stage_id=stage["stage_id"], owner=owner,
            timeout_seconds=6 * 60 * 60, lease_seconds=300,
            retryable_exit_codes=metadata.get("retryable_exit_codes") or [],
            soft_backoff_seconds=int(metadata.get("retry_backoff_seconds") or 300),
        )
        result["accepted_work_ids"] = reconcile_verifying_business_work(
            conn, work_id=str(stage["work_id"]),
        )
        return result


def dispatch_pool(*, db_path: Path, resources_path: Path, owner: str, workers: int) -> dict[str, Any]:
    count = max(1, min(int(workers), 8))
    with ThreadPoolExecutor(max_workers=count, thread_name_prefix="mcn-control-plane") as pool:
        futures = [
            pool.submit(
                dispatch_once, db_path=db_path, resources_path=resources_path,
                owner=f"{owner}-{index + 1}",
            )
            for index in range(count)
        ]
        results = [future.result() for future in futures]
    return {"ok": all(result.get("ok") for result in results), "workers": count, "results": results}


def apply_admission_snapshot(
    *, db_path: Path, contracts_path: Path, state_dir: Path,
    sample_seconds: float = 1.0,
) -> dict[str, Any]:
    contracts = load_contracts(contracts_path)
    telemetry = collect_telemetry(contracts, sample_seconds=sample_seconds)
    decision = decide(contracts, telemetry, _read_history(state_dir))
    if decision.get("global_freeze"):
        return {"ok": False, "global_freeze": True, "reasons": decision.get("global_hard_reasons") or []}
    decisions = {row["task_id"]: row for row in decision.get("tasks") or []}
    task_contracts = {row["id"]: row for row in contracts.get("tasks") or []}
    changed: list[dict[str, Any]] = []
    now = utc_now()
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT work_id,state,block_reason,metadata_json FROM work_items WHERE kind='business_batch' "
            "AND state IN ('queued','blocked_soft','blocked_hard','escalated')"
        ).fetchall()
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            task_decision = decisions.get(str(metadata.get("task_id") or ""))
            if not task_decision:
                continue
            hard = list(task_decision.get("hard_reasons") or [])
            task_id = str(metadata.get("task_id") or "")
            task_contract = task_contracts.get(task_id) or {}
            soft = linky_source_phase_soft_reasons(
                task_id=task_id,
                resource_claims=resource_claims(task_contract),
                soft_reasons=task_decision.get("soft_reasons") or [],
            )
            if hard:
                target_state, stage_state, reason, not_before = "blocked_hard", "blocked_hard", f"admission:{','.join(hard)}", ""
            elif soft:
                target_state = "escalated" if row["state"] == "escalated" else "blocked_soft"
                stage_state, reason = "blocked_soft", f"admission:{','.join(soft)}"
                not_before = (now + timedelta(minutes=5)).isoformat()
            else:
                target_state = "escalated" if row["state"] == "escalated" else "queued"
                stage_state, reason, not_before = "queued", "", ""
            if row["state"] == target_state and row["block_reason"] == reason:
                continue
            if row["state"] == "blocked_hard" and not str(row["block_reason"] or "").startswith("admission:"):
                continue
            conn.execute(
                "UPDATE work_items SET state=?,block_reason=?,not_before_utc=?,updated_at_utc=?,version=version+1 WHERE work_id=?",
                (target_state, reason, not_before, now.isoformat(), row["work_id"]),
            )
            conn.execute(
                "UPDATE work_stages SET state=?,not_before_utc=?,updated_at_utc=?,version=version+1 WHERE work_id=? "
                "AND state IN ('queued','blocked_soft','blocked_hard')",
                (stage_state, not_before, now.isoformat(), row["work_id"]),
            )
            changed.append({"work_id": row["work_id"], "state": target_state, "reason": reason})
    return {"ok": True, "global_freeze": False, "changed": changed, "decision": decision}


def admitted_dispatch_pool(
    *, db_path: Path, resources_path: Path, contracts_path: Path, state_dir: Path,
    owner: str, workers: int, sample_seconds: float,
) -> dict[str, Any]:
    migrate(db_path)
    with connect(db_path) as conn:
        terminal_reconciliation = reconcile_systemd_terminal_evidence(conn)
        recovered_acceptances = reconcile_verifying_business_work(conn)
    gate = apply_admission_snapshot(
        db_path=db_path, contracts_path=contracts_path, state_dir=state_dir,
        sample_seconds=sample_seconds,
    )
    if not gate.get("ok"):
        return {"ok": True, "deferred": True, "reason": "global_hard_gate", "gate": gate}
    dispatched = dispatch_pool(db_path=db_path, resources_path=resources_path, owner=owner, workers=workers)
    return {
        "ok": dispatched.get("ok") is True, "gate": gate, "dispatch": dispatched,
        "terminal_reconciliation": terminal_reconciliation,
        "recovered_acceptances": recovered_acceptances,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persistent MCN business task scheduler")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--resources", type=Path, default=DEFAULT_RESOURCES)
    parser.add_argument("--contracts", type=Path, default=DEFAULT_CONTRACTS)
    parser.add_argument("--shadow-state-dir", type=Path, default=DEFAULT_SHADOW_STATE)
    sub = parser.add_subparsers(dest="command", required=True)
    enqueue = sub.add_parser("enqueue-task")
    enqueue.add_argument("--task-id", required=True)
    enqueue.add_argument("--source-generation", default="timer")
    enqueue.add_argument(
        "--owner-thread",
        default=os.getenv("MCN_TASK_OWNER_THREAD_ID") or os.getenv("CODEX_THREAD_ID") or "systemd:timer",
    )
    dispatch = sub.add_parser("dispatch-once")
    dispatch.add_argument("--owner", default=f"dispatcher-{os.getpid()}")
    pool = sub.add_parser("dispatch-pool")
    pool.add_argument("--owner", default=f"dispatcher-{os.getpid()}")
    pool.add_argument("--workers", type=int, default=4)
    pool.add_argument("--sample-seconds", type=float, default=1.0)
    sub.add_parser("reconcile-verifying")
    status = sub.add_parser("status-task")
    status.add_argument("--task-id", required=True)
    status.add_argument("--target", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "enqueue-task":
            result = enqueue_task(
                task_id=args.task_id, source_generation=args.source_generation,
                contracts_path=args.contracts, db_path=args.db,
                owner_thread_id=args.owner_thread,
            )
        elif args.command == "dispatch-once":
            result = dispatch_once(db_path=args.db, resources_path=args.resources, owner=args.owner)
        elif args.command == "dispatch-pool":
            result = admitted_dispatch_pool(
                db_path=args.db, resources_path=args.resources, contracts_path=args.contracts,
                state_dir=args.shadow_state_dir, owner=args.owner, workers=args.workers,
                sample_seconds=args.sample_seconds,
            )
        elif args.command == "reconcile-verifying":
            migrate(args.db)
            with connect(args.db) as conn:
                terminal = reconcile_systemd_terminal_evidence(conn)
                accepted = reconcile_verifying_business_work(conn)
            result = {
                "ok": True,
                "terminal_reconciliation": terminal.get("recovered") or [],
                "accepted_work_ids": accepted,
            }
        else:
            result = status_task(db_path=args.db, task_id=args.task_id, target=args.target)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(exc).__name__}:{str(exc)[:500]}"}
    print(canonical_json(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
