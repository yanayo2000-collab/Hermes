from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.growth.canonical_evaluation_contracts import (
    OBJECTIVE_VERSION,
    canonical_hash,
    canonical_json,
    content_hash,
    validate_objective_contract,
)
from app.growth.exact_id_attribution_audit import ATTRIBUTION_VERSION, DEDUPE_VERSION
from app.growth.gate0_feasibility_assessment import (
    QUALIFICATION_VERSION,
    SOURCE_CONTRACT,
    SOURCE_METRIC,
)


CONTRACT_VERSION = "gle-g1-01b-signed-objective-authority-v1"
PROPOSAL_VERSION = "gle-g1-01b-objective-proposal-v1"
REQUEST_VERSION = "gle-g1-01b-objective-authority-request-v1"
RESPONSE_VERSION = "gle-g1-01b-objective-authority-response-v1"
REGISTRY_VERSION = "gle-g1-01b-objective-key-registry-v1"
MANIFEST_VERSION = "gle-g1-01b-objective-authority-manifest-v1"
REQUEST_MANIFEST_VERSION = "gle-g1-01b-objective-authority-request-manifest-v1"
METRIC_CONTRACT_VERSION = "gle-qualified-join-cpa-metric-contract-v1"
PRIMARY_METRIC_DEFINITION_VERSION = "gle-qualified-join-cpa-v1"
SIGNATURE_ALGORITHM = "RSA_PKCS1_V1_5_SHA256"
OPENSSL_BINARY = "/usr/bin/openssl"
MINIMUM_RSA_BITS = 2048
REQUIRED_ROLES = ("BUSINESS_OWNER", "DATA_OWNER", "TECH_OWNER")
ROLE_PURPOSES = {
    "BUSINESS_OWNER": "OBJECTIVE_BUSINESS_APPROVAL",
    "DATA_OWNER": "OBJECTIVE_METRIC_CONTRACT_APPROVAL",
    "TECH_OWNER": "OBJECTIVE_TECHNICAL_BINDING_APPROVAL",
}
EXACT_FILES = frozenset({
    "manifest.json",
    "authority-request.json",
    "authority-response.json",
    "objective-contract.json",
})
REQUEST_EXACT_FILES = frozenset({"manifest.json", "authority-request.json"})
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 6 * 1024 * 1024

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PEM_RE = re.compile(
    r"^-----BEGIN PUBLIC KEY-----\n(?:[A-Za-z0-9+/]{1,64}\n)+"
    r"-----END PUBLIC KEY-----\n$"
)


class ObjectiveContractAuthorityError(RuntimeError):
    pass


def _fail(code: str) -> None:
    raise ObjectiveContractAuthorityError(code)


