from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.growth.canonical_evaluation_contracts import (
    INVARIANT_FIELDS,
    SNAPSHOT_VERSION,
    canonical_hash,
    canonical_json,
    validate_experiment_spec,
    validate_invariant_projection,
    validate_input_snapshot,
    validate_objective_contract,
    validate_sha256,
    validate_utc,
)


ADAPTER_VERSION = "gle-e04-s04-01a-frozen-replay-input-adapter-v1"
ENVELOPE_VERSION = "gle-e04-s04-01a-replay-input-envelope-v1"
MANIFEST_VERSION = "gle-e04-s04-01a-replay-input-manifest-v1"
EXACT_FILES = frozenset({
    "manifest.json",
    "objective-contract.json",
    "copy-only-invariant-projection.json",
    "experiment-spec.json",
    "evaluation-input-snapshot.json",
    "replay-input-envelope.json",
})
OBJECT_FILES = {
    "objective_contract": "objective-contract.json",
    "invariant_projection": "copy-only-invariant-projection.json",
    "experiment_spec": "experiment-spec.json",
    "input_snapshot": "evaluation-input-snapshot.json",
}
MAX_ARTIFACT_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 8 * 1024 * 1024

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class FrozenReplayInputError(ValueError):
    pass


def _fail(code: str) -> None:
    raise FrozenReplayInputError(code)


def derive_replay_input_envelope(
    objective_contract: Mapping[str, Any],
    invariant_projection: Mapping[str, Any],
    experiment_spec: Mapping[str, Any],
    input_snapshot: Mapping[str, Any],
    *,
    replay_input_id: str,
    requested_split: str,
    synthetic_clock_at: str,
) -> dict[str, Any]:
    if not isinstance(replay_input_id, str) or not _ID_RE.fullmatch(replay_input_id):
        _fail("G104A_REPLAY_INPUT_ID_INVALID")
    if requested_split not in {"DEV", "VALIDATION"}:
        _fail("G104A_SPLIT_INVALID")
    clock = _utc(synthetic_clock_at, "G104A_SYNTHETIC_CLOCK_INVALID")

    try:
        objective = validate_objective_contract(objective_contract)
        invariant = validate_invariant_projection(invariant_projection)
        spec = validate_experiment_spec(experiment_spec)
        snapshot = validate_input_snapshot(input_snapshot)
    except ValueError as exc:
        raise FrozenReplayInputError(str(exc)) from exc
    _validate_cross_bindings(objective, invariant, spec, snapshot, clock)

    objects = {
        "objective_contract": objective,
        "invariant_projection": invariant,
        "experiment_spec": spec,
        "input_snapshot": snapshot,
    }
    source_file_sha256 = {
        name: hashlib.sha256(_json_bytes(value)).hexdigest()
        for name, value in objects.items()
    }
    object_hashes = {
        "objective_contract_hash": objective["contract_hash"],
        "invariant_projection_hash": invariant["projection_hash"],
        "experiment_spec_hash": spec["spec_hash"],
        "input_snapshot_hash": snapshot["snapshot_hash"],
    }
    version_binding = {
        "snapshot_schema_version": snapshot["schema_version"],
        "evaluator_version": snapshot["evaluator_version"],
        "policy_version": snapshot["policy_version"],
        "attribution_version": snapshot["attribution_version"],
        "dedup_version": snapshot["dedup_version"],
    }
    input_root = canonical_hash({
        "domain": "GLE_E04_S04_01A_FROZEN_REPLAY_INPUT_V1",
        "replay_input_id": replay_input_id,
        "requested_split": requested_split,
        "synthetic_clock_at": clock,
        "source_file_sha256": source_file_sha256,
        "object_hashes": object_hashes,
        "version_binding": version_binding,
        "lineage_id": spec["lineage_id"],
        "experiment_id": spec["experiment_id"],
        "snapshot_id": snapshot["snapshot_id"],
        "data_cutoff_at": snapshot["data_cutoff_at"],
    })
    envelope: dict[str, Any] = {
        "schema_version": ENVELOPE_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "replay_input_id": replay_input_id,
        "requested_split": requested_split,
        "synthetic_clock_at": clock,
        "lineage_id": spec["lineage_id"],
        "experiment_id": spec["experiment_id"],
        "snapshot_id": snapshot["snapshot_id"],
        "data_cutoff_at": snapshot["data_cutoff_at"],
        "snapshot_created_at": snapshot["created_at"],
        "source_file_sha256": source_file_sha256,
        "object_hashes": object_hashes,
        "version_binding": version_binding,
        "input_root": input_root,
        "status": "SYNTHETIC_CONTRACT_FIXTURE_ONLY",
        "reason_codes": [
            "FOUNDATION_SPEC_DRAFT",
            "REAL_SOURCE_BINDING_MISSING",
            "REPLAY_EVALUATOR_UNFROZEN",
            "REPLAY_POLICY_UNFROZEN",
        ],
        "trust_status": "UNSIGNED_LOCAL_SYNTHETIC_FIXTURE",
        "input_effect": "INPUT_ADAPTER_ONLY",
        "partition_effect": "NONE",
        "holdout_status": "LOCKED_NOT_ASSIGNED",
        "replay_executed": False,
        "replay_eligible": False,
        "golden_eligible": False,
        "gate1_effect": "NONE",
        "not_dataset_receipt": True,
        "not_replay_receipt": True,
        "not_gate_receipt": True,
        "envelope_hash": "",
    }
    envelope["envelope_hash"] = canonical_hash({
        key: value for key, value in envelope.items() if key != "envelope_hash"
    })
    return envelope


