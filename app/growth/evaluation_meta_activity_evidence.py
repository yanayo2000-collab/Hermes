"""Bounded GET-only Meta activity and current-state evidence for S04-01B4.

This module deliberately produces an observation fragment, not a canonical
EvaluationInputSnapshot and not a Gate receipt.  It reuses the reviewed G0-04
GET-only transport, projects only the frozen five-object status denominator,
and publishes a source-capture file so every derived record can be rebuilt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.growth.gate0_topology_audit import (
    G004ContractError,
    G004GraphError,
    GetOnlyGraphClient,
    canonical_json as _g004_canonical_json,
    normalize_actor_registry,
)


REQUEST_VERSION = "gle-e04-s04-01b4-meta-activity-request-v1"
CAPTURE_VERSION = "gle-e04-s04-01b4-meta-graph-capture-v1"
ACTIVITY_VERSION = "gle-e04-s04-01b4-meta-activity-observations-v1"
READBACK_VERSION = "gle-e04-s04-01b4-current-state-readbacks-v1"
COVERAGE_VERSION = "gle-e04-s04-01b4-meta-activity-coverage-v1"
MANIFEST_VERSION = "gle-e04-s04-01b4-meta-activity-manifest-v1"

GRAPH_API_VERSION = "v25.0"
ACTIVITY_FIELDS = (
    "id,event_time,date_time_in_timezone,event_type,object_id,object_type,"
    "changed_data,extra_data,actor_id,actor_name,application_id,application_name"
)
STATE_FIELDS = "id,account_id,campaign_id,adset_id,status,effective_status,updated_time"
STUDY_FIELDS = "id,type,start_time,end_time"
STUDY_CELL_FIELDS = "id,ad_ids"
CELL_ADSET_FIELDS = "id,campaign_id,account_id"
CELL_CAMPAIGN_FIELDS = "id,account_id"
EXACT_ARTIFACT_FILES = {
    "source-request.json",
    "graph-capture.json",
    "activity-observations.json",
    "current-state-readbacks.json",
    "coverage.json",
    "manifest.json",
}
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_REGISTRY_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_FILE_BYTES = 16 * 1024 * 1024
MAX_ACTIVITY_ROWS = 100
MAX_WINDOW = timedelta(days=31)
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_STATUS_VALUES = {"ACTIVE", "PAUSED"}

CEILING = {
    "source_content_authority": "NOT_VERIFIED",
    "actor_registry_selection_authority": "NOT_VERIFIED",
    "activity_effect": "CALLER_ANCHORED_GET_CAPTURE_CLAIM_ONLY",
    "live_graph_transport_attestation": "NOT_PROVIDED",
    "complete_event_journal": False,
    "external_mutation_coverage": "UNKNOWN_OUTSIDE_CAPTURE_WINDOW",
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


class MetaActivityEvidenceError(ValueError):
    """Stable validation, integrity, and I/O error contract."""


def canonical_json(value: Any) -> str:
    try:
        return _g004_canonical_json(value)
    except (G004ContractError, TypeError, ValueError) as exc:
        raise MetaActivityEvidenceError("G104B4_JSON_INVALID") from exc


def _json_bytes(value: Any) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def hash_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _unique_pairs(pairs: Sequence[tuple[str, Any]], code: str) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise MetaActivityEvidenceError(code)
        value[key] = item
    return value


def _json_document(raw: bytes, code: str = "G104B4_JSON_INVALID", *, canonical: bool = True) -> Any:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=lambda pairs: _unique_pairs(pairs, code),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise MetaActivityEvidenceError(code) from exc
    if canonical and raw != _json_bytes(value):
        raise MetaActivityEvidenceError(code)
    return value


def _sha(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise MetaActivityEvidenceError(code)
    return value


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise MetaActivityEvidenceError(code)
    return value


def _utc(value: Any, code: str = "G104B4_TIME_INVALID") -> datetime:
    if not isinstance(value, str) or not value or value.endswith("Z"):
        raise MetaActivityEvidenceError(code)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MetaActivityEvidenceError(code) from exc
    normalized = parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    if (
        normalized is None
        or parsed.utcoffset() != timezone.utc.utcoffset(None)
        or value != normalized.isoformat()
    ):
        raise MetaActivityEvidenceError(code)
    return normalized


def _source_utc(value: Any, code: str = "G104B4_SOURCE_TIME_INVALID") -> datetime:
    """Accept Graph's ISO offset variants, then emit one canonical UTC instant."""

    if not isinstance(value, str) or not value:
        raise MetaActivityEvidenceError(code)
    candidate = value.replace("Z", "+00:00")
    if re.search(r"[+-][0-9]{4}$", candidate):
        candidate = candidate[:-2] + ":" + candidate[-2:]
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise MetaActivityEvidenceError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MetaActivityEvidenceError(code)
    return parsed.astimezone(timezone.utc)


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _dir_identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_mtime_ns, value.st_ctime_ns)


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
            raise MetaActivityEvidenceError(code)
        chunks.append(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return b"".join(chunks)


@contextmanager
def _open_stable_file(
    path: Path,
    *,
    maximum: int,
    code: str,
    required_mode: int = 0o600,
) -> Iterable[tuple[int, bytes, str]]:
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    parent_fd = -1
    fd = -1
    try:
        parent = path.parent.resolve(strict=True)
        parent_fd = os.open(parent, dir_flags)
        parent_before = os.fstat(parent_fd)
        fd = os.open(path.name, file_flags, dir_fd=parent_fd)
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > maximum
            or stat.S_IMODE(before.st_mode) != required_mode
        ):
            raise MetaActivityEvidenceError(code)
        raw = _read_fd(fd, maximum, code)
        digest = hashlib.sha256(raw).hexdigest()
        yield fd, raw, digest
        after = os.fstat(fd)
        named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _file_identity(before) != _file_identity(after)
            or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)
            or _dir_identity(parent_before) != _dir_identity(os.fstat(parent_fd))
        ):
            raise MetaActivityEvidenceError(code)
    except OSError as exc:
        raise MetaActivityEvidenceError(code) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def read_external_json(
    path: str | Path,
    expected_sha256: str,
    *,
    maximum: int,
    code: str,
    canonical: bool = True,
) -> tuple[dict[str, Any], bytes]:
    expected = _sha(expected_sha256, code)
    with _open_stable_file(Path(path), maximum=maximum, code=code) as (_fd, raw, digest):
        if digest != expected:
            raise MetaActivityEvidenceError(code)
        value = _json_document(raw, code, canonical=canonical)
    if not isinstance(value, dict):
        raise MetaActivityEvidenceError(code)
    return value, raw


def _normalize_subject(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "account_id", "market", "study_id", "campaign_id", "cells",
    }:
        raise MetaActivityEvidenceError("G104B4_SUBJECT_INVALID")
    account_id = _identifier(value["account_id"], "G104B4_SUBJECT_INVALID")
    if account_id.startswith("act_"):
        raise MetaActivityEvidenceError("G104B4_SUBJECT_INVALID")
    subject = {
        "account_id": account_id,
        "market": value["market"],
        "study_id": _identifier(value["study_id"], "G104B4_SUBJECT_INVALID"),
        "campaign_id": _identifier(value["campaign_id"], "G104B4_SUBJECT_INVALID"),
        "cells": [],
    }
    if subject["market"] != "MX":
        raise MetaActivityEvidenceError("G104B4_SUBJECT_INVALID")
    cells = value["cells"]
    if not isinstance(cells, list) or len(cells) != 2:
        raise MetaActivityEvidenceError("G104B4_SUBJECT_INVALID")
    for raw in cells:
        if not isinstance(raw, dict) or set(raw) != {
            "cell_id", "study_cell_id", "adset_id", "ad_id",
        }:
            raise MetaActivityEvidenceError("G104B4_SUBJECT_INVALID")
        subject["cells"].append({
            key: _identifier(raw[key], "G104B4_SUBJECT_INVALID")
            for key in ("cell_id", "study_cell_id", "adset_id", "ad_id")
        })
    subject["cells"].sort(key=lambda item: item["cell_id"])
    if [item["cell_id"] for item in subject["cells"]] != ["C1", "C2"]:
        raise MetaActivityEvidenceError("G104B4_SUBJECT_INVALID")
    for key in ("study_cell_id", "adset_id", "ad_id"):
        if len({item[key] for item in subject["cells"]}) != 2:
            raise MetaActivityEvidenceError("G104B4_SUBJECT_INVALID")
    denominator_ids = {
        subject["campaign_id"],
        *(item["adset_id"] for item in subject["cells"]),
        *(item["ad_id"] for item in subject["cells"]),
    }
    if len(denominator_ids) != 5:
        raise MetaActivityEvidenceError("G104B4_SUBJECT_INVALID")
    return subject


