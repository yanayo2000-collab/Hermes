from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.growth.canonical_evaluation_contracts_v2 import (
    BUNDLE_VERSION_V2,
    CEILING as CONTRACT_CEILING_V2,
    CanonicalEvaluationContractV2Error,
    canonical_hash,
    canonical_json,
    validate_canonical_input_bundle_v2,
    validate_sha256,
    validate_utc,
)


ADAPTER_VERSION = "gle-e04-s04-01a2-frozen-replay-input-adapter-v2"
ENVELOPE_VERSION = "gle-e04-s04-01a2-replay-input-envelope-v2"
MANIFEST_VERSION = "gle-e04-s04-01a2-replay-input-manifest-v2"

BUNDLE_FILE = "canonical-input-bundle.json"
ENVELOPE_FILE = "replay-input-envelope.json"
MANIFEST_FILE = "manifest.json"
EXACT_FILES = frozenset({BUNDLE_FILE, ENVELOPE_FILE, MANIFEST_FILE})

MAX_ARTIFACT_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 6 * 1024 * 1024

ADAPTER_LAYER_CEILING = {
    "input_effect": "INPUT_ADAPTER_ONLY",
    "authority_reference_content_status": "NOT_OPENED_NOT_VERIFIED",
    "evaluator_implementation_content_status": "IDENTIFIER_BOUND_IMPLEMENTATION_NOT_OPENED",
    "policy_implementation_content_status": "IDENTIFIER_BOUND_IMPLEMENTATION_NOT_OPENED",
    "assignment_mechanism_content_status": "IDENTIFIER_BOUND_CAPABILITY_NOT_OPENED",
    "capability_assessment_content_status": "NOT_OPENED_NOT_VERIFIED",
    "allocation_effect": "NONE",
}
ADAPTER_CEILING = {**CONTRACT_CEILING_V2, **ADAPTER_LAYER_CEILING}

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class FrozenReplayInputV2Error(ValueError):
    pass


def _fail(code: str) -> None:
    raise FrozenReplayInputV2Error(code)


def read_anchored_canonical_bundle_v2(
    path: str | Path,
    *,
    expected_sha256: str,
) -> tuple[dict[str, Any], bytes]:
    expected = _sha(expected_sha256, "G104A2_BUNDLE_ANCHOR_INVALID")
    raw = _read_named_file(
        Path(path),
        max_bytes=MAX_ARTIFACT_FILE_BYTES,
        required_mode=0o600,
        invalid_code="G104A2_BUNDLE_FILE_INVALID",
        changed_code="G104A2_BUNDLE_CHANGED_DURING_READ",
    )
    if hashlib.sha256(raw).hexdigest() != expected:
        _fail("G104A2_BUNDLE_ANCHOR_MISMATCH")
    value = _json_document(raw, "G104A2_BUNDLE_JSON_INVALID")
    if not isinstance(value, Mapping):
        _fail("G104A2_BUNDLE_JSON_INVALID")
    bundle = _validate_bundle(value)
    return bundle, raw


