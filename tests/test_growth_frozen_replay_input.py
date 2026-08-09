from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from app.growth.canonical_evaluation_contracts import canonical_hash, canonical_json, content_hash
from app.growth.frozen_replay_input import (
    EXACT_FILES,
    FrozenReplayInputError,
    derive_replay_input_envelope,
    load_validated_frozen_replay_input_directory,
    read_canonical_input_file,
    write_frozen_replay_input_artifact,
)
from scripts.build_gle_frozen_replay_input import main as cli_main
from tests.test_growth_canonical_evaluation_contracts import (
    invariant_projection,
    objective,
    snapshot,
    spec,
)


def _objects() -> tuple[dict, dict, dict, dict]:
    obj = objective()
    invariant = invariant_projection()
    experiment = spec(obj, invariant)
    snap = snapshot(obj, experiment)
    return obj, invariant, experiment, snap


def _write_artifact(root: Path) -> tuple[dict, dict, dict, dict, dict]:
    obj, invariant, experiment, snap = _objects()
    manifest = write_frozen_replay_input_artifact(
        root,
        obj,
        invariant,
        experiment,
        snap,
        replay_input_id="replay-input-synthetic-1",
        requested_split="DEV",
        synthetic_clock_at="2026-08-08T01:00:00Z",
    )
    return obj, invariant, experiment, snap, manifest


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_json(path: Path, value: dict) -> bytes:
    raw = (canonical_json(value) + "\n").encode()
    path.write_bytes(raw)
    return raw


def test_synthetic_fixture_round_trips_with_permanent_replay_ceiling(tmp_path: Path) -> None:
    root = tmp_path / "fixture"
    obj, invariant, experiment, snap, manifest = _write_artifact(root)
    loaded = load_validated_frozen_replay_input_directory(
        root,
        expected_manifest_sha256=_sha(root / "manifest.json"),
    )
    assert loaded["objective_contract"] == obj
    assert loaded["invariant_projection"] == invariant
    assert loaded["experiment_spec"] == experiment
    assert loaded["input_snapshot"] == snap
    assert loaded["manifest"] == manifest
    assert set(path.name for path in root.iterdir()) == EXACT_FILES
    assert oct(root.stat().st_mode & 0o777) == "0o700"
    assert all(oct(path.stat().st_mode & 0o777) == "0o600" for path in root.iterdir())
    envelope = loaded["envelope"]
    assert envelope["status"] == "SYNTHETIC_CONTRACT_FIXTURE_ONLY"
    assert envelope["trust_status"] == "UNSIGNED_LOCAL_SYNTHETIC_FIXTURE"
    assert envelope["replay_executed"] is False
    assert envelope["replay_eligible"] is False
    assert envelope["golden_eligible"] is False
    assert envelope["holdout_status"] == "LOCKED_NOT_ASSIGNED"
    assert envelope["gate1_effect"] == "NONE"
    assert envelope["not_replay_receipt"] is True
    assert envelope["not_gate_receipt"] is True


def test_same_inputs_and_clock_are_deterministic_but_clock_is_bound() -> None:
    obj, invariant, experiment, snap = _objects()
    one = derive_replay_input_envelope(
        obj, invariant, experiment, snap,
        replay_input_id="replay-input-synthetic-1",
        requested_split="VALIDATION",
        synthetic_clock_at="2026-08-08T01:00:00Z",
    )
    two = derive_replay_input_envelope(
        deepcopy(obj), deepcopy(invariant), deepcopy(experiment), deepcopy(snap),
        replay_input_id="replay-input-synthetic-1",
        requested_split="VALIDATION",
        synthetic_clock_at="2026-08-08T01:00:00Z",
    )
    later = derive_replay_input_envelope(
        obj, invariant, experiment, snap,
        replay_input_id="replay-input-synthetic-1",
        requested_split="VALIDATION",
        synthetic_clock_at="2026-08-08T01:00:01Z",
    )
    assert one == two
    assert one["input_root"] != later["input_root"]
    assert one["envelope_hash"] != later["envelope_hash"]


@pytest.mark.parametrize("split", ["HOLDOUT", "TRAIN", ""])
def test_holdout_and_unknown_splits_are_rejected(split: str) -> None:
    obj, invariant, experiment, snap = _objects()
    with pytest.raises(FrozenReplayInputError, match="G104A_SPLIT_INVALID"):
        derive_replay_input_envelope(
            obj, invariant, experiment, snap,
            replay_input_id="replay-input-synthetic-1",
            requested_split=split,
            synthetic_clock_at="2026-08-08T01:00:00Z",
        )


