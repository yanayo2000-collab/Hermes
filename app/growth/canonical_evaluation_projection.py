from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from app.growth.canonical_evaluation_contracts import canonical_hash, canonical_json


PROJECTION_VERSION = "gle-g1-legacy-evaluation-projection-v1"
LEGACY_CALENDAR_MAP = {"D1": "SAFETY_CHECK", "D3": "TREND_ONLY", "D7": "BINDING_EFFECT_DECISION"}
KINDS = frozenset({"SINGLE_EXPERIMENT", "CREATIVE_GROUP", "AUDIENCE_PAIR"})


class LegacyProjectionError(ValueError):
    pass


def project_legacy_evaluation(kind: str, row: Mapping[str, Any]) -> dict[str, Any]:
    if kind not in KINDS:
        raise LegacyProjectionError("G101_LEGACY_KIND_INVALID")
    source = dict(row)
    if kind == "SINGLE_EXPERIMENT":
        source_id = _required(source, "evaluation_id")
        subjects = [_required(source, "experiment_id")]
        status = _required(source, "evaluation_status")
        evidence = {
            "baseline_window": _json_field(source, "baseline_window_json", dict),
            "post_window": _json_field(source, "post_window_json", dict),
            "baseline_metrics": _json_field(source, "baseline_metrics_json", dict),
            "post_metrics": _json_field(source, "post_metrics_json", dict),
            "data_quality_status": _text_field(source, "data_quality_status"),
            "dedupe_version": _text_field(source, "dedupe_version"),
            "attribution_version": _text_field(source, "attribution_version"),
        }
    elif kind == "CREATIVE_GROUP":
        source_id = _required(source, "group_evaluation_id")
        metrics = _json_field(source, "metrics_by_experiment_json", dict)
        subjects = sorted(str(key) for key in (metrics["value"] or {}) if str(key))
        status = _required(source, "decision_status")
        evidence = {
            "launch_id": _text_field(source, "launch_id"),
            "window": _json_field(source, "window_json", dict),
            "metrics_by_experiment": metrics,
            "ranking": _json_field(source, "ranking_json", list),
            "winner_experiment_id": _text_field(source, "winner_experiment_id"),
            "data_quality_status": _text_field(source, "data_quality_status"),
            "legacy_evidence": _json_field(source, "evidence_json", dict),
        }
    else:
        source_id = _required(source, "pair_evaluation_id")
        subjects = sorted([_required(source, "baseline_experiment_id"), _required(source, "challenger_experiment_id")])
        status = _required(source, "decision_status")
        evidence = {
            "launch_id": _text_field(source, "launch_id"),
            "metrics": _json_field(source, "metrics_json", dict),
            "winner_experiment_id": _text_field(source, "winner_experiment_id"),
            "legacy_evidence": _json_field(source, "evidence_json", dict),
        }
    if not subjects or len(subjects) != len(set(subjects)):
        raise LegacyProjectionError("G101_LEGACY_SUBJECT_INVALID")
    checkpoint = _required(source, "checkpoint")
    if checkpoint not in LEGACY_CALENDAR_MAP:
        raise LegacyProjectionError("G101_LEGACY_CHECKPOINT_INVALID")
    missing = sorted(_missing_paths(evidence))
    reasons = ["LEGACY_CALENDAR_CHECKPOINT", "LEGACY_INPUT_SNAPSHOT_MISSING", "LINEAGE_UNRESOLVED"]
    if missing:
        reasons.append("LEGACY_FIELDS_MISSING")
    projection = {
        "schema_version": PROJECTION_VERSION,
        "source_kind": kind,
        "source_id": source_id,
        "subject_experiment_ids": subjects,
        "legacy_checkpoint": checkpoint,
        "checkpoint_role_hint": LEGACY_CALENDAR_MAP[checkpoint],
        "legacy_status": status,
        "evidence": evidence,
        "missing_fields": missing,
        "evaluated_at": _legacy_utc(_required(source, "evaluated_at")),
        "lineage_status": "UNRESOLVED",
        "split": "UNASSIGNED",
        "binding_eligible": False,
        "causal_classification": "OBSERVATIONAL_ONLY",
        "reason_codes": sorted(reasons),
    }
    projection["projection_hash"] = canonical_hash(projection)
    return validate_legacy_projection(projection)


