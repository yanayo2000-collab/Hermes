from __future__ import annotations

import hashlib
import os
from copy import deepcopy
from pathlib import Path

import pytest

from app.growth.canonical_evaluation_contracts import canonical_hash, canonical_json
from app.growth.immutable_lineage_authority import REQUIRED_ROLES
from app.growth.lineage_devval_registry import (
    ALLOWED_SPLITS,
    ASSIGNMENT_ALGORITHM,
    HOLDOUT_STATUS,
    KEY_REGISTRY_VERSION,
    POLICY_VERSION,
    RESPONSE_VERSION,
    SEED_SELECTION_VERSION,
    SIGNATURE_ALGORITHM,
    SIGNATURE_PURPOSE,
    LineageDevvalRegistryError,
    _derive_assignments,
    build_registry_request,
    evaluate_registry_response,
    load_validated_registry_directory,
    seed_hash_for_reveal,
    signature_message,
    write_registry_artifacts,
)
from tests.test_growth_immutable_lineage_authority import (
    _evaluate as evaluate_authority,
    _public_key,
    _registry as authority_key_registry,
    _sha,
    _sign,
    _source_artifacts,
    _valid_response as valid_authority_response,
    _write as write_authority,
)
from scripts.build_gle_lineage_devval_registry import main as registry_cli_main


def _authority(tmp_path: Path, *, verified: bool) -> dict[str, object]:
    source = _source_artifacts(tmp_path / "source")
    if verified:
        registry, private_keys = authority_key_registry(tmp_path / "authority-keys")
        response = valid_authority_response(source["request"], registry, private_keys)
        fragment = evaluate_authority(source, response, registry)
        expected_key_hash = registry["registry_hash"]
    else:
        registry = None
        response = None
        fragment = evaluate_authority(source, None)
        expected_key_hash = None
    authority_dir = tmp_path / "authority"
    write_authority(
        source,
        fragment,
        authority_dir,
        response=response,
        registry=registry,
    )
    return {
        **source,
        "authority_dir": authority_dir,
        "authority_sha": _sha(authority_dir / "manifest.json"),
        "authority_key_hash": expected_key_hash,
    }


def _seed_and_policy(tmp_path: Path) -> tuple[str, Path, str, dict]:
    seed_reveal = "7" * 64
    selection = {
        "schema_version": SEED_SELECTION_VERSION,
        "selection_id": "devval-seed-selection-1",
        "selected_at": "2026-08-07T05:10:00Z",
        "seed_hash": seed_hash_for_reveal(seed_reveal),
    }
    selection["selection_hash"] = canonical_hash(selection)
    path = tmp_path / "seed-selection.json"
    path.write_text(canonical_json(selection) + "\n", encoding="utf-8")
    raw_sha = _sha(path)
    policy = {
        "schema_version": POLICY_VERSION,
        "policy_id": "devval-policy-1",
        "unit": "LINEAGE_ID",
        "allowed_splits": ALLOWED_SPLITS,
        "validation_threshold_bps": 5000,
        "algorithm": ASSIGNMENT_ALGORITHM,
        "seed_selection_file_sha256": raw_sha,
        "seed_hash": selection["seed_hash"],
        "seed_selected_at": selection["selected_at"],
        "holdout_status": HOLDOUT_STATUS,
    }
    policy["policy_hash"] = canonical_hash(policy)
    return seed_reveal, path, raw_sha, policy


def _devval_keys(tmp_path: Path) -> tuple[dict, dict[str, Path]]:
    authority_registry, private_keys = authority_key_registry(tmp_path)
    keys = []
    for item in authority_registry["keys"]:
        key = deepcopy(item)
        key["purposes"] = [SIGNATURE_PURPOSE]
        keys.append(key)
    registry = {
        "schema_version": KEY_REGISTRY_VERSION,
        "registry_id": "devval-registry-keys",
        "keys": keys,
    }
    registry["registry_hash"] = canonical_hash(registry)
    return registry, private_keys


