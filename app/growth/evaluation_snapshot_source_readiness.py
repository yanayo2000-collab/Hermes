from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.growth.canonical_evaluation_contracts import canonical_hash, canonical_json
from app.growth.lineage_devval_registry import load_validated_registry_directory


REQUEST_VERSION = "gle-e04-s04-01b-snapshot-source-readiness-request-v1"
OBSERVATION_VERSION = "gle-e04-s04-01b-snapshot-source-observation-v1"
GAP_VERSION = "gle-e04-s04-01b-snapshot-source-gap-v1"
MANIFEST_VERSION = "gle-e04-s04-01b-snapshot-source-readiness-manifest-v1"
CONTRACT_VERSION = "gle-e04-s04-01b-snapshot-source-readiness-v1"

ALLOWED_CHECKPOINTS = frozenset({"D1", "D3", "INFORMATION_LOOK", "FINAL", "HARD_STOP"})
ALLOWED_FIELD_STATUSES = frozenset({
    "ASSERTED_AVAILABLE", "MISSING", "UNFROZEN", "CONFLICT", "UNAUTHORIZED",
})
ALLOWED_GATE0_RESULTS = frozenset({"CONTROLLED_FEASIBLE", "QUASI_ONLY", "NOT_FEASIBLE"})
ALLOWED_SPLITS = frozenset({"DEV", "VALIDATION"})
HOLDOUT_STATUS = "LOCKED_NOT_ASSIGNED"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024
EXACT_FILES = frozenset({
    "manifest.json", "request.json", "source-observations.json", "gaps.ndjson",
})

REQUIRED_FIELD_PATHS = (
    "objective.approval_authority",
    "objective.primary_metric_versions",
    "objective.secondary_metrics",
    "objective.guardrails",
    "objective.risk_boundary",
    "spec.identity_and_lineage",
    "spec.copy_only_topology",
    "spec.invariant_projection",
    "spec.assignment_target",
    "spec.actual_allocation_readback",
    "spec.power_plan",
    "spec.evaluator_version",
    "spec.policy_version",
    "cell_metrics.spend",
    "cell_metrics.impressions",
    "cell_metrics.clicks",
    "cell_metrics.installs",
    "cell_metrics.qualified_joins",
    "cell_metrics.invalid_users",
    "cell_metrics.allocation_share",
    "data_quality.freshness",
    "data_quality.attribution_coverage",
    "data_quality.missing_sources",
    "data_quality.duplicate_rate",
    "mutation_provenance.complete_event_journal",
    "mutation_provenance.cutoff_binding",
)
FIELD_CONTRACT_HASH = canonical_hash({
    "contract_version": CONTRACT_VERSION,
    "required_field_paths": list(REQUIRED_FIELD_PATHS),
    "allowed_field_statuses": sorted(ALLOWED_FIELD_STATUSES),
})

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class EvaluationSnapshotSourceReadinessError(RuntimeError):
    pass


def _fail(code: str) -> None:
    raise EvaluationSnapshotSourceReadinessError(code)