def validate_legacy_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version", "source_kind", "source_id", "subject_experiment_ids", "legacy_checkpoint",
        "checkpoint_role_hint", "legacy_status", "evidence", "missing_fields", "evaluated_at", "lineage_status",
        "split", "binding_eligible", "causal_classification", "reason_codes", "projection_hash",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise LegacyProjectionError("G101_PROJECTION_SCHEMA_INVALID")
    body = dict(value)
    if body["schema_version"] != PROJECTION_VERSION or body["source_kind"] not in KINDS:
        raise LegacyProjectionError("G101_PROJECTION_VERSION_INVALID")
    _required(body, "source_id")
    subjects = body["subject_experiment_ids"]
    if not isinstance(subjects, list) or not subjects or subjects != sorted(set(subjects)):
        raise LegacyProjectionError("G101_LEGACY_SUBJECT_INVALID")
    if any(not isinstance(item, str) or not item or item != item.strip() for item in subjects):
        raise LegacyProjectionError("G101_LEGACY_SUBJECT_INVALID")
    if body["source_kind"] == "SINGLE_EXPERIMENT" and len(subjects) != 1:
        raise LegacyProjectionError("G101_LEGACY_SUBJECT_INVALID")
    if body["source_kind"] == "AUDIENCE_PAIR" and len(subjects) != 2:
        raise LegacyProjectionError("G101_LEGACY_SUBJECT_INVALID")
    if body["source_kind"] == "CREATIVE_GROUP" and not 2 <= len(subjects) <= 4:
        raise LegacyProjectionError("G101_LEGACY_SUBJECT_INVALID")
    if body["legacy_checkpoint"] not in LEGACY_CALENDAR_MAP or body["checkpoint_role_hint"] != LEGACY_CALENDAR_MAP[body["legacy_checkpoint"]]:
        raise LegacyProjectionError("G101_PROJECTION_CHECKPOINT_INVALID")
    _required(body, "legacy_status")
    if body["lineage_status"] != "UNRESOLVED" or body["split"] != "UNASSIGNED" or body["binding_eligible"] is not False:
        raise LegacyProjectionError("G101_PROJECTION_BINDING_INVALID")
    if body["causal_classification"] != "OBSERVATIONAL_ONLY":
        raise LegacyProjectionError("G101_PROJECTION_CAUSAL_INVALID")
    _validate_evidence(body["source_kind"], body["evidence"], subjects)
    if not isinstance(body["missing_fields"], list) or body["missing_fields"] != sorted(set(body["missing_fields"])):
        raise LegacyProjectionError("G101_PROJECTION_MISSING_FIELDS_INVALID")
    if body["missing_fields"] != sorted(_missing_paths(body["evidence"])):
        raise LegacyProjectionError("G101_PROJECTION_MISSING_FIELDS_INVALID")
    expected_reasons = ["LEGACY_CALENDAR_CHECKPOINT", "LEGACY_INPUT_SNAPSHOT_MISSING", "LINEAGE_UNRESOLVED"]
    if body["missing_fields"]:
        expected_reasons.append("LEGACY_FIELDS_MISSING")
    if body["reason_codes"] != sorted(expected_reasons):
        raise LegacyProjectionError("G101_PROJECTION_REASONS_INVALID")
    if _legacy_utc(_required(body, "evaluated_at")) != body["evaluated_at"]:
        raise LegacyProjectionError("G101_LEGACY_TIMESTAMP_NONCANONICAL")
    canonical_json(body)
    expected = canonical_hash({key: item for key, item in body.items() if key != "projection_hash"})
    if body["projection_hash"] != expected:
        raise LegacyProjectionError("G101_PROJECTION_HASH_MISMATCH")
    return body