def _signed_response(
    request: dict,
    seed_reveal: str,
    key_registry: dict,
    private_keys: dict[str, Path],
    *,
    authorized_at: str = "2026-08-07T05:40:00Z",
) -> dict:
    assignments = _derive_assignments(request, seed_reveal)
    response = {
        "schema_version": RESPONSE_VERSION,
        "request_hash": request["request_hash"],
        "authorized_at": authorized_at,
        "seed_reveal": seed_reveal,
        "assignment_payload_hash": canonical_hash(assignments),
    }
    response["response_payload_hash"] = canonical_hash(response)
    response["signatures"] = [{
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": key["key_id"],
        "signer_id": key["signer_id"],
        "role": key["role"],
        "purpose": SIGNATURE_PURPOSE,
        "object_hash": response["response_payload_hash"],
        "key_registry_hash": key_registry["registry_hash"],
        "signed_at": authorized_at,
        "signature_base64": _sign(
            private_keys[key["key_id"]],
            signature_message(
                response["response_payload_hash"],
                key_registry_hash=key_registry["registry_hash"],
                key_id=key["key_id"],
                signer_id=key["signer_id"],
                role=key["role"],
            ),
        ),
    } for key in key_registry["keys"]]
    return response


def _source_validation(
    source: dict[str, object],
    *,
    seed_path: Path | None = None,
    seed_sha: str | None = None,
    prior_dir: Path | None = None,
    prior_sha: str | None = None,
    devval_key_hash: str | None = None,
) -> dict:
    return {
        "authority_dir": source["authority_dir"],
        "expected_authority_manifest_sha256": source["authority_sha"],
        "expected_authority_key_registry_hash": source["authority_key_hash"],
        "candidate_dir": source["candidate_dir"],
        "expected_candidate_manifest_sha256": source["candidate_sha"],
        "audit_dir": source["audit_dir"],
        "expected_audit_manifest_sha256": source["audit_sha"],
        "seed_selection_file": seed_path,
        "expected_seed_selection_file_sha256": seed_sha,
        "prior_registry_dir": prior_dir,
        "expected_prior_manifest_sha256": prior_sha,
        "expected_prior_devval_key_registry_hash": (
            devval_key_hash if prior_dir is not None else None
        ),
    }


def _request(source: dict[str, object], **kwargs: object) -> dict:
    return build_registry_request(
        authority_dir=source["authority_dir"],
        expected_authority_manifest_sha256=source["authority_sha"],
        expected_authority_key_registry_hash=source["authority_key_hash"],
        candidate_dir=source["candidate_dir"],
        expected_candidate_manifest_sha256=source["candidate_sha"],
        audit_dir=source["audit_dir"],
        expected_audit_manifest_sha256=source["audit_sha"],
        registry_id=kwargs.pop("registry_id", "devval-registry-1"),
        generation=kwargs.pop("generation", 1),
        requested_at=kwargs.pop("requested_at", "2026-08-07T05:20:00Z"),
        evaluated_at=kwargs.pop("evaluated_at", "2026-08-07T06:00:00Z"),
        **kwargs,
    )


def test_missing_authority_materializes_blocked_zero_effect_artifact(tmp_path: Path) -> None:
    source = _authority(tmp_path, verified=False)
    request = _request(source)
    assert request["status"] == "BLOCKED"
    registry = evaluate_registry_response(
        request,
        None,
        trusted_key_registry=None,
        expected_devval_key_registry_hash=None,
        source_validation=_source_validation(source),
    )
    assert registry["status"] == "BLOCKED"
    assert registry["assignments"] == []
    assert registry["holdout_status"] == HOLDOUT_STATUS
    assert registry["replay_eligible"] is False
    output = tmp_path / "blocked-output"
    source_validation = _source_validation(source)
    write_registry_artifacts(
        request,
        None,
        None,
        registry,
        output,
        expected_devval_key_registry_hash=None,
        source_validation=source_validation,
    )
    loaded_request, loaded_registry = load_validated_registry_directory(
        output,
        expected_registry_manifest_sha256=_sha(output / "manifest.json"),
        expected_devval_key_registry_hash=None,
        source_validation=source_validation,
    )
    assert loaded_request == request
    assert loaded_registry == registry


