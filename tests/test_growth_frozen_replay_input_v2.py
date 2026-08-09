from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import app.growth.frozen_replay_input_v2 as adapter_module
from app.growth.canonical_evaluation_contracts_v2 import (
    CEILING as CONTRACT_CEILING_V2,
    canonical_hash,
    canonical_json,
    content_hash,
    validate_canonical_input_bundle_v2,
)
from app.growth.frozen_replay_input_v2 import (
    ADAPTER_CEILING,
    ADAPTER_LAYER_CEILING,
    BUNDLE_FILE,
    ENVELOPE_FILE,
    EXACT_FILES,
    MANIFEST_FILE,
    FrozenReplayInputV2Error,
    derive_replay_input_envelope_v2,
    load_validated_frozen_replay_input_v2_directory,
    read_anchored_canonical_bundle_v2,
    write_frozen_replay_input_v2_artifact,
)
from tests.test_growth_canonical_evaluation_contracts_v2 import _bundle as canonical_bundle
from tests.test_growth_canonical_evaluation_contracts import objective as objective_v1


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_gle_frozen_replay_input_v2.py"
MODULE = ROOT / "app" / "growth" / "frozen_replay_input_v2.py"


def _candidate_bundle() -> dict:
    value = canonical_bundle()
    spec = value["experiment_spec"]
    spec.update({"status": "AUTHORITY_CANDIDATE", "approved_at": None})
    for cell in spec["cells"]:
        for field in (
            "meta_campaign_id", "meta_adset_id", "meta_creative_id", "meta_ad_id",
            "meta_assignment_cell_id", "actual_allocation", "allocation_verified_at",
        ):
            cell[field] = None
    spec["assignment"]["readback_evidence_sha256"] = None
    spec["spec_hash"] = content_hash(spec, "spec_hash")
    snapshot = value["input_snapshot"]
    snapshot.update({
        "experiment_spec_hash": spec["spec_hash"],
        "allocation_basis": "SYNTHETIC_TARGET_FIXTURE",
        "allocation_readback_evidence_sha256": None,
        "allocation_verified_at": None,
    })
    snapshot["snapshot_hash"] = content_hash(snapshot, "snapshot_hash")
    value["bundle_hash"] = content_hash(value, "bundle_hash")
    assert validate_canonical_input_bundle_v2(value) == value
    return value


def _json_bytes(value: dict) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _write_bundle(path: Path, value: dict | None = None) -> str:
    raw = _json_bytes(value or _candidate_bundle())
    path.write_bytes(raw)
    path.chmod(0o600)
    return hashlib.sha256(raw).hexdigest()


def _build(root: Path, bundle_path: Path, bundle_sha: str, *, clock: str = "2026-08-08T01:00:00Z") -> dict:
    return write_frozen_replay_input_v2_artifact(
        root,
        bundle_path,
        expected_bundle_sha256=bundle_sha,
        replay_input_id="replay-v2-dev-1",
        requested_split="DEV",
        synthetic_clock_at=clock,
    )


def _raw_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite(path: Path, value: dict) -> bytes:
    raw = _json_bytes(value)
    path.write_bytes(raw)
    path.chmod(0o600)
    return raw


def _rehash_bundle(value: dict) -> None:
    value["bundle_hash"] = content_hash(value, "bundle_hash")


