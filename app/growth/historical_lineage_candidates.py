from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.growth.canonical_evaluation_contracts import canonical_hash, canonical_json, validate_sha256, validate_utc
from app.growth.historical_asof_audit import BUNDLE_VERSION, TABLES, validate_audit_bundle


MANIFEST_VERSION = "gle-g1-02a-asof-audit-manifest-v1"
CANDIDATE_VERSION = "gle-g1-02b-lineage-candidate-bundle-v1"
ENGINE_VERSION = "gle-g1-02b-lineage-candidate-engine-v1"
EXACT_INPUT_FILES = frozenset({"manifest.json", "records.ndjson", "gaps.ndjson", "coverage.json"})
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_NDJSON_ROWS = 100_000
LEGACY_TABLES = frozenset({
    "ad_experiment_evaluation", "ad_creative_group_evaluation", "ad_audience_pair_evaluation",
})
LEGACY_KIND_BY_TABLE = {
    "ad_experiment_evaluation": "SINGLE_EXPERIMENT",
    "ad_creative_group_evaluation": "CREATIVE_GROUP",
    "ad_audience_pair_evaluation": "AUDIENCE_PAIR",
}
REASON_CODES = frozenset({
    "PARENT_LINEAGE_EVIDENCE_MISSING", "SUBJECT_EXPERIMENT_MISSING", "LAUNCH_TOKEN_MISSING",
    "LAUNCH_TOKEN_CONFLICT", "SUBJECT_METADATA_CONFLICT", "COMPONENT_MEMBERSHIP_CONFLICT",
    "SUBJECT_METADATA_MISSING", "SINGLE_EXPERIMENT_COMPONENT_INSUFFICIENT",
    "INVALID_LEGACY_PROJECTION",
})
CONFLICT_REASONS = frozenset({
    "SUBJECT_EXPERIMENT_MISSING", "LAUNCH_TOKEN_CONFLICT", "SUBJECT_METADATA_CONFLICT",
    "COMPONENT_MEMBERSHIP_CONFLICT",
})


class HistoricalLineageCandidateError(RuntimeError):
    pass


def _fail(code: str) -> None:
    raise HistoricalLineageCandidateError(code)


