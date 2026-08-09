from __future__ import annotations

import ast
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from app.growth.canonical_evaluation_contracts import canonical_hash, canonical_json
from app.growth.evaluation_snapshot_source_readiness import (
    EXACT_FILES,
    OBSERVATION_VERSION,
    REQUIRED_FIELD_PATHS,
    EvaluationSnapshotSourceReadinessError,
    build_snapshot_source_readiness,
    load_validated_snapshot_source_readiness_directory,
    write_snapshot_source_readiness_artifact,
)
from app.growth.lineage_devval_registry import (
    evaluate_registry_response,
    write_registry_artifacts,
)
from scripts.audit_gle_evaluation_snapshot_sources import main as cli_main
from tests.test_growth_lineage_devval_registry import (
    _authority,
    _devval_keys,
    _request,
    _seed_and_policy,
    _signed_response,
    _source_validation,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: dict) -> str:
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")
    return _sha(path)


def _registry_artifact(tmp_path: Path, *, signed: bool) -> dict[str, object]:
    source = _authority(tmp_path / "upstream", verified=signed)
    if signed:
        seed_root = tmp_path / "seed"
        seed_root.mkdir()
        seed_reveal, seed_path, seed_sha, policy = _seed_and_policy(seed_root)
        request = _request(
            source,
            policy=policy,
            seed_selection_file=seed_path,
            expected_seed_selection_file_sha256=seed_sha,
        )
        keys, private_keys = _devval_keys(tmp_path / "devval-keys")
        response = _signed_response(request, seed_reveal, keys, private_keys)
        key_hash = keys["registry_hash"]
        source_validation = _source_validation(
            source, seed_path=seed_path, seed_sha=seed_sha,
        )
    else:
        request = _request(source)
        keys = None
        response = None
        key_hash = None
        source_validation = _source_validation(source)
    registry = evaluate_registry_response(
        request,
        response,
        trusted_key_registry=keys,
        expected_devval_key_registry_hash=key_hash,
        source_validation=source_validation,
    )
    root = tmp_path / "registry"
    write_registry_artifacts(
        request,
        response,
        keys,
        registry,
        root,
        expected_devval_key_registry_hash=key_hash,
        source_validation=source_validation,
    )
    return {
        "registry_dir": root,
        "registry_sha": _sha(root / "manifest.json"),
        "devval_key_hash": key_hash,
        "source_validation": source_validation,
        "request": request,
        "registry": registry,
    }


def _field(path: str, *, status: str = "ASSERTED_AVAILABLE") -> dict:
    if status == "ASSERTED_AVAILABLE":
        refs = [{
            "artifact_type": "snapshot-source-evidence-v1",
            "manifest_sha256": "1" * 64,
            "record_id": f"record-{path.replace('.', '-')}",
            "record_hash": hashlib.sha256(path.encode()).hexdigest(),
            "evidence_class": (
                "IMMUTABLE_MUTATION_JOURNAL"
                if path == "mutation_provenance.complete_event_journal"
                else "EXTERNALLY_ANCHORED_OBSERVATION"
            ),
        }]
        commitment = hashlib.sha256(("value:" + path).encode()).hexdigest()
        reasons: list[str] = []
    else:
        refs = []
        commitment = None
        reasons = [f"{status}_{path.replace('.', '_').upper()}"]
    return {
        "field_path": path,
        "status": status,
        "value_commitment": commitment,
        "source_refs": refs,
        "reason_codes": reasons,
    }