def derive_replay_input_envelope_v2(
    bundle: Mapping[str, Any],
    *,
    bundle_raw_sha256: str,
    replay_input_id: str,
    requested_split: str,
    synthetic_clock_at: str,
) -> dict[str, Any]:
    if not isinstance(replay_input_id, str) or not _ID_RE.fullmatch(replay_input_id):
        _fail("G104A2_REPLAY_INPUT_ID_INVALID")
    if not isinstance(requested_split, str) or requested_split not in {"DEV", "VALIDATION"}:
        _fail("G104A2_SPLIT_INVALID")
    raw_sha = _sha(bundle_raw_sha256, "G104A2_BUNDLE_ANCHOR_INVALID")
    clock = _utc(synthetic_clock_at, "G104A2_SYNTHETIC_CLOCK_INVALID")
    validated = _validate_bundle(bundle)
    if hashlib.sha256(_json_bytes(validated)).hexdigest() != raw_sha:
        _fail("G104A2_BUNDLE_ANCHOR_MISMATCH")
    _validate_adapter_profile(validated)

    objective = validated["objective_contract"]
    invariant = validated["invariant_projection"]
    spec = validated["experiment_spec"]
    snapshot = validated["input_snapshot"]
    if _instant(clock) < _instant(snapshot["created_at"]):
        _fail("G104A2_SYNTHETIC_CLOCK_BEFORE_SNAPSHOT")

    object_hashes = {
        "objective_contract_hash": objective["contract_hash"],
        "invariant_projection_hash": invariant["projection_hash"],
        "experiment_spec_hash": spec["spec_hash"],
        "input_snapshot_hash": snapshot["snapshot_hash"],
    }
    primary = objective["primary_metric"]
    version_binding = {
        "bundle_schema_version": validated["schema_version"],
        "objective_schema_version": objective["schema_version"],
        "invariant_schema_version": invariant["schema_version"],
        "spec_schema_version": spec["schema_version"],
        "snapshot_schema_version": snapshot["schema_version"],
        "evaluator_version": snapshot["evaluator_version"],
        "policy_version": snapshot["policy_version"],
        "metric_definition_version": snapshot["metric_definition_version"],
        "metric_contract_hash": snapshot["metric_contract_hash"],
        "attribution_version": snapshot["attribution_version"],
        "attribution_window": snapshot["attribution_window"],
        "dedup_version": snapshot["dedup_version"],
        "qualification_rule_version": snapshot["qualification_rule_version"],
    }
    if version_binding["metric_definition_version"] != primary["definition_version"]:
        _fail("G104A2_VERSION_BINDING_INVALID")

    input_root = canonical_hash({
        "domain": "GLE_E04_S04_01A2_FROZEN_REPLAY_INPUT_V2",
        "adapter_version": ADAPTER_VERSION,
        "replay_input_id": replay_input_id,
        "requested_split": requested_split,
        "requested_split_effect": "REQUESTED_CONTEXT_ONLY",
        "synthetic_clock_at": clock,
        "bundle_raw_sha256": raw_sha,
        "bundle_hash": validated["bundle_hash"],
        "object_hashes": object_hashes,
        "version_binding": version_binding,
        "lineage_id": spec["lineage_id"],
        "experiment_id": spec["experiment_id"],
        "snapshot_id": snapshot["snapshot_id"],
        "data_cutoff_at": snapshot["data_cutoff_at"],
        "status": "SYNTHETIC_AUTHORITY_CANDIDATE_CONTRACT_FIXTURE_ONLY",
        "trust_status": "UNSIGNED_LOCAL_SYNTHETIC_FIXTURE",
        "spec_shape": "AUTHORITY_CANDIDATE_UNVERIFIED",
        "adapter_ceiling": ADAPTER_CEILING,
    })
    envelope: dict[str, Any] = {
        "schema_version": ENVELOPE_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "replay_input_id": replay_input_id,
        "requested_split": requested_split,
        "requested_split_effect": "REQUESTED_CONTEXT_ONLY",
        "synthetic_clock_at": clock,
        "bundle_raw_sha256": raw_sha,
        "bundle_hash": validated["bundle_hash"],
        "object_hashes": object_hashes,
        "version_binding": version_binding,
        "lineage_id": spec["lineage_id"],
        "experiment_id": spec["experiment_id"],
        "snapshot_id": snapshot["snapshot_id"],
        "data_cutoff_at": snapshot["data_cutoff_at"],
        "snapshot_created_at": snapshot["created_at"],
        "spec_shape": "AUTHORITY_CANDIDATE_UNVERIFIED",
        "authority_reference_content_status": "NOT_OPENED_NOT_VERIFIED",
        "evaluator_implementation_content_status": ADAPTER_CEILING[
            "evaluator_implementation_content_status"
        ],
        "policy_implementation_content_status": ADAPTER_CEILING[
            "policy_implementation_content_status"
        ],
        "assignment_mechanism_content_status": ADAPTER_CEILING[
            "assignment_mechanism_content_status"
        ],
        "capability_assessment_content_status": ADAPTER_CEILING[
            "capability_assessment_content_status"
        ],
        "status": "SYNTHETIC_AUTHORITY_CANDIDATE_CONTRACT_FIXTURE_ONLY",
        "reason_codes": [
            "CALLER_ASSERTED_AUTHORITY_CANDIDATE_SHAPE_ONLY",
            "AUTHORITY_REFERENCE_CONTENT_NOT_OPENED",
            "EVALUATOR_IMPLEMENTATION_NOT_OPENED",
            "POLICY_IMPLEMENTATION_NOT_OPENED",
            "METRIC_CONTRACT_CONTENT_NOT_OPENED",
            "ASSIGNMENT_CAPABILITY_NOT_OPENED",
            "SOURCE_CONTENT_NOT_AUTHORITY_VERIFIED",
            "REQUESTED_SPLIT_NOT_PARTITION_ASSIGNMENT",
        ],
        "trust_status": "UNSIGNED_LOCAL_SYNTHETIC_FIXTURE",
        "validation_ceiling": dict(ADAPTER_CEILING),
        "input_root": input_root,
        "envelope_hash": "",
    }
    envelope["envelope_hash"] = canonical_hash({
        key: value for key, value in envelope.items() if key != "envelope_hash"
    })
    return envelope