def test_borrowed_objects_versions_and_time_fail_closed() -> None:
    obj, invariant, experiment, snap = _objects()
    borrowed = deepcopy(snap)
    borrowed["objective_contract_hash"] = "b" * 64
    borrowed["snapshot_hash"] = content_hash(borrowed, "snapshot_hash")
    with pytest.raises(FrozenReplayInputError, match="G104A_SNAPSHOT_BINDING_MISMATCH"):
        derive_replay_input_envelope(
            obj, invariant, experiment, borrowed,
            replay_input_id="replay-input-synthetic-1",
            requested_split="DEV",
            synthetic_clock_at="2026-08-08T01:00:00Z",
        )

    wrong_version = deepcopy(snap)
    wrong_version["attribution_version"] = "borrowed-attribution-v1"
    wrong_version["snapshot_hash"] = content_hash(wrong_version, "snapshot_hash")
    with pytest.raises(FrozenReplayInputError, match="G104A_VERSION_BINDING_MISMATCH"):
        derive_replay_input_envelope(
            obj, invariant, experiment, wrong_version,
            replay_input_id="replay-input-synthetic-1",
            requested_split="DEV",
            synthetic_clock_at="2026-08-08T01:00:00Z",
        )

    with pytest.raises(FrozenReplayInputError, match="G104A_TIME_BINDING_INVALID"):
        derive_replay_input_envelope(
            obj, invariant, experiment, snap,
            replay_input_id="replay-input-synthetic-1",
            requested_split="DEV",
            synthetic_clock_at="2026-08-07T23:59:59Z",
        )


def test_mutation_after_cutoff_is_rejected_by_foundation_validator() -> None:
    obj, invariant, experiment, snap = _objects()
    snap["mutation_events"] = [{
        "object_id": "ad-1", "field": "status", "before": "ACTIVE", "after": "PAUSED",
        "changed_at": "2026-08-08T00:00:01Z", "source": "EXTERNAL",
    }]
    snap["snapshot_hash"] = content_hash(snap, "snapshot_hash")
    with pytest.raises(FrozenReplayInputError, match="G101_MUTATION_AFTER_CUTOFF"):
        derive_replay_input_envelope(
            obj, invariant, experiment, snap,
            replay_input_id="replay-input-synthetic-1",
            requested_split="DEV",
            synthetic_clock_at="2026-08-08T01:00:00Z",
        )


def test_objective_risk_and_snapshot_allocation_cross_bindings_are_enforced() -> None:
    obj, invariant, experiment, snap = _objects()
    excessive = deepcopy(experiment)
    excessive["power_plan"]["max_test_budget"] = 200
    excessive["spec_hash"] = content_hash(excessive, "spec_hash")
    rebound = snapshot(obj, excessive)
    with pytest.raises(
        FrozenReplayInputError, match="G104A_OBJECTIVE_RISK_BINDING_MISMATCH"
    ):
        derive_replay_input_envelope(
            obj, invariant, excessive, rebound,
            replay_input_id="replay-input-synthetic-1",
            requested_split="DEV",
            synthetic_clock_at="2026-08-08T01:00:00Z",
        )

    invalid_shares = deepcopy(snap)
    invalid_shares["cell_metrics"]["cell-c1"]["allocation_share"] = 0.8
    invalid_shares["cell_metrics"]["cell-c2"]["allocation_share"] = 0.2
    invalid_shares["snapshot_hash"] = content_hash(invalid_shares, "snapshot_hash")
    with pytest.raises(FrozenReplayInputError, match="G104A_SNAPSHOT_ALLOCATION_INVALID"):
        derive_replay_input_envelope(
            obj, invariant, experiment, invalid_shares,
            replay_input_id="replay-input-synthetic-1",
            requested_split="DEV",
            synthetic_clock_at="2026-08-08T01:00:00Z",
        )

    boundary = deepcopy(snap)
    boundary["cell_metrics"]["cell-c1"]["allocation_share"] = 0.6
    boundary["cell_metrics"]["cell-c2"]["allocation_share"] = 0.4
    boundary["snapshot_hash"] = content_hash(boundary, "snapshot_hash")
    assert derive_replay_input_envelope(
        obj, invariant, experiment, boundary,
        replay_input_id="replay-input-synthetic-1",
        requested_split="DEV",
        synthetic_clock_at="2026-08-08T01:00:00Z",
    )["status"] == "SYNTHETIC_CONTRACT_FIXTURE_ONLY"