def _observation(
    source: dict[str, object],
    *,
    gate0_result: str = "CONTROLLED_FEASIBLE",
    missing_field: str | None = None,
) -> dict:
    request = source["request"]
    registry = source["registry"]
    subjects = []
    for assignment in registry["assignments"]:
        for canonical_id in assignment["canonical_experiment_ids"]:
            fields = [
                _field(path, status="MISSING" if path == missing_field else "ASSERTED_AVAILABLE")
                for path in REQUIRED_FIELD_PATHS
            ]
            subject = {
                "lineage_id": assignment["lineage_id"],
                "canonical_experiment_id": canonical_id,
                "split": assignment["split"],
                "fields": fields,
                "subject_hash": "",
            }
            subject["subject_hash"] = canonical_hash({
                key: value for key, value in subject.items() if key != "subject_hash"
            })
            subjects.append(subject)
    subjects.sort(key=lambda item: (item["lineage_id"], item["canonical_experiment_id"]))
    value = {
        "schema_version": OBSERVATION_VERSION,
        "observation_id": "snapshot-source-observation-1",
        "observed_at": "2026-08-08T02:00:00Z",
        "data_cutoff_at": request["authority_binding"]["data_cutoff_at"],
        "checkpoint": "D3",
        "claimed_gate0_result": gate0_result,
        "gate0_manifest_sha256": "3" * 64,
        "gate0_assessment_hash": "2" * 64,
        "gate0_evidence_refs": [{
            "artifact_type": "gate0-capability-assessment-v1",
            "manifest_sha256": "3" * 64,
            "record_id": "gate0-assessment-1",
            "record_hash": "2" * 64,
            "evidence_class": (
                "CONTROLLED_GATE0_ASSESSMENT"
                if gate0_result == "CONTROLLED_FEASIBLE"
                else "EXTERNALLY_ANCHORED_OBSERVATION"
            ),
        }],
        "upstream_registry_hash": registry["registry_hash"],
        "subjects": subjects,
        "observation_hash": "",
    }
    value["observation_hash"] = canonical_hash({
        key: item for key, item in value.items() if key != "observation_hash"
    })
    return value


def _args(source: dict[str, object], observation_path: Path, observation_sha: str) -> dict:
    return {
        "registry_dir": source["registry_dir"],
        "expected_registry_manifest_sha256": source["registry_sha"],
        "expected_devval_key_registry_hash": source["devval_key_hash"],
        "source_validation": source["source_validation"],
        "source_observation_file": observation_path,
        "expected_source_observation_sha256": observation_sha,
        "readiness_id": "snapshot-source-readiness-1",
        "requested_at": "2026-08-08T03:00:00Z",
        "checkpoint": "D3",
    }


def test_real_missing_authority_materializes_blocked_zero_subject_artifact(tmp_path: Path) -> None:
    source = _registry_artifact(tmp_path, signed=False)
    observation_path = tmp_path / "observations.json"
    observation_sha = _write_json(
        observation_path,
        _observation(source, gate0_result="QUASI_ONLY"),
    )
    output = tmp_path / "readiness"
    manifest = write_snapshot_source_readiness_artifact(
        output, **_args(source, observation_path, observation_sha),
    )
    loaded = load_validated_snapshot_source_readiness_directory(
        output,
        expected_manifest_sha256=_sha(output / "manifest.json"),
        **_args(source, observation_path, observation_sha),
    )
    assert loaded["manifest"] == manifest
    assert set(path.name for path in output.iterdir()) == EXACT_FILES
    assert manifest["status"] == "BLOCKED_UPSTREAM_AUTHORITY"
    assert manifest["subject_count"] == 0
    assert loaded["observation"]["subjects"] == []
    assert {item["field_path"] for item in loaded["gaps"]} == {
        "upstream.lineage_authority",
        "upstream.devval_partition",
        "gate0.controlled_feasibility",
    }
    assert manifest["snapshot_emitted"] is False
    assert manifest["replay_eligible"] is False
    assert manifest["golden_eligible"] is False
    assert manifest["holdout_status"] == "LOCKED_NOT_ASSIGNED"
    assert manifest["gate1_effect"] == "NONE"