def test_verified_authority_without_signatures_stays_pending(tmp_path: Path) -> None:
    source = _authority(tmp_path, verified=True)
    _, seed_path, seed_sha, policy = _seed_and_policy(tmp_path)
    request = _request(
        source,
        policy=policy,
        seed_selection_file=seed_path,
        expected_seed_selection_file_sha256=seed_sha,
    )
    assert request["status"] == "PENDING_SIGNATURES"
    assert len(request["eligible_lineages"]) == 1
    registry = evaluate_registry_response(
        request,
        None,
        trusted_key_registry=None,
        expected_devval_key_registry_hash=None,
        source_validation=_source_validation(source, seed_path=seed_path, seed_sha=seed_sha),
    )
    assert registry["status"] == "PENDING_SIGNATURES"
    assert registry["assignments"] == []
    assert registry["split_effect"] == "NONE"

    with pytest.raises(LineageDevvalRegistryError, match="G102B2B_PRIOR_INPUT_INCOMPLETE"):
        _request(
            source,
            policy=policy,
            seed_selection_file=seed_path,
            expected_seed_selection_file_sha256=seed_sha,
            expected_prior_devval_key_registry_hash="0" * 64,
        )


def test_three_role_signed_genesis_round_trips_with_no_holdout_effect(tmp_path: Path) -> None:
    source = _authority(tmp_path, verified=True)
    seed, seed_path, seed_sha, policy = _seed_and_policy(tmp_path)
    request = _request(
        source,
        policy=policy,
        seed_selection_file=seed_path,
        expected_seed_selection_file_sha256=seed_sha,
    )
    key_registry, private_keys = _devval_keys(tmp_path / "devval-keys")
    response = _signed_response(request, seed, key_registry, private_keys)
    registry = evaluate_registry_response(
        request,
        response,
        trusted_key_registry=key_registry,
        expected_devval_key_registry_hash=key_registry["registry_hash"],
        source_validation=_source_validation(
            source,
            seed_path=seed_path,
            seed_sha=seed_sha,
            devval_key_hash=key_registry["registry_hash"],
        ),
    )
    assert registry["status"] == "SIGNED_DETERMINISTIC_PARTITION"
    assert registry["split_effect"] == "DEV_VALIDATION_ASSIGNMENT_ONLY"
    assert len(registry["assignments"]) == 1
    assert registry["assignments"][0]["split"] in ALLOWED_SPLITS
    assert registry["holdout_status"] == HOLDOUT_STATUS
    assert registry["replay_eligible"] is False
    assert registry["golden_eligible"] is False
    assert registry["gate1_effect"] == "NONE"
    assert registry["not_dataset_receipt"] is True
    assert registry["not_gate_receipt"] is True

    output = tmp_path / "verified-output"
    source_validation = _source_validation(
        source,
        seed_path=seed_path,
        seed_sha=seed_sha,
        devval_key_hash=key_registry["registry_hash"],
    )
    write_registry_artifacts(
        request,
        response,
        key_registry,
        registry,
        output,
        expected_devval_key_registry_hash=key_registry["registry_hash"],
        source_validation=source_validation,
    )
    loaded_request, loaded_registry = load_validated_registry_directory(
        output,
        expected_registry_manifest_sha256=_sha(output / "manifest.json"),
        expected_devval_key_registry_hash=key_registry["registry_hash"],
        source_validation=source_validation,
    )
    assert loaded_request == request
    assert loaded_registry == registry


