"""Bounded, non-promoting mutation-provenance evidence for E04 S04-01B3.

The module only re-derives a mutation subset from caller-pinned immutable
SQLite bytes.  It never claims a complete journal, source authority, a real
Snapshot, Replay eligibility, Holdout access, or a Gate result.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

from app.growth.common import canonical_json, payload_hash
from app.growth.meta_execution_worker import execution_steps_for, is_delivery_status_step


REQUEST_VERSION = "gle-e04-s04-01b3-mutation-provenance-request-v1"
EVENTS_VERSION = "gle-e04-s04-01b3-mutation-events-v1"
COVERAGE_VERSION = "gle-e04-s04-01b3-mutation-coverage-v1"
ASSESSMENT_VERSION = "gle-e04-s04-01b3-mutation-assessment-v1"
MANIFEST_VERSION = "gle-e04-s04-01b3-mutation-manifest-v1"

EXACT_ARTIFACT_FILES = frozenset({
    "source-request.json",
    "mutation-events.json",
    "coverage.json",
    "provenance-assessment.json",
    "manifest.json",
})

CEILING = {
    "mutation_effect": "RECEIPT_OBSERVATION_SUBSET_ONLY",
    "source_content_authority": "NOT_VERIFIED",
    "source_provenance_effect": "NONE",
    "complete_event_journal": False,
    "external_mutation_coverage": "UNKNOWN",
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

MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 30 * 1024 * 1024 * 1024
MAX_ARTIFACT_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_FIELD_BYTES = 2 * 1024 * 1024
MAX_ROW_BYTES = 4 * 1024 * 1024
MAX_MATERIALIZED_BYTES = 256 * 1024 * 1024
MAX_TOTAL_SOURCE_MATERIALIZED_BYTES = 256 * 1024 * 1024
MAX_ACTION_ROWS = 5_000
MAX_APPROVAL_ROWS = 5_000
MAX_TASK_ROWS = 5_000
MAX_RECEIPT_ROWS = 20_000
MAX_EVENT_ROWS = 20_000

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTION_FIELDS = (
    "operation_action_id", "action_type", "action_scope", "target_type", "target_id",
    "payload_json", "status", "created_by", "created_at", "updated_at",
)
_APPROVAL_FIELDS = (
    "approval_id", "operation_action_id", "plan_hash", "plan_json", "status",
    "proposed_by", "approved_by", "approved_at", "expires_at", "consumed_at",
    "idempotency_key", "request_hash", "created_at", "updated_at",
)
_TASK_FIELDS = (
    "execution_task_id", "operation_action_id", "idempotency_key", "request_hash", "status",
    "current_step", "payload_json", "meta_object_ids_json", "created_at", "updated_at",
    "finished_at",
)
_RECEIPT_FIELDS = (
    "receipt_id", "execution_task_id", "step_name", "step_status", "step_result_json",
    "meta_object_ids_json", "verification_result_json", "created_at",
)
_EVENT_FIELDS = (
    "event_id", "experiment_id", "from_state", "to_state", "event_type", "actor",
    "reason", "evidence_json", "created_at",
)


class MutationProvenanceError(ValueError):
    pass


def hash_json(value: Any) -> str:
    return payload_hash(value)


def _json_bytes(value: Any) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _json_document(raw: bytes, code: str = "G104B3_JSON_INVALID") -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise MutationProvenanceError(code)
            value[key] = item
        return value

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(MutationProvenanceError(code)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MutationProvenanceError(code) from exc
    if raw != _json_bytes(value):
        raise MutationProvenanceError(code)
    return value


def _utc(value: Any, code: str = "G104B3_TIME_INVALID") -> datetime:
    if not isinstance(value, str) or not value or value.endswith("Z"):
        raise MutationProvenanceError(code)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MutationProvenanceError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MutationProvenanceError(code)
    normalized = parsed.astimezone(timezone.utc)
    if parsed.utcoffset() != timezone.utc.utcoffset(None) or value != normalized.isoformat():
        raise MutationProvenanceError(code)
    return normalized


def _sha(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise MutationProvenanceError(code)
    return value


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise MutationProvenanceError(code)
    return value


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_nlink, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )


def _dir_identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_mtime_ns, value.st_ctime_ns)


def _hash_fd(fd: int, maximum: int) -> str:
    digest = hashlib.sha256()
    consumed = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, min(1024 * 1024, maximum + 1 - consumed))
        if not chunk:
            break
        consumed += len(chunk)
        if consumed > maximum:
            raise MutationProvenanceError("G104B3_EXTERNAL_ARTIFACT_TOO_LARGE")
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _read_fd(fd: int, maximum: int, code: str) -> bytes:
    chunks: list[bytes] = []
    consumed = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, min(65536, maximum + 1 - consumed))
        if not chunk:
            break
        consumed += len(chunk)
        if consumed > maximum:
            raise MutationProvenanceError(code)
        chunks.append(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return b"".join(chunks)


@contextmanager
def _open_stable_file(
    path: Path, *, maximum: int, code: str, required_mode: int | None = None,
) -> Iterable[tuple[int, int, os.stat_result, str]]:
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    parent_fd = -1
    fd = -1
    try:
        parent_fd = os.open(path.parent.resolve(strict=True), dir_flags)
        parent_before = os.fstat(parent_fd)
        fd = os.open(path.name, file_flags, dir_fd=parent_fd)
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > maximum
            or (required_mode is not None and stat.S_IMODE(before.st_mode) != required_mode)
        ):
            raise MutationProvenanceError(code)
        digest = _hash_fd(fd, maximum)
        yield fd, parent_fd, before, digest
        after = os.fstat(fd)
        named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _file_identity(before) != _file_identity(after)
            or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)
            or _dir_identity(parent_before) != _dir_identity(os.fstat(parent_fd))
        ):
            raise MutationProvenanceError(code)
    except OSError as exc:
        raise MutationProvenanceError(code) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def read_external_request(path: str | Path, expected_sha256: str) -> tuple[dict[str, Any], bytes]:
    expected = _sha(expected_sha256, "G104B3_REQUEST_ANCHOR_INVALID")
    with _open_stable_file(
        Path(path), maximum=MAX_REQUEST_BYTES, code="G104B3_REQUEST_ARTIFACT_INVALID",
        required_mode=0o600,
    ) as (fd, _parent_fd, _before, digest):
        raw = _read_fd(fd, MAX_REQUEST_BYTES, "G104B3_REQUEST_ARTIFACT_INVALID")
        if digest != expected:
            raise MutationProvenanceError("G104B3_REQUEST_ANCHOR_MISMATCH")
        value = _json_document(raw)
    if not isinstance(value, dict):
        raise MutationProvenanceError("G104B3_REQUEST_INVALID")
    return value, raw


def _validate_request(raw: bytes, expected_sha256: str) -> dict[str, Any]:
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise MutationProvenanceError("G104B3_REQUEST_ARTIFACT_INVALID")
    expected = _sha(expected_sha256, "G104B3_REQUEST_ANCHOR_INVALID")
    if hashlib.sha256(raw).hexdigest() != expected:
        raise MutationProvenanceError("G104B3_REQUEST_ANCHOR_MISMATCH")
    value = _json_document(raw)
    keys = {
        "schema_version", "evidence_id", "requested_at", "window_start", "data_cutoff_at",
        "subject", "relevant_fields", "source_snapshot", "request_hash",
    }
    if not isinstance(value, dict) or set(value) != keys or value["schema_version"] != REQUEST_VERSION:
        raise MutationProvenanceError("G104B3_REQUEST_INVALID")
    evidence_id = _identifier(value["evidence_id"], "G104B3_REQUEST_INVALID")
    requested_at = _utc(value["requested_at"])
    window_start = _utc(value["window_start"])
    cutoff = _utc(value["data_cutoff_at"])
    if not window_start <= cutoff <= requested_at or cutoff - window_start > timedelta(days=31):
        raise MutationProvenanceError("G104B3_TIME_ORDER_INVALID")
    subject = value["subject"]
    if not isinstance(subject, dict) or set(subject) != {
        "account_id", "study_id", "launch_id", "campaign_id", "cells",
    }:
        raise MutationProvenanceError("G104B3_SUBJECT_INVALID")
    normalized_subject = {
        "account_id": _identifier(subject["account_id"], "G104B3_SUBJECT_INVALID"),
        "study_id": _identifier(subject["study_id"], "G104B3_SUBJECT_INVALID"),
        "launch_id": _identifier(subject["launch_id"], "G104B3_SUBJECT_INVALID"),
        "campaign_id": _identifier(subject["campaign_id"], "G104B3_SUBJECT_INVALID"),
        "cells": [],
    }
    cells = subject["cells"]
    if not isinstance(cells, list) or len(cells) != 2:
        raise MutationProvenanceError("G104B3_SUBJECT_INVALID")
    for raw_cell in cells:
        if not isinstance(raw_cell, dict) or set(raw_cell) != {
            "cell_id", "experiment_id", "study_cell_id", "adset_id", "ad_id",
        }:
            raise MutationProvenanceError("G104B3_SUBJECT_INVALID")
        normalized_subject["cells"].append({
            key: _identifier(raw_cell[key], "G104B3_SUBJECT_INVALID")
            for key in ("cell_id", "experiment_id", "study_cell_id", "adset_id", "ad_id")
        })
    normalized_subject["cells"].sort(key=lambda item: item["cell_id"])
    for key in ("cell_id", "experiment_id", "study_cell_id", "adset_id", "ad_id"):
        if len({cell[key] for cell in normalized_subject["cells"]}) != 2:
            raise MutationProvenanceError("G104B3_SUBJECT_INVALID")
    expected_fields = [
        {"object_type": "AD", "object_id": cell["ad_id"], "field": "status"}
        for cell in normalized_subject["cells"]
    ] + [
        {"object_type": "ADSET", "object_id": cell["adset_id"], "field": "status"}
        for cell in normalized_subject["cells"]
    ] + [{
        "object_type": "CAMPAIGN", "object_id": normalized_subject["campaign_id"], "field": "status",
    }]
    expected_fields.sort(key=lambda item: (item["object_type"], item["object_id"], item["field"]))
    if value["relevant_fields"] != expected_fields:
        raise MutationProvenanceError("G104B3_FIELD_DENOMINATOR_INVALID")
    source = value["source_snapshot"]
    if not isinstance(source, dict) or set(source) != {"logical_source_id", "sha256"}:
        raise MutationProvenanceError("G104B3_SOURCE_BINDING_INVALID")
    normalized_source = {
        "logical_source_id": _identifier(source["logical_source_id"], "G104B3_SOURCE_BINDING_INVALID"),
        "sha256": _sha(source["sha256"], "G104B3_SOURCE_BINDING_INVALID"),
    }
    normalized = {
        "schema_version": REQUEST_VERSION,
        "evidence_id": evidence_id,
        "requested_at": value["requested_at"],
        "window_start": value["window_start"],
        "data_cutoff_at": value["data_cutoff_at"],
        "subject": normalized_subject,
        "relevant_fields": expected_fields,
        "source_snapshot": normalized_source,
        "request_hash": value["request_hash"],
    }
    expected_hash = hash_json({key: item for key, item in normalized.items() if key != "request_hash"})
    if normalized["request_hash"] != expected_hash:
        raise MutationProvenanceError("G104B3_REQUEST_HASH_INVALID")
    return normalized


def _required_schema(conn: sqlite3.Connection, table: str, fields: Sequence[str], primary: str) -> None:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    columns = {str(row[1]) for row in rows}
    primary_columns = [str(row[1]) for row in rows if int(row[5]) > 0]
    if not set(fields).issubset(columns) or primary_columns != [primary]:
        raise MutationProvenanceError("G104B3_SOURCE_SCHEMA_INVALID:" + table)


def _preflight_rows(
    conn: sqlite3.Connection,
    *,
    table: str,
    fields: Sequence[str],
    where: str,
    params: Sequence[Any],
    maximum_rows: int,
) -> tuple[int, int]:
    projections = []
    for field in fields:
        projections.extend((f'typeof("{field}")', f'length(CAST("{field}" AS BLOB))'))
    sql = f'SELECT {", ".join(projections)} FROM "{table}" WHERE {where} LIMIT ?'
    total = 0
    count = 0
    for row in conn.execute(sql, (*params, maximum_rows + 1)):
        count += 1
        if count > maximum_rows:
            raise MutationProvenanceError("G104B3_SOURCE_ROW_LIMIT_EXCEEDED:" + table)
        row_bytes = 0
        for index in range(0, len(row), 2):
            value_type = str(row[index])
            size = int(row[index + 1] or 0)
            if value_type != "text" or size > MAX_FIELD_BYTES:
                raise MutationProvenanceError("G104B3_SOURCE_FIELD_INVALID:" + table)
            row_bytes += size
        if row_bytes > MAX_ROW_BYTES:
            raise MutationProvenanceError("G104B3_SOURCE_ROW_TOO_LARGE:" + table)
        total += row_bytes
        if total > MAX_MATERIALIZED_BYTES:
            raise MutationProvenanceError("G104B3_SOURCE_TOTAL_TOO_LARGE:" + table)
    return count, total


def _consume_materialization_budget(consumed: int, additional: int) -> int:
    total = consumed + additional
    if total > MAX_TOTAL_SOURCE_MATERIALIZED_BYTES:
        raise MutationProvenanceError("G104B3_SOURCE_GLOBAL_TOTAL_TOO_LARGE")
    return total


def _materialize_rows(
    conn: sqlite3.Connection,
    *,
    table: str,
    fields: Sequence[str],
    where: str,
    params: Sequence[Any],
    order: str,
) -> list[dict[str, Any]]:
    projection = ", ".join(f'"{item}"' for item in fields)
    return [dict(row) for row in conn.execute(
        f'SELECT {projection} FROM "{table}" WHERE {where} ORDER BY {order}', params,
    ).fetchall()]


def _decode_object(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise MutationProvenanceError(code)
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=lambda pairs: _unique_pairs(pairs, code),
            parse_constant=lambda _: (_ for _ in ()).throw(MutationProvenanceError(code)),
        )
    except json.JSONDecodeError as exc:
        raise MutationProvenanceError(code) from exc
    if not isinstance(parsed, dict):
        raise MutationProvenanceError(code)
    return parsed


def _unique_pairs(pairs: list[tuple[str, Any]], code: str) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise MutationProvenanceError(code)
        value[key] = item
    return value


def _value_commitment(value: Any) -> dict[str, Any]:
    return {"present": True, "value_hash": hash_json(value)}


def _event(
    *, object_type: str, object_id: str, field: str, before: Any, after: Any,
    receipt_observed_at: str, evidence_refs: list[dict[str, str]], receipt_chain_status: str,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "object_type": object_type,
        "object_id": object_id,
        "field": field,
        "planned_before": _value_commitment(before),
        "planned_after": _value_commitment(after),
        "observed_after": _value_commitment(after),
        "plan_claim": "APPROVED_INTENT_ONLY",
        "after_claim": "GET_READBACK_OBSERVED",
        "changed_at": None,
        "receipt_observed_at": receipt_observed_at,
        "source_class": "GLE_RECEIPT_CHAIN_OBSERVATION",
        "claim_strength": "GLE_RECEIPT_CHAIN_OBSERVATION_ONLY",
        "evidence_refs": sorted(evidence_refs, key=lambda item: (item["artifact_type"], item["record_id"])),
        "receipt_chain_status": receipt_chain_status,
        "event_hash": "",
    }
    value["event_hash"] = hash_json({key: item for key, item in value.items() if key != "event_hash"})
    return value


def _mapping(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MutationProvenanceError(code)
    return dict(value)


def _list(value: Any, code: str) -> list[Any]:
    if not isinstance(value, list):
        raise MutationProvenanceError(code)
    return list(value)


def _validate_reactivation_plan_shape(plan: Mapping[str, Any]) -> None:
    if str(plan.get("action_type") or "").upper() != "REACTIVATE_AD":
        raise MutationProvenanceError("G104B3_PLAN_ACTION_TYPE_INVALID")
    _utc(plan.get("expires_at"), "G104B3_PLAN_EXPIRY_INVALID")
    steps = _mapping(plan.get("steps"), "G104B3_PLAN_SHAPE_INVALID")
    campaign = _mapping(steps.get("CAMPAIGN_STATUS_UPDATE"), "G104B3_PLAN_SHAPE_INVALID")
    cells = _list(plan.get("cells"), "G104B3_PLAN_SHAPE_INVALID")
    if len(cells) != 2 or not campaign:
        raise MutationProvenanceError("G104B3_PLAN_SHAPE_INVALID")
    for raw_cell in cells:
        cell = _mapping(raw_cell, "G104B3_PLAN_SHAPE_INVALID")
        cell_steps = _mapping(cell.get("steps"), "G104B3_PLAN_SHAPE_INVALID")
        for name in ("ADSET_STATUS_UPDATE", "AD_STATUS_UPDATE"):
            if not _mapping(cell_steps.get(name), "G104B3_PLAN_SHAPE_INVALID"):
                raise MutationProvenanceError("G104B3_PLAN_SHAPE_INVALID")


def _subject_plan(plan: Mapping[str, Any], subject: Mapping[str, Any]) -> bool:
    experiment_ids = sorted(str(item) for item in _list(
        plan.get("experiment_ids"), "G104B3_PLAN_SHAPE_INVALID",
    ))
    expected_experiments = sorted(str(cell["experiment_id"]) for cell in subject["cells"])
    return (
        str(plan.get("target_account_id") or "") == subject["account_id"]
        and str(plan.get("launch_id") or plan.get("target_object_id") or "") == subject["launch_id"]
        and experiment_ids == expected_experiments
    )


def _status_steps(plan: Mapping[str, Any], subject: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    steps: dict[str, dict[str, str]] = {}
    campaign = _mapping(
        _mapping(plan.get("steps"), "G104B3_PLAN_SHAPE_INVALID").get("CAMPAIGN_STATUS_UPDATE"),
        "G104B3_PLAN_SHAPE_INVALID",
    )
    if campaign:
        steps["CAMPAIGN_STATUS_UPDATE"] = {
            "object_type": "CAMPAIGN", "object_id": str(campaign.get("target_id") or ""),
            "object_key": str(campaign.get("object_key") or ""),
            "before": str(campaign.get("before_status") or ""), "after": str(campaign.get("status") or ""),
            "expected_object_id": str(subject["campaign_id"]),
        }
    cells = {str(cell["cell_id"]).upper(): cell for cell in subject["cells"]}
    for raw_cell in _list(plan.get("cells"), "G104B3_PLAN_SHAPE_INVALID"):
        cell = _mapping(raw_cell, "G104B3_PLAN_SHAPE_INVALID")
        cell_key = str(cell.get("cell_key") or "").upper()
        expected = cells.get(cell_key)
        if not expected or str(cell.get("experiment_id") or "") != expected["experiment_id"]:
            continue
        for step_name, object_type, subject_key in (
            ("ADSET_STATUS_UPDATE", "ADSET", "adset_id"),
            ("AD_STATUS_UPDATE", "AD", "ad_id"),
        ):
            raw_step = _mapping(
                _mapping(cell.get("steps"), "G104B3_PLAN_SHAPE_INVALID").get(step_name),
                "G104B3_PLAN_SHAPE_INVALID",
            )
            if raw_step:
                steps[f"{cell_key}_{step_name}"] = {
                    "object_type": object_type, "object_id": str(raw_step.get("target_id") or ""),
                    "object_key": str(raw_step.get("object_key") or ""),
                    "before": str(raw_step.get("before_status") or ""),
                    "after": str(raw_step.get("status") or ""),
                    "expected_object_id": str(expected[subject_key]),
                }
    return steps


def _derive_gle_chain(
    action: Mapping[str, Any], approvals: list[Mapping[str, Any]], tasks: list[Mapping[str, Any]],
    receipts_by_task: Mapping[str, list[Mapping[str, Any]]], subject: Mapping[str, Any], cutoff: datetime,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    try:
        action_payload = _decode_object(action["payload_json"], "G104B3_ACTION_JSON_INVALID")
        plan = _mapping(action_payload.get("plan"), "G104B3_PLAN_SHAPE_INVALID")
    except (KeyError, TypeError, ValueError, MutationProvenanceError):
        return [], ["GLE_ACTION_PAYLOAD_INVALID"], True
    if not _subject_plan(plan, subject):
        return [], [], False
    if (
        str(action.get("action_type") or "").upper() != "REACTIVATE_AD"
        or str(plan.get("action_type") or "").upper() != "REACTIVATE_AD"
    ):
        return [], ["GLE_ACTION_TYPE_NOT_ADMITTED_FOR_EXACT_BEFORE_AFTER"], True
    _validate_reactivation_plan_shape(plan)
    if (
        str(action.get("status") or "").upper() != "VERIFIED"
        or str(action.get("action_scope") or "").upper() != "EXPERIMENT"
        or str(action.get("target_type") or "").upper() != str(plan.get("target_object_type") or "").upper()
        or str(action.get("target_id") or "") != str(plan.get("target_object_id") or "")
    ):
        return [], ["GLE_ACTION_NOT_VERIFIED"], True
    selected_approvals = [item for item in approvals if item["operation_action_id"] == action["operation_action_id"]]
    selected_tasks = [item for item in tasks if item["operation_action_id"] == action["operation_action_id"]]
    if len(selected_approvals) != 1 or len(selected_tasks) != 1:
        return [], ["GLE_APPROVAL_OR_TASK_DENOMINATOR_INVALID"], True
    approval = selected_approvals[0]
    task = selected_tasks[0]
    try:
        approval_plan = _decode_object(approval["plan_json"], "G104B3_APPROVAL_JSON_INVALID")
        task_payload = _decode_object(task["payload_json"], "G104B3_TASK_JSON_INVALID")
        object_ids = _decode_object(task["meta_object_ids_json"], "G104B3_TASK_JSON_INVALID")
        task_plan = _mapping(task_payload.get("plan"), "G104B3_TASK_JSON_INVALID")
        task_approval = _mapping(task_payload.get("approval"), "G104B3_TASK_JSON_INVALID")
        approved_at = _utc(approval["approved_at"])
        consumed_at = _utc(approval["consumed_at"])
        expires_at = _utc(approval["expires_at"])
        action_created = _utc(action["created_at"])
        action_updated = _utc(action["updated_at"])
        approval_created = _utc(approval["created_at"])
        approval_updated = _utc(approval["updated_at"])
        task_created = _utc(task["created_at"])
        task_updated = _utc(task["updated_at"])
        task_finished = _utc(task["finished_at"])
        plan_expires = _utc(plan.get("expires_at"), "G104B3_PLAN_EXPIRY_INVALID")
    except (KeyError, TypeError, ValueError, MutationProvenanceError):
        return [], ["GLE_APPROVAL_OR_TASK_INVALID"], True
    plan_digest = hash_json(plan)
    approval_snapshot = {
        "approval_id": str(approval["approval_id"]),
        "status": str(approval["status"]),
        "approved_by": str(approval["approved_by"]),
        "approved_at": str(approval["approved_at"]),
        "expires_at": str(approval["expires_at"]),
        "consumed_at": str(approval["consumed_at"]),
    }
    if (
        approval["status"] != "APPROVED"
        or approval["plan_hash"] != plan_digest
        or approval_plan != plan
        or approval["request_hash"] != hash_json({
            "operation_action_id": action["operation_action_id"], "plan": plan,
        })
        or not str(approval["idempotency_key"])
        or not str(approval["approved_by"]).startswith("operator:")
        or expires_at != plan_expires
        or not action_created <= approval_created <= approved_at <= consumed_at <= approval_updated <= cutoff
        or str(task["status"]).upper() not in {"SUCCESS", "VERIFIED"}
        or str(task["current_step"]).upper() != "RECEIPT"
        or not str(task["idempotency_key"])
        or str(task_payload.get("execution_mode") or "").lower() != "live"
        or str(task_payload.get("action_type") or "").upper() != "REACTIVATE_AD"
        or str(task_payload.get("account_id") or "") != subject["account_id"]
        or task_plan != plan
        or task_approval != approval_snapshot
        or task["request_hash"] != hash_json({
            "operation_action_id": action["operation_action_id"], "payload": task_payload,
        })
        or not consumed_at <= task_created <= task_updated <= task_finished <= action_updated <= cutoff
        or task_finished > expires_at
    ):
        return [], ["GLE_PLAN_APPROVAL_TASK_BINDING_INVALID"], True
    required_ids = {
        "campaign_id": str(subject["campaign_id"]),
        "study_id": str(subject["study_id"]),
    }
    for cell in subject["cells"]:
        prefix = str(cell["cell_id"]).lower()
        required_ids.update({
            f"{prefix}_study_cell_id": str(cell["study_cell_id"]),
            f"{prefix}_adset_id": str(cell["adset_id"]),
            f"{prefix}_ad_id": str(cell["ad_id"]),
        })
    if any(str(object_ids.get(key) or "") != expected for key, expected in required_ids.items()):
        return [], ["GLE_TASK_OBJECT_DENOMINATOR_INVALID"], True
    task_id = str(task["execution_task_id"])
    receipts = list(receipts_by_task.get(task_id) or [])
    try:
        expected_steps = list(execution_steps_for("REACTIVATE_AD", task_payload))
        status_steps = _status_steps(plan, subject)
    except (TypeError, ValueError, MutationProvenanceError):
        return [], ["GLE_PLAN_STATUS_STEP_INVALID"], True
    expected_receipts = expected_steps + ["VERIFY", "RECEIPT"]
    if [str(item["step_name"]).upper() for item in receipts] != expected_receipts:
        return [], ["GLE_RECEIPT_STEP_DENOMINATOR_INVALID"], True
    if set(status_steps) != set(expected_steps):
        return [], ["GLE_PLAN_STATUS_STEP_INVALID"], True
    if any(
        item["before"].upper() != "PAUSED" or item["after"].upper() != "ACTIVE"
        for item in status_steps.values()
    ):
        return [], ["GLE_PLAN_STATUS_TRANSITION_INVALID"], True
    final_statuses = {
        item["object_key"]: item["after"].upper() for item in status_steps.values()
    }
    events: list[dict[str, Any]] = []
    prior_time = task_created
    for receipt in receipts:
        step = str(receipt["step_name"]).upper()
        try:
            created_at = _utc(receipt["created_at"])
            result = _decode_object(receipt["step_result_json"], "G104B3_RECEIPT_JSON_INVALID")
            verification = _decode_object(receipt["verification_result_json"], "G104B3_RECEIPT_JSON_INVALID")
            receipt_ids = _decode_object(receipt["meta_object_ids_json"], "G104B3_RECEIPT_JSON_INVALID")
        except MutationProvenanceError:
            return [], ["GLE_RECEIPT_INVALID"], True
        expected_status = "VERIFIED" if step == "VERIFY" or is_delivery_status_step(step) else "SUCCESS"
        if step == "RECEIPT":
            expected_status = "SUCCESS"
        if (
            not prior_time <= created_at <= min(cutoff, expires_at, task_finished)
            or str(receipt["step_status"]).upper() != expected_status
            or receipt_ids != object_ids
        ):
            return [], ["GLE_RECEIPT_INVALID"], True
        prior_time = created_at
        if step in expected_steps:
            planned = status_steps[step]
            expected_object_id = planned.get("expected_object_id", planned["object_id"])
            verification_ids = verification.get("meta_object_ids")
            verification_statuses = verification.get("object_statuses")
            execution_ids = result.get("meta_object_ids")
            execution_result = result.get("result")
            if (
                planned["object_id"] != expected_object_id
                or not planned["object_key"]
                or str(receipt_ids.get(planned["object_key"]) or "") != planned["object_id"]
                or not planned["before"] or not planned["after"]
                or set(result) != {"status", "meta_object_ids", "result"}
                or str(result.get("status") or "").upper() != "SUCCESS"
                or str(verification.get("status") or "").upper() != "SUCCESS"
                or execution_ids != {planned["object_key"]: planned["object_id"]}
                or not isinstance(execution_result, dict)
                or execution_result.get("already_target_status") is True
                or not isinstance(verification_ids, dict)
                or str(verification_ids.get(planned["object_key"]) or "") != planned["object_id"]
                or not isinstance(verification_statuses, dict)
                or verification_statuses != {planned["object_key"]: planned["after"].upper()}
            ):
                return [], ["GLE_RECEIPT_OBJECT_OR_VALUE_INVALID"], True
            events.append(_event(
                object_type=planned["object_type"], object_id=planned["object_id"], field="status",
                before=planned["before"], after=planned["after"], receipt_observed_at=receipt["created_at"],
                evidence_refs=[
                    {"artifact_type": "GROWTH_OPERATION_ACTION", "record_id": str(action["operation_action_id"])},
                    {"artifact_type": "GROWTH_OPERATION_APPROVAL", "record_id": str(approval["approval_id"])},
                    {"artifact_type": "META_EXECUTION_TASK", "record_id": task_id},
                    {"artifact_type": "META_EXECUTION_TASK_RECEIPT", "record_id": str(receipt["receipt_id"])},
                ],
                receipt_chain_status="PLAN_APPROVAL_TASK_VERIFY_RECEIPT_CLOSED",
            ))
        elif step == "VERIFY":
            if (
                str(verification.get("status") or "").upper() != "SUCCESS"
                or verification.get("meta_object_ids") != object_ids
                or verification.get("object_statuses") != final_statuses
            ):
                return [], ["GLE_FINAL_VERIFY_INVALID"], True
        elif step == "RECEIPT":
            if (
                str(result.get("final_status") or "").upper() != "SUCCESS"
                or str(verification.get("status") or "").upper() != "SUCCESS"
                or verification.get("meta_object_ids") != object_ids
                or verification.get("object_statuses") != final_statuses
            ):
                return [], ["GLE_FINAL_RECEIPT_INVALID"], True
    return events, [
        "ACTUAL_BEFORE_NOT_OBSERVED",
        "MUTATION_TIME_NOT_OBSERVED",
        "EXTERNAL_ACTIVITY_NOT_CORRELATED",
    ], True


def _require_sidecars_absent(path: Path, parent_fd: int) -> None:
    for suffix in ("-wal", "-journal", "-shm"):
        try:
            value = os.stat(path.name + suffix, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if value.st_size:
            raise MutationProvenanceError("G104B3_SOURCE_SIDECAR_PRESENT:" + suffix)


def _load_source(
    path: Path, expected_sha256: str, request: Mapping[str, Any], source_fd: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    sqlite_path = f"/proc/self/fd/{source_fd}" if Path("/proc/self/fd").is_dir() else f"/dev/fd/{source_fd}"
    uri = f"file:{quote(sqlite_path, safe='/')}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        database_list = conn.execute("PRAGMA database_list").fetchall()
        if (
            int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1
            or len(database_list) != 1
            or str(database_list[0][1]) != "main"
            or str(database_list[0][2]) != sqlite_path
        ):
            raise MutationProvenanceError("G104B3_SOURCE_CONNECTION_INVALID")
        schemas = (
            ("growth_operation_action", _ACTION_FIELDS, "operation_action_id"),
            ("growth_operation_approval", _APPROVAL_FIELDS, "approval_id"),
            ("meta_execution_task", _TASK_FIELDS, "execution_task_id"),
            ("meta_execution_task_receipt", _RECEIPT_FIELDS, "receipt_id"),
            ("ad_experiment_events", _EVENT_FIELDS, "event_id"),
        )
        for table, fields, primary in schemas:
            _required_schema(conn, table, fields, primary)
        materialized_bytes = 0
        window = request["window_start"]
        cutoff = request["data_cutoff_at"]
        action_where = "action_scope='EXPERIMENT' AND created_at>=? AND created_at<=?"
        _, action_bytes = _preflight_rows(
            conn, table="growth_operation_action", fields=_ACTION_FIELDS,
            where=action_where, params=(window, cutoff), maximum_rows=MAX_ACTION_ROWS,
        )
        materialized_bytes = _consume_materialization_budget(materialized_bytes, action_bytes)
        actions = _materialize_rows(conn, table="growth_operation_action", fields=_ACTION_FIELDS,
                                    where=action_where, params=(window, cutoff), order="created_at, operation_action_id")
        action_ids = [str(item["operation_action_id"]) for item in actions]
        if action_ids:
            marks = ",".join("?" for _ in action_ids)
            related = f"operation_action_id IN ({marks})"
            _, approval_bytes = _preflight_rows(
                conn, table="growth_operation_approval", fields=_APPROVAL_FIELDS,
                where=related, params=action_ids, maximum_rows=MAX_APPROVAL_ROWS,
            )
            _, task_bytes = _preflight_rows(
                conn, table="meta_execution_task", fields=_TASK_FIELDS,
                where=related, params=action_ids, maximum_rows=MAX_TASK_ROWS,
            )
            materialized_bytes = _consume_materialization_budget(materialized_bytes, approval_bytes)
            materialized_bytes = _consume_materialization_budget(materialized_bytes, task_bytes)
            approvals = _materialize_rows(conn, table="growth_operation_approval", fields=_APPROVAL_FIELDS,
                                         where=related, params=action_ids, order="created_at, approval_id")
            tasks = _materialize_rows(conn, table="meta_execution_task", fields=_TASK_FIELDS,
                                     where=related, params=action_ids, order="created_at, execution_task_id")
        else:
            approvals, tasks = [], []
        task_ids = [str(item["execution_task_id"]) for item in tasks]
        if task_ids:
            marks = ",".join("?" for _ in task_ids)
            related = f"execution_task_id IN ({marks})"
            _, receipt_bytes = _preflight_rows(
                conn, table="meta_execution_task_receipt", fields=_RECEIPT_FIELDS,
                where=related, params=task_ids, maximum_rows=MAX_RECEIPT_ROWS,
            )
            materialized_bytes = _consume_materialization_budget(materialized_bytes, receipt_bytes)
            receipts = _materialize_rows(conn, table="meta_execution_task_receipt", fields=_RECEIPT_FIELDS,
                                        where=related, params=task_ids, order="created_at, receipt_id")
        else:
            receipts = []
        experiments = [str(cell["experiment_id"]) for cell in request["subject"]["cells"]]
        marks = ",".join("?" for _ in experiments)
        event_where = f"experiment_id IN ({marks}) AND created_at>=? AND created_at<=?"
        event_params = (*experiments, window, cutoff)
        _, event_bytes = _preflight_rows(
            conn, table="ad_experiment_events", fields=_EVENT_FIELDS,
            where=event_where, params=event_params, maximum_rows=MAX_EVENT_ROWS,
        )
        _consume_materialization_budget(materialized_bytes, event_bytes)
        retained = _materialize_rows(conn, table="ad_experiment_events", fields=_EVENT_FIELDS,
                                     where=event_where, params=event_params, order="created_at, event_id")
        return actions, approvals, tasks, receipts, retained
    finally:
        conn.close()


def derive_mutation_provenance(
    source_request_raw: bytes,
    *,
    expected_request_sha256: str,
    source_snapshot_path: str | Path,
    expected_source_snapshot_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    request = _validate_request(source_request_raw, expected_request_sha256)
    expected_source = _sha(expected_source_snapshot_sha256, "G104B3_SOURCE_ANCHOR_INVALID")
    if expected_source != request["source_snapshot"]["sha256"]:
        raise MutationProvenanceError("G104B3_SOURCE_BINDING_MISMATCH")
    snapshot = Path(source_snapshot_path)
    with _open_stable_file(
        snapshot, maximum=MAX_SOURCE_BYTES, code="G104B3_SOURCE_ARTIFACT_INVALID", required_mode=0o600,
    ) as (source_fd, parent_fd, _before, digest):
        if digest != expected_source:
            raise MutationProvenanceError("G104B3_SOURCE_ANCHOR_MISMATCH")
        _require_sidecars_absent(snapshot, parent_fd)
        actions, approvals, tasks, receipts, retained = _load_source(
            snapshot, expected_source, request, source_fd,
        )
        _require_sidecars_absent(snapshot, parent_fd)

    receipts_by_task: dict[str, list[Mapping[str, Any]]] = {}
    for receipt in receipts:
        receipts_by_task.setdefault(str(receipt["execution_task_id"]), []).append(receipt)
    cutoff = _utc(request["data_cutoff_at"])
    events: list[dict[str, Any]] = []
    gaps = {
        "EXTERNAL_MUTATION_DENOMINATOR_UNKNOWN",
        "META_ACTIVITY_SOURCE_NOT_PROVIDED",
        "CURRENT_STATE_READBACK_NOT_PROVIDED",
        "RETENTION_COMPLETENESS_UNKNOWN",
        "SOURCE_CONTENT_AUTHORITY_NOT_VERIFIED",
    }
    admitted_chains = 0
    for action in actions:
        try:
            derived, chain_gaps, subject_related = _derive_gle_chain(
                action, approvals, tasks, receipts_by_task, request["subject"], cutoff,
            )
        except MutationProvenanceError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise MutationProvenanceError("G104B3_SOURCE_NESTED_SHAPE_INVALID") from exc
        if subject_related:
            admitted_chains += 1
            gaps.update(chain_gaps)
            events.extend(derived)
    retained_context_hashes: list[str] = []
    window_start = _utc(request["window_start"])
    for row in retained:
        created_at = _utc(row["created_at"])
        if not window_start <= created_at <= cutoff:
            raise MutationProvenanceError("G104B3_RETAINED_EVENT_TIME_INVALID")
        before = str(row["from_state"])
        after = str(row["to_state"])
        if not before or not after:
            gaps.add("RETAINED_EVENT_STATE_VALUE_MISSING")
            continue
        retained_context_hashes.append(hash_json({
            "event_id": str(row["event_id"]),
            "experiment_id": str(row["experiment_id"]),
            "from_state_hash": hash_json(before),
            "to_state_hash": hash_json(after),
            "event_type_hash": hash_json(str(row["event_type"])),
            "created_at": str(row["created_at"]),
        }))
    expected_denominator = {
        (item["object_type"], item["object_id"], item["field"])
        for item in request["relevant_fields"]
    }
    if any(
        (item["object_type"], item["object_id"], item["field"]) not in expected_denominator
        for item in events
    ):
        raise MutationProvenanceError("G104B3_EVENT_DENOMINATOR_INVALID")
    events.sort(key=lambda item: (
        item["receipt_observed_at"], item["object_type"], item["object_id"], item["field"],
        item["event_hash"],
    ))
    gle_events = len(events)
    retained_events = len(retained_context_hashes)
    if gle_events:
        status = "RECONCILED_GLE_RECEIPT_OBSERVATION_SUBSET"
    elif retained_events:
        status = "INCOMPLETE_MUTATION_PROVENANCE"
    else:
        status = "NO_MUTATIONS_OBSERVED_WITH_INCOMPLETE_COVERAGE"
        gaps.add("NO_RETAINED_ROWS_OBSERVED")
    event_bundle: dict[str, Any] = {
        "schema_version": EVENTS_VERSION,
        "evidence_id": request["evidence_id"],
        "request_hash": request["request_hash"],
        "source_snapshot_sha256": expected_source,
        "events": events,
        "event_root": hash_json([item["event_hash"] for item in events]),
        "events_hash": "",
    }
    event_bundle["events_hash"] = hash_json({key: item for key, item in event_bundle.items() if key != "events_hash"})
    coverage: dict[str, Any] = {
        "schema_version": COVERAGE_VERSION,
        "evidence_id": request["evidence_id"],
        "request_hash": request["request_hash"],
        "window_start": request["window_start"],
        "data_cutoff_at": request["data_cutoff_at"],
        "local_rows": {
            "actions_in_window": len(actions),
            "subject_related_action_chains_examined": admitted_chains,
            "approval_rows": len(approvals),
            "task_rows": len(tasks),
            "receipt_rows": len(receipts),
            "retained_experiment_events": len(retained),
        },
        "normalized_events": {
            "gle_receipt_chain_observations": gle_events,
            "local_retained_context_rows": retained_events,
        },
        "retained_context_root": hash_json(sorted(retained_context_hashes)),
        "complete_event_journal": False,
        "external_mutation_coverage": "UNKNOWN",
        "retention_status": "UNKNOWN",
        "reason_codes": sorted(gaps),
        "coverage_hash": "",
    }
    coverage["coverage_hash"] = hash_json({key: item for key, item in coverage.items() if key != "coverage_hash"})
    assessment: dict[str, Any] = {
        "schema_version": ASSESSMENT_VERSION,
        "evidence_id": request["evidence_id"],
        "request_hash": request["request_hash"],
        "events_hash": event_bundle["events_hash"],
        "coverage_hash": coverage["coverage_hash"],
        "status": status,
        "trust_status": "SOURCE_CONTENT_AUTHORITY_NOT_VERIFIED",
        "reason_codes": sorted(gaps),
        "ceiling": dict(CEILING),
        "assessment_hash": "",
    }
    assessment["assessment_hash"] = hash_json({key: item for key, item in assessment.items() if key != "assessment_hash"})
    return request, event_bundle, coverage, assessment


def _manifest(
    request: Mapping[str, Any], events: Mapping[str, Any], coverage: Mapping[str, Any],
    assessment: Mapping[str, Any], payloads: Mapping[str, bytes],
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": MANIFEST_VERSION,
        "evidence_id": request["evidence_id"],
        "request_hash": request["request_hash"],
        "events_hash": events["events_hash"],
        "coverage_hash": coverage["coverage_hash"],
        "assessment_hash": assessment["assessment_hash"],
        "status": assessment["status"],
        "ceiling": dict(CEILING),
        "files": {
            name: {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
            for name, raw in sorted(payloads.items())
        },
        "manifest_hash": "",
    }
    value["manifest_hash"] = hash_json({key: item for key, item in value.items() if key != "manifest_hash"})
    return value


def _require_dir_identity(parent_fd: int, name: str, opened_fd: int) -> None:
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    opened = os.fstat(opened_fd)
    if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
        raise MutationProvenanceError("G104B3_OUTPUT_DIRECTORY_CHANGED")


def _write_file(directory_fd: int, name: str, raw: bytes) -> None:
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600, dir_fd=directory_fd,
    )
    try:
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                raise MutationProvenanceError("G104B3_WRITE_FAILED")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(name, 0o600, dir_fd=directory_fd, follow_symlinks=False)


def _write_directory(root: Path, payloads: Mapping[str, bytes]) -> None:
    if set(payloads) != EXACT_ARTIFACT_FILES or not root.name or root.name in {".", ".."}:
        raise MutationProvenanceError("G104B3_OUTPUT_INVALID")
    if any(len(raw) > MAX_ARTIFACT_FILE_BYTES for raw in payloads.values()) or sum(map(len, payloads.values())) > MAX_TOTAL_ARTIFACT_BYTES:
        raise MutationProvenanceError("G104B3_ARTIFACT_TOO_LARGE")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_path = root.parent.resolve(strict=True)
        parent_fd = os.open(parent_path, flags)
    except OSError as exc:
        raise MutationProvenanceError("G104B3_OUTPUT_PARENT_INVALID") from exc
    root_fd: int | None = None
    complete = False
    try:
        try:
            os.mkdir(root.name, 0o700, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise MutationProvenanceError("G104B3_OUTPUT_EXISTS") from exc
        root_fd = os.open(root.name, flags, dir_fd=parent_fd)
        os.fchmod(root_fd, 0o700)
        _require_dir_identity(parent_fd, root.name, root_fd)
        for name in sorted(payloads):
            _write_file(root_fd, name, payloads[name])
        if set(os.listdir(root_fd)) != EXACT_ARTIFACT_FILES:
            raise MutationProvenanceError("G104B3_OUTPUT_FILE_SET_INVALID")
        os.fsync(root_fd)
        _require_dir_identity(parent_fd, root.name, root_fd)
        complete = True
        os.fsync(parent_fd)
    except Exception as exc:
        if complete:
            raise MutationProvenanceError("G104B3_OUTPUT_DURABILITY_UNCERTAIN") from exc
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


def _read_directory(root: Path) -> dict[str, bytes]:
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    parent_fd: int | None = None
    root_fd: int | None = None
    try:
        parent_path = root.parent.resolve(strict=True)
        parent_fd = os.open(parent_path, dir_flags)
        root_fd = os.open(root.name, dir_flags, dir_fd=parent_fd)
        parent_before = os.fstat(parent_fd)
        root_before = os.fstat(root_fd)
        if stat.S_IMODE(root_before.st_mode) != 0o700 or set(os.listdir(root_fd)) != EXACT_ARTIFACT_FILES:
            raise MutationProvenanceError("G104B3_ARTIFACT_DIRECTORY_INVALID")
        result: dict[str, bytes] = {}
        total = 0
        for name in sorted(EXACT_ARTIFACT_FILES):
            fd = os.open(name, file_flags, dir_fd=root_fd)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1 or before.st_size > MAX_ARTIFACT_FILE_BYTES:
                    raise MutationProvenanceError("G104B3_ARTIFACT_FILE_INVALID")
                raw = _read_fd(fd, MAX_ARTIFACT_FILE_BYTES, "G104B3_ARTIFACT_TOO_LARGE")
                total += len(raw)
                after = os.fstat(fd)
                if total > MAX_TOTAL_ARTIFACT_BYTES or _file_identity(before) != _file_identity(after) or len(raw) != after.st_size:
                    raise MutationProvenanceError("G104B3_ARTIFACT_CHANGED_DURING_READ")
                result[name] = raw
            finally:
                os.close(fd)
        if set(os.listdir(root_fd)) != EXACT_ARTIFACT_FILES:
            raise MutationProvenanceError("G104B3_ARTIFACT_CHANGED_DURING_READ")
        _require_dir_identity(parent_fd, root.name, root_fd)
        if _dir_identity(parent_before) != _dir_identity(os.fstat(parent_fd)) or _dir_identity(root_before) != _dir_identity(os.fstat(root_fd)):
            raise MutationProvenanceError("G104B3_ARTIFACT_CHANGED_DURING_READ")
        return result
    except OSError as exc:
        raise MutationProvenanceError("G104B3_ARTIFACT_DIRECTORY_INVALID") from exc
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def write_mutation_provenance_artifact(
    output_dir: str | Path,
    source_request_raw: bytes,
    *,
    expected_request_sha256: str,
    source_snapshot_path: str | Path,
    expected_source_snapshot_sha256: str,
) -> dict[str, Any]:
    request, events, coverage, assessment = derive_mutation_provenance(
        source_request_raw,
        expected_request_sha256=expected_request_sha256,
        source_snapshot_path=source_snapshot_path,
        expected_source_snapshot_sha256=expected_source_snapshot_sha256,
    )
    payloads = {
        "source-request.json": source_request_raw,
        "mutation-events.json": _json_bytes(events),
        "coverage.json": _json_bytes(coverage),
        "provenance-assessment.json": _json_bytes(assessment),
    }
    manifest = _manifest(request, events, coverage, assessment, payloads)
    payloads["manifest.json"] = _json_bytes(manifest)
    output = Path(output_dir)
    _write_directory(output, payloads)
    loaded = load_validated_mutation_provenance_directory(
        output,
        expected_manifest_sha256=hashlib.sha256(payloads["manifest.json"]).hexdigest(),
        source_snapshot_path=source_snapshot_path,
    )
    if loaded["manifest"] != manifest:
        raise MutationProvenanceError("G104B3_WRITE_READBACK_MISMATCH")
    return manifest


def load_validated_mutation_provenance_directory(
    artifact_dir: str | Path,
    *,
    expected_manifest_sha256: str,
    source_snapshot_path: str | Path,
) -> dict[str, Any]:
    expected_manifest = _sha(expected_manifest_sha256, "G104B3_MANIFEST_ANCHOR_INVALID")
    raw = _read_directory(Path(artifact_dir))
    if hashlib.sha256(raw["manifest.json"]).hexdigest() != expected_manifest:
        raise MutationProvenanceError("G104B3_MANIFEST_ANCHOR_MISMATCH")
    request = _json_document(raw["source-request.json"])
    events = _json_document(raw["mutation-events.json"])
    coverage = _json_document(raw["coverage.json"])
    assessment = _json_document(raw["provenance-assessment.json"])
    manifest = _json_document(raw["manifest.json"])
    if not all(isinstance(item, dict) for item in (request, events, coverage, assessment, manifest)):
        raise MutationProvenanceError("G104B3_ARTIFACT_JSON_INVALID")
    expected_request_sha = hashlib.sha256(raw["source-request.json"]).hexdigest()
    expected_source_sha = str(request.get("source_snapshot", {}).get("sha256") or "")
    derived = derive_mutation_provenance(
        raw["source-request.json"],
        expected_request_sha256=expected_request_sha,
        source_snapshot_path=source_snapshot_path,
        expected_source_snapshot_sha256=expected_source_sha,
    )
    expected_request, expected_events, expected_coverage, expected_assessment = derived
    if request != expected_request or events != expected_events or coverage != expected_coverage or assessment != expected_assessment:
        raise MutationProvenanceError("G104B3_ARTIFACT_DERIVATION_MISMATCH")
    payloads = {name: raw[name] for name in EXACT_ARTIFACT_FILES - {"manifest.json"}}
    rebuilt_manifest = _manifest(expected_request, expected_events, expected_coverage, expected_assessment, payloads)
    if manifest != rebuilt_manifest:
        raise MutationProvenanceError("G104B3_MANIFEST_DERIVATION_MISMATCH")
    return {
        "request": expected_request,
        "events": expected_events,
        "coverage": expected_coverage,
        "assessment": expected_assessment,
        "manifest": rebuilt_manifest,
    }


__all__ = [
    "CEILING", "EXACT_ARTIFACT_FILES", "MutationProvenanceError",
    "derive_mutation_provenance", "hash_json", "load_validated_mutation_provenance_directory",
    "read_external_request", "write_mutation_provenance_artifact",
]
