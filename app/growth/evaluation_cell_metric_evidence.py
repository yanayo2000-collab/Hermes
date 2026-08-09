from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple
from urllib.parse import quote

from app.growth.gate0_feasibility_assessment import (
    G005ContractError,
    _normalize_policy,
    _normalize_subject,
    _validate_experiment_binding,
    _validate_transport_evidence,
    hash_json,
)
from app.growth.common import canonical_json


MAX_SOURCE_BYTES = 30 * 1024 * 1024 * 1024
MAX_FACT_ROWS = 200_000
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_TOTAL_PAYLOAD_BYTES = 512 * 1024 * 1024
MAX_VARIABLE_FIELD_BYTES = 8 * 1024 * 1024
MAX_MATERIALIZED_ROW_BYTES = 16 * 1024 * 1024
MAX_TOTAL_MATERIALIZED_BYTES = 640 * 1024 * 1024
MAX_COMBINED_WINDOW_DAYS = 31
MAX_SYNC_ROWS_PER_SOURCE = MAX_COMBINED_WINDOW_DAYS
MAX_SYNC_FIELD_BYTES = 1024
MAX_EXPERIMENT_ROWS = 2
MAX_EXPERIMENT_FIELD_BYTES = 1024 * 1024
MAX_TOTAL_EXPERIMENT_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 8 * 1024 * 1024
REQUEST_VERSION = "gle-e04-s04-01b2-cell-metric-evidence-request-v1"
EVIDENCE_VERSION = "gle-e04-s04-01b2-cell-metric-evidence-v1"
COVERAGE_VERSION = "gle-e04-s04-01b2-cell-metric-coverage-v1"
MANIFEST_VERSION = "gle-e04-s04-01b2-cell-metric-manifest-v1"
SOURCE_REQUEST_VERSION = "gle-g0-05-run-request-v1"
EXACT_ARTIFACT_FILES = frozenset({
    "manifest.json",
    "source-run-request.json",
    "cell-metric-evidence.json",
    "coverage.json",
})
CEILING = {
    "metric_effect": "REDERIVED_SUBSET_ONLY",
    "source_content_authority": "NOT_VERIFIED",
    "source_provenance_effect": "NONE",
    "objective_authority_effect": "NONE",
    "spec_authority_effect": "NONE",
    "snapshot_effect": "NONE",
    "snapshot_emitted": False,
    "partition_effect": "NONE",
    "holdout_status": "LOCKED_NOT_ASSIGNED",
    "replay_executed": False,
    "replay_eligible": False,
    "golden_eligible": False,
    "gate0_effect": "NONE",
    "gate0_result_effect": "UNCHANGED",
    "gate1_effect": "NONE",
    "not_dataset_receipt": True,
    "not_snapshot_receipt": True,
    "not_replay_receipt": True,
    "not_gate_receipt": True,
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FACT_FIELDS = (
    "date", "data_source", "platform", "account_id", "country", "media_source",
    "campaign_id", "adset_id", "ad_id", "impressions", "cost",
    "tugao_join_success_users", "payload_json", "updated_at",
)
_TEXT_FACT_FIELDS = frozenset({
    "date", "data_source", "platform", "account_id", "country", "media_source",
    "campaign_id", "adset_id", "ad_id", "payload_json", "updated_at",
})


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


G002B_SOURCE_COMMIT = "c2bdc06bb4926bb22de573e7967d4f4f5effa719"
G002B_RUNTIME_PATHS = (
    "app/tugao_funnel_api.py", "app/ad_dashboard_repository.py", "app/main_shared.py",
    "app/schema_migrations.py", "app/sqlite_write_queue.py",
    "scripts/backfill_ad_dashboard_fact_rows.py",
)


def read_json_artifact(path: Path) -> Dict[str, Any]:
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


def validate_transport_release(
    manifest_path: Path, receipt_path: Path, evidence: Mapping[str, Any],
) -> None:
    manifest = read_json_artifact(manifest_path)
    receipt = read_json_artifact(receipt_path)
    _validate_transport_release_documents(
        manifest,
        receipt,
        evidence,
        manifest_sha256=_sha256_file(manifest_path),
        receipt_sha256=_sha256_file(receipt_path),
    )


def _validate_transport_release_documents(
    manifest: Mapping[str, Any],
    receipt: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    manifest_sha256: str,
    receipt_sha256: str,
) -> None:
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
        or manifest_sha256 != evidence.get("manifest_sha256")
        or receipt_sha256 != evidence.get("receipt_sha256")
        or artifact_hash != evidence.get("deployed_artifact_sha256")
        or after.get("InvocationID") != evidence.get("backend_invocation_id")
        or not after.get("InvocationID")
        or after.get("InvocationID") == before.get("InvocationID")
        or _utc(finished_at) != _utc(evidence.get("deployed_at"))
    ):
        raise G005ContractError("G005_TRANSPORT_RECEIPT_MISMATCH")




def _market_matches(value: Any, market: str) -> bool:
    text = str(value or "").strip().upper()
    aliases = {"MX": {"MX", "MEXICO", "MÉXICO"}}
    return text in aliases.get(market, {market})


def _normalize_account_id(value: Any) -> str:
    text = str(value or "").strip()
    return text[4:] if text.lower().startswith("act_") else text


def _required_columns(conn: sqlite3.Connection, table: str, required: Iterable[str]) -> None:
    columns = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
    missing = set(required) - columns
    if missing:
        raise G005ContractError("G005_SOURCE_SCHEMA_MISSING:" + table)


def _source_state(path: Path) -> Tuple[int, int, int, int, int, str]:
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
    return (
        stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
        _sha256_file(path),
    )


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _hash_open_fd(fd: int, *, maximum: int) -> str:
    digest = hashlib.sha256()
    consumed = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, min(1024 * 1024, maximum + 1 - consumed))
        if not chunk:
            break
        consumed += len(chunk)
        if consumed > maximum:
            raise G005ContractError("G104B2_EXTERNAL_ARTIFACT_TOO_LARGE")
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