def validate_replay_input_envelope_v2(
    envelope: Mapping[str, Any],
    bundle: Mapping[str, Any],
    *,
    bundle_raw_sha256: str,
) -> dict[str, Any]:
    if not isinstance(envelope, Mapping):
        _fail("G104A2_ENVELOPE_INVALID")
    expected_keys = {
        "schema_version", "adapter_version", "replay_input_id", "requested_split",
        "requested_split_effect", "synthetic_clock_at", "bundle_raw_sha256", "bundle_hash",
        "object_hashes", "version_binding", "lineage_id", "experiment_id", "snapshot_id",
        "data_cutoff_at", "snapshot_created_at", "spec_shape",
        "authority_reference_content_status", "evaluator_implementation_content_status",
        "policy_implementation_content_status", "assignment_mechanism_content_status",
        "capability_assessment_content_status", "status", "reason_codes", "trust_status",
        "validation_ceiling", "input_root", "envelope_hash",
    }
    if set(envelope) != expected_keys:
        _fail("G104A2_ENVELOPE_INVALID")
    expected = derive_replay_input_envelope_v2(
        bundle,
        bundle_raw_sha256=bundle_raw_sha256,
        replay_input_id=envelope["replay_input_id"],
        requested_split=envelope["requested_split"],
        synthetic_clock_at=envelope["synthetic_clock_at"],
    )
    if dict(envelope) != expected:
        _fail("G104A2_ENVELOPE_DERIVATION_MISMATCH")
    return expected


def write_frozen_replay_input_v2_artifact(
    output_dir: str | Path,
    bundle_path: str | Path,
    *,
    expected_bundle_sha256: str,
    replay_input_id: str,
    requested_split: str,
    synthetic_clock_at: str,
) -> dict[str, Any]:
    bundle, bundle_raw = read_anchored_canonical_bundle_v2(
        bundle_path,
        expected_sha256=expected_bundle_sha256,
    )
    bundle_raw_sha256 = hashlib.sha256(bundle_raw).hexdigest()
    envelope = derive_replay_input_envelope_v2(
        bundle,
        bundle_raw_sha256=bundle_raw_sha256,
        replay_input_id=replay_input_id,
        requested_split=requested_split,
        synthetic_clock_at=synthetic_clock_at,
    )
    payloads = {
        BUNDLE_FILE: bundle_raw,
        ENVELOPE_FILE: _json_bytes(envelope),
    }
    manifest = _manifest(envelope, _descriptors(payloads))
    payloads[MANIFEST_FILE] = _json_bytes(manifest)
    _write_artifact_directory(Path(output_dir), payloads)

    loaded_raw = _read_artifact_directory(Path(output_dir))
    loaded = load_validated_frozen_replay_input_v2_directory(
        output_dir,
        expected_manifest_sha256=hashlib.sha256(loaded_raw[MANIFEST_FILE]).hexdigest(),
    )
    if loaded["manifest"] != manifest:
        _fail("G104A2_WRITE_READBACK_MISMATCH")
    return manifest