def build_objective_authority_request(
    *,
    proposal_file: str | Path,
    expected_proposal_sha256: str,
    request_id: str,
    requested_at: str,
    evaluated_at: str,
    trusted_key_registry_file: str | Path,
    expected_key_registry_sha256: str,
    expected_key_registry_hash: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    proposal_anchor = _sha(expected_proposal_sha256, "G101B_PROPOSAL_ANCHOR_INVALID")
    proposal = _validate_proposal(
        _read_external_json(Path(proposal_file), proposal_anchor, "G101B_PROPOSAL")
    )
    registry_anchor = _sha(expected_key_registry_sha256, "G101B_REGISTRY_RAW_ANCHOR_INVALID")
    registry_hash = _sha(expected_key_registry_hash, "G101B_REGISTRY_HASH_INVALID")
    registry = _validate_key_registry(
        _read_external_json(Path(trusted_key_registry_file), registry_anchor, "G101B_REGISTRY"),
        expected_registry_hash=registry_hash,
    )
    _identifier(request_id, "G101B_REQUEST_ID_INVALID")
    requested = _utc(requested_at, "G101B_REQUEST_TIME_INVALID")
    evaluated = _utc(evaluated_at, "G101B_EVALUATED_TIME_INVALID")
    if not (
        _instant(proposal["created_at"])
        <= _instant(requested)
        <= _instant(evaluated)
    ):
        _fail("G101B_REQUEST_TIME_ORDER_INVALID")
    source_binding = {
        "primary_metric_definition_version": PRIMARY_METRIC_DEFINITION_VERSION,
        "primary_metric_contract_version": METRIC_CONTRACT_VERSION,
        "primary_metric_contract_hash": proposal["primary_metric_contract"]["contract_hash"],
        "qualification_rule_version": QUALIFICATION_VERSION,
        "qualified_source_contract": SOURCE_CONTRACT,
        "qualified_source_metric": SOURCE_METRIC,
        "attribution_version": ATTRIBUTION_VERSION,
        "dedup_version": DEDUPE_VERSION,
    }
    authority_contract = {
        "algorithm": SIGNATURE_ALGORITHM,
        "minimum_rsa_bits": MINIMUM_RSA_BITS,
        "roles": list(REQUIRED_ROLES),
        "role_purposes": dict(ROLE_PURPOSES),
        "quorum": len(REQUIRED_ROLES),
    }
    request: dict[str, Any] = {
        "schema_version": REQUEST_VERSION,
        "contract_version": CONTRACT_VERSION,
        "request_id": request_id,
        "requested_at": requested,
        "evaluated_at": evaluated,
        "proposal_binding": {
            "proposal_sha256": proposal_anchor,
            "proposal_id": proposal["proposal_id"],
            "proposal_hash": proposal["proposal_hash"],
        },
        "key_registry_binding": {
            "registry_sha256": registry_anchor,
            "registry_hash": registry["registry_hash"],
        },
        "source_binding": source_binding,
        "authority_contract": authority_contract,
        "requested_effect": "OBJECTIVE_AUTHORITY_ATTESTATION_REQUESTED",
        "holdout_status": "LOCKED_NOT_ASSIGNED",
        "snapshot_effect": "NONE",
        "replay_eligible": False,
        "golden_eligible": False,
        "gate1_effect": "NONE",
        "not_dataset_receipt": True,
        "not_replay_receipt": True,
        "not_gate_receipt": True,
        "request_hash": "",
    }
    request["request_hash"] = canonical_hash({
        key: value for key, value in request.items() if key != "request_hash"
    })
    return _validate_request(request), proposal, registry


def write_objective_authority_request_artifact(
    output_dir: str | Path,
    **source_args: Any,
) -> dict[str, Any]:
    request, _, _ = build_objective_authority_request(**source_args)
    payloads = {"authority-request.json": _json_bytes(request)}
    manifest = _request_manifest(request, payloads)
    all_payloads = dict(payloads)
    all_payloads["manifest.json"] = _json_bytes(manifest)
    _write_artifact_directory(Path(output_dir), all_payloads, expected_files=REQUEST_EXACT_FILES)
    raw = _read_artifact_directory(Path(output_dir), expected_files=REQUEST_EXACT_FILES)
    loaded = load_validated_objective_authority_request_directory(
        output_dir,
        expected_request_manifest_sha256=hashlib.sha256(raw["manifest.json"]).hexdigest(),
        **source_args,
    )
    if loaded["manifest"] != manifest:
        _fail("G101B_REQUEST_WRITE_READBACK_MISMATCH")
    return manifest


def load_validated_objective_authority_request_directory(
    input_dir: str | Path,
    *,
    expected_request_manifest_sha256: str,
    **source_args: Any,
) -> dict[str, Any]:
    anchor = _sha(expected_request_manifest_sha256, "G101B_REQUEST_MANIFEST_ANCHOR_INVALID")
    raw = _read_artifact_directory(Path(input_dir), expected_files=REQUEST_EXACT_FILES)
    if hashlib.sha256(raw["manifest.json"]).hexdigest() != anchor:
        _fail("G101B_REQUEST_MANIFEST_ANCHOR_MISMATCH")
    values = {
        name: _parse_canonical_json(data, f"G101B_REQUEST_ARTIFACT_{name.upper()}")
        for name, data in raw.items()
    }
    request, proposal, registry = build_objective_authority_request(**source_args)
    expected_manifest = _request_manifest(
        request,
        {"authority-request.json": _json_bytes(request)},
    )
    if values != {
        "authority-request.json": request,
        "manifest.json": expected_manifest,
    }:
        _fail("G101B_REQUEST_SOURCE_SEMANTICS_MISMATCH")
    return {
        "manifest": expected_manifest,
        "request": request,
        "proposal": proposal,
        "registry": registry,
        "raw_manifest_sha256": anchor,
    }


def _evaluate_objective_authority(
    request: Mapping[str, Any],
    proposal: Mapping[str, Any],
    registry: Mapping[str, Any],
    *,
    request_manifest_sha256: str,
    response_file: str | Path,
    expected_response_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    request_value = _validate_request(request)
    proposal_value = _validate_proposal(proposal)
    if (
        request_value["proposal_binding"]["proposal_id"] != proposal_value["proposal_id"]
        or request_value["proposal_binding"]["proposal_hash"] != proposal_value["proposal_hash"]
        or request_value["source_binding"]["primary_metric_contract_hash"]
        != proposal_value["primary_metric_contract"]["contract_hash"]
    ):
        _fail("G101B_PROPOSAL_BINDING_MISMATCH")
    request_manifest_anchor = _sha(
        request_manifest_sha256, "G101B_REQUEST_MANIFEST_ANCHOR_INVALID"
    )
    response_anchor = _sha(expected_response_sha256, "G101B_RESPONSE_ANCHOR_INVALID")
    response = _read_external_json(Path(response_file), response_anchor, "G101B_RESPONSE")
    response_value, objective = _validate_response(
        response,
        request_value,
        proposal_value,
        registry,
        registry_raw_sha256=request_value["key_registry_binding"]["registry_sha256"],
        request_manifest_sha256=request_manifest_anchor,
    )
    return response_value, objective


def build_objective_authority_artifact(
    *,
    request_dir: str | Path,
    expected_request_manifest_sha256: str,
    proposal_file: str | Path,
    expected_proposal_sha256: str,
    request_id: str,
    requested_at: str,
    evaluated_at: str,
    trusted_key_registry_file: str | Path,
    expected_key_registry_sha256: str,
    expected_key_registry_hash: str,
    response_file: str | Path,
    expected_response_sha256: str,
) -> dict[str, Any]:
    request_source = load_validated_objective_authority_request_directory(
        request_dir,
        expected_request_manifest_sha256=expected_request_manifest_sha256,
        proposal_file=proposal_file,
        expected_proposal_sha256=expected_proposal_sha256,
        request_id=request_id,
        requested_at=requested_at,
        evaluated_at=evaluated_at,
        trusted_key_registry_file=trusted_key_registry_file,
        expected_key_registry_sha256=expected_key_registry_sha256,
        expected_key_registry_hash=expected_key_registry_hash,
    )
    request = request_source["request"]
    proposal = request_source["proposal"]
    registry = request_source["registry"]
    response, objective = _evaluate_objective_authority(
        request,
        proposal,
        registry,
        request_manifest_sha256=request_source["raw_manifest_sha256"],
        response_file=response_file,
        expected_response_sha256=expected_response_sha256,
    )
    payloads = {
        "authority-request.json": _json_bytes(request),
        "authority-response.json": _json_bytes(response),
        "objective-contract.json": _json_bytes(objective),
    }
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_VERSION,
        "contract_version": CONTRACT_VERSION,
        "status": "OBJECTIVE_AUTHORITY_ATTESTATION_VERIFIED",
        "reason_codes": ["EXTERNAL_REGISTRY_GOVERNANCE_NOT_CONTENT_VERIFIED"],
        "trust_status": "SIGNATURES_VALID_UNDER_EXTERNALLY_PINNED_REGISTRY",
        "authority_effect": "NONE",
        "attestation_effect": "OBJECTIVE_AUTHORITY_ATTESTATION_VERIFIED",
        "objective_effect": "SIGNED_CANDIDATE_NOT_GOVERNANCE_PROMOTED",
        "request_hash": request["request_hash"],
        "request_manifest_sha256": request_source["raw_manifest_sha256"],
        "proposal_sha256": request["proposal_binding"]["proposal_sha256"],
        "proposal_hash": request["proposal_binding"]["proposal_hash"],
        "response_sha256": expected_response_sha256,
        "key_registry_sha256": expected_key_registry_sha256,
        "key_registry_hash": registry["registry_hash"],
        "objective_contract_hash": objective["contract_hash"],
        "source_binding": request["source_binding"],
        "snapshot_effect": "NONE",
        "snapshot_emitted": False,
        "partition_effect": "NONE",
        "holdout_status": "LOCKED_NOT_ASSIGNED",
        "replay_eligible": False,
        "golden_eligible": False,
        "gate1_effect": "NONE",
        "not_dataset_receipt": True,
        "not_replay_receipt": True,
        "not_gate_receipt": True,
        "files": _descriptors(payloads),
        "manifest_hash": "",
    }
    manifest["manifest_hash"] = canonical_hash({
        key: value for key, value in manifest.items() if key != "manifest_hash"
    })
    return _validate_manifest(manifest, request, response, objective, payloads)


def write_objective_authority_artifact(output_dir: str | Path, **source_args: Any) -> dict[str, Any]:
    manifest = build_objective_authority_artifact(**source_args)
    request_source = load_validated_objective_authority_request_directory(
        source_args["request_dir"],
        expected_request_manifest_sha256=source_args["expected_request_manifest_sha256"],
        proposal_file=source_args["proposal_file"],
        expected_proposal_sha256=source_args["expected_proposal_sha256"],
        request_id=source_args["request_id"],
        requested_at=source_args["requested_at"],
        evaluated_at=source_args["evaluated_at"],
        trusted_key_registry_file=source_args["trusted_key_registry_file"],
        expected_key_registry_sha256=source_args["expected_key_registry_sha256"],
        expected_key_registry_hash=source_args["expected_key_registry_hash"],
    )
    request = request_source["request"]
    proposal = request_source["proposal"]
    registry = request_source["registry"]
    response, objective = _evaluate_objective_authority(
        request,
        proposal,
        registry,
        request_manifest_sha256=request_source["raw_manifest_sha256"],
        response_file=source_args["response_file"],
        expected_response_sha256=source_args["expected_response_sha256"],
    )
    payloads = {
        "authority-request.json": _json_bytes(request),
        "authority-response.json": _json_bytes(response),
        "objective-contract.json": _json_bytes(objective),
        "manifest.json": _json_bytes(manifest),
    }
    _write_artifact_directory(Path(output_dir), payloads)
    raw = _read_artifact_directory(Path(output_dir))
    loaded = load_validated_objective_authority_directory(
        output_dir,
        expected_manifest_sha256=hashlib.sha256(raw["manifest.json"]).hexdigest(),
        **source_args,
    )
    if loaded["manifest"] != manifest:
        _fail("G101B_WRITE_READBACK_MISMATCH")
    return manifest


def load_validated_objective_authority_directory(
    input_dir: str | Path,
    *,
    expected_manifest_sha256: str,
    **source_args: Any,
) -> dict[str, Any]:
    expected_anchor = _sha(expected_manifest_sha256, "G101B_MANIFEST_ANCHOR_INVALID")
    raw = _read_artifact_directory(Path(input_dir))
    if hashlib.sha256(raw["manifest.json"]).hexdigest() != expected_anchor:
        _fail("G101B_MANIFEST_ANCHOR_MISMATCH")
    values = {
        name: _parse_canonical_json(data, f"G101B_ARTIFACT_{name.upper()}")
        for name, data in raw.items()
    }
    expected_manifest = build_objective_authority_artifact(**source_args)
    request_source = load_validated_objective_authority_request_directory(
        source_args["request_dir"],
        expected_request_manifest_sha256=source_args["expected_request_manifest_sha256"],
        proposal_file=source_args["proposal_file"],
        expected_proposal_sha256=source_args["expected_proposal_sha256"],
        request_id=source_args["request_id"],
        requested_at=source_args["requested_at"],
        evaluated_at=source_args["evaluated_at"],
        trusted_key_registry_file=source_args["trusted_key_registry_file"],
        expected_key_registry_sha256=source_args["expected_key_registry_sha256"],
        expected_key_registry_hash=source_args["expected_key_registry_hash"],
    )
    request = request_source["request"]
    proposal = request_source["proposal"]
    registry = request_source["registry"]
    response, objective = _evaluate_objective_authority(
        request,
        proposal,
        registry,
        request_manifest_sha256=request_source["raw_manifest_sha256"],
        response_file=source_args["response_file"],
        expected_response_sha256=source_args["expected_response_sha256"],
    )
    expected_values = {
        "authority-request.json": request,
        "authority-response.json": response,
        "objective-contract.json": objective,
        "manifest.json": expected_manifest,
    }
    if values != expected_values:
        _fail("G101B_SOURCE_SEMANTICS_MISMATCH")
    return {
        "manifest": expected_manifest,
        "request": request,
        "response": response,
        "objective_contract": objective,
    }


def signature_message(
    payload_hash: str,
    *,
    request_manifest_sha256: str,
    key_registry_raw_sha256: str,
    key_registry_hash: str,
    key_id: str,
    signer_id: str,
    principal_id: str,
    role: str,
) -> bytes:
    _sha(payload_hash, "G101B_SIGNATURE_INVALID")
    _sha(request_manifest_sha256, "G101B_SIGNATURE_INVALID")
    _sha(key_registry_raw_sha256, "G101B_SIGNATURE_INVALID")
    _sha(key_registry_hash, "G101B_SIGNATURE_INVALID")
    for value in (key_id, signer_id, principal_id):
        _identifier(value, "G101B_SIGNATURE_INVALID")
    if role not in REQUIRED_ROLES:
        _fail("G101B_SIGNATURE_INVALID")
    return (
        "GLE_OBJECTIVE_AUTHORITY_V1\n"
        + canonical_json({
            "request_manifest_sha256": request_manifest_sha256,
            "key_registry_raw_sha256": key_registry_raw_sha256,
            "key_registry_hash": key_registry_hash,
            "key_id": key_id,
            "signer_id": signer_id,
            "principal_id": principal_id,
            "role": role,
            "purpose": ROLE_PURPOSES[role],
            "object_hash": payload_hash,
        })
        + "\n"
    ).encode("utf-8")


def _validate_proposal(value: Any) -> dict[str, Any]:
    keys = {
        "schema_version", "proposal_id", "created_at", "objective_contract_id", "version",
        "ad_account_id", "market", "currency", "business_goal", "primary_metric",
        "secondary_metrics", "guardrails", "risk_boundary", "primary_metric_contract",
        "created_by", "proposal_hash",
    }
    proposal = _exact(value, keys, "G101B_PROPOSAL_INVALID")
    if proposal["schema_version"] != PROPOSAL_VERSION:
        _fail("G101B_PROPOSAL_INVALID")
    for field in ("proposal_id", "objective_contract_id", "ad_account_id", "market", "created_by"):
        _identifier(proposal[field], "G101B_PROPOSAL_INVALID")
    _utc(proposal["created_at"], "G101B_PROPOSAL_INVALID")
    if type(proposal["version"]) is not int or proposal["version"] <= 0:
        _fail("G101B_PROPOSAL_INVALID")
    if proposal["currency"] != "USD" or proposal["business_goal"] != "REDUCE_QUALIFIED_JOIN_CPA":
        _fail("G101B_PROPOSAL_INVALID")
    primary = _exact(proposal["primary_metric"], {
        "metric_key", "definition_version", "attribution_window", "dedup_version",
        "qualification_rule_version", "min_business_improvement", "direction",
    }, "G101B_PROPOSAL_INVALID")
    if (
        primary["metric_key"] != "QUALIFIED_JOIN_CPA"
        or primary["definition_version"] != PRIMARY_METRIC_DEFINITION_VERSION
        or primary["dedup_version"] != DEDUPE_VERSION
        or primary["qualification_rule_version"] != QUALIFICATION_VERSION
        or primary["direction"] != "LOWER_IS_BETTER"
    ):
        _fail("G101B_PROPOSAL_SOURCE_VERSION_INVALID")
    metric_contract = _exact(proposal["primary_metric_contract"], {
        "schema_version", "formula", "numerator", "denominator", "attribution",
        "dedup", "settlement", "zero_event_rule", "contract_hash",
    }, "G101B_METRIC_CONTRACT_INVALID")
    expected_metric_contract = {
        "schema_version": METRIC_CONTRACT_VERSION,
        "formula": "SPEND_USD / QUALIFIED_JOIN_SUCCESS_USERS",
        "numerator": {
            "metric_key": "SPEND_USD",
            "source_contract": "META_AD_FACTS_CELL_CUTOFF_V1",
            "currency": "USD",
            "grain": "CELL_CUTOFF_CUMULATIVE",
        },
        "denominator": {
            "metric_key": SOURCE_METRIC,
            "source_contract": SOURCE_CONTRACT,
            "qualification_rule_version": QUALIFICATION_VERSION,
            "grain": "CELL_CUTOFF_CUMULATIVE",
        },
        "attribution": {
            "version": ATTRIBUTION_VERSION,
            "window": primary["attribution_window"],
        },
        "dedup": {
            "version": DEDUPE_VERSION,
            "unit": "CANONICAL_QUALIFIED_IDENTITY",
        },
        "settlement": {
            "status_required": "SETTLED_COMPLETE",
            "late_data_policy": "REBUILD_NEW_SNAPSHOT",
        },
        "zero_event_rule": "UNDEFINED_CPA_DATA_INCOMPLETE_NOT_INFINITY_OR_ZERO",
        "contract_hash": metric_contract["contract_hash"],
    }
    expected_metric_contract["contract_hash"] = canonical_hash({
        key: item for key, item in expected_metric_contract.items() if key != "contract_hash"
    })
    if metric_contract != expected_metric_contract:
        _fail("G101B_METRIC_CONTRACT_INVALID")
    # Reuse the canonical validator with the request time replaced later by the real authorization.
    provisional = _objective_from_proposal(proposal, authority_id="proposal-validation", authorized_at=proposal["created_at"])
    try:
        validate_objective_contract(provisional)
    except ValueError as exc:
        raise ObjectiveContractAuthorityError("G101B_PROPOSAL_OBJECTIVE_INVALID") from exc
    if proposal["proposal_hash"] != canonical_hash({
        key: item for key, item in proposal.items() if key != "proposal_hash"
    }):
        _fail("G101B_PROPOSAL_HASH_INVALID")
    return proposal


def _validate_request(value: Any) -> dict[str, Any]:
    keys = {
        "schema_version", "contract_version", "request_id", "requested_at", "evaluated_at",
        "proposal_binding", "key_registry_binding", "source_binding", "authority_contract", "requested_effect",
        "holdout_status", "snapshot_effect", "replay_eligible", "golden_eligible",
        "gate1_effect", "not_dataset_receipt", "not_replay_receipt", "not_gate_receipt",
        "request_hash",
    }
    request = _exact(value, keys, "G101B_REQUEST_INVALID")
    if request["schema_version"] != REQUEST_VERSION or request["contract_version"] != CONTRACT_VERSION:
        _fail("G101B_REQUEST_INVALID")
    if request["request_hash"] != canonical_hash({
        key: item for key, item in request.items() if key != "request_hash"
    }):
        _fail("G101B_REQUEST_HASH_INVALID")
    binding = _exact(
        request["key_registry_binding"],
        {"registry_sha256", "registry_hash"},
        "G101B_REQUEST_REGISTRY_BINDING_INVALID",
    )
    _sha(binding["registry_sha256"], "G101B_REQUEST_REGISTRY_BINDING_INVALID")
    _sha(binding["registry_hash"], "G101B_REQUEST_REGISTRY_BINDING_INVALID")
    expected_source = {
        "primary_metric_definition_version": PRIMARY_METRIC_DEFINITION_VERSION,
        "primary_metric_contract_version": METRIC_CONTRACT_VERSION,
        "primary_metric_contract_hash": request["source_binding"].get("primary_metric_contract_hash"),
        "qualification_rule_version": QUALIFICATION_VERSION,
        "qualified_source_contract": SOURCE_CONTRACT,
        "qualified_source_metric": SOURCE_METRIC,
        "attribution_version": ATTRIBUTION_VERSION,
        "dedup_version": DEDUPE_VERSION,
    }
    _sha(expected_source["primary_metric_contract_hash"], "G101B_REQUEST_SOURCE_BINDING_INVALID")
    expected_contract = {
        "algorithm": SIGNATURE_ALGORITHM,
        "minimum_rsa_bits": MINIMUM_RSA_BITS,
        "roles": list(REQUIRED_ROLES),
        "role_purposes": dict(ROLE_PURPOSES),
        "quorum": 3,
    }
    ceilings = (
        request["requested_effect"] == "OBJECTIVE_AUTHORITY_ATTESTATION_REQUESTED"
        and request["holdout_status"] == "LOCKED_NOT_ASSIGNED"
        and request["snapshot_effect"] == "NONE"
        and request["replay_eligible"] is False
        and request["golden_eligible"] is False
        and request["gate1_effect"] == "NONE"
        and request["not_dataset_receipt"] is True
        and request["not_replay_receipt"] is True
        and request["not_gate_receipt"] is True
    )
    if request["source_binding"] != expected_source or request["authority_contract"] != expected_contract or not ceilings:
        _fail("G101B_REQUEST_CEILING_INVALID")
    return request


def _validate_response(
    value: Any,
    request: Mapping[str, Any],
    proposal: Mapping[str, Any],
    registry: Mapping[str, Any],
    *,
    registry_raw_sha256: str,
    request_manifest_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    keys = {
        "schema_version", "authority_id", "request_hash", "request_manifest_sha256", "authorized_at",
        "objective_contract", "authority_payload_hash", "signatures",
    }
    response = _exact(value, keys, "G101B_RESPONSE_INVALID")
    if response["schema_version"] != RESPONSE_VERSION:
        _fail("G101B_RESPONSE_INVALID")
    _identifier(response["authority_id"], "G101B_RESPONSE_INVALID")
    authorized = _utc(response["authorized_at"], "G101B_RESPONSE_INVALID")
    if (
        response["request_hash"] != request["request_hash"]
        or response["request_manifest_sha256"] != request_manifest_sha256
        or not (
        _instant(request["requested_at"]) <= _instant(authorized) <= _instant(request["evaluated_at"])
        )
    ):
        _fail("G101B_RESPONSE_BINDING_INVALID")
    expected_objective = _objective_from_proposal(proposal, response["authority_id"], authorized)
    try:
        objective = validate_objective_contract(response["objective_contract"])
    except ValueError as exc:
        raise ObjectiveContractAuthorityError("G101B_OBJECTIVE_INVALID") from exc
    if objective != expected_objective:
        _fail("G101B_OBJECTIVE_PROPOSAL_MISMATCH")
    expected_payload_hash = canonical_hash({
        key: item for key, item in response.items()
        if key not in {"authority_payload_hash", "signatures"}
    })
    if response["authority_payload_hash"] != expected_payload_hash:
        _fail("G101B_RESPONSE_HASH_INVALID")
    _validate_signatures(response, registry, registry_raw_sha256=registry_raw_sha256)
    return response, objective


def _objective_from_proposal(
    proposal: Mapping[str, Any], authority_id: str, authorized_at: str,
) -> dict[str, Any]:
    objective = {
        "schema_version": OBJECTIVE_VERSION,
        "objective_contract_id": proposal["objective_contract_id"],
        "version": proposal["version"],
        "contract_hash": "0" * 64,
        "ad_account_id": proposal["ad_account_id"],
        "market": proposal["market"],
        "currency": proposal["currency"],
        "business_goal": proposal["business_goal"],
        "primary_metric": proposal["primary_metric"],
        "secondary_metrics": proposal["secondary_metrics"],
        "guardrails": proposal["guardrails"],
        "risk_boundary": proposal["risk_boundary"],
        "created_by": proposal["created_by"],
        "approved_by": f"objective-authority:{authority_id}",
        "approved_at": authorized_at,
    }
    objective["contract_hash"] = content_hash(objective, "contract_hash")
    return objective


def _validate_key_registry(value: Any, *, expected_registry_hash: str) -> dict[str, Any]:
    registry = _exact(value, {"schema_version", "registry_id", "keys", "registry_hash"}, "G101B_REGISTRY_INVALID")
    if registry["schema_version"] != REGISTRY_VERSION:
        _fail("G101B_REGISTRY_INVALID")
    _identifier(registry["registry_id"], "G101B_REGISTRY_INVALID")
    if registry["registry_hash"] != expected_registry_hash or registry["registry_hash"] != canonical_hash({
        key: item for key, item in registry.items() if key != "registry_hash"
    }):
        _fail("G101B_REGISTRY_HASH_MISMATCH")
    key_fields = {
        "key_id", "signer_id", "principal_id", "role", "purposes", "algorithm",
        "status", "valid_from", "valid_until", "public_key_pem",
    }
    if not isinstance(registry["keys"], list) or len(registry["keys"]) != 3:
        _fail("G101B_REGISTRY_INVALID")
    if registry["keys"] != sorted(registry["keys"], key=lambda item: item.get("key_id", "")):
        _fail("G101B_REGISTRY_INVALID")
    key_ids: set[str] = set()
    signers: set[str] = set()
    principals: set[str] = set()
    fingerprints: set[str] = set()
    roles: set[str] = set()
    for item in registry["keys"]:
        key = _exact(item, key_fields, "G101B_REGISTRY_INVALID")
        for field in ("key_id", "signer_id", "principal_id"):
            _identifier(key[field], "G101B_REGISTRY_INVALID")
        if (
            key["role"] not in REQUIRED_ROLES
            or key["purposes"] != [ROLE_PURPOSES[key["role"]]]
            or key["algorithm"] != SIGNATURE_ALGORITHM
            or key["status"] != "ACTIVE"
            or not isinstance(key["public_key_pem"], str)
            or not _PEM_RE.fullmatch(key["public_key_pem"])
        ):
            _fail("G101B_REGISTRY_INVALID")
        bits, fingerprint = _rsa_public_key_metadata(key["public_key_pem"])
        if bits < MINIMUM_RSA_BITS:
            _fail("G101B_REGISTRY_WEAK_KEY")
        _utc(key["valid_from"], "G101B_REGISTRY_INVALID")
        _utc(key["valid_until"], "G101B_REGISTRY_INVALID")
        if _instant(key["valid_until"]) <= _instant(key["valid_from"]):
            _fail("G101B_REGISTRY_INVALID")
        if (
            key["key_id"] in key_ids or key["signer_id"] in signers
            or key["principal_id"] in principals or fingerprint in fingerprints
            or key["role"] in roles
        ):
            _fail("G101B_REGISTRY_IDENTITY_NOT_DISTINCT")
        key_ids.add(key["key_id"])
        signers.add(key["signer_id"])
        principals.add(key["principal_id"])
        fingerprints.add(fingerprint)
        roles.add(key["role"])
    if roles != set(REQUIRED_ROLES):
        _fail("G101B_REGISTRY_INVALID")
    return registry


def _validate_signatures(
    response: Mapping[str, Any], registry: Mapping[str, Any], *, registry_raw_sha256: str,
) -> None:
    signatures = response["signatures"]
    fields = {
        "algorithm", "key_id", "signer_id", "principal_id", "role", "purpose",
        "object_hash", "request_manifest_sha256", "key_registry_raw_sha256", "key_registry_hash", "signed_at",
        "signature_base64",
    }
    if not isinstance(signatures, list) or len(signatures) != 3:
        _fail("G101B_SIGNATURE_SET_INVALID")
    key_by_id = {item["key_id"]: item for item in registry["keys"]}
    seen_roles: list[str] = []
    for signature in signatures:
        item = _exact(signature, fields, "G101B_SIGNATURE_INVALID")
        key = key_by_id.get(item["key_id"])
        if key is None:
            _fail("G101B_SIGNATURE_INVALID")
        signed_at = _utc(item["signed_at"], "G101B_SIGNATURE_INVALID")
        if (
            item["algorithm"] != SIGNATURE_ALGORITHM
            or item["signer_id"] != key["signer_id"]
            or item["principal_id"] != key["principal_id"]
            or item["role"] != key["role"]
            or item["purpose"] != ROLE_PURPOSES[key["role"]]
            or item["object_hash"] != response["authority_payload_hash"]
            or item["request_manifest_sha256"] != response["request_manifest_sha256"]
            or item["key_registry_raw_sha256"] != registry_raw_sha256
            or item["key_registry_hash"] != registry["registry_hash"]
            or signed_at != response["authorized_at"]
            or not (_instant(key["valid_from"]) <= _instant(signed_at) <= _instant(key["valid_until"]))
        ):
            _fail("G101B_SIGNATURE_INVALID")
        try:
            signature_bytes = base64.b64decode(item["signature_base64"], validate=True)
        except (TypeError, ValueError) as exc:
            raise ObjectiveContractAuthorityError("G101B_SIGNATURE_INVALID") from exc
        if not signature_bytes or not _verify_rsa_sha256(
            key["public_key_pem"],
            signature_bytes,
            signature_message(
                response["authority_payload_hash"],
                request_manifest_sha256=response["request_manifest_sha256"],
                key_registry_raw_sha256=registry_raw_sha256,
                key_registry_hash=registry["registry_hash"],
                key_id=item["key_id"],
                signer_id=item["signer_id"],
                principal_id=item["principal_id"],
                role=item["role"],
            ),
        ):
            _fail("G101B_SIGNATURE_INVALID")
        seen_roles.append(item["role"])
    if seen_roles != list(REQUIRED_ROLES):
        _fail("G101B_SIGNATURE_SET_INVALID")


def _validate_manifest(
    value: Any,
    request: Mapping[str, Any],
    response: Mapping[str, Any],
    objective: Mapping[str, Any],
    payloads: Mapping[str, bytes],
) -> dict[str, Any]:
    keys = {
        "schema_version", "contract_version", "status", "reason_codes", "trust_status",
        "authority_effect", "attestation_effect", "objective_effect", "request_hash",
        "request_manifest_sha256", "proposal_sha256", "proposal_hash",
        "response_sha256", "key_registry_sha256", "key_registry_hash",
        "objective_contract_hash", "source_binding", "snapshot_effect", "snapshot_emitted",
        "partition_effect", "holdout_status", "replay_eligible", "golden_eligible",
        "gate1_effect", "not_dataset_receipt", "not_replay_receipt", "not_gate_receipt",
        "files", "manifest_hash",
    }
    manifest = _exact(value, keys, "G101B_MANIFEST_INVALID")
    if (
        manifest["schema_version"] != MANIFEST_VERSION
        or manifest["contract_version"] != CONTRACT_VERSION
        or manifest["request_hash"] != request["request_hash"]
        or manifest["request_manifest_sha256"] != response["request_manifest_sha256"]
        or manifest["proposal_sha256"] != request["proposal_binding"]["proposal_sha256"]
        or manifest["proposal_hash"] != request["proposal_binding"]["proposal_hash"]
        or manifest["source_binding"] != request["source_binding"]
        or manifest["files"] != _descriptors(payloads)
        or manifest["manifest_hash"] != canonical_hash({
            key: item for key, item in manifest.items() if key != "manifest_hash"
        })
    ):
        _fail("G101B_MANIFEST_BINDING_INVALID")
    expected = {
        "status": "OBJECTIVE_AUTHORITY_ATTESTATION_VERIFIED",
        "reason_codes": ["EXTERNAL_REGISTRY_GOVERNANCE_NOT_CONTENT_VERIFIED"],
        "trust_status": "SIGNATURES_VALID_UNDER_EXTERNALLY_PINNED_REGISTRY",
        "authority_effect": "NONE",
        "attestation_effect": "OBJECTIVE_AUTHORITY_ATTESTATION_VERIFIED",
        "objective_effect": "SIGNED_CANDIDATE_NOT_GOVERNANCE_PROMOTED",
        "objective_contract_hash": objective["contract_hash"],
        "snapshot_effect": "NONE", "snapshot_emitted": False, "partition_effect": "NONE",
        "holdout_status": "LOCKED_NOT_ASSIGNED", "replay_eligible": False,
        "golden_eligible": False, "gate1_effect": "NONE", "not_dataset_receipt": True,
        "not_replay_receipt": True, "not_gate_receipt": True,
    }
    if any(manifest[key] != item for key, item in expected.items()):
        _fail("G101B_MANIFEST_CEILING_INVALID")
    return manifest


def _request_manifest(request: Mapping[str, Any], payloads: Mapping[str, bytes]) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": REQUEST_MANIFEST_VERSION,
        "contract_version": CONTRACT_VERSION,
        "status": "OBJECTIVE_AUTHORITY_REQUEST_FROZEN",
        "request_hash": request["request_hash"],
        "proposal_binding": request["proposal_binding"],
        "key_registry_binding": request["key_registry_binding"],
        "source_binding": request["source_binding"],
        "authority_effect": "NONE",
        "snapshot_effect": "NONE",
        "snapshot_emitted": False,
        "partition_effect": "NONE",
        "holdout_status": "LOCKED_NOT_ASSIGNED",
        "replay_eligible": False,
        "golden_eligible": False,
        "gate1_effect": "NONE",
        "not_dataset_receipt": True,
        "not_replay_receipt": True,
        "not_gate_receipt": True,
        "files": _descriptors(payloads),
        "manifest_hash": "",
    }
    manifest["manifest_hash"] = canonical_hash({
        key: item for key, item in manifest.items() if key != "manifest_hash"
    })
    return manifest


def _read_external_json(path: Path, expected_sha256: str, prefix: str) -> Any:
    data = _read_regular_file(path, prefix)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        _fail(f"{prefix}_ANCHOR_MISMATCH")
    return _parse_canonical_json(data, prefix)


def _read_regular_file(path: Path, prefix: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        parent = path.parent.resolve(strict=True)
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ObjectiveContractAuthorityError(f"{prefix}_UNREADABLE") from exc
    try:
        parent_before = os.fstat(parent_fd)
        fd = os.open(path.name, flags, dir_fd=parent_fd)
    except OSError as exc:
        os.close(parent_fd)
        raise ObjectiveContractAuthorityError(f"{prefix}_UNREADABLE") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > MAX_FILE_BYTES
        ):
            _fail(f"{prefix}_UNSAFE")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                _fail(f"{prefix}_OVERSIZED")
        after = os.fstat(fd)
        identity = lambda item: (
            item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode), item.st_nlink,
            item.st_size, item.st_mtime_ns, item.st_ctime_ns,
        )
        if identity(before) != identity(after):
            _fail(f"{prefix}_CHANGED_DURING_READ")
        named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        parent_after = os.fstat(parent_fd)
        if identity(after) != identity(named) or identity(parent_before) != identity(parent_after):
            _fail(f"{prefix}_CHANGED_DURING_READ")
        data = b"".join(chunks)
        if len(data) != after.st_size:
            _fail(f"{prefix}_CHANGED_DURING_READ")
        return data
    finally:
        os.close(fd)
        os.close(parent_fd)


