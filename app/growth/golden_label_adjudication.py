from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import stat
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.growth.canonical_evaluation_contracts import (
    EVALUATION_RESULTS,
    canonical_hash,
    canonical_json,
    validate_sha256,
    validate_utc,
)
from app.growth.historical_lineage_candidates import (
    HistoricalLineageCandidateError,
    _canonical_json_document,
    _read_regular,
    load_validated_audit_directory,
)
from app.growth.immutable_lineage_authority import (
    MINIMUM_RSA_BITS,
    SIGNATURE_ALGORITHM,
    _rsa_public_key_metadata,
    _verify_rsa_sha256,
    load_validated_candidate_directory,
)
from app.growth.lineage_devval_registry import load_validated_registry_directory


REQUEST_VERSION = "gle-g1-03a-blind-label-request-v1"
TASK_VERSION = "gle-g1-03a-blind-label-task-v1"
REVIEW_RESPONSE_VERSION = "gle-g1-03a-blind-review-response-v1"
DELIVERY_VERSION = "gle-g1-03a-blind-delivery-receipt-v1"
ADJUDICATION_VERSION = "gle-g1-03a-adjudication-v1"
LEDGER_VERSION = "gle-g1-03a-label-fragment-v1"
ROUND_VERSION = "gle-g1-03a-review-round-v1"
REVIEWER_KEY_REGISTRY_VERSION = "gle-g1-03a-reviewer-key-registry-v1"
MANIFEST_VERSION = "gle-g1-03a-artifact-manifest-v1"
REVIEW_PACKET_MANIFEST_VERSION = "gle-g1-03a-review-packet-manifest-v1"
LABEL_CONTRACT_VERSION = "gle-g1-03a-audit-label-contract-v1"
BLINDING_MAP_VERSION = "gle-g1-03a-custodian-blinding-map-v1"

REVIEW_SIGNATURE_PURPOSE = "BLIND_GOLDEN_LABEL_REVIEW"
DELIVERY_SIGNATURE_PURPOSE = "BLIND_LABEL_PACKET_DELIVERY"
BLINDING_MAP_SIGNATURE_PURPOSE = "BLIND_TASK_ID_ISSUANCE"
REVIEWER_ROLES = ("REVIEWER_A", "REVIEWER_B", "ADJUDICATOR_C")
ALL_ROLES = ("BLINDING_CUSTODIAN",) + REVIEWER_ROLES
ALLOWED_SPLITS = frozenset({"DEV", "VALIDATION"})
HOLDOUT_STATUS = "LOCKED_NOT_ASSIGNED"
MAX_ARTIFACT_FILE_BYTES = 8 * 1024 * 1024
EXACT_FILES = frozenset({
    "manifest.json",
    "assignment-request.json",
    "trusted-reviewer-key-registry.json",
    "blind-tasks.ndjson",
    "blind-responses.ndjson",
    "adjudications.ndjson",
    "label-fragments.ndjson",
    "review-round.json",
})
REVIEW_PACKET_FILES = frozenset({"manifest.json", "blind-payloads.ndjson"})

DECISIONS = frozenset({
    "STOP_FOR_SAFETY",
    "CREATE_DATA_FIX_TASK",
    "INVALIDATE_EXPERIMENT",
    "CONTINUE_WAITING",
    "CLOSE_UNDERPOWERED",
    "CLOSE_FUTILE",
    "CLOSE_NEUTRAL",
    "RETAIN_CHAMPION",
    "GRADUATE_CHALLENGER",
    "PAUSE_LOSER_AND_CREATE_NEXT",
})
ACTION_PROPOSALS = frozenset({
    "PAUSE_LOSER",
    "GENERATE_NEXT_EXPERIMENT_DRAFT",
    "CREATE_NEXT_CHALLENGER_PAUSED",
    "NONE",
})

LABEL_CONTRACT = {
    "schema_version": LABEL_CONTRACT_VERSION,
    "input_kind": "G1_02A_REDACTED_AUDIT_FACT_PACKET_NOT_CANONICAL_SNAPSHOT",
    "allowed_outcomes": {
        "DATA_INCOMPLETE": {
            "CREATE_DATA_FIX_TASK": {
                "action_proposals": ["NONE"],
                "reason_codes": ["DATA_INCOMPLETE"],
            },
        },
        "WAITING_EVIDENCE": {
            "CONTINUE_WAITING": {
                "action_proposals": ["NONE"],
                "reason_codes": ["MORE_EVIDENCE_REQUIRED"],
            },
        },
    },
    "critical_risk_labels": [
        "AUDIT_EVIDENCE_INCOMPLETE",
        "LINEAGE_EVIDENCE_UNRESOLVED",
        "MUTABLE_SOURCE_PREIMAGE_UNAVAILABLE",
    ],
    "none_action_is_exclusive": True,
}
LABEL_CONTRACT_HASH = canonical_hash(LABEL_CONTRACT)

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_OPAQUE_TASK_ID_RE = re.compile(r"blind_case_[0-9a-f]{32}")
_PEM_RE = re.compile(
    r"-----BEGIN PUBLIC KEY-----\n(?:[A-Za-z0-9+/=]+\n)+-----END PUBLIC KEY-----\n?"
)


class GoldenLabelAdjudicationError(RuntimeError):
    pass


def _fail(code: str) -> None:
    raise GoldenLabelAdjudicationError(code)


def issue_blinding_map_payload(
    *,
    review_round_id: str,
    issued_at: str,
    candidate_entry_hashes: Sequence[str],
) -> dict[str, Any]:
    """Create the only supported unsigned map payload for external custodian signing."""
    _identifier(review_round_id, "G103A_BLINDING_MAP_INVALID")
    _utc(issued_at, "G103A_BLINDING_MAP_INVALID")
    if (
        not isinstance(candidate_entry_hashes, (list, tuple))
        or not candidate_entry_hashes
        or list(candidate_entry_hashes) != sorted(set(candidate_entry_hashes))
    ):
        _fail("G103A_BLINDING_MAP_INVALID")
    assignments = []
    for entry_hash in candidate_entry_hashes:
        _hash(entry_hash, "G103A_BLINDING_MAP_INVALID")
        assignments.append({
            "candidate_entry_hash": entry_hash,
            "opaque_task_id": "blind_case_" + secrets.token_hex(16),
        })
    value = {
        "schema_version": BLINDING_MAP_VERSION,
        "review_round_id": review_round_id,
        "issued_at": issued_at,
        "issuance_method": "PYTHON_SECRETS_TOKEN_HEX_16",
        "assignments": assignments,
    }
    value["blinding_map_hash"] = canonical_hash(value)
    return value


