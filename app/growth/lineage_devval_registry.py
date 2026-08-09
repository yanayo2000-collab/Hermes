from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import stat
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
    HistoricalLineageCandidateError,
    _canonical_json_document,
    _read_regular,
)
from app.growth.immutable_lineage_authority import (
    MINIMUM_RSA_BITS,
    REQUIRED_ROLES,
    SIGNATURE_ALGORITHM,
    _rsa_public_key_metadata,
    _verify_rsa_sha256,
    load_validated_authority_directory,
)


REQUEST_VERSION = "gle-g1-02b2b-devval-registry-request-v1"
RESPONSE_VERSION = "gle-g1-02b2b-devval-registry-response-v1"
POLICY_VERSION = "gle-g1-02b2b-devval-policy-v1"
REGISTRY_VERSION = "gle-g1-02b2b-devval-registry-v1"
KEY_REGISTRY_VERSION = "gle-g1-02b2b-devval-key-registry-v1"
SEED_SELECTION_VERSION = "gle-g1-02b2b-seed-selection-record-v1"
MANIFEST_VERSION = "gle-g1-02b2b-devval-artifact-manifest-v1"

SIGNATURE_PURPOSE = "DEV_VALIDATION_REGISTRY_ATTESTATION"
ASSIGNMENT_ALGORITHM = "SHA256_U64_THRESHOLD_V1"
ALLOWED_SPLITS = ["DEV", "VALIDATION"]
HOLDOUT_STATUS = "LOCKED_NOT_ASSIGNED"
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
EXACT_FILES = frozenset({
    "manifest.json",
    "registry-request.json",
    "registry-response.json",
    "trusted-key-registry.json",
    "devval-registry.json",
})

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_REASON_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_PEM_RE = re.compile(
    r"-----BEGIN PUBLIC KEY-----\n(?:[A-Za-z0-9+/=]+\n)+-----END PUBLIC KEY-----\n?"
)


class LineageDevvalRegistryError(RuntimeError):
    pass


def _fail(code: str) -> None:
    raise LineageDevvalRegistryError(code)


def build_registry_request(
    *,
    authority_dir: str | Path,
    expected_authority_manifest_sha256: str,
    expected_authority_key_registry_hash: str | None,
    candidate_dir: str | Path,
    expected_candidate_manifest_sha256: str,
    audit_dir: str | Path,
    expected_audit_manifest_sha256: str,
    registry_id: str,
    generation: int,
    requested_at: str,
    evaluated_at: str,
    policy: Mapping[str, Any] | None = None,
    seed_selection_file: str | Path | None = None,
    expected_seed_selection_file_sha256: str | None = None,
    prior_registry_dir: str | Path | None = None,
    expected_prior_manifest_sha256: str | None = None,
    expected_prior_devval_key_registry_hash: str | None = None,
) -> dict[str, Any]:
    context = _load_authority_context(
        authority_dir=authority_dir,
        expected_authority_manifest_sha256=expected_authority_manifest_sha256,
        expected_authority_key_registry_hash=expected_authority_key_registry_hash,
        candidate_dir=candidate_dir,
        expected_candidate_manifest_sha256=expected_candidate_manifest_sha256,
        audit_dir=audit_dir,
        expected_audit_manifest_sha256=expected_audit_manifest_sha256,
    )
    return _build_request(
        context,
        registry_id=registry_id,
        generation=generation,
        requested_at=requested_at,
        evaluated_at=evaluated_at,
        policy=policy,
        seed_selection_file=seed_selection_file,
        expected_seed_selection_file_sha256=expected_seed_selection_file_sha256,
        prior_registry_dir=prior_registry_dir,
        expected_prior_manifest_sha256=expected_prior_manifest_sha256,
        expected_prior_devval_key_registry_hash=expected_prior_devval_key_registry_hash,
    )


def validate_registry_request(
    value: Mapping[str, Any],
    *,
    authority_dir: str | Path,
    expected_authority_manifest_sha256: str,
    expected_authority_key_registry_hash: str | None,
    candidate_dir: str | Path,
    expected_candidate_manifest_sha256: str,
    audit_dir: str | Path,
    expected_audit_manifest_sha256: str,
    seed_selection_file: str | Path | None = None,
    expected_seed_selection_file_sha256: str | None = None,
    prior_registry_dir: str | Path | None = None,
    expected_prior_manifest_sha256: str | None = None,
    expected_prior_devval_key_registry_hash: str | None = None,
) -> dict[str, Any]:
    request = _validate_request_shape(value)
    expected = build_registry_request(
        authority_dir=authority_dir,
        expected_authority_manifest_sha256=expected_authority_manifest_sha256,
        expected_authority_key_registry_hash=expected_authority_key_registry_hash,
        candidate_dir=candidate_dir,
        expected_candidate_manifest_sha256=expected_candidate_manifest_sha256,
        audit_dir=audit_dir,
        expected_audit_manifest_sha256=expected_audit_manifest_sha256,
        registry_id=request["registry_id"],
        generation=request["generation"],
        requested_at=request["requested_at"],
        evaluated_at=request["evaluated_at"],
        policy=request["policy"],
        seed_selection_file=seed_selection_file,
        expected_seed_selection_file_sha256=expected_seed_selection_file_sha256,
        prior_registry_dir=prior_registry_dir,
        expected_prior_manifest_sha256=expected_prior_manifest_sha256,
        expected_prior_devval_key_registry_hash=expected_prior_devval_key_registry_hash,
    )
    if request != expected:
        _fail("G102B2B_REQUEST_SOURCE_SEMANTICS_MISMATCH")
    return request


def evaluate_registry_response(
    request: Mapping[str, Any],
    response: Mapping[str, Any] | None,
    *,
    trusted_key_registry: Mapping[str, Any] | None,
    expected_devval_key_registry_hash: str | None,
    source_validation: Mapping[str, Any],
) -> dict[str, Any]:
    request = validate_registry_request(request, **dict(source_validation))
    response_snapshot = deepcopy(response)
    registry_snapshot = deepcopy(trusted_key_registry)
    if request["status"] == "BLOCKED":
        if response_snapshot is not None or registry_snapshot is not None:
            _fail("G102B2B_BLOCKED_TRUST_INPUT_INVALID")
        return _registry_fragment(
            request,
            status="BLOCKED",
            reasons=request["reason_codes"],
            assignments=[],
            response_hash=None,
            key_registry_hash=None,
        )
    if response_snapshot is None:
        if registry_snapshot is not None or expected_devval_key_registry_hash is not None:
            _fail("G102B2B_PENDING_TRUST_INPUT_INVALID")
        return _registry_fragment(
            request,
            status="PENDING_SIGNATURES",
            reasons=["SIGNED_DEVVAL_RESPONSE_MISSING"],
            assignments=[],
            response_hash=None,
            key_registry_hash=None,
        )
    if registry_snapshot is None or expected_devval_key_registry_hash is None:
        _fail("G102B2B_TRUST_ROOT_MISSING")
    registry = _validate_key_registry(
        registry_snapshot,
        expected_key_registry_hash=expected_devval_key_registry_hash,
    )
    response_value, assignments = _validate_response(request, response_snapshot, registry)
    return _registry_fragment(
        request,
        status="SIGNED_DETERMINISTIC_PARTITION",
        reasons=[],
        assignments=assignments,
        response_hash=canonical_hash(response_value),
        key_registry_hash=registry["registry_hash"],
    )