def test_signed_partition_and_complete_assertions_remain_unverified(tmp_path: Path) -> None:
    source = _registry_artifact(tmp_path, signed=True)
    observation_path = tmp_path / "observations.json"
    observation_sha = _write_json(observation_path, _observation(source))
    request, observation, gaps = build_snapshot_source_readiness(
        **_args(source, observation_path, observation_sha),
    )
    assert request["status"] == "SOURCE_ASSERTIONS_UNVERIFIED"
    assert request["subject_count"] > 0
    assert gaps
    assert any(item["reason_codes"] == ["SOURCE_FIELD_CONTENT_NOT_VERIFIED"] for item in gaps)
    assert observation["subjects"]
    assert request["snapshot_effect"] == "NONE"
    assert request["snapshot_emitted"] is False
    assert request["not_snapshot_receipt"] is True
    assert request["not_replay_receipt"] is True
    assert request["not_gate_receipt"] is True


def test_missing_metric_is_not_zero_filled_and_stays_incomplete(tmp_path: Path) -> None:
    source = _registry_artifact(tmp_path, signed=True)
    observation = _observation(source, missing_field="cell_metrics.invalid_users")
    path = tmp_path / "observations.json"
    sha = _write_json(path, observation)
    request, validated, gaps = build_snapshot_source_readiness(**_args(source, path, sha))
    assert request["status"] == "SOURCE_INCOMPLETE"
    missing = [item for item in gaps if item["field_path"] == "cell_metrics.invalid_users"]
    assert missing
    fields = [
        field for subject in validated["subjects"] for field in subject["fields"]
        if field["field_path"] == "cell_metrics.invalid_users"
    ]
    assert fields and all(field["value_commitment"] is None for field in fields)


def test_quasi_gate0_cannot_be_promoted_by_complete_source_observation(tmp_path: Path) -> None:
    source = _registry_artifact(tmp_path, signed=True)
    path = tmp_path / "observations.json"
    sha = _write_json(path, _observation(source, gate0_result="QUASI_ONLY"))
    request, _, gaps = build_snapshot_source_readiness(**_args(source, path, sha))
    assert request["status"] == "BLOCKED_GATE0_NOT_CONTROLLED"
    assert any(item["field_path"] == "gate0.controlled_feasibility" for item in gaps)
    assert request["snapshot_emitted"] is False


def test_future_observation_cannot_be_backdated_into_request(tmp_path: Path) -> None:
    source = _registry_artifact(tmp_path, signed=True)
    observation = _observation(source)
    observation["observed_at"] = "2026-08-08T04:00:00Z"
    observation["observation_hash"] = canonical_hash({
        key: value for key, value in observation.items() if key != "observation_hash"
    })
    path = tmp_path / "future.json"
    sha = _write_json(path, observation)
    with pytest.raises(EvaluationSnapshotSourceReadinessError, match="G104B_CUTOFF_BINDING_INVALID"):
        build_snapshot_source_readiness(**_args(source, path, sha))


def test_subject_denominator_holdout_and_observation_anchor_fail_closed(tmp_path: Path) -> None:
    source = _registry_artifact(tmp_path, signed=True)
    observation = _observation(source)
    missing = deepcopy(observation)
    missing["subjects"] = missing["subjects"][:-1]
    missing["observation_hash"] = canonical_hash({
        key: value for key, value in missing.items() if key != "observation_hash"
    })
    path = tmp_path / "missing.json"
    sha = _write_json(path, missing)
    with pytest.raises(EvaluationSnapshotSourceReadinessError, match="G104B_SUBJECT_DENOMINATOR_MISMATCH"):
        build_snapshot_source_readiness(**_args(source, path, sha))

    holdout = deepcopy(observation)
    holdout["subjects"][0]["split"] = "HOLDOUT"
    holdout["subjects"][0]["subject_hash"] = canonical_hash({
        key: value for key, value in holdout["subjects"][0].items() if key != "subject_hash"
    })
    holdout["observation_hash"] = canonical_hash({
        key: value for key, value in holdout.items() if key != "observation_hash"
    })
    holdout_path = tmp_path / "holdout.json"
    holdout_sha = _write_json(holdout_path, holdout)
    with pytest.raises(EvaluationSnapshotSourceReadinessError, match="G104B_HOLDOUT_FORBIDDEN"):
        build_snapshot_source_readiness(**_args(source, holdout_path, holdout_sha))

    valid_path = tmp_path / "valid.json"
    _write_json(valid_path, observation)
    with pytest.raises(EvaluationSnapshotSourceReadinessError, match="G104B_OBSERVATION_ANCHOR_MISMATCH"):
        build_snapshot_source_readiness(**_args(source, valid_path, "f" * 64))