def validate_replay_input_envelope(
    envelope: Mapping[str, Any],
    objective_contract: Mapping[str, Any],
    invariant_projection: Mapping[str, Any],
    experiment_spec: Mapping[str, Any],
    input_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(envelope, Mapping):
        _fail("G104A_ENVELOPE_INVALID")
    expected_keys = {
        "schema_version", "adapter_version", "replay_input_id", "requested_split",
        "synthetic_clock_at", "lineage_id", "experiment_id", "snapshot_id",
        "data_cutoff_at", "snapshot_created_at", "source_file_sha256", "object_hashes",
        "version_binding", "input_root", "status", "reason_codes", "trust_status",
        "input_effect", "partition_effect", "holdout_status", "replay_executed",
        "replay_eligible", "golden_eligible", "gate1_effect", "not_dataset_receipt",
        "not_replay_receipt", "not_gate_receipt", "envelope_hash",
    }
    if set(envelope) != expected_keys:
        _fail("G104A_ENVELOPE_INVALID")
    expected = derive_replay_input_envelope(
        objective_contract,
        invariant_projection,
        experiment_spec,
        input_snapshot,
        replay_input_id=envelope["replay_input_id"],
        requested_split=envelope["requested_split"],
        synthetic_clock_at=envelope["synthetic_clock_at"],
    )
    if dict(envelope) != expected:
        _fail("G104A_ENVELOPE_DERIVATION_MISMATCH")
    return expected


def write_frozen_replay_input_artifact(
    output_dir: str | Path,
    objective_contract: Mapping[str, Any],
    invariant_projection: Mapping[str, Any],
    experiment_spec: Mapping[str, Any],
    input_snapshot: Mapping[str, Any],
    *,
    replay_input_id: str,
    requested_split: str,
    synthetic_clock_at: str,
) -> dict[str, Any]:
    envelope = derive_replay_input_envelope(
        objective_contract,
        invariant_projection,
        experiment_spec,
        input_snapshot,
        replay_input_id=replay_input_id,
        requested_split=requested_split,
        synthetic_clock_at=synthetic_clock_at,
    )
    objects = {
        "objective_contract": validate_objective_contract(objective_contract),
        "invariant_projection": validate_invariant_projection(invariant_projection),
        "experiment_spec": validate_experiment_spec(experiment_spec),
        "input_snapshot": validate_input_snapshot(input_snapshot),
    }
    payloads = {
        OBJECT_FILES[name]: _json_bytes(value) for name, value in objects.items()
    }
    payloads["replay-input-envelope.json"] = _json_bytes(envelope)
    manifest = _manifest(envelope, _descriptors(payloads))
    payloads["manifest.json"] = _json_bytes(manifest)
    _write_artifact_directory(Path(output_dir), payloads)

    raw = _read_artifact_directory(Path(output_dir))
    loaded = load_validated_frozen_replay_input_directory(
        output_dir,
        expected_manifest_sha256=hashlib.sha256(raw["manifest.json"]).hexdigest(),
    )
    if loaded["manifest"] != manifest:
        _fail("G104A_WRITE_READBACK_MISMATCH")
    return manifest


def load_validated_frozen_replay_input_directory(
    input_dir: str | Path,
    *,
    expected_manifest_sha256: str,
) -> dict[str, Any]:
    expected_sha = _sha(expected_manifest_sha256, "G104A_MANIFEST_ANCHOR_INVALID")
    raw = _read_artifact_directory(Path(input_dir))
    if hashlib.sha256(raw["manifest.json"]).hexdigest() != expected_sha:
        _fail("G104A_MANIFEST_ANCHOR_MISMATCH")
    values = {
        name: _json_document(raw[file_name], "G104A_ARTIFACT_JSON_INVALID")
        for name, file_name in OBJECT_FILES.items()
    }
    envelope = _json_document(
        raw["replay-input-envelope.json"], "G104A_ARTIFACT_JSON_INVALID"
    )
    manifest = _json_document(raw["manifest.json"], "G104A_ARTIFACT_JSON_INVALID")
    if not all(isinstance(value, Mapping) for value in [*values.values(), envelope, manifest]):
        _fail("G104A_ARTIFACT_JSON_INVALID")
    validated_envelope = validate_replay_input_envelope(
        envelope,
        values["objective_contract"],
        values["invariant_projection"],
        values["experiment_spec"],
        values["input_snapshot"],
    )
    _validate_manifest(manifest, raw, validated_envelope)
    expected_manifest = _manifest(
        validated_envelope,
        {
            name: {"sha256": hashlib.sha256(raw[name]).hexdigest(), "size_bytes": len(raw[name])}
            for name in EXACT_FILES - {"manifest.json"}
        },
    )
    if dict(manifest) != expected_manifest:
        _fail("G104A_MANIFEST_DERIVATION_MISMATCH")
    return {
        **{name: dict(value) for name, value in values.items()},
        "envelope": validated_envelope,
        "manifest": expected_manifest,
    }


def read_canonical_input_file(path: str | Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(Path(path), flags)
    except OSError as exc:
        raise FrozenReplayInputError("G104A_INPUT_FILE_INVALID") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > MAX_ARTIFACT_FILE_BYTES
        ):
            _fail("G104A_INPUT_FILE_INVALID")
        chunks: list[bytes] = []
        consumed = 0
        while True:
            chunk = os.read(fd, min(65536, MAX_ARTIFACT_FILE_BYTES + 1 - consumed))
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > MAX_ARTIFACT_FILE_BYTES:
                _fail("G104A_ARTIFACT_TOO_LARGE")
            chunks.append(chunk)
        after = os.fstat(fd)
        if _file_identity(before) != _file_identity(after) or consumed != after.st_size:
            _fail("G104A_INPUT_CHANGED_DURING_READ")
        value = _json_document(b"".join(chunks), "G104A_INPUT_JSON_INVALID")
        if not isinstance(value, Mapping):
            _fail("G104A_INPUT_JSON_INVALID")
        return dict(value)
    finally:
        os.close(fd)


def _validate_cross_bindings(
    objective: Mapping[str, Any],
    invariant: Mapping[str, Any],
    spec: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    synthetic_clock_at: str,
) -> None:
    if spec["objective_contract_id"] != objective["objective_contract_id"]:
        _fail("G104A_OBJECTIVE_ID_MISMATCH")
    if (
        invariant["experiment_id"] != spec["experiment_id"]
        or invariant["projection_hash"] != spec["invariant_config_hash"]
        or invariant["invariant_field_hashes"]["IMAGE_SHA"] != spec["cells"][0]["image_sha"]
        or invariant["invariant_field_hashes"]["IMAGE_SHA"] != spec["cells"][1]["image_sha"]
    ):
        _fail("G104A_INVARIANT_BINDING_MISMATCH")
    if (
        snapshot["objective_contract_hash"] != objective["contract_hash"]
        or snapshot["experiment_spec_hash"] != spec["spec_hash"]
        or snapshot["experiment_id"] != spec["experiment_id"]
        or set(snapshot["cell_metrics"]) != {item["cell_id"] for item in spec["cells"]}
    ):
        _fail("G104A_SNAPSHOT_BINDING_MISMATCH")
    primary = objective["primary_metric"]
    if (
        snapshot["policy_version"] != spec["evaluation_plan"]["policy_version"]
        or snapshot["evaluator_version"] != spec["evaluation_plan"]["method_version"]
        or snapshot["attribution_version"] != primary["definition_version"]
        or snapshot["dedup_version"] != primary["dedup_version"]
    ):
        _fail("G104A_VERSION_BINDING_MISMATCH")
    if (
        spec["status"] != "DRAFT"
        or spec["evaluation_plan"]["method_version"] != "UNFROZEN"
        or spec["evaluation_plan"]["policy_version"] != "UNFROZEN"
        or snapshot["schema_version"] != SNAPSHOT_VERSION
    ):
        _fail("G104A_FOUNDATION_CEILING_INVALID")
    risk = objective["risk_boundary"]
    power = spec["power_plan"]
    if (
        float(power["max_test_budget"]) > float(risk["max_test_budget"])
        or _instant(power["hard_deadline_at"]) > _instant(risk["hard_deadline_at"])
    ):
        _fail("G104A_OBJECTIVE_RISK_BINDING_MISMATCH")
    allocation_shares = [
        float(metrics["allocation_share"])
        for metrics in snapshot["cell_metrics"].values()
    ]
    if not math.isclose(sum(allocation_shares), 1.0, rel_tol=0, abs_tol=1e-12):
        _fail("G104A_SNAPSHOT_ALLOCATION_INVALID")
    target_allocation = spec["assignment"]["target_allocation"]
    allowed_deviation = float(spec["assignment"]["allowed_allocation_deviation"])
    if any(
        abs(float(metrics["allocation_share"]) - float(target_allocation[cell_id]))
        > allowed_deviation
        for cell_id, metrics in snapshot["cell_metrics"].items()
    ):
        _fail("G104A_SNAPSHOT_ALLOCATION_INVALID")
    if (
        _instant(spec["created_at"]) < _instant(objective["approved_at"])
        or _instant(snapshot["data_cutoff_at"]) < _instant(spec["created_at"])
        or _instant(snapshot["created_at"]) < _instant(spec["created_at"])
        or _instant(synthetic_clock_at) < _instant(snapshot["created_at"])
    ):
        _fail("G104A_TIME_BINDING_INVALID")


def _manifest(envelope: Mapping[str, Any], files: Mapping[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": MANIFEST_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "replay_input_id": envelope["replay_input_id"],
        "requested_split": envelope["requested_split"],
        "input_root": envelope["input_root"],
        "envelope_hash": envelope["envelope_hash"],
        "status": envelope["status"],
        "trust_status": envelope["trust_status"],
        "input_effect": "INPUT_ADAPTER_ONLY",
        "partition_effect": "NONE",
        "holdout_status": "LOCKED_NOT_ASSIGNED",
        "replay_executed": False,
        "replay_eligible": False,
        "golden_eligible": False,
        "gate1_effect": "NONE",
        "not_dataset_receipt": True,
        "not_replay_receipt": True,
        "not_gate_receipt": True,
        "files": dict(files),
        "manifest_hash": "",
    }
    value["manifest_hash"] = canonical_hash({
        key: item for key, item in value.items() if key != "manifest_hash"
    })
    return value


def _validate_manifest(
    manifest: Mapping[str, Any], raw: Mapping[str, bytes], envelope: Mapping[str, Any]
) -> None:
    expected_keys = set(_manifest(envelope, {}).keys())
    if set(manifest) != expected_keys:
        _fail("G104A_MANIFEST_INVALID")
    if not isinstance(manifest["files"], Mapping) or set(manifest["files"]) != EXACT_FILES - {"manifest.json"}:
        _fail("G104A_MANIFEST_INVALID")
    if manifest["manifest_hash"] != canonical_hash({
        key: value for key, value in manifest.items() if key != "manifest_hash"
    }):
        _fail("G104A_MANIFEST_HASH_MISMATCH")
    for name, descriptor in manifest["files"].items():
        if not isinstance(descriptor, Mapping) or set(descriptor) != {"sha256", "size_bytes"}:
            _fail("G104A_MANIFEST_FILE_INVALID")
        _sha(descriptor["sha256"], "G104A_MANIFEST_FILE_INVALID")
        if (
            type(descriptor["size_bytes"]) is not int
            or descriptor["size_bytes"] != len(raw[name])
            or descriptor["sha256"] != hashlib.sha256(raw[name]).hexdigest()
        ):
            _fail("G104A_FILE_INTEGRITY_MISMATCH")


def _descriptors(payloads: Mapping[str, bytes]) -> dict[str, dict[str, Any]]:
    return {
        name: {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
        for name, raw in payloads.items()
    }


def _write_artifact_directory(root: Path, payloads: Mapping[str, bytes]) -> None:
    if set(payloads) != EXACT_FILES or not root.name or root.name in {".", ".."}:
        _fail("G104A_OUTPUT_INVALID")
    if any(len(raw) > MAX_ARTIFACT_FILE_BYTES for raw in payloads.values()) or sum(map(len, payloads.values())) > MAX_TOTAL_ARTIFACT_BYTES:
        _fail("G104A_ARTIFACT_TOO_LARGE")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(root.parent, flags)
    except OSError as exc:
        raise FrozenReplayInputError("G104A_OUTPUT_PARENT_INVALID") from exc
    root_fd: int | None = None
    complete = False
    try:
        try:
            os.mkdir(root.name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            _fail("G104A_OUTPUT_EXISTS")
        root_fd = os.open(root.name, flags, dir_fd=parent_fd)
        os.fchmod(root_fd, 0o700)
        _require_directory_identity(parent_fd, root.name, root_fd)
        for name in sorted(payloads):
            _write_file_at(root_fd, name, payloads[name])
        if set(os.listdir(root_fd)) != EXACT_FILES:
            _fail("G104A_OUTPUT_FILE_SET_INVALID")
        os.fsync(root_fd)
        _require_directory_identity(parent_fd, root.name, root_fd)
        complete = True
        os.fsync(parent_fd)
    except Exception as exc:
        if complete:
            raise FrozenReplayInputError("G104A_OUTPUT_DURABILITY_UNCERTAIN") from exc
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
                _fail("G104A_WRITE_FAILED")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(name, 0o600, dir_fd=directory_fd, follow_symlinks=False)


def _read_artifact_directory(root: Path) -> dict[str, bytes]:
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        root_fd = os.open(root, dir_flags)
    except OSError as exc:
        raise FrozenReplayInputError("G104A_ARTIFACT_DIRECTORY_INVALID") from exc
    try:
        before_dir = os.fstat(root_fd)
        if not stat.S_ISDIR(before_dir.st_mode) or stat.S_IMODE(before_dir.st_mode) != 0o700:
            _fail("G104A_ARTIFACT_MODE_INVALID")
        if set(os.listdir(root_fd)) != EXACT_FILES:
            _fail("G104A_ARTIFACT_FILE_SET_INVALID")
        raw: dict[str, bytes] = {}
        total = 0
        for name in sorted(EXACT_FILES):
            try:
                fd = os.open(name, file_flags, dir_fd=root_fd)
            except OSError as exc:
                raise FrozenReplayInputError("G104A_ARTIFACT_FILE_INVALID") from exc
            try:
                before = os.fstat(fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_nlink != 1
                    or before.st_size > MAX_ARTIFACT_FILE_BYTES
                ):
                    _fail("G104A_ARTIFACT_FILE_INVALID")
                chunks: list[bytes] = []
                consumed = 0
                while True:
                    chunk = os.read(fd, min(65536, MAX_ARTIFACT_FILE_BYTES + 1 - consumed))
                    if not chunk:
                        break
                    consumed += len(chunk)
                    total += len(chunk)
                    if consumed > MAX_ARTIFACT_FILE_BYTES or total > MAX_TOTAL_ARTIFACT_BYTES:
                        _fail("G104A_ARTIFACT_TOO_LARGE")
                    chunks.append(chunk)
                after = os.fstat(fd)
                if _file_identity(before) != _file_identity(after) or consumed != after.st_size:
                    _fail("G104A_ARTIFACT_CHANGED_DURING_READ")
                raw[name] = b"".join(chunks)
            finally:
                os.close(fd)
        after_dir = os.fstat(root_fd)
        if set(os.listdir(root_fd)) != EXACT_FILES or _dir_identity(before_dir) != _dir_identity(after_dir):
            _fail("G104A_ARTIFACT_CHANGED_DURING_READ")
        return raw
    finally:
        os.close(root_fd)


def _require_directory_identity(parent_fd: int, name: str, opened_fd: int) -> None:
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    opened = os.fstat(opened_fd)
    if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
        _fail("G104A_OUTPUT_DIRECTORY_CHANGED")


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_nlink, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )


def _dir_identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_mtime_ns, value.st_ctime_ns)


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
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(code)
    if raw != _json_bytes(value):
        _fail(code)
    return value


def _json_bytes(value: Any) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _sha(value: Any, code: str) -> str:
    try:
        return validate_sha256(value, code=code)
    except ValueError as exc:
        raise FrozenReplayInputError(str(exc)) from exc


def _utc(value: Any, code: str) -> str:
    try:
        result = validate_utc(value, code=code)
    except ValueError as exc:
        raise FrozenReplayInputError(str(exc)) from exc
    assert isinstance(result, str)
    return result


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