@contextmanager
def _open_stable_named_file(
    path: Path,
    *,
    maximum: int,
    code: str,
    required_mode: int | None = None,
) -> Iterable[tuple[int, os.stat_result, int, str]]:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    parent_fd = -1
    fd = -1
    try:
        parent_path = path.parent.resolve(strict=True)
        parent_fd = os.open(parent_path, directory_flags)
        parent_before = os.fstat(parent_fd)
        fd = os.open(path.name, file_flags, dir_fd=parent_fd)
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        raise G005ContractError(code) from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > maximum
            or (required_mode is not None and stat.S_IMODE(before.st_mode) != required_mode)
        ):
            raise G005ContractError(code)
        digest = _hash_open_fd(fd, maximum=maximum)
        yield fd, before, parent_fd, digest
        after = os.fstat(fd)
        named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _identity(before) != _identity(after)
            or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)
            or _dir_identity(parent_before) != _dir_identity(os.fstat(parent_fd))
        ):
            raise G005ContractError(code)
    except OSError as exc:
        raise G005ContractError(code) from exc
    finally:
        os.close(fd)
        os.close(parent_fd)


def _read_open_fd(fd: int, *, maximum: int, code: str) -> bytes:
    chunks: list[bytes] = []
    consumed = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, min(65536, maximum + 1 - consumed))
        if not chunk:
            break
        consumed += len(chunk)
        if consumed > maximum:
            raise G005ContractError(code)
        chunks.append(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return b"".join(chunks)


def read_external_canonical_json(path: Path, expected_sha256: str) -> tuple[dict[str, Any], bytes]:
    expected = _sha256(expected_sha256, "G104B2_REQUEST_ANCHOR_INVALID")
    with _open_stable_named_file(
        path,
        maximum=MAX_ARTIFACT_FILE_BYTES,
        code="G104B2_REQUEST_ARTIFACT_INVALID",
        required_mode=0o600,
    ) as (fd, _before, _parent_fd, digest):
        raw = _read_open_fd(fd, maximum=MAX_ARTIFACT_FILE_BYTES, code="G104B2_REQUEST_ARTIFACT_INVALID")
        if digest != expected:
            raise G005ContractError("G104B2_REQUEST_ANCHOR_MISMATCH")
        value = _json_document(raw)
    if not isinstance(value, dict):
        raise G005ContractError("G104B2_SOURCE_REQUEST_INVALID")
    return value, raw


def _payload(row: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        value = json.loads(str(row.get("payload_json") or "{}"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _qualified_dimensions_match(
    row: Mapping[str, Any], payload: Mapping[str, Any], policy: Mapping[str, Any],
) -> bool:
    return (
        str(row.get("platform") or "") == "Meta"
        and str(row.get("country") or "") == str(policy["qualified_country"])
        and str(row.get("media_source") or "") == str(policy["qualified_media_source"])
        and str(payload.get("external_app") or "") == str(policy["qualified_external_app"])
    )


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
    *,
    require_all_timestamps: bool = False,
) -> Decimal:
    ages = []
    for rows in rows_by_source:
        latest_by_grain: Dict[Tuple[str, str, str, str], datetime] = {}
        for row in rows:
            if not row.get("updated_at"):
                if require_all_timestamps:
                    raise G005ContractError("G104B2_SOURCE_TIMESTAMP_MISSING")
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


def _materialize_sync_rows(
    conn: sqlite3.Connection, start: str, end: str, source: str,
) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT date,status FROM ad_dashboard_sync_state WHERE source=? AND date BETWEEN ? AND ?",
        (source, start, end),
    ))


def _sync_ok_dates(conn: sqlite3.Connection, start: str, end: str, source: str) -> set[str]:
    rows = _materialize_sync_rows(conn, start, end, source)
    return {str(row["date"]) for row in rows if str(row["status"] or "").lower() == "ok"}


def _preflight_sync_rows(
    conn: sqlite3.Connection, start: str, end: str, source: str,
) -> None:
    cursor = conn.execute(
        "SELECT typeof(source),length(CAST(source AS BLOB)),"
        "typeof(date),length(CAST(date AS BLOB)),"
        "typeof(status),length(CAST(status AS BLOB)) "
        "FROM ad_dashboard_sync_state WHERE source=? AND date BETWEEN ? AND ? LIMIT ?",
        (source, start, end, MAX_SYNC_ROWS_PER_SOURCE + 1),
    )
    count = 0
    for count, row in enumerate(cursor, start=1):
        if count > MAX_SYNC_ROWS_PER_SOURCE:
            raise G005ContractError("G104B2_SYNC_ROW_LIMIT_EXCEEDED")
        for field_index in range(3):
            if str(row[field_index * 2]) != "text":
                raise G005ContractError("G104B2_SYNC_FIELD_INVALID")
            field_size = int(row[field_index * 2 + 1] or 0)
            if field_size <= 0 or field_size > MAX_SYNC_FIELD_BYTES:
                raise G005ContractError("G104B2_SYNC_FIELD_INVALID")
    aggregate = conn.execute(
        "SELECT COUNT(*),COUNT(DISTINCT date) FROM ("
        "SELECT date FROM ad_dashboard_sync_state "
        "WHERE source=? AND date BETWEEN ? AND ? LIMIT ?)",
        (source, start, end, MAX_SYNC_ROWS_PER_SOURCE + 1),
    ).fetchone()
    if int(aggregate[0]) != count or int(aggregate[1]) != count:
        raise G005ContractError("G104B2_SYNC_GRAIN_INVALID")


_EXPERIMENT_FIELDS = (
    "experiment_id", "account_id", "country", "source_campaign_id",
    "source_adset_id", "source_ad_id", "control_definition_json",
)


def _preflight_experiment_rows(
    conn: sqlite3.Connection, experiment_ids: list[str],
) -> None:
    projection = ",".join(
        f'typeof("{field}"),length(CAST("{field}" AS BLOB))'
        for field in _EXPERIMENT_FIELDS
    )
    cursor = conn.execute(
        f"SELECT {projection} FROM ad_experiment WHERE experiment_id IN (?,?) LIMIT ?",
        (*experiment_ids, MAX_EXPERIMENT_ROWS + 1),
    )
    count = 0
    total_bytes = 0
    for count, row in enumerate(cursor, start=1):
        if count > MAX_EXPERIMENT_ROWS:
            raise G005ContractError("G104B2_EXPERIMENT_ROW_LIMIT_EXCEEDED")
        for field_index, _field in enumerate(_EXPERIMENT_FIELDS):
            if str(row[field_index * 2]) != "text":
                raise G005ContractError("G104B2_EXPERIMENT_FIELD_INVALID")
            field_size = int(row[field_index * 2 + 1] or 0)
            if field_size <= 0 or field_size > MAX_EXPERIMENT_FIELD_BYTES:
                raise G005ContractError("G104B2_EXPERIMENT_FIELD_INVALID")
            total_bytes += field_size
            if total_bytes > MAX_TOTAL_EXPERIMENT_BYTES:
                raise G005ContractError("G104B2_EXPERIMENT_BYTES_LIMIT_EXCEEDED")
    aggregate = conn.execute(
        "SELECT COUNT(*),COUNT(DISTINCT experiment_id) FROM ("
        "SELECT experiment_id FROM ad_experiment WHERE experiment_id IN (?,?) LIMIT ?)",
        (*experiment_ids, MAX_EXPERIMENT_ROWS + 1),
    ).fetchone()
    if count < MAX_EXPERIMENT_ROWS:
        raise G005ContractError("G005_EXPERIMENT_BINDING_INVALID")
    if (
        int(aggregate[0]) != count
        or int(aggregate[1]) != count
    ):
        raise G005ContractError("G104B2_EXPERIMENT_GRAIN_INVALID")


def _materialize_experiment_rows(
    conn: sqlite3.Connection, experiment_ids: list[str],
) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        "SELECT experiment_id,account_id,country,source_campaign_id,source_adset_id,source_ad_id,"
        "control_definition_json FROM ad_experiment WHERE experiment_id IN (?,?) ORDER BY experiment_id",
        tuple(experiment_ids),
    )]