def test_full_rehash_cannot_promote_snapshot_replay_or_gate(tmp_path: Path) -> None:
    source = _registry_artifact(tmp_path, signed=False)
    observation_path = tmp_path / "observations.json"
    observation_sha = _write_json(
        observation_path, _observation(source, gate0_result="QUASI_ONLY"),
    )
    args = _args(source, observation_path, observation_sha)
    output = tmp_path / "readiness"
    write_snapshot_source_readiness_artifact(output, **args)
    request_path = output / "request.json"
    manifest_path = output / "manifest.json"
    request = json.loads(request_path.read_text())
    request.update({
        "status": "SOURCE_ASSERTIONS_UNVERIFIED",
        "snapshot_effect": "CANONICAL_SNAPSHOT",
        "snapshot_emitted": True,
        "replay_eligible": True,
        "gate1_effect": "PASS",
    })
    request["request_hash"] = canonical_hash({
        key: value for key, value in request.items() if key != "request_hash"
    })
    request_raw = (canonical_json(request) + "\n").encode()
    request_path.write_bytes(request_raw)
    manifest = json.loads(manifest_path.read_text())
    manifest.update({
        "status": "SOURCE_ASSERTIONS_UNVERIFIED", "snapshot_effect": "CANONICAL_SNAPSHOT",
        "snapshot_emitted": True, "replay_eligible": True, "gate1_effect": "PASS",
        "request_hash": request["request_hash"],
    })
    manifest["files"]["request.json"] = {
        "sha256": hashlib.sha256(request_raw).hexdigest(), "size_bytes": len(request_raw),
    }
    manifest["manifest_hash"] = canonical_hash({
        key: value for key, value in manifest.items() if key != "manifest_hash"
    })
    _write_json(manifest_path, manifest)
    with pytest.raises(EvaluationSnapshotSourceReadinessError, match="G104B_CEILING_INVALID"):
        load_validated_snapshot_source_readiness_directory(
            output, expected_manifest_sha256=_sha(manifest_path), **args,
        )


def test_unsigned_quasi_to_controlled_full_rehash_stays_unverified(tmp_path: Path) -> None:
    source = _registry_artifact(tmp_path, signed=True)
    observation = _observation(source, gate0_result="QUASI_ONLY")
    observation["claimed_gate0_result"] = "CONTROLLED_FEASIBLE"
    observation["gate0_evidence_refs"][0]["evidence_class"] = "CONTROLLED_GATE0_ASSESSMENT"
    observation["observation_hash"] = canonical_hash({
        key: value for key, value in observation.items() if key != "observation_hash"
    })
    path = tmp_path / "forged-controlled.json"
    sha = _write_json(path, observation)
    request, _, gaps = build_snapshot_source_readiness(**_args(source, path, sha))
    assert request["status"] == "SOURCE_ASSERTIONS_UNVERIFIED"
    assert request["snapshot_emitted"] is False
    assert request["replay_eligible"] is False
    assert any("GATE0_RESULT_CONTENT_NOT_VERIFIED" in gap["reason_codes"] for gap in gaps)