def load_validated_frozen_replay_input_v2_directory(
    input_dir: str | Path,
    *,
    expected_manifest_sha256: str,
) -> dict[str, Any]:
    expected_sha = _sha(expected_manifest_sha256, "G104A2_MANIFEST_ANCHOR_INVALID")
    raw = _read_artifact_directory(Path(input_dir))
    manifest_raw_sha256 = hashlib.sha256(raw[MANIFEST_FILE]).hexdigest()
    if manifest_raw_sha256 != expected_sha:
        _fail("G104A2_MANIFEST_ANCHOR_MISMATCH")
    bundle_value = _json_document(raw[BUNDLE_FILE], "G104A2_ARTIFACT_JSON_INVALID")
    envelope_value = _json_document(raw[ENVELOPE_FILE], "G104A2_ARTIFACT_JSON_INVALID")
    manifest_value = _json_document(raw[MANIFEST_FILE], "G104A2_ARTIFACT_JSON_INVALID")
    if not all(isinstance(value, Mapping) for value in (bundle_value, envelope_value, manifest_value)):
        _fail("G104A2_ARTIFACT_JSON_INVALID")
    bundle = _validate_bundle(bundle_value)
    bundle_raw_sha256 = hashlib.sha256(raw[BUNDLE_FILE]).hexdigest()
    envelope = validate_replay_input_envelope_v2(
        envelope_value,
        bundle,
        bundle_raw_sha256=bundle_raw_sha256,
    )
    _validate_manifest(manifest_value, raw, envelope)
    expected_manifest = _manifest(
        envelope,
        {
            name: {
                "sha256": hashlib.sha256(raw[name]).hexdigest(),
                "size_bytes": len(raw[name]),
            }
            for name in EXACT_FILES - {MANIFEST_FILE}
        },
    )
    if dict(manifest_value) != expected_manifest:
        _fail("G104A2_MANIFEST_DERIVATION_MISMATCH")
    return {
        "bundle": bundle,
        "envelope": envelope,
        "manifest": expected_manifest,
        "manifest_raw_sha256": manifest_raw_sha256,
    }