def test_full_rehash_cannot_promote_replay_or_gate_ceiling(tmp_path: Path) -> None:
    root = tmp_path / "fixture"
    _write_artifact(root)
    envelope_path = root / "replay-input-envelope.json"
    manifest_path = root / "manifest.json"
    envelope = json.loads(envelope_path.read_text())
    envelope.update({"status": "REPLAY_READY", "replay_eligible": True, "gate1_effect": "PASS"})
    envelope["envelope_hash"] = canonical_hash({
        key: value for key, value in envelope.items() if key != "envelope_hash"
    })
    envelope_raw = _rewrite_json(envelope_path, envelope)
    manifest = json.loads(manifest_path.read_text())
    manifest.update({
        "status": "REPLAY_READY", "replay_eligible": True, "gate1_effect": "PASS",
        "envelope_hash": envelope["envelope_hash"],
    })
    manifest["files"]["replay-input-envelope.json"] = {
        "sha256": hashlib.sha256(envelope_raw).hexdigest(),
        "size_bytes": len(envelope_raw),
    }
    manifest["manifest_hash"] = canonical_hash({
        key: value for key, value in manifest.items() if key != "manifest_hash"
    })
    _rewrite_json(manifest_path, manifest)
    with pytest.raises(FrozenReplayInputError, match="G104A_ENVELOPE_DERIVATION_MISMATCH"):
        load_validated_frozen_replay_input_directory(
            root,
            expected_manifest_sha256=_sha(manifest_path),
        )


def test_manifest_anchor_extra_symlink_and_fifo_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "anchor"
    _write_artifact(root)
    with pytest.raises(FrozenReplayInputError, match="G104A_MANIFEST_ANCHOR_MISMATCH"):
        load_validated_frozen_replay_input_directory(
            root, expected_manifest_sha256="f" * 64,
        )

    extra = tmp_path / "extra"
    _write_artifact(extra)
    (extra / "extra.json").write_text("{}\n")
    with pytest.raises(FrozenReplayInputError, match="G104A_ARTIFACT_FILE_SET_INVALID"):
        load_validated_frozen_replay_input_directory(
            extra, expected_manifest_sha256=_sha(extra / "manifest.json"),
        )

    symlink = tmp_path / "symlink"
    _write_artifact(symlink)
    target = symlink / "objective-contract.json"
    raw = target.read_bytes()
    target.unlink()
    backing = tmp_path / "objective-backing.json"
    backing.write_bytes(raw)
    target.symlink_to(backing)
    with pytest.raises(FrozenReplayInputError, match="G104A_ARTIFACT_FILE_INVALID"):
        load_validated_frozen_replay_input_directory(
            symlink, expected_manifest_sha256=_sha(symlink / "manifest.json"),
        )

    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "fifo"
        _write_artifact(fifo)
        target = fifo / "objective-contract.json"
        target.unlink()
        os.mkfifo(target, 0o600)
        with pytest.raises(FrozenReplayInputError, match="G104A_ARTIFACT_FILE_INVALID"):
            load_validated_frozen_replay_input_directory(
                fifo, expected_manifest_sha256=_sha(fifo / "manifest.json"),
            )


def test_noncanonical_input_and_existing_output_are_rejected(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"a":1,"a":1}\n')
    with pytest.raises(FrozenReplayInputError, match="G104A_INPUT_JSON_INVALID"):
        read_canonical_input_file(duplicate)

    root = tmp_path / "fixture"
    obj, invariant, experiment, snap, _ = _write_artifact(root)
    with pytest.raises(FrozenReplayInputError, match="G104A_OUTPUT_EXISTS"):
        write_frozen_replay_input_artifact(
            root, obj, invariant, experiment, snap,
            replay_input_id="replay-input-synthetic-1",
            requested_split="DEV",
            synthetic_clock_at="2026-08-08T01:00:00Z",
        )


def test_cli_builds_blocked_fixture_and_never_reports_replay_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    obj, invariant, experiment, snap = _objects()
    inputs = {
        "objective.json": obj,
        "invariant.json": invariant,
        "spec.json": experiment,
        "snapshot.json": snap,
    }
    for name, value in inputs.items():
        (tmp_path / name).write_text(canonical_json(value) + "\n")
    output = tmp_path / "cli-output"
    rc = cli_main([
        "--objective-contract", str(tmp_path / "objective.json"),
        "--invariant-projection", str(tmp_path / "invariant.json"),
        "--experiment-spec", str(tmp_path / "spec.json"),
        "--input-snapshot", str(tmp_path / "snapshot.json"),
        "--replay-input-id", "replay-input-cli-1",
        "--requested-split", "DEV",
        "--synthetic-clock-at", "2026-08-08T01:00:00Z",
        "--output-dir", str(output),
    ])
    assert rc == 2
    stdout = json.loads(capsys.readouterr().out)
    assert stdout["ok"] is False
    assert stdout["status"] == "SYNTHETIC_CONTRACT_FIXTURE_ONLY"
    assert stdout["replay_executed"] is False
    assert stdout["gate1_effect"] == "NONE"


def test_runtime_has_no_wall_clock_database_network_or_meta_dependency() -> None:
    source = Path("app/growth/frozen_replay_input.py").read_text()
    assert "datetime.now" not in source
    assert "time.time" not in source
    assert "sqlite3" not in source
    assert "requests" not in source
    assert "urllib" not in source
    assert "socket" not in source
    assert "meta" not in source.lower()
