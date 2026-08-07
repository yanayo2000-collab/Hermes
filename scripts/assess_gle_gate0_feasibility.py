#!/usr/bin/env python3
"""Build an unsigned GLE Gate 0 candidate from immutable local evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.growth.common import canonical_json
from app.growth.gate0_feasibility_assessment import (
    G005ContractError,
    INPUT_VERSION,
    assess_gate0,
    exit_code_for_candidate,
    hash_json,
)


RUN_REQUEST_VERSION = "gle-g0-05-run-request-v1"
MAX_SOURCE_BYTES = 30 * 1024 * 1024 * 1024
G002B_SOURCE_COMMIT = "c2bdc06bb4926bb22de573e7967d4f4f5effa719"
G002B_RUNTIME_PATHS = (
    "app/tugao_funnel_api.py", "app/ad_dashboard_repository.py", "app/main_shared.py",
    "app/schema_migrations.py", "app/sqlite_write_queue.py",
    "scripts/backfill_ad_dashboard_fact_rows.py",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise OSError
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise G005ContractError("G005_ARTIFACT_UNREADABLE") from exc
    if not isinstance(value, dict):
        raise G005ContractError("G005_ARTIFACT_UNREADABLE")
    return value


def _verify_governance_integrity(value: Mapping[str, Any]) -> str:
    payload = dict(value)
    integrity = payload.pop("integrity", None)
    expected = str(integrity.get("payload_sha256") or "") if isinstance(integrity, dict) else ""
    actual = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    if expected != actual or str(integrity.get("algorithm") or "") != "sha256":
        raise G005ContractError("G005_TRANSPORT_RECEIPT_MISMATCH")
    return expected


def _validate_transport_release(
    manifest_path: Path, receipt_path: Path, evidence: Mapping[str, Any],
) -> None:
    manifest = _read_json(manifest_path)
    receipt = _read_json(receipt_path)
    manifest_integrity = _verify_governance_integrity(manifest)
    _verify_governance_integrity(receipt)
    files = manifest.get("artifacts", {}).get("files") if isinstance(manifest.get("artifacts"), dict) else None
    if not isinstance(files, list):
        raise G005ContractError("G005_TRANSPORT_RECEIPT_MISMATCH")
    runtime = []
    by_path = {str(item.get("path") or ""): item for item in files if isinstance(item, dict)}
    for path in G002B_RUNTIME_PATHS:
        item = by_path.get(path)
        if (
            not isinstance(item, dict)
            or len(str(item.get("sha256") or "")) != 64
            or not isinstance(item.get("size_bytes"), int)
            or item["size_bytes"] <= 0
        ):
            raise G005ContractError("G005_TRANSPORT_ARTIFACT_MISMATCH")
        runtime.append({"path": path, "sha256": item["sha256"], "size_bytes": item["size_bytes"]})
    artifact_hash = hash_json(runtime)
    change_source = manifest.get("change_source") if isinstance(manifest.get("change_source"), dict) else {}
    receipt_manifest = receipt.get("manifest") if isinstance(receipt.get("manifest"), dict) else {}
    required_manifest = {
        "schema_version", "record_type", "release_id", "created_at_utc", "environment",
        "change_source", "plan_sha256", "artifacts", "systemd", "databases", "backup",
        "verification", "rollback", "integrity",
    }
    required_receipt = {
        "schema_version", "record_type", "receipt_id", "receipt_path", "release_id",
        "manifest", "unit", "started_at_utc", "finished_at_utc", "status", "error",
        "validation", "before", "after", "command", "smokes", "integrity",
    }
    systemd_units = manifest.get("systemd", {}).get("units") if isinstance(manifest.get("systemd"), dict) else None
    verification = manifest.get("verification") if isinstance(manifest.get("verification"), dict) else {}
    validation = receipt.get("validation") if isinstance(receipt.get("validation"), dict) else {}
    command = receipt.get("command") if isinstance(receipt.get("command"), dict) else {}
    command_result = command.get("result") if isinstance(command.get("result"), dict) else {}
    smokes = receipt.get("smokes")
    before = receipt.get("before", {}).get("state", {}) if isinstance(receipt.get("before"), dict) else {}
    after = receipt.get("after", {}).get("state", {}) if isinstance(receipt.get("after"), dict) else {}
    finished_at = str(receipt.get("finished_at_utc") or "")
    if (
        manifest.get("schema_version") != 1
        or not required_manifest.issubset(manifest)
        or manifest.get("record_type") != "mcn_release_manifest"
        or receipt.get("schema_version") != 1
        or not required_receipt.issubset(receipt)
        or receipt.get("record_type") != "mcn_controlled_restart_receipt"
        or manifest.get("release_id") != evidence.get("release_id")
        or receipt.get("release_id") != evidence.get("release_id")
        or str(receipt.get("status") or "").lower() != "passed"
        or receipt.get("unit") != "mcn-backend.service"
        or not isinstance(systemd_units, list)
        or not any(isinstance(item, dict) and item.get("name") == "mcn-backend.service" for item in systemd_units)
        or not isinstance(manifest.get("databases"), list) or not manifest["databases"]
        or not isinstance(manifest.get("backup"), dict)
        or not isinstance(verification.get("tests"), list)
        or not isinstance(verification.get("smokes"), list)
        or not isinstance(manifest.get("rollback"), dict)
        or change_source.get("kind") != "codex_task"
        or not str(change_source.get("base_revision") or "").strip()
        or receipt_manifest.get("payload_sha256") != manifest_integrity
        or validation.get("ok") is not True
        or validation.get("phase") != "restart"
        or validation.get("release_id") != evidence.get("release_id")
        or command_result.get("returncode") != 0
        or command_result.get("timed_out") is not False
        or not isinstance(smokes, list) or not smokes
        or any(not isinstance(item, dict) or item.get("status") != "passed" for item in smokes)
        or after.get("ActiveState") != "active"
        or receipt.get("error") is not None
        or change_source.get("reference") != G002B_SOURCE_COMMIT
        or _sha256_file(manifest_path) != evidence.get("manifest_sha256")
        or _sha256_file(receipt_path) != evidence.get("receipt_sha256")
        or artifact_hash != evidence.get("deployed_artifact_sha256")
        or after.get("InvocationID") != evidence.get("backend_invocation_id")
        or not after.get("InvocationID")
        or after.get("InvocationID") == before.get("InvocationID")
        or _utc(finished_at) != _utc(evidence.get("deployed_at"))
    ):
        raise G005ContractError("G005_TRANSPORT_RECEIPT_MISMATCH")


def _utc(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise G005ContractError("G005_TIME_INVALID") from exc
    if parsed.tzinfo is None:
        raise G005ContractError("G005_TIME_INVALID")
    return parsed.astimezone(timezone.utc)


def _date(value: Any) -> str:
    text = str(value or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise G005ContractError("G005_DATE_INVALID") from exc


def _market_matches(value: Any, market: str) -> bool:
    text = str(value or "").strip().upper()
    aliases = {"MX": {"MX", "MEXICO", "MÉXICO"}}
    return text in aliases.get(market, {market})


def _normalize_account_id(value: Any) -> str:
    text = str(value or "").strip()
    return text[4:] if text.lower().startswith("act_") else text


def _publish_new(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _required_columns(conn: sqlite3.Connection, table: str, required: Iterable[str]) -> None:
    columns = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
    missing = set(required) - columns
    if missing:
        raise G005ContractError("G005_SOURCE_SCHEMA_MISSING:" + table)


def _source_state(path: Path) -> Tuple[int, int, str]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise G005ContractError("G005_SOURCE_UNREADABLE") from exc
    if not path.is_file() or stat.st_size <= 0 or stat.st_size > MAX_SOURCE_BYTES:
        raise G005ContractError("G005_SOURCE_UNREADABLE")
    for suffix in ("-wal", "-journal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists() and sidecar.stat().st_size:
            raise G005ContractError("G005_SOURCE_SIDECAR_PRESENT:" + suffix)
    return stat.st_size, stat.st_mtime_ns, _sha256_file(path)


def _payload(row: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        value = json.loads(str(row.get("payload_json") or "{}"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _nonnegative_decimal(value: Any, code: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise G005ContractError(code)
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise G005ContractError(code) from None
    if not number.is_finite() or number < 0:
        raise G005ContractError(code)
    return number


def _nonnegative_count(value: Any, code: str) -> int:
    number = _nonnegative_decimal(value, code)
    if number != number.to_integral_value():
        raise G005ContractError(code)
    return int(number)


def _freshness_hours(
    rows_by_source: Iterable[Iterable[Mapping[str, Any]]],
    cutoff: datetime,
) -> Decimal:
    ages = []
    for rows in rows_by_source:
        latest_by_grain: Dict[Tuple[str, str, str, str], datetime] = {}
        for row in rows:
            if not row.get("updated_at"):
                continue
            grain = tuple(str(row.get(key) or "") for key in ("date", "campaign_id", "adset_id", "ad_id"))
            observed = _utc(row["updated_at"])
            latest_by_grain[grain] = max(latest_by_grain.get(grain, observed), observed)
        if not latest_by_grain:
            return Decimal("1000000000")
        if any(latest > cutoff for latest in latest_by_grain.values()):
            raise G005ContractError("G005_SOURCE_FUTURE_TIMESTAMP")
        ages.append(max(
            Decimal(str((cutoff - latest).total_seconds())) / Decimal("3600")
            for latest in latest_by_grain.values()
        ))
    return max(ages, default=Decimal("1000000000"))


def _sync_ok_dates(conn: sqlite3.Connection, start: str, end: str, source: str) -> set[str]:
    rows = conn.execute(
        "SELECT date,status FROM ad_dashboard_sync_state WHERE source=? AND date BETWEEN ? AND ?",
        (source, start, end),
    ).fetchall()
    return {str(row["date"]) for row in rows if str(row["status"] or "").lower() == "ok"}


def _collect_observations(
    db_path: Path,
    request: Mapping[str, Any],
    expected_sha256: str,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    before = _source_state(db_path)
    if before[2] != expected_sha256:
        raise G005ContractError("G005_SOURCE_HASH_MISMATCH")
    windows = dict(request["windows"])
    allocation_start, allocation_end = _date(windows["allocation_start"]), _date(windows["allocation_end"])
    baseline_start, baseline_end = _date(windows["baseline_start"]), _date(windows["baseline_end"])
    transport = request.get("qualified_transport_evidence")
    natural_value = (
        dict(transport).get("natural_evidence_not_before_date")
        if isinstance(transport, dict)
        else request.get("natural_evidence_not_before_date")
    )
    natural_start = _date(natural_value)
    if allocation_start < natural_start:
        raise G005ContractError("G005_NATURAL_EVIDENCE_WINDOW_INVALID")
    if len(_days(allocation_start, allocation_end)) > 14:
        raise G005ContractError("G005_ALLOCATION_WINDOW_TOO_LARGE")
    if len(_days(baseline_start, baseline_end)) != 14:
        raise G005ContractError("G005_BASELINE_WINDOW_INVALID")
    subject = dict(request["subject"])
    account_id = str(subject["ad_account_id"])
    market = str(subject["market"]).upper()
    cells = list(subject["cells"])
    by_tuple = {
        (str(cell["campaign_id"]), str(cell["adset_id"]), str(cell["ad_id"])): str(cell["cell_id"])
        for cell in cells
    }
    by_cell = {str(cell["cell_id"]): cell for cell in cells}
    uri = f"file:{quote(str(db_path.resolve()), safe='/')}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        _required_columns(conn, "ad_dashboard_fact_rows", {
            "date", "data_source", "platform", "account_id", "country", "campaign_id",
            "media_source", "adset_id", "ad_id", "impressions", "cost", "tugao_join_success_users",
            "payload_json", "updated_at",
        })
        _required_columns(conn, "ad_dashboard_sync_state", {"source", "date", "status"})
        _required_columns(conn, "ad_experiment", {
            "experiment_id", "account_id", "country", "source_campaign_id",
            "source_adset_id", "source_ad_id", "control_definition_json",
        })
        lower, upper = min(allocation_start, baseline_start), max(allocation_end, baseline_end)
        facts = [dict(row) for row in conn.execute(
            "SELECT date,data_source,platform,account_id,country,media_source,campaign_id,adset_id,ad_id,"
            "impressions,cost,tugao_join_success_users,payload_json,updated_at "
            "FROM ad_dashboard_fact_rows WHERE date BETWEEN ? AND ?",
            (lower, upper),
        ).fetchall()]
        media_sync = _sync_ok_dates(conn, lower, upper, "all")
        tugao_sync = _sync_ok_dates(conn, lower, upper, "tugao_funnel")
        experiment_ids = [str(cell["experiment_id"]) for cell in cells]
        experiment_rows = [dict(row) for row in conn.execute(
            "SELECT experiment_id,account_id,country,source_campaign_id,source_adset_id,source_ad_id,"
            "control_definition_json FROM ad_experiment WHERE experiment_id IN (?,?) ORDER BY experiment_id",
            tuple(experiment_ids),
        ).fetchall()]
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
    finally:
        conn.close()
    after = _source_state(db_path)
    if after != before:
        raise G005ContractError("G005_SOURCE_DRIFTED")

    bindings = []
    for row in experiment_rows:
        try:
            control = json.loads(str(row.get("control_definition_json") or "{}"))
        except ValueError as exc:
            raise G005ContractError("G005_EXPERIMENT_BINDING_INVALID") from exc
        randomization = control.get("meta_randomization") if isinstance(control, dict) else None
        if not isinstance(randomization, dict):
            raise G005ContractError("G005_EXPERIMENT_BINDING_INVALID")
        if (
            _normalize_account_id(row.get("account_id")) != _normalize_account_id(account_id)
            or not _market_matches(row.get("country"), market)
        ):
            raise G005ContractError("G005_EXPERIMENT_BINDING_MISMATCH")
        bindings.append({
            "experiment_id": str(row.get("experiment_id") or ""),
            "study_id": str(randomization.get("study_id") or ""),
            "study_cell_id": str(randomization.get("study_cell_id") or ""),
            "campaign_id": str(row.get("source_campaign_id") or ""),
            "adset_id": str(row.get("source_adset_id") or ""),
            "ad_id": str(row.get("source_ad_id") or ""),
            "readback_verified": randomization.get("readback_verified") is True,
        })
    experiment_binding = {
        "source_snapshot_sha256": expected_sha256,
        "bindings": sorted(bindings, key=lambda item: item["experiment_id"]),
    }
    experiment_binding["evidence_hash"] = hash_json(experiment_binding)

    meta_rows = [
        row for row in facts
        if str(row.get("data_source") or "").lower() == "meta"
        and _normalize_account_id(row.get("account_id")) == _normalize_account_id(account_id)
        and _market_matches(row.get("country"), market)
    ]
    meta_allowlist = {
        (str(row["date"]), str(row.get("campaign_id") or ""), str(row.get("adset_id") or ""), str(row.get("ad_id") or ""))
        for row in meta_rows
        if all(str(row.get(key) or "") for key in ("campaign_id", "adset_id", "ad_id"))
    }
    allocation_aggregate: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(
        lambda: {"impressions": 0, "spend": Decimal("0")},
    )
    allocation_evidence = []
    for row in meta_rows:
        day = str(row["date"])
        identity = (str(row.get("campaign_id") or ""), str(row.get("adset_id") or ""), str(row.get("ad_id") or ""))
        if not allocation_start <= day <= allocation_end or identity not in by_tuple:
            continue
        cell_id = by_tuple[identity]
        key = (day, cell_id)
        impressions = _nonnegative_count(row.get("impressions") or 0, "G005_ALLOCATION_INVALID")
        spend = _nonnegative_decimal(row.get("cost") or 0, "G005_ALLOCATION_INVALID")
        allocation_aggregate[key]["impressions"] += impressions
        allocation_aggregate[key]["spend"] += spend
        allocation_evidence.append([day, *identity, impressions, str(spend)])
    allocation_rows = [
        {"date": day, "cell_id": cell, "ad_id": str(by_cell[cell]["ad_id"]),
         "impressions": values["impressions"], "spend_usd": str(values["spend"])}
        for (day, cell), values in sorted(allocation_aggregate.items())
    ]
    allocation_dates = set(_days(allocation_start, allocation_end))
    complete_dates = allocation_dates & media_sync & tugao_sync
    expected_allocation_keys = {(day, cell_id) for day in complete_dates for cell_id in by_cell}
    observed_allocation_keys = set(allocation_aggregate)
    cutoff = _utc(request["data_cutoff_at"])
    settled = _utc(allocation_end + "T23:59:59+00:00") <= cutoff - timedelta(
        hours=int(request["policy"]["reporting_settlement_hours"]),
    )
    allocation_meta = [
        row for row in meta_rows
        if allocation_start <= str(row["date"]) <= allocation_end
        and (str(row.get("campaign_id") or ""), str(row.get("adset_id") or ""), str(row.get("ad_id") or "")) in by_tuple
    ]
    allocation_tugao = [
        row for row in facts
        if str(row.get("data_source") or "").lower() == "tugaofunnel"
        and allocation_start <= str(row["date"]) <= allocation_end
        and (str(row.get("campaign_id") or ""), str(row.get("adset_id") or ""), str(row.get("ad_id") or "")) in by_tuple
        and _market_matches(row.get("country"), market)
        and _payload(row).get("qualified_join_metric_observed") is True
        and _payload(row).get("qualified_join_source_field") == "guild_join_success_users"
        and _payload(row).get("source_metric_contract") == "tugao_funnel_daily_metrics_api_v1"
        and bool(str(_payload(row).get("external_app") or "").strip())
        and bool(str(row.get("media_source") or "").strip())
    ]
    freshness = _freshness_hours((allocation_meta, allocation_tugao), cutoff)
    allocation = {
        "window_start": allocation_start + "T00:00:00+00:00",
        "window_end": allocation_end + "T23:59:59+00:00",
        "settled": settled,
        "pagination_complete": (
            complete_dates == allocation_dates
            and expected_allocation_keys == observed_allocation_keys
        ),
        "source_freshness_hours": str(freshness), "complete_days": len(complete_dates),
        "rows": allocation_rows, "evidence_hash": hash_json(sorted(allocation_evidence)),
    }

    qualified_cells: Dict[str, int] = defaultdict(int)
    eligible_qualified_joins = 0
    exact_attributed_qualified_joins = 0
    qualified_evidence = []
    target_observed_keys = set()
    target_campaigns = {str(cell["campaign_id"]) for cell in cells}
    for row in facts:
        if str(row.get("data_source") or "").lower() != "tugaofunnel" or str(row.get("platform") or "").lower() == "internal":
            continue
        day = str(row["date"])
        if not allocation_start <= day <= allocation_end or day < natural_start:
            continue
        identity = (str(row.get("campaign_id") or ""), str(row.get("adset_id") or ""), str(row.get("ad_id") or ""))
        if identity[0] not in target_campaigns or not _market_matches(row.get("country"), market):
            continue
        payload = _payload(row)
        if payload.get("qualified_join_metric_observed") is not True:
            continue
        if payload.get("qualified_join_source_field") != "guild_join_success_users" or payload.get("source_metric_contract") != "tugao_funnel_daily_metrics_api_v1":
            continue
        external_app = str(payload.get("external_app") or "").strip()
        media_source = str(row.get("media_source") or "").strip()
        if not external_app or not media_source:
            continue
        count = _nonnegative_count(row.get("tugao_join_success_users") or 0, "G005_QUALIFIED_JOIN_INVALID")
        eligible_qualified_joins += count
        if (day, *identity) not in meta_allowlist:
            continue
        if payload.get("qualified_join_exact_attribution") is not True or payload.get("qualified_join_attribution_status") != "exact":
            continue
        if identity in by_tuple:
            cell_id = by_tuple[identity]
            exact_attributed_qualified_joins += count
            qualified_cells[cell_id] += count
            target_observed_keys.add((day, cell_id))
            qualified_evidence.append([
                day, str(row.get("country") or ""), media_source,
                *identity, external_app, count,
            ])
    expected_target_keys = {(day, cell_id) for day in complete_dates for cell_id in by_cell}
    qualified = {
        "source_contract": "tugao_funnel_daily_metrics_api_v1",
        "source_metric": "guild_join_success_users",
        "qualification_version": "tugaofunnel-guild-join-success-v1",
        "window_start": allocation_start + "T00:00:00+00:00",
        "window_end": allocation_end + "T23:59:59+00:00",
        "complete": (
            allocation_dates.issubset(tugao_sync)
            and expected_target_keys == target_observed_keys
        ),
        "source_freshness_hours": str(freshness),
        "eligible_qualified_joins": eligible_qualified_joins,
        "exact_attributed_qualified_joins": exact_attributed_qualified_joins,
        "cells": [
            {"cell_id": str(cell["cell_id"]), "ad_id": str(cell["ad_id"]),
             "qualified_joins": qualified_cells.get(str(cell["cell_id"]), 0)}
            for cell in cells
        ],
        "evidence_hash": hash_json(sorted(qualified_evidence)),
    }

    baseline_meta = [row for row in meta_rows if baseline_start <= str(row["date"]) <= baseline_end]
    baseline_campaigns = {
        str(row.get("campaign_id") or "") for row in baseline_meta
        if str(row.get("campaign_id") or "")
    }
    baseline_impressions = sum(
        (_nonnegative_count(row.get("impressions") or 0, "G005_BASELINE_INVALID") for row in baseline_meta),
        0,
    )
    baseline_spend = sum(
        (_nonnegative_decimal(row.get("cost") or 0, "G005_BASELINE_INVALID") for row in baseline_meta),
        Decimal("0"),
    )
    exact_impressions = sum(
        _nonnegative_count(row.get("impressions") or 0, "G005_BASELINE_INVALID") for row in baseline_meta
        if all(str(row.get(key) or "") for key in ("campaign_id", "adset_id", "ad_id"))
    )
    baseline_joins = 0
    baseline_eligible_joins = 0
    baseline_evidence = []
    for row in facts:
        day = str(row["date"])
        if not baseline_start <= day <= baseline_end or str(row.get("data_source") or "").lower() != "tugaofunnel" or str(row.get("platform") or "").lower() == "internal":
            continue
        identity = (str(row.get("campaign_id") or ""), str(row.get("adset_id") or ""), str(row.get("ad_id") or ""))
        if identity[0] not in baseline_campaigns or not _market_matches(row.get("country"), market):
            continue
        payload = _payload(row)
        if not (
            payload.get("qualified_join_metric_observed") is True
            and payload.get("qualified_join_source_field") == "guild_join_success_users"
            and payload.get("source_metric_contract") == "tugao_funnel_daily_metrics_api_v1"
        ):
            continue
        external_app = str(payload.get("external_app") or "").strip()
        media_source = str(row.get("media_source") or "").strip()
        if not external_app or not media_source:
            continue
        count = _nonnegative_count(row.get("tugao_join_success_users") or 0, "G005_BASELINE_INVALID")
        baseline_eligible_joins += count
        if (
            payload.get("qualified_join_exact_attribution") is not True
            or payload.get("qualified_join_attribution_status") != "exact"
            or (day, *identity) not in meta_allowlist
        ):
            continue
        baseline_joins += count
        baseline_evidence.append([
            day, str(row.get("country") or ""), media_source,
            *identity, external_app, count,
        ])
    baseline = {
        "window_start": baseline_start + "T00:00:00+00:00",
        "window_end": baseline_end + "T23:59:59+00:00",
        "complete_days": len(
            set(_days(baseline_start, baseline_end))
            & media_sync
            & tugao_sync
            & {str(row["date"]) for row in baseline_meta}
        ),
        "total_impressions": baseline_impressions, "qualified_joins": baseline_joins,
        "total_spend_usd": str(baseline_spend),
        "event_attribution_coverage": str(
            Decimal(baseline_joins) / Decimal(baseline_eligible_joins)
            if baseline_eligible_joins else Decimal("0")
        ),
        "exposure_identity_coverage": str(
            Decimal(exact_impressions) / Decimal(baseline_impressions)
            if baseline_impressions else Decimal("0")
        ),
        "source_freshness_hours": str(freshness),
        "evidence_hash": hash_json({"facts": sorted(baseline_evidence), "data_version": data_version}),
    }
    baseline["attribution_coverage"] = str(min(
        Decimal(baseline["event_attribution_coverage"]),
        Decimal(baseline["exposure_identity_coverage"]),
    ))
    baseline_tugao = [
        row for row in facts
        if str(row.get("data_source") or "").lower() == "tugaofunnel"
        and baseline_start <= str(row["date"]) <= baseline_end
        and str(row.get("campaign_id") or "") in baseline_campaigns
        and _market_matches(row.get("country"), market)
        and _payload(row).get("qualified_join_metric_observed") is True
        and _payload(row).get("qualified_join_source_field") == "guild_join_success_users"
        and _payload(row).get("source_metric_contract") == "tugao_funnel_daily_metrics_api_v1"
        and bool(str(_payload(row).get("external_app") or "").strip())
        and bool(str(row.get("media_source") or "").strip())
    ]
    baseline["source_freshness_hours"] = str(
        _freshness_hours((baseline_meta, baseline_tugao), cutoff),
    )
    return allocation, qualified, baseline, experiment_binding


def _days(start: str, end: str) -> List[str]:
    cursor = datetime.strptime(start, "%Y-%m-%d").date()
    last = datetime.strptime(end, "%Y-%m-%d").date()
    values = []
    while cursor <= last:
        values.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return values


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Build unsigned GLE Gate 0 feasibility candidate")
    result.add_argument("--request", type=Path, required=True)
    result.add_argument("--governance-config", type=Path, required=True)
    result.add_argument("--g004-manifest", type=Path, required=True)
    result.add_argument("--g004-receipt", type=Path, required=True)
    result.add_argument("--g004-evidence", type=Path, required=True)
    result.add_argument("--g004a-manifest", type=Path, required=True)
    result.add_argument("--g004a-receipt", type=Path, required=True)
    result.add_argument("--g004a-evidence", type=Path, required=True)
    result.add_argument("--g001-input", type=Path, required=True)
    result.add_argument("--g001-report", type=Path, required=True)
    result.add_argument("--database", type=Path, required=True)
    result.add_argument("--database-sha256", required=True)
    result.add_argument("--qualified-transport-manifest", type=Path, required=True)
    result.add_argument("--qualified-transport-receipt", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--manifest-output", type=Path, required=True)
    return result


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    if (
        args.output.exists()
        or args.manifest_output.exists()
        or args.output.resolve() == args.manifest_output.resolve()
        or args.output.resolve().parent != args.manifest_output.resolve().parent
    ):
        print("G005_OUTPUT_NOT_IMMUTABLE", file=sys.stderr)
        return 64
    try:
        request = _read_json(args.request)
        if request.get("schema_version") != RUN_REQUEST_VERSION or set(request) != {
            "schema_version", "assessment_id", "requested_at", "data_cutoff_at", "subject",
            "policy", "windows", "qualified_transport_evidence",
        }:
            raise G005ContractError("G005_RUN_REQUEST_INVALID")
        natural_start = _date(dict(request["qualified_transport_evidence"])["natural_evidence_not_before_date"])
        if _date(dict(request["windows"])["allocation_start"]) < natural_start:
            raise G005ContractError("G005_NATURAL_EVIDENCE_WINDOW_INVALID")
        source_hash = str(args.database_sha256 or "").lower()
        _validate_transport_release(
            args.qualified_transport_manifest, args.qualified_transport_receipt,
            dict(request["qualified_transport_evidence"]),
        )
        allocation, qualified, baseline, experiment_binding = _collect_observations(
            args.database, request, source_hash,
        )
        raw = {
            "schema_version": INPUT_VERSION,
            "assessment_id": request["assessment_id"], "requested_at": request["requested_at"],
            "data_cutoff_at": request["data_cutoff_at"], "subject": request["subject"],
            "qualified_transport_evidence": request["qualified_transport_evidence"],
            "policy": request["policy"], "source_snapshot_sha256": source_hash,
            "capability_manifest": _read_json(args.g004_manifest),
            "capability_receipt": _read_json(args.g004_receipt),
            "capability_evidence": _read_json(args.g004_evidence),
            "audience_manifest": _read_json(args.g004a_manifest),
            "audience_receipt": _read_json(args.g004a_receipt),
            "audience_evidence": _read_json(args.g004a_evidence),
            "attribution_input_contract": _read_json(args.g001_input),
            "attribution_report": _read_json(args.g001_report),
            "allocation_observation": allocation, "qualified_join_observation": qualified,
            "experiment_binding_observation": experiment_binding,
            "baseline_observation": baseline,
            "governance_contract": _read_json(args.governance_config),
        }
        candidate = assess_gate0(raw)
        serialized = canonical_json(candidate) + "\n"
        _publish_new(args.output, serialized)
        manifest = {
            "schema_version": "gle-g0-05-candidate-manifest-v1",
            "candidate_file": args.output.name,
            "candidate_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "candidate_body_hash": candidate["candidate_body_hash"],
            "source_snapshot_sha256": source_hash,
            "committed": True,
        }
        _publish_new(args.manifest_output, canonical_json(manifest) + "\n")
        sys.stdout.write(serialized)
        return exit_code_for_candidate(candidate)
    except G005ContractError as exc:
        print(str(exc), file=sys.stderr)
        return 64
    except Exception:
        print("G005_UNEXPECTED_FAILURE", file=sys.stderr)
        return 66


if __name__ == "__main__":
    raise SystemExit(main())