def test_seed_signature_and_holdout_tampering_fail_closed(tmp_path: Path) -> None:
    source = _authority(tmp_path, verified=True)
    seed, seed_path, seed_sha, policy = _seed_and_policy(tmp_path)
    request = _request(
        source,
        policy=policy,
        seed_selection_file=seed_path,
        expected_seed_selection_file_sha256=seed_sha,
    )
    key_registry, private_keys = _devval_keys(tmp_path / "devval-keys")
    response = _signed_response(request, seed, key_registry, private_keys)

    wrong_seed = deepcopy(response)
    wrong_seed["seed_reveal"] = "8" * 64
    wrong_seed["response_payload_hash"] = canonical_hash({
        key: value for key, value in wrong_seed.items()
        if key not in {"response_payload_hash", "signatures"}
    })
    with pytest.raises(LineageDevvalRegistryError, match="G102B2B_SEED_REVEAL_MISMATCH"):
        evaluate_registry_response(
            request,
            wrong_seed,
            trusted_key_registry=key_registry,
            expected_devval_key_registry_hash=key_registry["registry_hash"],
            source_validation=_source_validation(
                source,
                seed_path=seed_path,
                seed_sha=seed_sha,
                devval_key_hash=key_registry["registry_hash"],
            ),
        )

    wrong_purpose = deepcopy(response)
    wrong_purpose["signatures"][0]["purpose"] = "LINEAGE_AUTHORITY_ATTESTATION"
    with pytest.raises(LineageDevvalRegistryError, match="G102B2B_SIGNATURE_INVALID"):
        evaluate_registry_response(
            request,
            wrong_purpose,
            trusted_key_registry=key_registry,
            expected_devval_key_registry_hash=key_registry["registry_hash"],
            source_validation=_source_validation(
                source,
                seed_path=seed_path,
                seed_sha=seed_sha,
                devval_key_hash=key_registry["registry_hash"],
            ),
        )

    registry = evaluate_registry_response(
        request,
        response,
        trusted_key_registry=key_registry,
        expected_devval_key_registry_hash=key_registry["registry_hash"],
        source_validation=_source_validation(
            source,
            seed_path=seed_path,
            seed_sha=seed_sha,
            devval_key_hash=key_registry["registry_hash"],
        ),
    )
    promoted = deepcopy(registry)
    promoted["holdout_status"] = "ASSIGNED"
    promoted["registry_hash"] = canonical_hash({
        key: value for key, value in promoted.items() if key != "registry_hash"
    })
    with pytest.raises(LineageDevvalRegistryError, match="G102B2B_REGISTRY_"):
        write_registry_artifacts(
            request,
            response,
            key_registry,
            promoted,
            tmp_path / "promoted",
            expected_devval_key_registry_hash=key_registry["registry_hash"],
            source_validation=_source_validation(
                source,
                seed_path=seed_path,
                seed_sha=seed_sha,
                devval_key_hash=key_registry["registry_hash"],
            ),
        )


def test_source_aware_evaluator_rejects_fully_rehashed_forged_lineage(tmp_path: Path) -> None:
    source = _authority(tmp_path, verified=True)
    seed, seed_path, seed_sha, policy = _seed_and_policy(tmp_path)
    request = _request(
        source,
        policy=policy,
        seed_selection_file=seed_path,
        expected_seed_selection_file_sha256=seed_sha,
    )
    forged = deepcopy(request)
    lineage = forged["eligible_lineages"][0]
    lineage["canonical_experiment_ids"] = ["forged-study"]
    lineage["authority_membership_hash"] = canonical_hash({
        "lineage_id": lineage["lineage_id"],
        "canonical_experiment_ids": lineage["canonical_experiment_ids"],
        "authority_node_hashes": lineage["authority_node_hashes"],
    })
    forged["request_hash"] = canonical_hash({
        key: value for key, value in forged.items() if key != "request_hash"
    })
    key_registry, private_keys = _devval_keys(tmp_path / "devval-keys")
    forged_response = _signed_response(forged, seed, key_registry, private_keys)
    with pytest.raises(LineageDevvalRegistryError, match="G102B2B_REQUEST_SOURCE_SEMANTICS_MISMATCH"):
        evaluate_registry_response(
            forged,
            forged_response,
            trusted_key_registry=key_registry,
            expected_devval_key_registry_hash=key_registry["registry_hash"],
            source_validation=_source_validation(
                source,
                seed_path=seed_path,
                seed_sha=seed_sha,
                devval_key_hash=key_registry["registry_hash"],
            ),
        )


def test_registry_rejects_lineage_purpose_and_reused_spki(tmp_path: Path) -> None:
    source = _authority(tmp_path, verified=True)
    seed, seed_path, seed_sha, policy = _seed_and_policy(tmp_path)
    request = _request(
        source,
        policy=policy,
        seed_selection_file=seed_path,
        expected_seed_selection_file_sha256=seed_sha,
    )
    key_registry, private_keys = _devval_keys(tmp_path / "devval-keys")

    wrong_purpose_registry = deepcopy(key_registry)
    for key in wrong_purpose_registry["keys"]:
        key["purposes"] = ["LINEAGE_AUTHORITY_ATTESTATION"]
    wrong_purpose_registry["registry_hash"] = canonical_hash({
        key: value for key, value in wrong_purpose_registry.items() if key != "registry_hash"
    })
    response = _signed_response(request, seed, wrong_purpose_registry, private_keys)
    with pytest.raises(LineageDevvalRegistryError, match="G102B2B_KEY_REGISTRY_INVALID"):
        evaluate_registry_response(
            request,
            response,
            trusted_key_registry=wrong_purpose_registry,
            expected_devval_key_registry_hash=wrong_purpose_registry["registry_hash"],
            source_validation=_source_validation(
                source,
                seed_path=seed_path,
                seed_sha=seed_sha,
                devval_key_hash=wrong_purpose_registry["registry_hash"],
            ),
        )

    reused_spki = deepcopy(key_registry)
    public_key = reused_spki["keys"][0]["public_key_pem"]
    for key in reused_spki["keys"]:
        key["public_key_pem"] = public_key
    reused_spki["registry_hash"] = canonical_hash({
        key: value for key, value in reused_spki.items() if key != "registry_hash"
    })
    response = _signed_response(request, seed, reused_spki, private_keys)
    with pytest.raises(LineageDevvalRegistryError, match="G102B2B_KEY_REGISTRY_INVALID"):
        evaluate_registry_response(
            request,
            response,
            trusted_key_registry=reused_spki,
            expected_devval_key_registry_hash=reused_spki["registry_hash"],
            source_validation=_source_validation(
                source,
                seed_path=seed_path,
                seed_sha=seed_sha,
                devval_key_hash=reused_spki["registry_hash"],
            ),
        )