def build_label_assignment_request(
    *,
    registry_dir: str | Path,
    expected_registry_manifest_sha256: str,
    expected_devval_key_registry_hash: str | None,
    source_validation: Mapping[str, Any],
    review_round_id: str,
    requested_at: str,
    evaluated_at: str,
    label_version: str,
    reviewer_key_registry: Mapping[str, Any] | None = None,
    expected_reviewer_key_registry_hash: str | None = None,
    expected_reviewer_key_registry_sha256: str | None = None,
    blinding_map: Mapping[str, Any] | None = None,
    expected_blinding_map_sha256: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    (
        source_request,
        source_registry,
        authority_nodes,
        candidate_entries,
        audit_records,
        source_denominator,
    ) = _load_source(
        registry_dir=registry_dir,
        expected_registry_manifest_sha256=expected_registry_manifest_sha256,
        expected_devval_key_registry_hash=expected_devval_key_registry_hash,
        source_validation=source_validation,
    )
    _identifier(review_round_id, "G103A_REVIEW_ROUND_ID_INVALID")
    if label_version != LABEL_CONTRACT_VERSION:
        _fail("G103A_LABEL_CONTRACT_INVALID")
    requested = _utc(requested_at, "G103A_TIME_INVALID")
    evaluated = _utc(evaluated_at, "G103A_TIME_INVALID")
    cutoff = source_request["authority_binding"]["data_cutoff_at"]
    if not (_instant(cutoff) <= _instant(requested) <= _instant(evaluated)):
        _fail("G103A_TIME_INVALID")

    source_signed = source_registry["status"] == "SIGNED_DETERMINISTIC_PARTITION"
    if source_signed:
        if not source_registry["assignments"]:
            _fail("G103A_SOURCE_ASSIGNMENTS_EMPTY")
        if (
            reviewer_key_registry is None
            or expected_reviewer_key_registry_hash is None
            or expected_reviewer_key_registry_sha256 is None
            or blinding_map is None
            or expected_blinding_map_sha256 is None
        ):
            _fail("G103A_REVIEWER_TRUST_ROOT_MISSING")
        reviewer_registry = _validate_reviewer_key_registry(
            reviewer_key_registry,
            expected_key_registry_hash=expected_reviewer_key_registry_hash,
            expected_registry_sha256=expected_reviewer_key_registry_sha256,
        )
        validated_blinding_map = _validate_blinding_map(
            blinding_map,
            expected_blinding_map_sha256=expected_blinding_map_sha256,
            review_round_id=review_round_id,
            data_cutoff_at=cutoff,
            requested_at=requested,
            registry=reviewer_registry,
        )
        tasks = _derive_tasks(
            source_request,
            source_registry,
            authority_nodes,
            candidate_entries,
            audit_records,
            blinding_map=validated_blinding_map,
            registry_manifest_sha256=expected_registry_manifest_sha256,
            review_round_id=review_round_id,
            label_version=label_version,
        )
        status = "TASKS_READY_FOR_BLIND_REVIEW"
        reasons = ["BLIND_REVIEWS_PENDING"]
        reviewer_binding = _reviewer_binding(
            reviewer_registry,
            expected_registry_sha256=expected_reviewer_key_registry_sha256,
        )
        blinding_binding = {
            "blinding_map_sha256": expected_blinding_map_sha256,
            "blinding_map_hash": validated_blinding_map["blinding_map_hash"],
            "custodian_principal_id": validated_blinding_map["signature"]["principal_id"],
            "id_algorithm": "CUSTODIAN_ASSERTED_CSPRNG_128BIT",
        }
    else:
        if source_registry["assignments"] != [] or source_registry["split_effect"] != "NONE":
            _fail("G103A_BLOCKED_SOURCE_INVALID")
        tasks = []
        status = "BLOCKED_SOURCE_PARTITION"
        reasons = ["SIGNED_DEV_VALIDATION_PARTITION_MISSING"]
        if any(item is not None for item in (
            reviewer_key_registry,
            expected_reviewer_key_registry_hash,
            expected_reviewer_key_registry_sha256,
            blinding_map,
            expected_blinding_map_sha256,
        )):
            _fail("G103A_BLOCKED_INPUT_INVALID")
        reviewer_binding = None
        blinding_binding = None

    source_binding = {
        "registry_manifest_sha256": expected_registry_manifest_sha256,
        "registry_request_hash": source_request["request_hash"],
        "registry_hash": source_registry["registry_hash"],
        "registry_state_root": source_registry["registry_state_root"],
        "registry_status": source_registry["status"],
        "devval_key_registry_hash": expected_devval_key_registry_hash,
        "authority_manifest_sha256": source_request["authority_binding"]["authority_manifest_sha256"],
        "audit_manifest_sha256": source_request["authority_binding"]["audit_manifest_sha256"],
        "candidate_manifest_sha256": source_request["authority_binding"]["candidate_manifest_sha256"],
        "data_cutoff_at": cutoff,
    }
    request = {
        "schema_version": REQUEST_VERSION,
        "review_round_id": review_round_id,
        "requested_at": requested,
        "evaluated_at": evaluated,
        "label_version": label_version,
        "label_contract_hash": LABEL_CONTRACT_HASH,
        "reviewer_binding": reviewer_binding,
        "blinding_binding": blinding_binding,
        "source_binding": source_binding,
        "source_denominator": {
            **source_denominator,
            "assigned_evaluation_task_count": len(tasks),
        },
        "task_count": len(tasks),
        "task_root_hash": canonical_hash([item["task_hash"] for item in tasks]),
        "reviewer_packet_root_hash": canonical_hash([
            item["blind_payload_hash"] for item in tasks
        ]),
        "status": status,
        "reason_codes": reasons,
        "allowed_splits": ["DEV", "VALIDATION"],
        "holdout_status": HOLDOUT_STATUS,
        "not_golden_case": True,
        "golden_eligible": False,
        "replay_eligible": False,
        "gate1_effect": "NONE",
        "not_dataset_receipt": True,
        "not_replay_receipt": True,
        "not_gate_receipt": True,
    }
    request["request_hash"] = canonical_hash(request)
    return _validate_request(request), _validate_tasks(tasks, request)


def evaluate_label_round(
    request: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
    review_responses: Sequence[Mapping[str, Any]],
    adjudications: Sequence[Mapping[str, Any]],
    *,
    reviewer_key_registry: Mapping[str, Any] | None,
    expected_reviewer_key_registry_hash: str | None,
    source_context: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    expected_request, expected_tasks = build_label_assignment_request(**dict(source_context))
    request_value = _validate_request(request)
    tasks_value = _validate_tasks(tasks, request_value)
    if request_value != expected_request or tasks_value != expected_tasks:
        _fail("G103A_SOURCE_SEMANTICS_MISMATCH")

    responses_snapshot = deepcopy(list(review_responses))
    adjudications_snapshot = deepcopy(list(adjudications))
    if request_value["status"] == "BLOCKED_SOURCE_PARTITION":
        if (
            reviewer_key_registry is not None
            or expected_reviewer_key_registry_hash is not None
            or responses_snapshot
            or adjudications_snapshot
        ):
            _fail("G103A_BLOCKED_INPUT_INVALID")
        ledger: list[dict[str, Any]] = []
        return ledger, _round_summary(
            request_value,
            ledger,
            reviewer_key_registry_hash=None,
            status="BLOCKED_SOURCE_PARTITION",
            reasons=request_value["reason_codes"],
        )

    if reviewer_key_registry is None or expected_reviewer_key_registry_hash is None:
        _fail("G103A_REVIEWER_TRUST_ROOT_MISSING")
    registry = _validate_reviewer_key_registry(
        reviewer_key_registry,
        expected_key_registry_hash=expected_reviewer_key_registry_hash,
        expected_registry_sha256=request_value["reviewer_binding"]["registry_sha256"],
    )
    if _reviewer_binding(
        registry,
        expected_registry_sha256=request_value["reviewer_binding"]["registry_sha256"],
    ) != request_value["reviewer_binding"]:
        _fail("G103A_REVIEWER_BINDING_INVALID")
    responses = _validate_review_responses(
        responses_snapshot, request_value, tasks_value, registry,
    )
    adjudication_values = _validate_adjudications(
        adjudications_snapshot, request_value, tasks_value, responses, registry,
    )
    ledger = _derive_ledger(request_value, tasks_value, responses, adjudication_values)
    statuses = {item["status"] for item in ledger}
    if statuses.issubset({"PAIR_AGREED", "ARBITRATED"}) and ledger:
        aggregate_status = "ASSIGNED_EVALUATION_SUBSET_LABELS_RESOLVED"
        reasons: list[str] = []
    elif "CONFLICT_PENDING_ADJUDICATION" in statuses:
        aggregate_status = "ADJUDICATION_PENDING"
        reasons = ["THIRD_REVIEWER_ADJUDICATION_PENDING"]
    else:
        aggregate_status = "BLIND_REVIEWS_PENDING"
        reasons = ["DOUBLE_BLIND_REVIEWS_INCOMPLETE"]
    return ledger, _round_summary(
        request_value,
        ledger,
        reviewer_key_registry_hash=registry["registry_hash"],
        status=aggregate_status,
        reasons=reasons,
    )


def _write_label_round_artifacts(
    request: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
    review_responses: Sequence[Mapping[str, Any]],
    adjudications: Sequence[Mapping[str, Any]],
    reviewer_key_registry: Mapping[str, Any] | None,
    ledger: Sequence[Mapping[str, Any]],
    round_summary: Mapping[str, Any],
    output_dir: str | Path,
    *,
    expected_reviewer_key_registry_hash: str | None,
    source_context: Mapping[str, Any],
) -> dict[str, Any]:
    request_snapshot = deepcopy(dict(request))
    tasks_snapshot = deepcopy(list(tasks))
    responses_snapshot = deepcopy(list(review_responses))
    adjudications_snapshot = deepcopy(list(adjudications))
    reviewer_registry_snapshot = deepcopy(reviewer_key_registry)
    expected_ledger, expected_round = evaluate_label_round(
        request_snapshot,
        tasks_snapshot,
        responses_snapshot,
        adjudications_snapshot,
        reviewer_key_registry=reviewer_registry_snapshot,
        expected_reviewer_key_registry_hash=expected_reviewer_key_registry_hash,
        source_context=source_context,
    )
    if list(ledger) != expected_ledger or dict(round_summary) != expected_round:
        _fail("G103A_DERIVATION_MISMATCH")

    payloads = {
        "assignment-request.json": _json_bytes(request_snapshot),
        "trusted-reviewer-key-registry.json": _json_bytes(reviewer_registry_snapshot),
        "blind-tasks.ndjson": _ndjson(tasks_snapshot),
        "blind-responses.ndjson": _ndjson(responses_snapshot),
        "adjudications.ndjson": _ndjson(adjudications_snapshot),
        "label-fragments.ndjson": _ndjson(expected_ledger),
        "review-round.json": _json_bytes(expected_round),
    }
    descriptors = _descriptors(payloads)
    manifest = _manifest_projection(
        request_snapshot, expected_round,
        expected_reviewer_key_registry_hash=expected_reviewer_key_registry_hash,
        files=descriptors,
    )
    payloads["manifest.json"] = _json_bytes(manifest)
    _write_artifact_directory(Path(output_dir).expanduser(), payloads, exact_files=EXACT_FILES)
    return manifest


def load_validated_label_round_directory(
    input_dir: str | Path,
    *,
    expected_label_manifest_sha256: str,
    expected_reviewer_key_registry_hash: str | None,
    source_context: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    _hash(expected_label_manifest_sha256, "G103A_MANIFEST_ANCHOR_INVALID")
    raw = _read_artifact_directory(Path(input_dir).expanduser(), exact_files=EXACT_FILES)
    if hashlib.sha256(raw["manifest.json"]).hexdigest() != expected_label_manifest_sha256:
        _fail("G103A_MANIFEST_ANCHOR_MISMATCH")
    manifest = _json_document(raw["manifest.json"], "G103A_MANIFEST_JSON_INVALID")
    request = _json_document(raw["assignment-request.json"], "G103A_REQUEST_JSON_INVALID")
    reviewer_registry = _json_document(
        raw["trusted-reviewer-key-registry.json"], "G103A_REVIEWER_REGISTRY_JSON_INVALID",
    )
    tasks = _ndjson_document(raw["blind-tasks.ndjson"], "G103A_TASKS_NDJSON_INVALID")
    responses = _ndjson_document(
        raw["blind-responses.ndjson"], "G103A_RESPONSES_NDJSON_INVALID",
    )
    adjudications = _ndjson_document(
        raw["adjudications.ndjson"], "G103A_ADJUDICATIONS_NDJSON_INVALID",
    )
    ledger = _ndjson_document(
        raw["label-fragments.ndjson"], "G103A_LEDGER_NDJSON_INVALID",
    )
    round_summary = _json_document(raw["review-round.json"], "G103A_ROUND_JSON_INVALID")
    if not all(isinstance(item, Mapping) for item in (manifest, request, round_summary)):
        _fail("G103A_ARTIFACT_JSON_INVALID")
    _validate_manifest_files(manifest, raw)
    expected_ledger, expected_round = evaluate_label_round(
        request,
        tasks,
        responses,
        adjudications,
        reviewer_key_registry=reviewer_registry,
        expected_reviewer_key_registry_hash=expected_reviewer_key_registry_hash,
        source_context=source_context,
    )
    if ledger != expected_ledger or round_summary != expected_round:
        _fail("G103A_DERIVATION_MISMATCH")
    expected_manifest = _manifest_projection(
        request,
        expected_round,
        expected_reviewer_key_registry_hash=expected_reviewer_key_registry_hash,
        files=manifest["files"],
    )
    if manifest != expected_manifest:
        _fail("G103A_MANIFEST_DERIVATION_MISMATCH")
    return request, expected_ledger, expected_round


def _write_reviewer_packet_artifacts(
    request: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
    output_dir: str | Path,
    *,
    source_context: Mapping[str, Any],
) -> dict[str, Any]:
    expected_request, expected_tasks = build_label_assignment_request(**dict(source_context))
    request_value = _validate_request(request)
    tasks_value = _validate_tasks(tasks, request_value)
    if request_value != expected_request or tasks_value != expected_tasks:
        _fail("G103A_SOURCE_SEMANTICS_MISMATCH")
    if request_value["status"] != "TASKS_READY_FOR_BLIND_REVIEW":
        _fail("G103A_REVIEW_PACKET_SOURCE_BLOCKED")
    reviewer_payloads = [deepcopy(item["blind_payload"]) for item in tasks_value]
    payloads = {"blind-payloads.ndjson": _ndjson(reviewer_payloads)}
    manifest = _review_packet_manifest(
        request_value,
        reviewer_payloads,
        files=_descriptors(payloads),
    )
    payloads["manifest.json"] = _json_bytes(manifest)
    _write_artifact_directory(
        Path(output_dir).expanduser(), payloads, exact_files=REVIEW_PACKET_FILES,
    )
    return manifest


def load_validated_reviewer_packet_directory(
    input_dir: str | Path,
    *,
    expected_reviewer_packet_manifest_sha256: str,
    source_context: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    _hash(
        expected_reviewer_packet_manifest_sha256,
        "G103A_REVIEW_PACKET_MANIFEST_ANCHOR_INVALID",
    )
    raw = _read_artifact_directory(
        Path(input_dir).expanduser(), exact_files=REVIEW_PACKET_FILES,
    )
    if (
        hashlib.sha256(raw["manifest.json"]).hexdigest()
        != expected_reviewer_packet_manifest_sha256
    ):
        _fail("G103A_REVIEW_PACKET_MANIFEST_ANCHOR_MISMATCH")
    manifest = _json_document(
        raw["manifest.json"], "G103A_REVIEW_PACKET_MANIFEST_JSON_INVALID",
    )
    reviewer_payloads = _ndjson_document(
        raw["blind-payloads.ndjson"], "G103A_REVIEW_PACKET_NDJSON_INVALID",
    )
    expected_request, expected_tasks = build_label_assignment_request(**dict(source_context))
    if expected_request["status"] != "TASKS_READY_FOR_BLIND_REVIEW":
        _fail("G103A_REVIEW_PACKET_SOURCE_BLOCKED")
    expected_payloads = [deepcopy(item["blind_payload"]) for item in expected_tasks]
    if reviewer_payloads != expected_payloads:
        _fail("G103A_REVIEW_PACKET_DERIVATION_MISMATCH")
    _validate_review_packet_manifest(manifest, raw)
    expected_manifest = _review_packet_manifest(
        expected_request,
        expected_payloads,
        files=manifest["files"],
    )
    if manifest != expected_manifest:
        _fail("G103A_REVIEW_PACKET_MANIFEST_DERIVATION_MISMATCH")
    return expected_payloads, expected_manifest


def write_label_round_artifact_domains(
    request: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
    review_responses: Sequence[Mapping[str, Any]],
    adjudications: Sequence[Mapping[str, Any]],
    reviewer_key_registry: Mapping[str, Any] | None,
    ledger: Sequence[Mapping[str, Any]],
    round_summary: Mapping[str, Any],
    coordinator_output_dir: str | Path,
    reviewer_output_dir: str | Path | None,
    *,
    expected_reviewer_key_registry_hash: str | None,
    source_context: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    request_value = _validate_request(request)
    tasks_value = _validate_tasks(tasks, request_value)
    coordinator_root, parent_path, parent_identity = _canonical_output_sibling(
        coordinator_output_dir,
    )
    reviewer_root: Path | None = None
    if tasks_value:
        if reviewer_output_dir is None:
            _fail("G103A_REVIEW_PACKET_OUTPUT_REQUIRED")
        reviewer_root, reviewer_parent, reviewer_parent_identity = _canonical_output_sibling(
            reviewer_output_dir,
        )
        if (
            reviewer_parent != parent_path
            or reviewer_parent_identity != parent_identity
            or reviewer_root.name == coordinator_root.name
        ):
            _fail("G103A_OUTPUT_DOMAINS_NOT_DISTINCT_SIBLINGS")
    elif reviewer_output_dir is not None:
        _fail("G103A_BLOCKED_REVIEW_PACKET_FORBIDDEN")

    output_roots = [coordinator_root]
    if reviewer_root is not None:
        output_roots.append(reviewer_root)
    for output_root in output_roots:
        try:
            os.stat(output_root, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise GoldenLabelAdjudicationError("G103A_OUTPUT_INVALID") from exc
        _fail("G103A_OUTPUT_EXISTS")

    reviewer_manifest = None
    if reviewer_root is not None:
        reviewer_manifest = _write_reviewer_packet_artifacts(
            request_value,
            tasks_value,
            reviewer_root,
            source_context=source_context,
        )
    coordinator_manifest = _write_label_round_artifacts(
        request_value,
        tasks_value,
        review_responses,
        adjudications,
        reviewer_key_registry,
        ledger,
        round_summary,
        coordinator_root,
        expected_reviewer_key_registry_hash=expected_reviewer_key_registry_hash,
        source_context=source_context,
    )
    parent_after = os.stat(parent_path, follow_symlinks=False)
    if (
        parent_after.st_dev,
        parent_after.st_ino,
        stat.S_IMODE(parent_after.st_mode),
    ) != parent_identity:
        _fail("G103A_OUTPUT_PARENT_CHANGED")

    coordinator_raw = _read_artifact_directory(
        coordinator_root, exact_files=EXACT_FILES,
    )
    load_validated_label_round_directory(
        coordinator_root,
        expected_label_manifest_sha256=hashlib.sha256(
            coordinator_raw["manifest.json"]
        ).hexdigest(),
        expected_reviewer_key_registry_hash=expected_reviewer_key_registry_hash,
        source_context=source_context,
    )
    if reviewer_root is not None:
        reviewer_raw = _read_artifact_directory(
            reviewer_root, exact_files=REVIEW_PACKET_FILES,
        )
        load_validated_reviewer_packet_directory(
            reviewer_root,
            expected_reviewer_packet_manifest_sha256=hashlib.sha256(
                reviewer_raw["manifest.json"]
            ).hexdigest(),
            source_context=source_context,
        )
    return coordinator_manifest, reviewer_manifest


def _canonical_output_sibling(
    value: str | Path,
) -> tuple[Path, Path, tuple[int, int, int]]:
    root = Path(value).expanduser()
    if not root.name or root.name in {".", ".."}:
        _fail("G103A_OUTPUT_INVALID")
    try:
        parent = root.parent.resolve(strict=True)
        parent_stat = os.stat(parent, follow_symlinks=False)
    except OSError as exc:
        raise GoldenLabelAdjudicationError("G103A_OUTPUT_PARENT_INVALID") from exc
    if not stat.S_ISDIR(parent_stat.st_mode):
        _fail("G103A_OUTPUT_PARENT_INVALID")
    return (
        parent / root.name,
        parent,
        (parent_stat.st_dev, parent_stat.st_ino, stat.S_IMODE(parent_stat.st_mode)),
    )


def _review_packet_manifest(
    request: Mapping[str, Any],
    reviewer_payloads: Sequence[Mapping[str, Any]],
    *,
    files: Mapping[str, Any],
) -> dict[str, Any]:
    value = {
        "schema_version": REVIEW_PACKET_MANIFEST_VERSION,
        "review_round_id": request["review_round_id"],
        "packet_count": len(reviewer_payloads),
        "packet_root_hash": canonical_hash([
            canonical_hash(item) for item in reviewer_payloads
        ]),
        "label_contract_hash": LABEL_CONTRACT_HASH,
        "blindness_scope": "REVIEWER_SAFE_PAYLOADS_ONLY_NO_COORDINATOR_MAPPING",
        "holdout_status": HOLDOUT_STATUS,
        "not_golden_case": True,
        "golden_eligible": False,
        "replay_eligible": False,
        "gate1_effect": "NONE",
        "not_dataset_receipt": True,
        "not_replay_receipt": True,
        "not_gate_receipt": True,
        "files": dict(files),
    }
    if value["packet_root_hash"] != request["reviewer_packet_root_hash"]:
        _fail("G103A_REVIEW_PACKET_ROOT_MISMATCH")
    value["manifest_hash"] = canonical_hash(value)
    return value


def _validate_review_packet_manifest(
    value: Any, raw: Mapping[str, bytes],
) -> None:
    fields = {
        "schema_version", "review_round_id", "packet_count", "packet_root_hash",
        "label_contract_hash", "blindness_scope", "holdout_status", "not_golden_case",
        "golden_eligible", "replay_eligible", "gate1_effect", "not_dataset_receipt",
        "not_replay_receipt", "not_gate_receipt", "files", "manifest_hash",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail("G103A_REVIEW_PACKET_MANIFEST_INVALID")
    if (
        value["schema_version"] != REVIEW_PACKET_MANIFEST_VERSION
        or type(value["packet_count"]) is not int
        or value["packet_count"] <= 0
        or value["label_contract_hash"] != LABEL_CONTRACT_HASH
        or value["blindness_scope"]
        != "REVIEWER_SAFE_PAYLOADS_ONLY_NO_COORDINATOR_MAPPING"
        or value["holdout_status"] != HOLDOUT_STATUS
        or value["not_golden_case"] is not True
        or value["golden_eligible"] is not False
        or value["replay_eligible"] is not False
        or value["gate1_effect"] != "NONE"
        or value["not_dataset_receipt"] is not True
        or value["not_replay_receipt"] is not True
        or value["not_gate_receipt"] is not True
        or not isinstance(value["files"], Mapping)
        or set(value["files"]) != {"blind-payloads.ndjson"}
        or value["manifest_hash"] != canonical_hash({
            key: item for key, item in value.items() if key != "manifest_hash"
        })
    ):
        _fail("G103A_REVIEW_PACKET_MANIFEST_INVALID")
    _identifier(value["review_round_id"], "G103A_REVIEW_PACKET_MANIFEST_INVALID")
    _hash(value["packet_root_hash"], "G103A_REVIEW_PACKET_MANIFEST_INVALID")
    descriptor = value["files"]["blind-payloads.ndjson"]
    if not isinstance(descriptor, Mapping) or set(descriptor) != {"sha256", "size_bytes"}:
        _fail("G103A_REVIEW_PACKET_MANIFEST_INVALID")
    _hash(descriptor["sha256"], "G103A_REVIEW_PACKET_MANIFEST_INVALID")
    if (
        type(descriptor["size_bytes"]) is not int
        or descriptor["size_bytes"] != len(raw["blind-payloads.ndjson"])
        or descriptor["sha256"]
        != hashlib.sha256(raw["blind-payloads.ndjson"]).hexdigest()
    ):
        _fail("G103A_REVIEW_PACKET_INTEGRITY_MISMATCH")


def signature_message(
    object_hash: str,
    *,
    key_registry_hash: str,
    key_id: str,
    signer_id: str,
    principal_id: str,
    role: str,
    purpose: str,
) -> bytes:
    _hash(object_hash, "G103A_SIGNATURE_OBJECT_HASH_INVALID")
    _hash(key_registry_hash, "G103A_SIGNATURE_REGISTRY_HASH_INVALID")
    _identifier(key_id, "G103A_SIGNATURE_KEY_INVALID")
    _identifier(signer_id, "G103A_SIGNATURE_SIGNER_INVALID")
    _identifier(principal_id, "G103A_SIGNATURE_PRINCIPAL_INVALID")
    if role not in ALL_ROLES or purpose not in {
        REVIEW_SIGNATURE_PURPOSE,
        DELIVERY_SIGNATURE_PURPOSE,
        BLINDING_MAP_SIGNATURE_PURPOSE,
    }:
        _fail("G103A_SIGNATURE_DOMAIN_INVALID")
    return (
        "GLE_BLIND_LABEL_V1\n"
        f"{key_registry_hash}\n{key_id}\n{signer_id}\n{principal_id}\n{role}\n{purpose}\n{object_hash}\n"
    ).encode()


def _load_source(
    *,
    registry_dir: str | Path,
    expected_registry_manifest_sha256: str,
    expected_devval_key_registry_hash: str | None,
    source_validation: Mapping[str, Any],
) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, dict[str, Any]],
    dict[tuple[str, str], dict[str, Any]], dict[tuple[str, str], dict[str, Any]],
    dict[str, int],
]:
    try:
        request, registry = load_validated_registry_directory(
            registry_dir,
            expected_registry_manifest_sha256=expected_registry_manifest_sha256,
            expected_devval_key_registry_hash=expected_devval_key_registry_hash,
            source_validation=source_validation,
        )
    except Exception as exc:
        raise GoldenLabelAdjudicationError(str(exc)) from exc
    nodes: dict[str, dict[str, Any]] = {}
    candidate_entries: dict[tuple[str, str], dict[str, Any]] = {}
    audit_records: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        candidate, _ = load_validated_candidate_directory(
            source_validation["candidate_dir"],
            expected_candidate_manifest_sha256=(
                source_validation["expected_candidate_manifest_sha256"]
            ),
            audit_dir=source_validation["audit_dir"],
            expected_audit_manifest_sha256=(
                source_validation["expected_audit_manifest_sha256"]
            ),
        )
        audit_bundle, _ = load_validated_audit_directory(
            source_validation["audit_dir"],
            expected_manifest_sha256=source_validation["expected_audit_manifest_sha256"],
        )
    except Exception as exc:
        raise GoldenLabelAdjudicationError(str(exc)) from exc
    for entry in candidate["lineage_candidates"]:
        identity = (entry["source_table"], entry["source_id"])
        if identity in candidate_entries:
            _fail("G103A_CANDIDATE_ENTRY_DUPLICATE")
        candidate_entries[identity] = dict(entry)
    for record in audit_bundle["records"]:
        identity = (record["source_table"], record["source_id"])
        if identity in audit_records:
            _fail("G103A_AUDIT_RECORD_DUPLICATE")
        audit_records[identity] = dict(record)
    coverage = candidate["coverage"]
    source_denominator = {
        "legacy_evaluation_count": coverage["legacy_evaluation_count"],
        "component_candidate_count": coverage["component_candidate_count"],
        "unresolved_count": coverage["unresolved_count"],
        "conflict_count": coverage["conflict_count"],
        "authority_node_count": sum(
            len(item["canonical_experiment_ids"])
            for item in request["eligible_lineages"]
        ),
        "named_exclusion_entry_count": 0,
        "maturing_current_context_count": audit_bundle["coverage"][
            "cutoff_eligible_experiment_current_context"
        ]["maturing_count"],
        "maturing_context_not_asof": audit_bundle["coverage"][
            "cutoff_eligible_experiment_current_context"
        ]["not_asof"],
    }
    if registry["status"] == "SIGNED_DETERMINISTIC_PARTITION":
        authority_dir = source_validation.get("authority_dir")
        if not isinstance(authority_dir, (str, Path)):
            _fail("G103A_AUTHORITY_SOURCE_MISSING")
        try:
            response = _canonical_json_document(
                _read_regular(Path(authority_dir) / "authority-response.json"),
                "G103A_AUTHORITY_RESPONSE_INVALID",
            )
        except HistoricalLineageCandidateError as exc:
            raise GoldenLabelAdjudicationError(str(exc)) from exc
        if not isinstance(response, Mapping) or not isinstance(response.get("lineage_nodes"), list):
            _fail("G103A_AUTHORITY_RESPONSE_INVALID")
        expected_response_hash = request["authority_binding"]["authority_response_hash"]
        if expected_response_hash is None or canonical_hash(response) != expected_response_hash:
            _fail("G103A_AUTHORITY_RESPONSE_BINDING_INVALID")
        for node in response["lineage_nodes"]:
            if not isinstance(node, Mapping):
                _fail("G103A_AUTHORITY_RESPONSE_INVALID")
            identifier = node.get("canonical_experiment_id")
            if not isinstance(identifier, str) or identifier in nodes:
                _fail("G103A_AUTHORITY_RESPONSE_INVALID")
            nodes[identifier] = dict(node)
        expected_ids: set[str] = set()
        for lineage in request["eligible_lineages"]:
            lineage_ids = set(lineage["canonical_experiment_ids"])
            actual_lineage_nodes = [
                node for node in nodes.values() if node.get("lineage_id") == lineage["lineage_id"]
            ]
            if (
                set(node["canonical_experiment_id"] for node in actual_lineage_nodes) != lineage_ids
                or sorted(canonical_hash(node) for node in actual_lineage_nodes)
                != lineage["authority_node_hashes"]
            ):
                _fail("G103A_AUTHORITY_NODE_BINDING_INVALID")
            expected_ids.update(lineage_ids)
        if set(nodes) != expected_ids:
            _fail("G103A_AUTHORITY_NODE_BINDING_INVALID")
        source_denominator["named_exclusion_entry_count"] = sum(
            len(item["candidate_entry_refs"])
            for item in response["exclusions"]
        )
    return (
        request,
        registry,
        nodes,
        candidate_entries,
        audit_records,
        source_denominator,
    )


def _derive_tasks(
    source_request: Mapping[str, Any],
    source_registry: Mapping[str, Any],
    authority_nodes: Mapping[str, Mapping[str, Any]],
    candidate_entries: Mapping[tuple[str, str], Mapping[str, Any]],
    audit_records: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    blinding_map: Mapping[str, Any],
    registry_manifest_sha256: str,
    review_round_id: str,
    label_version: str,
) -> list[dict[str, Any]]:
    task_inputs: list[tuple[Mapping[str, Any], str, Mapping[str, Any], Mapping[str, Any]]] = []
    used_candidate_entries: set[str] = set()
    authority_manifest = source_request["authority_binding"]["authority_manifest_sha256"]
    for assignment in source_registry["assignments"]:
        if assignment["split"] not in ALLOWED_SPLITS:
            _fail("G103A_HOLDOUT_FORBIDDEN")
        for canonical_id in assignment["canonical_experiment_ids"]:
            node = authority_nodes.get(canonical_id)
            if node is None or node.get("lineage_id") != assignment["lineage_id"]:
                _fail("G103A_AUTHORITY_NODE_MISSING")
            for candidate_ref in node["candidate_entry_refs"]:
                identity = (candidate_ref["source_table"], candidate_ref["source_id"])
                entry = candidate_entries.get(identity)
                if entry is None or entry["entry_hash"] != candidate_ref["entry_hash"]:
                    _fail("G103A_CANDIDATE_ENTRY_BINDING_INVALID")
                if entry["entry_hash"] in used_candidate_entries:
                    _fail("G103A_CANDIDATE_ENTRY_REUSED")
                used_candidate_entries.add(entry["entry_hash"])
                task_inputs.append((assignment, canonical_id, node, entry))
    task_inputs.sort(key=lambda item: (
        item[0]["lineage_id"], item[1], item[3]["source_table"], item[3]["source_id"],
    ))
    expected_entry_hashes = [item[3]["entry_hash"] for item in task_inputs]
    map_assignments = blinding_map["assignments"]
    if [item["candidate_entry_hash"] for item in map_assignments] != sorted(
        expected_entry_hashes
    ):
        _fail("G103A_BLINDING_MAP_DENOMINATOR_MISMATCH")
    task_id_by_entry = {
        item["candidate_entry_hash"]: item["opaque_task_id"] for item in map_assignments
    }
    tasks: list[dict[str, Any]] = []
    for assignment, canonical_id, node, entry in task_inputs:
        evidence_refs = [{
            "evidence_id": f"devval:{assignment['assignment_hash']}",
            "artifact_type": "G1_02B2B_DEVVAL_REGISTRY",
            "manifest_sha256": registry_manifest_sha256,
            "record_id": assignment["lineage_id"],
            "record_hash": assignment["assignment_hash"],
            "evidence_class": "SIGNED_DEV_VALIDATION_ASSIGNMENT",
        }, {
            "evidence_id": f"authority:{canonical_hash(node)}",
            "artifact_type": "G1_02B2A_LINEAGE_AUTHORITY",
            "manifest_sha256": authority_manifest,
            "record_id": canonical_id,
            "record_hash": canonical_hash(node),
            "evidence_class": "SIGNED_LINEAGE_AUTHORITY_ATTESTATION",
        }, {
            "evidence_id": f"candidate:{entry['entry_hash']}",
            "artifact_type": "G1_02B1_LINEAGE_CANDIDATE",
            "manifest_sha256": source_request["authority_binding"]["candidate_manifest_sha256"],
            "record_id": f"{entry['source_table']}:{entry['source_id']}",
            "record_hash": entry["entry_hash"],
            "evidence_class": "SOURCE_BOUND_LEGACY_EVALUATION_CANDIDATE",
        }]
        for source_ref in entry["evidence_refs"]:
            identity = (source_ref["source_table"], source_ref["source_id"])
            record = audit_records.get(identity)
            if record is None or record["record_hash"] != source_ref["record_hash"]:
                _fail("G103A_AUDIT_RECORD_BINDING_INVALID")
            evidence_refs.append({
                "evidence_id": f"audit:{canonical_hash(source_ref)}",
                "artifact_type": "G1_02A_FROZEN_AUDIT_RECORD",
                "manifest_sha256": source_request["authority_binding"]["audit_manifest_sha256"],
                "record_id": f"{source_ref['source_table']}:{source_ref['source_id']}",
                "record_hash": source_ref["record_hash"],
                "evidence_class": "FROZEN_AUDIT_RECORD_HASH",
            })
        for item in node.get("authority_evidence_refs", []):
            if not isinstance(item, Mapping):
                _fail("G103A_AUTHORITY_EVIDENCE_INVALID")
            ref = dict(item)
            ref["evidence_id"] = f"assertion:{canonical_hash(item)}"
            ref["evidence_class"] = "AUTHORITY_ASSERTED_NOT_CONTENT_VERIFIED"
            evidence_refs.append(ref)
        evidence_refs = list({item["evidence_id"]: item for item in evidence_refs}.values())
        evidence_refs.sort(key=lambda item: item["evidence_id"])
        opaque_task_id = task_id_by_entry[entry["entry_hash"]]
        frozen_facts = _frozen_evidence_facts(
            entry,
            audit_records,
            task_id=opaque_task_id,
        )
        task = {
            "schema_version": TASK_VERSION,
            "task_id": opaque_task_id,
            "review_round_id": review_round_id,
            "lineage_id": assignment["lineage_id"],
            "canonical_experiment_id": canonical_id,
            "dataset_split": assignment["split"],
            "candidate_source_table": entry["source_table"],
            "candidate_source_id": entry["source_id"],
            "candidate_entry_hash": entry["entry_hash"],
            "assignment_hash": assignment["assignment_hash"],
            "authority_membership_hash": assignment["authority_membership_hash"],
            "evidence_refs": evidence_refs,
            "label_version": label_version,
            "snapshot_id": None,
            "blind_policy": {
                "engine_output_visible": False,
                "peer_label_visible": False,
                "legacy_conclusion_visible": False,
                "allowed_payload": "REDACTED_AUDIT_FACTS_ONLY",
            },
            "not_golden_case": True,
        }
        task["blind_payload"] = {
            "schema_version": TASK_VERSION,
            "task_id": task["task_id"],
            "evidence_packet": {
                "schema_version": "gle-g1-03a-redacted-audit-evidence-v1",
                "facts": frozen_facts,
            },
            "label_version": task["label_version"],
            "label_contract_hash": LABEL_CONTRACT_HASH,
            "blind_policy": task["blind_policy"],
        }
        task["blind_payload_hash"] = canonical_hash(task["blind_payload"])
        task["task_hash"] = canonical_hash(task)
        tasks.append(task)
    tasks.sort(key=lambda item: item["task_id"])
    return tasks


def _frozen_evidence_facts(
    entry: Mapping[str, Any],
    audit_records: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    task_id: str,
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    primary_seen = False
    ordered_refs = sorted(
        entry["evidence_refs"],
        key=lambda item: (item["source_table"], item["source_id"], item["record_hash"]),
    )
    for index, source_ref in enumerate(ordered_refs, start=1):
        record = audit_records[(source_ref["source_table"], source_ref["source_id"])]
        primary = (
            source_ref["source_table"] == entry["source_table"]
            and source_ref["source_id"] == entry["source_id"]
        )
        primary_seen = primary_seen or primary
        projection = record["projection"]
        projection_facts: dict[str, Any] = {}
        if primary:
            projection_facts = {
                "source_kind": projection.get("source_kind"),
                "evaluated_at_present": projection.get("evaluated_at") is not None,
                "subject_count": len(projection.get("subject_experiment_ids", [])),
                "missing_field_count": len(projection.get("missing_fields", [])),
            }
        elif record["source_table"] == "ad_experiment":
            projection_facts = {
                "control_commitment_present": isinstance(projection.get("control_hash"), str),
                "hypothesis_commitment_present": isinstance(
                    projection.get("hypothesis_hash"), str
                ),
            }
        fact = {
            "fact_id": f"fact_{index:03d}",
            "fact_kind": (
                "PRIMARY_FROZEN_EVALUATION_AUDIT_FACT"
                if primary else "SUPPORTING_FROZEN_EXPERIMENT_AUDIT_FACT"
            ),
            "semantic_at_present": record["semantic_at"] is not None,
            "cutoff_disposition": record["cutoff_disposition"],
            "reconstruction_status": record["reconstruction_status"],
            "audit_reason_codes": record["reason_codes"],
            "projection_facts": projection_facts,
        }
        facts.append(fact)
    if not primary_seen or not facts:
        _fail("G103A_PRIMARY_AUDIT_EVIDENCE_MISSING")
    # The task-local identifier is intentionally the only linkage in the reviewer packet.
    if not _OPAQUE_TASK_ID_RE.fullmatch(task_id):
        _fail("G103A_BLIND_TASK_ID_INVALID")
    return facts


def _validate_request(value: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "schema_version", "review_round_id", "requested_at", "evaluated_at",
        "label_version", "label_contract_hash", "reviewer_binding", "blinding_binding",
        "source_binding",
        "source_denominator", "task_count", "task_root_hash", "reviewer_packet_root_hash",
        "status",
        "reason_codes", "allowed_splits", "holdout_status", "not_golden_case",
        "golden_eligible", "replay_eligible", "gate1_effect", "not_dataset_receipt",
        "not_replay_receipt", "not_gate_receipt", "request_hash",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G103A_REQUEST_INVALID")
    request = dict(value)
    _identifier(request["review_round_id"], "G103A_REQUEST_INVALID")
    if request["label_version"] != LABEL_CONTRACT_VERSION:
        _fail("G103A_LABEL_CONTRACT_INVALID")
    _utc(request["requested_at"], "G103A_REQUEST_INVALID")
    _utc(request["evaluated_at"], "G103A_REQUEST_INVALID")
    _validate_source_binding(request["source_binding"])
    _validate_source_denominator(request["source_denominator"])
    if (
        request["schema_version"] != REQUEST_VERSION
        or request["label_contract_hash"] != LABEL_CONTRACT_HASH
        or type(request["task_count"]) is not int
        or request["task_count"] < 0
        or request["status"] not in {
            "BLOCKED_SOURCE_PARTITION", "TASKS_READY_FOR_BLIND_REVIEW",
        }
        or not isinstance(request["reason_codes"], list)
        or not request["reason_codes"]
        or request["reason_codes"] != sorted(set(request["reason_codes"]))
        or any(not isinstance(item, str) or not _CODE_RE.fullmatch(item) for item in request["reason_codes"])
        or request["allowed_splits"] != ["DEV", "VALIDATION"]
        or request["holdout_status"] != HOLDOUT_STATUS
        or request["not_golden_case"] is not True
        or request["golden_eligible"] is not False
        or request["replay_eligible"] is not False
        or request["gate1_effect"] != "NONE"
        or request["not_dataset_receipt"] is not True
        or request["not_replay_receipt"] is not True
        or request["not_gate_receipt"] is not True
    ):
        _fail("G103A_REQUEST_INVALID")
    _hash(request["task_root_hash"], "G103A_REQUEST_INVALID")
    _hash(request["reviewer_packet_root_hash"], "G103A_REQUEST_INVALID")
    if (
        (request["status"] == "BLOCKED_SOURCE_PARTITION")
        != (request["task_count"] == 0)
        or request["source_denominator"]["assigned_evaluation_task_count"]
        != request["task_count"]
    ):
        _fail("G103A_REQUEST_STATE_INVALID")
    if request["status"] == "BLOCKED_SOURCE_PARTITION":
        if request["reviewer_binding"] is not None or request["blinding_binding"] is not None:
            _fail("G103A_REQUEST_STATE_INVALID")
    else:
        _validate_reviewer_binding(request["reviewer_binding"])
        _validate_blinding_binding(request["blinding_binding"], request["reviewer_binding"])
        if (
            request["source_denominator"]["assigned_evaluation_task_count"]
            + request["source_denominator"]["named_exclusion_entry_count"]
            != request["source_denominator"]["legacy_evaluation_count"]
        ):
            _fail("G103A_SOURCE_DENOMINATOR_INVALID")
    if request["request_hash"] != canonical_hash({
        key: item for key, item in request.items() if key != "request_hash"
    }):
        _fail("G103A_REQUEST_HASH_INVALID")
    return request


def _validate_source_binding(value: Any) -> None:
    keys = {
        "registry_manifest_sha256", "registry_request_hash", "registry_hash",
        "registry_state_root", "registry_status", "devval_key_registry_hash",
        "authority_manifest_sha256", "audit_manifest_sha256",
        "candidate_manifest_sha256", "data_cutoff_at",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G103A_SOURCE_BINDING_INVALID")
    for key in (
        "registry_manifest_sha256", "registry_request_hash", "registry_hash",
        "authority_manifest_sha256", "audit_manifest_sha256", "candidate_manifest_sha256",
    ):
        _hash(value[key], "G103A_SOURCE_BINDING_INVALID")
    for key in ("registry_state_root", "devval_key_registry_hash"):
        if value[key] is not None:
            _hash(value[key], "G103A_SOURCE_BINDING_INVALID")
    _utc(value["data_cutoff_at"], "G103A_SOURCE_BINDING_INVALID")
    if value["registry_status"] not in {
        "BLOCKED", "PENDING_SIGNATURES", "SIGNED_DETERMINISTIC_PARTITION",
    }:
        _fail("G103A_SOURCE_BINDING_INVALID")
    signed = value["registry_status"] == "SIGNED_DETERMINISTIC_PARTITION"
    if (value["registry_state_root"] is not None) != signed or (
        value["devval_key_registry_hash"] is not None
    ) != signed:
        _fail("G103A_SOURCE_BINDING_INVALID")


def _validate_source_denominator(value: Any) -> None:
    keys = {
        "legacy_evaluation_count", "component_candidate_count", "unresolved_count",
        "conflict_count", "authority_node_count", "named_exclusion_entry_count",
        "maturing_current_context_count", "maturing_context_not_asof",
        "assigned_evaluation_task_count",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G103A_SOURCE_DENOMINATOR_INVALID")
    integer_keys = keys - {"maturing_context_not_asof"}
    if any(type(value[key]) is not int or value[key] < 0 for key in integer_keys):
        _fail("G103A_SOURCE_DENOMINATOR_INVALID")
    if value["maturing_context_not_asof"] is not True:
        _fail("G103A_SOURCE_DENOMINATOR_INVALID")
    if value["assigned_evaluation_task_count"] > value["legacy_evaluation_count"]:
        _fail("G103A_SOURCE_DENOMINATOR_INVALID")


def _validate_reviewer_binding(value: Any) -> None:
    keys = {
        "registry_sha256", "registry_hash", "identity_issuer",
        "identity_registry_manifest_sha256", "role_assignments",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G103A_REVIEWER_BINDING_INVALID")
    _hash(value["registry_sha256"], "G103A_REVIEWER_BINDING_INVALID")
    _hash(value["registry_hash"], "G103A_REVIEWER_BINDING_INVALID")
    _identifier(value["identity_issuer"], "G103A_REVIEWER_BINDING_INVALID")
    _hash(value["identity_registry_manifest_sha256"], "G103A_REVIEWER_BINDING_INVALID")
    assignments = value["role_assignments"]
    fields = {"role", "key_id", "signer_id", "principal_id", "spki_sha256"}
    if (
        not isinstance(assignments, list)
        or len(assignments) != len(ALL_ROLES)
        or assignments != sorted(assignments, key=lambda item: ALL_ROLES.index(item.get("role")) if isinstance(item, Mapping) and item.get("role") in ALL_ROLES else len(ALL_ROLES))
    ):
        _fail("G103A_REVIEWER_BINDING_INVALID")
    for item in assignments:
        if not isinstance(item, Mapping) or set(item) != fields:
            _fail("G103A_REVIEWER_BINDING_INVALID")
        for key in ("key_id", "signer_id", "principal_id"):
            _identifier(item[key], "G103A_REVIEWER_BINDING_INVALID")
        if item["role"] not in ALL_ROLES:
            _fail("G103A_REVIEWER_BINDING_INVALID")
        _hash(item["spki_sha256"], "G103A_REVIEWER_BINDING_INVALID")


def _validate_blinding_binding(value: Any, reviewer_binding: Mapping[str, Any]) -> None:
    keys = {
        "blinding_map_sha256", "blinding_map_hash", "custodian_principal_id",
        "id_algorithm",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G103A_BLINDING_BINDING_INVALID")
    _hash(value["blinding_map_sha256"], "G103A_BLINDING_BINDING_INVALID")
    _hash(value["blinding_map_hash"], "G103A_BLINDING_BINDING_INVALID")
    _identifier(value["custodian_principal_id"], "G103A_BLINDING_BINDING_INVALID")
    custodian = next(
        (
            item for item in reviewer_binding["role_assignments"]
            if item["role"] == "BLINDING_CUSTODIAN"
        ),
        None,
    )
    if (
        custodian is None
        or value["custodian_principal_id"] != custodian["principal_id"]
        or value["id_algorithm"] != "CUSTODIAN_ASSERTED_CSPRNG_128BIT"
    ):
        _fail("G103A_BLINDING_BINDING_INVALID")


def _validate_tasks(values: Sequence[Mapping[str, Any]], request: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(values, (list, tuple)):
        _fail("G103A_TASKS_INVALID")
    tasks = [dict(item) if isinstance(item, Mapping) else _raise("G103A_TASKS_INVALID") for item in values]
    if tasks != sorted(tasks, key=lambda item: item.get("task_id", "")):
        _fail("G103A_TASKS_INVALID")
    keys = {
        "schema_version", "task_id", "review_round_id", "lineage_id",
        "canonical_experiment_id", "dataset_split", "candidate_source_table",
        "candidate_source_id", "candidate_entry_hash", "assignment_hash",
        "authority_membership_hash", "evidence_refs", "label_version", "snapshot_id",
        "blind_policy", "not_golden_case", "blind_payload", "blind_payload_hash", "task_hash",
    }
    seen: set[str] = set()
    for task in tasks:
        if set(task) != keys:
            _fail("G103A_TASKS_INVALID")
        for key in ("task_id", "review_round_id", "lineage_id", "canonical_experiment_id", "label_version"):
            _identifier(task[key], "G103A_TASKS_INVALID")
        for key in ("candidate_source_table", "candidate_source_id"):
            _identifier(task[key], "G103A_TASKS_INVALID")
        if task["task_id"] in seen:
            _fail("G103A_TASKS_INVALID")
        seen.add(task["task_id"])
        if (
            task["schema_version"] != TASK_VERSION
            or not _OPAQUE_TASK_ID_RE.fullmatch(task["task_id"])
            or task["review_round_id"] != request["review_round_id"]
            or task["label_version"] != request["label_version"]
            or task["dataset_split"] not in ALLOWED_SPLITS
            or task["snapshot_id"] is not None
            or task["blind_policy"] != {
                "engine_output_visible": False,
                "peer_label_visible": False,
                "legacy_conclusion_visible": False,
                "allowed_payload": "REDACTED_AUDIT_FACTS_ONLY",
            }
            or task["not_golden_case"] is not True
        ):
            _fail("G103A_TASKS_INVALID")
        _hash(task["assignment_hash"], "G103A_TASKS_INVALID")
        _hash(task["candidate_entry_hash"], "G103A_TASKS_INVALID")
        _hash(task["authority_membership_hash"], "G103A_TASKS_INVALID")
        _validate_evidence_refs(task["evidence_refs"])
        expected_blind_payload = {
            "schema_version": TASK_VERSION,
            "task_id": task["task_id"],
            "evidence_packet": task["blind_payload"].get("evidence_packet")
            if isinstance(task["blind_payload"], Mapping) else None,
            "label_version": task["label_version"],
            "label_contract_hash": LABEL_CONTRACT_HASH,
            "blind_policy": task["blind_policy"],
        }
        _validate_frozen_evidence_packet(expected_blind_payload["evidence_packet"])
        expected_blind_hash = canonical_hash(expected_blind_payload)
        if task["blind_payload"] != expected_blind_payload:
            _fail("G103A_BLIND_PAYLOAD_INVALID")
        if task["blind_payload_hash"] != expected_blind_hash or task["task_hash"] != canonical_hash({
            key: item for key, item in task.items() if key != "task_hash"
        }):
            _fail("G103A_TASK_HASH_INVALID")
    if (
        len(tasks) != request["task_count"]
        or canonical_hash([item["task_hash"] for item in tasks]) != request["task_root_hash"]
        or canonical_hash([
            item["blind_payload_hash"] for item in tasks
        ]) != request["reviewer_packet_root_hash"]
    ):
        _fail("G103A_TASK_DENOMINATOR_MISMATCH")
    return tasks


def _validate_frozen_evidence_packet(value: Any) -> None:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"schema_version", "facts"}
        or value["schema_version"] != "gle-g1-03a-redacted-audit-evidence-v1"
        or not isinstance(value["facts"], list)
        or not value["facts"]
    ):
        _fail("G103A_FROZEN_EVIDENCE_INVALID")
    fact_fields = {
        "fact_id", "fact_kind", "semantic_at_present", "cutoff_disposition",
        "reconstruction_status", "audit_reason_codes", "projection_facts",
    }
    primary_count = 0
    for index, fact in enumerate(value["facts"], start=1):
        if not isinstance(fact, Mapping) or set(fact) != fact_fields:
            _fail("G103A_FROZEN_EVIDENCE_INVALID")
        if fact["fact_id"] != f"fact_{index:03d}" or fact["fact_kind"] not in {
            "PRIMARY_FROZEN_EVALUATION_AUDIT_FACT",
            "SUPPORTING_FROZEN_EXPERIMENT_AUDIT_FACT",
        }:
            _fail("G103A_FROZEN_EVIDENCE_INVALID")
        primary_count += fact["fact_kind"] == "PRIMARY_FROZEN_EVALUATION_AUDIT_FACT"
        if (
            type(fact["semantic_at_present"]) is not bool
            or not isinstance(fact["cutoff_disposition"], str)
            or not isinstance(fact["reconstruction_status"], str)
            or not isinstance(fact["audit_reason_codes"], list)
            or fact["audit_reason_codes"] != sorted(set(fact["audit_reason_codes"]))
            or not isinstance(fact["projection_facts"], Mapping)
        ):
            _fail("G103A_FROZEN_EVIDENCE_INVALID")
        projection = fact["projection_facts"]
        if fact["fact_kind"] == "PRIMARY_FROZEN_EVALUATION_AUDIT_FACT":
            if (
                set(projection) != {
                    "source_kind", "evaluated_at_present", "subject_count",
                    "missing_field_count",
                }
                or projection["source_kind"] not in {
                    "SINGLE_EXPERIMENT", "CREATIVE_GROUP", "AUDIENCE_PAIR",
                }
                or type(projection["evaluated_at_present"]) is not bool
                or type(projection["subject_count"]) is not int
                or projection["subject_count"] < 1
                or type(projection["missing_field_count"]) is not int
                or projection["missing_field_count"] < 0
            ):
                _fail("G103A_FROZEN_EVIDENCE_INVALID")
        elif (
            set(projection) != {
                "control_commitment_present", "hypothesis_commitment_present",
            }
            or type(projection["control_commitment_present"]) is not bool
            or type(projection["hypothesis_commitment_present"]) is not bool
        ):
            _fail("G103A_FROZEN_EVIDENCE_INVALID")
    if primary_count != 1:
        _fail("G103A_PRIMARY_AUDIT_EVIDENCE_MISSING")


def _validate_evidence_refs(values: Any) -> None:
    keys = {
        "evidence_id", "artifact_type", "manifest_sha256", "record_id",
        "record_hash", "evidence_class",
    }
    if (
        not isinstance(values, list)
        or not values
        or values != sorted(values, key=lambda item: item.get("evidence_id", "") if isinstance(item, Mapping) else "")
    ):
        _fail("G103A_EVIDENCE_REFS_INVALID")
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, Mapping) or set(item) != keys:
            _fail("G103A_EVIDENCE_REFS_INVALID")
        for key in ("evidence_id", "artifact_type", "record_id", "evidence_class"):
            _identifier(item[key], "G103A_EVIDENCE_REFS_INVALID")
        if item["evidence_id"] in seen:
            _fail("G103A_EVIDENCE_REFS_INVALID")
        seen.add(item["evidence_id"])
        _hash(item["manifest_sha256"], "G103A_EVIDENCE_REFS_INVALID")
        _hash(item["record_hash"], "G103A_EVIDENCE_REFS_INVALID")


def _validate_reviewer_key_registry(
    value: Mapping[str, Any], *, expected_key_registry_hash: str,
    expected_registry_sha256: str,
) -> dict[str, Any]:
    _hash(expected_key_registry_hash, "G103A_REVIEWER_REGISTRY_ANCHOR_INVALID")
    _hash(expected_registry_sha256, "G103A_REVIEWER_REGISTRY_ANCHOR_INVALID")
    keys = {
        "schema_version", "registry_id", "identity_issuer",
        "identity_registry_manifest_sha256", "keys", "registry_hash",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G103A_REVIEWER_REGISTRY_INVALID")
    registry = dict(value)
    if (
        registry["schema_version"] != REVIEWER_KEY_REGISTRY_VERSION
        or registry["registry_hash"] != canonical_hash({
            key: item for key, item in registry.items() if key != "registry_hash"
        })
        or registry["registry_hash"] != expected_key_registry_hash
        or hashlib.sha256(_json_bytes(registry)).hexdigest() != expected_registry_sha256
    ):
        _fail("G103A_REVIEWER_REGISTRY_INVALID")
    _identifier(registry["registry_id"], "G103A_REVIEWER_REGISTRY_INVALID")
    _identifier(registry["identity_issuer"], "G103A_REVIEWER_REGISTRY_INVALID")
    _hash(
        registry["identity_registry_manifest_sha256"],
        "G103A_REVIEWER_REGISTRY_INVALID",
    )
    key_fields = {
        "key_id", "signer_id", "principal_id", "role", "purposes", "algorithm", "status",
        "valid_from", "valid_until", "public_key_pem",
    }
    if (
        not isinstance(registry["keys"], list)
        or len(registry["keys"]) != len(ALL_ROLES)
        or registry["keys"] != sorted(registry["keys"], key=lambda item: item.get("key_id", "") if isinstance(item, Mapping) else "")
    ):
        _fail("G103A_REVIEWER_REGISTRY_INVALID")
    seen_keys: set[str] = set()
    seen_signers: set[str] = set()
    seen_principals: set[str] = set()
    seen_roles: set[str] = set()
    fingerprints: set[str] = set()
    for item in registry["keys"]:
        if not isinstance(item, Mapping) or set(item) != key_fields:
            _fail("G103A_REVIEWER_REGISTRY_INVALID")
        for key in ("key_id", "signer_id", "principal_id"):
            _identifier(item[key], "G103A_REVIEWER_REGISTRY_INVALID")
        expected_purposes = (
            sorted([DELIVERY_SIGNATURE_PURPOSE, BLINDING_MAP_SIGNATURE_PURPOSE])
            if item["role"] == "BLINDING_CUSTODIAN"
            else [REVIEW_SIGNATURE_PURPOSE]
        )
        if (
            item["key_id"] in seen_keys
            or item["signer_id"] in seen_signers
            or item["principal_id"] in seen_principals
            or item["role"] in seen_roles
            or item["role"] not in ALL_ROLES
            or item["purposes"] != expected_purposes
            or item["algorithm"] != SIGNATURE_ALGORITHM
            or item["status"] != "ACTIVE"
            or not isinstance(item["public_key_pem"], str)
            or not _PEM_RE.fullmatch(item["public_key_pem"])
        ):
            _fail("G103A_REVIEWER_REGISTRY_INVALID")
        bits, fingerprint = _rsa_public_key_metadata(item["public_key_pem"])
        if bits < MINIMUM_RSA_BITS or fingerprint in fingerprints:
            _fail("G103A_REVIEWER_REGISTRY_INVALID")
        seen_keys.add(item["key_id"])
        seen_signers.add(item["signer_id"])
        seen_principals.add(item["principal_id"])
        seen_roles.add(item["role"])
        fingerprints.add(fingerprint)
        if _instant(_utc(item["valid_until"], "G103A_REVIEWER_REGISTRY_INVALID")) <= _instant(
            _utc(item["valid_from"], "G103A_REVIEWER_REGISTRY_INVALID")
        ):
            _fail("G103A_REVIEWER_REGISTRY_INVALID")
    if seen_roles != set(ALL_ROLES):
        _fail("G103A_REVIEWER_REGISTRY_INVALID")
    return registry


def _validate_blinding_map(
    value: Mapping[str, Any],
    *,
    expected_blinding_map_sha256: str,
    review_round_id: str,
    data_cutoff_at: str,
    requested_at: str,
    registry: Mapping[str, Any],
) -> dict[str, Any]:
    _hash(expected_blinding_map_sha256, "G103A_BLINDING_MAP_ANCHOR_INVALID")
    fields = {
        "schema_version", "review_round_id", "issued_at", "issuance_method", "assignments",
        "blinding_map_hash", "signature",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail("G103A_BLINDING_MAP_INVALID")
    result = deepcopy(dict(value))
    raw = _json_bytes(result)
    if len(raw) > MAX_ARTIFACT_FILE_BYTES:
        _fail("G103A_BLINDING_MAP_TOO_LARGE")
    if hashlib.sha256(raw).hexdigest() != expected_blinding_map_sha256:
        _fail("G103A_BLINDING_MAP_ANCHOR_MISMATCH")
    issued_at = _utc(result["issued_at"], "G103A_BLINDING_MAP_INVALID")
    if (
        result["schema_version"] != BLINDING_MAP_VERSION
        or result["review_round_id"] != review_round_id
        or result["issuance_method"] != "PYTHON_SECRETS_TOKEN_HEX_16"
        or not (
            _instant(data_cutoff_at)
            <= _instant(issued_at)
            <= _instant(requested_at)
        )
        or not isinstance(result["assignments"], list)
        or not result["assignments"]
    ):
        _fail("G103A_BLINDING_MAP_INVALID")
    assignment_fields = {"candidate_entry_hash", "opaque_task_id"}
    if result["assignments"] != sorted(
        result["assignments"],
        key=lambda item: item.get("candidate_entry_hash", "")
        if isinstance(item, Mapping) else "",
    ):
        _fail("G103A_BLINDING_MAP_INVALID")
    entry_hashes: set[str] = set()
    opaque_ids: set[str] = set()
    for item in result["assignments"]:
        if not isinstance(item, Mapping) or set(item) != assignment_fields:
            _fail("G103A_BLINDING_MAP_INVALID")
        _hash(item["candidate_entry_hash"], "G103A_BLINDING_MAP_INVALID")
        if (
            not isinstance(item["opaque_task_id"], str)
            or not _OPAQUE_TASK_ID_RE.fullmatch(item["opaque_task_id"])
            or item["candidate_entry_hash"] in entry_hashes
            or item["opaque_task_id"] in opaque_ids
        ):
            _fail("G103A_BLINDING_MAP_INVALID")
        entry_hashes.add(item["candidate_entry_hash"])
        opaque_ids.add(item["opaque_task_id"])
    expected_hash = canonical_hash({
        key: item for key, item in result.items()
        if key not in {"blinding_map_hash", "signature"}
    })
    if result["blinding_map_hash"] != expected_hash:
        _fail("G103A_BLINDING_MAP_HASH_INVALID")
    _validate_signature(
        result["signature"],
        object_hash=expected_hash,
        expected_role="BLINDING_CUSTODIAN",
        expected_purpose=BLINDING_MAP_SIGNATURE_PURPOSE,
        expected_signed_at=issued_at,
        registry=registry,
    )
    return result


def _reviewer_binding(
    registry: Mapping[str, Any], *, expected_registry_sha256: str,
) -> dict[str, Any]:
    assignments = []
    for role in ALL_ROLES:
        item = next(key for key in registry["keys"] if key["role"] == role)
        _, fingerprint = _rsa_public_key_metadata(item["public_key_pem"])
        assignments.append({
            "role": role,
            "key_id": item["key_id"],
            "signer_id": item["signer_id"],
            "principal_id": item["principal_id"],
            "spki_sha256": fingerprint,
        })
    value = {
        "registry_sha256": expected_registry_sha256,
        "registry_hash": registry["registry_hash"],
        "identity_issuer": registry["identity_issuer"],
        "identity_registry_manifest_sha256": registry["identity_registry_manifest_sha256"],
        "role_assignments": assignments,
    }
    _validate_reviewer_binding(value)
    return value


def _validate_review_responses(
    values: Sequence[Mapping[str, Any]], request: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]], registry: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(values, list) or len(values) > 2 * len(tasks):
        _fail("G103A_RESPONSES_INVALID")
    responses = [dict(item) if isinstance(item, Mapping) else _raise("G103A_RESPONSES_INVALID") for item in values]
    if responses != sorted(responses, key=lambda item: (item.get("task_id", ""), item.get("reviewer_role", ""))):
        _fail("G103A_RESPONSES_INVALID")
    task_by_id = {item["task_id"]: item for item in tasks}
    seen: set[tuple[str, str]] = set()
    response_ids: set[str] = set()
    delivery_ids: set[str] = set()
    keys = {
        "schema_version", "response_id", "request_hash", "task_id",
        "reviewer_id", "reviewer_role", "delivery_receipt", "label", "submitted_at",
        "blind_attestation", "response_payload_hash", "signature",
    }
    for response in responses:
        if set(response) != keys:
            _fail("G103A_RESPONSES_INVALID")
        task = task_by_id.get(response["task_id"])
        if task is None or response["reviewer_role"] not in {"REVIEWER_A", "REVIEWER_B"}:
            _fail("G103A_RESPONSES_INVALID")
        pair = (response["task_id"], response["reviewer_role"])
        if pair in seen:
            _fail("G103A_RESPONSES_INVALID")
        seen.add(pair)
        _identifier(response["response_id"], "G103A_RESPONSES_INVALID")
        _identifier(response["reviewer_id"], "G103A_RESPONSES_INVALID")
        delivery_id = response["delivery_receipt"].get("delivery_id") if isinstance(
            response["delivery_receipt"], Mapping
        ) else None
        if not isinstance(delivery_id, str):
            _fail("G103A_DELIVERY_INVALID")
        if response["response_id"] in response_ids or delivery_id in delivery_ids:
            _fail("G103A_RESPONSE_ID_REUSED")
        response_ids.add(response["response_id"])
        delivery_ids.add(delivery_id)
        submitted = _utc(response["submitted_at"], "G103A_RESPONSES_INVALID")
        if (
            response["schema_version"] != REVIEW_RESPONSE_VERSION
            or response["request_hash"] != request["request_hash"]
            or response["blind_attestation"] != {
                "engine_output_seen": False,
                "peer_label_seen": False,
                "legacy_conclusion_seen": False,
            }
            or not (
                _instant(request["requested_at"])
                <= _instant(submitted)
                <= _instant(request["evaluated_at"])
            )
        ):
            _fail("G103A_RESPONSES_INVALID")
        _validate_label(response["label"], task)
        _validate_delivery(response["delivery_receipt"], request, task, response, registry)
        expected_payload = canonical_hash({
            key: item for key, item in response.items()
            if key not in {"response_payload_hash", "signature"}
        })
        if response["response_payload_hash"] != expected_payload:
            _fail("G103A_RESPONSE_HASH_INVALID")
        _validate_signature(
            response["signature"],
            object_hash=expected_payload,
            expected_role=response["reviewer_role"],
            expected_purpose=REVIEW_SIGNATURE_PURPOSE,
            expected_signed_at=submitted,
            registry=registry,
        )
        if response["signature"]["principal_id"] != response["reviewer_id"]:
            _fail("G103A_REVIEWER_IDENTITY_INVALID")
    by_task = {task["task_id"]: {} for task in tasks}
    for response in responses:
        by_task[response["task_id"]][response["reviewer_role"]] = response
    for pair in by_task.values():
        if set(pair) == {"REVIEWER_A", "REVIEWER_B"}:
            latest_delivery = max(
                _instant(pair["REVIEWER_A"]["delivery_receipt"]["delivered_at"]),
                _instant(pair["REVIEWER_B"]["delivery_receipt"]["delivered_at"]),
            )
            earliest_submission = min(
                _instant(pair["REVIEWER_A"]["submitted_at"]),
                _instant(pair["REVIEWER_B"]["submitted_at"]),
            )
            if latest_delivery > earliest_submission:
                _fail("G103A_BLIND_DELIVERY_ORDER_INVALID")
    return responses


def _validate_delivery(
    value: Any, request: Mapping[str, Any], task: Mapping[str, Any],
    response: Mapping[str, Any], registry: Mapping[str, Any],
) -> None:
    keys = {
        "schema_version", "delivery_id", "request_hash", "task_id",
        "blind_payload_hash", "recipient_reviewer_id", "recipient_role", "delivered_at",
        "visible_artifact_hashes", "delivery_payload_hash", "signature",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G103A_DELIVERY_INVALID")
    _identifier(value["delivery_id"], "G103A_DELIVERY_INVALID")
    delivered = _utc(value["delivered_at"], "G103A_DELIVERY_INVALID")
    if (
        value["schema_version"] != DELIVERY_VERSION
        or value["request_hash"] != request["request_hash"]
        or value["task_id"] != task["task_id"]
        or value["blind_payload_hash"] != task["blind_payload_hash"]
        or value["recipient_reviewer_id"] != response["reviewer_id"]
        or value["recipient_role"] != response["reviewer_role"]
        or value["visible_artifact_hashes"] != [task["blind_payload_hash"]]
        or not (
            _instant(request["requested_at"])
            <= _instant(delivered)
            <= _instant(response["submitted_at"])
        )
    ):
        _fail("G103A_DELIVERY_INVALID")
    expected_payload = canonical_hash({
        key: item for key, item in value.items()
        if key not in {"delivery_payload_hash", "signature"}
    })
    if value["delivery_payload_hash"] != expected_payload:
        _fail("G103A_DELIVERY_HASH_INVALID")
    _validate_signature(
        value["signature"],
        object_hash=expected_payload,
        expected_role="BLINDING_CUSTODIAN",
        expected_purpose=DELIVERY_SIGNATURE_PURPOSE,
        expected_signed_at=delivered,
        registry=registry,
    )


def _validate_adjudications(
    values: Sequence[Mapping[str, Any]], request: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]], responses: Sequence[Mapping[str, Any]],
    registry: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(values, list) or len(values) > len(tasks):
        _fail("G103A_ADJUDICATIONS_INVALID")
    adjudications = [dict(item) if isinstance(item, Mapping) else _raise("G103A_ADJUDICATIONS_INVALID") for item in values]
    if adjudications != sorted(adjudications, key=lambda item: item.get("task_id", "")):
        _fail("G103A_ADJUDICATIONS_INVALID")
    task_by_id = {item["task_id"]: item for item in tasks}
    response_by_pair = {(item["task_id"], item["reviewer_role"]): item for item in responses}
    seen: set[str] = set()
    adjudication_ids: set[str] = set()
    delivery_ids: set[str] = set()
    keys = {
        "schema_version", "adjudication_id", "request_hash", "task_id",
        "reviewer_id", "reviewer_role", "reviewer_a_response_hash",
        "reviewer_b_response_hash", "label", "submitted_at", "blind_attestation",
        "delivery_receipt", "adjudication_payload_hash", "signature",
    }
    for item in adjudications:
        if set(item) != keys or item["task_id"] in seen:
            _fail("G103A_ADJUDICATIONS_INVALID")
        seen.add(item["task_id"])
        task = task_by_id.get(item["task_id"])
        response_a = response_by_pair.get((item["task_id"], "REVIEWER_A"))
        response_b = response_by_pair.get((item["task_id"], "REVIEWER_B"))
        if task is None or response_a is None or response_b is None:
            _fail("G103A_ADJUDICATION_WITHOUT_PAIR")
        if _label_projection(response_a["label"]) == _label_projection(response_b["label"]):
            _fail("G103A_UNNEEDED_ADJUDICATION")
        submitted = _utc(item["submitted_at"], "G103A_ADJUDICATIONS_INVALID")
        _identifier(item["adjudication_id"], "G103A_ADJUDICATIONS_INVALID")
        _identifier(item["reviewer_id"], "G103A_ADJUDICATIONS_INVALID")
        delivery_id = item["delivery_receipt"].get("delivery_id") if isinstance(
            item["delivery_receipt"], Mapping
        ) else None
        if not isinstance(delivery_id, str):
            _fail("G103A_ADJUDICATOR_DELIVERY_INVALID")
        if item["adjudication_id"] in adjudication_ids or delivery_id in delivery_ids:
            _fail("G103A_ADJUDICATION_ID_REUSED")
        adjudication_ids.add(item["adjudication_id"])
        delivery_ids.add(delivery_id)
        if (
            item["schema_version"] != ADJUDICATION_VERSION
            or item["request_hash"] != request["request_hash"]
            or item["reviewer_role"] != "ADJUDICATOR_C"
            or item["reviewer_a_response_hash"] != response_a["response_payload_hash"]
            or item["reviewer_b_response_hash"] != response_b["response_payload_hash"]
            or item["blind_attestation"] != {
                "engine_output_seen": False,
                "reviewer_labels_seen": True,
                "legacy_conclusion_seen": False,
            }
            or not (
                max(_instant(response_a["submitted_at"]), _instant(response_b["submitted_at"]))
                <= _instant(submitted)
                <= _instant(request["evaluated_at"])
            )
        ):
            _fail("G103A_ADJUDICATIONS_INVALID")
        _validate_label(item["label"], task)
        _validate_adjudicator_delivery(
            item["delivery_receipt"], request, task, item, response_a, response_b, registry,
        )
        expected_payload = canonical_hash({
            key: value for key, value in item.items()
            if key not in {"adjudication_payload_hash", "signature"}
        })
        if item["adjudication_payload_hash"] != expected_payload:
            _fail("G103A_ADJUDICATION_HASH_INVALID")
        _validate_signature(
            item["signature"],
            object_hash=expected_payload,
            expected_role="ADJUDICATOR_C",
            expected_purpose=REVIEW_SIGNATURE_PURPOSE,
            expected_signed_at=submitted,
            registry=registry,
        )
        if item["signature"]["principal_id"] != item["reviewer_id"]:
            _fail("G103A_REVIEWER_IDENTITY_INVALID")
    return adjudications


def _validate_adjudicator_delivery(
    value: Any,
    request: Mapping[str, Any],
    task: Mapping[str, Any],
    adjudication: Mapping[str, Any],
    response_a: Mapping[str, Any],
    response_b: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> None:
    keys = {
        "schema_version", "delivery_id", "request_hash", "task_id",
        "blind_payload_hash", "recipient_reviewer_id", "recipient_role", "delivered_at",
        "visible_artifact_hashes", "delivery_payload_hash", "signature",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G103A_ADJUDICATOR_DELIVERY_INVALID")
    _identifier(value["delivery_id"], "G103A_ADJUDICATOR_DELIVERY_INVALID")
    delivered = _utc(value["delivered_at"], "G103A_ADJUDICATOR_DELIVERY_INVALID")
    expected_visible = [
        task["blind_payload_hash"],
        response_a["response_payload_hash"],
        response_b["response_payload_hash"],
    ]
    if (
        value["schema_version"] != DELIVERY_VERSION
        or value["request_hash"] != request["request_hash"]
        or value["task_id"] != task["task_id"]
        or value["blind_payload_hash"] != task["blind_payload_hash"]
        or value["recipient_reviewer_id"] != adjudication["reviewer_id"]
        or value["recipient_role"] != "ADJUDICATOR_C"
        or value["visible_artifact_hashes"] != expected_visible
        or not (
            max(_instant(response_a["submitted_at"]), _instant(response_b["submitted_at"]))
            <= _instant(delivered)
            <= _instant(adjudication["submitted_at"])
        )
    ):
        _fail("G103A_ADJUDICATOR_DELIVERY_INVALID")
    expected_payload = canonical_hash({
        key: item for key, item in value.items()
        if key not in {"delivery_payload_hash", "signature"}
    })
    if value["delivery_payload_hash"] != expected_payload:
        _fail("G103A_ADJUDICATOR_DELIVERY_HASH_INVALID")
    _validate_signature(
        value["signature"],
        object_hash=expected_payload,
        expected_role="BLINDING_CUSTODIAN",
        expected_purpose=DELIVERY_SIGNATURE_PURPOSE,
        expected_signed_at=delivered,
        registry=registry,
    )


def _validate_label(value: Any, task: Mapping[str, Any]) -> None:
    keys = {
        "expected_evaluation_result", "expected_decision", "action_proposals",
        "expected_reason_codes", "evidence_ref_ids", "critical_risk_labels",
        "label_hash",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G103A_LABEL_INVALID")
    result_contract = LABEL_CONTRACT["allowed_outcomes"].get(
        value.get("expected_evaluation_result")
    ) if isinstance(value, Mapping) else None
    decision_contract = result_contract.get(value.get("expected_decision")) \
        if isinstance(result_contract, Mapping) else None
    if (
        value["expected_evaluation_result"] not in EVALUATION_RESULTS
        or value["expected_decision"] not in DECISIONS
        or not isinstance(decision_contract, Mapping)
        or not isinstance(value["action_proposals"], list)
        or not value["action_proposals"]
        or len(value["action_proposals"]) > len(ACTION_PROPOSALS)
        or value["action_proposals"] != sorted(set(value["action_proposals"]))
        or any(item not in ACTION_PROPOSALS for item in value["action_proposals"])
        or value["action_proposals"] != decision_contract["action_proposals"]
        or ("NONE" in value["action_proposals"] and value["action_proposals"] != ["NONE"])
        or not isinstance(value["expected_reason_codes"], list)
        or not value["expected_reason_codes"]
        or len(value["expected_reason_codes"]) > 64
        or value["expected_reason_codes"] != sorted(set(value["expected_reason_codes"]))
        or any(not isinstance(item, str) or not _CODE_RE.fullmatch(item) for item in value["expected_reason_codes"])
        or not set(value["expected_reason_codes"]).issubset(decision_contract["reason_codes"])
        or not isinstance(value["critical_risk_labels"], list)
        or len(value["critical_risk_labels"]) > 64
        or value["critical_risk_labels"] != sorted(set(value["critical_risk_labels"]))
        or any(not isinstance(item, str) or not _CODE_RE.fullmatch(item) for item in value["critical_risk_labels"])
        or not set(value["critical_risk_labels"]).issubset(
            LABEL_CONTRACT["critical_risk_labels"]
        )
    ):
        _fail("G103A_LABEL_INVALID")
    facts = task["blind_payload"]["evidence_packet"]["facts"]
    evidence_ids = [item["fact_id"] for item in facts]
    primary_ids = {
        item["fact_id"] for item in facts
        if item["fact_kind"] == "PRIMARY_FROZEN_EVALUATION_AUDIT_FACT"
    }
    if (
        not isinstance(value["evidence_ref_ids"], list)
        or not value["evidence_ref_ids"]
        or value["evidence_ref_ids"] != sorted(set(value["evidence_ref_ids"]))
        or not set(value["evidence_ref_ids"]).issubset(evidence_ids)
        or not set(value["evidence_ref_ids"]).intersection(primary_ids)
    ):
        _fail("G103A_LABEL_EVIDENCE_INVALID")
    if value["label_hash"] != canonical_hash({
        key: item for key, item in value.items() if key != "label_hash"
    }):
        _fail("G103A_LABEL_HASH_INVALID")


def _label_projection(label: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in label.items() if key != "label_hash"}


def _validate_signature(
    value: Any,
    *,
    object_hash: str,
    expected_role: str,
    expected_purpose: str,
    expected_signed_at: str,
    registry: Mapping[str, Any],
) -> None:
    fields = {
        "algorithm", "key_id", "signer_id", "principal_id", "role", "purpose", "object_hash",
        "key_registry_hash", "signed_at", "signature_base64",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail("G103A_SIGNATURE_INVALID")
    keys = {item["key_id"]: item for item in registry["keys"]}
    key = keys.get(value["key_id"])
    signed_at = _utc(value["signed_at"], "G103A_SIGNATURE_INVALID")
    if (
        key is None
        or value["algorithm"] != SIGNATURE_ALGORITHM
        or value["signer_id"] != key["signer_id"]
        or value["principal_id"] != key["principal_id"]
        or value["role"] != expected_role
        or value["role"] != key["role"]
        or value["purpose"] != expected_purpose
        or value["purpose"] not in key["purposes"]
        or value["object_hash"] != object_hash
        or value["key_registry_hash"] != registry["registry_hash"]
        or signed_at != expected_signed_at
        or not (_instant(key["valid_from"]) <= _instant(signed_at) <= _instant(key["valid_until"]))
    ):
        _fail("G103A_SIGNATURE_INVALID")
    try:
        signature_bytes = base64.b64decode(value["signature_base64"], validate=True)
    except (TypeError, ValueError) as exc:
        raise GoldenLabelAdjudicationError("G103A_SIGNATURE_INVALID") from exc
    if not signature_bytes or not _verify_rsa_sha256(
        key["public_key_pem"],
        signature_bytes,
        signature_message(
            object_hash,
            key_registry_hash=registry["registry_hash"],
            key_id=value["key_id"],
            signer_id=value["signer_id"],
            principal_id=value["principal_id"],
            role=value["role"],
            purpose=value["purpose"],
        ),
    ):
        _fail("G103A_SIGNATURE_INVALID")


def _derive_ledger(
    request: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]],
    responses: Sequence[Mapping[str, Any]], adjudications: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    response_by_pair = {(item["task_id"], item["reviewer_role"]): item for item in responses}
    adjudication_by_task = {item["task_id"]: item for item in adjudications}
    ledger: list[dict[str, Any]] = []
    for task in tasks:
        response_a = response_by_pair.get((task["task_id"], "REVIEWER_A"))
        response_b = response_by_pair.get((task["task_id"], "REVIEWER_B"))
        adjudication = adjudication_by_task.get(task["task_id"])
        if response_a is None or response_b is None:
            if adjudication is not None:
                _fail("G103A_ADJUDICATION_WITHOUT_PAIR")
            status = "DOUBLE_REVIEW_PENDING"
            final_label = None
        elif _label_projection(response_a["label"]) == _label_projection(response_b["label"]):
            if adjudication is not None:
                _fail("G103A_UNNEEDED_ADJUDICATION")
            status = "PAIR_AGREED"
            final_label = response_a["label"]
        elif adjudication is None:
            status = "CONFLICT_PENDING_ADJUDICATION"
            final_label = None
        else:
            status = "ARBITRATED"
            final_label = adjudication["label"]
        item = {
            "schema_version": LEDGER_VERSION,
            "task_id": task["task_id"],
            "task_hash": task["task_hash"],
            "lineage_id": task["lineage_id"],
            "canonical_experiment_id": task["canonical_experiment_id"],
            "dataset_split": task["dataset_split"],
            "candidate_source_table": task["candidate_source_table"],
            "candidate_source_id": task["candidate_source_id"],
            "candidate_entry_hash": task["candidate_entry_hash"],
            "reviewer_a_response_hash": response_a["response_payload_hash"] if response_a else None,
            "reviewer_b_response_hash": response_b["response_payload_hash"] if response_b else None,
            "adjudication_hash": adjudication["adjudication_payload_hash"] if adjudication else None,
            "status": status,
            "resolved_label": final_label,
            "blindness_claim": "SIGNED_PROCESS_ATTESTATION_NOT_NONDISCLOSURE_PROOF",
            "snapshot_id": None,
            "not_golden_case": True,
        }
        item["fragment_hash"] = canonical_hash(item)
        ledger.append(item)
    return ledger


def _round_summary(
    request: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]],
    *, reviewer_key_registry_hash: str | None, status: str, reasons: Sequence[str],
) -> dict[str, Any]:
    counts = {
        key: sum(item["status"] == key for item in ledger)
        for key in (
            "DOUBLE_REVIEW_PENDING", "CONFLICT_PENDING_ADJUDICATION",
            "PAIR_AGREED", "ARBITRATED",
        )
    }
    round_value = {
        "schema_version": ROUND_VERSION,
        "review_round_id": request["review_round_id"],
        "request_hash": request["request_hash"],
        "reviewer_key_registry_hash": reviewer_key_registry_hash,
        "source_denominator": request["source_denominator"],
        "task_count": request["task_count"],
        "reviewer_packet_root_hash": request["reviewer_packet_root_hash"],
        "fragment_count": len(ledger),
        "status_counts": counts,
        "fragment_root_hash": canonical_hash([item["fragment_hash"] for item in ledger]),
        "status": status,
        "reason_codes": sorted(set(reasons)),
        "trust_status": (
            "SIGNED_BLIND_REVIEW_PROCESS_ATTESTATION"
            if status == "ASSIGNED_EVALUATION_SUBSET_LABELS_RESOLVED"
            else "NO_LABEL_EFFECT"
        ),
        "label_effect": (
            "ASSIGNED_EVALUATION_SUBSET_PACKET_FOR_ASSEMBLY_ONLY"
            if status == "ASSIGNED_EVALUATION_SUBSET_LABELS_RESOLVED"
            else "NONE"
        ),
        "holdout_status": HOLDOUT_STATUS,
        "not_golden_case": True,
        "golden_eligible": False,
        "replay_eligible": False,
        "gate1_effect": "NONE",
        "s02_03_effect": "NONE",
        "not_dataset_receipt": True,
        "not_replay_receipt": True,
        "not_gate_receipt": True,
    }
    round_value["round_hash"] = canonical_hash(round_value)
    return _validate_round(round_value, request, ledger)


def _validate_round(
    value: Mapping[str, Any], request: Mapping[str, Any], ledger: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    keys = {
        "schema_version", "review_round_id", "request_hash", "reviewer_key_registry_hash",
        "source_denominator", "task_count", "reviewer_packet_root_hash", "fragment_count",
        "status_counts", "fragment_root_hash", "status",
        "reason_codes", "trust_status", "label_effect", "holdout_status", "not_golden_case",
        "golden_eligible", "replay_eligible", "gate1_effect", "s02_03_effect", "not_dataset_receipt",
        "not_replay_receipt", "not_gate_receipt", "round_hash",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("G103A_ROUND_INVALID")
    result = dict(value)
    resolved = result["status"] == "ASSIGNED_EVALUATION_SUBSET_LABELS_RESOLVED"
    blocked = result["status"] == "BLOCKED_SOURCE_PARTITION"
    if (
        result["schema_version"] != ROUND_VERSION
        or result["review_round_id"] != request["review_round_id"]
        or result["request_hash"] != request["request_hash"]
        or result["source_denominator"] != request["source_denominator"]
        or result["task_count"] != request["task_count"]
        or result["reviewer_packet_root_hash"] != request["reviewer_packet_root_hash"]
        or result["fragment_count"] != len(ledger)
        or result["status"] not in {
            "BLOCKED_SOURCE_PARTITION", "BLIND_REVIEWS_PENDING",
            "ADJUDICATION_PENDING", "ASSIGNED_EVALUATION_SUBSET_LABELS_RESOLVED",
        }
        or result["holdout_status"] != HOLDOUT_STATUS
        or result["not_golden_case"] is not True
        or result["golden_eligible"] is not False
        or result["replay_eligible"] is not False
        or result["gate1_effect"] != "NONE"
        or result["s02_03_effect"] != "NONE"
        or result["not_dataset_receipt"] is not True
        or result["not_replay_receipt"] is not True
        or result["not_gate_receipt"] is not True
        or (result["reviewer_key_registry_hash"] is None) != blocked
        or (result["trust_status"] == "SIGNED_BLIND_REVIEW_PROCESS_ATTESTATION") != resolved
        or (
            result["label_effect"]
            == "ASSIGNED_EVALUATION_SUBSET_PACKET_FOR_ASSEMBLY_ONLY"
        ) != resolved
    ):
        _fail("G103A_ROUND_INVALID")
    if result["reviewer_key_registry_hash"] is not None:
        _hash(result["reviewer_key_registry_hash"], "G103A_ROUND_INVALID")
    _hash(result["fragment_root_hash"], "G103A_ROUND_INVALID")
    _hash(result["reviewer_packet_root_hash"], "G103A_ROUND_INVALID")
    if result["round_hash"] != canonical_hash({
        key: item for key, item in result.items() if key != "round_hash"
    }):
        _fail("G103A_ROUND_HASH_INVALID")
    return result


def _manifest_projection(
    request: Mapping[str, Any], round_summary: Mapping[str, Any],
    *, expected_reviewer_key_registry_hash: str | None, files: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = {
        "schema_version": MANIFEST_VERSION,
        "review_round_id": request["review_round_id"],
        "request_hash": request["request_hash"],
        "round_hash": round_summary["round_hash"],
        "source_binding": request["source_binding"],
        "source_denominator": request["source_denominator"],
        "label_contract_hash": request["label_contract_hash"],
        "reviewer_binding": request["reviewer_binding"],
        "blinding_binding": request["blinding_binding"],
        "expected_reviewer_key_registry_hash": expected_reviewer_key_registry_hash,
        "status": round_summary["status"],
        "trust_status": round_summary["trust_status"],
        "task_count": request["task_count"],
        "reviewer_packet_root_hash": request["reviewer_packet_root_hash"],
        "fragment_count": round_summary["fragment_count"],
        "label_effect": round_summary["label_effect"],
        "holdout_status": HOLDOUT_STATUS,
        "not_golden_case": True,
        "golden_eligible": False,
        "replay_eligible": False,
        "gate1_effect": "NONE",
        "s02_03_effect": "NONE",
        "not_dataset_receipt": True,
        "not_replay_receipt": True,
        "not_gate_receipt": True,
        "files": dict(files),
    }
    manifest["manifest_hash"] = canonical_hash(manifest)
    return manifest


def _validate_manifest_files(manifest: Mapping[str, Any], raw: Mapping[str, bytes]) -> None:
    keys = {
        "schema_version", "review_round_id", "request_hash", "round_hash", "source_binding",
        "source_denominator", "label_contract_hash", "reviewer_binding", "blinding_binding",
        "expected_reviewer_key_registry_hash", "status", "trust_status", "task_count",
        "fragment_count", "reviewer_packet_root_hash", "label_effect", "holdout_status",
        "not_golden_case",
        "golden_eligible", "replay_eligible", "gate1_effect", "s02_03_effect", "not_dataset_receipt",
        "not_replay_receipt", "not_gate_receipt", "files", "manifest_hash",
    }
    if not isinstance(manifest, Mapping) or set(manifest) != keys:
        _fail("G103A_MANIFEST_INVALID")
    payload_names = EXACT_FILES - {"manifest.json"}
    if (
        manifest["schema_version"] != MANIFEST_VERSION
        or manifest["s02_03_effect"] != "NONE"
        or not isinstance(manifest["files"], Mapping)
        or set(manifest["files"]) != payload_names
        or manifest["manifest_hash"] != canonical_hash({
            key: item for key, item in manifest.items() if key != "manifest_hash"
        })
    ):
        _fail("G103A_MANIFEST_INVALID")
    for name in payload_names:
        descriptor = manifest["files"][name]
        if not isinstance(descriptor, Mapping) or set(descriptor) != {"sha256", "size_bytes"}:
            _fail("G103A_MANIFEST_FILES_INVALID")
        _hash(descriptor["sha256"], "G103A_MANIFEST_FILES_INVALID")
        if (
            type(descriptor["size_bytes"]) is not int
            or descriptor["size_bytes"] < 0
            or descriptor["size_bytes"] != len(raw[name])
            or descriptor["sha256"] != hashlib.sha256(raw[name]).hexdigest()
        ):
            _fail("G103A_FILE_INTEGRITY_MISMATCH")


def _write_artifact_directory(
    root: Path, payloads: Mapping[str, bytes], *, exact_files: frozenset[str],
) -> None:
    if set(payloads) != exact_files or not root.name or root.name in {".", ".."}:
        _fail("G103A_OUTPUT_INVALID")
    for raw in payloads.values():
        if len(raw) > MAX_ARTIFACT_FILE_BYTES:
            _fail("G103A_ARTIFACT_TOO_LARGE")
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(root.parent, parent_flags)
    except OSError as exc:
        raise GoldenLabelAdjudicationError("G103A_OUTPUT_PARENT_INVALID") from exc
    final_fd: int | None = None
    complete = False
    try:
        try:
            os.mkdir(root.name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            _fail("G103A_OUTPUT_EXISTS")
        final_fd = os.open(root.name, parent_flags, dir_fd=parent_fd)
        os.fchmod(final_fd, 0o700)
        _require_directory_identity(parent_fd, root.name, final_fd)
        for name in sorted(payloads):
            _write_exclusive_at(final_fd, name, payloads[name])
        if set(os.listdir(final_fd)) != exact_files:
            _fail("G103A_OUTPUT_FILE_SET_INVALID")
        os.fsync(final_fd)
        _require_directory_identity(parent_fd, root.name, final_fd)
        complete = True
        os.fsync(parent_fd)
    except Exception as exc:
        if complete:
            raise GoldenLabelAdjudicationError("G103A_OUTPUT_DURABILITY_UNCERTAIN") from exc
        if final_fd is not None:
            for name in exact_files:
                try:
                    os.unlink(name, dir_fd=final_fd)
                except FileNotFoundError:
                    pass
            try:
                os.fsync(final_fd)
            except OSError:
                pass
            try:
                _require_directory_identity(parent_fd, root.name, final_fd)
                os.rmdir(root.name, dir_fd=parent_fd)
            except (FileNotFoundError, OSError):
                pass
        raise
    finally:
        if final_fd is not None:
            os.close(final_fd)
        os.close(parent_fd)


def _write_exclusive_at(directory_fd: int, name: str, raw: bytes) -> None:
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
                _fail("G103A_ARTIFACT_WRITE_FAILED")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(name, 0o600, dir_fd=directory_fd, follow_symlinks=False)


def _require_directory_identity(parent_fd: int, name: str, opened_fd: int) -> None:
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    opened = os.fstat(opened_fd)
    if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
        _fail("G103A_OUTPUT_DIRECTORY_CHANGED")


def _read_artifact_directory(
    root: Path, *, exact_files: frozenset[str],
) -> dict[str, bytes]:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        root_fd = os.open(root, directory_flags)
    except OSError as exc:
        raise GoldenLabelAdjudicationError("G103A_ARTIFACT_DIRECTORY_INVALID") from exc
    try:
        root_before = os.fstat(root_fd)
        if not stat.S_ISDIR(root_before.st_mode) or stat.S_IMODE(root_before.st_mode) != 0o700:
            _fail("G103A_ARTIFACT_MODE_INVALID")
        if set(os.listdir(root_fd)) != exact_files:
            _fail("G103A_ARTIFACT_FILE_SET_INVALID")
        raw: dict[str, bytes] = {}
        for name in sorted(exact_files):
            try:
                fd = os.open(name, file_flags, dir_fd=root_fd)
            except OSError as exc:
                raise GoldenLabelAdjudicationError("G103A_ARTIFACT_FILE_INVALID") from exc
            try:
                before = os.fstat(fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_nlink != 1
                    or before.st_size > MAX_ARTIFACT_FILE_BYTES
                ):
                    _fail("G103A_ARTIFACT_FILE_INVALID")
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(fd, min(65536, MAX_ARTIFACT_FILE_BYTES + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARTIFACT_FILE_BYTES:
                        _fail("G103A_ARTIFACT_TOO_LARGE")
                    chunks.append(chunk)
                after = os.fstat(fd)
                before_identity = (
                    before.st_dev, before.st_ino, before.st_mode, before.st_size,
                    before.st_mtime_ns, before.st_ctime_ns,
                )
                after_identity = (
                    after.st_dev, after.st_ino, after.st_mode, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns,
                )
                if before_identity != after_identity or total != after.st_size:
                    _fail("G103A_ARTIFACT_CHANGED_DURING_READ")
                raw[name] = b"".join(chunks)
            finally:
                os.close(fd)
        root_after = os.fstat(root_fd)
        before_dir = (
            root_before.st_dev, root_before.st_ino, root_before.st_mode,
            root_before.st_mtime_ns, root_before.st_ctime_ns,
        )
        after_dir = (
            root_after.st_dev, root_after.st_ino, root_after.st_mode,
            root_after.st_mtime_ns, root_after.st_ctime_ns,
        )
        if set(os.listdir(root_fd)) != exact_files or before_dir != after_dir:
            _fail("G103A_ARTIFACT_CHANGED_DURING_READ")
        return raw
    finally:
        os.close(root_fd)


def _json_document(raw: bytes, code: str) -> Any:
    try:
        return _canonical_json_document(raw, code)
    except HistoricalLineageCandidateError as exc:
        raise GoldenLabelAdjudicationError(str(exc)) from exc


def _ndjson_document(raw: bytes, code: str) -> list[dict[str, Any]]:
    if not raw:
        return []
    if not raw.endswith(b"\n") or b"\n\n" in raw:
        _fail(code)
    items: list[dict[str, Any]] = []
    for line in raw.splitlines(keepends=True):
        value = _json_document(line, code)
        if not isinstance(value, Mapping):
            _fail(code)
        items.append(dict(value))
    return items


def _json_bytes(value: Any) -> bytes:
    return (canonical_json(value) + "\n").encode()


def _ndjson(items: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(_json_bytes(dict(item)) for item in items)


def _descriptors(payloads: Mapping[str, bytes]) -> dict[str, dict[str, Any]]:
    descriptors: dict[str, dict[str, Any]] = {}
    for name, raw in payloads.items():
        if len(raw) > MAX_ARTIFACT_FILE_BYTES:
            _fail("G103A_ARTIFACT_TOO_LARGE")
        descriptors[name] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
    return descriptors


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        _fail(code)
    return value


def _hash(value: Any, code: str) -> str:
    try:
        return validate_sha256(value, code=code)
    except ValueError as exc:
        raise GoldenLabelAdjudicationError(str(exc)) from exc


def _utc(value: Any, code: str) -> str:
    try:
        result = validate_utc(value, code=code)
    except ValueError as exc:
        raise GoldenLabelAdjudicationError(str(exc)) from exc
    assert isinstance(result, str)
    return result


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _raise(code: str) -> Any:
    _fail(code)