def _denominator(subject: Mapping[str, Any]) -> list[dict[str, str]]:
    result = [{
        "object_type": "CAMPAIGN",
        "object_id": str(subject["campaign_id"]),
        "field": "status",
    }]
    for cell in list(subject["cells"]):
        result.extend([
            {"object_type": "ADSET", "object_id": str(cell["adset_id"]), "field": "status"},
            {"object_type": "AD", "object_id": str(cell["ad_id"]), "field": "status"},
        ])
    return sorted(result, key=lambda item: (item["object_type"], item["object_id"]))


def validate_request(raw: bytes, expected_sha256: str) -> dict[str, Any]:
    expected = _sha(expected_sha256, "G104B4_REQUEST_ANCHOR_INVALID")
    if (
        not isinstance(raw, bytes)
        or not raw
        or len(raw) > MAX_REQUEST_BYTES
        or hashlib.sha256(raw).hexdigest() != expected
    ):
        raise MetaActivityEvidenceError("G104B4_REQUEST_ANCHOR_MISMATCH")
    value = _json_document(raw)
    keys = {
        "schema_version", "capture_id", "requested_at", "window_start_at",
        "data_cutoff_at", "graph_api_version", "subject", "relevant_fields",
        "study_contract", "activity_contract", "actor_registry", "transport_policy", "ceiling",
        "request_hash",
    }
    if not isinstance(value, dict) or set(value) != keys or value["schema_version"] != REQUEST_VERSION:
        raise MetaActivityEvidenceError("G104B4_REQUEST_INVALID")
    requested = _utc(value["requested_at"])
    window_start = _utc(value["window_start_at"])
    cutoff = _utc(value["data_cutoff_at"])
    if not window_start <= cutoff <= requested or cutoff - window_start > MAX_WINDOW:
        raise MetaActivityEvidenceError("G104B4_TIME_ORDER_INVALID")
    if value["graph_api_version"] != GRAPH_API_VERSION:
        raise MetaActivityEvidenceError("G104B4_GRAPH_VERSION_INVALID")
    subject = _normalize_subject(value["subject"])
    study_contract = value["study_contract"]
    if not isinstance(study_contract, dict) or set(study_contract) != {
        "study_type", "start_at", "end_at",
    } or study_contract["study_type"] != "SPLIT_TEST":
        raise MetaActivityEvidenceError("G104B4_STUDY_CONTRACT_INVALID")
    study_start = _utc(study_contract["start_at"], "G104B4_STUDY_CONTRACT_INVALID")
    study_end = _utc(study_contract["end_at"], "G104B4_STUDY_CONTRACT_INVALID")
    if not study_start <= window_start <= cutoff <= study_end:
        raise MetaActivityEvidenceError("G104B4_STUDY_WINDOW_INVALID")
    activity_contract = value["activity_contract"]
    if activity_contract != {"allowed_event_types": ["STATUS_UPDATE"]}:
        raise MetaActivityEvidenceError("G104B4_ACTIVITY_CONTRACT_INVALID")
    relevant = _denominator(subject)
    if value["relevant_fields"] != relevant:
        raise MetaActivityEvidenceError("G104B4_FIELD_DENOMINATOR_INVALID")
    registry = value["actor_registry"]
    if not isinstance(registry, dict) or set(registry) != {"raw_sha256", "semantic_hash"}:
        raise MetaActivityEvidenceError("G104B4_REGISTRY_BINDING_INVALID")
    registry = {
        "raw_sha256": _sha(registry["raw_sha256"], "G104B4_REGISTRY_BINDING_INVALID"),
        "semantic_hash": _sha(registry["semantic_hash"], "G104B4_REGISTRY_BINDING_INVALID"),
    }
    expected_policy = {
        "allowed_methods": ["GET"],
        "max_pages": 5,
        "max_events": MAX_ACTIVITY_ROWS,
        "clock_skew_seconds": 60,
    }
    if value["transport_policy"] != expected_policy or value["ceiling"] != CEILING:
        raise MetaActivityEvidenceError("G104B4_REQUEST_CEILING_INVALID")
    normalized = {
        "schema_version": REQUEST_VERSION,
        "capture_id": _identifier(value["capture_id"], "G104B4_REQUEST_INVALID"),
        "requested_at": value["requested_at"],
        "window_start_at": value["window_start_at"],
        "data_cutoff_at": value["data_cutoff_at"],
        "graph_api_version": GRAPH_API_VERSION,
        "subject": subject,
        "relevant_fields": relevant,
        "study_contract": dict(study_contract),
        "activity_contract": dict(activity_contract),
        "actor_registry": registry,
        "transport_policy": expected_policy,
        "ceiling": dict(CEILING),
        "request_hash": value["request_hash"],
    }
    expected_hash = hash_json({key: item for key, item in normalized.items() if key != "request_hash"})
    if normalized["request_hash"] != expected_hash:
        raise MetaActivityEvidenceError("G104B4_REQUEST_HASH_INVALID")
    return normalized