def test_second_generation_preserves_prior_assignment_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source = _authority(tmp_path, verified=True)
    seed, seed_path, seed_sha, policy = _seed_and_policy(tmp_path)
    key_registry, private_keys = _devval_keys(tmp_path / "devval-keys")
    request1 = _request(
        source,
        policy=policy,
        seed_selection_file=seed_path,
        expected_seed_selection_file_sha256=seed_sha,
    )
    response1 = _signed_response(request1, seed, key_registry, private_keys)
    registry1 = evaluate_registry_response(
        request1,
        response1,
        trusted_key_registry=key_registry,
        expected_devval_key_registry_hash=key_registry["registry_hash"],
        source_validation=_source_validation(
            source,
            seed_path=seed_path,
            seed_sha=seed_sha,
            devval_key_hash=key_registry["registry_hash"],
        ),
    )
    prior_dir = tmp_path / "generation-1"
    source_validation1 = _source_validation(
        source,
        seed_path=seed_path,
        seed_sha=seed_sha,
        devval_key_hash=key_registry["registry_hash"],
    )
    write_registry_artifacts(
        request1,
        response1,
        key_registry,
        registry1,
        prior_dir,
        expected_devval_key_registry_hash=key_registry["registry_hash"],
        source_validation=source_validation1,
    )
    prior_sha = _sha(prior_dir / "manifest.json")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(canonical_json(policy) + "\n", encoding="utf-8")
    pending_output = tmp_path / "generation-2-pending"
    pending_rc = registry_cli_main([
        "--audit-dir", str(source["audit_dir"]),
        "--expected-audit-manifest-sha256", str(source["audit_sha"]),
        "--candidate-dir", str(source["candidate_dir"]),
        "--expected-candidate-manifest-sha256", str(source["candidate_sha"]),
        "--authority-dir", str(source["authority_dir"]),
        "--expected-authority-manifest-sha256", str(source["authority_sha"]),
        "--expected-authority-key-registry-hash", str(source["authority_key_hash"]),
        "--registry-id", "devval-registry-1",
        "--generation", "2",
        "--requested-at", "2026-08-07T06:10:00Z",
        "--evaluated-at", "2026-08-07T07:00:00Z",
        "--policy", str(policy_path),
        "--prior-registry-dir", str(prior_dir),
        "--expected-prior-manifest-sha256", prior_sha,
        "--expected-prior-devval-key-registry-hash", key_registry["registry_hash"],
        "--output-dir", str(pending_output),
    ])
    assert pending_rc == 2
    pending_summary = __import__("json").loads(capsys.readouterr().out)
    assert pending_summary["status"] == "PENDING_SIGNATURES"
    assert pending_summary["assignment_count"] == 0
    assert pending_summary["split_effect"] == "NONE"
    missing_prior_key_rc = registry_cli_main([
        "--audit-dir", str(source["audit_dir"]),
        "--expected-audit-manifest-sha256", str(source["audit_sha"]),
        "--candidate-dir", str(source["candidate_dir"]),
        "--expected-candidate-manifest-sha256", str(source["candidate_sha"]),
        "--authority-dir", str(source["authority_dir"]),
        "--expected-authority-manifest-sha256", str(source["authority_sha"]),
        "--expected-authority-key-registry-hash", str(source["authority_key_hash"]),
        "--registry-id", "devval-registry-1",
        "--generation", "2",
        "--requested-at", "2026-08-07T06:10:00Z",
        "--evaluated-at", "2026-08-07T07:00:00Z",
        "--policy", str(policy_path),
        "--prior-registry-dir", str(prior_dir),
        "--expected-prior-manifest-sha256", prior_sha,
        "--output-dir", str(tmp_path / "missing-prior-key"),
    ])
    assert missing_prior_key_rc == 64
    assert "G102B2B_CLI_PRIOR_TRUST_INPUTS_INCOMPLETE" in capsys.readouterr().err

    request2 = _request(
        source,
        generation=2,
        requested_at="2026-08-07T06:10:00Z",
        evaluated_at="2026-08-07T07:00:00Z",
        policy=policy,
        prior_registry_dir=prior_dir,
        expected_prior_manifest_sha256=prior_sha,
        expected_prior_devval_key_registry_hash=key_registry["registry_hash"],
    )
    response2 = _signed_response(
        request2,
        seed,
        key_registry,
        private_keys,
        authorized_at="2026-08-07T06:40:00Z",
    )
    registry2 = evaluate_registry_response(
        request2,
        response2,
        trusted_key_registry=key_registry,
        expected_devval_key_registry_hash=key_registry["registry_hash"],
        source_validation=_source_validation(
            source,
            prior_dir=prior_dir,
            prior_sha=prior_sha,
            devval_key_hash=key_registry["registry_hash"],
        ),
    )
    assert request2["retained_assignments"] == registry1["assignments"]
    assert registry2["assignments"] == registry1["assignments"]
    assert registry2["registry_state_root"] != registry1["registry_state_root"]

    tampered_prior_sha = "0" * 64
    with pytest.raises(LineageDevvalRegistryError, match="G102B2B_PRIOR_MANIFEST_ANCHOR_MISMATCH"):
        _request(
            source,
            generation=2,
            requested_at="2026-08-07T06:10:00Z",
            evaluated_at="2026-08-07T07:00:00Z",
            policy=policy,
            prior_registry_dir=prior_dir,
            expected_prior_manifest_sha256=tampered_prior_sha,
            expected_prior_devval_key_registry_hash=key_registry["registry_hash"],
        )
    with pytest.raises(LineageDevvalRegistryError, match="G102B2B_KEY_REGISTRY_INVALID"):
        _request(
            source,
            generation=2,
            requested_at="2026-08-07T06:10:00Z",
            evaluated_at="2026-08-07T07:00:00Z",
            policy=policy,
            prior_registry_dir=prior_dir,
            expected_prior_manifest_sha256=prior_sha,
            expected_prior_devval_key_registry_hash="0" * 64,
        )

    forged_request = deepcopy(request2)
    forged_assignment = forged_request["retained_assignments"][0]
    forged_assignment["split"] = (
        "DEV" if forged_assignment["split"] == "VALIDATION" else "VALIDATION"
    )
    forged_assignment["score_u64"] ^= 1
    forged_assignment["assignment_hash"] = canonical_hash({
        key: value for key, value in forged_assignment.items()
        if key != "assignment_hash"
    })
    forged_request["request_hash"] = canonical_hash({
        key: value for key, value in forged_request.items()
        if key != "request_hash"
    })
    with pytest.raises(
        LineageDevvalRegistryError,
        match="G102B2B_PRIOR_ASSIGNMENT_DETERMINISM_CONFLICT",
    ):
        _derive_assignments(forged_request, seed)