def _validate_bundle(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return validate_canonical_input_bundle_v2(value)
    except CanonicalEvaluationContractV2Error as exc:
        raise FrozenReplayInputV2Error(str(exc)) from exc


def _validate_adapter_profile(bundle: Mapping[str, Any]) -> None:
    if bundle["schema_version"] != BUNDLE_VERSION_V2 or bundle["validation_ceiling"] != CONTRACT_CEILING_V2:
        _fail("G104A2_BUNDLE_CEILING_INVALID")
    spec = bundle["experiment_spec"]
    snapshot = bundle["input_snapshot"]
    evaluation = spec["evaluation_plan"]
    assignment = spec["assignment"]
    if (
        spec["status"] != "AUTHORITY_CANDIDATE"
        or spec["approved_at"] is not None
        or spec["authority_attestation_ref"] is None
        or spec["authority_validation_status"] != "UNVERIFIED_REFERENCE_ONLY"
        or spec["authority_effect"] != "NONE"
        or evaluation["method_version"].upper() == "UNFROZEN"
        or evaluation["policy_version"].upper() == "UNFROZEN"
    ):
        _fail("G104A2_SPEC_SHAPE_CEILING_INVALID")
    if assignment["readback_evidence_sha256"] is not None:
        _fail("G104A2_SYNTHETIC_ALLOCATION_INVALID")
    for cell in spec["cells"]:
        if (
            cell["actual_allocation"] is not None
            or cell["allocation_verified_at"] is not None
            or any(cell[field] is not None for field in (
                "meta_campaign_id", "meta_adset_id", "meta_creative_id", "meta_ad_id",
                "meta_assignment_cell_id",
            ))
        ):
            _fail("G104A2_SYNTHETIC_ALLOCATION_INVALID")
    if (
        snapshot["allocation_basis"] != "SYNTHETIC_TARGET_FIXTURE"
        or snapshot["allocation_readback_evidence_sha256"] is not None
        or snapshot["allocation_verified_at"] is not None
        or snapshot["source_validation_status"] != "SYNTHETIC_FIXTURE_ONLY"
        or snapshot["snapshot_effect"] != "NONE"
    ):
        _fail("G104A2_SYNTHETIC_SNAPSHOT_INVALID")
    if snapshot["mutation_events"] != []:
        _fail("G104A2_SYNTHETIC_MUTATION_INVALID")
    targets = assignment["target_allocation"]
    for cell_id, target in targets.items():
        observed = snapshot["cell_metrics"][cell_id]["allocation_share"]
        if float(observed) != float(target):
            _fail("G104A2_SYNTHETIC_ALLOCATION_INVALID")


def _manifest(envelope: Mapping[str, Any], files: Mapping[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": MANIFEST_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "replay_input_id": envelope["replay_input_id"],
        "requested_split": envelope["requested_split"],
        "requested_split_effect": envelope["requested_split_effect"],
        "bundle_hash": envelope["bundle_hash"],
        "input_root": envelope["input_root"],
        "envelope_hash": envelope["envelope_hash"],
        "status": envelope["status"],
        "trust_status": envelope["trust_status"],
        "validation_ceiling": dict(ADAPTER_CEILING),
        "files": dict(files),
        "manifest_hash": "",
    }
    value["manifest_hash"] = canonical_hash({
        key: item for key, item in value.items() if key != "manifest_hash"
    })
    return value


def _validate_manifest(
    manifest: Mapping[str, Any],
    raw: Mapping[str, bytes],
    envelope: Mapping[str, Any],
) -> None:
    expected_keys = set(_manifest(envelope, {}).keys())
    if set(manifest) != expected_keys:
        _fail("G104A2_MANIFEST_INVALID")
    files = manifest["files"]
    if not isinstance(files, Mapping) or set(files) != EXACT_FILES - {MANIFEST_FILE}:
        _fail("G104A2_MANIFEST_INVALID")
    if manifest["manifest_hash"] != canonical_hash({
        key: value for key, value in manifest.items() if key != "manifest_hash"
    }):
        _fail("G104A2_MANIFEST_HASH_MISMATCH")
    for name, descriptor in files.items():
        if not isinstance(descriptor, Mapping) or set(descriptor) != {"sha256", "size_bytes"}:
            _fail("G104A2_MANIFEST_FILE_INVALID")
        _sha(descriptor["sha256"], "G104A2_MANIFEST_FILE_INVALID")
        if (
            type(descriptor["size_bytes"]) is not int
            or descriptor["size_bytes"] != len(raw[name])
            or descriptor["sha256"] != hashlib.sha256(raw[name]).hexdigest()
        ):
            _fail("G104A2_FILE_INTEGRITY_MISMATCH")


def _descriptors(payloads: Mapping[str, bytes]) -> dict[str, dict[str, Any]]:
    return {
        name: {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
        for name, raw in payloads.items()
    }


def _write_artifact_directory(root: Path, payloads: Mapping[str, bytes]) -> None:
    if set(payloads) != EXACT_FILES or not root.name or root.name in {".", ".."}:
        _fail("G104A2_OUTPUT_INVALID")
    if (
        any(not raw or len(raw) > MAX_ARTIFACT_FILE_BYTES for raw in payloads.values())
        or sum(map(len, payloads.values())) > MAX_TOTAL_ARTIFACT_BYTES
    ):
        _fail("G104A2_ARTIFACT_TOO_LARGE")
    parent = _open_parent(root, "G104A2_OUTPUT_PARENT_INVALID")
    parent_fd, grand_fd, parent_name, parent_before = parent
    root_fd: int | None = None
    complete = False
    try:
        try:
            os.mkdir(root.name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            _fail("G104A2_OUTPUT_EXISTS")
        root_fd = os.open(root.name, _directory_flags(), dir_fd=parent_fd)
        os.fchmod(root_fd, 0o700)
        _require_named_directory(parent_fd, root.name, root_fd, "G104A2_OUTPUT_DIRECTORY_CHANGED")
        for name in (BUNDLE_FILE, ENVELOPE_FILE, MANIFEST_FILE):
            _write_file_at(root_fd, name, payloads[name])
        if _bounded_directory_names(root_fd, "G104A2_OUTPUT_FILE_SET_INVALID") != EXACT_FILES:
            _fail("G104A2_OUTPUT_FILE_SET_INVALID")
        os.fsync(root_fd)
        _require_named_directory(parent_fd, root.name, root_fd, "G104A2_OUTPUT_DIRECTORY_CHANGED")
        _require_parent_binding_identity(
            grand_fd,
            parent_name,
            parent_fd,
            parent_before,
            "G104A2_OUTPUT_PARENT_CHANGED",
        )
        complete = True
        os.fsync(parent_fd)
    except Exception as exc:
        if complete:
            raise FrozenReplayInputV2Error("G104A2_OUTPUT_DURABILITY_UNCERTAIN") from exc
        if root_fd is not None:
            for name in EXACT_FILES:
                try:
                    os.unlink(name, dir_fd=root_fd)
                except OSError:
                    pass
            try:
                _require_named_directory(parent_fd, root.name, root_fd, "G104A2_OUTPUT_DIRECTORY_CHANGED")
                os.rmdir(root.name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)
        os.close(grand_fd)


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
                _fail("G104A2_WRITE_FAILED")
            offset += written
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)


def _bounded_directory_names(directory_fd: int, code: str) -> frozenset[str]:
    names: list[str] = []
    try:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > len(EXACT_FILES):
                    _fail(code)
    except FrozenReplayInputV2Error:
        raise
    except OSError as exc:
        raise FrozenReplayInputV2Error(code) from exc
    return frozenset(names)


def _read_artifact_directory(root: Path) -> dict[str, bytes]:
    parent_fd, grand_fd, parent_name, parent_before = _open_parent(
        root, "G104A2_ARTIFACT_PARENT_INVALID"
    )
    root_fd: int | None = None
    try:
        try:
            root_fd = os.open(root.name, _directory_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise FrozenReplayInputV2Error("G104A2_ARTIFACT_DIRECTORY_INVALID") from exc
        before_dir = os.fstat(root_fd)
        named_dir = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(before_dir.st_mode)
            or stat.S_IMODE(before_dir.st_mode) != 0o700
            or _directory_identity(before_dir) != _directory_identity(named_dir)
            or _bounded_directory_names(
                root_fd, "G104A2_ARTIFACT_DIRECTORY_INVALID"
            ) != EXACT_FILES
        ):
            _fail("G104A2_ARTIFACT_DIRECTORY_INVALID")
        raw: dict[str, bytes] = {}
        total = 0
        for name in sorted(EXACT_FILES):
            try:
                fd = os.open(name, _file_flags(), dir_fd=root_fd)
            except OSError as exc:
                raise FrozenReplayInputV2Error("G104A2_ARTIFACT_FILE_INVALID") from exc
            try:
                before = os.fstat(fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_nlink != 1
                    or before.st_size <= 0
                    or before.st_size > MAX_ARTIFACT_FILE_BYTES
                ):
                    _fail("G104A2_ARTIFACT_FILE_INVALID")
                chunks: list[bytes] = []
                consumed = 0
                while True:
                    chunk = os.read(fd, min(65536, MAX_ARTIFACT_FILE_BYTES + 1 - consumed))
                    if not chunk:
                        break
                    consumed += len(chunk)
                    total += len(chunk)
                    if consumed > MAX_ARTIFACT_FILE_BYTES or total > MAX_TOTAL_ARTIFACT_BYTES:
                        _fail("G104A2_ARTIFACT_TOO_LARGE")
                    chunks.append(chunk)
                after = os.fstat(fd)
                named = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if (
                    _file_identity(before) != _file_identity(after)
                    or _file_identity(after) != _file_identity(named)
                    or consumed != after.st_size
                ):
                    _fail("G104A2_ARTIFACT_CHANGED_DURING_READ")
                raw[name] = b"".join(chunks)
            finally:
                os.close(fd)
        after_dir = os.fstat(root_fd)
        named_after = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _bounded_directory_names(
                root_fd, "G104A2_ARTIFACT_CHANGED_DURING_READ"
            ) != EXACT_FILES
            or _directory_identity(before_dir) != _directory_identity(after_dir)
            or _directory_identity(after_dir) != _directory_identity(named_after)
        ):
            _fail("G104A2_ARTIFACT_CHANGED_DURING_READ")
        _require_parent_identity(
            grand_fd, parent_name, parent_fd, parent_before, "G104A2_ARTIFACT_PARENT_CHANGED"
        )
        return raw
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)
        os.close(grand_fd)


def _read_named_file(
    path: Path,
    *,
    max_bytes: int,
    required_mode: int,
    invalid_code: str,
    changed_code: str,
) -> bytes:
    if not path.name or path.name in {".", ".."}:
        _fail(invalid_code)
    parent_fd, grand_fd, parent_name, parent_before = _open_parent(path, invalid_code)
    fd: int | None = None
    try:
        try:
            fd = os.open(path.name, _file_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise FrozenReplayInputV2Error(invalid_code) from exc
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != required_mode
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            _fail(invalid_code)
        chunks: list[bytes] = []
        consumed = 0
        while True:
            chunk = os.read(fd, min(65536, max_bytes + 1 - consumed))
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > max_bytes:
                _fail(invalid_code)
            chunks.append(chunk)
        after = os.fstat(fd)
        named_after = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _file_identity(before) != _file_identity(after)
            or _file_identity(after) != _file_identity(named_after)
            or consumed != after.st_size
        ):
            _fail(changed_code)
        _require_parent_identity(grand_fd, parent_name, parent_fd, parent_before, changed_code)
        return b"".join(chunks)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)
        os.close(grand_fd)


