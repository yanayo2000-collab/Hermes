#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo


DEFAULT_CONTROL_DB = Path("/data/mcn-data/control/mcn_control_plane.db")
DEFAULT_AUTOMATION_DB = Path("/opt/mcn-ai-automation/data/automation.db")
DEFAULT_ANALYTICS_DB = Path("/data/mcn-data/analytics/streamer_analytics.db")
DEFAULT_SNAPSHOT = Path(
    "/var/lib/mcn-ai-automation/business-completion-evidence/latest.json"
)
SUPPORTED_DAILY_PUBLICATIONS = {
    "linky-daily-incremental": "linky",
    "sugo-daily-incremental": "sugo",
}
SUPPORTED_NEWCOMER_PUBLICATIONS = {
    "linky-daily-newcomers": "linky",
    "timo-daily-newcomers": "timo",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=2000")
    return connection


def _writable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=15.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA busy_timeout=15000")
    return connection


def collect_publication_evidence(
    automation_db: Path,
    analytics_db: Path,
) -> list[dict[str, Any]]:
    """Return only exact source-success plus ready-publication matches."""
    with _readonly(analytics_db) as analytics:
        materializations = {
            str(row["app_name"]): dict(row)
            for row in analytics.execute(
                "SELECT app_name,status,data_as_of,profile_count,streamer_daily_count,"
                "daily_summary_count,error_message,materialized_at "
                "FROM streamer_analytics_materialization_state "
                "WHERE app_name IN ('linky','sugo')"
            ).fetchall()
        }
    task_for_app = {app: task_id for task_id, app in SUPPORTED_DAILY_PUBLICATIONS.items()}
    evidence: list[dict[str, Any]] = []
    with _readonly(automation_db) as source:
        runs = source.execute(
            "SELECT run_id,app_name,date_from,date_to,status,guild_count,profile_count,"
            "revenue_count,error_code,error_message,created_at,updated_at,run_scope,scope_key "
            "FROM streamer_external_sync_runs "
            "WHERE status='success' AND app_name IN ('linky','sugo') "
            "ORDER BY updated_at DESC LIMIT 120"
        ).fetchall()
    seen: set[tuple[str, str]] = set()
    for row in runs:
        app = str(row["app_name"] or "")
        target = str(row["date_to"] or "")
        key = (app, target)
        if key in seen or not target:
            continue
        materialized = materializations.get(app) or {}
        if str(materialized.get("status") or "") != "ready":
            continue
        data_as_of = str(materialized.get("data_as_of") or "")
        if data_as_of < target:
            continue
        run_updated = _parse_timestamp(row["updated_at"])
        materialized_at = _parse_timestamp(materialized.get("materialized_at"))
        if run_updated is None or materialized_at is None:
            continue
        record = {
            "evidence_contract": "source_success_plus_ready_publication_v1",
            "task_id": task_for_app[app],
            "app": app,
            "target": target,
            "source_run_id": str(row["run_id"] or ""),
            "source_status": "success",
            "source_updated_at_utc": run_updated.astimezone(timezone.utc).isoformat(),
            "guild_count": int(row["guild_count"] or 0),
            "source_profile_count": int(row["profile_count"] or 0),
            "source_revenue_count": int(row["revenue_count"] or 0),
            "publication_status": "ready",
            "publication_data_as_of": data_as_of,
            "publication_materialized_at_utc": materialized_at.astimezone(timezone.utc).isoformat(),
            "publication_profile_count": int(materialized.get("profile_count") or 0),
            "publication_streamer_daily_count": int(materialized.get("streamer_daily_count") or 0),
            "publication_daily_summary_count": int(materialized.get("daily_summary_count") or 0),
        }
        record["evidence_id"] = hashlib.sha256(
            canonical_json(record).encode("utf-8")
        ).hexdigest()
        evidence.append(record)
        seen.add(key)
    return evidence


def collect_newcomer_publication_evidence(
    automation_db: Path,
) -> list[dict[str, Any]]:
    """Return exact, internally consistent terminal newcomer publications.

    A publication is evidence only when its latest revision is complete, all
    expected guild rows are present, aggregate counts match, and the durable
    completion event carries the same checksum.  No service is started here.
    """
    task_for_app = {
        app: task_id for task_id, app in SUPPORTED_NEWCOMER_PUBLICATIONS.items()
    }
    evidence: list[dict[str, Any]] = []
    with _readonly(automation_db) as source:
        publications = source.execute(
            "SELECT p.* FROM newcomer_daily_publications p "
            "JOIN (SELECT platform,business_date,MAX(revision) AS revision "
            "FROM newcomer_daily_publications WHERE platform IN ('linky','timo') "
            "GROUP BY platform,business_date) latest "
            "ON latest.platform=p.platform AND latest.business_date=p.business_date "
            "AND latest.revision=p.revision "
            "WHERE p.status='complete' AND p.publication_type='complete' "
            "ORDER BY p.completed_at DESC LIMIT 120"
        ).fetchall()
        for row in publications:
            app = str(row["platform"] or "").lower()
            target = str(row["business_date"] or "")
            revision = int(row["revision"] or 0)
            expected = int(row["expected_guild_count"] or 0)
            completed = int(row["completed_guild_count"] or 0)
            checksum = str(row["checksum"] or "")
            completed_at = _parse_timestamp(row["completed_at"])
            if (
                app not in task_for_app
                or not target
                or revision < 1
                or expected < 1
                or completed != expected
                or not checksum
                or completed_at is None
            ):
                continue
            aggregate = source.execute(
                "SELECT COUNT(*) AS guild_rows,COUNT(DISTINCT guild_executor_key) AS guilds,"
                "COALESCE(SUM(summary_count),0) AS summary_count,"
                "COALESCE(SUM(member_count),0) AS member_count,"
                "COALESCE(SUM(unique_member_count),0) AS unique_member_count "
                "FROM newcomer_daily_publication_guilds "
                "WHERE platform=? AND business_date=? AND revision=?",
                (app, target, revision),
            ).fetchone()
            if (
                aggregate is None
                or int(aggregate["guild_rows"] or 0) != expected
                or int(aggregate["guilds"] or 0) != expected
                or int(aggregate["summary_count"] or 0) != int(row["summary_count"] or 0)
                or int(aggregate["member_count"] or 0) != int(row["member_count"] or 0)
                or int(aggregate["unique_member_count"] or 0)
                != int(row["unique_member_count"] or 0)
            ):
                continue
            event = source.execute(
                "SELECT event_id,delivery_status,created_at,delivered_at FROM newcomer_publication_events "
                "WHERE platform=? AND business_date=? AND revision=? AND checksum=? "
                "AND event_type='mcn.newcomers.daily.completed' "
                "ORDER BY created_at DESC LIMIT 1",
                (app, target, revision, checksum),
            ).fetchone()
            if event is None:
                continue
            completed_at_utc = completed_at.astimezone(timezone.utc).isoformat()
            record = {
                "evidence_contract": "complete_newcomer_publication_v1",
                "task_id": task_for_app[app],
                "app": app,
                "target": target,
                "service_unit": f"mcn-{app}-daily-newcomers.service",
                "source_run_id": f"newcomer:{app}:{target}:revision:{revision}",
                "source_status": "success",
                "source_updated_at_utc": completed_at_utc,
                "guild_count": expected,
                "publication_status": "complete",
                "publication_data_as_of": target,
                "publication_materialized_at_utc": completed_at_utc,
                "publication_revision": revision,
                "publication_checksum": checksum,
                "publication_summary_count": int(row["summary_count"] or 0),
                "publication_member_count": int(row["member_count"] or 0),
                "publication_unique_member_count": int(row["unique_member_count"] or 0),
                "completion_event_id": str(event["event_id"] or ""),
                "completion_event_delivery_status": str(event["delivery_status"] or ""),
            }
            record["evidence_id"] = hashlib.sha256(
                canonical_json(record).encode("utf-8")
            ).hexdigest()
            evidence.append(record)
    return evidence


def build_snapshot(evidence: Sequence[dict[str, Any]], *, generated_at: datetime | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at_utc": (generated_at or utc_now()).astimezone(timezone.utc).isoformat(),
        "evidence_contract": "source_success_plus_ready_publication_v1",
        "completions": list(evidence),
    }


def write_snapshot(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o750)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".latest.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o640)
    os.replace(temporary, path)


def _work_metadata(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(str(row["metadata_json"] or "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def reconcile_control_plane(
    control_db: Path,
    evidence: Sequence[dict[str, Any]],
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    evidence_by_key = {
        (str(item.get("task_id") or ""), str(item.get("target") or "")): dict(item)
        for item in evidence
    }
    marker = (now or utc_now()).astimezone(timezone.utc)
    changed: list[dict[str, Any]] = []
    refused: list[dict[str, str]] = []
    connection_factory = _readonly if dry_run else _writable
    with connection_factory(control_db) as connection:
        rows = connection.execute(
            "SELECT * FROM work_items WHERE kind='business_batch' "
            "AND state IN ('blocked_soft','blocked_hard','escalated','manual_review')"
        ).fetchall()
        for work in rows:
            metadata = _work_metadata(work)
            task_id = str(metadata.get("task_id") or "")
            target = str(metadata.get("target") or "")
            proof = evidence_by_key.get((task_id, target))
            if proof is None:
                continue
            stages = connection.execute(
                "SELECT * FROM work_stages WHERE work_id=? ORDER BY ordinal,stage_id",
                (work["work_id"],),
            ).fetchall()
            if len(stages) != 1 or str(stages[0]["stage_key"] or "") != "run_service":
                refused.append({"work_id": str(work["work_id"]), "reason": "stage_shape_not_single_run_service"})
                continue
            stage = stages[0]
            if str(stage["state"] or "") not in {"manual_review", "blocked_soft", "blocked_hard", "failed"}:
                refused.append({"work_id": str(work["work_id"]), "reason": "stage_state_not_reconcilable"})
                continue
            service_unit = str(proof.get("service_unit") or "")
            if service_unit:
                try:
                    dependency_units = json.loads(str(stage["dependency_units_json"] or "[]"))
                    command = json.loads(str(stage["command_json"] or "[]"))
                except (json.JSONDecodeError, TypeError):
                    dependency_units, command = [], []
                if (
                    str(metadata.get("service_unit") or "") != service_unit
                    or dependency_units != [service_unit]
                    or service_unit not in command
                ):
                    refused.append({
                        "work_id": str(work["work_id"]),
                        "reason": "service_unit_evidence_mismatch",
                    })
                    continue
            deadline = _parse_timestamp(work["deadline_at_utc"])
            source_completed_at = _parse_timestamp(proof.get("source_updated_at_utc"))
            publication_completed_at = _parse_timestamp(
                proof.get("publication_materialized_at_utc")
            )
            if source_completed_at is None or publication_completed_at is None:
                refused.append({
                    "work_id": str(work["work_id"]),
                    "reason": "evidence_timestamp_invalid",
                })
                continue
            source_generation = str(metadata.get("source_generation") or "timer").strip()
            work_created_at = _parse_timestamp(work["created_at_utc"])
            if (
                source_generation != "timer"
                and (
                    work_created_at is None
                    or source_completed_at < work_created_at
                    or publication_completed_at < work_created_at
                )
            ):
                refused.append({
                    "work_id": str(work["work_id"]),
                    "reason": "evidence_predates_non_timer_work",
                })
                continue
            completed_at = max(source_completed_at, publication_completed_at)
            deadline_missed = bool(deadline and completed_at > deadline)
            original_result_text = str(stage["result_json"] or "{}")
            try:
                original_result = json.loads(original_result_text)
            except json.JSONDecodeError:
                original_result = {"raw": original_result_text[:1000]}
            reconciliation = {
                "contract": str(
                    proof.get("evidence_contract")
                    or "source_success_plus_ready_publication_v1"
                ),
                "evidence_id": proof["evidence_id"],
                "source_run_id": proof["source_run_id"],
                "source_updated_at_utc": proof["source_updated_at_utc"],
                "publication_data_as_of": proof["publication_data_as_of"],
                "publication_materialized_at_utc": proof["publication_materialized_at_utc"],
                "reconciled_at_utc": marker.isoformat(),
                "deadline_missed": deadline_missed,
                "original_work_state": str(work["state"]),
                "original_stage_state": str(stage["state"]),
                "original_stage_result": original_result,
                "durable_evidence": proof,
            }
            changed.append({
                "work_id": str(work["work_id"]),
                "stage_id": str(stage["stage_id"]),
                "task_id": task_id,
                "target": target,
                **reconciliation,
            })
            if dry_run:
                continue
            metadata["completion_reconciliation"] = reconciliation
            metadata["freshness_slo_missed"] = deadline_missed
            stage_result = {
                "returncode": 0,
                "recovered_from": "durable_business_completion_evidence",
                "completion_reconciliation": reconciliation,
            }
            connection.execute("BEGIN IMMEDIATE")
            stage_cursor = connection.execute(
                "UPDATE work_stages SET state='succeeded',not_before_utc='',lease_owner='',"
                "lease_expires_at_utc='',finished_at_utc=?,result_json=?,updated_at_utc=?,version=version+1 "
                "WHERE stage_id=? AND version=? AND state=?",
                (
                    completed_at.isoformat(), canonical_json(stage_result), marker.isoformat(),
                    stage["stage_id"], int(stage["version"]), stage["state"],
                ),
            )
            work_cursor = connection.execute(
                "UPDATE work_items SET state='accepted',block_reason=?,not_before_utc='',metadata_json=?,"
                "updated_at_utc=?,version=version+1 WHERE work_id=? AND version=? AND state=?",
                (
                    "accepted_after_deadline" if deadline_missed else "durable_completion_reconciled",
                    canonical_json(metadata), marker.isoformat(), work["work_id"],
                    int(work["version"]), work["state"],
                ),
            )
            if stage_cursor.rowcount != 1 or work_cursor.rowcount != 1:
                connection.rollback()
                raise RuntimeError(f"completion_reconciliation_cas_failed:{work['work_id']}")
            connection.execute("DELETE FROM resource_leases WHERE stage_id=?", (stage["stage_id"],))
            connection.execute(
                "INSERT INTO work_events(work_id,stage_id,event_type,from_state,to_state,detail_json,created_at_utc) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    work["work_id"], stage["stage_id"], "business_completion_reconciled",
                    work["state"], "accepted", canonical_json(reconciliation), marker.isoformat(),
                ),
            )
            connection.commit()
    return {"ok": True, "dry_run": dry_run, "changed": changed, "refused": refused}


def publication_completion_for_shadow(
    task: dict[str, Any],
    observed_at: datetime,
    timezone_name: str,
    *,
    snapshot_path: Path = DEFAULT_SNAPSHOT,
) -> dict[str, Any]:
    task_id = str(task.get("id") or "")
    if task_id not in SUPPORTED_DAILY_PUBLICATIONS:
        return {"completed": False, "available": False, "applicable": False}
    if str(task.get("cadence") or "") != "daily_previous_day":
        return {"completed": False, "available": False, "applicable": False}
    target = (observed_at.astimezone(ZoneInfo(timezone_name)).date() - timedelta(days=1)).isoformat()
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        rows = payload.get("completions") or []
        if int(payload.get("schema_version") or 0) != 1 or not isinstance(rows, list):
            raise ValueError("business_completion_snapshot_schema_invalid")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {
            "completed": False,
            "available": False,
            "applicable": True,
            "target": target,
            "error": f"{type(exc).__name__}:{str(exc)[:160]}",
        }
    for row in rows:
        if str(row.get("task_id") or "") == task_id and str(row.get("target") or "") == target:
            return {
                "completed": True,
                "available": True,
                "applicable": True,
                "target": target,
                "evidence_id": str(row.get("evidence_id") or ""),
                "source_run_id": str(row.get("source_run_id") or ""),
                "publication_data_as_of": str(row.get("publication_data_as_of") or ""),
                "publication_materialized_at_utc": str(row.get("publication_materialized_at_utc") or ""),
                "snapshot_generated_at_utc": str(payload.get("generated_at_utc") or ""),
            }
    return {
        "completed": False,
        "available": True,
        "applicable": True,
        "target": target,
        "snapshot_generated_at_utc": str(payload.get("generated_at_utc") or ""),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reconcile P2 work from durable business completion evidence.")
    parser.add_argument("--control-db", type=Path, default=DEFAULT_CONTROL_DB)
    parser.add_argument("--automation-db", type=Path, default=DEFAULT_AUTOMATION_DB)
    parser.add_argument("--analytics-db", type=Path, default=DEFAULT_ANALYTICS_DB)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--snapshot-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        evidence = collect_publication_evidence(args.automation_db, args.analytics_db)
        evidence.extend(collect_newcomer_publication_evidence(args.automation_db))
        snapshot = build_snapshot(evidence)
        if not args.dry_run:
            write_snapshot(args.snapshot, snapshot)
        reconciliation = (
            {"ok": True, "dry_run": args.dry_run, "changed": [], "refused": []}
            if args.snapshot_only
            else reconcile_control_plane(args.control_db, evidence, dry_run=args.dry_run)
        )
        result = {
            "ok": True,
            "evidence_count": len(evidence),
            "snapshot": str(args.snapshot),
            "snapshot_written": not args.dry_run,
            "reconciliation": reconciliation,
        }
    except Exception as exc:  # noqa: BLE001 - timer must emit structured evidence
        result = {"ok": False, "error": f"{type(exc).__name__}:{str(exc)[:500]}"}
    print(canonical_json(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