def test_artifact_reader_rejects_mode_drift_and_extra_file(tmp_path: Path) -> None:
    source = _authority(tmp_path, verified=False)
    request = _request(source)
    source_validation = _source_validation(source)
    registry = evaluate_registry_response(
        request,
        None,
        trusted_key_registry=None,
        expected_devval_key_registry_hash=None,
        source_validation=source_validation,
    )
    output = tmp_path / "artifact-modes"
    write_registry_artifacts(
        request,
        None,
        None,
        registry,
        output,
        expected_devval_key_registry_hash=None,
        source_validation=source_validation,
    )
    manifest_sha = _sha(output / "manifest.json")
    request_path = output / "registry-request.json"
    request_path.chmod(0o644)
    with pytest.raises(LineageDevvalRegistryError, match="G102B2B_ARTIFACT_MODE_INVALID"):
        load_validated_registry_directory(
            output,
            expected_registry_manifest_sha256=manifest_sha,
            expected_devval_key_registry_hash=None,
            source_validation=source_validation,
        )
    request_path.chmod(0o600)
    extra = output / "extra.json"
    extra.write_text("{}\n", encoding="utf-8")
    extra.chmod(0o600)
    with pytest.raises(LineageDevvalRegistryError, match="G102B2B_ARTIFACT_FILE_SET_INVALID"):
        load_validated_registry_directory(
            output,
            expected_registry_manifest_sha256=manifest_sha,
            expected_devval_key_registry_hash=None,
            source_validation=source_validation,
        )
    extra.unlink()
    request_path.unlink()
    os.mkfifo(request_path, 0o600)
    with pytest.raises(LineageDevvalRegistryError, match="G102B2B_ARTIFACT_FILE_INVALID"):
        load_validated_registry_directory(
            output,
            expected_registry_manifest_sha256=manifest_sha,
            expected_devval_key_registry_hash=None,
            source_validation=source_validation,
        )