def _open_parent(path: Path, code: str) -> tuple[int, int, str, os.stat_result]:
    try:
        parent = path.parent.resolve(strict=True)
        grand_fd = os.open(parent.parent, _directory_flags())
        named = os.stat(parent.name, dir_fd=grand_fd, follow_symlinks=False)
        parent_fd = os.open(parent.name, _directory_flags(), dir_fd=grand_fd)
        opened = os.fstat(parent_fd)
    except OSError as exc:
        try:
            os.close(grand_fd)
        except (NameError, OSError):
            pass
        _fail(code)
    if not stat.S_ISDIR(opened.st_mode) or _directory_identity(named) != _directory_identity(opened):
        os.close(parent_fd)
        os.close(grand_fd)
        _fail(code)
    return parent_fd, grand_fd, parent.name, opened


def _require_parent_identity(
    grand_fd: int,
    parent_name: str,
    parent_fd: int,
    before: os.stat_result,
    code: str,
) -> None:
    try:
        opened = os.fstat(parent_fd)
        named = os.stat(parent_name, dir_fd=grand_fd, follow_symlinks=False)
    except OSError:
        _fail(code)
    if (
        _directory_identity(before) != _directory_identity(opened)
        or _directory_identity(opened) != _directory_identity(named)
    ):
        _fail(code)