def load_validated_audit_directory(
    input_dir: str | os.PathLike[str], *, expected_manifest_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        validate_sha256(expected_manifest_sha256, code="G102B_EXPECTED_MANIFEST_HASH_INVALID")
    except ValueError as exc:
        raise HistoricalLineageCandidateError(str(exc)) from exc
    unresolved_root = Path(input_dir).expanduser()
    if unresolved_root.is_symlink():
        _fail("G102B_INPUT_DIRECTORY_INVALID")
    root = unresolved_root.resolve()
    if not root.is_dir():
        _fail("G102B_INPUT_DIRECTORY_INVALID")
    names = {item.name for item in root.iterdir()}
    if names != EXACT_INPUT_FILES:
        _fail("G102B_INPUT_FILE_SET_INVALID")
    raw = {name: _read_regular(root / name) for name in sorted(EXACT_INPUT_FILES)}
    manifest_sha = hashlib.sha256(raw["manifest.json"]).hexdigest()
    if manifest_sha != expected_manifest_sha256:
        _fail("G102B_MANIFEST_ANCHOR_MISMATCH")
    manifest = _canonical_json_document(raw["manifest.json"], "G102B_MANIFEST_JSON_INVALID")
    manifest_keys = {
        "schema_version", "audit_id", "data_cutoff_at", "bundle_hash", "source_snapshot_hash",
        "request", "source_snapshot", "table_manifests", "coverage", "status",
        "replay_eligibility", "not_replay_receipt", "trust_status", "files", "manifest_hash",
    }
    if not isinstance(manifest, Mapping) or set(manifest) != manifest_keys or manifest["schema_version"] != MANIFEST_VERSION:
        _fail("G102B_MANIFEST_SCHEMA_INVALID")
    if manifest["manifest_hash"] != canonical_hash({key: value for key, value in manifest.items() if key != "manifest_hash"}):
        _fail("G102B_MANIFEST_HASH_MISMATCH")
    if not isinstance(manifest["files"], Mapping) or set(manifest["files"]) != EXACT_INPUT_FILES - {"manifest.json"}:
        _fail("G102B_MANIFEST_FILE_SET_INVALID")
    for name in sorted(EXACT_INPUT_FILES - {"manifest.json"}):
        descriptor = manifest["files"][name]
        if not isinstance(descriptor, Mapping) or set(descriptor) != {"sha256", "size_bytes"}:
            _fail("G102B_MANIFEST_FILE_DESCRIPTOR_INVALID")
        try:
            validate_sha256(descriptor["sha256"], code="G102B_MANIFEST_FILE_HASH_INVALID")
        except ValueError as exc:
            raise HistoricalLineageCandidateError(str(exc)) from exc
        if type(descriptor["size_bytes"]) is not int or descriptor["size_bytes"] < 0:
            _fail("G102B_MANIFEST_FILE_SIZE_INVALID")
        if len(raw[name]) != descriptor["size_bytes"] or hashlib.sha256(raw[name]).hexdigest() != descriptor["sha256"]:
            _fail("G102B_INPUT_FILE_INTEGRITY_MISMATCH")
    records = _canonical_ndjson(raw["records.ndjson"], "G102B_RECORDS_INVALID")
    gaps = _canonical_ndjson(raw["gaps.ndjson"], "G102B_GAPS_INVALID")
    coverage = _canonical_json_document(raw["coverage.json"], "G102B_COVERAGE_INVALID")
    bundle = {
        "schema_version": BUNDLE_VERSION,
        "request": manifest["request"],
        "source_snapshot": manifest["source_snapshot"],
        "table_manifests": manifest["table_manifests"],
        "records": records,
        "gaps": gaps,
        "coverage": coverage,
        "status": manifest["status"],
        "replay_eligibility": manifest["replay_eligibility"],
        "not_replay_receipt": manifest["not_replay_receipt"],
        "trust_status": manifest["trust_status"],
        "bundle_hash": manifest["bundle_hash"],
    }
    try:
        bundle = validate_audit_bundle(bundle)
    except Exception as exc:
        raise HistoricalLineageCandidateError("G102B_INPUT_BUNDLE_INVALID") from exc
    if (
        manifest["audit_id"] != bundle["request"]["audit_id"]
        or manifest["data_cutoff_at"] != bundle["request"]["data_cutoff_at"]
        or manifest["source_snapshot_hash"] != bundle["source_snapshot"]["source_snapshot_hash"]
        or manifest["coverage"] != bundle["coverage"]
    ):
        _fail("G102B_MANIFEST_BUNDLE_BINDING_MISMATCH")
    if (
        bundle["trust_status"] != "UNSIGNED_LOCAL_CAPTURE"
        or bundle["status"] != "INCOMPLETE"
        or bundle["replay_eligibility"] != "AUDIT_ONLY"
        or bundle["not_replay_receipt"] is not True
    ):
        _fail("G102B_INPUT_TRUST_CEILING_INVALID")
    binding = {
        "input_manifest_sha256": manifest_sha,
        "input_manifest_hash": manifest["manifest_hash"],
        "input_bundle_hash": bundle["bundle_hash"],
        "input_request_hash": bundle["request"]["request_hash"],
        "input_source_snapshot_hash": bundle["source_snapshot"]["source_snapshot_hash"],
        "input_authoritative_asof_hash": bundle["source_snapshot"]["authoritative_asof_hash"],
        "input_table_manifest_hashes": {
            item["table"]: item["table_manifest_hash"] for item in bundle["table_manifests"]
        },
        "data_cutoff_at": bundle["request"]["data_cutoff_at"],
    }
    return bundle, binding


def derive_lineage_candidates_from_audit_directory(
    audit_dir: str | os.PathLike[str], *, expected_manifest_sha256: str,
    derivation_id: str, derived_at: str,
) -> dict[str, Any]:
    bundle, binding = load_validated_audit_directory(
        audit_dir, expected_manifest_sha256=expected_manifest_sha256,
    )
    return _derive_lineage_candidates_from_validated(
        bundle, binding, derivation_id=derivation_id, derived_at=derived_at,
    )


def _derive_lineage_candidates_from_validated(
    audit_bundle: Mapping[str, Any], input_binding: Mapping[str, Any], *,
    derivation_id: str, derived_at: str, validate_output: bool = True,
) -> dict[str, Any]:
    if not isinstance(derivation_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", derivation_id):
        _fail("G102B_DERIVATION_ID_INVALID")
    try:
        derived_at = validate_utc(derived_at, code="G102B_DERIVED_AT_INVALID")
        bundle = validate_audit_bundle(audit_bundle)
    except Exception as exc:
        if isinstance(exc, HistoricalLineageCandidateError):
            raise
        raise HistoricalLineageCandidateError("G102B_INPUT_BUNDLE_INVALID") from exc
    binding = _validate_input_binding(input_binding, bundle)
    if _utc_datetime(derived_at) < _utc_datetime(binding["data_cutoff_at"]):
        _fail("G102B_DERIVED_BEFORE_CUTOFF")
    table_hashes = {item["table"]: item["table_manifest_hash"] for item in bundle["table_manifests"]}
    experiments = {
        item["source_id"]: item for item in bundle["records"] if item["source_table"] == "ad_experiment"
    }
    experiment_members_by_launch: dict[str, set[str]] = {}
    for experiment_id, record in experiments.items():
        launch_token = record["projection"]["launch_token"]
        if launch_token:
            experiment_members_by_launch.setdefault(launch_token, set()).add(experiment_id)
    legacy = [item for item in bundle["records"] if item["source_table"] in LEGACY_TABLES]
    launch_memberships: dict[str, set[tuple[str, ...]]] = {}
    prelim: list[dict[str, Any]] = []
    for item in legacy:
        projection = item["projection"]
        if projection.get("status") == "INVALID_LEGACY_PROJECTION":
            prelim.append(_invalid_entry(item, table_hashes[item["source_table"]]))
            continue
        subjects = list(projection["subject_experiment_ids"])
        subject_records = [experiments.get(subject) for subject in subjects]
        subject_launch_tokens = [
            record["projection"]["launch_token"] for record in subject_records if record is not None
        ]
        launch_tokens = {
            value for value in subject_launch_tokens if value
        }
        evaluation_launch = _evaluation_launch_token(projection)
        if evaluation_launch:
            launch_tokens.add(evaluation_launch)
        metadata = {
            (
                record["projection"]["account_token"], record["projection"]["market"],
                record["projection"]["platform"],
            )
            for record in subject_records if record is not None
        }
        reasons = {"PARENT_LINEAGE_EVIDENCE_MISSING"}
        conflict = False
        if any(record is None for record in subject_records):
            reasons.add("SUBJECT_EXPERIMENT_MISSING")
            conflict = True
        if not evaluation_launch or len(subject_launch_tokens) != len(subjects) or any(not value for value in subject_launch_tokens):
            reasons.add("LAUNCH_TOKEN_MISSING")
        if len(launch_tokens) > 1:
            reasons.add("LAUNCH_TOKEN_CONFLICT")
            conflict = True
        if len(metadata) > 1:
            reasons.add("SUBJECT_METADATA_CONFLICT")
            conflict = True
        metadata_complete = len(metadata) == 1 and all(next(iter(metadata)))
        if not metadata_complete:
            reasons.add("SUBJECT_METADATA_MISSING")
        if len(subjects) == 1:
            reasons.add("SINGLE_EXPERIMENT_COMPONENT_INSUFFICIENT")
        if (
            evaluation_launch
            and len(subjects) >= 2
            and set(subjects) != experiment_members_by_launch.get(evaluation_launch, set())
        ):
            reasons.add("COMPONENT_MEMBERSHIP_CONFLICT")
            conflict = True
        launch_token = next(iter(launch_tokens)) if len(launch_tokens) == 1 else None
        component_eligible = (
            len(subjects) >= 2 and not conflict and evaluation_launch
            and len(subject_launch_tokens) == len(subjects)
            and all(value == evaluation_launch for value in subject_launch_tokens)
            and metadata_complete
        )
        membership_launch = evaluation_launch or launch_token
        if membership_launch and len(subjects) >= 2:
            launch_memberships.setdefault(membership_launch, set()).add(tuple(subjects))
        prelim.append({
            "source_table": item["source_table"], "source_id": item["source_id"],
            "source_kind": projection["source_kind"], "subject_experiment_ids": subjects,
            "launch_token": launch_token, "metadata": sorted([list(value) for value in metadata]),
            "reasons": reasons, "conflict": conflict, "record": item,
            "component_eligible": bool(component_eligible),
            "subject_records": [record for record in subject_records if record is not None],
        })
    entries: list[dict[str, Any]] = []
    component_map: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for item in prelim:
        if "entry_hash" in item:
            entries.append(item)
            continue
        reasons = set(item["reasons"])
        if item["launch_token"] and len(launch_memberships.get(item["launch_token"], set())) > 1:
            reasons.add("COMPONENT_MEMBERSHIP_CONFLICT")
            item["conflict"] = True
        component_id = None
        if item["component_eligible"]:
            key = (item["launch_token"], tuple(item["subject_experiment_ids"]))
            component_id = "component_" + canonical_hash({"launch_token": key[0], "members": list(key[1])})[:24]
            component = component_map.setdefault(key, {
                "component_id": component_id, "launch_token": key[0],
                "subject_experiment_ids": list(key[1]), "evaluation_refs": [],
                "status": "COMPONENT_RESOLVED_PARENT_UNRESOLVED", "reason_codes": [],
                "evidence_refs": [],
            })
            component["evaluation_refs"].append({"source_table": item["source_table"], "source_id": item["source_id"]})
            if item["conflict"]:
                component["status"] = "CONFLICT"
            component["reason_codes"] = sorted(set(component["reason_codes"]) | reasons)
            component["evidence_refs"].extend(_evidence_refs(item, table_hashes))
        if item["conflict"]:
            status = "CONFLICT"
        elif component_id is not None:
            status = "COMPONENT_RESOLVED_PARENT_UNRESOLVED"
        else:
            status = "UNRESOLVED_INSUFFICIENT_EVIDENCE"
        entry = {
            "source_table": item["source_table"], "source_id": item["source_id"],
            "source_kind": item["source_kind"], "subject_experiment_ids": item["subject_experiment_ids"],
            "component_id": component_id, "lineage_id": None, "lineage_status": status,
            "split": "UNASSIGNED", "reason_codes": sorted(reasons),
            "evidence_refs": _dedupe_refs(_evidence_refs(item, table_hashes)),
        }
        entry["entry_hash"] = canonical_hash(entry)
        entries.append(entry)
    components: list[dict[str, Any]] = []
    for component in component_map.values():
        component["evaluation_refs"] = sorted(
            {canonical_json(value): value for value in component["evaluation_refs"]}.values(),
            key=lambda value: (value["source_table"], value["source_id"]),
        )
        component["evidence_refs"] = _dedupe_refs(component["evidence_refs"])
        component["component_hash"] = canonical_hash(component)
        components.append(component)
    entries.sort(key=lambda value: (value["source_table"], value["source_id"]))
    components.sort(key=lambda value: value["component_id"])
    coverage = _candidate_coverage(entries, components)
    output = {
        "schema_version": CANDIDATE_VERSION, "engine_version": ENGINE_VERSION,
        "derivation_id": derivation_id, "derived_at": derived_at, "input_binding": binding,
        "lineage_candidates": entries, "components": components,
        "split_registry": {
            "policy_status": "UNFROZEN", "allowed_splits": ["DEV", "VALIDATION"],
            "assignments": [], "holdout_status": "LOCKED_NOT_ASSIGNED",
            "blocking_reasons": ["EXACT_PARENT_LINEAGE_UNRESOLVED", "DEV_VALIDATION_POLICY_UNFROZEN"],
        },
        "coverage": coverage, "trust_status": "UNSIGNED_LOCAL_DERIVATION",
        "evidence_use": "AUDIT_ONLY", "replay_eligible": False, "gate1_effect": "NONE",
        "not_dataset_receipt": True,
    }
    output["candidate_hash"] = canonical_hash(output)
    if not validate_output:
        return output
    return _validate_lineage_candidate_bundle_from_source(output, bundle, binding)


def validate_lineage_candidate_bundle(
    value: Mapping[str, Any], *, audit_dir: str | os.PathLike[str], expected_manifest_sha256: str,
) -> dict[str, Any]:
    source_bundle, source_binding = load_validated_audit_directory(
        audit_dir, expected_manifest_sha256=expected_manifest_sha256,
    )
    return _validate_lineage_candidate_bundle_from_source(value, source_bundle, source_binding)


def _validate_lineage_candidate_bundle_from_source(
    value: Mapping[str, Any], source_audit_bundle: Mapping[str, Any],
    source_input_binding: Mapping[str, Any],
) -> dict[str, Any]:
    keys = {
        "schema_version", "engine_version", "derivation_id", "derived_at", "input_binding",
        "lineage_candidates", "components", "split_registry", "coverage", "trust_status",
        "evidence_use", "replay_eligible", "gate1_effect", "not_dataset_receipt", "candidate_hash",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G102B_CANDIDATE_SCHEMA_INVALID")
    body = dict(value)
    if body["schema_version"] != CANDIDATE_VERSION or body["engine_version"] != ENGINE_VERSION:
        _fail("G102B_CANDIDATE_VERSION_INVALID")
    try:
        validate_utc(body["derived_at"], code="G102B_DERIVED_AT_INVALID")
    except ValueError as exc:
        raise HistoricalLineageCandidateError(str(exc)) from exc
    binding = body["input_binding"]
    binding_keys = {
        "input_manifest_sha256", "input_manifest_hash", "input_bundle_hash", "input_request_hash",
        "input_source_snapshot_hash", "input_authoritative_asof_hash", "input_table_manifest_hashes",
        "data_cutoff_at",
    }
    if not isinstance(binding, Mapping) or set(binding) != binding_keys:
        _fail("G102B_INPUT_BINDING_SCHEMA_INVALID")
    for key in binding_keys - {"data_cutoff_at", "input_table_manifest_hashes"}:
        try:
            validate_sha256(binding[key], code="G102B_INPUT_BINDING_HASH_INVALID")
        except ValueError as exc:
            raise HistoricalLineageCandidateError(str(exc)) from exc
    try:
        validate_utc(binding["data_cutoff_at"], code="G102B_INPUT_CUTOFF_INVALID")
    except ValueError as exc:
        raise HistoricalLineageCandidateError(str(exc)) from exc
    if _utc_datetime(body["derived_at"]) < _utc_datetime(binding["data_cutoff_at"]):
        _fail("G102B_DERIVED_BEFORE_CUTOFF")
    table_hashes = binding["input_table_manifest_hashes"]
    expected_tables = {item["table"] for item in TABLES}
    if not isinstance(table_hashes, Mapping) or set(table_hashes) != expected_tables:
        _fail("G102B_INPUT_TABLE_HASHES_INVALID")
    for value_hash in table_hashes.values():
        try:
            validate_sha256(value_hash, code="G102B_INPUT_TABLE_HASHES_INVALID")
        except ValueError as exc:
            raise HistoricalLineageCandidateError(str(exc)) from exc
    if not isinstance(body["lineage_candidates"], list) or not isinstance(body["components"], list):
        _fail("G102B_CANDIDATE_SCHEMA_INVALID")
    try:
        source_bundle = validate_audit_bundle(source_audit_bundle)
    except Exception as exc:
        raise HistoricalLineageCandidateError("G102B_INPUT_BUNDLE_INVALID") from exc
    _validate_input_binding(binding, source_bundle)
    if binding != source_input_binding:
        _fail("G102B_INPUT_MANIFEST_BINDING_MISMATCH")
    source_records = {
        (item["source_table"], item["source_id"]): item for item in source_bundle["records"]
    }
    expected_identities = {
        identity for identity in source_records if identity[0] in LEGACY_TABLES
    }
    identities: set[tuple[str, str]] = set()
    allowed_statuses = {"COMPONENT_RESOLVED_PARENT_UNRESOLVED", "UNRESOLVED_INSUFFICIENT_EVIDENCE", "CONFLICT"}
    for item in body["lineage_candidates"]:
        expected = {
            "source_table", "source_id", "source_kind", "subject_experiment_ids", "component_id",
            "lineage_id", "lineage_status", "split", "reason_codes", "evidence_refs", "entry_hash",
        }
        if not isinstance(item, Mapping) or set(item) != expected:
            _fail("G102B_ENTRY_SCHEMA_INVALID")
        identity = (item["source_table"], item["source_id"])
        if identity in identities or identity[0] not in LEGACY_TABLES or not identity[1]:
            _fail("G102B_ENTRY_IDENTITY_INVALID")
        identities.add(identity)
        source_record = source_records.get(identity)
        if source_record is None or source_record["record_hash"] != _own_record_hash(item):
            _fail("G102B_ENTRY_SOURCE_BINDING_INVALID")
        source_projection = source_record["projection"]
        expected_source_kind = (
            "INVALID" if source_projection.get("status") == "INVALID_LEGACY_PROJECTION"
            else source_projection["source_kind"]
        )
        expected_subjects = [] if expected_source_kind == "INVALID" else source_projection["subject_experiment_ids"]
        if item["source_kind"] != expected_source_kind or item["subject_experiment_ids"] != expected_subjects:
            _fail("G102B_ENTRY_SOURCE_BINDING_INVALID")
        expected_kind = LEGACY_KIND_BY_TABLE[identity[0]]
        if item["source_kind"] not in {expected_kind, "INVALID"}:
            _fail("G102B_ENTRY_KIND_INVALID")
        subjects = item["subject_experiment_ids"]
        if not isinstance(subjects, list) or subjects != sorted(set(subjects)):
            _fail("G102B_ENTRY_SUBJECTS_INVALID")
        if (
            (item["source_kind"] == "INVALID" and subjects)
            or (item["source_kind"] == "SINGLE_EXPERIMENT" and len(subjects) != 1)
            or (item["source_kind"] == "CREATIVE_GROUP" and not 2 <= len(subjects) <= 4)
            or (item["source_kind"] == "AUDIENCE_PAIR" and len(subjects) != 2)
        ):
            _fail("G102B_ENTRY_SUBJECTS_INVALID")
        if item["lineage_id"] is not None or item["split"] != "UNASSIGNED" or item["lineage_status"] not in allowed_statuses:
            _fail("G102B_ENTRY_ASSIGNMENT_FORBIDDEN")
        _validate_reasons(item["reason_codes"])
        _validate_evidence_refs(item["evidence_refs"], table_hashes, source_records=source_records)
        _validate_entry_semantics(item)
        if item["entry_hash"] != canonical_hash({key: val for key, val in item.items() if key != "entry_hash"}):
            _fail("G102B_ENTRY_HASH_MISMATCH")
    if identities != expected_identities:
        _fail("G102B_ENTRY_DENOMINATOR_MISMATCH")
    component_ids: set[str] = set()
    for item in body["components"]:
        expected = {
            "component_id", "launch_token", "subject_experiment_ids", "evaluation_refs", "status",
            "reason_codes", "evidence_refs", "component_hash",
        }
        if not isinstance(item, Mapping) or set(item) != expected or item["status"] not in {"COMPONENT_RESOLVED_PARENT_UNRESOLVED", "CONFLICT"}:
            _fail("G102B_COMPONENT_SCHEMA_INVALID")
        if item["component_id"] in component_ids or not re.fullmatch(r"component_[0-9a-f]{24}", item["component_id"]):
            _fail("G102B_COMPONENT_ID_INVALID")
        component_ids.add(item["component_id"])
        if not isinstance(item["launch_token"], str) or not re.fullmatch(r"launch_[0-9a-f]{24}", item["launch_token"]):
            _fail("G102B_COMPONENT_LAUNCH_INVALID")
        if not isinstance(item["subject_experiment_ids"], list) or len(item["subject_experiment_ids"]) < 2 or item["subject_experiment_ids"] != sorted(set(item["subject_experiment_ids"])):
            _fail("G102B_COMPONENT_MEMBERS_INVALID")
        expected_component_id = "component_" + canonical_hash({
            "launch_token": item["launch_token"], "members": item["subject_experiment_ids"],
        })[:24]
        if item["component_id"] != expected_component_id:
            _fail("G102B_COMPONENT_ID_INVALID")
        _validate_reasons(item["reason_codes"])
        _validate_evidence_refs(item["evidence_refs"], table_hashes, source_records=source_records)
        if item["component_hash"] != canonical_hash({key: val for key, val in item.items() if key != "component_hash"}):
            _fail("G102B_COMPONENT_HASH_MISMATCH")
    component_by_id = {item["component_id"]: item for item in body["components"]}
    component_entries: dict[str, list[Mapping[str, Any]]] = {key: [] for key in component_by_id}
    for item in body["lineage_candidates"]:
        component_id = item["component_id"]
        if component_id is None:
            if item["lineage_status"] == "COMPONENT_RESOLVED_PARENT_UNRESOLVED":
                _fail("G102B_ENTRY_COMPONENT_BINDING_INVALID")
            continue
        if component_id not in component_by_id or item["subject_experiment_ids"] != component_by_id[component_id]["subject_experiment_ids"]:
            _fail("G102B_ENTRY_COMPONENT_BINDING_INVALID")
        component_entries[component_id].append(item)
    for component_id, component in component_by_id.items():
        entries = component_entries[component_id]
        expected_refs = sorted(
            ({"source_table": item["source_table"], "source_id": item["source_id"]} for item in entries),
            key=lambda item: (item["source_table"], item["source_id"]),
        )
        if not entries or component["evaluation_refs"] != expected_refs:
            _fail("G102B_COMPONENT_ENTRY_CLOSURE_INVALID")
        expected_status = "CONFLICT" if any(item["lineage_status"] == "CONFLICT" for item in entries) else "COMPONENT_RESOLVED_PARENT_UNRESOLVED"
        expected_reasons = sorted({reason for item in entries for reason in item["reason_codes"]})
        expected_evidence = _dedupe_refs([ref for item in entries for ref in item["evidence_refs"]])
        if (
            component["status"] != expected_status
            or component["reason_codes"] != expected_reasons
            or component["evidence_refs"] != expected_evidence
        ):
            _fail("G102B_COMPONENT_ENTRY_CLOSURE_INVALID")
    split = body["split_registry"]
    if split != {
        "policy_status": "UNFROZEN", "allowed_splits": ["DEV", "VALIDATION"], "assignments": [],
        "holdout_status": "LOCKED_NOT_ASSIGNED",
        "blocking_reasons": ["EXACT_PARENT_LINEAGE_UNRESOLVED", "DEV_VALIDATION_POLICY_UNFROZEN"],
    }:
        _fail("G102B_SPLIT_REGISTRY_INVALID")
    if body["coverage"] != _candidate_coverage(body["lineage_candidates"], body["components"]):
        _fail("G102B_COVERAGE_MISMATCH")
    if (
        body["trust_status"] != "UNSIGNED_LOCAL_DERIVATION" or body["evidence_use"] != "AUDIT_ONLY"
        or body["replay_eligible"] is not False or body["gate1_effect"] != "NONE"
        or body["not_dataset_receipt"] is not True
    ):
        _fail("G102B_TRUST_CEILING_INVALID")
    if body["candidate_hash"] != canonical_hash({key: val for key, val in body.items() if key != "candidate_hash"}):
        _fail("G102B_CANDIDATE_HASH_MISMATCH")
    expected = _derive_lineage_candidates_from_validated(
        source_bundle, source_input_binding,
        derivation_id=body["derivation_id"], derived_at=body["derived_at"], validate_output=False,
    )
    if body != expected:
        _fail("G102B_DERIVATION_SEMANTICS_MISMATCH")
    return body


def write_lineage_candidate_bundle(
    value: Mapping[str, Any], output_dir: str | os.PathLike[str], *,
    audit_dir: str | os.PathLike[str], expected_manifest_sha256: str,
) -> dict[str, Any]:
    bundle = validate_lineage_candidate_bundle(
        value, audit_dir=audit_dir, expected_manifest_sha256=expected_manifest_sha256,
    )
    output = Path(output_dir)
    if output.exists():
        _fail("G102B_OUTPUT_EXISTS")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        payloads = {
            "lineage_candidates.ndjson": "".join(canonical_json(item) + "\n" for item in bundle["lineage_candidates"]).encode(),
            "components.ndjson": "".join(canonical_json(item) + "\n" for item in bundle["components"]).encode(),
            "coverage.json": (canonical_json(bundle["coverage"]) + "\n").encode(),
        }
        files = {}
        for name, payload in payloads.items():
            (temporary / name).write_bytes(payload)
            files[name] = {"sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)}
        manifest = {
            "schema_version": "gle-g1-02b-lineage-candidate-manifest-v1",
            "derivation_id": bundle["derivation_id"], "derived_at": bundle["derived_at"],
            "candidate_hash": bundle["candidate_hash"], "input_binding": bundle["input_binding"],
            "split_registry": bundle["split_registry"], "coverage": bundle["coverage"],
            "trust_status": bundle["trust_status"], "evidence_use": bundle["evidence_use"],
            "replay_eligible": bundle["replay_eligible"], "gate1_effect": bundle["gate1_effect"],
            "not_dataset_receipt": bundle["not_dataset_receipt"], "files": files,
        }
        manifest["manifest_hash"] = canonical_hash(manifest)
        (temporary / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
        temporary.replace(output)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _validate_input_binding(value: Mapping[str, Any], bundle: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "input_manifest_sha256", "input_manifest_hash", "input_bundle_hash", "input_request_hash",
        "input_source_snapshot_hash", "input_authoritative_asof_hash", "input_table_manifest_hashes",
        "data_cutoff_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        _fail("G102B_INPUT_BINDING_SCHEMA_INVALID")
    binding = dict(value)
    for key in expected - {"data_cutoff_at", "input_table_manifest_hashes"}:
        try:
            validate_sha256(binding[key], code="G102B_INPUT_BINDING_HASH_INVALID")
        except ValueError as exc:
            raise HistoricalLineageCandidateError(str(exc)) from exc
    if (
        binding["input_bundle_hash"] != bundle["bundle_hash"]
        or binding["input_request_hash"] != bundle["request"]["request_hash"]
        or binding["input_source_snapshot_hash"] != bundle["source_snapshot"]["source_snapshot_hash"]
        or binding["input_authoritative_asof_hash"] != bundle["source_snapshot"]["authoritative_asof_hash"]
        or binding["data_cutoff_at"] != bundle["request"]["data_cutoff_at"]
        or binding["input_table_manifest_hashes"] != {
            item["table"]: item["table_manifest_hash"] for item in bundle["table_manifests"]
        }
    ):
        _fail("G102B_INPUT_BINDING_MISMATCH")
    return binding


def _invalid_entry(item: Mapping[str, Any], table_manifest_hash: str) -> dict[str, Any]:
    entry = {
        "source_table": item["source_table"], "source_id": item["source_id"], "source_kind": "INVALID",
        "subject_experiment_ids": [], "component_id": None, "lineage_id": None,
        "lineage_status": "UNRESOLVED_INSUFFICIENT_EVIDENCE", "split": "UNASSIGNED",
        "reason_codes": ["INVALID_LEGACY_PROJECTION", "PARENT_LINEAGE_EVIDENCE_MISSING"],
        "evidence_refs": [{
            "source_table": item["source_table"], "source_id": item["source_id"],
            "record_hash": item["record_hash"], "table_manifest_hash": table_manifest_hash,
            "field_paths": ["projection.status"],
        }],
    }
    entry["entry_hash"] = canonical_hash(entry)
    return entry


def _evaluation_launch_token(projection: Mapping[str, Any]) -> str:
    summary = projection.get("evidence_summary")
    if not isinstance(summary, Mapping):
        return ""
    wrapper = summary.get("launch_id")
    if isinstance(wrapper, Mapping) and wrapper.get("status") == "PRESENT":
        return str(wrapper.get("safe_value") or "")
    return ""


def _evidence_refs(item: Mapping[str, Any], table_hashes: Mapping[str, str]) -> list[dict[str, Any]]:
    record = item["record"]
    own_paths = ["projection.subject_experiment_ids"]
    if item["source_kind"] in {"CREATIVE_GROUP", "AUDIENCE_PAIR"}:
        own_paths.append("projection.evidence_summary.launch_id")
    refs = [{
        "source_table": record["source_table"], "source_id": record["source_id"],
        "record_hash": record["record_hash"], "table_manifest_hash": table_hashes[record["source_table"]],
        "field_paths": sorted(own_paths),
    }]
    for subject in item["subject_records"]:
        refs.append({
            "source_table": "ad_experiment", "source_id": subject["source_id"],
            "record_hash": subject["record_hash"], "table_manifest_hash": table_hashes["ad_experiment"],
            "field_paths": sorted([
                "projection.experiment_id", "projection.launch_token", "projection.account_token",
                "projection.market", "projection.platform",
            ]),
        })
    return refs


def _dedupe_refs(values: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        {canonical_json(value): dict(value) for value in values}.values(),
        key=lambda value: (value["source_table"], value["source_id"], value["field_paths"]),
    )


def _validate_evidence_refs(
    values: Any, table_hashes: Mapping[str, str], *,
    source_records: Mapping[tuple[str, str], Mapping[str, Any]],
) -> None:
    expected = {"source_table", "source_id", "record_hash", "table_manifest_hash", "field_paths"}
    if not isinstance(values, list) or not values:
        _fail("G102B_EVIDENCE_REFS_INVALID")
    if values != _dedupe_refs(values):
        _fail("G102B_EVIDENCE_REFS_INVALID")
    for item in values:
        if not isinstance(item, Mapping) or set(item) != expected or not item["source_id"]:
            _fail("G102B_EVIDENCE_REFS_INVALID")
        if item["source_table"] not in table_hashes or item["table_manifest_hash"] != table_hashes[item["source_table"]]:
            _fail("G102B_EVIDENCE_REFS_INVALID")
        source_record = source_records.get((item["source_table"], item["source_id"]))
        if source_record is None or source_record["record_hash"] != item["record_hash"]:
            _fail("G102B_EVIDENCE_SOURCE_BINDING_INVALID")
        try:
            validate_sha256(item["record_hash"], code="G102B_EVIDENCE_HASH_INVALID")
            validate_sha256(item["table_manifest_hash"], code="G102B_EVIDENCE_HASH_INVALID")
        except ValueError as exc:
            raise HistoricalLineageCandidateError(str(exc)) from exc
        if not isinstance(item["field_paths"], list) or item["field_paths"] != sorted(set(item["field_paths"])):
            _fail("G102B_EVIDENCE_REFS_INVALID")


def _validate_reasons(values: Any) -> None:
    if not isinstance(values, list) or not values or values != sorted(set(values)):
        _fail("G102B_REASON_CODES_INVALID")
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", value) for value in values):
        _fail("G102B_REASON_CODES_INVALID")
    if not set(values) <= REASON_CODES or "PARENT_LINEAGE_EVIDENCE_MISSING" not in values:
        _fail("G102B_REASON_CODES_INVALID")


def _own_record_hash(item: Mapping[str, Any]) -> str:
    values = item.get("evidence_refs")
    if not isinstance(values, list):
        return ""
    matches = [
        ref for ref in values if isinstance(ref, Mapping)
        and ref.get("source_table") == item.get("source_table")
        and ref.get("source_id") == item.get("source_id")
    ]
    return str(matches[0].get("record_hash") or "") if len(matches) == 1 else ""


def _utc_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)


def _validate_entry_semantics(item: Mapping[str, Any]) -> None:
    reasons = set(item["reason_codes"])
    own_refs = [
        ref for ref in item["evidence_refs"]
        if ref["source_table"] == item["source_table"] and ref["source_id"] == item["source_id"]
    ]
    experiment_refs = [ref for ref in item["evidence_refs"] if ref["source_table"] == "ad_experiment"]
    experiment_ids = sorted(ref["source_id"] for ref in experiment_refs)
    subjects = item["subject_experiment_ids"]
    if (
        len(own_refs) != 1
        or experiment_ids != sorted(set(experiment_ids))
        or not set(experiment_ids) <= set(subjects)
        or (("SUBJECT_EXPERIMENT_MISSING" in reasons) != (len(experiment_ids) < len(subjects)))
    ):
        _fail("G102B_ENTRY_EVIDENCE_CLOSURE_INVALID")
    if item["source_kind"] == "INVALID":
        if (
            item["component_id"] is not None
            or item["lineage_status"] != "UNRESOLVED_INSUFFICIENT_EVIDENCE"
            or reasons != {"INVALID_LEGACY_PROJECTION", "PARENT_LINEAGE_EVIDENCE_MISSING"}
            or experiment_refs
            or own_refs[0]["field_paths"] != ["projection.status"]
        ):
            _fail("G102B_ENTRY_STATE_INVALID")
        return
    if "INVALID_LEGACY_PROJECTION" in reasons:
        _fail("G102B_ENTRY_STATE_INVALID")
    expected_own_paths = ["projection.subject_experiment_ids"]
    if item["source_kind"] in {"CREATIVE_GROUP", "AUDIENCE_PAIR"}:
        expected_own_paths.append("projection.evidence_summary.launch_id")
    if own_refs[0]["field_paths"] != sorted(expected_own_paths):
        _fail("G102B_ENTRY_EVIDENCE_CLOSURE_INVALID")
    expected_experiment_paths = sorted([
        "projection.experiment_id", "projection.launch_token", "projection.account_token",
        "projection.market", "projection.platform",
    ])
    if any(ref["field_paths"] != expected_experiment_paths for ref in experiment_refs):
        _fail("G102B_ENTRY_EVIDENCE_CLOSURE_INVALID")
    conflict = bool(reasons & CONFLICT_REASONS)
    if (item["lineage_status"] == "CONFLICT") != conflict:
        _fail("G102B_ENTRY_STATE_INVALID")
    if item["component_id"] is None:
        if item["lineage_status"] not in {"UNRESOLVED_INSUFFICIENT_EVIDENCE", "CONFLICT"}:
            _fail("G102B_ENTRY_STATE_INVALID")
        if (
            item["source_kind"] != "SINGLE_EXPERIMENT" and not conflict
            and not reasons & {"LAUNCH_TOKEN_MISSING", "SUBJECT_METADATA_MISSING"}
        ):
            _fail("G102B_ENTRY_STATE_INVALID")
    elif item["lineage_status"] not in {"COMPONENT_RESOLVED_PARENT_UNRESOLVED", "CONFLICT"}:
        _fail("G102B_ENTRY_STATE_INVALID")
    elif reasons & {"LAUNCH_TOKEN_MISSING", "SUBJECT_METADATA_MISSING"}:
        _fail("G102B_ENTRY_STATE_INVALID")
    if item["source_kind"] == "SINGLE_EXPERIMENT" and "SINGLE_EXPERIMENT_COMPONENT_INSUFFICIENT" not in reasons:
        _fail("G102B_ENTRY_STATE_INVALID")
    if item["source_kind"] != "SINGLE_EXPERIMENT" and "SINGLE_EXPERIMENT_COMPONENT_INSUFFICIENT" in reasons:
        _fail("G102B_ENTRY_STATE_INVALID")


def _candidate_coverage(entries: list[Mapping[str, Any]], components: list[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "legacy_evaluation_count": len(entries),
        "component_candidate_count": len(components),
        "component_resolved_parent_unresolved_count": sum(
            item["lineage_status"] == "COMPONENT_RESOLVED_PARENT_UNRESOLVED" for item in entries
        ),
        "unresolved_count": sum(item["lineage_status"] == "UNRESOLVED_INSUFFICIENT_EVIDENCE" for item in entries),
        "conflict_count": sum(item["lineage_status"] == "CONFLICT" for item in entries),
        "dev_assignment_count": 0, "validation_assignment_count": 0, "holdout_assignment_count": 0,
    }


def _read_regular(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise HistoricalLineageCandidateError("G102B_INPUT_FILE_INVALID") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            _fail("G102B_INPUT_FILE_INVALID")
        data = b""
        while len(data) <= MAX_FILE_BYTES:
            chunk = os.read(fd, min(1024 * 1024, MAX_FILE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        after = os.fstat(fd)
        if len(data) > MAX_FILE_BYTES or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            _fail("G102B_INPUT_FILE_DRIFT")
        return data
    finally:
        os.close(fd)


def _json_load(raw: bytes, code: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail(code)
            result[key] = value
        return result

    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=pairs,
            parse_constant=lambda _value: _fail(code),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalLineageCandidateError(code) from exc


def _canonical_json_document(raw: bytes, code: str) -> Any:
    value = _json_load(raw, code)
    try:
        expected = (canonical_json(value) + "\n").encode("utf-8")
    except ValueError as exc:
        raise HistoricalLineageCandidateError(code) from exc
    if raw != expected:
        _fail(code)
    return value


def _canonical_ndjson(raw: bytes, code: str) -> list[dict[str, Any]]:
    if not raw.endswith(b"\n") and raw:
        _fail(code)
    lines = raw.splitlines()
    if len(lines) > MAX_NDJSON_ROWS or any(not line for line in lines):
        _fail(code)
    result = []
    for line in lines:
        value = _json_load(line, code)
        try:
            expected = canonical_json(value).encode("utf-8")
        except ValueError as exc:
            raise HistoricalLineageCandidateError(code) from exc
        if not isinstance(value, Mapping) or line != expected:
            _fail(code)
        result.append(dict(value))
    return result