def _write_artifact_directory(
    root: Path,
    payloads: Mapping[str, bytes],
    *,
    expected_files: frozenset[str] = EXACT_FILES,
) -> None:
    if set(payloads) != expected_files or sum(map(len, payloads.values())) > MAX_TOTAL_BYTES:
        _fail("G101B_ARTIFACT_PAYLOAD_INVALID")
    parent = root.parent.resolve(strict=True)
    if not parent.is_dir() or root.parent != parent or root.exists() or root.is_symlink():
        _fail("G101B_OUTPUT_INVALID")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    root_fd: int | None = None
    completed = False
    try:
        try:
            os.mkdir(root.name, 0o700, dir_fd=parent_fd)
        except OSError as exc:
            raise ObjectiveContractAuthorityError("G101B_OUTPUT_EXISTS") from exc
        root_fd = os.open(root.name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        root_stat = os.fstat(root_fd)
        for name in sorted(payloads):
            data = payloads[name]
            if len(data) > MAX_FILE_BYTES:
                _fail("G101B_ARTIFACT_FILE_OVERSIZED")
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=root_fd,
            )
            try:
                written = 0
                while written < len(data):
                    count = os.write(fd, data[written:])
                    if count <= 0:
                        _fail("G101B_ARTIFACT_WRITE_FAILED")
                    written += count
                os.fsync(fd)
            finally:
                os.close(fd)
        if set(os.listdir(root_fd)) != expected_files:
            _fail("G101B_ARTIFACT_FILE_SET_INVALID")
        os.fsync(root_fd)
        named = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (root_stat.st_dev, root_stat.st_ino):
            _fail("G101B_OUTPUT_IDENTITY_CHANGED")
        completed = True
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise ObjectiveContractAuthorityError("G101B_OUTPUT_DURABILITY_UNCERTAIN") from exc
    except Exception:
        if not completed and root_fd is not None:
            for name in expected_files:
                try:
                    os.unlink(name, dir_fd=root_fd)
                except OSError:
                    pass
            try:
                named = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
                opened = os.fstat(root_fd)
                if (named.st_dev, named.st_ino) == (opened.st_dev, opened.st_ino):
                    os.rmdir(root.name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)