def test_artifact_manifest_rehash_cannot_promote_assignment(tmp_path: Path) -> None:
    source = _authority(tmp_path, verified=False)
    request = _request(source)
    registry = evaluate_registry_response(
        request,
        None,
        trusted_key_registry=None,
        expected_devval_key_registry_hash=None,
        source_validation=_source_validation(source),
    )
    output = tmp_path / "blocked-output"
    source_validation = _source_validation(source)
    write_registry_artifacts(
        request,
        None,
        None,
        registry,
        output,
        expected_devval_key_registry_hash=None,
        source_validation=source_validation,
    )
    registry_path = output / "devval-registry.json"
    payload = deepcopy(registry)
    payload["split_effect"] = "DEV_VALIDATION_ASSIGNMENT_ONLY"
    payload["registry_hash"] = canonical_hash({
        key: value for key, value in payload.items() if key != "registry_hash"
    })
    registry_path.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    manifest_path = output / "manifest.json"
    manifest = __import__("json").loads(manifest_path.read_text())
    raw = registry_path.read_bytes()
    manifest["files"]["devval-registry.json"] = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }
    manifest["manifest_hash"] = canonical_hash({
        key: value for key, value in manifest.items() if key != "manifest_hash"
    })
    manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    with pytest.raises(LineageDevvalRegistryError, match="G102B2B_REGISTRY_"):
        load_validated_registry_directory(
            output,
            expected_registry_manifest_sha256=_sha(manifest_path),
            expected_devval_key_registry_hash=None,
            source_validation=source_validation,
        )


def test_devval_registry_uses_distinct_signing_purpose_and_roles() -> None:
    assert SIGNATURE_PURPOSE == "DEV_VALIDATION_REGISTRY_ATTESTATION"
    assert list(REQUIRED_ROLES) == ["BUSINESS_OWNER", "DATA_OWNER", "TECH_OWNER"]


def test_cli_materializes_missing_authority_as_blocked_diagnostic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source = _authority(tmp_path, verified=False)
    output = tmp_path / "cli-output"
    rc = registry_cli_main([
        "--audit-dir", str(source["audit_dir"]),
        "--expected-audit-manifest-sha256", str(source["audit_sha"]),
        "--candidate-dir", str(source["candidate_dir"]),
        "--expected-candidate-manifest-sha256", str(source["candidate_sha"]),
        "--authority-dir", str(source["authority_dir"]),
        "--expected-authority-manifest-sha256", str(source["authority_sha"]),
        "--registry-id", "devval-cli-blocked",
        "--generation", "1",
        "--requested-at", "2026-08-07T05:20:00Z",
        "--evaluated-at", "2026-08-07T06:00:00Z",
        "--output-dir", str(output),
    ])
    assert rc == 2
    summary = __import__("json").loads(capsys.readouterr().out)
    assert summary["status"] == "BLOCKED"
    assert summary["assignment_count"] == 0
    assert summary["holdout_status"] == HOLDOUT_STATUS
    assert {item.name for item in output.iterdir()} == {
        "manifest.json", "registry-request.json", "registry-response.json",
        "trusted-key-registry.json", "devval-registry.json",
    }
