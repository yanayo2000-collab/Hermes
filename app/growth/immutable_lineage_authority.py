from __future__ import annotations

import base64
import hashlib
import re
import shutil
import subprocess
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from app.growth.canonical_evaluation_contracts import (
    canonical_hash,
    canonical_json,
    validate_sha256,
    validate_utc,
)
from app.growth.historical_lineage_candidates import (
    CANDIDATE_MANIFEST_VERSION,
    CANDIDATE_VERSION,
    ENGINE_VERSION as CANDIDATE_ENGINE_VERSION,
    HistoricalLineageCandidateError,
    _canonical_json_document,
    _canonical_ndjson,
    _read_regular,
    validate_lineage_candidate_bundle,
)


REQUEST_VERSION = "gle-g1-02b2-lineage-authority-request-v1"
RESPONSE_VERSION = "gle-g1-02b2-lineage-authority-response-v1"
FRAGMENT_VERSION = "gle-g1-02b2-lineage-authority-fragment-v1"
REGISTRY_VERSION = "gle-g1-02b2-trusted-key-registry-v1"
CONTRACT_VERSION = "gle-g1-02b2-immutable-lineage-authority-v1"
ARTIFACT_MANIFEST_VERSION = "gle-g1-02b2-lineage-authority-artifact-manifest-v1"
EXACT_CANDIDATE_FILES = frozenset({
    "manifest.json", "lineage_candidates.ndjson", "components.ndjson", "coverage.json",
})
EXACT_AUTHORITY_FILES = frozenset({
    "manifest.json", "authority-request.json", "authority-response.json",
    "trusted-key-registry.json", "authority-fragment.json",
})
REQUIRED_ROLES = ("BUSINESS_OWNER", "DATA_OWNER", "TECH_OWNER")
SIGNATURE_PURPOSE = "LINEAGE_AUTHORITY_ATTESTATION"
SIGNATURE_ALGORITHM = "RSA_PKCS1_V1_5_SHA256"
OPENSSL_BINARY = "/usr/bin/openssl"
MINIMUM_RSA_BITS = 2048
FORBIDDEN_INFERENCES = (
    "ACCOUNT_MATCH", "CREATED_AT_PROXIMITY", "LAUNCH_TOKEN", "NAME_SIMILARITY",
    "OBJECT_ID_REUSE", "WINNER_RELATION",
)
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_LINEAGE_RE = re.compile(r"lineage_[0-9a-f]{24}")
_EXCLUSION_RE = re.compile(r"exclusion_[0-9a-f]{24}")
_PEM_RE = re.compile(
    r"-----BEGIN PUBLIC KEY-----\n(?:[A-Za-z0-9+/=]+\n)+-----END PUBLIC KEY-----\n?"
)


class ImmutableLineageAuthorityError(RuntimeError):
    pass


def _fail(code: str) -> None:
    raise ImmutableLineageAuthorityError(code)