def _read_artifact_directory(
    root: Path,
    *,
    expected_files: frozenset[str] = EXACT_FILES,
) -> dict[str, bytes]:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    parent_fd: int | None = None
    try:
        parent = root.parent.resolve(strict=True)
        parent_fd = os.open(parent, flags)
        parent_before = os.fstat(parent_fd)
        root_fd = os.open(root.name, flags, dir_fd=parent_fd)
    except OSError as exc:
        if parent_fd is not None:
            os.close(parent_fd)
        raise ObjectiveContractAuthorityError("G101B_ARTIFACT_UNREADABLE") from exc
    try:
        before = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o700
            or before.st_nlink < 2
        ):
            _fail("G101B_ARTIFACT_DIRECTORY_INVALID")
        if set(os.listdir(root_fd)) != expected_files:
            _fail("G101B_ARTIFACT_FILE_SET_INVALID")
        result: dict[str, bytes] = {}
        total = 0
        for name in sorted(expected_files):
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=root_fd)
            try:
                first = os.fstat(fd)
                if not stat.S_ISREG(first.st_mode) or first.st_nlink != 1 or stat.S_IMODE(first.st_mode) != 0o600:
                    _fail("G101B_ARTIFACT_FILE_INVALID")
                data = b""
                while True:
                    chunk = os.read(fd, min(1024 * 1024, MAX_FILE_BYTES + 1 - len(data)))
                    if not chunk:
                        break
                    data += chunk
                    if len(data) > MAX_FILE_BYTES:
                        _fail("G101B_ARTIFACT_FILE_OVERSIZED")
                second = os.fstat(fd)
                named = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                identity = lambda item: (
                    item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode), item.st_nlink,
                    item.st_size, item.st_mtime_ns, item.st_ctime_ns,
                )
                if (
                    identity(first) != identity(second)
                    or identity(second) != identity(named)
                    or len(data) != second.st_size
                ):
                    _fail("G101B_ARTIFACT_CHANGED_DURING_READ")
                result[name] = data
                total += len(data)
            finally:
                os.close(fd)
        after = os.fstat(root_fd)
        directory_identity = lambda item: (
            item.st_dev, item.st_ino, stat.S_IMODE(item.st_mode), item.st_nlink,
            item.st_size, item.st_mtime_ns, item.st_ctime_ns,
        )
        named = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        parent_after = os.fstat(parent_fd)
        if (
            set(os.listdir(root_fd)) != expected_files
            or directory_identity(before) != directory_identity(after)
            or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)
            or directory_identity(parent_before) != directory_identity(parent_after)
        ):
            _fail("G101B_ARTIFACT_CHANGED_DURING_READ")
        if total > MAX_TOTAL_BYTES:
            _fail("G101B_ARTIFACT_OVERSIZED")
        return result
    finally:
        os.close(root_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _descriptors(payloads: Mapping[str, bytes]) -> list[dict[str, Any]]:
    return [
        {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
        for name, data in sorted(payloads.items())
    ]


def _parse_canonical_json(data: bytes, code: str) -> Any:
    try:
        text = data.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=lambda _: _fail(code))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObjectiveContractAuthorityError(code) from exc
    if data != _json_bytes(value):
        _fail(f"{code}_NONCANONICAL")
    return value


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("G101B_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _json_bytes(value: Any) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _rsa_public_key_metadata(public_key_pem: str) -> tuple[int, str]:
    with tempfile.TemporaryDirectory(prefix="gle-objective-key-") as directory:
        key = Path(directory) / "public.pem"
        key.write_text(public_key_pem, encoding="ascii")
        try:
            text_result = subprocess.run(
                [OPENSSL_BINARY, "pkey", "-pubin", "-in", str(key), "-text", "-noout"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            der_result = subprocess.run(
                [OPENSSL_BINARY, "pkey", "-pubin", "-in", str(key), "-outform", "DER"],
                capture_output=True, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ObjectiveContractAuthorityError("G101B_SIGNATURE_VERIFIER_UNAVAILABLE") from exc
        match = re.search(r"Public-Key: \((\d+) bit\)", text_result.stdout)
        if text_result.returncode or der_result.returncode or not match:
            _fail("G101B_REGISTRY_INVALID")
        return int(match.group(1)), hashlib.sha256(der_result.stdout).hexdigest()


def _verify_rsa_sha256(public_key_pem: str, signature: bytes, message: bytes) -> bool:
    with tempfile.TemporaryDirectory(prefix="gle-objective-signature-") as directory:
        root = Path(directory)
        key = root / "public.pem"
        sig = root / "signature.bin"
        key.write_text(public_key_pem, encoding="ascii")
        sig.write_bytes(signature)
        try:
            result = subprocess.run(
                [OPENSSL_BINARY, "dgst", "-sha256", "-verify", str(key), "-signature", str(sig)],
                input=message, capture_output=True, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ObjectiveContractAuthorityError("G101B_SIGNATURE_VERIFIER_UNAVAILABLE") from exc
        return result.returncode == 0


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
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ObjectiveContractAuthorityError(code) from exc
    return value


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