def test_controlled_claim_requires_one_exact_manifest_record_class_ref(tmp_path: Path) -> None:
    source = _registry_artifact(tmp_path, signed=True)
    observation = _observation(source)
    observation["gate0_evidence_refs"][0]["record_hash"] = "4" * 64
    observation["observation_hash"] = canonical_hash({
        key: value for key, value in observation.items() if key != "observation_hash"
    })
    path = tmp_path / "wrong-gate0-record.json"
    sha = _write_json(path, observation)
    with pytest.raises(EvaluationSnapshotSourceReadinessError, match="G104B_GATE0_EVIDENCE_INVALID"):
        build_snapshot_source_readiness(**_args(source, path, sha))


def test_extra_symlink_fifo_and_output_overwrite_fail_closed(tmp_path: Path) -> None:
    source = _registry_artifact(tmp_path, signed=False)
    observation_path = tmp_path / "observations.json"
    observation_sha = _write_json(
        observation_path, _observation(source, gate0_result="QUASI_ONLY"),
    )
    args = _args(source, observation_path, observation_sha)
    output = tmp_path / "readiness"
    write_snapshot_source_readiness_artifact(output, **args)
    with pytest.raises(EvaluationSnapshotSourceReadinessError, match="G104B_OUTPUT_EXISTS"):
        write_snapshot_source_readiness_artifact(output, **args)

    (output / "extra.json").write_text("{}\n")
    with pytest.raises(EvaluationSnapshotSourceReadinessError, match="G104B_ARTIFACT_FILE_SET_INVALID"):
        load_validated_snapshot_source_readiness_directory(
            output, expected_manifest_sha256=_sha(output / "manifest.json"), **args,
        )

    symlink = tmp_path / "observations-link.json"
    symlink.symlink_to(observation_path)
    with pytest.raises(EvaluationSnapshotSourceReadinessError, match="G104B_INPUT_FILE_INVALID"):
        build_snapshot_source_readiness(**_args(source, symlink, observation_sha))

    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "observations.fifo"
        os.mkfifo(fifo)
        with pytest.raises(EvaluationSnapshotSourceReadinessError, match="G104B_INPUT_FILE_INVALID"):
            build_snapshot_source_readiness(**_args(source, fifo, observation_sha))


def test_cli_blocked_exit_two_and_never_claims_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = _registry_artifact(tmp_path, signed=False)
    observation_path = tmp_path / "observations.json"
    observation_sha = _write_json(
        observation_path, _observation(source, gate0_result="QUASI_ONLY"),
    )
    validation = source["source_validation"]
    rc = cli_main([
        "--audit-dir", str(validation["audit_dir"]),
        "--expected-audit-manifest-sha256", validation["expected_audit_manifest_sha256"],
        "--candidate-dir", str(validation["candidate_dir"]),
        "--expected-candidate-manifest-sha256", validation["expected_candidate_manifest_sha256"],
        "--authority-dir", str(validation["authority_dir"]),
        "--expected-authority-manifest-sha256", validation["expected_authority_manifest_sha256"],
        "--registry-dir", str(source["registry_dir"]),
        "--expected-registry-manifest-sha256", source["registry_sha"],
        "--source-observations", str(observation_path),
        "--expected-source-observations-sha256", observation_sha,
        "--readiness-id", "snapshot-source-readiness-1",
        "--requested-at", "2026-08-08T03:00:00Z",
        "--checkpoint", "D3",
        "--output-dir", str(tmp_path / "cli-output"),
    ])
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["status"] == "BLOCKED_UPSTREAM_AUTHORITY"
    assert payload["snapshot_emitted"] is False
    assert payload["gate1_effect"] == "NONE"


def test_runtime_has_no_db_network_meta_or_wall_clock_dependency() -> None:
    source = Path("app/growth/evaluation_snapshot_source_readiness.py").read_text()
    tree = ast.parse(source)
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported.isdisjoint({"sqlite3", "requests", "urllib", "socket", "httpx", "time"})
    assert "datetime.now" not in source
    assert "Meta" not in source