def _validate_evidence(kind: str, evidence: Any, subjects: list[str]) -> None:
    schemas = {
        "SINGLE_EXPERIMENT": {
            "baseline_window": dict, "post_window": dict, "baseline_metrics": dict, "post_metrics": dict,
            "data_quality_status": str, "dedupe_version": str, "attribution_version": str,
        },
        "CREATIVE_GROUP": {
            "launch_id": str, "window": dict, "metrics_by_experiment": dict, "ranking": list,
            "winner_experiment_id": str, "data_quality_status": str, "legacy_evidence": dict,
        },
        "AUDIENCE_PAIR": {
            "launch_id": str, "metrics": dict, "winner_experiment_id": str, "legacy_evidence": dict,
        },
    }
    expected = schemas[kind]
    if not isinstance(evidence, Mapping) or set(evidence) != set(expected):
        raise LegacyProjectionError("G101_PROJECTION_EVIDENCE_SCHEMA_INVALID")
    for field, expected_type in expected.items():
        wrapper = evidence[field]
        if not isinstance(wrapper, Mapping) or set(wrapper) != {"status", "value"}:
            raise LegacyProjectionError("G101_PROJECTION_EVIDENCE_FIELD_INVALID")
        if wrapper["status"] == "MISSING":
            if wrapper["value"] is not None:
                raise LegacyProjectionError("G101_PROJECTION_EVIDENCE_FIELD_INVALID")
        elif wrapper["status"] == "PRESENT":
            if not isinstance(wrapper["value"], expected_type):
                raise LegacyProjectionError("G101_PROJECTION_EVIDENCE_FIELD_INVALID")
        else:
            raise LegacyProjectionError("G101_PROJECTION_EVIDENCE_FIELD_INVALID")
    if kind == "CREATIVE_GROUP" and evidence["metrics_by_experiment"]["status"] == "PRESENT":
        if sorted(evidence["metrics_by_experiment"]["value"]) != subjects:
            raise LegacyProjectionError("G101_PROJECTION_EVIDENCE_SUBJECT_MISMATCH")
    if kind in {"CREATIVE_GROUP", "AUDIENCE_PAIR"}:
        winner = evidence["winner_experiment_id"]
        if winner["status"] == "PRESENT" and winner["value"] not in subjects:
            raise LegacyProjectionError("G101_PROJECTION_WINNER_SUBJECT_MISMATCH")


def _text_field(row: Mapping[str, Any], field: str) -> dict[str, Any]:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        return {"status": "MISSING", "value": None}
    return {"status": "PRESENT", "value": value}


def _json_field(row: Mapping[str, Any], field: str, expected_type: type) -> dict[str, Any]:
    raw = row.get(field)
    if raw is None or raw == "":
        return {"status": "MISSING", "value": None}
    if isinstance(raw, expected_type):
        parsed = raw
    else:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise LegacyProjectionError(f"G101_LEGACY_JSON_INVALID:{field}") from exc
    if not isinstance(parsed, expected_type):
        raise LegacyProjectionError(f"G101_LEGACY_JSON_TYPE_INVALID:{field}")
    canonical_json(parsed)
    return {"status": "PRESENT", "value": parsed}


def _missing_paths(value: Any, prefix: str = "") -> list[str]:
    result: list[str] = []
    if isinstance(value, Mapping):
        if value.get("status") == "MISSING" and set(value) == {"status", "value"}:
            return [prefix]
        for key, item in value.items():
            result.extend(_missing_paths(item, f"{prefix}.{key}" if prefix else str(key)))
    return result


def _required(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value or value != value.strip():
        raise LegacyProjectionError(f"G101_LEGACY_FIELD_INVALID:{field}")
    return value


def _legacy_utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise LegacyProjectionError("G101_LEGACY_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise LegacyProjectionError("G101_LEGACY_TIMESTAMP_INVALID")
    normalized = parsed.astimezone(timezone.utc)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")