def _validate_registry(raw: bytes, expected_sha256: str, request: Mapping[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(raw, bytes)
        or not raw
        or len(raw) > MAX_REGISTRY_BYTES
        or hashlib.sha256(raw).hexdigest()
        != _sha(expected_sha256, "G104B4_REGISTRY_ANCHOR_INVALID")
    ):
        raise MetaActivityEvidenceError("G104B4_REGISTRY_ANCHOR_MISMATCH")
    value = _json_document(raw)
    if not isinstance(value, dict):
        raise MetaActivityEvidenceError("G104B4_REGISTRY_INVALID")
    try:
        normalized = normalize_actor_registry(value, request["actor_registry"]["semantic_hash"])
    except G004ContractError as exc:
        raise MetaActivityEvidenceError("G104B4_REGISTRY_INVALID") from exc
    if request["actor_registry"]["raw_sha256"] != expected_sha256:
        raise MetaActivityEvidenceError("G104B4_REGISTRY_BINDING_MISMATCH")
    return normalized


def _object_specs(subject: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    values = [("CAMPAIGN", str(subject["campaign_id"]), "campaign")]
    for cell in list(subject["cells"]):
        suffix = str(cell["cell_id"])
        values.extend([
            ("ADSET", str(cell["adset_id"]), f"adset_{suffix}"),
            ("AD", str(cell["ad_id"]), f"ad_{suffix}"),
        ])
    return values


def _safe_state(raw: Any, *, object_type: str, object_id: str, subject: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or str(raw.get("id") or "") != object_id:
        raise MetaActivityEvidenceError("G104B4_STATE_READBACK_INVALID")
    account_id = str(raw.get("account_id") or "").removeprefix("act_")
    campaign_id = str(raw.get("campaign_id") or "")
    if account_id != subject["account_id"]:
        raise MetaActivityEvidenceError("G104B4_STATE_READBACK_INVALID")
    if object_type in {"ADSET", "AD"} and campaign_id != subject["campaign_id"]:
        raise MetaActivityEvidenceError("G104B4_STATE_READBACK_INVALID")
    if object_type == "AD":
        expected_adset = next(
            (str(cell["adset_id"]) for cell in subject["cells"] if str(cell["ad_id"]) == object_id),
            "",
        )
        if str(raw.get("adset_id") or "") != expected_adset:
            raise MetaActivityEvidenceError("G104B4_STATE_READBACK_INVALID")
    # The frozen denominator is the configured ``status`` field.  Meta's
    # effective_status is a different projection and must never replace it.
    status_value = str(raw.get("status") or "").upper()
    if status_value not in _STATUS_VALUES:
        status_value = "UNKNOWN"
    updated = str(raw.get("updated_time") or "")
    if updated:
        updated = _source_utc(updated).isoformat()
    return {
        "object_type": object_type,
        "object_id": object_id,
        "field": "status",
        "status": status_value,
        "updated_time": updated or None,
    }


def _edge_rows(value: Any, code: str) -> list[dict[str, Any]]:
    if (
        not isinstance(value, dict)
        or value.get("pagination_complete") is not True
        or value.get("page_count") != 1
        or not isinstance(value.get("data"), list)
    ):
        raise MetaActivityEvidenceError(code)
    rows = value["data"]
    if not all(isinstance(item, dict) for item in rows):
        raise MetaActivityEvidenceError(code)
    return [dict(item) for item in rows]


def _capture_topology(
    client: GetOnlyGraphClient,
    subject: Mapping[str, Any],
    study_contract: Mapping[str, Any],
) -> dict[str, Any]:
    study = client.get(str(subject["study_id"]), fields=STUDY_FIELDS)
    try:
        study_start = _source_utc(study.get("start_time")).isoformat()
        study_end = _source_utc(study.get("end_time")).isoformat()
    except MetaActivityEvidenceError as exc:
        raise MetaActivityEvidenceError("G104B4_TOPOLOGY_INVALID") from exc
    if (
        str(study.get("id") or "") != subject["study_id"]
        or str(study.get("type") or "") != study_contract["study_type"]
        or study_start != study_contract["start_at"]
        or study_end != study_contract["end_at"]
    ):
        raise MetaActivityEvidenceError("G104B4_TOPOLOGY_INVALID")
    study_cells = _edge_rows(
        client.get_edge(f"{subject['study_id']}/cells", fields=STUDY_CELL_FIELDS),
        "G104B4_TOPOLOGY_INVALID",
    )
    study_cell_ids = [item.get("id") for item in study_cells]
    if (
        len(study_cells) != 2
        or any(not isinstance(item, str) for item in study_cell_ids)
        or len(set(study_cell_ids)) != 2
        or set(study_cell_ids) != {str(item["study_cell_id"]) for item in subject["cells"]}
    ):
        raise MetaActivityEvidenceError("G104B4_TOPOLOGY_INVALID")
    bindings: list[dict[str, Any]] = []
    for cell in subject["cells"]:
        adsets = _edge_rows(
            client.get_edge(f"{cell['study_cell_id']}/adsets", fields=CELL_ADSET_FIELDS),
            "G104B4_TOPOLOGY_INVALID",
        )
        campaigns = _edge_rows(
            client.get_edge(f"{cell['study_cell_id']}/campaigns", fields=CELL_CAMPAIGN_FIELDS),
            "G104B4_TOPOLOGY_INVALID",
        )
        if len(adsets) != 1 or len(campaigns) != 1:
            raise MetaActivityEvidenceError("G104B4_TOPOLOGY_INVALID")
        adset = adsets[0]
        campaign = campaigns[0]
        account = str(subject["account_id"])
        if (
            str(adset.get("id") or "") != cell["adset_id"]
            or str(adset.get("campaign_id") or "") != subject["campaign_id"]
            or str(adset.get("account_id") or "").removeprefix("act_") != account
            or str(campaign.get("id") or "") != subject["campaign_id"]
            or str(campaign.get("account_id") or "").removeprefix("act_") != account
        ):
            raise MetaActivityEvidenceError("G104B4_TOPOLOGY_INVALID")
        cell_row = next(
            item for item in study_cells if str(item.get("id") or "") == cell["study_cell_id"]
        )
        ad_ids = cell_row.get("ad_ids")
        if (
            not isinstance(ad_ids, list)
            or len(ad_ids) != 1
            or not isinstance(ad_ids[0], str)
            or ad_ids[0] != str(cell["ad_id"])
        ):
            raise MetaActivityEvidenceError("G104B4_TOPOLOGY_INVALID")
        bindings.append({
            "cell_id": cell["cell_id"],
            "study_cell_id": cell["study_cell_id"],
            "campaign_id": subject["campaign_id"],
            "adset_id": cell["adset_id"],
            "ad_id": cell["ad_id"],
        })
    result = {
        "study_id": subject["study_id"],
        "study_type": study_contract["study_type"],
        "study_start_at": study_contract["start_at"],
        "study_end_at": study_contract["end_at"],
        "cell_bindings": bindings,
        "topology_hash": "",
    }
    result["topology_hash"] = hash_json({key: item for key, item in result.items() if key != "topology_hash"})
    return result


def _status_transition(raw: Mapping[str, Any]) -> tuple[str | None, str | None, str]:
    transitions: list[tuple[str, str]] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 3:
            return
        if isinstance(value, str):
            if len(value.encode("utf-8")) > 65536:
                raise MetaActivityEvidenceError("G104B4_ACTIVITY_ROW_INVALID")
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return
            visit(decoded, depth + 1)
            return
        if isinstance(value, list):
            if len(value) > 128:
                raise MetaActivityEvidenceError("G104B4_ACTIVITY_ROW_INVALID")
            for item in value:
                visit(item, depth + 1)
            return
        if not isinstance(value, dict):
            return
        field = str(value.get("field") or value.get("name") or value.get("attribute") or "").lower()
        before = value.get("old_value", value.get("from"))
        after = value.get("new_value", value.get("to"))
        if before not in (None, "") and after not in (None, "") and field == "status":
            pair = (str(before).upper(), str(after).upper())
            if pair[0] in _STATUS_VALUES and pair[1] in _STATUS_VALUES:
                transitions.append(pair)
        for key in ("status", "changes", "changed_fields", "data"):
            if key in value:
                visit(value[key], depth + 1)

    visit(raw.get("changed_data"))
    visit(raw.get("extra_data"))
    unique = sorted(set(transitions))
    if len(unique) != 1:
        return None, None, "UNCLASSIFIED_OR_CONFLICTING_TRANSITION"
    before, after = unique[0]
    if before == after:
        return before, after, "NO_OP_TRANSITION_OBSERVED"
    return before, after, "EXACT_STATUS_TRANSITION_OBSERVED"


def _safe_activity(
    raw: Any,
    *,
    request: Mapping[str, Any],
    allowed_principals: set[tuple[str, str]],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MetaActivityEvidenceError("G104B4_ACTIVITY_ROW_INVALID")
    activity_id = _identifier(raw.get("id"), "G104B4_ACTIVITY_ROW_INVALID")
    object_id = _identifier(raw.get("object_id"), "G104B4_ACTIVITY_ROW_INVALID")
    denominator = {item["object_id"]: item["object_type"] for item in request["relevant_fields"]}
    if object_id not in denominator:
        raise MetaActivityEvidenceError("G104B4_ACTIVITY_OBJECT_OUT_OF_SCOPE")
    if raw.get("object_type") != denominator[object_id]:
        raise MetaActivityEvidenceError("G104B4_ACTIVITY_OBJECT_TYPE_INVALID")
    event_time_raw = raw.get("event_time")
    local_time_raw = raw.get("date_time_in_timezone")
    if not event_time_raw and not local_time_raw:
        raise MetaActivityEvidenceError("G104B4_SOURCE_TIME_INVALID")
    event_at = _source_utc(event_time_raw or local_time_raw)
    if event_time_raw and local_time_raw and _source_utc(local_time_raw) != event_at:
        raise MetaActivityEvidenceError("G104B4_ACTIVITY_TIME_CONFLICT")
    if not _utc(request["window_start_at"]) <= event_at <= _utc(request["data_cutoff_at"]):
        raise MetaActivityEvidenceError("G104B4_ACTIVITY_TIME_OUT_OF_SCOPE")
    actor_id = str(raw.get("actor_id") or "")
    application_id = str(raw.get("application_id") or "")
    if actor_id and not _ID_RE.fullmatch(actor_id):
        raise MetaActivityEvidenceError("G104B4_ACTIVITY_ROW_INVALID")
    if application_id and not _ID_RE.fullmatch(application_id):
        raise MetaActivityEvidenceError("G104B4_ACTIVITY_ROW_INVALID")
    event_type = str(raw.get("event_type") or "")[:256]
    if event_type in request["activity_contract"]["allowed_event_types"]:
        before, after, transition_status = _status_transition(raw)
    else:
        before, after, transition_status = None, None, "UNCLASSIFIED_OR_CONFLICTING_TRANSITION"
    if actor_id and application_id and (actor_id, application_id) in allowed_principals:
        source_class = "ACTOR_REGISTRY_MATCHED_OBSERVATION"
    elif actor_id or application_id:
        source_class = "EXTERNAL_OR_UNGOVERNED_OBSERVATION"
    else:
        source_class = "UNKNOWN_ACTOR_OBSERVATION"
    value = {
        "activity_id": activity_id,
        "object_type": denominator[object_id],
        "object_id": object_id,
        "field": "status",
        "before": before,
        "after": after,
        "changed_at": event_at.isoformat(),
        "event_type": event_type,
        "actor_id": actor_id or None,
        "application_id": application_id or None,
        "source_class": source_class,
        "transition_status": transition_status,
        "observation_hash": "",
    }
    value["observation_hash"] = hash_json({key: item for key, item in value.items() if key != "observation_hash"})
    return value


class _QueryRecordingSession:
    """Record token-free query commitments while delegating the real GET."""

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.claims: list[dict[str, Any]] = []

    def get(self, url: str, *, params: Mapping[str, Any], timeout: int, allow_redirects: bool) -> Any:
        prefix = f"https://graph.facebook.com/{GRAPH_API_VERSION}/"
        if not isinstance(url, str) or not url.startswith(prefix):
            raise MetaActivityEvidenceError("G104B4_QUERY_CLAIM_INVALID")
        safe_params = {str(key): value for key, value in params.items() if key != "access_token"}
        if "access_token" not in params or any(
            not isinstance(value, (str, int, float, bool)) for value in safe_params.values()
        ):
            raise MetaActivityEvidenceError("G104B4_QUERY_CLAIM_INVALID")
        self.claims.append({
            "method": "GET",
            "endpoint": f"/{GRAPH_API_VERSION}/{url[len(prefix):]}",
            "params": safe_params,
        })
        return self.delegate.get(
            url, params=dict(params), timeout=timeout, allow_redirects=allow_redirects,
        )


def _capture_graph(
    *,
    request: Mapping[str, Any],
    registry: Mapping[str, Any],
    session: Any,
    access_token: str,
    now: datetime,
) -> dict[str, Any]:
    requested_at = _utc(request["requested_at"])
    cutoff_at = _utc(request["data_cutoff_at"])
    if not cutoff_at <= now <= requested_at + timedelta(seconds=60):
        raise MetaActivityEvidenceError("G104B4_CAPTURE_CLOCK_INVALID")
    specs = _object_specs(request["subject"])
    account_path = f"act_{request['subject']['account_id']}"
    allowed_paths = {
        f"{account_path}/activities",
        request["subject"]["study_id"],
        f"{request['subject']['study_id']}/cells",
        *(item[1] for item in specs),
        *(
            path
            for cell in request["subject"]["cells"]
            for path in (
                f"{cell['study_cell_id']}/adsets",
                f"{cell['study_cell_id']}/campaigns",
            )
        ),
    }
    try:
        recording_session = _QueryRecordingSession(session)
        client = GetOnlyGraphClient(
            session=recording_session,
            access_token=access_token,
            now=now,
            allowed_paths=allowed_paths,
            max_pages=5,
            max_items=MAX_ACTIVITY_ROWS,
        )
        first = {
            key: _safe_state(
                client.get(object_id, fields=STATE_FIELDS),
                object_type=object_type,
                object_id=object_id,
                subject=request["subject"],
            )
            for object_type, object_id, key in specs
        }
        topology = _capture_topology(client, request["subject"], request["study_contract"])
        activity_body = client.get_edge(
            f"{account_path}/activities",
            fields=ACTIVITY_FIELDS,
            params={"since": request["window_start_at"], "until": request["data_cutoff_at"]},
        )
        last = {
            key: _safe_state(
                client.get(object_id, fields=STATE_FIELDS),
                object_type=object_type,
                object_id=object_id,
                subject=request["subject"],
            )
            for object_type, object_id, key in specs
        }
    except MetaActivityEvidenceError:
        raise
    except Exception as exc:
        raise MetaActivityEvidenceError("G104B4_GRAPH_CAPTURE_FAILED") from exc
    rows = activity_body.get("data")
    if (
        not isinstance(rows, list)
        or len(rows) > MAX_ACTIVITY_ROWS
        or activity_body.get("pagination_complete") is not True
        or type(activity_body.get("page_count")) is not int
        or not 1 <= activity_body["page_count"] <= 5
    ):
        raise MetaActivityEvidenceError("G104B4_ACTIVITY_PAGINATION_INVALID")
    allowed_principals = {
        (str(item["actor_id"]), str(item["application_id"]))
        for item in registry["principals"]
        if "ACTIVATE" in item["roles"]
    }
    activities = [
        _safe_activity(raw, request=request, allowed_principals=allowed_principals)
        for raw in rows
    ]
    if len({item["activity_id"] for item in activities}) != len(activities):
        raise MetaActivityEvidenceError("G104B4_ACTIVITY_DUPLICATE")
    activities.sort(key=lambda item: (item["changed_at"], item["activity_id"]))
    proof = client.proof()
    if proof.get("allowed_methods") != ["GET"] or any(
        int(proof.get(key) or 0) != 0
        for key in (
            "post_count", "put_count", "patch_count", "delete_count", "redirect_count",
            "batch_count", "async_job_count", "meta_object_writes",
        )
    ):
        raise MetaActivityEvidenceError("G104B4_ZERO_WRITE_INVALID")
    capture = {
        "schema_version": CAPTURE_VERSION,
        "capture_id": request["capture_id"],
        "request_hash": request["request_hash"],
        "captured_at": now.isoformat(),
        "topology": topology,
        "first_states": [first[key] for _object_type, _object_id, key in specs],
        "last_states": [last[key] for _object_type, _object_id, key in specs],
        "activities": activities,
        "pagination": {
            "complete": True,
            "page_count": activity_body["page_count"],
            "row_count": len(activities),
        },
        "query_claim_journal": recording_session.claims,
        "response_journal_claim": [entry.__dict__ for entry in client.journal],
        "get_only_call_claim": proof,
        "capture_hash": "",
    }
    capture["capture_hash"] = hash_json({key: item for key, item in capture.items() if key != "capture_hash"})
    return capture


def _validate_transport_claims(
    capture: Mapping[str, Any], request: Mapping[str, Any], page_count: int,
) -> None:
    specs = _object_specs(request["subject"])
    account_path = f"act_{request['subject']['account_id']}"
    expected: list[tuple[str, str, int]] = [
        (f"/{GRAPH_API_VERSION}/{object_id}", STATE_FIELDS, 1)
        for _object_type, object_id, _key in specs
    ]
    expected.extend([
        (f"/{GRAPH_API_VERSION}/{request['subject']['study_id']}", STUDY_FIELDS, 1),
        (f"/{GRAPH_API_VERSION}/{request['subject']['study_id']}/cells", STUDY_CELL_FIELDS, 1),
    ])
    for cell in request["subject"]["cells"]:
        expected.extend([
            (f"/{GRAPH_API_VERSION}/{cell['study_cell_id']}/adsets", CELL_ADSET_FIELDS, 1),
            (f"/{GRAPH_API_VERSION}/{cell['study_cell_id']}/campaigns", CELL_CAMPAIGN_FIELDS, 1),
        ])
    expected.extend([
        (f"/{GRAPH_API_VERSION}/{account_path}/activities", ACTIVITY_FIELDS, page)
        for page in range(1, page_count + 1)
    ])
    expected.extend([
        (f"/{GRAPH_API_VERSION}/{object_id}", STATE_FIELDS, 1)
        for _object_type, object_id, _key in specs
    ])
    journal = capture["response_journal_claim"]
    query_claims = capture["query_claim_journal"]
    if not isinstance(journal, list) or len(journal) != len(expected):
        raise MetaActivityEvidenceError("G104B4_TRANSPORT_JOURNAL_INVALID")
    if not isinstance(query_claims, list) or len(query_claims) != len(expected):
        raise MetaActivityEvidenceError("G104B4_QUERY_CLAIM_INVALID")
    activity_after_values: list[str] = []
    activity_endpoint = f"/{GRAPH_API_VERSION}/{account_path}/activities"
    for item, query, (endpoint, fields, page) in zip(journal, query_claims, expected):
        if not isinstance(item, dict) or set(item) != {
            "endpoint", "fields", "page", "http_status", "response_hash",
            "response_size", "observed_at",
        }:
            raise MetaActivityEvidenceError("G104B4_TRANSPORT_JOURNAL_INVALID")
        if not isinstance(query, dict) or set(query) != {"method", "endpoint", "params"}:
            raise MetaActivityEvidenceError("G104B4_QUERY_CLAIM_INVALID")
        params = query["params"]
        if query["method"] != "GET" or query["endpoint"] != endpoint or not isinstance(params, dict):
            raise MetaActivityEvidenceError("G104B4_QUERY_CLAIM_INVALID")
        expected_params: dict[str, Any] = {"fields": fields}
        if endpoint.endswith(("/cells", "/adsets", "/campaigns")):
            expected_params["limit"] = 100
        if endpoint == activity_endpoint:
            expected_params.update({
                "limit": 100,
                "since": request["window_start_at"],
                "until": request["data_cutoff_at"],
            })
            if page > 1:
                after = params.get("after")
                if not isinstance(after, str) or not after or len(after) > 2048:
                    raise MetaActivityEvidenceError("G104B4_QUERY_CLAIM_INVALID")
                activity_after_values.append(after)
                expected_params["after"] = after
        if params != expected_params:
            raise MetaActivityEvidenceError("G104B4_QUERY_CLAIM_INVALID")
        if (
            item["endpoint"] != endpoint
            or item["fields"] != fields
            or type(item["page"]) is not int
            or item["page"] != page
            or item["http_status"] != 200
            or type(item["response_size"]) is not int
            or not 0 < item["response_size"] <= 8 * 1024 * 1024
            or not _SHA_RE.fullmatch(str(item["response_hash"] or ""))
            or item["observed_at"] != capture["captured_at"]
        ):
            raise MetaActivityEvidenceError("G104B4_TRANSPORT_JOURNAL_INVALID")
    if len(activity_after_values) != len(set(activity_after_values)):
        raise MetaActivityEvidenceError("G104B4_QUERY_CLAIM_INVALID")


def _validate_capture(
    capture: Any, request: Mapping[str, Any], registry: Mapping[str, Any],
) -> dict[str, Any]:
    keys = {
        "schema_version", "capture_id", "request_hash", "captured_at", "topology", "first_states",
        "last_states", "activities", "pagination", "query_claim_journal",
        "response_journal_claim", "get_only_call_claim", "capture_hash",
    }
    if not isinstance(capture, dict) or set(capture) != keys or capture["schema_version"] != CAPTURE_VERSION:
        raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
    if capture["capture_id"] != request["capture_id"] or capture["request_hash"] != request["request_hash"]:
        raise MetaActivityEvidenceError("G104B4_CAPTURE_BINDING_INVALID")
    captured_at = _utc(capture["captured_at"])
    if not _utc(request["data_cutoff_at"]) <= captured_at <= (
        _utc(request["requested_at"]) + timedelta(seconds=60)
    ):
        raise MetaActivityEvidenceError("G104B4_CAPTURE_CLOCK_INVALID")
    specs = _object_specs(request["subject"])
    expected_topology = {
        "study_id": request["subject"]["study_id"],
        "study_type": request["study_contract"]["study_type"],
        "study_start_at": request["study_contract"]["start_at"],
        "study_end_at": request["study_contract"]["end_at"],
        "cell_bindings": [{
            "cell_id": cell["cell_id"],
            "study_cell_id": cell["study_cell_id"],
            "campaign_id": request["subject"]["campaign_id"],
            "adset_id": cell["adset_id"],
            "ad_id": cell["ad_id"],
        } for cell in request["subject"]["cells"]],
        "topology_hash": "",
    }
    expected_topology["topology_hash"] = hash_json({
        key: item for key, item in expected_topology.items() if key != "topology_hash"
    })
    if capture["topology"] != expected_topology:
        raise MetaActivityEvidenceError("G104B4_TOPOLOGY_INVALID")
    expected_pairs = [(item[0], item[1]) for item in specs]
    for name in ("first_states", "last_states"):
        values = capture[name]
        if not isinstance(values, list) or len(values) != 5:
            raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
        if [(item.get("object_type"), item.get("object_id")) for item in values] != expected_pairs:
            raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
        for item in values:
            if not isinstance(item, dict) or set(item) != {
                "object_type", "object_id", "field", "status", "updated_time",
            } or item["field"] != "status" or item["status"] not in {*_STATUS_VALUES, "UNKNOWN"}:
                raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
            if item["updated_time"] is not None:
                if _utc(item["updated_time"]) > captured_at:
                    raise MetaActivityEvidenceError("G104B4_CAPTURE_TIME_INVALID")
    for first_item, last_item in zip(capture["first_states"], capture["last_states"]):
        if (
            first_item["updated_time"] is not None
            and last_item["updated_time"] is not None
            and _utc(first_item["updated_time"]) > _utc(last_item["updated_time"])
        ):
            raise MetaActivityEvidenceError("G104B4_CAPTURE_TIME_INVALID")
    activities = capture["activities"]
    if not isinstance(activities, list) or len(activities) > MAX_ACTIVITY_ROWS:
        raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
    if len({item.get("activity_id") for item in activities if isinstance(item, dict)}) != len(activities):
        raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
    allowed_objects = {(item["object_type"], item["object_id"]) for item in request["relevant_fields"]}
    allowed_principals = {
        (str(item["actor_id"]), str(item["application_id"]))
        for item in registry["principals"]
        if "ACTIVATE" in item["roles"]
    }
    for item in activities:
        if not isinstance(item, dict) or set(item) != {
            "activity_id", "object_type", "object_id", "field", "before", "after",
            "changed_at", "event_type", "actor_id", "application_id", "source_class",
            "transition_status", "observation_hash",
        }:
            raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
        if (item["object_type"], item["object_id"]) not in allowed_objects or item["field"] != "status":
            raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
        changed_at = _utc(item["changed_at"])
        if not _utc(request["window_start_at"]) <= changed_at <= _utc(request["data_cutoff_at"]):
            raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
        for identity_value in (item["actor_id"], item["application_id"]):
            if identity_value is not None and (
                not isinstance(identity_value, str) or not _ID_RE.fullmatch(identity_value)
            ):
                raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
        identity = (str(item["actor_id"] or ""), str(item["application_id"] or ""))
        if all(identity):
            expected_source = (
                "ACTOR_REGISTRY_MATCHED_OBSERVATION"
                if identity in allowed_principals else "EXTERNAL_OR_UNGOVERNED_OBSERVATION"
            )
        elif any(identity):
            expected_source = "EXTERNAL_OR_UNGOVERNED_OBSERVATION"
        else:
            expected_source = "UNKNOWN_ACTOR_OBSERVATION"
        if item["source_class"] != expected_source:
            raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
        transition = item["transition_status"]
        event_type = item["event_type"]
        if (
            not isinstance(event_type, str)
            or not event_type
            or len(event_type.encode("utf-8")) > 256
        ):
            raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
        if transition not in {
            "EXACT_STATUS_TRANSITION_OBSERVED",
            "NO_OP_TRANSITION_OBSERVED",
            "UNCLASSIFIED_OR_CONFLICTING_TRANSITION",
        }:
            raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
        if transition == "EXACT_STATUS_TRANSITION_OBSERVED" and (
            item["before"] not in _STATUS_VALUES
            or item["after"] not in _STATUS_VALUES
            or item["before"] == item["after"]
        ):
            raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
        if transition == "NO_OP_TRANSITION_OBSERVED" and (
            item["before"] not in _STATUS_VALUES or item["before"] != item["after"]
        ):
            raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
        if transition == "UNCLASSIFIED_OR_CONFLICTING_TRANSITION" and (
            item["before"] is not None or item["after"] is not None
        ):
            raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
        allowed_event = event_type in request["activity_contract"]["allowed_event_types"]
        if not allowed_event and transition != "UNCLASSIFIED_OR_CONFLICTING_TRANSITION":
            raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
        expected = hash_json({key: value for key, value in item.items() if key != "observation_hash"})
        if item["observation_hash"] != expected:
            raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
    pagination = capture["pagination"]
    proof = capture["get_only_call_claim"]
    journal = capture["response_journal_claim"]
    proof_keys = {
        "allowed_methods", "get_count", "post_count", "put_count", "patch_count",
        "delete_count", "redirect_count", "batch_count", "async_job_count",
        "meta_object_writes", "request_journal_hash",
    }
    if (
        pagination != {
            "complete": True,
            "page_count": pagination.get("page_count") if isinstance(pagination, dict) else None,
            "row_count": len(activities),
        }
        or type(pagination.get("row_count")) is not int
        or type(pagination.get("page_count")) is not int
        or not 1 <= pagination["page_count"] <= 5
        or not isinstance(proof, dict)
        or set(proof) != proof_keys
        or proof.get("allowed_methods") != ["GET"]
        or not isinstance(journal, list)
        or len(journal) != proof.get("get_count")
        or proof.get("request_journal_hash") != hash_json(journal)
        or any(int(proof.get(key) or 0) != 0 for key in (
            "post_count", "put_count", "patch_count", "delete_count", "redirect_count",
            "batch_count", "async_job_count", "meta_object_writes",
        ))
    ):
        raise MetaActivityEvidenceError("G104B4_TRANSPORT_PROOF_INVALID")
    _validate_transport_claims(capture, request, pagination["page_count"])
    if activities != sorted(activities, key=lambda item: (item["changed_at"], item["activity_id"])):
        raise MetaActivityEvidenceError("G104B4_CAPTURE_INVALID")
    expected_hash = hash_json({key: item for key, item in capture.items() if key != "capture_hash"})
    if capture["capture_hash"] != expected_hash:
        raise MetaActivityEvidenceError("G104B4_CAPTURE_HASH_INVALID")
    return capture


def _derive_from_capture(
    request: Mapping[str, Any], capture: Mapping[str, Any], registry: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    _validate_capture(capture, request, registry)
    first = {(item["object_type"], item["object_id"]): item for item in capture["first_states"]}
    last = {(item["object_type"], item["object_id"]): item for item in capture["last_states"]}
    readbacks: list[dict[str, Any]] = []
    gaps: set[str] = {
        "ACTUAL_BEFORE_NOT_INDEPENDENTLY_VERIFIED",
        "ACTOR_REGISTRY_SELECTION_AUTHORITY_NOT_VERIFIED",
        "CURRENT_STATE_READBACK_NOT_ASOF_CUTOFF",
        "LIVE_GRAPH_TRANSPORT_NOT_EXTERNALLY_ATTESTED",
        "RETENTION_LOWER_BOUND_NOT_PROVIDED",
        "SOURCE_CONTENT_AUTHORITY_NOT_VERIFIED",
    }
    for denominator in request["relevant_fields"]:
        key = (denominator["object_type"], denominator["object_id"])
        before = first[key]
        after = last[key]
        stable = before["status"] == after["status"] and before["status"] != "UNKNOWN"
        if not stable:
            gaps.add("CURRENT_STATE_DRIFT_OR_UNKNOWN")
        if after["updated_time"] and _utc(after["updated_time"]) > _utc(request["data_cutoff_at"]):
            gaps.add("CURRENT_STATE_UPDATED_AFTER_CUTOFF")
        value = {
            "object_type": key[0],
            "object_id": key[1],
            "field": "status",
            "first_observed_status": before["status"],
            "last_observed_status": after["status"],
            "first_updated_time": before["updated_time"],
            "last_updated_time": after["updated_time"],
            "stable_during_capture": stable,
            "readback_hash": "",
        }
        value["readback_hash"] = hash_json({key: item for key, item in value.items() if key != "readback_hash"})
        readbacks.append(value)
    observations = list(capture["activities"])
    exact_by_object: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in observations:
        if item["transition_status"] in {
            "EXACT_STATUS_TRANSITION_OBSERVED", "NO_OP_TRANSITION_OBSERVED",
        }:
            exact_by_object.setdefault((item["object_type"], item["object_id"]), []).append(item)
    for rows in exact_by_object.values():
        ordered = sorted(rows, key=lambda item: (item["changed_at"], item["activity_id"]))
        if len({item["changed_at"] for item in ordered}) != len(ordered) or any(
            previous["after"] != current["before"]
            for previous, current in zip(ordered, ordered[1:])
        ):
            gaps.add("ACTIVITY_TRANSITION_SEQUENCE_CONFLICT")
    external = [item for item in observations if item["source_class"] == "EXTERNAL_OR_UNGOVERNED_OBSERVATION"]
    unknown = [item for item in observations if item["source_class"] == "UNKNOWN_ACTOR_OBSERVATION"]
    unclassified = [item for item in observations if item["transition_status"] != "EXACT_STATUS_TRANSITION_OBSERVED"]
    if unknown:
        gaps.add("ACTOR_PROVENANCE_UNKNOWN")
    if unclassified:
        gaps.add("ACTIVITY_TRANSITION_UNCLASSIFIED_OR_NO_OP")
    conflict = False
    for object_id in {item["object_id"] for item in observations}:
        exact = [
            item for item in observations
            if item["object_id"] == object_id
            and item["transition_status"] == "EXACT_STATUS_TRANSITION_OBSERVED"
        ]
        by_time: dict[str, set[tuple[str, str]]] = {}
        for item in exact:
            by_time.setdefault(item["changed_at"], set()).add((item["before"], item["after"]))
        if any(len(values) > 1 for values in by_time.values()):
            conflict = True
            gaps.add("CONFLICTING_ACTIVITY_EVENTS")
        if exact:
            latest = sorted(exact, key=lambda item: (item["changed_at"], item["activity_id"]))[-1]
            key = (latest["object_type"], latest["object_id"])
            if last[key]["status"] != latest["after"]:
                conflict = True
                gaps.add("ACTIVITY_CURRENT_STATE_CONFLICT")
    if external:
        status = "POLLUTED_EXTERNAL_OR_UNGOVERNED_ACTIVITY_CLAIM"
        gaps.add("EXTERNAL_OR_UNGOVERNED_ACTIVITY_OBSERVED")
    elif (
        conflict
        or any(not item["stable_during_capture"] for item in readbacks)
        or unknown
        or unclassified
        or "ACTIVITY_TRANSITION_SEQUENCE_CONFLICT" in gaps
    ):
        status = "INCOMPLETE_CALLER_ANCHORED_ACTIVITY_OR_STATE_CLAIM"
    else:
        status = "CALLER_ANCHORED_GET_CAPTURE_CLAIM_REDERIVED"
    activity_bundle = {
        "schema_version": ACTIVITY_VERSION,
        "capture_id": request["capture_id"],
        "request_hash": request["request_hash"],
        "capture_hash": capture["capture_hash"],
        "observations": observations,
        "observation_root": hash_json([item["observation_hash"] for item in observations]),
        "activity_hash": "",
    }
    activity_bundle["activity_hash"] = hash_json({
        key: item for key, item in activity_bundle.items() if key != "activity_hash"
    })
    readback_bundle = {
        "schema_version": READBACK_VERSION,
        "capture_id": request["capture_id"],
        "request_hash": request["request_hash"],
        "capture_hash": capture["capture_hash"],
        "readbacks": readbacks,
        "readback_root": hash_json([item["readback_hash"] for item in readbacks]),
        "readbacks_hash": "",
    }
    readback_bundle["readbacks_hash"] = hash_json({
        key: item for key, item in readback_bundle.items() if key != "readbacks_hash"
    })
    coverage = {
        "schema_version": COVERAGE_VERSION,
        "capture_id": request["capture_id"],
        "request_hash": request["request_hash"],
        "capture_hash": capture["capture_hash"],
        "status": status,
        "window_start_at": request["window_start_at"],
        "data_cutoff_at": request["data_cutoff_at"],
        "denominator_fields": len(request["relevant_fields"]),
        "stable_current_state_fields": sum(item["stable_during_capture"] for item in readbacks),
        "observed_activity_rows": len(observations),
        "registry_matched_rows": sum(
            item["source_class"] == "ACTOR_REGISTRY_MATCHED_OBSERVATION" for item in observations
        ),
        "external_or_ungoverned_rows": len(external),
        "unknown_actor_rows": len(unknown),
        "pagination_complete": True,
        "capture_window_query_claim_complete": True,
        "live_graph_transport_attested": False,
        "retention_coverage": "UNKNOWN",
        "complete_event_journal": False,
        "reason_codes": sorted(gaps),
        "ceiling": dict(CEILING),
        "coverage_hash": "",
    }
    coverage["coverage_hash"] = hash_json({key: item for key, item in coverage.items() if key != "coverage_hash"})
    return activity_bundle, readback_bundle, coverage


def capture_meta_activity_evidence(
    request_raw: bytes,
    *,
    expected_request_sha256: str,
    actor_registry_raw: bytes,
    expected_actor_registry_sha256: str,
    session: Any,
    access_token: str,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    request = validate_request(request_raw, expected_request_sha256)
    registry = _validate_registry(actor_registry_raw, expected_actor_registry_sha256, request)
    if not access_token:
        raise MetaActivityEvidenceError("G104B4_TOKEN_MISSING")
    if now.tzinfo is None or now.utcoffset() is None:
        raise MetaActivityEvidenceError("G104B4_CAPTURE_CLOCK_INVALID")
    normalized_now = now.astimezone(timezone.utc)
    capture = _capture_graph(
        request=request,
        registry=registry,
        session=session,
        access_token=access_token,
        now=normalized_now,
    )
    activity, readbacks, coverage = _derive_from_capture(request, capture, registry)
    return request, capture, activity, readbacks, coverage


def _manifest(
    request: Mapping[str, Any],
    capture: Mapping[str, Any],
    activity: Mapping[str, Any],
    readbacks: Mapping[str, Any],
    coverage: Mapping[str, Any],
    payloads: Mapping[str, bytes],
) -> dict[str, Any]:
    value = {
        "schema_version": MANIFEST_VERSION,
        "capture_id": request["capture_id"],
        "request_hash": request["request_hash"],
        "capture_hash": capture["capture_hash"],
        "activity_hash": activity["activity_hash"],
        "readbacks_hash": readbacks["readbacks_hash"],
        "coverage_hash": coverage["coverage_hash"],
        "status": coverage["status"],
        "ceiling": dict(CEILING),
        "files": {
            name: {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
            for name, raw in sorted(payloads.items())
        },
        "manifest_hash": "",
    }
    value["manifest_hash"] = hash_json({key: item for key, item in value.items() if key != "manifest_hash"})
    return value


def _require_dir_identity(parent_fd: int, name: str, opened_fd: int) -> None:
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    opened = os.fstat(opened_fd)
    if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
        raise MetaActivityEvidenceError("G104B4_OUTPUT_IDENTITY_DRIFT")


def _write_file(directory_fd: int, name: str, raw: bytes) -> None:
    if not raw or len(raw) > MAX_ARTIFACT_FILE_BYTES:
        raise MetaActivityEvidenceError("G104B4_OUTPUT_TOO_LARGE")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        written = 0
        while written < len(raw):
            count = os.write(fd, raw[written:])
            if count <= 0:
                raise MetaActivityEvidenceError("G104B4_OUTPUT_WRITE_FAILED")
            written += count
        os.fsync(fd)
        value = os.fstat(fd)
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or stat.S_IMODE(value.st_mode) != 0o600:
            raise MetaActivityEvidenceError("G104B4_OUTPUT_WRITE_FAILED")
    finally:
        os.close(fd)


def _write_directory(root: Path, payloads: Mapping[str, bytes]) -> None:
    if set(payloads) != EXACT_ARTIFACT_FILES:
        raise MetaActivityEvidenceError("G104B4_OUTPUT_FILE_SET_INVALID")
    parent = root.parent.resolve(strict=True)
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_flags = parent_flags
    parent_fd = os.open(parent, parent_flags)
    root_fd = -1
    complete = False
    try:
        parent_before = os.fstat(parent_fd)
        try:
            os.mkdir(root.name, 0o700, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise MetaActivityEvidenceError("G104B4_OUTPUT_EXISTS") from exc
        root_fd = os.open(root.name, root_flags, dir_fd=parent_fd)
        _require_dir_identity(parent_fd, root.name, root_fd)
        if stat.S_IMODE(os.fstat(root_fd).st_mode) != 0o700:
            raise MetaActivityEvidenceError("G104B4_OUTPUT_MODE_INVALID")
        write_order = sorted(EXACT_ARTIFACT_FILES - {"manifest.json"}) + ["manifest.json"]
        for name in write_order:
            _write_file(root_fd, name, payloads[name])
        if set(os.listdir(root_fd)) != EXACT_ARTIFACT_FILES:
            raise MetaActivityEvidenceError("G104B4_OUTPUT_FILE_SET_INVALID")
        os.fsync(root_fd)
        _require_dir_identity(parent_fd, root.name, root_fd)
        complete = True
        os.fsync(parent_fd)
        if _dir_identity(parent_before) == _dir_identity(os.fstat(parent_fd)):
            return
        # ctime/mtime necessarily change because this call created a child; identity must not.
        after = os.fstat(parent_fd)
        if (parent_before.st_dev, parent_before.st_ino, stat.S_IMODE(parent_before.st_mode)) != (
            after.st_dev, after.st_ino, stat.S_IMODE(after.st_mode),
        ):
            raise MetaActivityEvidenceError("G104B4_OUTPUT_IDENTITY_DRIFT")
    except Exception:
        if complete:
            raise MetaActivityEvidenceError("G104B4_OUTPUT_DURABILITY_UNCERTAIN")
        if root_fd >= 0:
            for name in sorted(payloads):
                try:
                    os.unlink(name, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
            try:
                _require_dir_identity(parent_fd, root.name, root_fd)
                os.rmdir(root.name, dir_fd=parent_fd)
            except (FileNotFoundError, OSError, MetaActivityEvidenceError):
                pass
        raise
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(parent_fd)


def _read_directory(root: Path) -> dict[str, bytes]:
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    parent_fd = -1
    root_fd = -1
    try:
        parent_fd = os.open(root.parent.resolve(strict=True), parent_flags)
        parent_before = os.fstat(parent_fd)
        root_fd = os.open(root.name, parent_flags, dir_fd=parent_fd)
        root_before = os.fstat(root_fd)
        if not stat.S_ISDIR(root_before.st_mode) or stat.S_IMODE(root_before.st_mode) != 0o700:
            raise MetaActivityEvidenceError("G104B4_ARTIFACT_DIRECTORY_INVALID")
        _require_dir_identity(parent_fd, root.name, root_fd)
        if set(os.listdir(root_fd)) != EXACT_ARTIFACT_FILES:
            raise MetaActivityEvidenceError("G104B4_ARTIFACT_FILE_SET_INVALID")
        result: dict[str, bytes] = {}
        for name in sorted(EXACT_ARTIFACT_FILES):
            fd = os.open(name, file_flags, dir_fd=root_fd)
            try:
                before = os.fstat(fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_size <= 0
                    or before.st_size > MAX_ARTIFACT_FILE_BYTES
                ):
                    raise MetaActivityEvidenceError("G104B4_ARTIFACT_FILE_INVALID")
                raw = _read_fd(fd, MAX_ARTIFACT_FILE_BYTES, "G104B4_ARTIFACT_FILE_INVALID")
                after = os.fstat(fd)
                named = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if _file_identity(before) != _file_identity(after) or (
                    named.st_dev, named.st_ino
                ) != (after.st_dev, after.st_ino):
                    raise MetaActivityEvidenceError("G104B4_ARTIFACT_IDENTITY_DRIFT")
                result[name] = raw
            finally:
                os.close(fd)
        if set(os.listdir(root_fd)) != EXACT_ARTIFACT_FILES:
            raise MetaActivityEvidenceError("G104B4_ARTIFACT_FILE_SET_INVALID")
        _require_dir_identity(parent_fd, root.name, root_fd)
        if _file_identity(root_before) != _file_identity(os.fstat(root_fd)):
            raise MetaActivityEvidenceError("G104B4_ARTIFACT_IDENTITY_DRIFT")
        parent_after = os.fstat(parent_fd)
        if (parent_before.st_dev, parent_before.st_ino, stat.S_IMODE(parent_before.st_mode)) != (
            parent_after.st_dev, parent_after.st_ino, stat.S_IMODE(parent_after.st_mode),
        ):
            raise MetaActivityEvidenceError("G104B4_ARTIFACT_IDENTITY_DRIFT")
        return result
    except OSError as exc:
        raise MetaActivityEvidenceError("G104B4_ARTIFACT_DIRECTORY_INVALID") from exc
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def write_meta_activity_evidence_artifact(
    output_dir: str | Path,
    *,
    request_raw: bytes,
    expected_request_sha256: str,
    actor_registry_raw: bytes,
    expected_actor_registry_sha256: str,
    session: Any,
    access_token: str,
    now: datetime,
) -> dict[str, Any]:
    request, capture, activity, readbacks, coverage = capture_meta_activity_evidence(
        request_raw,
        expected_request_sha256=expected_request_sha256,
        actor_registry_raw=actor_registry_raw,
        expected_actor_registry_sha256=expected_actor_registry_sha256,
        session=session,
        access_token=access_token,
        now=now,
    )
    payloads = {
        "source-request.json": bytes(request_raw),
        "graph-capture.json": _json_bytes(capture),
        "activity-observations.json": _json_bytes(activity),
        "current-state-readbacks.json": _json_bytes(readbacks),
        "coverage.json": _json_bytes(coverage),
    }
    manifest = _manifest(request, capture, activity, readbacks, coverage, payloads)
    payloads["manifest.json"] = _json_bytes(manifest)
    _write_directory(Path(output_dir), payloads)
    loaded = load_validated_meta_activity_evidence_directory(
        output_dir,
        expected_manifest_sha256=hashlib.sha256(payloads["manifest.json"]).hexdigest(),
        expected_request_sha256=expected_request_sha256,
        actor_registry_raw=actor_registry_raw,
        expected_actor_registry_sha256=expected_actor_registry_sha256,
    )
    if loaded["manifest"] != manifest:
        raise MetaActivityEvidenceError("G104B4_OUTPUT_READBACK_INVALID")
    return {
        "manifest": manifest,
        "manifest_sha256": hashlib.sha256(payloads["manifest.json"]).hexdigest(),
    }


def load_validated_meta_activity_evidence_directory(
    artifact_dir: str | Path,
    *,
    expected_manifest_sha256: str,
    expected_request_sha256: str,
    actor_registry_raw: bytes,
    expected_actor_registry_sha256: str,
) -> dict[str, Any]:
    expected_manifest = _sha(expected_manifest_sha256, "G104B4_MANIFEST_ANCHOR_INVALID")
    raw = _read_directory(Path(artifact_dir))
    if hashlib.sha256(raw["manifest.json"]).hexdigest() != expected_manifest:
        raise MetaActivityEvidenceError("G104B4_MANIFEST_ANCHOR_MISMATCH")
    request = validate_request(raw["source-request.json"], expected_request_sha256)
    registry = _validate_registry(actor_registry_raw, expected_actor_registry_sha256, request)
    capture = _json_document(raw["graph-capture.json"])
    activity = _json_document(raw["activity-observations.json"])
    readbacks = _json_document(raw["current-state-readbacks.json"])
    coverage = _json_document(raw["coverage.json"])
    manifest = _json_document(raw["manifest.json"])
    capture = _validate_capture(capture, request, registry)
    expected_activity, expected_readbacks, expected_coverage = _derive_from_capture(
        request, capture, registry,
    )
    if activity != expected_activity or readbacks != expected_readbacks or coverage != expected_coverage:
        raise MetaActivityEvidenceError("G104B4_ARTIFACT_REDERIVATION_MISMATCH")
    payloads = {name: raw[name] for name in EXACT_ARTIFACT_FILES - {"manifest.json"}}
    expected_manifest_value = _manifest(
        request, capture, expected_activity, expected_readbacks, expected_coverage, payloads,
    )
    if manifest != expected_manifest_value:
        raise MetaActivityEvidenceError("G104B4_MANIFEST_INVALID")
    return {
        "request": request,
        "capture": capture,
        "activity": expected_activity,
        "readbacks": expected_readbacks,
        "coverage": expected_coverage,
        "manifest": expected_manifest_value,
    }


__all__ = [
    "CEILING",
    "MetaActivityEvidenceError",
    "capture_meta_activity_evidence",
    "canonical_json",
    "hash_json",
    "load_validated_meta_activity_evidence_directory",
    "read_external_json",
    "validate_request",
    "write_meta_activity_evidence_artifact",
]