def build_snapshot_source_readiness(
    *,
    registry_dir: str | Path,
    expected_registry_manifest_sha256: str,
    expected_devval_key_registry_hash: str | None,
    source_validation: Mapping[str, Any],
    source_observation_file: str | Path,
    expected_source_observation_sha256: str,
    readiness_id: str,
    requested_at: str,
    checkpoint: str,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Build a readiness fragment. It never builds an EvaluationInputSnapshot."""
    _identifier(readiness_id, "G104B_READINESS_ID_INVALID")
    requested = _utc(requested_at, "G104B_REQUEST_TIME_INVALID")
    if checkpoint not in ALLOWED_CHECKPOINTS:
        _fail("G104B_CHECKPOINT_INVALID")
    registry_anchor = _sha(expected_registry_manifest_sha256, "G104B_REGISTRY_ANCHOR_INVALID")
    try:
        registry_request, registry = load_validated_registry_directory(
            registry_dir,
            expected_registry_manifest_sha256=registry_anchor,
            expected_devval_key_registry_hash=expected_devval_key_registry_hash,
            source_validation=source_validation,
        )
    except Exception as exc:
        raise EvaluationSnapshotSourceReadinessError("G104B_UPSTREAM_REGISTRY_INVALID") from exc
    observation = read_source_observation_file(
        source_observation_file,
        expected_sha256=expected_source_observation_sha256,
    )
    observation = _validate_observation(observation, registry_request, registry)
    cutoff = registry_request["authority_binding"]["data_cutoff_at"]
    if (
        observation["data_cutoff_at"] != cutoff
        or not (
            _instant(cutoff)
            <= _instant(observation["observed_at"])
            <= _instant(requested)
        )
    ):
        _fail("G104B_CUTOFF_BINDING_INVALID")
    if observation["checkpoint"] != checkpoint:
        _fail("G104B_CHECKPOINT_BINDING_INVALID")

    source_binding = {
        "audit_manifest_sha256": registry_request["authority_binding"]["audit_manifest_sha256"],
        "candidate_manifest_sha256": registry_request["authority_binding"]["candidate_manifest_sha256"],
        "authority_manifest_sha256": registry_request["authority_binding"]["authority_manifest_sha256"],
        "authority_status": registry_request["authority_binding"]["status"],
        "registry_manifest_sha256": registry_anchor,
        "registry_request_hash": registry_request["request_hash"],
        "registry_hash": registry["registry_hash"],
        "registry_state_root": registry["registry_state_root"],
        "registry_status": registry["status"],
        "devval_key_registry_hash": expected_devval_key_registry_hash,
        "data_cutoff_at": cutoff,
        "claimed_gate0_result": observation["claimed_gate0_result"],
        "gate0_manifest_sha256": observation["gate0_manifest_sha256"],
        "gate0_assessment_hash": observation["gate0_assessment_hash"],
        "gate0_evidence_root": canonical_hash(observation["gate0_evidence_refs"]),
        "source_observation_sha256": expected_source_observation_sha256,
        "source_observation_hash": observation["observation_hash"],
    }
    gaps = _derive_gaps(registry_request, registry, observation)
    if registry["status"] != "SIGNED_DETERMINISTIC_PARTITION":
        status = "BLOCKED_UPSTREAM_AUTHORITY"
        reasons = ["LINEAGE_AUTHORITY_OR_DEVVAL_PARTITION_NOT_VERIFIED"]
    elif observation["claimed_gate0_result"] != "CONTROLLED_FEASIBLE":
        status = "BLOCKED_GATE0_NOT_CONTROLLED"
        reasons = ["GATE0_CONTROLLED_FEASIBLE_MISSING"]
    elif any(
        field["status"] != "ASSERTED_AVAILABLE"
        for subject in observation["subjects"]
        for field in subject["fields"]
    ):
        status = "SOURCE_INCOMPLETE"
        reasons = sorted({code for gap in gaps for code in gap["reason_codes"]})
    else:
        status = "SOURCE_ASSERTIONS_UNVERIFIED"
        reasons = ["GATE0_RESULT_CONTENT_NOT_VERIFIED", "SOURCE_FIELD_CONTENT_NOT_VERIFIED"]
    request: dict[str, Any] = {
        "schema_version": REQUEST_VERSION,
        "contract_version": CONTRACT_VERSION,
        "readiness_id": readiness_id,
        "requested_at": requested,
        "checkpoint": checkpoint,
        "field_contract_hash": FIELD_CONTRACT_HASH,
        "source_binding": source_binding,
        "subject_count": len(observation["subjects"]),
        "gap_count": len(gaps),
        "gap_root_hash": canonical_hash([item["gap_hash"] for item in gaps]),
        "status": status,
        "reason_codes": reasons,
        "trust_status": "EXTERNALLY_ANCHORED_UNSIGNED_SOURCE_ASSERTIONS",
        "snapshot_effect": "NONE",
        "snapshot_emitted": False,
        "partition_effect": "NONE",
        "holdout_status": HOLDOUT_STATUS,
        "replay_executed": False,
        "replay_eligible": False,
        "golden_eligible": False,
        "gate1_effect": "NONE",
        "not_snapshot_receipt": True,
        "not_dataset_receipt": True,
        "not_replay_receipt": True,
        "not_gate_receipt": True,
        "request_hash": "",
    }
    request["request_hash"] = canonical_hash({key: value for key, value in request.items() if key != "request_hash"})
    return _validate_request(request), observation, _validate_gaps(gaps, request)


def validate_snapshot_source_readiness(
    request: Mapping[str, Any],
    observation: Mapping[str, Any],
    gaps: Sequence[Mapping[str, Any]],
    **source_args: Any,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    expected = build_snapshot_source_readiness(**source_args)
    request_value = _validate_request(request)
    observation_value = _validate_observation_shape(observation)
    gap_values = _validate_gaps(gaps, request_value)
    if (request_value, observation_value, gap_values) != expected:
        _fail("G104B_SOURCE_SEMANTICS_MISMATCH")
    return expected


def write_snapshot_source_readiness_artifact(
    output_dir: str | Path,
    **source_args: Any,
) -> dict[str, Any]:
    request, observation, gaps = build_snapshot_source_readiness(**source_args)
    payloads = {
        "request.json": _json_bytes(request),
        "source-observations.json": _json_bytes(observation),
        "gaps.ndjson": _ndjson_bytes(gaps),
    }
    manifest = _manifest(request, payloads)
    payloads["manifest.json"] = _json_bytes(manifest)
    _write_artifact_directory(Path(output_dir), payloads)
    raw = _read_artifact_directory(Path(output_dir))
    loaded = load_validated_snapshot_source_readiness_directory(
        output_dir,
        expected_manifest_sha256=hashlib.sha256(raw["manifest.json"]).hexdigest(),
        **source_args,
    )
    if loaded["manifest"] != manifest:
        _fail("G104B_WRITE_READBACK_MISMATCH")
    return manifest


def load_validated_snapshot_source_readiness_directory(
    input_dir: str | Path,
    *,
    expected_manifest_sha256: str,
    **source_args: Any,
) -> dict[str, Any]:
    anchor = _sha(expected_manifest_sha256, "G104B_MANIFEST_ANCHOR_INVALID")
    raw = _read_artifact_directory(Path(input_dir))
    if hashlib.sha256(raw["manifest.json"]).hexdigest() != anchor:
        _fail("G104B_MANIFEST_ANCHOR_MISMATCH")
    request = _json_document(raw["request.json"], "G104B_REQUEST_JSON_INVALID")
    observation = _json_document(raw["source-observations.json"], "G104B_OBSERVATION_JSON_INVALID")
    gaps = _ndjson_document(raw["gaps.ndjson"])
    manifest = _json_document(raw["manifest.json"], "G104B_MANIFEST_JSON_INVALID")
    if not all(isinstance(value, Mapping) for value in (request, observation, manifest)):
        _fail("G104B_ARTIFACT_JSON_INVALID")
    expected_request, expected_observation, expected_gaps = validate_snapshot_source_readiness(
        request, observation, gaps, **source_args,
    )
    _validate_manifest(manifest, raw, expected_request)
    expected_manifest = _manifest(expected_request, {
        name: raw[name] for name in EXACT_FILES - {"manifest.json"}
    })
    if dict(manifest) != expected_manifest:
        _fail("G104B_MANIFEST_DERIVATION_MISMATCH")
    return {
        "request": expected_request,
        "observation": expected_observation,
        "gaps": expected_gaps,
        "manifest": expected_manifest,
    }


def read_source_observation_file(path: str | Path, *, expected_sha256: str) -> dict[str, Any]:
    expected = _sha(expected_sha256, "G104B_OBSERVATION_ANCHOR_INVALID")
    raw = _read_single_regular(Path(path))
    if hashlib.sha256(raw).hexdigest() != expected:
        _fail("G104B_OBSERVATION_ANCHOR_MISMATCH")
    value = _json_document(raw, "G104B_OBSERVATION_JSON_INVALID")
    if not isinstance(value, Mapping):
        _fail("G104B_OBSERVATION_JSON_INVALID")
    return dict(value)


def _validate_observation(
    value: Mapping[str, Any],
    registry_request: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> dict[str, Any]:
    observation = _validate_observation_shape(value)
    expected_subjects: list[tuple[str, str, str]] = []
    if registry["status"] == "SIGNED_DETERMINISTIC_PARTITION":
        for assignment in registry["assignments"]:
            if assignment["split"] not in ALLOWED_SPLITS:
                _fail("G104B_HOLDOUT_FORBIDDEN")
            expected_subjects.extend(
                (assignment["lineage_id"], canonical_id, assignment["split"])
                for canonical_id in assignment["canonical_experiment_ids"]
            )
    actual_subjects = [
        (item["lineage_id"], item["canonical_experiment_id"], item["split"])
        for item in observation["subjects"]
    ]
    if actual_subjects != sorted(expected_subjects):
        _fail("G104B_SUBJECT_DENOMINATOR_MISMATCH")
    if observation["upstream_registry_hash"] != registry["registry_hash"]:
        _fail("G104B_REGISTRY_BINDING_MISMATCH")
    if observation["data_cutoff_at"] != registry_request["authority_binding"]["data_cutoff_at"]:
        _fail("G104B_CUTOFF_BINDING_INVALID")
    return observation


def _validate_observation_shape(value: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "schema_version", "observation_id", "observed_at", "data_cutoff_at", "checkpoint",
        "claimed_gate0_result", "gate0_manifest_sha256", "gate0_assessment_hash",
        "gate0_evidence_refs", "upstream_registry_hash", "subjects", "observation_hash",
    }
    body = _exact(value, keys, "G104B_OBSERVATION_SCHEMA_INVALID")
    if body["schema_version"] != OBSERVATION_VERSION:
        _fail("G104B_OBSERVATION_VERSION_INVALID")
    _identifier(body["observation_id"], "G104B_OBSERVATION_ID_INVALID")
    observed = _utc(body["observed_at"], "G104B_OBSERVATION_TIME_INVALID")
    cutoff = _utc(body["data_cutoff_at"], "G104B_OBSERVATION_TIME_INVALID")
    if _instant(observed) < _instant(cutoff):
        _fail("G104B_OBSERVATION_TIME_INVALID")
    if body["checkpoint"] not in ALLOWED_CHECKPOINTS:
        _fail("G104B_CHECKPOINT_INVALID")
    if body["claimed_gate0_result"] not in ALLOWED_GATE0_RESULTS:
        _fail("G104B_GATE0_RESULT_INVALID")
    _sha(body["gate0_manifest_sha256"], "G104B_GATE0_HASH_INVALID")
    _sha(body["gate0_assessment_hash"], "G104B_GATE0_HASH_INVALID")
    body["gate0_evidence_refs"] = _validate_refs(body["gate0_evidence_refs"])
    if not body["gate0_evidence_refs"] or not any(
        ref["manifest_sha256"] == body["gate0_manifest_sha256"]
        for ref in body["gate0_evidence_refs"]
    ):
        _fail("G104B_GATE0_EVIDENCE_INVALID")
    if body["claimed_gate0_result"] == "CONTROLLED_FEASIBLE" and not any(
        ref["evidence_class"] == "CONTROLLED_GATE0_ASSESSMENT"
        and ref["manifest_sha256"] == body["gate0_manifest_sha256"]
        and ref["record_hash"] == body["gate0_assessment_hash"]
        for ref in body["gate0_evidence_refs"]
    ):
        _fail("G104B_GATE0_EVIDENCE_INVALID")
    _sha(body["upstream_registry_hash"], "G104B_REGISTRY_HASH_INVALID")
    if not isinstance(body["subjects"], list):
        _fail("G104B_SUBJECTS_INVALID")
    subjects = [_validate_subject(item) for item in body["subjects"]]
    if subjects != sorted(subjects, key=lambda item: (item["lineage_id"], item["canonical_experiment_id"])):
        _fail("G104B_SUBJECT_ORDER_INVALID")
    if len({item["canonical_experiment_id"] for item in subjects}) != len(subjects):
        _fail("G104B_SUBJECT_DUPLICATE")
    body["subjects"] = subjects
    if body["observation_hash"] != canonical_hash({key: item for key, item in body.items() if key != "observation_hash"}):
        _fail("G104B_OBSERVATION_HASH_MISMATCH")
    return body


def _validate_subject(value: Any) -> dict[str, Any]:
    body = _exact(value, {
        "lineage_id", "canonical_experiment_id", "split", "fields", "subject_hash",
    }, "G104B_SUBJECT_SCHEMA_INVALID")
    _identifier(body["lineage_id"], "G104B_LINEAGE_ID_INVALID")
    _identifier(body["canonical_experiment_id"], "G104B_EXPERIMENT_ID_INVALID")
    if body["split"] not in ALLOWED_SPLITS:
        _fail("G104B_HOLDOUT_FORBIDDEN")
    if not isinstance(body["fields"], list):
        _fail("G104B_FIELDS_INVALID")
    fields = [_validate_field(item) for item in body["fields"]]
    if [item["field_path"] for item in fields] != list(REQUIRED_FIELD_PATHS):
        _fail("G104B_FIELD_DENOMINATOR_MISMATCH")
    body["fields"] = fields
    if body["subject_hash"] != canonical_hash({key: item for key, item in body.items() if key != "subject_hash"}):
        _fail("G104B_SUBJECT_HASH_MISMATCH")
    return body


def _validate_field(value: Any) -> dict[str, Any]:
    body = _exact(value, {
        "field_path", "status", "value_commitment", "source_refs", "reason_codes",
    }, "G104B_FIELD_SCHEMA_INVALID")
    if body["field_path"] not in REQUIRED_FIELD_PATHS or body["status"] not in ALLOWED_FIELD_STATUSES:
        _fail("G104B_FIELD_INVALID")
    if body["status"] == "ASSERTED_AVAILABLE":
        _sha(body["value_commitment"], "G104B_FIELD_COMMITMENT_INVALID")
        if not body["source_refs"] or body["reason_codes"]:
            _fail("G104B_FIELD_AVAILABLE_INVALID")
    else:
        if body["value_commitment"] is not None or not body["reason_codes"]:
            _fail("G104B_FIELD_GAP_INVALID")
    body["source_refs"] = _validate_refs(body["source_refs"])
    body["reason_codes"] = _reason_codes(
        body["reason_codes"], allow_empty=body["status"] == "ASSERTED_AVAILABLE",
    )
    return body


def _validate_refs(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        _fail("G104B_SOURCE_REFS_INVALID")
    refs: list[dict[str, Any]] = []
    for value in values:
        ref = _exact(value, {
            "artifact_type", "manifest_sha256", "record_id", "record_hash", "evidence_class",
        }, "G104B_SOURCE_REF_INVALID")
        _identifier(ref["artifact_type"], "G104B_SOURCE_REF_INVALID")
        _sha(ref["manifest_sha256"], "G104B_SOURCE_REF_INVALID")
        _identifier(ref["record_id"], "G104B_SOURCE_REF_INVALID")
        _sha(ref["record_hash"], "G104B_SOURCE_REF_INVALID")
        if ref["evidence_class"] not in {
            "EXTERNALLY_ANCHORED_OBSERVATION", "SIGNED_SOURCE_ASSERTION",
            "IMMUTABLE_MUTATION_JOURNAL", "CONTROLLED_GATE0_ASSESSMENT",
        }:
            _fail("G104B_SOURCE_REF_INVALID")
        refs.append(ref)
    key = lambda ref: (
        ref["artifact_type"], ref["manifest_sha256"], ref["record_id"],
        ref["record_hash"], ref["evidence_class"],
    )
    if refs != sorted(refs, key=key) or len({key(ref) for ref in refs}) != len(refs):
        _fail("G104B_SOURCE_REF_ORDER_INVALID")
    return refs


def _derive_gaps(
    registry_request: Mapping[str, Any],
    registry: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    if registry_request["authority_binding"]["status"] != "VERIFIED":
        gaps.append(_gap("GLOBAL", None, "upstream.lineage_authority", "UNAUTHORIZED", ["LINEAGE_AUTHORITY_NOT_VERIFIED"], []))
    if registry["status"] != "SIGNED_DETERMINISTIC_PARTITION":
        gaps.append(_gap("GLOBAL", None, "upstream.devval_partition", "UNAUTHORIZED", ["DEVVAL_PARTITION_NOT_SIGNED"], []))
    if observation["claimed_gate0_result"] != "CONTROLLED_FEASIBLE":
        gaps.append(_gap("GLOBAL", None, "gate0.controlled_feasibility", "UNAUTHORIZED", ["GATE0_CONTROLLED_FEASIBLE_MISSING"], []))
    else:
        gaps.append(_gap(
            "GLOBAL", None, "gate0.controlled_feasibility", "UNAUTHORIZED",
            ["GATE0_RESULT_CONTENT_NOT_VERIFIED"], observation["gate0_evidence_refs"],
        ))
    for subject in observation["subjects"]:
        for field in subject["fields"]:
            if field["status"] == "ASSERTED_AVAILABLE":
                gaps.append(_gap(
                    "SUBJECT", subject["canonical_experiment_id"], field["field_path"],
                    "UNAUTHORIZED", ["SOURCE_FIELD_CONTENT_NOT_VERIFIED"], field["source_refs"],
                ))
            else:
                gaps.append(_gap(
                    "SUBJECT",
                    subject["canonical_experiment_id"],
                    field["field_path"],
                    field["status"],
                    field["reason_codes"],
                    field["source_refs"],
                ))
    gaps.sort(key=lambda item: (item["scope"], item["subject_id"] or "", item["field_path"]))
    return gaps


def _gap(
    scope: str,
    subject_id: str | None,
    field_path: str,
    status: str,
    reasons: Sequence[str],
    refs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    value = {
        "schema_version": GAP_VERSION,
        "scope": scope,
        "subject_id": subject_id,
        "field_path": field_path,
        "status": status,
        "reason_codes": list(reasons),
        "evidence_refs": [dict(item) for item in refs],
        "gap_hash": "",
    }
    value["gap_hash"] = canonical_hash({key: item for key, item in value.items() if key != "gap_hash"})
    return value


def _validate_request(value: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "schema_version", "contract_version", "readiness_id", "requested_at", "checkpoint",
        "field_contract_hash", "source_binding", "subject_count", "gap_count", "gap_root_hash",
        "status", "reason_codes", "trust_status", "snapshot_effect", "snapshot_emitted",
        "partition_effect", "holdout_status", "replay_executed", "replay_eligible",
        "golden_eligible", "gate1_effect", "not_snapshot_receipt", "not_dataset_receipt",
        "not_replay_receipt", "not_gate_receipt", "request_hash",
    }
    body = _exact(value, keys, "G104B_REQUEST_SCHEMA_INVALID")
    if body["schema_version"] != REQUEST_VERSION or body["contract_version"] != CONTRACT_VERSION:
        _fail("G104B_REQUEST_VERSION_INVALID")
    _identifier(body["readiness_id"], "G104B_READINESS_ID_INVALID")
    _utc(body["requested_at"], "G104B_REQUEST_TIME_INVALID")
    if body["checkpoint"] not in ALLOWED_CHECKPOINTS or body["field_contract_hash"] != FIELD_CONTRACT_HASH:
        _fail("G104B_REQUEST_CONTRACT_INVALID")
    if body["status"] not in {
        "BLOCKED_UPSTREAM_AUTHORITY", "BLOCKED_GATE0_NOT_CONTROLLED",
        "SOURCE_INCOMPLETE", "SOURCE_ASSERTIONS_UNVERIFIED",
    }:
        _fail("G104B_STATUS_INVALID")
    if type(body["subject_count"]) is not int or body["subject_count"] < 0:
        _fail("G104B_COUNT_INVALID")
    if type(body["gap_count"]) is not int or body["gap_count"] < 0:
        _fail("G104B_COUNT_INVALID")
    _sha(body["gap_root_hash"], "G104B_GAP_ROOT_INVALID")
    body["reason_codes"] = _reason_codes(body["reason_codes"])
    binding_keys = {
        "audit_manifest_sha256", "candidate_manifest_sha256", "authority_manifest_sha256",
        "authority_status", "registry_manifest_sha256", "registry_request_hash", "registry_hash",
        "registry_state_root", "registry_status", "devval_key_registry_hash", "data_cutoff_at",
        "claimed_gate0_result", "gate0_manifest_sha256", "gate0_assessment_hash", "gate0_evidence_root",
        "source_observation_sha256", "source_observation_hash",
    }
    binding = _exact(body["source_binding"], binding_keys, "G104B_SOURCE_BINDING_INVALID")
    for field in (
        "audit_manifest_sha256", "candidate_manifest_sha256", "authority_manifest_sha256",
        "registry_manifest_sha256", "registry_request_hash", "registry_hash",
        "gate0_manifest_sha256", "gate0_assessment_hash", "gate0_evidence_root",
        "source_observation_sha256", "source_observation_hash",
    ):
        _sha(binding[field], "G104B_SOURCE_BINDING_INVALID")
    if binding["registry_state_root"] is not None:
        _sha(binding["registry_state_root"], "G104B_SOURCE_BINDING_INVALID")
    elif binding["registry_status"] == "SIGNED_DETERMINISTIC_PARTITION":
        _fail("G104B_SOURCE_BINDING_INVALID")
    if binding["devval_key_registry_hash"] is not None:
        _sha(binding["devval_key_registry_hash"], "G104B_SOURCE_BINDING_INVALID")
    _utc(binding["data_cutoff_at"], "G104B_SOURCE_BINDING_INVALID")
    ceiling = {
        "trust_status": "EXTERNALLY_ANCHORED_UNSIGNED_SOURCE_ASSERTIONS",
        "snapshot_effect": "NONE", "snapshot_emitted": False,
        "partition_effect": "NONE", "holdout_status": HOLDOUT_STATUS,
        "replay_executed": False, "replay_eligible": False,
        "golden_eligible": False, "gate1_effect": "NONE",
        "not_snapshot_receipt": True, "not_dataset_receipt": True,
        "not_replay_receipt": True, "not_gate_receipt": True,
    }
    if any(body[key] != expected for key, expected in ceiling.items()):
        _fail("G104B_CEILING_INVALID")
    if body["request_hash"] != canonical_hash({key: item for key, item in body.items() if key != "request_hash"}):
        _fail("G104B_REQUEST_HASH_MISMATCH")
    return body


def _validate_gaps(values: Sequence[Mapping[str, Any]], request: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        _fail("G104B_GAPS_INVALID")
    result: list[dict[str, Any]] = []
    for value in values:
        gap = _exact(value, {
            "schema_version", "scope", "subject_id", "field_path", "status",
            "reason_codes", "evidence_refs", "gap_hash",
        }, "G104B_GAP_SCHEMA_INVALID")
        if gap["schema_version"] != GAP_VERSION or gap["scope"] not in {"GLOBAL", "SUBJECT"}:
            _fail("G104B_GAP_INVALID")
        if gap["subject_id"] is not None:
            _identifier(gap["subject_id"], "G104B_GAP_INVALID")
        if gap["status"] not in ALLOWED_FIELD_STATUSES - {"ASSERTED_AVAILABLE"}:
            _fail("G104B_GAP_INVALID")
        gap["reason_codes"] = _reason_codes(gap["reason_codes"])
        gap["evidence_refs"] = _validate_refs(gap["evidence_refs"])
        if gap["gap_hash"] != canonical_hash({key: item for key, item in gap.items() if key != "gap_hash"}):
            _fail("G104B_GAP_HASH_MISMATCH")
        result.append(gap)
    if result != sorted(result, key=lambda item: (item["scope"], item["subject_id"] or "", item["field_path"])):
        _fail("G104B_GAP_ORDER_INVALID")
    if len({item["gap_hash"] for item in result}) != len(result):
        _fail("G104B_GAP_DUPLICATE")
    if request["gap_count"] != len(result) or request["gap_root_hash"] != canonical_hash([item["gap_hash"] for item in result]):
        _fail("G104B_GAP_BINDING_INVALID")
    return result


def _manifest(request: Mapping[str, Any], payloads: Mapping[str, bytes]) -> dict[str, Any]:
    files = {
        name: {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
        for name, raw in payloads.items()
    }
    value: dict[str, Any] = {
        "schema_version": MANIFEST_VERSION,
        "contract_version": CONTRACT_VERSION,
        "readiness_id": request["readiness_id"],
        "request_hash": request["request_hash"],
        "status": request["status"],
        "trust_status": request["trust_status"],
        "subject_count": request["subject_count"],
        "gap_count": request["gap_count"],
        "snapshot_effect": "NONE",
        "snapshot_emitted": False,
        "partition_effect": "NONE",
        "holdout_status": HOLDOUT_STATUS,
        "replay_executed": False,
        "replay_eligible": False,
        "golden_eligible": False,
        "gate1_effect": "NONE",
        "not_snapshot_receipt": True,
        "not_dataset_receipt": True,
        "not_replay_receipt": True,
        "not_gate_receipt": True,
        "files": files,
        "manifest_hash": "",
    }
    value["manifest_hash"] = canonical_hash({key: item for key, item in value.items() if key != "manifest_hash"})
    return value


def _validate_manifest(value: Mapping[str, Any], raw: Mapping[str, bytes], request: Mapping[str, Any]) -> None:
    expected = _manifest(request, {name: raw[name] for name in EXACT_FILES - {"manifest.json"}})
    if dict(value) != expected:
        _fail("G104B_MANIFEST_INVALID")


def _json_bytes(value: Any) -> bytes:
    raw = (canonical_json(value) + "\n").encode()
    if len(raw) > MAX_FILE_BYTES:
        _fail("G104B_ARTIFACT_TOO_LARGE")
    return raw


def _ndjson_bytes(values: Sequence[Mapping[str, Any]]) -> bytes:
    raw = b"".join((canonical_json(value) + "\n").encode() for value in values)
    if len(raw) > MAX_FILE_BYTES:
        _fail("G104B_ARTIFACT_TOO_LARGE")
    return raw


def _json_document(raw: bytes, code: str) -> Any:
    try:
        text = raw.decode("utf-8")
        if not text.endswith("\n") or text.count("\n") != 1:
            _fail(code)
        value = json.loads(text, object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(code)
    if _json_bytes(value) != raw:
        _fail(code)
    return value


def _ndjson_document(raw: bytes) -> list[dict[str, Any]]:
    if raw and not raw.endswith(b"\n"):
        _fail("G104B_GAPS_JSON_INVALID")
    values: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            value = json.loads(line.decode("utf-8"), object_pairs_hook=_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError):
            _fail("G104B_GAPS_JSON_INVALID")
        if not isinstance(value, Mapping) or (canonical_json(value) + "\n").encode() != line + b"\n":
            _fail("G104B_GAPS_JSON_INVALID")
        values.append(dict(value))
    return values


def _pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values:
        if key in result:
            _fail("G104B_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _read_single_regular(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise EvaluationSnapshotSourceReadinessError("G104B_INPUT_FILE_INVALID") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_FILE_BYTES:
            _fail("G104B_INPUT_FILE_INVALID")
        raw = _read_fd(fd, MAX_FILE_BYTES)
        after = os.fstat(fd)
        if _identity(before) != _identity(after) or len(raw) != after.st_size:
            _fail("G104B_INPUT_CHANGED_DURING_READ")
        return raw
    finally:
        os.close(fd)


def _read_artifact_directory(root: Path) -> dict[str, bytes]:
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        root_fd = os.open(root, dir_flags)
    except OSError as exc:
        raise EvaluationSnapshotSourceReadinessError("G104B_ARTIFACT_DIRECTORY_INVALID") from exc
    try:
        before_dir = os.fstat(root_fd)
        if not stat.S_ISDIR(before_dir.st_mode) or stat.S_IMODE(before_dir.st_mode) != 0o700:
            _fail("G104B_ARTIFACT_DIRECTORY_INVALID")
        if set(os.listdir(root_fd)) != EXACT_FILES:
            _fail("G104B_ARTIFACT_FILE_SET_INVALID")
        raw: dict[str, bytes] = {}
        total = 0
        for name in sorted(EXACT_FILES):
            try:
                fd = os.open(name, file_flags, dir_fd=root_fd)
            except OSError as exc:
                raise EvaluationSnapshotSourceReadinessError("G104B_ARTIFACT_FILE_INVALID") from exc
            try:
                before = os.fstat(fd)
                if (
                    not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > MAX_FILE_BYTES
                ):
                    _fail("G104B_ARTIFACT_FILE_INVALID")
                value = _read_fd(fd, MAX_FILE_BYTES)
                after = os.fstat(fd)
                if _identity(before) != _identity(after) or len(value) != after.st_size:
                    _fail("G104B_ARTIFACT_CHANGED_DURING_READ")
                raw[name] = value
                total += len(value)
                if total > MAX_TOTAL_BYTES:
                    _fail("G104B_ARTIFACT_TOO_LARGE")
            finally:
                os.close(fd)
        after_dir = os.fstat(root_fd)
        if _identity(before_dir) != _identity(after_dir) or set(os.listdir(root_fd)) != EXACT_FILES:
            _fail("G104B_ARTIFACT_CHANGED_DURING_READ")
        return raw
    finally:
        os.close(root_fd)


def _write_artifact_directory(root: Path, payloads: Mapping[str, bytes]) -> None:
    if set(payloads) != EXACT_FILES or not root.name or root.name in {".", ".."}:
        _fail("G104B_OUTPUT_INVALID")
    if any(len(raw) > MAX_FILE_BYTES for raw in payloads.values()) or sum(map(len, payloads.values())) > MAX_TOTAL_BYTES:
        _fail("G104B_ARTIFACT_TOO_LARGE")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(root.parent, flags)
    except OSError as exc:
        raise EvaluationSnapshotSourceReadinessError("G104B_OUTPUT_PARENT_INVALID") from exc
    root_fd: int | None = None
    complete = False
    try:
        try:
            os.mkdir(root.name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            _fail("G104B_OUTPUT_EXISTS")
        root_fd = os.open(root.name, flags, dir_fd=parent_fd)
        os.fchmod(root_fd, 0o700)
        _require_directory_identity(parent_fd, root.name, root_fd)
        for name in sorted(payloads):
            _write_file(root_fd, name, payloads[name])
        if set(os.listdir(root_fd)) != EXACT_FILES:
            _fail("G104B_OUTPUT_FILE_SET_INVALID")
        os.fsync(root_fd)
        _require_directory_identity(parent_fd, root.name, root_fd)
        complete = True
        os.fsync(parent_fd)
    except Exception as exc:
        if complete:
            raise EvaluationSnapshotSourceReadinessError("G104B_OUTPUT_DURABILITY_UNCERTAIN") from exc
        if root_fd is not None:
            for name in EXACT_FILES:
                try:
                    os.unlink(name, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
            try:
                _require_directory_identity(parent_fd, root.name, root_fd)
                os.rmdir(root.name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)


def _write_file(directory_fd: int, name: str, raw: bytes) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
    try:
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                _fail("G104B_WRITE_FAILED")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(name, 0o600, dir_fd=directory_fd, follow_symlinks=False)


def _read_fd(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    consumed = 0
    while True:
        chunk = os.read(fd, min(65536, limit + 1 - consumed))
        if not chunk:
            break
        consumed += len(chunk)
        if consumed > limit:
            _fail("G104B_ARTIFACT_TOO_LARGE")
        chunks.append(chunk)
    return b"".join(chunks)


def _require_directory_identity(parent_fd: int, name: str, root_fd: int) -> None:
    by_name = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    by_fd = os.fstat(root_fd)
    if not stat.S_ISDIR(by_name.st_mode) or (by_name.st_dev, by_name.st_ino) != (by_fd.st_dev, by_fd.st_ino):
        _fail("G104B_OUTPUT_DIRECTORY_CHANGED")


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev, value.st_ino, stat.S_IMODE(value.st_mode), value.st_nlink,
        value.st_size, value.st_mtime_ns, value.st_ctime_ns,
    )


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail(code)
    return dict(value)


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        _fail(code)
    return value


def _sha(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        _fail(code)
    return value


def _utc(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        _fail(code)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _fail(code)
    if parsed.tzinfo != timezone.utc:
        _fail(code)
    return value


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _reason_codes(values: Any, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(values, list) or (not values and not allow_empty):
        _fail("G104B_REASON_CODES_INVALID")
    if values != sorted(set(values)) or any(not isinstance(value, str) or not _CODE_RE.fullmatch(value) for value in values):
        _fail("G104B_REASON_CODES_INVALID")
    return list(values)
