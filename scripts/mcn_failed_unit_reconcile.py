#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/bin/python"
CONTROL_DB = Path("/data/mcn-data/control/mcn_control_plane.db")
WEEKLY_STATUS = ROOT / "data/timo_anchor_export_cache_weekly_status.json"
STREAMER_EVIDENCE = Path(
    "/var/lib/mcn-ai-automation/streamer-analytics-reconcile-evidence/latest.json"
)
RECONCILE_UNIT = "mcn-failed-unit-reconcile.service"
ALLOWED_UNITS = (
    "mcn-streamer-analytics-publish.service",
    "mcn-timo-anchor-export-cache-weekly.service",
)
WEEKLY_GUILDS = {
    "Agency MX somente",
    "TIMO001",
    "agency of BR somente",
}


def _timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text.rsplit(" ", 1)[0], "%a %Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _failed_units(run: Callable[..., subprocess.CompletedProcess[str]]) -> set[str]:
    completed = run(
        ["systemctl", "--failed", "--no-legend", "--plain"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return {
        line.split()[0]
        for line in (completed.stdout or "").splitlines()
        if line.strip() and line.split()[0] != RECONCILE_UNIT
    }


def _unit_failure_time(
    unit: str,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> datetime | None:
    completed = run(
        ["systemctl", "show", unit, "-p", "ActiveState", "-p", "ExecMainExitTimestamp"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    properties = {
        key: value
        for line in (completed.stdout or "").splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }
    if properties.get("ActiveState") not in {"inactive", "failed"}:
        return None
    return _timestamp(properties.get("ExecMainExitTimestamp"))


def _weekly_proof(path: Path, failed_at: datetime) -> dict[str, Any]:
    payload = _json(path)
    updated_at = _timestamp(payload.get("updated_at"))
    results = payload.get("results")
    guilds = [row for row in results if isinstance(row, dict)] if isinstance(results, list) else []
    guild_names = {str(row.get("guild_name") or "") for row in guilds}
    nested_results = [
        item
        for row in guilds
        for item in (
            row.get("result", {}).get("results", {}).values()
            if isinstance(row.get("result"), dict)
            and isinstance(row.get("result", {}).get("results"), dict)
            else []
        )
        if isinstance(item, dict)
    ]
    ok = bool(
        payload.get("ok") is True
        and updated_at is not None
        and updated_at > failed_at
        and guild_names == WEEKLY_GUILDS
        and all(row.get("ok") is True for row in guilds)
        and nested_results
        and all(item.get("ok") is True for item in nested_results)
    )
    return {
        "ok": ok,
        "contract": "weekly_status_all_guilds_success_after_failure_v1",
        "evidence_at_utc": updated_at.astimezone(timezone.utc).isoformat() if updated_at else "",
        "guild_count": len(guilds),
        "guild_names": sorted(guild_names),
    }


def _accepted_streamer_work(control_db: Path, evidence_id: str) -> str:
    if not evidence_id:
        return ""
    try:
        with sqlite3.connect(f"file:{control_db}?mode=ro", uri=True, timeout=5.0) as connection:
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute(
                "SELECT work_id,metadata_json FROM work_items "
                "WHERE kind='business_batch' AND state='accepted' "
                "AND title LIKE 'streamer-analytics-publish % source_unchanged' "
                "ORDER BY updated_at_utc DESC LIMIT 20"
            ).fetchall()
        for work_id, metadata_json in rows:
            try:
                metadata = json.loads(str(metadata_json or "{}"))
            except json.JSONDecodeError:
                continue
            if str(metadata.get("evidence_id") or "") == evidence_id:
                return str(work_id or "")
    except sqlite3.Error:
        return ""
    return ""


def _streamer_proof(path: Path, control_db: Path, failed_at: datetime) -> dict[str, Any]:
    payload = _json(path)
    recorded_at = _timestamp(payload.get("recorded_at_utc"))
    target_today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    evidence_id = str(payload.get("evidence_id") or "")
    work_id = _accepted_streamer_work(control_db, evidence_id)
    ok = bool(
        payload.get("status") == "verified_noop"
        and payload.get("reason") == "source_unchanged"
        and payload.get("source_generation") == "timer"
        and str(payload.get("target") or "") == target_today
        and recorded_at is not None
        and recorded_at > failed_at
        and work_id
    )
    return {
        "ok": ok,
        "contract": "accepted_streamer_noop_after_failure_v1",
        "evidence_at_utc": recorded_at.astimezone(timezone.utc).isoformat() if recorded_at else "",
        "evidence_id": evidence_id,
        "work_id": work_id,
    }


def reconcile(
    *,
    dry_run: bool = False,
    weekly_status: Path = WEEKLY_STATUS,
    streamer_evidence: Path = STREAMER_EVIDENCE,
    control_db: Path = CONTROL_DB,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    audit = run(
        [str(PYTHON), str(ROOT / "scripts/mcn_release_governance.py"), "audit-restart", "--unit", "mcn-backend.service"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        audit_payload = json.loads((audit.stdout or "").strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        audit_payload = {}
    if audit.returncode != 0 or audit_payload.get("ok") is not True or audit_payload.get("matching_receipt_status") != "passed":
        return {"ok": False, "deferred": True, "reason": "backend_receipt_not_passed", "cleared": []}

    failed = _failed_units(run)
    proofs: dict[str, dict[str, Any]] = {}
    cleared: list[str] = []
    for unit in ALLOWED_UNITS:
        if unit not in failed:
            continue
        failed_at = _unit_failure_time(unit, run)
        if failed_at is None:
            proofs[unit] = {"ok": False, "reason": "unit_not_inactive_or_failure_time_missing"}
            continue
        proof = (
            _weekly_proof(weekly_status, failed_at)
            if unit == "mcn-timo-anchor-export-cache-weekly.service"
            else _streamer_proof(streamer_evidence, control_db, failed_at)
        )
        proofs[unit] = proof
        if not proof.get("ok"):
            continue
        if dry_run:
            cleared.append(unit)
            continue
        reset = run(
            ["systemctl", "reset-failed", unit],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if reset.returncode != 0:
            proofs[unit]["reset_error"] = (reset.stderr or "")[-300:]
            continue
        if unit not in _failed_units(run):
            cleared.append(unit)
        else:
            proofs[unit]["reset_error"] = "unit_remains_failed"
    return {
        "ok": True,
        "dry_run": dry_run,
        "failed_allowlisted": sorted(failed.intersection(ALLOWED_UNITS)),
        "cleared": cleared,
        "proofs": proofs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Clear only evidence-proven stale MCN failed-unit markers.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = reconcile(dry_run=bool(args.dry_run))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 75 if result.get("deferred") else 1


if __name__ == "__main__":
    raise SystemExit(main())