def _require_parent_binding_identity(
    grand_fd: int,
    parent_name: str,
    parent_fd: int,
    before: os.stat_result,
    code: str,
) -> None:
    try:
        opened = os.fstat(parent_fd)
        named = os.stat(parent_name, dir_fd=grand_fd, follow_symlinks=False)
    except OSError:
        _fail(code)
    # Creating the artifact directory necessarily changes parent mtime, ctime,
    # and usually nlink.  The stable binding is the named parent inode + mode.
    if not (
        stat.S_ISDIR(opened.st_mode)
        and _directory_binding_identity(before) == _directory_binding_identity(opened)
        and _directory_binding_identity(opened) == _directory_binding_identity(named)
    ):
        _fail(code)


def _require_named_directory(parent_fd: int, name: str, opened_fd: int, code: str) -> None:
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(opened_fd)
    except OSError:
        _fail(code)
    if not stat.S_ISDIR(named.st_mode) or _directory_identity(named) != _directory_identity(opened):
        _fail(code)


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _file_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_nlink, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )


def _directory_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
        value.st_mtime_ns, value.st_ctime_ns,
    )


def _directory_binding_identity(value: os.stat_result) -> tuple[int, ...]:
    return value.st_dev, value.st_ino, value.st_mode


def _json_document(raw: bytes, code: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail(code)
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _: _fail(code),
        )
        canonical_raw = _json_bytes(value)
    except FrozenReplayInputV2Error:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
        TypeError,
        OverflowError,
    ):
        _fail(code)
    if raw != canonical_raw:
        _fail(code)
    return value


def _json_bytes(value: Any) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _sha(value: Any, code: str) -> str:
    try:
        return validate_sha256(value, code=code)
    except CanonicalEvaluationContractV2Error as exc:
        raise FrozenReplayInputV2Error(str(exc)) from exc


def _utc(value: Any, code: str) -> str:
    try:
        result = validate_utc(value, code=code)
    except CanonicalEvaluationContractV2Error as exc:
        raise FrozenReplayInputV2Error(str(exc)) from exc
    if result is None:
        _fail(code)
    return result


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    return parsed.astimezone(timezone.utc)