def write_registry_artifacts(
    request: Mapping[str, Any],
    response: Mapping[str, Any] | None,
    trusted_key_registry: Mapping[str, Any] | None,
    registry: Mapping[str, Any],
    output_dir: str | Path,
    *,
    expected_devval_key_registry_hash: str | None,
    source_validation: Mapping[str, Any],
) -> dict[str, Any]:
    request_snapshot = deepcopy(request)
    response_snapshot = deepcopy(response)
    key_registry_snapshot = deepcopy(trusted_key_registry)
    request_snapshot = validate_registry_request(request_snapshot, **dict(source_validation))
    expected_registry = evaluate_registry_response(
        request_snapshot,
        response_snapshot,
        trusted_key_registry=key_registry_snapshot,
        expected_devval_key_registry_hash=expected_devval_key_registry_hash,
        source_validation=source_validation,
    )
    registry_value = _validate_registry(registry, request_snapshot)
    if registry_value != expected_registry:
        _fail("G102B2B_REGISTRY_DERIVATION_MISMATCH")
    output = Path(output_dir)
    if output.exists() or output.is_symlink():
        _fail("G102B2B_OUTPUT_EXISTS")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        payloads = {
            "registry-request.json": (canonical_json(request_snapshot) + "\n").encode(),
            "registry-response.json": (canonical_json(response_snapshot) + "\n").encode(),
            "trusted-key-registry.json": (canonical_json(key_registry_snapshot) + "\n").encode(),
            "devval-registry.json": (canonical_json(registry_value) + "\n").encode(),
        }
        files: dict[str, dict[str, Any]] = {}
        for name, payload in payloads.items():
            if len(payload) > MAX_ARTIFACT_BYTES:
                _fail("G102B2B_ARTIFACT_TOO_LARGE")
            path = temporary / name
            path.write_bytes(payload)
            path.chmod(0o600)
            files[name] = {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        manifest = _manifest_projection(
            request_snapshot,
            registry_value,
            expected_devval_key_registry_hash=expected_devval_key_registry_hash,
            files=files,
        )
        manifest_payload = (canonical_json(manifest) + "\n").encode()
        if len(manifest_payload) > MAX_ARTIFACT_BYTES:
            _fail("G102B2B_ARTIFACT_TOO_LARGE")
        manifest_path = temporary / "manifest.json"
        manifest_path.write_bytes(manifest_payload)
        manifest_path.chmod(0o600)
        temporary.chmod(0o700)
        temporary.replace(output)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def load_validated_registry_directory(
    registry_dir: str | Path,
    *,
    expected_registry_manifest_sha256: str,
    expected_devval_key_registry_hash: str | None,
    source_validation: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    _hash(expected_registry_manifest_sha256, "G102B2B_MANIFEST_ANCHOR_INVALID")
    raw, values = _read_artifact_directory(registry_dir)
    if hashlib.sha256(raw["manifest.json"]).hexdigest() != expected_registry_manifest_sha256:
        _fail("G102B2B_MANIFEST_ANCHOR_MISMATCH")
    manifest = _validate_manifest(values["manifest.json"], raw)
    request = validate_registry_request(values["registry-request.json"], **dict(source_validation))
    response = values["registry-response.json"]
    key_registry = values["trusted-key-registry.json"]
    expected = evaluate_registry_response(
        request,
        response,
        trusted_key_registry=key_registry,
        expected_devval_key_registry_hash=expected_devval_key_registry_hash,
        source_validation=source_validation,
    )
    registry = _validate_registry(values["devval-registry.json"], request)
    if registry != expected:
        _fail("G102B2B_REGISTRY_DERIVATION_MISMATCH")
    expected_manifest = _manifest_projection(
        request,
        registry,
        expected_devval_key_registry_hash=expected_devval_key_registry_hash,
        files=manifest["files"],
    )
    if manifest != expected_manifest:
        _fail("G102B2B_MANIFEST_DERIVATION_MISMATCH")
    return request, registry


def seed_hash_for_reveal(seed_reveal: str) -> str:
    _hash(seed_reveal, "G102B2B_SEED_REVEAL_INVALID")
    return hashlib.sha256(("GLE_DEVVAL_SEED_V1\n" + seed_reveal).encode()).hexdigest()


def signature_message(
    response_payload_hash: str,
    *,
    key_registry_hash: str,
    key_id: str,
    signer_id: str,
    role: str,
) -> bytes:
    _hash(response_payload_hash, "G102B2B_SIGNATURE_OBJECT_HASH_INVALID")
    _hash(key_registry_hash, "G102B2B_SIGNATURE_REGISTRY_HASH_INVALID")
    _identifier(key_id, "G102B2B_SIGNATURE_KEY_INVALID")
    _identifier(signer_id, "G102B2B_SIGNATURE_SIGNER_INVALID")
    if role not in REQUIRED_ROLES:
        _fail("G102B2B_SIGNATURE_ROLE_INVALID")
    return (
        "GLE_DEVVAL_REGISTRY_V1\n"
        f"{key_registry_hash}\n{key_id}\n{signer_id}\n{role}\n"
        f"{SIGNATURE_PURPOSE}\n{response_payload_hash}\n"
    ).encode()


def _build_request(
    context: Mapping[str, Any],
    *,
    registry_id: str,
    generation: int,
    requested_at: str,
    evaluated_at: str,
    policy: Mapping[str, Any] | None,
    seed_selection_file: str | Path | None,
    expected_seed_selection_file_sha256: str | None,
    prior_registry_dir: str | Path | None,
    expected_prior_manifest_sha256: str | None,
    expected_prior_devval_key_registry_hash: str | None,
) -> dict[str, Any]:
    _identifier(registry_id, "G102B2B_REGISTRY_ID_INVALID")
    if type(generation) is not int or generation <= 0:
        _fail("G102B2B_GENERATION_INVALID")
    requested = _utc(requested_at, "G102B2B_REQUEST_TIME_INVALID")
    evaluated = _utc(evaluated_at, "G102B2B_REQUEST_TIME_INVALID")
    cutoff = context["authority_binding"]["data_cutoff_at"]
    if not (_instant(cutoff) <= _instant(requested) <= _instant(evaluated)):
        _fail("G102B2B_REQUEST_TIME_INVALID")

    authority_verified = context["status"] == "VERIFIED"
    prior_binding = None
    retained_assignments: list[dict[str, Any]] = []
    prior_policy = None
    if any(item is not None for item in (
        prior_registry_dir,
        expected_prior_manifest_sha256,
        expected_prior_devval_key_registry_hash,
    )):
        if not authority_verified or not (
            prior_registry_dir is not None
            and expected_prior_manifest_sha256 is not None
            and expected_prior_devval_key_registry_hash is not None
        ):
            _fail("G102B2B_PRIOR_INPUT_INCOMPLETE")
        prior_request, prior_registry = _load_prior_registry_basic(
            prior_registry_dir,
            expected_manifest_sha256=expected_prior_manifest_sha256,
            expected_key_registry_hash=expected_prior_devval_key_registry_hash,
        )
        if (
            generation != prior_registry["generation"] + 1
            or registry_id != prior_registry["registry_id"]
            or prior_registry["status"] != "SIGNED_DETERMINISTIC_PARTITION"
        ):
            _fail("G102B2B_PRIOR_CHAIN_CONFLICT")
        prior_binding = {
            "manifest_sha256": expected_prior_manifest_sha256,
            "registry_hash": prior_registry["registry_hash"],
            "generation": prior_registry["generation"],
            "registry_state_root": prior_registry["registry_state_root"],
        }
        retained_assignments = prior_registry["assignments"]
        prior_policy = prior_request["policy"]
    elif generation != 1:
        _fail("G102B2B_PRIOR_REQUIRED")

    if not authority_verified:
        if any(item is not None for item in (
            policy,
            seed_selection_file,
            expected_seed_selection_file_sha256,
            prior_registry_dir,
            expected_prior_manifest_sha256,
            expected_prior_devval_key_registry_hash,
        )):
            _fail("G102B2B_BLOCKED_INPUT_INVALID")
        status = "BLOCKED"
        reasons = ["LINEAGE_AUTHORITY_NOT_VERIFIED"]
        policy_value = None
        eligible = []
    else:
        status = "PENDING_SIGNATURES"
        reasons = ["DEVVAL_SIGNATURES_PENDING"]
        if prior_policy is not None:
            if seed_selection_file is not None or expected_seed_selection_file_sha256 is not None:
                _fail("G102B2B_SEED_SELECTION_REUSE_INVALID")
            policy_value = _validate_policy(policy)
            if policy_value != prior_policy:
                _fail("G102B2B_POLICY_DRIFT")
        else:
            seed_selection = _load_seed_selection(
                seed_selection_file,
                expected_file_sha256=expected_seed_selection_file_sha256,
            )
            policy_value = _validate_policy(policy)
            if (
                policy_value["seed_selection_file_sha256"]
                != expected_seed_selection_file_sha256
                or policy_value["seed_hash"] != seed_selection["seed_hash"]
                or policy_value["seed_selected_at"] != seed_selection["selected_at"]
                or _instant(seed_selection["selected_at"]) > _instant(requested)
            ):
                _fail("G102B2B_SEED_SELECTION_BINDING_INVALID")
        eligible = context["eligible_lineages"]
        retained_by_id = {item["lineage_id"]: item for item in retained_assignments}
        eligible_by_id = {item["lineage_id"]: item for item in eligible}
        if not set(retained_by_id).issubset(eligible_by_id):
            _fail("G102B2B_PRIOR_LINEAGE_REMOVED")
        for lineage_id, assignment in retained_by_id.items():
            candidate = eligible_by_id[lineage_id]
            if (
                assignment["canonical_experiment_ids"] != candidate["canonical_experiment_ids"]
                or assignment["authority_membership_hash"] != candidate["authority_membership_hash"]
            ):
                _fail("G102B2B_PRIOR_MEMBERSHIP_DRIFT")

    request = {
        "schema_version": REQUEST_VERSION,
        "registry_id": registry_id,
        "generation": generation,
        "requested_at": requested,
        "evaluated_at": evaluated,
        "authority_binding": context["authority_binding"],
        "prior_binding": prior_binding,
        "policy": policy_value,
        "eligible_lineages": eligible,
        "retained_assignments": retained_assignments,
        "status": status,
        "reason_codes": reasons,
        "holdout_status": HOLDOUT_STATUS,
        "replay_eligible": False,
        "golden_eligible": False,
        "gate1_effect": "NONE",
        "not_dataset_receipt": True,
        "not_gate_receipt": True,
    }
    request["request_hash"] = canonical_hash(request)
    return _validate_request_shape(request)


def _load_authority_context(**kwargs: Any) -> dict[str, Any]:
    request, fragment = load_validated_authority_directory(
        kwargs["authority_dir"],
        expected_authority_manifest_sha256=kwargs["expected_authority_manifest_sha256"],
        expected_key_registry_hash=kwargs["expected_authority_key_registry_hash"],
        candidate_dir=kwargs["candidate_dir"],
        expected_candidate_manifest_sha256=kwargs["expected_candidate_manifest_sha256"],
        audit_dir=kwargs["audit_dir"],
        expected_audit_manifest_sha256=kwargs["expected_audit_manifest_sha256"],
    )
    authority_root = Path(kwargs["authority_dir"]).resolve()
    response_raw = _read_regular(authority_root / "authority-response.json")
    try:
        response = _canonical_json_document(response_raw, "G102B2B_AUTHORITY_RESPONSE_INVALID")
    except HistoricalLineageCandidateError as exc:
        raise LineageDevvalRegistryError(str(exc)) from exc
    eligible: list[dict[str, Any]] = []
    if fragment["status"] == "VERIFIED":
        if not isinstance(response, Mapping) or canonical_hash(response) != fragment["response_hash"]:
            _fail("G102B2B_AUTHORITY_RESPONSE_BINDING_INVALID")
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for node in response["lineage_nodes"]:
            grouped.setdefault(node["lineage_id"], []).append(node)
        for lineage_id, nodes in sorted(grouped.items()):
            canonical_ids = sorted(item["canonical_experiment_id"] for item in nodes)
            node_hashes = sorted(canonical_hash(item) for item in nodes)
            membership = {
                "lineage_id": lineage_id,
                "canonical_experiment_ids": canonical_ids,
                "authority_node_hashes": node_hashes,
            }
            eligible.append({
                **membership,
                "authority_membership_hash": canonical_hash(membership),
            })
    binding = {
        "authority_manifest_sha256": kwargs["expected_authority_manifest_sha256"],
        "authority_fragment_hash": fragment["fragment_hash"],
        "authority_request_hash": request["request_hash"],
        "authority_response_hash": fragment["response_hash"],
        "authority_id": fragment["authority_id"],
        "authority_key_registry_hash": kwargs["expected_authority_key_registry_hash"],
        "audit_manifest_sha256": kwargs["expected_audit_manifest_sha256"],
        "candidate_manifest_sha256": kwargs["expected_candidate_manifest_sha256"],
        "data_cutoff_at": request["input_binding"]["data_cutoff_at"],
        "status": fragment["status"],
        "authority_effect": fragment["authority_effect"],
    }
    return {
        "status": fragment["status"],
        "authority_binding": binding,
        "eligible_lineages": eligible,
    }


def _validate_request_shape(value: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "schema_version", "registry_id", "generation", "requested_at", "evaluated_at",
        "authority_binding", "prior_binding", "policy", "eligible_lineages",
        "retained_assignments", "status", "reason_codes", "holdout_status",
        "replay_eligible", "golden_eligible", "gate1_effect", "not_dataset_receipt",
        "not_gate_receipt", "request_hash",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G102B2B_REQUEST_INVALID")
    request = dict(value)
    _identifier(request["registry_id"], "G102B2B_REQUEST_INVALID")
    if request["schema_version"] != REQUEST_VERSION:
        _fail("G102B2B_REQUEST_INVALID")
    if type(request["generation"]) is not int or request["generation"] <= 0:
        _fail("G102B2B_REQUEST_INVALID")
    _utc(request["requested_at"], "G102B2B_REQUEST_INVALID")
    _utc(request["evaluated_at"], "G102B2B_REQUEST_INVALID")
    _validate_authority_binding(request["authority_binding"])
    _validate_prior_binding(request["prior_binding"])
    if request["policy"] is not None:
        _validate_policy(request["policy"])
    _validate_eligible_lineages(request["eligible_lineages"])
    _validate_assignments(request["retained_assignments"], allow_empty=True)
    if request["status"] not in {"BLOCKED", "PENDING_SIGNATURES"}:
        _fail("G102B2B_REQUEST_INVALID")
    if (
        not isinstance(request["reason_codes"], list)
        or not request["reason_codes"]
        or request["reason_codes"] != sorted(set(request["reason_codes"]))
        or any(not isinstance(item, str) or not _REASON_RE.fullmatch(item) for item in request["reason_codes"])
        or request["holdout_status"] != HOLDOUT_STATUS
        or request["replay_eligible"] is not False
        or request["golden_eligible"] is not False
        or request["gate1_effect"] != "NONE"
        or request["not_dataset_receipt"] is not True
        or request["not_gate_receipt"] is not True
    ):
        _fail("G102B2B_REQUEST_CEILING_INVALID")
    if request["status"] == "BLOCKED" and (
        request["policy"] is not None
        or request["eligible_lineages"] != []
        or request["retained_assignments"] != []
        or request["prior_binding"] is not None
    ):
        _fail("G102B2B_REQUEST_STATE_INVALID")
    if request["status"] == "PENDING_SIGNATURES" and (
        request["policy"] is None
        or not request["eligible_lineages"]
        or request["authority_binding"]["status"] != "VERIFIED"
        or request["authority_binding"]["authority_effect"]
        != "LINEAGE_AUTHORITY_ATTESTATION_VERIFIED"
    ):
        _fail("G102B2B_REQUEST_STATE_INVALID")
    if request["request_hash"] != canonical_hash({
        key: item for key, item in request.items() if key != "request_hash"
    }):
        _fail("G102B2B_REQUEST_HASH_INVALID")
    return request


def _validate_authority_binding(value: Any) -> None:
    keys = {
        "authority_manifest_sha256", "authority_fragment_hash", "authority_request_hash",
        "authority_response_hash", "authority_id", "authority_key_registry_hash",
        "audit_manifest_sha256", "candidate_manifest_sha256", "data_cutoff_at", "status",
        "authority_effect",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G102B2B_AUTHORITY_BINDING_INVALID")
    for key in (
        "authority_manifest_sha256", "authority_fragment_hash", "authority_request_hash",
        "audit_manifest_sha256", "candidate_manifest_sha256",
    ):
        _hash(value[key], "G102B2B_AUTHORITY_BINDING_INVALID")
    for key in ("authority_response_hash", "authority_key_registry_hash"):
        if value[key] is not None:
            _hash(value[key], "G102B2B_AUTHORITY_BINDING_INVALID")
    if value["authority_id"] is not None:
        _identifier(value["authority_id"], "G102B2B_AUTHORITY_BINDING_INVALID")
    _utc(value["data_cutoff_at"], "G102B2B_AUTHORITY_BINDING_INVALID")
    if value["status"] not in {"MISSING", "VERIFIED", "CONFLICT", "INVALID"}:
        _fail("G102B2B_AUTHORITY_BINDING_INVALID")
    verified = value["status"] == "VERIFIED"
    if (
        (value["authority_effect"] == "LINEAGE_AUTHORITY_ATTESTATION_VERIFIED") != verified
        or (value["authority_id"] is not None) != verified
        or (value["authority_response_hash"] is not None) != verified
        or (value["authority_key_registry_hash"] is not None) != verified
    ):
        _fail("G102B2B_AUTHORITY_BINDING_INVALID")


def _validate_prior_binding(value: Any) -> None:
    if value is None:
        return
    keys = {"manifest_sha256", "registry_hash", "generation", "registry_state_root"}
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G102B2B_PRIOR_BINDING_INVALID")
    _hash(value["manifest_sha256"], "G102B2B_PRIOR_BINDING_INVALID")
    _hash(value["registry_hash"], "G102B2B_PRIOR_BINDING_INVALID")
    _hash(value["registry_state_root"], "G102B2B_PRIOR_BINDING_INVALID")
    if type(value["generation"]) is not int or value["generation"] <= 0:
        _fail("G102B2B_PRIOR_BINDING_INVALID")


def _validate_policy(value: Any) -> dict[str, Any]:
    keys = {
        "schema_version", "policy_id", "unit", "allowed_splits",
        "validation_threshold_bps", "algorithm", "seed_selection_file_sha256",
        "seed_hash", "seed_selected_at", "holdout_status", "policy_hash",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G102B2B_POLICY_INVALID")
    policy = dict(value)
    _identifier(policy["policy_id"], "G102B2B_POLICY_INVALID")
    if (
        policy["schema_version"] != POLICY_VERSION
        or policy["unit"] != "LINEAGE_ID"
        or policy["allowed_splits"] != ALLOWED_SPLITS
        or type(policy["validation_threshold_bps"]) is not int
        or not 1 <= policy["validation_threshold_bps"] <= 9999
        or policy["algorithm"] != ASSIGNMENT_ALGORITHM
        or policy["holdout_status"] != HOLDOUT_STATUS
    ):
        _fail("G102B2B_POLICY_INVALID")
    _hash(policy["seed_selection_file_sha256"], "G102B2B_POLICY_INVALID")
    _hash(policy["seed_hash"], "G102B2B_POLICY_INVALID")
    _utc(policy["seed_selected_at"], "G102B2B_POLICY_INVALID")
    if policy["policy_hash"] != canonical_hash({
        key: item for key, item in policy.items() if key != "policy_hash"
    }):
        _fail("G102B2B_POLICY_INVALID")
    return policy


def _validate_eligible_lineages(values: Any) -> list[dict[str, Any]]:
    keys = {
        "lineage_id", "canonical_experiment_ids", "authority_node_hashes",
        "authority_membership_hash",
    }
    if not isinstance(values, list) or values != sorted(
        values,
        key=lambda item: item.get("lineage_id", "") if isinstance(item, Mapping) else "",
    ):
        _fail("G102B2B_ELIGIBLE_LINEAGES_INVALID")
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, Mapping) or set(item) != keys:
            _fail("G102B2B_ELIGIBLE_LINEAGES_INVALID")
        _identifier(item["lineage_id"], "G102B2B_ELIGIBLE_LINEAGES_INVALID")
        if item["lineage_id"] in seen:
            _fail("G102B2B_ELIGIBLE_LINEAGES_INVALID")
        seen.add(item["lineage_id"])
        for field in ("canonical_experiment_ids", "authority_node_hashes"):
            if (
                not isinstance(item[field], list)
                or not item[field]
                or item[field] != sorted(set(item[field]))
            ):
                _fail("G102B2B_ELIGIBLE_LINEAGES_INVALID")
        for identifier in item["canonical_experiment_ids"]:
            _identifier(identifier, "G102B2B_ELIGIBLE_LINEAGES_INVALID")
        for node_hash in item["authority_node_hashes"]:
            _hash(node_hash, "G102B2B_ELIGIBLE_LINEAGES_INVALID")
        expected = canonical_hash({
            "lineage_id": item["lineage_id"],
            "canonical_experiment_ids": item["canonical_experiment_ids"],
            "authority_node_hashes": item["authority_node_hashes"],
        })
        if item["authority_membership_hash"] != expected:
            _fail("G102B2B_ELIGIBLE_LINEAGES_INVALID")
    return values


def _load_seed_selection(
    path: str | Path | None, *, expected_file_sha256: str | None,
) -> dict[str, Any]:
    if path is None or expected_file_sha256 is None:
        _fail("G102B2B_SEED_SELECTION_MISSING")
    _hash(expected_file_sha256, "G102B2B_SEED_SELECTION_ANCHOR_INVALID")
    raw = _read_regular(Path(path))
    if len(raw) > MAX_ARTIFACT_BYTES or hashlib.sha256(raw).hexdigest() != expected_file_sha256:
        _fail("G102B2B_SEED_SELECTION_ANCHOR_MISMATCH")
    try:
        value = _canonical_json_document(raw, "G102B2B_SEED_SELECTION_INVALID")
    except HistoricalLineageCandidateError as exc:
        raise LineageDevvalRegistryError(str(exc)) from exc
    keys = {
        "schema_version", "selection_id", "selected_at", "seed_hash",
        "selection_hash",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G102B2B_SEED_SELECTION_INVALID")
    value = dict(value)
    _identifier(value["selection_id"], "G102B2B_SEED_SELECTION_INVALID")
    _utc(value["selected_at"], "G102B2B_SEED_SELECTION_INVALID")
    _hash(value["seed_hash"], "G102B2B_SEED_SELECTION_INVALID")
    if (
        value["schema_version"] != SEED_SELECTION_VERSION
        or value["selection_hash"] != canonical_hash({
            key: item for key, item in value.items() if key != "selection_hash"
        })
    ):
        _fail("G102B2B_SEED_SELECTION_INVALID")
    return value


def _validate_response(
    request: Mapping[str, Any], value: Mapping[str, Any], key_registry: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    keys = {
        "schema_version", "request_hash", "authorized_at", "seed_reveal",
        "assignment_payload_hash", "response_payload_hash", "signatures",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G102B2B_RESPONSE_INVALID")
    response = dict(value)
    authorized = _utc(response["authorized_at"], "G102B2B_RESPONSE_INVALID")
    if (
        response["schema_version"] != RESPONSE_VERSION
        or response["request_hash"] != request["request_hash"]
        or not (
            _instant(request["requested_at"]) <= _instant(authorized)
            <= _instant(request["evaluated_at"])
        )
    ):
        _fail("G102B2B_RESPONSE_INVALID")
    _hash(response["seed_reveal"], "G102B2B_RESPONSE_INVALID")
    if seed_hash_for_reveal(response["seed_reveal"]) != request["policy"]["seed_hash"]:
        _fail("G102B2B_SEED_REVEAL_MISMATCH")
    assignments = _derive_assignments(request, response["seed_reveal"])
    if response["assignment_payload_hash"] != canonical_hash(assignments):
        _fail("G102B2B_ASSIGNMENT_PAYLOAD_MISMATCH")
    expected_payload_hash = canonical_hash({
        key: item for key, item in response.items()
        if key not in {"response_payload_hash", "signatures"}
    })
    if response["response_payload_hash"] != expected_payload_hash:
        _fail("G102B2B_RESPONSE_HASH_MISMATCH")
    _validate_signatures(response, key_registry)
    return response, assignments


def _derive_assignments(request: Mapping[str, Any], seed_reveal: str) -> list[dict[str, Any]]:
    retained_by_id = {
        item["lineage_id"]: item for item in request["retained_assignments"]
    }
    assignments: list[dict[str, Any]] = []
    policy = request["policy"]
    threshold = ((1 << 64) * policy["validation_threshold_bps"]) // 10000
    for lineage in request["eligible_lineages"]:
        digest = hashlib.sha256((
            "GLE_DEVVAL_ASSIGNMENT_V1\n"
            f"{policy['policy_hash']}\n{seed_reveal}\n{lineage['lineage_id']}\n"
        ).encode()).digest()
        score = int.from_bytes(digest[:8], "big")
        assignment = {
            "lineage_id": lineage["lineage_id"],
            "canonical_experiment_ids": lineage["canonical_experiment_ids"],
            "authority_membership_hash": lineage["authority_membership_hash"],
            "split": "VALIDATION" if score < threshold else "DEV",
            "score_u64": score,
            "policy_hash": policy["policy_hash"],
        }
        assignment["assignment_hash"] = canonical_hash(assignment)
        retained = retained_by_id.get(lineage["lineage_id"])
        if retained is not None and retained != assignment:
            _fail("G102B2B_PRIOR_ASSIGNMENT_DETERMINISM_CONFLICT")
        assignments.append(assignment)
    assignments.sort(key=lambda item: item["lineage_id"])
    return _validate_assignments(assignments, allow_empty=False)


def _validate_assignments(values: Any, *, allow_empty: bool) -> list[dict[str, Any]]:
    keys = {
        "lineage_id", "canonical_experiment_ids", "authority_membership_hash", "split",
        "score_u64", "policy_hash", "assignment_hash",
    }
    if (
        not isinstance(values, list)
        or (not allow_empty and not values)
        or values != sorted(
            values,
            key=lambda item: item.get("lineage_id", "") if isinstance(item, Mapping) else "",
        )
    ):
        _fail("G102B2B_ASSIGNMENTS_INVALID")
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, Mapping) or set(item) != keys:
            _fail("G102B2B_ASSIGNMENTS_INVALID")
        _identifier(item["lineage_id"], "G102B2B_ASSIGNMENTS_INVALID")
        if item["lineage_id"] in seen:
            _fail("G102B2B_ASSIGNMENTS_INVALID")
        seen.add(item["lineage_id"])
        if (
            not isinstance(item["canonical_experiment_ids"], list)
            or not item["canonical_experiment_ids"]
            or item["canonical_experiment_ids"] != sorted(set(item["canonical_experiment_ids"]))
            or item["split"] not in ALLOWED_SPLITS
            or type(item["score_u64"]) is not int
            or not 0 <= item["score_u64"] < 1 << 64
        ):
            _fail("G102B2B_ASSIGNMENTS_INVALID")
        for identifier in item["canonical_experiment_ids"]:
            _identifier(identifier, "G102B2B_ASSIGNMENTS_INVALID")
        _hash(item["authority_membership_hash"], "G102B2B_ASSIGNMENTS_INVALID")
        _hash(item["policy_hash"], "G102B2B_ASSIGNMENTS_INVALID")
        if item["assignment_hash"] != canonical_hash({
            key: value for key, value in item.items() if key != "assignment_hash"
        }):
            _fail("G102B2B_ASSIGNMENTS_INVALID")
    return values


def _registry_fragment(
    request: Mapping[str, Any],
    *,
    status: str,
    reasons: list[str],
    assignments: list[dict[str, Any]],
    response_hash: str | None,
    key_registry_hash: str | None,
) -> dict[str, Any]:
    verified = status == "SIGNED_DETERMINISTIC_PARTITION"
    if verified:
        registry_state_root = canonical_hash({
            "registry_id": request["registry_id"],
            "generation": request["generation"],
            "policy_hash": request["policy"]["policy_hash"],
            "prior_manifest_sha256": (
                request["prior_binding"]["manifest_sha256"]
                if request["prior_binding"] else None
            ),
            "assignment_hashes": [item["assignment_hash"] for item in assignments],
        })
    else:
        registry_state_root = None
    registry = {
        "schema_version": REGISTRY_VERSION,
        "registry_id": request["registry_id"],
        "generation": request["generation"],
        "request_hash": request["request_hash"],
        "authority_binding": request["authority_binding"],
        "policy_hash": request["policy"]["policy_hash"] if request["policy"] else None,
        "prior_binding": request["prior_binding"],
        "response_hash": response_hash,
        "key_registry_hash": key_registry_hash,
        "registry_state_root": registry_state_root,
        "assignments": assignments,
        "status": status,
        "reason_codes": sorted(set(reasons)),
        "trust_status": "SIGNED_DETERMINISTIC_PARTITION" if verified else "NO_SPLIT_EFFECT",
        "split_effect": "DEV_VALIDATION_ASSIGNMENT_ONLY" if verified else "NONE",
        "holdout_status": HOLDOUT_STATUS,
        "replay_eligible": False,
        "golden_eligible": False,
        "gate1_effect": "NONE",
        "not_dataset_receipt": True,
        "not_gate_receipt": True,
    }
    registry["registry_hash"] = canonical_hash(registry)
    return _validate_registry(registry, request)


def _validate_registry(value: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "schema_version", "registry_id", "generation", "request_hash", "authority_binding",
        "policy_hash", "prior_binding", "response_hash", "key_registry_hash", "registry_state_root",
        "assignments", "status", "reason_codes", "trust_status", "split_effect",
        "holdout_status", "replay_eligible", "golden_eligible", "gate1_effect",
        "not_dataset_receipt", "not_gate_receipt", "registry_hash",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G102B2B_REGISTRY_INVALID")
    registry = dict(value)
    if (
        registry["schema_version"] != REGISTRY_VERSION
        or registry["registry_id"] != request["registry_id"]
        or registry["generation"] != request["generation"]
        or registry["request_hash"] != request["request_hash"]
        or registry["authority_binding"] != request["authority_binding"]
        or registry["prior_binding"] != request["prior_binding"]
        or registry["status"] not in {"BLOCKED", "PENDING_SIGNATURES", "SIGNED_DETERMINISTIC_PARTITION"}
        or registry["holdout_status"] != HOLDOUT_STATUS
        or registry["replay_eligible"] is not False
        or registry["golden_eligible"] is not False
        or registry["gate1_effect"] != "NONE"
        or registry["not_dataset_receipt"] is not True
        or registry["not_gate_receipt"] is not True
    ):
        _fail("G102B2B_REGISTRY_INVALID")
    verified = registry["status"] == "SIGNED_DETERMINISTIC_PARTITION"
    _validate_assignments(registry["assignments"], allow_empty=not verified)
    if (
        not isinstance(registry["reason_codes"], list)
        or registry["reason_codes"] != sorted(set(registry["reason_codes"]))
        or any(
            not isinstance(item, str) or not _REASON_RE.fullmatch(item)
            for item in registry["reason_codes"]
        )
        or registry["policy_hash"]
        != (request["policy"]["policy_hash"] if request["policy"] else None)
    ):
        _fail("G102B2B_REGISTRY_STATE_INVALID")
    for key in ("response_hash", "key_registry_hash", "registry_state_root"):
        if registry[key] is not None:
            _hash(registry[key], "G102B2B_REGISTRY_STATE_INVALID")
    if (
        (not registry["reason_codes"]) != verified
        or (registry["trust_status"] == "SIGNED_DETERMINISTIC_PARTITION") != verified
        or (registry["split_effect"] == "DEV_VALIDATION_ASSIGNMENT_ONLY") != verified
        or (registry["registry_state_root"] is not None) != verified
        or (registry["response_hash"] is not None) != verified
        or (registry["key_registry_hash"] is not None) != verified
    ):
        _fail("G102B2B_REGISTRY_STATE_INVALID")
    if registry["registry_hash"] != canonical_hash({
        key: item for key, item in registry.items() if key != "registry_hash"
    }):
        _fail("G102B2B_REGISTRY_HASH_INVALID")
    return registry


def _validate_key_registry(
    value: Mapping[str, Any], *, expected_key_registry_hash: str,
) -> dict[str, Any]:
    _hash(expected_key_registry_hash, "G102B2B_KEY_REGISTRY_ANCHOR_INVALID")
    keys = {"schema_version", "registry_id", "keys", "registry_hash"}
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G102B2B_KEY_REGISTRY_INVALID")
    registry = dict(value)
    if (
        registry["schema_version"] != KEY_REGISTRY_VERSION
        or registry["registry_hash"] != canonical_hash({
            key: item for key, item in registry.items() if key != "registry_hash"
        })
        or registry["registry_hash"] != expected_key_registry_hash
    ):
        _fail("G102B2B_KEY_REGISTRY_INVALID")
    _identifier(registry["registry_id"], "G102B2B_KEY_REGISTRY_INVALID")
    key_fields = {
        "key_id", "signer_id", "role", "purposes", "algorithm", "status",
        "valid_from", "valid_until", "public_key_pem",
    }
    if (
        not isinstance(registry["keys"], list)
        or len(registry["keys"]) < len(REQUIRED_ROLES)
        or registry["keys"] != sorted(
            registry["keys"], key=lambda item: item.get("key_id", "") if isinstance(item, Mapping) else "",
        )
    ):
        _fail("G102B2B_KEY_REGISTRY_INVALID")
    key_ids: set[str] = set()
    signers: set[str] = set()
    fingerprints: set[str] = set()
    for item in registry["keys"]:
        if not isinstance(item, Mapping) or set(item) != key_fields:
            _fail("G102B2B_KEY_REGISTRY_INVALID")
        _identifier(item["key_id"], "G102B2B_KEY_REGISTRY_INVALID")
        _identifier(item["signer_id"], "G102B2B_KEY_REGISTRY_INVALID")
        if (
            item["key_id"] in key_ids
            or item["signer_id"] in signers
            or item["role"] not in REQUIRED_ROLES
            or item["purposes"] != [SIGNATURE_PURPOSE]
            or item["algorithm"] != SIGNATURE_ALGORITHM
            or item["status"] != "ACTIVE"
            or not isinstance(item["public_key_pem"], str)
            or not _PEM_RE.fullmatch(item["public_key_pem"])
        ):
            _fail("G102B2B_KEY_REGISTRY_INVALID")
        key_ids.add(item["key_id"])
        signers.add(item["signer_id"])
        bits, fingerprint = _rsa_public_key_metadata(item["public_key_pem"])
        if bits < MINIMUM_RSA_BITS or fingerprint in fingerprints:
            _fail("G102B2B_KEY_REGISTRY_INVALID")
        fingerprints.add(fingerprint)
        if _instant(_utc(item["valid_until"], "G102B2B_KEY_REGISTRY_INVALID")) <= _instant(
            _utc(item["valid_from"], "G102B2B_KEY_REGISTRY_INVALID")
        ):
            _fail("G102B2B_KEY_REGISTRY_INVALID")
    return registry


def _validate_signatures(response: Mapping[str, Any], registry: Mapping[str, Any]) -> None:
    fields = {
        "algorithm", "key_id", "signer_id", "role", "purpose", "object_hash",
        "key_registry_hash", "signed_at", "signature_base64",
    }
    signatures = response["signatures"]
    if not isinstance(signatures, list) or len(signatures) != len(REQUIRED_ROLES):
        _fail("G102B2B_SIGNATURE_SET_INVALID")
    keys = {item["key_id"]: item for item in registry["keys"]}
    roles: list[str] = []
    signer_ids: list[str] = []
    key_ids: list[str] = []
    for signature in signatures:
        if not isinstance(signature, Mapping) or set(signature) != fields:
            _fail("G102B2B_SIGNATURE_INVALID")
        key = keys.get(signature["key_id"])
        signed_at = _utc(signature["signed_at"], "G102B2B_SIGNATURE_INVALID")
        if (
            key is None
            or signature["algorithm"] != SIGNATURE_ALGORITHM
            or signature["signer_id"] != key["signer_id"]
            or signature["role"] != key["role"]
            or signature["purpose"] != SIGNATURE_PURPOSE
            or signature["object_hash"] != response["response_payload_hash"]
            or signature["key_registry_hash"] != registry["registry_hash"]
            or signed_at != response["authorized_at"]
            or not (_instant(key["valid_from"]) <= _instant(signed_at) <= _instant(key["valid_until"]))
        ):
            _fail("G102B2B_SIGNATURE_INVALID")
        try:
            signature_bytes = base64.b64decode(signature["signature_base64"], validate=True)
        except (TypeError, ValueError) as exc:
            raise LineageDevvalRegistryError("G102B2B_SIGNATURE_INVALID") from exc
        if not signature_bytes or not _verify_rsa_sha256(
            key["public_key_pem"],
            signature_bytes,
            signature_message(
                response["response_payload_hash"],
                key_registry_hash=registry["registry_hash"],
                key_id=signature["key_id"],
                signer_id=signature["signer_id"],
                role=signature["role"],
            ),
        ):
            _fail("G102B2B_SIGNATURE_INVALID")
        roles.append(signature["role"])
        signer_ids.append(signature["signer_id"])
        key_ids.append(signature["key_id"])
    if (
        roles != list(REQUIRED_ROLES)
        or len(set(signer_ids)) != len(REQUIRED_ROLES)
        or len(set(key_ids)) != len(REQUIRED_ROLES)
    ):
        _fail("G102B2B_SIGNATURE_SET_INVALID")


def _load_prior_registry_basic(
    directory: str | Path,
    *,
    expected_manifest_sha256: str,
    expected_key_registry_hash: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw, values = _read_artifact_directory(directory)
    _hash(expected_manifest_sha256, "G102B2B_PRIOR_MANIFEST_ANCHOR_INVALID")
    if hashlib.sha256(raw["manifest.json"]).hexdigest() != expected_manifest_sha256:
        _fail("G102B2B_PRIOR_MANIFEST_ANCHOR_MISMATCH")
    _validate_manifest(values["manifest.json"], raw)
    request = _validate_request_shape(values["registry-request.json"])
    if request["status"] != "PENDING_SIGNATURES":
        _fail("G102B2B_PRIOR_REGISTRY_INVALID")
    key_registry = _validate_key_registry(
        values["trusted-key-registry.json"],
        expected_key_registry_hash=expected_key_registry_hash,
    )
    response, assignments = _validate_response(
        request, values["registry-response.json"], key_registry,
    )
    expected = _registry_fragment(
        request,
        status="SIGNED_DETERMINISTIC_PARTITION",
        reasons=[],
        assignments=assignments,
        response_hash=canonical_hash(response),
        key_registry_hash=key_registry["registry_hash"],
    )
    registry = _validate_registry(values["devval-registry.json"], request)
    if registry != expected:
        _fail("G102B2B_PRIOR_REGISTRY_INVALID")
    expected_manifest = _manifest_projection(
        request,
        registry,
        expected_devval_key_registry_hash=expected_key_registry_hash,
        files=values["manifest.json"]["files"],
    )
    if values["manifest.json"] != expected_manifest:
        _fail("G102B2B_PRIOR_MANIFEST_INVALID")
    return request, registry


def _read_artifact_directory(
    directory: str | Path,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    root_input = Path(directory).expanduser()
    if root_input.is_symlink():
        _fail("G102B2B_ARTIFACT_DIRECTORY_INVALID")
    raw: dict[str, bytes] = {}
    values: dict[str, Any] = {}
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        root_fd = os.open(root_input, directory_flags)
    except OSError as exc:
        raise LineageDevvalRegistryError("G102B2B_ARTIFACT_DIRECTORY_INVALID") from exc
    try:
        root_before = os.fstat(root_fd)
        if not stat.S_ISDIR(root_before.st_mode) or stat.S_IMODE(root_before.st_mode) != 0o700:
            _fail("G102B2B_ARTIFACT_MODE_INVALID")
        if set(os.listdir(root_fd)) != EXACT_FILES:
            _fail("G102B2B_ARTIFACT_FILE_SET_INVALID")
        for name in sorted(EXACT_FILES):
            try:
                file_fd = os.open(name, file_flags, dir_fd=root_fd)
            except OSError as exc:
                raise LineageDevvalRegistryError("G102B2B_ARTIFACT_FILE_INVALID") from exc
            try:
                before = os.fstat(file_fd)
                if not stat.S_ISREG(before.st_mode):
                    _fail("G102B2B_ARTIFACT_FILE_INVALID")
                if stat.S_IMODE(before.st_mode) != 0o600:
                    _fail("G102B2B_ARTIFACT_MODE_INVALID")
                if before.st_size > MAX_ARTIFACT_BYTES:
                    _fail("G102B2B_ARTIFACT_TOO_LARGE")
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(file_fd, min(65536, MAX_ARTIFACT_BYTES + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > MAX_ARTIFACT_BYTES:
                        _fail("G102B2B_ARTIFACT_TOO_LARGE")
                after = os.fstat(file_fd)
                if (
                    (
                        before.st_dev, before.st_ino, before.st_mode,
                        before.st_size, before.st_mtime_ns, before.st_ctime_ns,
                    )
                    != (
                        after.st_dev, after.st_ino, after.st_mode,
                        after.st_size, after.st_mtime_ns, after.st_ctime_ns,
                    )
                    or total != after.st_size
                ):
                    _fail("G102B2B_ARTIFACT_CHANGED_DURING_READ")
                payload = b"".join(chunks)
            finally:
                os.close(file_fd)
            raw[name] = payload
            values[name] = _canonical_json_document(payload, "G102B2B_ARTIFACT_JSON_INVALID")
        root_after = os.fstat(root_fd)
        if (
            set(os.listdir(root_fd)) != EXACT_FILES
            or (
                root_before.st_dev, root_before.st_ino, root_before.st_mode,
                root_before.st_mtime_ns, root_before.st_ctime_ns,
            )
            != (
                root_after.st_dev, root_after.st_ino, root_after.st_mode,
                root_after.st_mtime_ns, root_after.st_ctime_ns,
            )
        ):
            _fail("G102B2B_ARTIFACT_CHANGED_DURING_READ")
    except HistoricalLineageCandidateError as exc:
        raise LineageDevvalRegistryError(str(exc)) from exc
    finally:
        os.close(root_fd)
    return raw, values


def _manifest_projection(
    request: Mapping[str, Any],
    registry: Mapping[str, Any],
    *,
    expected_devval_key_registry_hash: str | None,
    files: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = {
        "schema_version": MANIFEST_VERSION,
        "registry_id": request["registry_id"],
        "generation": request["generation"],
        "request_hash": request["request_hash"],
        "registry_hash": registry["registry_hash"],
        "status": registry["status"],
        "trust_status": registry["trust_status"],
        "expected_devval_key_registry_hash": expected_devval_key_registry_hash,
        "assignment_count": len(registry["assignments"]),
        "split_effect": registry["split_effect"],
        "holdout_status": HOLDOUT_STATUS,
        "replay_eligible": False,
        "golden_eligible": False,
        "gate1_effect": "NONE",
        "not_dataset_receipt": True,
        "not_gate_receipt": True,
        "files": dict(files),
    }
    manifest["manifest_hash"] = canonical_hash(manifest)
    return manifest


def _validate_manifest(value: Any, raw: Mapping[str, bytes]) -> dict[str, Any]:
    keys = {
        "schema_version", "registry_id", "generation", "request_hash", "registry_hash",
        "status", "trust_status", "expected_devval_key_registry_hash", "assignment_count",
        "split_effect", "holdout_status", "replay_eligible", "golden_eligible",
        "gate1_effect", "not_dataset_receipt", "not_gate_receipt", "files", "manifest_hash",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G102B2B_MANIFEST_INVALID")
    manifest = dict(value)
    payload_names = EXACT_FILES - {"manifest.json"}
    if (
        manifest["schema_version"] != MANIFEST_VERSION
        or not isinstance(manifest["files"], Mapping)
        or set(manifest["files"]) != payload_names
        or manifest["manifest_hash"] != canonical_hash({
            key: item for key, item in manifest.items() if key != "manifest_hash"
        })
    ):
        _fail("G102B2B_MANIFEST_INVALID")
    for name in sorted(payload_names):
        descriptor = manifest["files"][name]
        if not isinstance(descriptor, Mapping) or set(descriptor) != {"sha256", "size_bytes"}:
            _fail("G102B2B_MANIFEST_INVALID")
        if (
            descriptor["sha256"] != hashlib.sha256(raw[name]).hexdigest()
            or descriptor["size_bytes"] != len(raw[name])
        ):
            _fail("G102B2B_MANIFEST_FILE_MISMATCH")
    return manifest


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        _fail(code)
    return value


def _hash(value: Any, code: str) -> str:
    try:
        return validate_sha256(value, code=code)
    except ValueError as exc:
        raise LineageDevvalRegistryError(str(exc)) from exc


def _utc(value: Any, code: str) -> str:
    try:
        result = validate_utc(value, code=code)
    except ValueError as exc:
        raise LineageDevvalRegistryError(str(exc)) from exc
    assert isinstance(result, str)
    return result


def _instant(value: str) -> str:
    # validate_utc freezes a single second-resolution Z representation, so lexical order is instant order.
    return value