def test_authority_candidate_synthetic_bundle_round_trips_with_exact_three_file_ceiling(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "bundle.json"
    bundle_sha = _write_bundle(bundle_path)
    root = tmp_path / "artifact"
    manifest = _build(root, bundle_path, bundle_sha)

    assert set(path.name for path in root.iterdir()) == EXACT_FILES
    assert stat_mode(root) == 0o700
    assert all(stat_mode(path) == 0o600 for path in root.iterdir())
    loaded = load_validated_frozen_replay_input_v2_directory(
        root,
        expected_manifest_sha256=_raw_sha(root / MANIFEST_FILE),
    )
    assert loaded["bundle"] == _candidate_bundle()
    assert loaded["manifest"] == manifest
    assert manifest["status"] == "SYNTHETIC_AUTHORITY_CANDIDATE_CONTRACT_FIXTURE_ONLY"
    assert manifest["requested_split_effect"] == "REQUESTED_CONTEXT_ONLY"
    assert manifest["validation_ceiling"] == ADAPTER_CEILING
    assert manifest["files"][BUNDLE_FILE]["sha256"] == bundle_sha
    assert set(manifest["files"]) == {BUNDLE_FILE, ENVELOPE_FILE}
    envelope = loaded["envelope"]
    assert envelope["spec_shape"] == "AUTHORITY_CANDIDATE_UNVERIFIED"
    assert envelope["authority_reference_content_status"] == "NOT_OPENED_NOT_VERIFIED"
    assert envelope["validation_ceiling"] == ADAPTER_CEILING
    assert set(ADAPTER_LAYER_CEILING).isdisjoint(CONTRACT_CEILING_V2)
    assert all(ADAPTER_CEILING[key] == value for key, value in CONTRACT_CEILING_V2.items())
    assert envelope["assignment_mechanism_content_status"] == (
        "IDENTIFIER_BOUND_CAPABILITY_NOT_OPENED"
    )
    assert envelope["capability_assessment_content_status"] == "NOT_OPENED_NOT_VERIFIED"
    assert manifest["validation_ceiling"]["metric_contract_content_status"] == (
        "NOT_OPENED_NOT_VERIFIED"
    )
    assert CONTRACT_CEILING_V2 == loaded["bundle"]["validation_ceiling"]
    assert not any(ADAPTER_CEILING[field] for field in (
        "snapshot_emitted", "replay_executed", "replay_eligible", "golden_eligible",
    ))


def test_same_bundle_and_clock_are_deterministic_but_clock_and_split_are_bound(tmp_path: Path) -> None:
    path = tmp_path / "bundle.json"
    digest = _write_bundle(path)
    bundle, raw = read_anchored_canonical_bundle_v2(path, expected_sha256=digest)
    base = dict(
        bundle_raw_sha256=hashlib.sha256(raw).hexdigest(),
        replay_input_id="replay-v2-1",
        requested_split="DEV",
        synthetic_clock_at="2026-08-08T01:00:00Z",
    )
    first = derive_replay_input_envelope_v2(bundle, **base)
    assert first == derive_replay_input_envelope_v2(bundle, **base)
    later = derive_replay_input_envelope_v2(
        bundle, **{**base, "synthetic_clock_at": "2026-08-08T01:00:01Z"}
    )
    validation = derive_replay_input_envelope_v2(
        bundle, **{**base, "requested_split": "VALIDATION"}
    )
    assert len({first["input_root"], later["input_root"], validation["input_root"]}) == 3
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_BUNDLE_ANCHOR_MISMATCH"):
        derive_replay_input_envelope_v2(bundle, **{**base, "bundle_raw_sha256": "0" * 64})


@pytest.mark.parametrize("split", ["HOLDOUT", "UNKNOWN", "", "DEV "])
def test_holdout_and_unknown_split_are_rejected(tmp_path: Path, split: str) -> None:
    path = tmp_path / "bundle.json"
    digest = _write_bundle(path)
    bundle, raw = read_anchored_canonical_bundle_v2(path, expected_sha256=digest)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_SPLIT_INVALID"):
        derive_replay_input_envelope_v2(
            bundle,
            bundle_raw_sha256=hashlib.sha256(raw).hexdigest(),
            replay_input_id="replay-v2-1",
            requested_split=split,
            synthetic_clock_at="2026-08-08T01:00:00Z",
        )


def test_adapter_rejects_draft_and_approved_shape_even_when_contract_objects_are_rehashed(
    tmp_path: Path,
) -> None:
    draft = _candidate_bundle()
    spec = draft["experiment_spec"]
    spec.update({"status": "DRAFT", "authority_attestation_ref": None})
    spec["evaluation_plan"].update({"method_version": "UNFROZEN", "policy_version": "UNFROZEN"})
    spec["spec_hash"] = content_hash(spec, "spec_hash")
    snapshot = draft["input_snapshot"]
    snapshot.update({
        "experiment_spec_hash": spec["spec_hash"],
        "evaluator_version": "UNFROZEN",
        "policy_version": "UNFROZEN",
    })
    snapshot["snapshot_hash"] = content_hash(snapshot, "snapshot_hash")
    _rehash_bundle(draft)
    path = tmp_path / "draft.json"
    digest = _write_bundle(path, draft)
    with pytest.raises(FrozenReplayInputV2Error, match="G101C_SNAPSHOT_VERSION_INVALID"):
        read_anchored_canonical_bundle_v2(path, expected_sha256=digest)

    approved = canonical_bundle()
    path = tmp_path / "approved.json"
    digest = _write_bundle(path, approved)
    bundle, raw = read_anchored_canonical_bundle_v2(path, expected_sha256=digest)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_SPEC_SHAPE_CEILING_INVALID"):
        derive_replay_input_envelope_v2(
            bundle,
            bundle_raw_sha256=hashlib.sha256(raw).hexdigest(),
            replay_input_id="replay-v2-1",
            requested_split="DEV",
            synthetic_clock_at="2026-08-08T01:00:00Z",
        )


def test_candidate_authority_reference_is_bound_but_never_promoted(tmp_path: Path) -> None:
    first = _candidate_bundle()
    second = copy.deepcopy(first)
    ref = second["experiment_spec"]["authority_attestation_ref"]
    ref["authority_id"] = "caller-replaced-authority-shape"
    ref["authority_manifest_sha256"] = "9" * 64
    second["experiment_spec"]["spec_hash"] = content_hash(second["experiment_spec"], "spec_hash")
    second["input_snapshot"]["experiment_spec_hash"] = second["experiment_spec"]["spec_hash"]
    second["input_snapshot"]["snapshot_hash"] = content_hash(second["input_snapshot"], "snapshot_hash")
    _rehash_bundle(second)

    roots = []
    for index, bundle in enumerate((first, second)):
        path = tmp_path / f"bundle-{index}.json"
        digest = _write_bundle(path, bundle)
        value, raw = read_anchored_canonical_bundle_v2(path, expected_sha256=digest)
        envelope = derive_replay_input_envelope_v2(
            value,
            bundle_raw_sha256=hashlib.sha256(raw).hexdigest(),
            replay_input_id="replay-v2-1",
            requested_split="DEV",
            synthetic_clock_at="2026-08-08T01:00:00Z",
        )
        assert envelope["validation_ceiling"]["spec_authority_effect"] == "NONE"
        assert envelope["authority_reference_content_status"] == "NOT_OPENED_NOT_VERIFIED"
        assert not envelope["validation_ceiling"]["replay_eligible"]
        roots.append(envelope["input_root"])
    assert roots[0] != roots[1]


@pytest.mark.parametrize("mutation,error", [
    ("metric_binding", "G101C_BUNDLE_METRIC_BINDING_INVALID"),
    ("cell_set", "G101C_BUNDLE_CELL_SET_INVALID"),
    ("clock", "G104A2_SYNTHETIC_CLOCK_BEFORE_SNAPSHOT"),
])
def test_nested_binding_and_time_attacks_fail_closed(
    tmp_path: Path,
    mutation: str,
    error: str,
) -> None:
    value = _candidate_bundle()
    clock = "2026-08-08T01:00:00Z"
    if mutation == "metric_binding":
        value["input_snapshot"]["attribution_version"] = "wrong-attribution-v2"
        value["input_snapshot"]["snapshot_hash"] = content_hash(value["input_snapshot"], "snapshot_hash")
        _rehash_bundle(value)
    elif mutation == "cell_set":
        value["input_snapshot"]["cell_metrics"]["borrowed-cell"] = value["input_snapshot"]["cell_metrics"].pop("cell-c2")
        value["input_snapshot"]["snapshot_hash"] = content_hash(value["input_snapshot"], "snapshot_hash")
        _rehash_bundle(value)
    else:
        clock = "2026-08-08T00:00:00Z"
    path = tmp_path / "bundle.json"
    digest = _write_bundle(path, value)
    if mutation != "clock":
        with pytest.raises(FrozenReplayInputV2Error, match=error):
            read_anchored_canonical_bundle_v2(path, expected_sha256=digest)
        return
    bundle, raw = read_anchored_canonical_bundle_v2(path, expected_sha256=digest)
    with pytest.raises(FrozenReplayInputV2Error, match=error):
        derive_replay_input_envelope_v2(
            bundle,
            bundle_raw_sha256=hashlib.sha256(raw).hexdigest(),
            replay_input_id="replay-v2-1",
            requested_split="DEV",
            synthetic_clock_at=clock,
        )


def test_external_bundle_anchor_canonical_bytes_and_v1_object_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "bundle.json"
    digest = _write_bundle(path)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_BUNDLE_ANCHOR_MISMATCH"):
        read_anchored_canonical_bundle_v2(path, expected_sha256="0" * 64)

    value = json.loads(path.read_text())
    raw = json.dumps(value, indent=2).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_BUNDLE_JSON_INVALID"):
        read_anchored_canonical_bundle_v2(path, expected_sha256=hashlib.sha256(raw).hexdigest())

    raw = _json_bytes(_candidate_bundle()).replace(b'{"bundle_hash":', b'{"bundle_hash":"0","bundle_hash":', 1)
    path.write_bytes(raw)
    path.chmod(0o600)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_BUNDLE_JSON_INVALID"):
        read_anchored_canonical_bundle_v2(path, expected_sha256=hashlib.sha256(raw).hexdigest())

    v1 = _candidate_bundle()
    v1["objective_contract"] = objective_v1()
    _rehash_bundle(v1)
    digest = _write_bundle(path, v1)
    with pytest.raises(FrozenReplayInputV2Error, match="G101C_OBJECTIVE_SCHEMA_INVALID"):
        read_anchored_canonical_bundle_v2(path, expected_sha256=digest)

    oversized = b"{" + b" " * adapter_module.MAX_ARTIFACT_FILE_BYTES + b"}"
    path.write_bytes(oversized)
    path.chmod(0o600)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_BUNDLE_FILE_INVALID"):
        read_anchored_canonical_bundle_v2(
            path, expected_sha256=hashlib.sha256(oversized).hexdigest()
        )


def test_unsafe_input_files_and_existing_output_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bundle.json"
    digest = _write_bundle(path)
    hardlink = tmp_path / "hardlink.json"
    os.link(path, hardlink)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_BUNDLE_FILE_INVALID"):
        read_anchored_canonical_bundle_v2(path, expected_sha256=digest)
    hardlink.unlink()
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(path)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_BUNDLE_FILE_INVALID"):
        read_anchored_canonical_bundle_v2(symlink, expected_sha256=digest)
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "bundle.fifo"
        os.mkfifo(fifo, 0o600)
        with pytest.raises(FrozenReplayInputV2Error, match="G104A2_BUNDLE_FILE_INVALID"):
            read_anchored_canonical_bundle_v2(fifo, expected_sha256="0" * 64)
    root = tmp_path / "artifact"
    root.mkdir()
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_OUTPUT_EXISTS"):
        _build(root, path, digest)


def test_candidate_with_physical_identity_is_rejected_by_synthetic_profile(tmp_path: Path) -> None:
    value = _candidate_bundle()
    value["experiment_spec"]["cells"][0]["meta_campaign_id"] = "caller-meta-campaign"
    value["experiment_spec"]["cells"][1]["meta_campaign_id"] = "caller-meta-campaign"
    value["experiment_spec"]["spec_hash"] = content_hash(
        value["experiment_spec"], "spec_hash"
    )
    value["input_snapshot"]["experiment_spec_hash"] = value["experiment_spec"]["spec_hash"]
    value["input_snapshot"]["snapshot_hash"] = content_hash(
        value["input_snapshot"], "snapshot_hash"
    )
    _rehash_bundle(value)
    path = tmp_path / "bundle.json"
    digest = _write_bundle(path, value)
    bundle, raw = read_anchored_canonical_bundle_v2(path, expected_sha256=digest)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_SYNTHETIC_ALLOCATION_INVALID"):
        derive_replay_input_envelope_v2(
            bundle,
            bundle_raw_sha256=hashlib.sha256(raw).hexdigest(),
            replay_input_id="replay-v2-1",
            requested_split="DEV",
            synthetic_clock_at="2026-08-08T01:00:00Z",
        )


def test_synthetic_allocation_requires_exact_target_not_allowed_deviation(
    tmp_path: Path,
) -> None:
    value = _candidate_bundle()
    value["input_snapshot"]["cell_metrics"]["cell-c1"]["allocation_share"] = 0.6
    value["input_snapshot"]["cell_metrics"]["cell-c2"]["allocation_share"] = 0.4
    value["input_snapshot"]["snapshot_hash"] = content_hash(
        value["input_snapshot"], "snapshot_hash"
    )
    _rehash_bundle(value)
    path = tmp_path / "bundle.json"
    digest = _write_bundle(path, value)
    bundle, raw = read_anchored_canonical_bundle_v2(path, expected_sha256=digest)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_SYNTHETIC_ALLOCATION_INVALID"):
        derive_replay_input_envelope_v2(
            bundle,
            bundle_raw_sha256=hashlib.sha256(raw).hexdigest(),
            replay_input_id="replay-v2-1",
            requested_split="DEV",
            synthetic_clock_at="2026-08-08T01:00:00Z",
        )


def test_synthetic_profile_rejects_rehashed_mutation_observation(tmp_path: Path) -> None:
    value = _candidate_bundle()
    value["input_snapshot"]["mutation_events"] = [{
        "object_id": "meta-ad-1",
        "field": "status",
        "before": "PAUSED",
        "after": "ACTIVE",
        "changed_at": "2026-08-07T23:59:00Z",
        "source": "EXTERNAL",
    }]
    value["input_snapshot"]["snapshot_hash"] = content_hash(
        value["input_snapshot"], "snapshot_hash"
    )
    _rehash_bundle(value)
    path = tmp_path / "bundle.json"
    digest = _write_bundle(path, value)
    bundle, raw = read_anchored_canonical_bundle_v2(path, expected_sha256=digest)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_SYNTHETIC_MUTATION_INVALID"):
        derive_replay_input_envelope_v2(
            bundle,
            bundle_raw_sha256=hashlib.sha256(raw).hexdigest(),
            replay_input_id="replay-v2-1",
            requested_split="DEV",
            synthetic_clock_at="2026-08-08T01:00:00Z",
        )


def test_writer_reports_parent_fsync_failure_as_durability_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bundle.json"
    digest = _write_bundle(path)
    real_fsync = adapter_module.os.fsync
    calls = 0

    def fail_parent_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 5:
            raise OSError("injected parent fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(adapter_module.os, "fsync", fail_parent_fsync)
    root = tmp_path / "artifact"
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_OUTPUT_DURABILITY_UNCERTAIN"):
        _build(root, path, digest)
    assert root.is_dir()
    assert {item.name for item in root.iterdir()} == EXACT_FILES


def test_writer_rejects_output_directory_name_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bundle.json"
    digest = _write_bundle(path)
    root = tmp_path / "artifact"
    moved = tmp_path / "moved-artifact"
    real_write = adapter_module._write_file_at
    replaced = False

    def swap_after_first_write(directory_fd: int, name: str, raw: bytes) -> None:
        nonlocal replaced
        real_write(directory_fd, name, raw)
        if not replaced:
            replaced = True
            root.rename(moved)
            root.mkdir(mode=0o700)

    monkeypatch.setattr(adapter_module, "_write_file_at", swap_after_first_write)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_OUTPUT_DIRECTORY_CHANGED"):
        _build(root, path, digest)


def test_input_and_artifact_name_replacement_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = tmp_path / "bundle.json"
    bundle_sha = _write_bundle(bundle_path)
    bundle_raw = bundle_path.read_bytes()
    moved_bundle = tmp_path / "moved-bundle.json"
    real_read = adapter_module.os.read
    swapped = False

    def swap_input_name(fd: int, size: int) -> bytes:
        nonlocal swapped
        chunk = real_read(fd, size)
        if not swapped:
            swapped = True
            bundle_path.rename(moved_bundle)
            bundle_path.write_bytes(bundle_raw)
            bundle_path.chmod(0o600)
        return chunk

    monkeypatch.setattr(adapter_module.os, "read", swap_input_name)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_BUNDLE_CHANGED_DURING_READ"):
        read_anchored_canonical_bundle_v2(bundle_path, expected_sha256=bundle_sha)

    monkeypatch.setattr(adapter_module.os, "read", real_read)
    bundle_path.unlink()
    moved_bundle.rename(bundle_path)
    root = tmp_path / "artifact"
    _build(root, bundle_path, bundle_sha)
    artifact_raw = {item.name: item.read_bytes() for item in root.iterdir()}
    manifest_sha = _raw_sha(root / MANIFEST_FILE)
    moved_root = tmp_path / "moved-artifact"
    swapped = False

    def swap_artifact_name(fd: int, size: int) -> bytes:
        nonlocal swapped
        chunk = real_read(fd, size)
        if not swapped:
            swapped = True
            root.rename(moved_root)
            root.mkdir(mode=0o700)
            for name, raw in artifact_raw.items():
                replacement = root / name
                replacement.write_bytes(raw)
                replacement.chmod(0o600)
        return chunk

    monkeypatch.setattr(adapter_module.os, "read", swap_artifact_name)
    with pytest.raises(
        FrozenReplayInputV2Error,
        match="G104A2_ARTIFACT_(CHANGED_DURING_READ|PARENT_CHANGED)",
    ):
        load_validated_frozen_replay_input_v2_directory(
            root, expected_manifest_sha256=manifest_sha
        )


def test_exact_set_special_file_and_full_rehash_ceiling_promotion_are_rejected(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle.json"
    bundle_sha = _write_bundle(bundle_path)

    extra = tmp_path / "extra"
    _build(extra, bundle_path, bundle_sha)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_MANIFEST_ANCHOR_MISMATCH"):
        load_validated_frozen_replay_input_v2_directory(
            extra, expected_manifest_sha256="0" * 64
        )
    (extra / "extra.json").write_text("{}\n")
    (extra / "extra.json").chmod(0o600)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_ARTIFACT_DIRECTORY_INVALID"):
        load_validated_frozen_replay_input_v2_directory(
            extra, expected_manifest_sha256=_raw_sha(extra / MANIFEST_FILE)
        )

    special = tmp_path / "special"
    _build(special, bundle_path, bundle_sha)
    target = special / ENVELOPE_FILE
    target.unlink()
    target.symlink_to(special / BUNDLE_FILE)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_ARTIFACT_FILE_INVALID"):
        load_validated_frozen_replay_input_v2_directory(
            special, expected_manifest_sha256=_raw_sha(special / MANIFEST_FILE)
        )

    promoted = tmp_path / "promoted"
    _build(promoted, bundle_path, bundle_sha)
    envelope = json.loads((promoted / ENVELOPE_FILE).read_text())
    envelope["validation_ceiling"]["replay_eligible"] = True
    envelope["envelope_hash"] = canonical_hash({
        key: value for key, value in envelope.items() if key != "envelope_hash"
    })
    envelope_raw = _rewrite(promoted / ENVELOPE_FILE, envelope)
    manifest = json.loads((promoted / MANIFEST_FILE).read_text())
    manifest["envelope_hash"] = envelope["envelope_hash"]
    manifest["files"][ENVELOPE_FILE] = {
        "sha256": hashlib.sha256(envelope_raw).hexdigest(), "size_bytes": len(envelope_raw),
    }
    manifest["validation_ceiling"]["replay_eligible"] = True
    manifest["manifest_hash"] = canonical_hash({
        key: value for key, value in manifest.items() if key != "manifest_hash"
    })
    _rewrite(promoted / MANIFEST_FILE, manifest)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_ENVELOPE_DERIVATION_MISMATCH"):
        load_validated_frozen_replay_input_v2_directory(
            promoted, expected_manifest_sha256=_raw_sha(promoted / MANIFEST_FILE)
        )


def test_malformed_split_and_outer_content_promotion_fail_after_full_rehash(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle.json"
    bundle_sha = _write_bundle(bundle_path)
    bundle, bundle_raw = read_anchored_canonical_bundle_v2(
        bundle_path, expected_sha256=bundle_sha
    )
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_SPLIT_INVALID"):
        derive_replay_input_envelope_v2(
            bundle,
            bundle_raw_sha256=hashlib.sha256(bundle_raw).hexdigest(),
            replay_input_id="replay-v2-1",
            requested_split=[],  # type: ignore[arg-type]
            synthetic_clock_at="2026-08-08T01:00:00Z",
        )

    malformed = tmp_path / "malformed"
    _build(malformed, bundle_path, bundle_sha)
    envelope = json.loads((malformed / ENVELOPE_FILE).read_text())
    envelope["requested_split"] = []
    envelope["envelope_hash"] = canonical_hash({
        key: value for key, value in envelope.items() if key != "envelope_hash"
    })
    envelope_raw = _rewrite(malformed / ENVELOPE_FILE, envelope)
    manifest = json.loads((malformed / MANIFEST_FILE).read_text())
    manifest["requested_split"] = []
    manifest["envelope_hash"] = envelope["envelope_hash"]
    manifest["files"][ENVELOPE_FILE] = {
        "sha256": hashlib.sha256(envelope_raw).hexdigest(), "size_bytes": len(envelope_raw),
    }
    manifest["manifest_hash"] = canonical_hash({
        key: value for key, value in manifest.items() if key != "manifest_hash"
    })
    _rewrite(malformed / MANIFEST_FILE, manifest)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_SPLIT_INVALID"):
        load_validated_frozen_replay_input_v2_directory(
            malformed, expected_manifest_sha256=_raw_sha(malformed / MANIFEST_FILE)
        )

    promoted = tmp_path / "content-promoted"
    _build(promoted, bundle_path, bundle_sha)
    envelope = json.loads((promoted / ENVELOPE_FILE).read_text())
    envelope["validation_ceiling"]["metric_contract_content_status"] = "VERIFIED"
    envelope["envelope_hash"] = canonical_hash({
        key: value for key, value in envelope.items() if key != "envelope_hash"
    })
    envelope_raw = _rewrite(promoted / ENVELOPE_FILE, envelope)
    manifest = json.loads((promoted / MANIFEST_FILE).read_text())
    manifest["validation_ceiling"]["metric_contract_content_status"] = "VERIFIED"
    manifest["envelope_hash"] = envelope["envelope_hash"]
    manifest["files"][ENVELOPE_FILE] = {
        "sha256": hashlib.sha256(envelope_raw).hexdigest(), "size_bytes": len(envelope_raw),
    }
    manifest["manifest_hash"] = canonical_hash({
        key: value for key, value in manifest.items() if key != "manifest_hash"
    })
    _rewrite(promoted / MANIFEST_FILE, manifest)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_ENVELOPE_DERIVATION_MISMATCH"):
        load_validated_frozen_replay_input_v2_directory(
            promoted, expected_manifest_sha256=_raw_sha(promoted / MANIFEST_FILE)
        )


def test_directory_scan_stops_after_first_extra_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = tmp_path / "bundle.json"
    bundle_sha = _write_bundle(bundle_path)
    root = tmp_path / "artifact"
    _build(root, bundle_path, bundle_sha)
    for index in range(100):
        extra = root / f"extra-{index:03d}"
        extra.write_text("x")
        extra.chmod(0o600)

    real_scandir = adapter_module.os.scandir
    consumed = 0

    class CountingEntries:
        def __init__(self, inner: object) -> None:
            self.inner = inner

        def __enter__(self) -> "CountingEntries":
            self.inner.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self.inner.__exit__(*args)

        def __iter__(self) -> "CountingEntries":
            return self

        def __next__(self) -> object:
            nonlocal consumed
            consumed += 1
            if consumed > len(EXACT_FILES) + 1:
                raise AssertionError("directory scan consumed beyond bounded exact-set gate")
            return next(self.inner)

    def counting_scandir(path: object = ".") -> CountingEntries:
        return CountingEntries(real_scandir(path))

    monkeypatch.setattr(adapter_module.os, "scandir", counting_scandir)
    with pytest.raises(FrozenReplayInputV2Error, match="G104A2_ARTIFACT_DIRECTORY_INVALID"):
        load_validated_frozen_replay_input_v2_directory(
            root, expected_manifest_sha256=_raw_sha(root / MANIFEST_FILE)
        )
    assert consumed == len(EXACT_FILES) + 1


def test_cli_returns_blocked_exit_two_and_invalid_exit_64_without_traceback(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle.json"
    bundle_sha = _write_bundle(bundle_path)
    output = tmp_path / "artifact"
    command = [
        sys.executable, str(SCRIPT),
        "--bundle", str(bundle_path),
        "--expected-bundle-sha256", bundle_sha,
        "--replay-input-id", "replay-v2-cli-1",
        "--requested-split", "VALIDATION",
        "--synthetic-clock-at", "2026-08-08T01:00:00Z",
        "--output-dir", str(output),
    ]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    assert result.returncode == 2, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["contract_effect"] == "V2_SCHEMA_AND_SYNTHETIC_VALIDATION_ONLY"
    assert payload["metric_contract_content_status"] == "NOT_OPENED_NOT_VERIFIED"
    assert payload["authority_reference_content_status"] == "NOT_OPENED_NOT_VERIFIED"
    assert payload["assignment_mechanism_content_status"] == (
        "IDENTIFIER_BOUND_CAPABILITY_NOT_OPENED"
    )
    assert payload["allocation_effect"] == "NONE"
    assert payload["requested_split_effect"] == "REQUESTED_CONTEXT_ONLY"
    assert payload["replay_eligible"] is False
    assert payload["gate0_result_effect"] == "UNCHANGED"
    assert payload["gate1_effect"] == "NONE"
    assert payload["manifest_raw_sha256"] == _raw_sha(output / MANIFEST_FILE)

    invalid_command = command.copy()
    invalid_command[invalid_command.index("--expected-bundle-sha256") + 1] = "0" * 64
    invalid_command[invalid_command.index("--output-dir") + 1] = str(tmp_path / "invalid")
    invalid = subprocess.run(
        invalid_command,
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert invalid.returncode == 64
    assert "Traceback" not in invalid.stderr

    missing = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert missing.returncode == 64
    assert "Traceback" not in missing.stderr

    for depth in (800, 2000):
        deep_path = tmp_path / f"deep-{depth}.json"
        deep_raw = b"[" * depth + b"0" + b"]" * depth + b"\n"
        deep_path.write_bytes(deep_raw)
        deep_path.chmod(0o600)
        deep_command = command.copy()
        deep_command[deep_command.index("--bundle") + 1] = str(deep_path)
        deep_command[deep_command.index("--expected-bundle-sha256") + 1] = hashlib.sha256(
            deep_raw
        ).hexdigest()
        deep_command[deep_command.index("--output-dir") + 1] = str(
            tmp_path / f"deep-output-{depth}"
        )
        deep = subprocess.run(
            deep_command, cwd=ROOT, text=True, capture_output=True, check=False,
        )
        assert deep.returncode == 64
        assert "Traceback" not in deep.stderr


def test_runtime_has_no_wall_clock_database_network_meta_or_evaluator_dependency() -> None:
    tree = ast.parse(MODULE.read_text())
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not imported.intersection({"sqlite3", "requests", "urllib", "httpx", "socket"})
    source = MODULE.read_text()
    for forbidden in (
        "datetime.now", "time.time", "os.environ", "MetaGraph", "evaluate(", "DecisionPolicy",
    ):
        assert forbidden not in source
    assert "os.listdir" not in source


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