def load_validated_candidate_directory(
    candidate_dir: str | Path,
    *,
    expected_candidate_manifest_sha256: str,
    audit_dir: str | Path,
    expected_audit_manifest_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _hash(expected_candidate_manifest_sha256, "G102B2_CANDIDATE_MANIFEST_HASH_INVALID")
    root_input = Path(candidate_dir).expanduser()
    if root_input.is_symlink():
        _fail("G102B2_CANDIDATE_DIRECTORY_INVALID")
    root = root_input.resolve()
    if not root.is_dir() or {item.name for item in root.iterdir()} != EXACT_CANDIDATE_FILES:
        _fail("G102B2_CANDIDATE_FILE_SET_INVALID")
    try:
        raw = {name: _read_regular(root / name) for name in sorted(EXACT_CANDIDATE_FILES)}
    except HistoricalLineageCandidateError as exc:
        raise ImmutableLineageAuthorityError("G102B2_CANDIDATE_FILE_INVALID") from exc
    manifest_sha = hashlib.sha256(raw["manifest.json"]).hexdigest()
    if manifest_sha != expected_candidate_manifest_sha256:
        _fail("G102B2_CANDIDATE_MANIFEST_ANCHOR_MISMATCH")
    try:
        manifest = _canonical_json_document(
            raw["manifest.json"], "G102B2_CANDIDATE_MANIFEST_JSON_INVALID",
        )
        entries = _canonical_ndjson(raw["lineage_candidates.ndjson"], "G102B2_ENTRIES_INVALID")
        components = _canonical_ndjson(raw["components.ndjson"], "G102B2_COMPONENTS_INVALID")
        coverage = _canonical_json_document(raw["coverage.json"], "G102B2_COVERAGE_INVALID")
    except HistoricalLineageCandidateError as exc:
        raise ImmutableLineageAuthorityError(str(exc)) from exc
    manifest_keys = {
        "schema_version", "derivation_id", "derived_at", "candidate_hash", "input_binding",
        "split_registry", "coverage", "trust_status", "evidence_use", "replay_eligible",
        "gate1_effect", "not_dataset_receipt", "files", "manifest_hash",
    }
    if (
        not isinstance(manifest, Mapping)
        or set(manifest) != manifest_keys
        or manifest["schema_version"] != CANDIDATE_MANIFEST_VERSION
        or manifest["manifest_hash"] != canonical_hash({
            key: value for key, value in manifest.items() if key != "manifest_hash"
        })
    ):
        _fail("G102B2_CANDIDATE_MANIFEST_INVALID")
    expected_payload_files = EXACT_CANDIDATE_FILES - {"manifest.json"}
    if not isinstance(manifest["files"], Mapping) or set(manifest["files"]) != expected_payload_files:
        _fail("G102B2_CANDIDATE_FILE_SET_INVALID")
    for name in sorted(expected_payload_files):
        descriptor = manifest["files"][name]
        if not isinstance(descriptor, Mapping) or set(descriptor) != {"sha256", "size_bytes"}:
            _fail("G102B2_CANDIDATE_FILE_DESCRIPTOR_INVALID")
        _hash(descriptor["sha256"], "G102B2_CANDIDATE_FILE_HASH_INVALID")
        if (
            type(descriptor["size_bytes"]) is not int
            or descriptor["size_bytes"] < 0
            or len(raw[name]) != descriptor["size_bytes"]
            or hashlib.sha256(raw[name]).hexdigest() != descriptor["sha256"]
        ):
            _fail("G102B2_CANDIDATE_FILE_INTEGRITY_MISMATCH")
    candidate = {
        "schema_version": CANDIDATE_VERSION,
        "engine_version": CANDIDATE_ENGINE_VERSION,
        "derivation_id": manifest["derivation_id"],
        "derived_at": manifest["derived_at"],
        "input_binding": manifest["input_binding"],
        "lineage_candidates": entries,
        "components": components,
        "split_registry": manifest["split_registry"],
        "coverage": coverage,
        "trust_status": manifest["trust_status"],
        "evidence_use": manifest["evidence_use"],
        "replay_eligible": manifest["replay_eligible"],
        "gate1_effect": manifest["gate1_effect"],
        "not_dataset_receipt": manifest["not_dataset_receipt"],
        "candidate_hash": manifest["candidate_hash"],
    }
    try:
        candidate = validate_lineage_candidate_bundle(
            candidate,
            audit_dir=audit_dir,
            expected_manifest_sha256=expected_audit_manifest_sha256,
        )
    except Exception as exc:
        raise ImmutableLineageAuthorityError("G102B2_CANDIDATE_SOURCE_BINDING_INVALID") from exc
    for key in (
        "candidate_hash", "input_binding", "split_registry", "coverage", "trust_status",
        "evidence_use", "replay_eligible", "gate1_effect", "not_dataset_receipt",
    ):
        if manifest[key] != candidate[key]:
            _fail("G102B2_CANDIDATE_MANIFEST_BINDING_MISMATCH")
    binding = {
        "audit_manifest_sha256": expected_audit_manifest_sha256,
        "candidate_manifest_sha256": manifest_sha,
        "candidate_manifest_hash": manifest["manifest_hash"],
        "candidate_hash": candidate["candidate_hash"],
        "audit_bundle_hash": candidate["input_binding"]["input_bundle_hash"],
        "audit_request_hash": candidate["input_binding"]["input_request_hash"],
        "audit_source_snapshot_hash": candidate["input_binding"]["input_source_snapshot_hash"],
        "audit_authoritative_asof_hash": candidate["input_binding"]["input_authoritative_asof_hash"],
        "data_cutoff_at": candidate["input_binding"]["data_cutoff_at"],
    }
    return candidate, binding


def build_authority_request(
    *,
    candidate_dir: str | Path,
    expected_candidate_manifest_sha256: str,
    audit_dir: str | Path,
    expected_audit_manifest_sha256: str,
    request_id: str,
    requested_at: str,
    evaluated_at: str,
) -> dict[str, Any]:
    candidate, binding = load_validated_candidate_directory(
        candidate_dir,
        expected_candidate_manifest_sha256=expected_candidate_manifest_sha256,
        audit_dir=audit_dir,
        expected_audit_manifest_sha256=expected_audit_manifest_sha256,
    )
    return _build_request(
        candidate,
        binding,
        request_id=request_id,
        requested_at=requested_at,
        evaluated_at=evaluated_at,
    )


def validate_authority_request(
    value: Mapping[str, Any],
    *,
    candidate_dir: str | Path,
    expected_candidate_manifest_sha256: str,
    audit_dir: str | Path,
    expected_audit_manifest_sha256: str,
) -> dict[str, Any]:
    candidate, binding = load_validated_candidate_directory(
        candidate_dir,
        expected_candidate_manifest_sha256=expected_candidate_manifest_sha256,
        audit_dir=audit_dir,
        expected_audit_manifest_sha256=expected_audit_manifest_sha256,
    )
    request = _validate_request_shape(value)
    expected = _build_request(
        candidate,
        binding,
        request_id=request["request_id"],
        requested_at=request["requested_at"],
        evaluated_at=request["evaluated_at"],
    )
    if request != expected:
        _fail("G102B2_REQUEST_SOURCE_SEMANTICS_MISMATCH")
    return request


def evaluate_authority_response(
    request: Mapping[str, Any],
    response: Mapping[str, Any] | None,
    *,
    trusted_key_registry: Mapping[str, Any] | None,
    expected_key_registry_hash: str | None,
    candidate_dir: str | Path,
    expected_candidate_manifest_sha256: str,
    audit_dir: str | Path,
    expected_audit_manifest_sha256: str,
) -> dict[str, Any]:
    response_snapshot = deepcopy(response)
    registry_snapshot = deepcopy(trusted_key_registry)
    request = validate_authority_request(
        request,
        candidate_dir=candidate_dir,
        expected_candidate_manifest_sha256=expected_candidate_manifest_sha256,
        audit_dir=audit_dir,
        expected_audit_manifest_sha256=expected_audit_manifest_sha256,
    )
    if response_snapshot is None:
        return _fragment(request, None, status="MISSING", reasons=["SIGNED_AUTHORITY_RESPONSE_MISSING"])
    try:
        if registry_snapshot is None or expected_key_registry_hash is None:
            _fail("G102B2_TRUST_ROOT_MISSING")
        registry = _validate_key_registry(
            registry_snapshot, expected_key_registry_hash=expected_key_registry_hash,
        )
        response_snapshot = _validate_response(request, response_snapshot, registry)
    except ImmutableLineageAuthorityError as exc:
        code = str(exc).split(":", 1)[0]
        status = "CONFLICT" if code in {
            "G102B2_DENOMINATOR_CONFLICT", "G102B2_LINEAGE_GRAPH_CONFLICT",
            "G102B2_COMPONENT_CONFLICT", "G102B2_EXPERIMENT_MEMBERSHIP_CONFLICT",
        } else "INVALID"
        return _fragment(request, response_snapshot, status=status, reasons=[code])
    return _fragment(request, response_snapshot, status="VERIFIED", reasons=[])


def write_authority_artifacts(
    request: Mapping[str, Any],
    fragment: Mapping[str, Any],
    output_dir: str | Path,
    *,
    response: Mapping[str, Any] | None,
    trusted_key_registry: Mapping[str, Any] | None,
    expected_key_registry_hash: str | None,
    candidate_dir: str | Path,
    expected_candidate_manifest_sha256: str,
    audit_dir: str | Path,
    expected_audit_manifest_sha256: str,
) -> dict[str, Any]:
    response_snapshot = deepcopy(response)
    registry_snapshot = deepcopy(trusted_key_registry)
    request = validate_authority_request(
        request,
        candidate_dir=candidate_dir,
        expected_candidate_manifest_sha256=expected_candidate_manifest_sha256,
        audit_dir=audit_dir,
        expected_audit_manifest_sha256=expected_audit_manifest_sha256,
    )
    expected_fragment = evaluate_authority_response(
        request,
        response_snapshot,
        trusted_key_registry=registry_snapshot,
        expected_key_registry_hash=expected_key_registry_hash,
        candidate_dir=candidate_dir,
        expected_candidate_manifest_sha256=expected_candidate_manifest_sha256,
        audit_dir=audit_dir,
        expected_audit_manifest_sha256=expected_audit_manifest_sha256,
    )
    fragment = _validate_fragment(fragment, request)
    if fragment != expected_fragment:
        _fail("G102B2_FRAGMENT_DERIVATION_MISMATCH")
    output = Path(output_dir)
    if output.exists():
        _fail("G102B2_OUTPUT_EXISTS")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        payloads = {
            "authority-request.json": (canonical_json(request) + "\n").encode(),
            "authority-response.json": (canonical_json(response_snapshot) + "\n").encode(),
            "trusted-key-registry.json": (
                canonical_json(registry_snapshot) + "\n"
            ).encode(),
            "authority-fragment.json": (canonical_json(fragment) + "\n").encode(),
        }
        files = {}
        for name, payload in payloads.items():
            (temporary / name).write_bytes(payload)
            files[name] = {"sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)}
        manifest = {
            "schema_version": ARTIFACT_MANIFEST_VERSION,
            "request_id": request["request_id"],
            "request_hash": request["request_hash"],
            "fragment_hash": fragment["fragment_hash"],
            "status": fragment["status"],
            "trust_status": fragment["trust_status"],
            "authority_effect": fragment["authority_effect"],
            "expected_key_registry_hash": expected_key_registry_hash,
            "split_assignment_count": 0,
            "split_effect": "NONE",
            "holdout_status": "LOCKED_NOT_ASSIGNED",
            "replay_eligible": False,
            "golden_eligible": False,
            "gate1_effect": "NONE",
            "not_dataset_receipt": True,
            "not_gate_receipt": True,
            "files": files,
        }
        manifest["manifest_hash"] = canonical_hash(manifest)
        (temporary / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
        temporary.replace(output)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def load_validated_authority_directory(
    authority_dir: str | Path,
    *,
    expected_authority_manifest_sha256: str,
    expected_key_registry_hash: str | None,
    candidate_dir: str | Path,
    expected_candidate_manifest_sha256: str,
    audit_dir: str | Path,
    expected_audit_manifest_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _hash(expected_authority_manifest_sha256, "G102B2_ARTIFACT_MANIFEST_ANCHOR_INVALID")
    root_input = Path(authority_dir).expanduser()
    if root_input.is_symlink():
        _fail("G102B2_ARTIFACT_DIRECTORY_INVALID")
    root = root_input.resolve()
    if not root.is_dir() or {item.name for item in root.iterdir()} != EXACT_AUTHORITY_FILES:
        _fail("G102B2_ARTIFACT_FILE_SET_INVALID")
    try:
        raw = {name: _read_regular(root / name) for name in sorted(EXACT_AUTHORITY_FILES)}
        manifest = _canonical_json_document(
            raw["manifest.json"], "G102B2_ARTIFACT_MANIFEST_INVALID",
        )
        request = _canonical_json_document(
            raw["authority-request.json"], "G102B2_ARTIFACT_REQUEST_INVALID",
        )
        response = _canonical_json_document(
            raw["authority-response.json"], "G102B2_ARTIFACT_RESPONSE_INVALID",
        )
        registry = _canonical_json_document(
            raw["trusted-key-registry.json"], "G102B2_ARTIFACT_REGISTRY_INVALID",
        )
        fragment = _canonical_json_document(
            raw["authority-fragment.json"], "G102B2_ARTIFACT_FRAGMENT_INVALID",
        )
    except HistoricalLineageCandidateError as exc:
        raise ImmutableLineageAuthorityError(str(exc)) from exc
    if hashlib.sha256(raw["manifest.json"]).hexdigest() != expected_authority_manifest_sha256:
        _fail("G102B2_ARTIFACT_MANIFEST_ANCHOR_MISMATCH")
    manifest_keys = {
        "schema_version", "request_id", "request_hash", "fragment_hash", "status",
        "trust_status", "authority_effect", "expected_key_registry_hash",
        "split_assignment_count", "split_effect", "holdout_status", "replay_eligible",
        "golden_eligible", "gate1_effect", "not_dataset_receipt", "not_gate_receipt",
        "files", "manifest_hash",
    }
    if not isinstance(manifest, Mapping) or set(manifest) != manifest_keys:
        _fail("G102B2_ARTIFACT_MANIFEST_INVALID")
    if (
        manifest["schema_version"] != ARTIFACT_MANIFEST_VERSION
        or manifest["manifest_hash"] != canonical_hash({
            key: value for key, value in manifest.items() if key != "manifest_hash"
        })
    ):
        _fail("G102B2_ARTIFACT_MANIFEST_INVALID")
    payload_names = EXACT_AUTHORITY_FILES - {"manifest.json"}
    if not isinstance(manifest["files"], Mapping) or set(manifest["files"]) != payload_names:
        _fail("G102B2_ARTIFACT_FILE_SET_INVALID")
    for name in sorted(payload_names):
        descriptor = manifest["files"][name]
        if not isinstance(descriptor, Mapping) or set(descriptor) != {"sha256", "size_bytes"}:
            _fail("G102B2_ARTIFACT_FILE_DESCRIPTOR_INVALID")
        _hash(descriptor["sha256"], "G102B2_ARTIFACT_FILE_DESCRIPTOR_INVALID")
        if (
            type(descriptor["size_bytes"]) is not int
            or descriptor["size_bytes"] < 0
            or descriptor["size_bytes"] != len(raw[name])
            or descriptor["sha256"] != hashlib.sha256(raw[name]).hexdigest()
        ):
            _fail("G102B2_ARTIFACT_FILE_INTEGRITY_MISMATCH")
    if not isinstance(request, Mapping) or not isinstance(fragment, Mapping):
        _fail("G102B2_ARTIFACT_PAYLOAD_INVALID")
    if response is not None and not isinstance(response, Mapping):
        _fail("G102B2_ARTIFACT_RESPONSE_INVALID")
    if registry is not None and not isinstance(registry, Mapping):
        _fail("G102B2_ARTIFACT_REGISTRY_INVALID")
    if manifest["expected_key_registry_hash"] != expected_key_registry_hash:
        _fail("G102B2_ARTIFACT_KEY_REGISTRY_ANCHOR_MISMATCH")
    validated_request = validate_authority_request(
        request,
        candidate_dir=candidate_dir,
        expected_candidate_manifest_sha256=expected_candidate_manifest_sha256,
        audit_dir=audit_dir,
        expected_audit_manifest_sha256=expected_audit_manifest_sha256,
    )
    expected_fragment = evaluate_authority_response(
        validated_request,
        response,
        trusted_key_registry=registry,
        expected_key_registry_hash=expected_key_registry_hash,
        candidate_dir=candidate_dir,
        expected_candidate_manifest_sha256=expected_candidate_manifest_sha256,
        audit_dir=audit_dir,
        expected_audit_manifest_sha256=expected_audit_manifest_sha256,
    )
    validated_fragment = _validate_fragment(fragment, validated_request)
    if validated_fragment != expected_fragment:
        _fail("G102B2_ARTIFACT_FRAGMENT_DERIVATION_MISMATCH")
    expected_manifest_projection = {
        "request_id": validated_request["request_id"],
        "request_hash": validated_request["request_hash"],
        "fragment_hash": validated_fragment["fragment_hash"],
        "status": validated_fragment["status"],
        "trust_status": validated_fragment["trust_status"],
        "authority_effect": validated_fragment["authority_effect"],
        "expected_key_registry_hash": expected_key_registry_hash,
        "split_assignment_count": 0,
        "split_effect": validated_fragment["split_effect"],
        "holdout_status": validated_fragment["holdout_status"],
        "replay_eligible": validated_fragment["replay_eligible"],
        "golden_eligible": validated_fragment["golden_eligible"],
        "gate1_effect": validated_fragment["gate1_effect"],
        "not_dataset_receipt": validated_fragment["not_dataset_receipt"],
        "not_gate_receipt": validated_fragment["not_gate_receipt"],
    }
    if any(manifest[key] != value for key, value in expected_manifest_projection.items()):
        _fail("G102B2_ARTIFACT_MANIFEST_BINDING_MISMATCH")
    return validated_request, validated_fragment


def lineage_id_for_root(canonical_experiment_id: str, spec_hash: str) -> str:
    _identifier(canonical_experiment_id, "G102B2_CANONICAL_EXPERIMENT_ID_INVALID")
    _hash(spec_hash, "G102B2_SPEC_HASH_INVALID")
    return "lineage_" + canonical_hash({
        "root_canonical_experiment_id": canonical_experiment_id,
        "root_spec_hash": spec_hash,
    })[:24]


def signature_message(
    payload_hash: str,
    *,
    key_registry_hash: str,
    key_id: str,
    signer_id: str,
    role: str,
) -> bytes:
    _hash(payload_hash, "G102B2_AUTHORITY_PAYLOAD_HASH_INVALID")
    _hash(key_registry_hash, "G102B2_KEY_REGISTRY_ANCHOR_INVALID")
    _identifier(key_id, "G102B2_SIGNATURE_INVALID")
    _identifier(signer_id, "G102B2_SIGNATURE_INVALID")
    if role not in REQUIRED_ROLES:
        _fail("G102B2_SIGNATURE_INVALID")
    return (
        "GLE_LINEAGE_AUTHORITY_V1\n"
        + canonical_json({
            "key_registry_hash": key_registry_hash,
            "key_id": key_id,
            "object_hash": payload_hash,
            "purpose": SIGNATURE_PURPOSE,
            "role": role,
            "signer_id": signer_id,
        })
        + "\n"
    ).encode()


def _build_request(
    candidate: Mapping[str, Any], binding: Mapping[str, Any], *, request_id: str,
    requested_at: str, evaluated_at: str,
) -> dict[str, Any]:
    _identifier(request_id, "G102B2_REQUEST_ID_INVALID")
    requested_at = _utc(requested_at, "G102B2_REQUESTED_AT_INVALID")
    evaluated_at = _utc(evaluated_at, "G102B2_EVALUATED_AT_INVALID")
    if not (
        _instant(binding["data_cutoff_at"])
        <= _instant(requested_at)
        <= _instant(evaluated_at)
    ):
        _fail("G102B2_REQUEST_BEFORE_CUTOFF")
    entries = [{
        "source_table": item["source_table"],
        "source_id": item["source_id"],
        "entry_hash": item["entry_hash"],
        "component_id": item["component_id"],
        "subject_experiment_ids": item["subject_experiment_ids"],
        "lineage_status": item["lineage_status"],
    } for item in candidate["lineage_candidates"]]
    components = [{
        "component_id": item["component_id"],
        "component_hash": item["component_hash"],
        "subject_experiment_ids": item["subject_experiment_ids"],
        "status": item["status"],
    } for item in candidate["components"]]
    entries.sort(key=lambda item: (item["source_table"], item["source_id"]))
    components.sort(key=lambda item: item["component_id"])
    denominator = {
        "legacy_entries": entries,
        "components": components,
        "legacy_entry_count": len(entries),
        "component_count": len(components),
        "subject_experiment_ids": sorted({
            subject for item in entries for subject in item["subject_experiment_ids"]
        }),
    }
    denominator["denominator_hash"] = canonical_hash(denominator)
    request = {
        "schema_version": REQUEST_VERSION,
        "contract_version": CONTRACT_VERSION,
        "request_id": request_id,
        "requested_at": requested_at,
        "evaluated_at": evaluated_at,
        "input_binding": dict(binding),
        "denominator": denominator,
        "authority_contract": {
            "canonical_level": "STUDY",
            "required_roles": list(REQUIRED_ROLES),
            "signature_algorithm": SIGNATURE_ALGORITHM,
            "signature_purpose": SIGNATURE_PURPOSE,
            "complete_denominator_required": True,
            "parent_edge_source": "EXTERNAL_IMMUTABLE_AUTHORITY_ONLY",
            "forbidden_inferences": list(FORBIDDEN_INFERENCES),
            "split_effect": "NONE",
            "holdout_status": "LOCKED_NOT_ASSIGNED",
        },
        "trust_status": "UNSIGNED_AUTHORITY_REQUEST",
        "authority_effect": "NONE",
        "replay_eligible": False,
        "gate1_effect": "NONE",
        "not_dataset_receipt": True,
    }
    request["request_hash"] = canonical_hash(request)
    return _validate_request_shape(request)


def _validate_request_shape(value: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "schema_version", "contract_version", "request_id", "requested_at", "evaluated_at", "input_binding",
        "denominator", "authority_contract", "trust_status", "authority_effect",
        "replay_eligible", "gate1_effect", "not_dataset_receipt", "request_hash",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G102B2_REQUEST_SCHEMA_INVALID")
    request = dict(value)
    if request["schema_version"] != REQUEST_VERSION or request["contract_version"] != CONTRACT_VERSION:
        _fail("G102B2_REQUEST_VERSION_INVALID")
    _identifier(request["request_id"], "G102B2_REQUEST_ID_INVALID")
    _utc(request["requested_at"], "G102B2_REQUESTED_AT_INVALID")
    _utc(request["evaluated_at"], "G102B2_EVALUATED_AT_INVALID")
    binding_keys = {
        "audit_manifest_sha256", "candidate_manifest_sha256", "candidate_manifest_hash",
        "candidate_hash", "audit_bundle_hash", "audit_request_hash",
        "audit_source_snapshot_hash", "audit_authoritative_asof_hash", "data_cutoff_at",
    }
    binding = request["input_binding"]
    if not isinstance(binding, Mapping) or set(binding) != binding_keys:
        _fail("G102B2_INPUT_BINDING_INVALID")
    for key in binding_keys - {"data_cutoff_at"}:
        _hash(binding[key], "G102B2_INPUT_BINDING_INVALID")
    _utc(binding["data_cutoff_at"], "G102B2_INPUT_BINDING_INVALID")
    if not (
        _instant(binding["data_cutoff_at"])
        <= _instant(request["requested_at"])
        <= _instant(request["evaluated_at"])
    ):
        _fail("G102B2_REQUEST_BEFORE_CUTOFF")
    denominator = request["denominator"]
    denominator_keys = {
        "legacy_entries", "components", "legacy_entry_count", "component_count",
        "subject_experiment_ids", "denominator_hash",
    }
    if not isinstance(denominator, Mapping) or set(denominator) != denominator_keys:
        _fail("G102B2_DENOMINATOR_INVALID")
    if denominator["denominator_hash"] != canonical_hash({
        key: item for key, item in denominator.items() if key != "denominator_hash"
    }):
        _fail("G102B2_DENOMINATOR_HASH_MISMATCH")
    entries = denominator["legacy_entries"]
    components = denominator["components"]
    if (
        not isinstance(entries, list)
        or entries != sorted(entries, key=lambda item: (item["source_table"], item["source_id"]))
        or len({(item["source_table"], item["source_id"]) for item in entries}) != len(entries)
        or denominator["legacy_entry_count"] != len(entries)
        or not isinstance(components, list)
        or components != sorted(components, key=lambda item: item["component_id"])
        or len({item["component_id"] for item in components}) != len(components)
        or denominator["component_count"] != len(components)
    ):
        _fail("G102B2_DENOMINATOR_INVALID")
    entry_keys = {
        "source_table", "source_id", "entry_hash", "component_id",
        "subject_experiment_ids", "lineage_status",
    }
    for item in entries:
        if not isinstance(item, Mapping) or set(item) != entry_keys:
            _fail("G102B2_DENOMINATOR_INVALID")
        _hash(item["entry_hash"], "G102B2_DENOMINATOR_INVALID")
        if (
            not item["source_table"] or not item["source_id"]
            or item["subject_experiment_ids"] != sorted(set(item["subject_experiment_ids"]))
        ):
            _fail("G102B2_DENOMINATOR_INVALID")
    component_keys = {"component_id", "component_hash", "subject_experiment_ids", "status"}
    for item in components:
        if not isinstance(item, Mapping) or set(item) != component_keys:
            _fail("G102B2_DENOMINATOR_INVALID")
        _hash(item["component_hash"], "G102B2_DENOMINATOR_INVALID")
        if item["subject_experiment_ids"] != sorted(set(item["subject_experiment_ids"])):
            _fail("G102B2_DENOMINATOR_INVALID")
    subjects = sorted({subject for item in entries for subject in item["subject_experiment_ids"]})
    if denominator["subject_experiment_ids"] != subjects:
        _fail("G102B2_DENOMINATOR_INVALID")
    if request["authority_contract"] != {
        "canonical_level": "STUDY",
        "required_roles": list(REQUIRED_ROLES),
        "signature_algorithm": SIGNATURE_ALGORITHM,
        "signature_purpose": SIGNATURE_PURPOSE,
        "complete_denominator_required": True,
        "parent_edge_source": "EXTERNAL_IMMUTABLE_AUTHORITY_ONLY",
        "forbidden_inferences": list(FORBIDDEN_INFERENCES),
        "split_effect": "NONE",
        "holdout_status": "LOCKED_NOT_ASSIGNED",
    }:
        _fail("G102B2_AUTHORITY_CONTRACT_INVALID")
    if (
        request["trust_status"] != "UNSIGNED_AUTHORITY_REQUEST"
        or request["authority_effect"] != "NONE"
        or request["replay_eligible"] is not False
        or request["gate1_effect"] != "NONE"
        or request["not_dataset_receipt"] is not True
    ):
        _fail("G102B2_REQUEST_TRUST_CEILING_INVALID")
    if request["request_hash"] != canonical_hash({
        key: item for key, item in request.items() if key != "request_hash"
    }):
        _fail("G102B2_REQUEST_HASH_MISMATCH")
    return request


def _validate_response(
    request: Mapping[str, Any], value: Mapping[str, Any], registry: Mapping[str, Any],
) -> dict[str, Any]:
    keys = {
        "schema_version", "contract_version", "authority_id", "request_hash", "authorized_at",
        "lineage_nodes", "exclusions", "authority_payload_hash", "signatures",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G102B2_RESPONSE_SCHEMA_INVALID")
    response = dict(value)
    if response["schema_version"] != RESPONSE_VERSION or response["contract_version"] != CONTRACT_VERSION:
        _fail("G102B2_RESPONSE_VERSION_INVALID")
    _identifier(response["authority_id"], "G102B2_AUTHORITY_ID_INVALID")
    authorized_at = _utc(response["authorized_at"], "G102B2_AUTHORIZED_AT_INVALID")
    if response["request_hash"] != request["request_hash"]:
        _fail("G102B2_RESPONSE_REQUEST_BINDING_INVALID")
    if not (
        _instant(request["requested_at"])
        <= _instant(authorized_at)
        <= _instant(request["evaluated_at"])
    ):
        _fail("G102B2_RESPONSE_TIME_INVALID")
    if not isinstance(response["lineage_nodes"], list) or not isinstance(response["exclusions"], list):
        _fail("G102B2_RESPONSE_SCHEMA_INVALID")
    if (
        response["lineage_nodes"] != sorted(
            response["lineage_nodes"],
            key=lambda item: item.get("canonical_experiment_id", "")
            if isinstance(item, Mapping) else "",
        )
        or response["exclusions"] != sorted(
            response["exclusions"],
            key=lambda item: item.get("exclusion_id", "") if isinstance(item, Mapping) else "",
        )
    ):
        _fail("G102B2_RESPONSE_ORDER_INVALID")
    payload = {key: item for key, item in response.items() if key not in {"authority_payload_hash", "signatures"}}
    if response["authority_payload_hash"] != canonical_hash(payload):
        _fail("G102B2_AUTHORITY_PAYLOAD_HASH_MISMATCH")
    _validate_authority_semantics(request, response["lineage_nodes"], response["exclusions"])
    _validate_signatures(response, registry)
    return response


def _validate_authority_semantics(
    request: Mapping[str, Any], nodes: list[Any], exclusions: list[Any],
) -> None:
    denominator = request["denominator"]
    entries = {
        (item["source_table"], item["source_id"]): item
        for item in denominator["legacy_entries"]
    }
    components = {item["component_id"]: item for item in denominator["components"]}
    expected_entry_refs = {
        (key[0], key[1], item["entry_hash"]) for key, item in entries.items()
    }
    node_keys = {
        "lineage_id", "canonical_experiment_id", "component_id",
        "member_legacy_experiment_ids", "parent_canonical_experiment_id",
        "parent_component_id", "iteration_no", "spec_hash", "parent_spec_hash",
        "candidate_entry_refs", "authority_evidence_refs",
    }
    exclusion_keys = {
        "exclusion_id", "component_ids", "candidate_entry_refs", "reason_codes",
        "authority_evidence_refs",
    }
    node_by_experiment: dict[str, Mapping[str, Any]] = {}
    covered_entries: list[tuple[str, str, str]] = []
    covered_components: list[str] = []
    experiment_owner: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, Mapping) or set(node) != node_keys:
            _fail("G102B2_LINEAGE_NODE_INVALID")
        _identifier(node["canonical_experiment_id"], "G102B2_CANONICAL_EXPERIMENT_ID_INVALID")
        _hash(node["spec_hash"], "G102B2_SPEC_HASH_INVALID")
        if not _LINEAGE_RE.fullmatch(str(node["lineage_id"])):
            _fail("G102B2_LINEAGE_ID_INVALID")
        if node["canonical_experiment_id"] in node_by_experiment:
            _fail("G102B2_LINEAGE_GRAPH_CONFLICT")
        node_by_experiment[node["canonical_experiment_id"]] = node
        component = components.get(node["component_id"])
        if component is None or component["status"] == "CONFLICT":
            _fail("G102B2_COMPONENT_CONFLICT")
        if node["member_legacy_experiment_ids"] != component["subject_experiment_ids"]:
            _fail("G102B2_COMPONENT_CONFLICT")
        if type(node["iteration_no"]) is not int or node["iteration_no"] <= 0:
            _fail("G102B2_LINEAGE_GRAPH_CONFLICT")
        for experiment_id in node["member_legacy_experiment_ids"]:
            prior = experiment_owner.get(experiment_id)
            if prior is not None:
                _fail("G102B2_EXPERIMENT_MEMBERSHIP_CONFLICT")
            experiment_owner[experiment_id] = node["canonical_experiment_id"]
        refs = _validate_candidate_entry_refs(node["candidate_entry_refs"], entries)
        expected_node_refs = {
            (key[0], key[1], item["entry_hash"])
            for key, item in entries.items()
            if item["component_id"] == node["component_id"]
        }
        if set(refs) != expected_node_refs:
            _fail("G102B2_DENOMINATOR_CONFLICT")
        evidence_classes = _validate_authority_evidence_refs(node["authority_evidence_refs"])
        required_class = (
            "EXPLICIT_ROOT_DECLARATION"
            if node["parent_canonical_experiment_id"] is None
            else "EXPLICIT_PARENT_EDGE"
        )
        if required_class not in evidence_classes:
            _fail("G102B2_AUTHORITY_EVIDENCE_INVALID")
        covered_entries.extend(refs)
        covered_components.append(node["component_id"])
    for exclusion in exclusions:
        if not isinstance(exclusion, Mapping) or set(exclusion) != exclusion_keys:
            _fail("G102B2_EXCLUSION_INVALID")
        if not _EXCLUSION_RE.fullmatch(str(exclusion["exclusion_id"])):
            _fail("G102B2_EXCLUSION_INVALID")
        refs = _validate_candidate_entry_refs(exclusion["candidate_entry_refs"], entries)
        component_ids = exclusion["component_ids"]
        if (
            not refs
            or not isinstance(component_ids, list)
            or component_ids != sorted(set(component_ids))
            or any(component_id not in components for component_id in component_ids)
        ):
            _fail("G102B2_EXCLUSION_INVALID")
        if (
            not isinstance(exclusion["reason_codes"], list)
            or not exclusion["reason_codes"]
            or exclusion["reason_codes"] != sorted(set(exclusion["reason_codes"]))
            or any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", str(code)) for code in exclusion["reason_codes"])
        ):
            _fail("G102B2_EXCLUSION_INVALID")
        evidence_classes = _validate_authority_evidence_refs(exclusion["authority_evidence_refs"])
        if "NAMED_EXCLUSION" not in evidence_classes:
            _fail("G102B2_AUTHORITY_EVIDENCE_INVALID")
        referenced_component_ids = {
            entries[(ref[0], ref[1])]["component_id"]
            for ref in refs
            if entries[(ref[0], ref[1])]["component_id"] is not None
        }
        if referenced_component_ids != set(component_ids):
            _fail("G102B2_DENOMINATOR_CONFLICT")
        expected_exclusion_id = "exclusion_" + canonical_hash({
            "component_ids": component_ids,
            "candidate_entry_refs": exclusion["candidate_entry_refs"],
            "reason_codes": exclusion["reason_codes"],
        })[:24]
        if exclusion["exclusion_id"] != expected_exclusion_id:
            _fail("G102B2_EXCLUSION_INVALID")
        covered_entries.extend(refs)
        covered_components.extend(component_ids)
    if (
        len(covered_entries) != len(set(covered_entries))
        or set(covered_entries) != expected_entry_refs
        or len(covered_components) != len(set(covered_components))
        or set(covered_components) != set(components)
    ):
        _fail("G102B2_DENOMINATOR_CONFLICT")
    for node in nodes:
        parent_id = node["parent_canonical_experiment_id"]
        if parent_id is None:
            if (
                node["parent_component_id"] is not None
                or node["parent_spec_hash"] is not None
                or node["iteration_no"] != 1
                or node["lineage_id"] != lineage_id_for_root(
                    node["canonical_experiment_id"], node["spec_hash"],
                )
            ):
                _fail("G102B2_LINEAGE_GRAPH_CONFLICT")
            continue
        parent = node_by_experiment.get(parent_id)
        if (
            parent is None
            or parent["lineage_id"] != node["lineage_id"]
            or parent["component_id"] != node["parent_component_id"]
            or parent["spec_hash"] != node["parent_spec_hash"]
            or node["iteration_no"] != parent["iteration_no"] + 1
        ):
            _fail("G102B2_LINEAGE_GRAPH_CONFLICT")


def _validate_candidate_entry_refs(
    values: Any, entries: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[tuple[str, str, str]]:
    keys = {"source_table", "source_id", "entry_hash"}
    if not isinstance(values, list):
        _fail("G102B2_CANDIDATE_ENTRY_REF_INVALID")
    for item in values:
        if not isinstance(item, Mapping) or set(item) != keys:
            _fail("G102B2_CANDIDATE_ENTRY_REF_INVALID")
    if values != sorted(values, key=lambda item: (item["source_table"], item["source_id"])):
        _fail("G102B2_CANDIDATE_ENTRY_REF_INVALID")
    refs = []
    for item in values:
        expected = entries.get((item["source_table"], item["source_id"]))
        if expected is None or expected["entry_hash"] != item["entry_hash"]:
            _fail("G102B2_CANDIDATE_ENTRY_REF_INVALID")
        refs.append((item["source_table"], item["source_id"], item["entry_hash"]))
    if len(refs) != len(set(refs)):
        _fail("G102B2_CANDIDATE_ENTRY_REF_INVALID")
    return refs


def _validate_authority_evidence_refs(values: Any) -> set[str]:
    keys = {"artifact_type", "manifest_sha256", "record_id", "record_hash", "evidence_class"}
    if not isinstance(values, list) or not values:
        _fail("G102B2_AUTHORITY_EVIDENCE_INVALID")
    for item in values:
        if not isinstance(item, Mapping) or set(item) != keys:
            _fail("G102B2_AUTHORITY_EVIDENCE_INVALID")
    if values != sorted(
        values,
        key=lambda item: (
            item["artifact_type"], item["manifest_sha256"], item["record_id"],
            item["record_hash"], item["evidence_class"],
        ),
    ):
        _fail("G102B2_AUTHORITY_EVIDENCE_INVALID")
    identities: set[tuple[str, str, str]] = set()
    classes: set[str] = set()
    for item in values:
        _identifier(item["artifact_type"], "G102B2_AUTHORITY_EVIDENCE_INVALID")
        _identifier(item["record_id"], "G102B2_AUTHORITY_EVIDENCE_INVALID")
        _hash(item["manifest_sha256"], "G102B2_AUTHORITY_EVIDENCE_INVALID")
        _hash(item["record_hash"], "G102B2_AUTHORITY_EVIDENCE_INVALID")
        if item["evidence_class"] not in {
            "EXPLICIT_PARENT_EDGE", "EXPLICIT_ROOT_DECLARATION", "NAMED_EXCLUSION",
            "SIGNED_COMPONENT_MEMBERSHIP",
        }:
            _fail("G102B2_AUTHORITY_EVIDENCE_INVALID")
        identity = (item["artifact_type"], item["manifest_sha256"], item["record_id"])
        if identity in identities:
            _fail("G102B2_AUTHORITY_EVIDENCE_INVALID")
        identities.add(identity)
        classes.add(item["evidence_class"])
    return classes


def _validate_key_registry(
    value: Mapping[str, Any], *, expected_key_registry_hash: str,
) -> dict[str, Any]:
    _hash(expected_key_registry_hash, "G102B2_KEY_REGISTRY_ANCHOR_INVALID")
    keys = {"schema_version", "registry_id", "keys", "registry_hash"}
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G102B2_KEY_REGISTRY_INVALID")
    registry = dict(value)
    if registry["schema_version"] != REGISTRY_VERSION:
        _fail("G102B2_KEY_REGISTRY_INVALID")
    _identifier(registry["registry_id"], "G102B2_KEY_REGISTRY_INVALID")
    if registry["registry_hash"] != canonical_hash({
        key: item for key, item in registry.items() if key != "registry_hash"
    }) or registry["registry_hash"] != expected_key_registry_hash:
        _fail("G102B2_KEY_REGISTRY_ANCHOR_MISMATCH")
    key_fields = {
        "key_id", "signer_id", "role", "purposes", "algorithm", "status",
        "valid_from", "valid_until", "public_key_pem",
    }
    if not isinstance(registry["keys"], list) or len(registry["keys"]) < len(REQUIRED_ROLES):
        _fail("G102B2_KEY_REGISTRY_INVALID")
    if registry["keys"] != sorted(
        registry["keys"],
        key=lambda item: item.get("key_id", "") if isinstance(item, Mapping) else "",
    ):
        _fail("G102B2_KEY_REGISTRY_INVALID")
    seen: set[str] = set()
    fingerprints: set[str] = set()
    for item in registry["keys"]:
        if not isinstance(item, Mapping) or set(item) != key_fields:
            _fail("G102B2_KEY_REGISTRY_INVALID")
        _identifier(item["key_id"], "G102B2_KEY_REGISTRY_INVALID")
        _identifier(item["signer_id"], "G102B2_KEY_REGISTRY_INVALID")
        if item["key_id"] in seen or item["role"] not in REQUIRED_ROLES:
            _fail("G102B2_KEY_REGISTRY_INVALID")
        seen.add(item["key_id"])
        if (
            item["algorithm"] != SIGNATURE_ALGORITHM
            or item["status"] != "ACTIVE"
            or item["purposes"] != [SIGNATURE_PURPOSE]
            or not isinstance(item["public_key_pem"], str)
            or not _PEM_RE.fullmatch(item["public_key_pem"])
        ):
            _fail("G102B2_KEY_REGISTRY_INVALID")
        bits, fingerprint = _rsa_public_key_metadata(item["public_key_pem"])
        if bits < MINIMUM_RSA_BITS or fingerprint in fingerprints:
            _fail("G102B2_KEY_REGISTRY_INVALID")
        fingerprints.add(fingerprint)
        valid_from = _utc(item["valid_from"], "G102B2_KEY_REGISTRY_INVALID")
        valid_until = _utc(item["valid_until"], "G102B2_KEY_REGISTRY_INVALID")
        if _instant(valid_until) <= _instant(valid_from):
            _fail("G102B2_KEY_REGISTRY_INVALID")
    return registry


def _validate_signatures(response: Mapping[str, Any], registry: Mapping[str, Any]) -> None:
    signatures = response["signatures"]
    fields = {
        "algorithm", "key_id", "signer_id", "role", "purpose", "object_hash",
        "key_registry_hash", "signed_at", "signature_base64",
    }
    if not isinstance(signatures, list) or len(signatures) != len(REQUIRED_ROLES):
        _fail("G102B2_SIGNATURE_SET_INVALID")
    key_by_id = {item["key_id"]: item for item in registry["keys"]}
    roles: list[str] = []
    signers: list[str] = []
    key_ids: list[str] = []
    for signature in signatures:
        if not isinstance(signature, Mapping) or set(signature) != fields:
            _fail("G102B2_SIGNATURE_INVALID")
        key = key_by_id.get(signature["key_id"])
        signed_at = _utc(signature["signed_at"], "G102B2_SIGNATURE_INVALID")
        if (
            key is None
            or signature["algorithm"] != SIGNATURE_ALGORITHM
            or signature["signer_id"] != key["signer_id"]
            or signature["role"] != key["role"]
            or signature["purpose"] != SIGNATURE_PURPOSE
            or signature["object_hash"] != response["authority_payload_hash"]
            or signature["key_registry_hash"] != registry["registry_hash"]
            or signed_at != response["authorized_at"]
            or not (_instant(key["valid_from"]) <= _instant(signed_at) <= _instant(key["valid_until"]))
        ):
            _fail("G102B2_SIGNATURE_INVALID")
        try:
            signature_bytes = base64.b64decode(signature["signature_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ImmutableLineageAuthorityError("G102B2_SIGNATURE_INVALID") from exc
        if not signature_bytes or not _verify_rsa_sha256(
            key["public_key_pem"],
            signature_bytes,
            signature_message(
                response["authority_payload_hash"],
                key_registry_hash=registry["registry_hash"],
                key_id=signature["key_id"],
                signer_id=signature["signer_id"],
                role=signature["role"],
            ),
        ):
            _fail("G102B2_SIGNATURE_INVALID")
        roles.append(signature["role"])
        signers.append(signature["signer_id"])
        key_ids.append(signature["key_id"])
    if (
        roles != list(REQUIRED_ROLES)
        or len(set(signers)) != len(REQUIRED_ROLES)
        or len(set(key_ids)) != len(REQUIRED_ROLES)
    ):
        _fail("G102B2_SIGNATURE_SET_INVALID")


def _verify_rsa_sha256(public_key_pem: str, signature: bytes, message: bytes) -> bool:
    with tempfile.TemporaryDirectory(prefix="gle-lineage-authority-") as directory:
        root = Path(directory)
        key_path = root / "public.pem"
        signature_path = root / "signature.bin"
        key_path.write_text(public_key_pem, encoding="ascii")
        signature_path.write_bytes(signature)
        try:
            result = subprocess.run(
                [OPENSSL_BINARY, "dgst", "-sha256", "-verify", str(key_path), "-signature", str(signature_path)],
                input=message,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ImmutableLineageAuthorityError("G102B2_SIGNATURE_VERIFIER_UNAVAILABLE") from exc
        return result.returncode == 0


def _rsa_public_key_metadata(public_key_pem: str) -> tuple[int, str]:
    with tempfile.TemporaryDirectory(prefix="gle-lineage-authority-key-") as directory:
        key_path = Path(directory) / "public.pem"
        key_path.write_text(public_key_pem, encoding="ascii")
        try:
            result = subprocess.run(
                [OPENSSL_BINARY, "pkey", "-pubin", "-in", str(key_path), "-text", "-noout"],
                capture_output=True,
                timeout=10,
                check=False,
                text=True,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ImmutableLineageAuthorityError("G102B2_SIGNATURE_VERIFIER_UNAVAILABLE") from exc
        match = re.search(r"(?:RSA )?Public-Key: \((\d+) bit\)", result.stdout + result.stderr)
        if result.returncode != 0 or match is None:
            _fail("G102B2_KEY_REGISTRY_INVALID")
        try:
            der = subprocess.run(
                [OPENSSL_BINARY, "pkey", "-pubin", "-in", str(key_path), "-outform", "DER"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ImmutableLineageAuthorityError("G102B2_SIGNATURE_VERIFIER_UNAVAILABLE") from exc
        if der.returncode != 0 or not der.stdout:
            _fail("G102B2_KEY_REGISTRY_INVALID")
        return int(match.group(1)), hashlib.sha256(der.stdout).hexdigest()


def _fragment(
    request: Mapping[str, Any], response: Mapping[str, Any] | None, *, status: str, reasons: list[str],
) -> dict[str, Any]:
    verified = status == "VERIFIED"
    response_hash = canonical_hash(response) if isinstance(response, Mapping) else None
    authority_id = response.get("authority_id") if isinstance(response, Mapping) else None
    fragment = {
        "schema_version": FRAGMENT_VERSION,
        "contract_version": CONTRACT_VERSION,
        "request_id": request["request_id"],
        "request_hash": request["request_hash"],
        "authority_id": authority_id,
        "response_hash": response_hash,
        "status": status,
        "reason_codes": sorted(set(reasons)),
        "verified_roles": list(REQUIRED_ROLES) if verified else [],
        "trust_status": "EXTERNALLY_SIGNED_AUTHORITY_ATTESTATION" if verified else "NO_AUTHORITY_EFFECT",
        "authority_effect": "LINEAGE_AUTHORITY_ATTESTATION_VERIFIED" if verified else "NONE",
        "split_assignments": [],
        "split_effect": "NONE",
        "holdout_status": "LOCKED_NOT_ASSIGNED",
        "replay_eligible": False,
        "golden_eligible": False,
        "gate1_effect": "NONE",
        "not_dataset_receipt": True,
        "not_gate_receipt": True,
    }
    fragment["fragment_hash"] = canonical_hash(fragment)
    return _validate_fragment(fragment, request)


def _validate_fragment(value: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "schema_version", "contract_version", "request_id", "request_hash", "authority_id",
        "response_hash", "status", "reason_codes", "verified_roles", "trust_status",
        "authority_effect", "split_assignments", "split_effect", "holdout_status",
        "replay_eligible", "golden_eligible", "gate1_effect", "not_dataset_receipt",
        "not_gate_receipt", "fragment_hash",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G102B2_FRAGMENT_INVALID")
    fragment = dict(value)
    if (
        fragment["schema_version"] != FRAGMENT_VERSION
        or fragment["contract_version"] != CONTRACT_VERSION
        or fragment["request_id"] != request["request_id"]
        or fragment["request_hash"] != request["request_hash"]
        or fragment["status"] not in {"MISSING", "VERIFIED", "CONFLICT", "INVALID"}
        or fragment["split_assignments"] != []
        or fragment["split_effect"] != "NONE"
        or fragment["holdout_status"] != "LOCKED_NOT_ASSIGNED"
        or fragment["replay_eligible"] is not False
        or fragment["golden_eligible"] is not False
        or fragment["gate1_effect"] != "NONE"
        or fragment["not_dataset_receipt"] is not True
        or fragment["not_gate_receipt"] is not True
    ):
        _fail("G102B2_FRAGMENT_INVALID")
    verified = fragment["status"] == "VERIFIED"
    if (
        (fragment["verified_roles"] == list(REQUIRED_ROLES)) != verified
        or (fragment["trust_status"] == "EXTERNALLY_SIGNED_AUTHORITY_ATTESTATION") != verified
        or (fragment["authority_effect"] == "LINEAGE_AUTHORITY_ATTESTATION_VERIFIED") != verified
        or (not fragment["reason_codes"]) != verified
    ):
        _fail("G102B2_FRAGMENT_STATE_INVALID")
    if fragment["fragment_hash"] != canonical_hash({
        key: item for key, item in fragment.items() if key != "fragment_hash"
    }):
        _fail("G102B2_FRAGMENT_HASH_MISMATCH")
    return fragment


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        _fail(code)
    return value


def _hash(value: Any, code: str) -> str:
    try:
        return validate_sha256(value, code=code)
    except ValueError as exc:
        raise ImmutableLineageAuthorityError(str(exc)) from exc


def _utc(value: Any, code: str) -> str:
    try:
        normalized = validate_utc(value, code=code)
    except ValueError as exc:
        raise ImmutableLineageAuthorityError(str(exc)) from exc
    assert isinstance(normalized, str)
    return normalized


def _instant(value: str) -> int:
    # Canonical timestamps are second-precision UTC; lexical order is chronological.
    return int(value[0:4] + value[5:7] + value[8:10] + value[11:13] + value[14:16] + value[17:19])