def _parse_control_definition(value: Any) -> Mapping[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except ValueError as exc:
        raise G005ContractError("G005_EXPERIMENT_BINDING_INVALID") from exc
    if not isinstance(parsed, dict):
        raise G005ContractError("G005_EXPERIMENT_BINDING_INVALID")
    return parsed


def _preflight_fact_rows(
    conn: sqlite3.Connection,
    lower: str,
    upper: str,
    *,
    max_fact_rows: int,
    max_payload_bytes: int,
    max_total_payload_bytes: int,
) -> None:
    fields = ("row_id", *_FACT_FIELDS)
    projection = ",".join(
        f'typeof("{field}"),length(CAST("{field}" AS BLOB))'
        for field in fields
    )
    cursor = conn.execute(
        f"SELECT {projection} FROM ad_dashboard_fact_rows "
        "WHERE date BETWEEN ? AND ? LIMIT ?",
        (lower, upper, max_fact_rows + 1),
    )
    total_payload = 0
    total_materialized = 0
    for index, row in enumerate(cursor, start=1):
        if index > max_fact_rows:
            raise G005ContractError("G005_SOURCE_ROW_LIMIT_EXCEEDED")
        row_materialized = 0
        for field_index, field in enumerate(fields):
            storage_class = str(row[field_index * 2])
            field_size = int(row[field_index * 2 + 1] or 0)
            if field == "row_id":
                if storage_class != "text" or field_size <= 0:
                    raise G005ContractError("G104B2_SOURCE_ROW_IDENTITY_INVALID")
            elif field in _TEXT_FACT_FIELDS:
                if storage_class not in {"text", "null"}:
                    raise G005ContractError("G104B2_SOURCE_FIELD_TYPE_INVALID")
            elif storage_class not in {"integer", "real", "text", "null"}:
                raise G005ContractError("G104B2_SOURCE_FIELD_TYPE_INVALID")
            if field_size > MAX_VARIABLE_FIELD_BYTES:
                raise G005ContractError("G104B2_SOURCE_FIELD_LIMIT_EXCEEDED")
            if field == "payload_json":
                if field_size > max_payload_bytes:
                    raise G005ContractError("G104B2_SOURCE_PAYLOAD_LIMIT_EXCEEDED")
                total_payload += field_size
                if total_payload > max_total_payload_bytes:
                    raise G005ContractError("G104B2_SOURCE_PAYLOAD_LIMIT_EXCEEDED")
            row_materialized += field_size
        if row_materialized > MAX_MATERIALIZED_ROW_BYTES:
            raise G005ContractError("G104B2_SOURCE_ROW_BYTES_LIMIT_EXCEEDED")
        total_materialized += row_materialized
        if total_materialized > MAX_TOTAL_MATERIALIZED_BYTES:
            raise G005ContractError("G104B2_SOURCE_MATERIALIZED_BYTES_LIMIT_EXCEEDED")


def _materialize_fact_rows(
    conn: sqlite3.Connection,
    lower: str,
    upper: str,
    max_fact_rows: int | None,
) -> list[dict[str, Any]]:
    query = (
        "SELECT " + ",".join(_FACT_FIELDS)
        + " FROM ad_dashboard_fact_rows WHERE date BETWEEN ? AND ?"
    )
    params: tuple[Any, ...] = (lower, upper)
    if max_fact_rows is not None:
        query += " LIMIT ?"
        params += (max_fact_rows + 1,)
    facts = []
    for index, row in enumerate(conn.execute(query, params), start=1):
        if max_fact_rows is not None and index > max_fact_rows:
            raise G005ContractError("G005_SOURCE_ROW_LIMIT_EXCEEDED")
        facts.append(dict(row))
    return facts


def collect_gate0_observations(
    db_path: Path,
    request: Mapping[str, Any],
    expected_sha256: str,
    *,
    source_fd: int | None = None,
    max_fact_rows: int | None = None,
    max_payload_bytes: int | None = None,
    max_total_payload_bytes: int | None = None,
    strict_metric_evidence: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    before = _source_state(db_path) if source_fd is None else None
    if before is not None and before[-1] != expected_sha256:
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
    policy = dict(request["policy"])
    account_id = str(subject["ad_account_id"])
    market = str(subject["market"]).upper()
    cells = list(subject["cells"])
    by_tuple = {
        (str(cell["campaign_id"]), str(cell["adset_id"]), str(cell["ad_id"])): str(cell["cell_id"])
        for cell in cells
    }
    by_cell = {str(cell["cell_id"]): cell for cell in cells}
    if source_fd is None:
        sqlite_path = str(db_path.resolve())
    else:
        sqlite_path = f"/proc/self/fd/{source_fd}" if Path("/proc/self/fd").is_dir() else f"/dev/fd/{source_fd}"
    uri = f"file:{quote(sqlite_path, safe='/')}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        if strict_metric_evidence:
            database_list = conn.execute("PRAGMA database_list").fetchall()
            if (
                int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1
                or len(database_list) != 1
                or str(database_list[0][1]) != "main"
                or str(database_list[0][2]) != sqlite_path
            ):
                raise G005ContractError("G104B2_SOURCE_CONNECTION_INVALID")
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
        if strict_metric_evidence:
            _validate_metric_source_identity(conn)
        if max_fact_rows is not None:
            if max_payload_bytes is None or max_total_payload_bytes is None:
                raise G005ContractError("G104B2_SOURCE_BOUNDS_INVALID")
            _preflight_fact_rows(
                conn,
                lower,
                upper,
                max_fact_rows=max_fact_rows,
                max_payload_bytes=max_payload_bytes,
                max_total_payload_bytes=max_total_payload_bytes,
            )
        experiment_ids = [str(cell["experiment_id"]) for cell in cells]
        if strict_metric_evidence:
            _preflight_sync_rows(conn, lower, upper, "all")
            _preflight_sync_rows(conn, lower, upper, "tugao_funnel")
            _preflight_experiment_rows(conn, experiment_ids)
        facts = _materialize_fact_rows(conn, lower, upper, max_fact_rows)
        media_sync = _sync_ok_dates(conn, lower, upper, "all")
        tugao_sync = _sync_ok_dates(conn, lower, upper, "tugao_funnel")
        experiment_rows = _materialize_experiment_rows(conn, experiment_ids)
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
    finally:
        conn.close()
    if source_fd is None:
        after = _source_state(db_path)
        if after != before:
            raise G005ContractError("G005_SOURCE_DRIFTED")

    bindings = []
    for row in experiment_rows:
        control = _parse_control_definition(row.get("control_definition_json"))
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
        and (not strict_metric_evidence or str(row.get("platform") or "") == "Meta")
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
        and _qualified_dimensions_match(row, _payload(row), policy)
        and _payload(row).get("qualified_join_metric_observed") is True
        and _payload(row).get("qualified_join_source_field") == "guild_join_success_users"
        and _payload(row).get("source_metric_contract") == "tugao_funnel_daily_metrics_api_v1"
        and bool(str(_payload(row).get("external_app") or "").strip())
        and bool(str(row.get("media_source") or "").strip())
    ]
    if strict_metric_evidence:
        admitted = [*allocation_meta, *allocation_tugao]
        grains = [
            (
                str(row.get("date") or ""),
                str(row.get("data_source") or "").lower(),
                str(row.get("platform") or ""),
                _normalize_account_id(row.get("account_id")),
                market,
                str(row.get("media_source") or ""),
                str(row.get("campaign_id") or ""),
                str(row.get("adset_id") or ""),
                str(row.get("ad_id") or ""),
            )
            for row in admitted
        ]
        if len(grains) != len(set(grains)):
            raise G005ContractError("G104B2_SOURCE_GRAIN_DUPLICATE")
    freshness = _freshness_hours(
        (allocation_meta, allocation_tugao),
        cutoff,
        require_all_timestamps=strict_metric_evidence,
    )
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
        if identity[0] not in target_campaigns:
            continue
        payload = _payload(row)
        if not _qualified_dimensions_match(row, payload, policy):
            continue
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
    baseline_observed_grains = set()
    for row in facts:
        day = str(row["date"])
        if not baseline_start <= day <= baseline_end or str(row.get("data_source") or "").lower() != "tugaofunnel" or str(row.get("platform") or "").lower() == "internal":
            continue
        identity = (str(row.get("campaign_id") or ""), str(row.get("adset_id") or ""), str(row.get("ad_id") or ""))
        if identity[0] not in baseline_campaigns:
            continue
        payload = _payload(row)
        if not _qualified_dimensions_match(row, payload, policy):
            continue
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
        if all(identity):
            baseline_observed_grains.add((
                day, str(row.get("country") or ""), str(row.get("media_source") or ""),
                *identity, external_app,
            ))
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
    baseline_days = set(_days(baseline_start, baseline_end))
    baseline_expected_grains = {
        (
            str(row["date"]), str(policy["qualified_country"]),
            str(policy["qualified_media_source"]), str(row.get("campaign_id") or ""),
            str(row.get("adset_id") or ""), str(row.get("ad_id") or ""),
            str(policy["qualified_external_app"]),
        )
        for row in baseline_meta
        if all(str(row.get(key) or "") for key in ("campaign_id", "adset_id", "ad_id"))
    }
    baseline_grain_complete_dates = {
        day for day in baseline_days
        if any(grain[0] == day for grain in baseline_expected_grains)
        and {
            grain for grain in baseline_expected_grains if grain[0] == day
        }.issubset(baseline_observed_grains)
    }
    baseline = {
        "window_start": baseline_start + "T00:00:00+00:00",
        "window_end": baseline_end + "T23:59:59+00:00",
        "complete_days": len(
            baseline_days
            & media_sync
            & tugao_sync
            & {str(row["date"]) for row in baseline_meta}
            & baseline_grain_complete_dates
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
        "evidence_hash": hash_json({
            "facts": sorted(baseline_evidence),
            "expected_grains": sorted(baseline_expected_grains),
            "observed_grains": sorted(baseline_observed_grains),
            "data_version": data_version,
        }),
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
        and _qualified_dimensions_match(row, _payload(row), policy)
        and _payload(row).get("qualified_join_metric_observed") is True
        and _payload(row).get("qualified_join_source_field") == "guild_join_success_users"
        and _payload(row).get("source_metric_contract") == "tugao_funnel_daily_metrics_api_v1"
        and bool(str(_payload(row).get("external_app") or "").strip())
        and bool(str(row.get("media_source") or "").strip())
    ]
    baseline["source_freshness_hours"] = str(
        _freshness_hours(
            (baseline_meta, baseline_tugao),
            cutoff,
            require_all_timestamps=strict_metric_evidence,
        ),
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





def _validate_metric_source_identity(
    conn: sqlite3.Connection,
) -> None:
    columns = conn.execute('PRAGMA table_info("ad_dashboard_fact_rows")').fetchall()
    primary = [str(row[1]) for row in columns if int(row[5]) > 0]
    if primary != ["row_id"]:
        raise G005ContractError("G104B2_SOURCE_ROW_IDENTITY_INVALID")


def _external_json_document(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise G005ContractError("G104B2_EXTERNAL_ARTIFACT_INVALID")
            value[key] = item
        return value

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                G005ContractError("G104B2_EXTERNAL_ARTIFACT_INVALID")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G005ContractError("G104B2_EXTERNAL_ARTIFACT_INVALID") from exc
    if not isinstance(value, dict):
        raise G005ContractError("G104B2_EXTERNAL_ARTIFACT_INVALID")
    return value


def _require_sidecars_absent(path: Path, parent_fd: int) -> None:
    for suffix in ("-wal", "-journal", "-shm"):
        try:
            value = os.stat(path.name + suffix, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if value.st_size:
            raise G005ContractError("G005_SOURCE_SIDECAR_PRESENT:" + suffix)


def derive_cell_metric_evidence(
    source_request_raw: bytes,
    *,
    evidence_id: str,
    source_request_sha256: str,
    source_snapshot_path: str | Path,
    source_snapshot_sha256: str,
    transport_manifest_path: str | Path,
    transport_manifest_sha256: str,
    transport_receipt_path: str | Path,
    transport_receipt_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Rebuild the bounded metric subset from externally pinned source bytes."""
    if not isinstance(evidence_id, str) or not _ID_RE.fullmatch(evidence_id):
        raise G005ContractError("G104B2_EVIDENCE_ID_INVALID")
    request_sha = _sha256(source_request_sha256, "G104B2_REQUEST_ANCHOR_INVALID")
    snapshot_sha = _sha256(source_snapshot_sha256, "G104B2_SOURCE_ANCHOR_INVALID")
    manifest_sha = _sha256(transport_manifest_sha256, "G104B2_TRANSPORT_ANCHOR_INVALID")
    receipt_sha = _sha256(transport_receipt_sha256, "G104B2_TRANSPORT_ANCHOR_INVALID")
    if (
        not isinstance(source_request_raw, bytes)
        or not source_request_raw
        or len(source_request_raw) > MAX_ARTIFACT_FILE_BYTES
    ):
        raise G005ContractError("G104B2_REQUEST_ARTIFACT_INVALID")
    if hashlib.sha256(source_request_raw).hexdigest() != request_sha:
        raise G005ContractError("G104B2_REQUEST_ANCHOR_MISMATCH")
    parsed_request = _json_document(source_request_raw)
    if not isinstance(parsed_request, Mapping):
        raise G005ContractError("G104B2_SOURCE_REQUEST_INVALID")
    request = _validate_source_request(parsed_request)
    manifest_path = Path(transport_manifest_path)
    receipt_path = Path(transport_receipt_path)
    snapshot_path = Path(source_snapshot_path)
    with _open_stable_named_file(
        snapshot_path, maximum=MAX_SOURCE_BYTES, code="G005_SOURCE_DRIFTED",
    ) as (snapshot_fd, _snapshot_before, snapshot_parent_fd, actual_snapshot_sha), _open_stable_named_file(
        manifest_path, maximum=32 * 1024 * 1024, code="G104B2_TRANSPORT_CHANGED_DURING_READ",
    ) as (manifest_fd, _manifest_before, _manifest_parent_fd, actual_manifest_sha), _open_stable_named_file(
        receipt_path, maximum=32 * 1024 * 1024, code="G104B2_TRANSPORT_CHANGED_DURING_READ",
    ) as (receipt_fd, _receipt_before, _receipt_parent_fd, actual_receipt_sha):
        _require_sidecars_absent(snapshot_path, snapshot_parent_fd)
        if actual_snapshot_sha != snapshot_sha:
            raise G005ContractError("G005_SOURCE_HASH_MISMATCH")
        if actual_manifest_sha != manifest_sha or actual_receipt_sha != receipt_sha:
            raise G005ContractError("G104B2_TRANSPORT_ANCHOR_MISMATCH")
        manifest_raw = _read_open_fd(
            manifest_fd, maximum=32 * 1024 * 1024, code="G104B2_EXTERNAL_ARTIFACT_INVALID",
        )
        receipt_raw = _read_open_fd(
            receipt_fd, maximum=32 * 1024 * 1024, code="G104B2_EXTERNAL_ARTIFACT_INVALID",
        )
        _validate_transport_release_documents(
            _external_json_document(manifest_raw),
            _external_json_document(receipt_raw),
            dict(request["qualified_transport_evidence"]),
            manifest_sha256=actual_manifest_sha,
            receipt_sha256=actual_receipt_sha,
        )
        allocation, qualified, _, experiment_binding = collect_gate0_observations(
            snapshot_path,
            request,
            snapshot_sha,
            source_fd=snapshot_fd,
            max_fact_rows=MAX_FACT_ROWS,
            max_payload_bytes=MAX_PAYLOAD_BYTES,
            max_total_payload_bytes=MAX_TOTAL_PAYLOAD_BYTES,
            strict_metric_evidence=True,
        )
        _validate_experiment_binding(experiment_binding, request["subject"], snapshot_sha)
        _require_sidecars_absent(snapshot_path, snapshot_parent_fd)
    cell_ids = [str(item["cell_id"]) for item in request["subject"]["cells"]]
    impressions = {cell_id: 0 for cell_id in cell_ids}
    spend = {cell_id: Decimal("0") for cell_id in cell_ids}
    for row in allocation["rows"]:
        cell_id = str(row["cell_id"])
        if cell_id not in impressions:
            raise G005ContractError("G104B2_CELL_SET_MISMATCH")
        impressions[cell_id] += _nonnegative_count(
            row["impressions"], "G104B2_IMPRESSIONS_INVALID",
        )
        spend[cell_id] += _nonnegative_decimal(
            row["spend_usd"], "G104B2_SPEND_INVALID",
        )
    joins = {
        str(item["cell_id"]): _nonnegative_count(
            item["qualified_joins"], "G104B2_QUALIFIED_JOINS_INVALID",
        )
        for item in qualified["cells"]
    }
    if set(joins) != set(cell_ids):
        raise G005ContractError("G104B2_CELL_SET_MISMATCH")
    total_impressions = sum(impressions.values())
    complete = bool(
        allocation["settled"]
        and allocation["pagination_complete"]
        and qualified["complete"]
        and total_impressions > 0
        and int(qualified["exact_attributed_qualified_joins"])
        == int(qualified["eligible_qualified_joins"])
    )
    eligible = int(qualified["eligible_qualified_joins"])
    exact = int(qualified["exact_attributed_qualified_joins"])
    attribution_coverage = (
        str(Decimal(exact) / Decimal(eligible)) if eligible > 0 else None
    )
    gaps = [
        {"field": field, "reason_code": reason}
        for field, reason in (
            ("cell_metrics.clicks", "EXACT_CLICK_SOURCE_NOT_ADMITTED"),
            ("cell_metrics.installs", "EXACT_INSTALL_SOURCE_NOT_ADMITTED"),
            ("cell_metrics.invalid_users", "INVALID_USER_DEFINITION_UNFROZEN"),
            ("data_quality.duplicate_rate", "CANONICAL_DUPLICATE_RATE_SOURCE_MISSING"),
        )
    ]
    gaps.append({
        "field": "source_provenance",
        "reason_code": "SOURCE_CONTENT_AUTHORITY_NOT_VERIFIED",
    })
    if not allocation["settled"]:
        gaps.append({"field": "window", "reason_code": "REPORTING_WINDOW_NOT_SETTLED"})
    if not allocation["pagination_complete"]:
        gaps.append({"field": "allocation", "reason_code": "ALLOCATION_GRAIN_INCOMPLETE"})
    if not qualified["complete"]:
        gaps.append({"field": "qualified_joins", "reason_code": "QUALIFIED_GRAIN_INCOMPLETE"})
    if total_impressions <= 0:
        gaps.append({"field": "allocation_share", "reason_code": "ZERO_IMPRESSION_DENOMINATOR"})
    if attribution_coverage is None:
        gaps.append({"field": "data_quality.attribution_coverage", "reason_code": "ZERO_ATTRIBUTION_DENOMINATOR"})
    elif Decimal(attribution_coverage) != Decimal("1"):
        gaps.append({
            "field": "cell_metrics.qualified_joins",
            "reason_code": "QUALIFIED_ATTRIBUTION_INCOMPLETE",
        })
    cells = []
    for cell_id in cell_ids:
        share = (
            str(Decimal(impressions[cell_id]) / Decimal(total_impressions))
            if total_impressions > 0 else None
        )
        cells.append({
            "cell_id": cell_id,
            "spend_usd": str(spend[cell_id]),
            "impressions": impressions[cell_id],
            "qualified_joins": joins[cell_id],
            "allocation_share": share,
            "clicks": None,
            "installs": None,
            "invalid_users": None,
        })
    source_binding = {
        "source_request_sha256": request_sha,
        "source_snapshot_sha256": snapshot_sha,
        "transport_manifest_sha256": manifest_sha,
        "transport_receipt_sha256": receipt_sha,
        "allocation_evidence_hash": allocation["evidence_hash"],
        "qualified_evidence_hash": qualified["evidence_hash"],
        "experiment_binding_hash": experiment_binding["evidence_hash"],
    }
    evidence: dict[str, Any] = {
        "schema_version": EVIDENCE_VERSION,
        "evidence_id": evidence_id,
        "requested_at": request["requested_at"],
        "data_cutoff_at": request["data_cutoff_at"],
        "subject": request["subject"],
        "window_start": allocation["window_start"],
        "window_end": allocation["window_end"],
        "source_binding": source_binding,
        "source_contract": qualified["source_contract"],
        "source_metric": qualified["source_metric"],
        "qualification_version": qualified["qualification_version"],
        "cells": cells,
        "data_quality": {
            "source_freshness_hours": allocation["source_freshness_hours"],
            "attribution_coverage": attribution_coverage,
            "missing_sources": sorted({item["field"] for item in gaps}),
            "duplicate_rate": None,
        },
        "status": "REDERIVED_METRIC_SUBSET_FROM_PINNED_BYTES" if complete else "INCOMPLETE_METRIC_SUBSET",
        "trust_status": "SOURCE_CONTENT_AUTHORITY_NOT_VERIFIED",
        "reason_codes": sorted({item["reason_code"] for item in gaps}),
        "ceiling": dict(CEILING),
        "evidence_hash": "",
    }
    evidence["evidence_hash"] = hash_json({
        key: value for key, value in evidence.items() if key != "evidence_hash"
    })
    coverage: dict[str, Any] = {
        "schema_version": COVERAGE_VERSION,
        "evidence_id": evidence_id,
        "cell_count": len(cells),
        "complete_days": allocation["complete_days"],
        "settled": allocation["settled"],
        "allocation_complete": allocation["pagination_complete"],
        "qualified_complete": qualified["complete"],
        "observed_fields": [
            "cell_metrics.allocation_share",
            "cell_metrics.impressions",
            "cell_metrics.qualified_joins",
            "cell_metrics.spend",
            "data_quality.freshness",
        ] + (["data_quality.attribution_coverage"] if attribution_coverage is not None else []),
        "rederived_fields": ([
            "cell_metrics.allocation_share",
            "cell_metrics.impressions",
            "cell_metrics.qualified_joins",
            "cell_metrics.spend",
            "data_quality.freshness",
        ] + (["data_quality.attribution_coverage"] if attribution_coverage is not None else [])) if complete else [],
        "verified_fields": [],
        "gaps": sorted(gaps, key=lambda item: (item["field"], item["reason_code"])),
        "status": evidence["status"],
        "snapshot_effect": "NONE",
        "gate1_effect": "NONE",
        "coverage_hash": "",
    }
    coverage["coverage_hash"] = hash_json({
        key: value for key, value in coverage.items() if key != "coverage_hash"
    })
    artifact_request: dict[str, Any] = {
        "schema_version": REQUEST_VERSION,
        "evidence_id": evidence_id,
        "requested_at": request["requested_at"],
        "data_cutoff_at": request["data_cutoff_at"],
        "source_binding": source_binding,
        "request_hash": "",
    }
    artifact_request["request_hash"] = hash_json({
        key: value for key, value in artifact_request.items() if key != "request_hash"
    })
    return artifact_request, evidence, coverage


def write_cell_metric_evidence_artifact(
    output_dir: str | Path,
    source_request_raw: bytes,
    **derive_kwargs: Any,
) -> dict[str, Any]:
    request, evidence, coverage = derive_cell_metric_evidence(
        source_request_raw, **derive_kwargs,
    )
    payloads = {
        "source-run-request.json": source_request_raw,
        "cell-metric-evidence.json": _json_bytes(evidence),
        "coverage.json": _json_bytes(coverage),
    }
    manifest = _artifact_manifest(request, evidence, coverage, payloads)
    payloads["manifest.json"] = _json_bytes(manifest)
    output = Path(output_dir)
    _write_artifact_directory(output, payloads)
    loaded = load_validated_cell_metric_evidence_directory(
        output,
        expected_manifest_sha256=hashlib.sha256(payloads["manifest.json"]).hexdigest(),
        source_snapshot_path=derive_kwargs["source_snapshot_path"],
        transport_manifest_path=derive_kwargs["transport_manifest_path"],
        transport_receipt_path=derive_kwargs["transport_receipt_path"],
    )
    if loaded["manifest"] != manifest:
        raise G005ContractError("G104B2_WRITE_READBACK_MISMATCH")
    return manifest


def load_validated_cell_metric_evidence_directory(
    artifact_dir: str | Path,
    *,
    expected_manifest_sha256: str,
    source_snapshot_path: str | Path,
    transport_manifest_path: str | Path,
    transport_receipt_path: str | Path,
) -> dict[str, Any]:
    expected = _sha256(expected_manifest_sha256, "G104B2_MANIFEST_ANCHOR_INVALID")
    raw = _read_artifact_directory(Path(artifact_dir))
    if hashlib.sha256(raw["manifest.json"]).hexdigest() != expected:
        raise G005ContractError("G104B2_MANIFEST_ANCHOR_MISMATCH")
    source_request = _json_document(raw["source-run-request.json"])
    evidence = _json_document(raw["cell-metric-evidence.json"])
    coverage = _json_document(raw["coverage.json"])
    manifest = _json_document(raw["manifest.json"])
    if not all(isinstance(item, Mapping) for item in (source_request, evidence, coverage, manifest)):
        raise G005ContractError("G104B2_ARTIFACT_JSON_INVALID")
    binding = dict(evidence["source_binding"])
    request, expected_evidence, expected_coverage = derive_cell_metric_evidence(
        raw["source-run-request.json"],
        evidence_id=evidence["evidence_id"],
        source_request_sha256=binding["source_request_sha256"],
        source_snapshot_path=source_snapshot_path,
        source_snapshot_sha256=binding["source_snapshot_sha256"],
        transport_manifest_path=transport_manifest_path,
        transport_manifest_sha256=binding["transport_manifest_sha256"],
        transport_receipt_path=transport_receipt_path,
        transport_receipt_sha256=binding["transport_receipt_sha256"],
    )
    if dict(evidence) != expected_evidence or dict(coverage) != expected_coverage:
        raise G005ContractError("G104B2_ARTIFACT_DERIVATION_MISMATCH")
    payloads = {name: raw[name] for name in EXACT_ARTIFACT_FILES - {"manifest.json"}}
    expected_manifest = _artifact_manifest(request, expected_evidence, expected_coverage, payloads)
    if dict(manifest) != expected_manifest:
        raise G005ContractError("G104B2_MANIFEST_DERIVATION_MISMATCH")
    return {
        "request": request,
        "evidence": expected_evidence,
        "coverage": expected_coverage,
        "manifest": expected_manifest,
    }


def _validate_source_request(value: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "schema_version", "assessment_id", "requested_at", "data_cutoff_at",
        "subject", "policy", "windows", "qualified_transport_evidence",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise G005ContractError("G104B2_SOURCE_REQUEST_INVALID")
    request = dict(value)
    if request["schema_version"] != SOURCE_REQUEST_VERSION:
        raise G005ContractError("G104B2_SOURCE_REQUEST_INVALID")
    if not isinstance(request["assessment_id"], str) or not _ID_RE.fullmatch(request["assessment_id"]):
        raise G005ContractError("G104B2_SOURCE_REQUEST_INVALID")
    requested_at = _utc(request["requested_at"])
    cutoff = _utc(request["data_cutoff_at"])
    if cutoff > requested_at:
        raise G005ContractError("G104B2_TIME_ORDER_INVALID")
    subject = _normalize_subject(request["subject"])
    policy = _normalize_policy(request["policy"])
    transport = _validate_transport_evidence(request["qualified_transport_evidence"])
    windows = request["windows"]
    if not isinstance(windows, Mapping) or set(windows) != {
        "allocation_start", "allocation_end", "baseline_start", "baseline_end",
    }:
        raise G005ContractError("G104B2_SOURCE_REQUEST_INVALID")
    allocation_start = _date(windows["allocation_start"])
    allocation_end = _date(windows["allocation_end"])
    baseline_start = _date(windows["baseline_start"])
    baseline_end = _date(windows["baseline_end"])
    if (
        allocation_start > allocation_end
        or baseline_start > baseline_end
        or len(_days(allocation_start, allocation_end)) > 14
        or len(_days(baseline_start, baseline_end)) != policy["baseline_window_days"]
        or len(_days(min(allocation_start, baseline_start), max(allocation_end, baseline_end)))
        > MAX_COMBINED_WINDOW_DAYS
        or allocation_start < transport["natural_evidence_not_before_date"]
    ):
        raise G005ContractError("G104B2_SOURCE_WINDOW_INVALID")
    normalized = {
        "schema_version": request["schema_version"],
        "assessment_id": request["assessment_id"],
        "requested_at": request["requested_at"],
        "data_cutoff_at": request["data_cutoff_at"],
        "subject": subject,
        "policy": policy,
        "windows": {
            "allocation_start": allocation_start,
            "allocation_end": allocation_end,
            "baseline_start": baseline_start,
            "baseline_end": baseline_end,
        },
        "qualified_transport_evidence": transport,
    }
    return normalized


def _artifact_manifest(
    request: Mapping[str, Any],
    evidence: Mapping[str, Any],
    coverage: Mapping[str, Any],
    payloads: Mapping[str, bytes],
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": MANIFEST_VERSION,
        "evidence_id": evidence["evidence_id"],
        "request_hash": request["request_hash"],
        "evidence_hash": evidence["evidence_hash"],
        "coverage_hash": coverage["coverage_hash"],
        "status": evidence["status"],
        "ceiling": dict(CEILING),
        "files": {
            name: {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            }
            for name, raw in sorted(payloads.items())
        },
        "manifest_hash": "",
    }
    value["manifest_hash"] = hash_json({
        key: item for key, item in value.items() if key != "manifest_hash"
    })
    return value


def _write_artifact_directory(root: Path, payloads: Mapping[str, bytes]) -> None:
    if set(payloads) != EXACT_ARTIFACT_FILES or not root.name or root.name in {".", ".."}:
        raise G005ContractError("G104B2_OUTPUT_INVALID")
    if any(len(raw) > MAX_ARTIFACT_FILE_BYTES for raw in payloads.values()) or sum(map(len, payloads.values())) > MAX_TOTAL_ARTIFACT_BYTES:
        raise G005ContractError("G104B2_ARTIFACT_TOO_LARGE")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(root.parent, flags)
    root_fd: int | None = None
    complete = False
    try:
        try:
            os.mkdir(root.name, 0o700, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise G005ContractError("G104B2_OUTPUT_EXISTS") from exc
        root_fd = os.open(root.name, flags, dir_fd=parent_fd)
        os.fchmod(root_fd, 0o700)
        _require_dir_identity(parent_fd, root.name, root_fd)
        for name in sorted(payloads):
            _write_file_at(root_fd, name, payloads[name])
        if set(os.listdir(root_fd)) != EXACT_ARTIFACT_FILES:
            raise G005ContractError("G104B2_OUTPUT_FILE_SET_INVALID")
        os.fsync(root_fd)
        _require_dir_identity(parent_fd, root.name, root_fd)
        complete = True
        os.fsync(parent_fd)
    except Exception as exc:
        if complete:
            raise G005ContractError("G104B2_OUTPUT_DURABILITY_UNCERTAIN") from exc
        if root_fd is not None:
            for name in EXACT_ARTIFACT_FILES:
                try:
                    os.unlink(name, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
            try:
                _require_dir_identity(parent_fd, root.name, root_fd)
                os.rmdir(root.name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)


def _write_file_at(directory_fd: int, name: str, raw: bytes) -> None:
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                raise G005ContractError("G104B2_WRITE_FAILED")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(name, 0o600, dir_fd=directory_fd, follow_symlinks=False)


def _read_artifact_directory(root: Path) -> dict[str, bytes]:
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    parent_fd = os.open(root.parent.resolve(strict=True), dir_flags)
    root_fd = os.open(root.name, dir_flags, dir_fd=parent_fd)
    try:
        parent_before = os.fstat(parent_fd)
        root_before = os.fstat(root_fd)
        if stat.S_IMODE(root_before.st_mode) != 0o700 or set(os.listdir(root_fd)) != EXACT_ARTIFACT_FILES:
            raise G005ContractError("G104B2_ARTIFACT_DIRECTORY_INVALID")
        raw: dict[str, bytes] = {}
        total = 0
        for name in sorted(EXACT_ARTIFACT_FILES):
            fd = os.open(name, file_flags, dir_fd=root_fd)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1 or before.st_size > MAX_ARTIFACT_FILE_BYTES:
                    raise G005ContractError("G104B2_ARTIFACT_FILE_INVALID")
                chunks = []
                consumed = 0
                while True:
                    chunk = os.read(fd, min(65536, MAX_ARTIFACT_FILE_BYTES + 1 - consumed))
                    if not chunk:
                        break
                    consumed += len(chunk)
                    total += len(chunk)
                    if consumed > MAX_ARTIFACT_FILE_BYTES or total > MAX_TOTAL_ARTIFACT_BYTES:
                        raise G005ContractError("G104B2_ARTIFACT_TOO_LARGE")
                    chunks.append(chunk)
                after = os.fstat(fd)
                if _file_identity(before) != _file_identity(after) or consumed != after.st_size:
                    raise G005ContractError("G104B2_ARTIFACT_CHANGED_DURING_READ")
                raw[name] = b"".join(chunks)
            finally:
                os.close(fd)
        if set(os.listdir(root_fd)) != EXACT_ARTIFACT_FILES:
            raise G005ContractError("G104B2_ARTIFACT_CHANGED_DURING_READ")
        _require_dir_identity(parent_fd, root.name, root_fd)
        if _dir_identity(parent_before) != _dir_identity(os.fstat(parent_fd)) or _dir_identity(root_before) != _dir_identity(os.fstat(root_fd)):
            raise G005ContractError("G104B2_ARTIFACT_CHANGED_DURING_READ")
        return raw
    finally:
        os.close(root_fd)
        os.close(parent_fd)


def _require_dir_identity(parent_fd: int, name: str, opened_fd: int) -> None:
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    opened = os.fstat(opened_fd)
    if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
        raise G005ContractError("G104B2_OUTPUT_DIRECTORY_CHANGED")


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_nlink, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )


def _dir_identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_mtime_ns, value.st_ctime_ns)


def _json_document(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise G005ContractError("G104B2_ARTIFACT_JSON_INVALID")
            result[key] = value
        return result
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                G005ContractError("G104B2_ARTIFACT_JSON_INVALID")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise G005ContractError("G104B2_ARTIFACT_JSON_INVALID") from exc
    if raw != _json_bytes(value):
        raise G005ContractError("G104B2_ARTIFACT_JSON_INVALID")
    return value


def _json_bytes(value: Any) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise G005ContractError(code)
    return value


__all__ = [
    "CEILING",
    "EXACT_ARTIFACT_FILES",
    "collect_gate0_observations",
    "derive_cell_metric_evidence",
    "load_validated_cell_metric_evidence_directory",
    "read_external_canonical_json",
    "read_json_artifact",
    "validate_transport_release",
    "write_cell_metric_evidence_artifact",
]
