from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

import app.growth.objective_contract_authority as authority_module
from app.growth.canonical_evaluation_contracts import OBJECTIVE_VERSION, canonical_hash, canonical_json, content_hash
from app.growth.exact_id_attribution_audit import ATTRIBUTION_VERSION, DEDUPE_VERSION
from app.growth.gate0_feasibility_assessment import QUALIFICATION_VERSION, SOURCE_CONTRACT, SOURCE_METRIC
from app.growth.objective_contract_authority import (
    MAX_FILE_BYTES,
    METRIC_CONTRACT_VERSION,
    PRIMARY_METRIC_DEFINITION_VERSION,
    PROPOSAL_VERSION,
    REGISTRY_VERSION,
    REQUIRED_ROLES,
    RESPONSE_VERSION,
    ROLE_PURPOSES,
    SIGNATURE_ALGORITHM,
    ObjectiveContractAuthorityError,
    build_objective_authority_request,
    load_validated_objective_authority_directory,
    load_validated_objective_authority_request_directory,
    signature_message,
    write_objective_authority_artifact,
    write_objective_authority_request_artifact,
)
from scripts.build_gle_objective_contract_authority import main as cli_main


def _bytes(value) -> bytes:
    return (canonical_json(value) + "\n").encode()


def _write(path: Path, value) -> str:
    path.write_bytes(_bytes(value))
    path.chmod(0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _metric_contract(attribution_window: str = "14d-click") -> dict:
    value = {
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
        "attribution": {"version": ATTRIBUTION_VERSION, "window": attribution_window},
        "dedup": {"version": DEDUPE_VERSION, "unit": "CANONICAL_QUALIFIED_IDENTITY"},
        "settlement": {
            "status_required": "SETTLED_COMPLETE",
            "late_data_policy": "REBUILD_NEW_SNAPSHOT",
        },
        "zero_event_rule": "UNDEFINED_CPA_DATA_INCOMPLETE_NOT_INFINITY_OR_ZERO",
        "contract_hash": "",
    }
    value["contract_hash"] = canonical_hash({k: v for k, v in value.items() if k != "contract_hash"})
    return value


def _proposal() -> dict:
    value = {
        "schema_version": PROPOSAL_VERSION,
        "proposal_id": "objective-proposal-mx-v1",
        "created_at": "2026-08-09T07:00:00Z",
        "objective_contract_id": "objective-mx-copy-only-v1",
        "version": 1,
        "ad_account_id": "1012060198097836",
        "market": "MX",
        "currency": "USD",
        "business_goal": "REDUCE_QUALIFIED_JOIN_CPA",
        "primary_metric": {
            "metric_key": "QUALIFIED_JOIN_CPA",
            "definition_version": PRIMARY_METRIC_DEFINITION_VERSION,
            "attribution_window": "14d-click",
            "dedup_version": DEDUPE_VERSION,
            "qualification_rule_version": QUALIFICATION_VERSION,
            "min_business_improvement": 0.3,
            "direction": "LOWER_IS_BETTER",
        },
        "primary_metric_contract": _metric_contract(),
        "secondary_metrics": [
            {"metric_key": "CPI", "definition_version": "meta-cpi-v1", "purpose": "DIAGNOSTIC"},
            {"metric_key": "CTR", "definition_version": "meta-ctr-v1", "purpose": "TREND_ONLY"},
        ],
        "guardrails": [
            {"metric_key": "SPEND_USD", "operator": "LTE", "threshold": 20, "severity": "HARD_STOP"},
        ],
        "risk_boundary": {
            "max_test_budget": 20,
            "max_daily_budget": 2,
            "max_write_requests_per_action": 4,
            "hard_deadline_at": "2026-08-21T00:00:00Z",
            "approval_ttl_seconds": 3600,
        },
        "created_by": "Chauncey",
        "proposal_hash": "",
    }
    value["proposal_hash"] = canonical_hash({k: v for k, v in value.items() if k != "proposal_hash"})
    return value


def _public_key(private_key: Path) -> str:
    return subprocess.run(
        ["openssl", "pkey", "-in", str(private_key), "-pubout"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _prepare(
    tmp_path: Path,
    *,
    same_principal: bool = False,
    same_key: bool = False,
    key_bits: int = 2048,
    valid_until: str = "2026-08-10T00:00:00Z",
) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    proposal_path = tmp_path / "proposal.json"
    proposal = _proposal()
    proposal_sha = _write(proposal_path, proposal)
    private_keys: dict[str, Path] = {}
    keys = []
    shared_private: Path | None = None
    for index, role in enumerate(REQUIRED_ROLES, 1):
        private_key = shared_private if same_key and shared_private else tmp_path / f"private-{index}.pem"
        if not private_key.exists():
            subprocess.run(
                ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", f"rsa_keygen_bits:{key_bits}", "-out", str(private_key)],
                capture_output=True,
                check=True,
            )
            private_key.chmod(0o600)
        if same_key and shared_private is None:
            shared_private = private_key
        private_keys[role] = private_key
        keys.append({
            "key_id": f"key-{index}",
            "signer_id": f"signer-{index}",
            "principal_id": "principal-one" if same_principal else f"principal-{index}",
            "role": role,
            "purposes": [ROLE_PURPOSES[role]],
            "algorithm": SIGNATURE_ALGORITHM,
            "status": "ACTIVE",
            "valid_from": "2026-08-09T00:00:00Z",
            "valid_until": valid_until,
            "public_key_pem": _public_key(private_key),
        })
    registry = {
        "schema_version": REGISTRY_VERSION,
        "registry_id": "objective-authority-keys-v1",
        "keys": sorted(keys, key=lambda item: item["key_id"]),
        "registry_hash": "",
    }
    registry["registry_hash"] = canonical_hash({k: v for k, v in registry.items() if k != "registry_hash"})
    registry_path = tmp_path / "registry.json"
    registry_sha = _write(registry_path, registry)
    source_args = {
        "proposal_file": proposal_path,
        "expected_proposal_sha256": proposal_sha,
        "request_id": "objective-authority-request-1",
        "requested_at": "2026-08-09T08:00:00Z",
        "evaluated_at": "2026-08-09T10:00:00Z",
        "trusted_key_registry_file": registry_path,
        "expected_key_registry_sha256": registry_sha,
        "expected_key_registry_hash": registry["registry_hash"],
    }
    request_dir = tmp_path / "frozen-request"
    write_objective_authority_request_artifact(request_dir, **source_args)
    request_manifest_sha = hashlib.sha256((request_dir / "manifest.json").read_bytes()).hexdigest()
    loaded = load_validated_objective_authority_request_directory(
        request_dir,
        expected_request_manifest_sha256=request_manifest_sha,
        **source_args,
    )
    request = loaded["request"]
    authority_id = "objective-authority-mx-v1"
    authorized_at = "2026-08-09T09:00:00Z"
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
    response = {
        "schema_version": RESPONSE_VERSION,
        "authority_id": authority_id,
        "request_hash": request["request_hash"],
        "request_manifest_sha256": request_manifest_sha,
        "authorized_at": authorized_at,
        "objective_contract": objective,
        "authority_payload_hash": "",
        "signatures": [],
    }
    response["authority_payload_hash"] = canonical_hash({
        k: v for k, v in response.items() if k not in {"authority_payload_hash", "signatures"}
    })
    for role in REQUIRED_ROLES:
        key = next(item for item in keys if item["role"] == role)
        message = signature_message(
            response["authority_payload_hash"],
            request_manifest_sha256=request_manifest_sha,
            key_registry_raw_sha256=registry_sha,
            key_registry_hash=registry["registry_hash"],
            key_id=key["key_id"],
            signer_id=key["signer_id"],
            principal_id=key["principal_id"],
            role=role,
        )
        signature_path = tmp_path / f"{role}.sig"
        subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(private_keys[role]), "-out", str(signature_path)],
            input=message,
            check=True,
        )
        response["signatures"].append({
            "algorithm": SIGNATURE_ALGORITHM,
            "key_id": key["key_id"],
            "signer_id": key["signer_id"],
            "principal_id": key["principal_id"],
            "role": role,
            "purpose": ROLE_PURPOSES[role],
            "object_hash": response["authority_payload_hash"],
            "request_manifest_sha256": request_manifest_sha,
            "key_registry_raw_sha256": registry_sha,
            "key_registry_hash": registry["registry_hash"],
            "signed_at": authorized_at,
            "signature_base64": base64.b64encode(signature_path.read_bytes()).decode(),
        })
    response_path = tmp_path / "response.json"
    response_sha = _write(response_path, response)
    return {
        **source_args,
        "request_dir": request_dir,
        "expected_request_manifest_sha256": request_manifest_sha,
        "response_file": response_path,
        "expected_response_sha256": response_sha,
    }


def _finalize(tmp_path: Path, args: dict, name: str = "final") -> tuple[Path, dict]:
    output = tmp_path / name
    manifest = write_objective_authority_artifact(output, **args)
    return output, manifest


def test_two_stage_request_then_attestation_is_source_aware_and_ceiling_locked(tmp_path: Path) -> None:
    args = _prepare(tmp_path)
    output, manifest = _finalize(tmp_path, args)
    assert manifest["status"] == "OBJECTIVE_AUTHORITY_ATTESTATION_VERIFIED"
    assert manifest["trust_status"] == "SIGNATURES_VALID_UNDER_EXTERNALLY_PINNED_REGISTRY"
    assert manifest["authority_effect"] == "NONE"
    assert manifest["attestation_effect"] == "OBJECTIVE_AUTHORITY_ATTESTATION_VERIFIED"
    assert manifest["objective_effect"] == "SIGNED_CANDIDATE_NOT_GOVERNANCE_PROMOTED"
    assert manifest["snapshot_emitted"] is False
    assert manifest["replay_eligible"] is False
    assert manifest["golden_eligible"] is False
    assert manifest["gate1_effect"] == "NONE"
    loaded = load_validated_objective_authority_directory(
        output,
        expected_manifest_sha256=hashlib.sha256((output / "manifest.json").read_bytes()).hexdigest(),
        **args,
    )
    assert loaded["objective_contract"]["primary_metric"]["definition_version"] == PRIMARY_METRIC_DEFINITION_VERSION
    assert loaded["objective_contract"]["primary_metric"]["definition_version"] != ATTRIBUTION_VERSION
    assert manifest["request_manifest_sha256"] == args["expected_request_manifest_sha256"]


def test_prepare_cli_is_pending_and_finalize_cli_is_local_terminal(tmp_path: Path) -> None:
    args = _prepare(tmp_path)
    cli_request = tmp_path / "cli-request"
    source_cli = [
        "--proposal", str(args["proposal_file"]),
        "--expected-proposal-sha256", args["expected_proposal_sha256"],
        "--request-id", "objective-authority-request-1",
        "--requested-at", "2026-08-09T08:00:00Z",
        "--evaluated-at", "2026-08-09T10:00:00Z",
        "--trusted-key-registry", str(args["trusted_key_registry_file"]),
        "--expected-key-registry-sha256", args["expected_key_registry_sha256"],
        "--expected-key-registry-hash", args["expected_key_registry_hash"],
    ]
    assert cli_main(["prepare", *source_cli, "--output-dir", str(cli_request)]) == 2
    cli_request_sha = hashlib.sha256((cli_request / "manifest.json").read_bytes()).hexdigest()
    # The response is bound to the first frozen request, so another byte-identical request
    # directory still has the same raw manifest anchor and can be finalized safely.
    assert cli_request_sha == args["expected_request_manifest_sha256"]
    assert cli_main([
        "finalize", *source_cli,
        "--request-dir", str(cli_request),
        "--expected-request-manifest-sha256", cli_request_sha,
        "--response", str(args["response_file"]),
        "--expected-response-sha256", args["expected_response_sha256"],
        "--output-dir", str(tmp_path / "cli-final"),
    ]) == 2


def test_response_cannot_borrow_or_replace_frozen_request(tmp_path: Path) -> None:
    args = _prepare(tmp_path)
    wrong = deepcopy(args)
    wrong["expected_request_manifest_sha256"] = "f" * 64
    with pytest.raises(ObjectiveContractAuthorityError, match="G101B_REQUEST_MANIFEST_ANCHOR_MISMATCH"):
        _finalize(tmp_path, wrong, "wrong-request")

    proposal = json.loads(args["proposal_file"].read_text())
    proposal["primary_metric"]["min_business_improvement"] = 0.4
    proposal["proposal_hash"] = canonical_hash({k: v for k, v in proposal.items() if k != "proposal_hash"})
    args["expected_proposal_sha256"] = _write(args["proposal_file"], proposal)
    with pytest.raises(ObjectiveContractAuthorityError, match="G101B_REQUEST_SOURCE_SEMANTICS_MISMATCH"):
        _finalize(tmp_path, args, "borrowed-response")


def test_metric_contract_tamper_and_source_version_mismatch_fail(tmp_path: Path) -> None:
    proposal = _proposal()
    proposal["primary_metric_contract"]["formula"] = "SPEND_USD / INSTALLS"
    proposal["primary_metric_contract"]["contract_hash"] = canonical_hash({
        k: v for k, v in proposal["primary_metric_contract"].items() if k != "contract_hash"
    })
    proposal["proposal_hash"] = canonical_hash({k: v for k, v in proposal.items() if k != "proposal_hash"})
    path = tmp_path / "bad-proposal.json"
    sha = _write(path, proposal)
    good = _prepare(tmp_path / "good")
    with pytest.raises(ObjectiveContractAuthorityError, match="G101B_METRIC_CONTRACT_INVALID"):
        build_objective_authority_request(
            proposal_file=path,
            expected_proposal_sha256=sha,
            request_id="bad-request",
            requested_at="2026-08-09T08:00:00Z",
            evaluated_at="2026-08-09T10:00:00Z",
            trusted_key_registry_file=good["trusted_key_registry_file"],
            expected_key_registry_sha256=good["expected_key_registry_sha256"],
            expected_key_registry_hash=good["expected_key_registry_hash"],
        )


@pytest.mark.parametrize("kwargs,code", [
    ({"same_principal": True}, "G101B_REGISTRY_IDENTITY_NOT_DISTINCT"),
    ({"same_key": True}, "G101B_REGISTRY_IDENTITY_NOT_DISTINCT"),
    ({"key_bits": 1024}, "G101B_REGISTRY_WEAK_KEY"),
])
def test_registry_quorum_requires_distinct_principals_spki_and_strong_keys(tmp_path: Path, kwargs: dict, code: str) -> None:
    with pytest.raises(ObjectiveContractAuthorityError, match=code):
        _prepare(tmp_path, **kwargs)


def test_expired_key_and_wrong_signature_purpose_fail(tmp_path: Path) -> None:
    with pytest.raises(ObjectiveContractAuthorityError, match="G101B_SIGNATURE_INVALID"):
        args = _prepare(tmp_path / "expired", valid_until="2026-08-09T08:30:00Z")
        _finalize(tmp_path, args, "expired-final")

    args = _prepare(tmp_path / "purpose")
    response = json.loads(args["response_file"].read_text())
    response["signatures"][0]["purpose"] = "LINEAGE_AUTHORITY_ATTESTATION"
    args["expected_response_sha256"] = _write(args["response_file"], response)
    with pytest.raises(ObjectiveContractAuthorityError, match="G101B_SIGNATURE_INVALID"):
        _finalize(tmp_path, args, "wrong-purpose")


def test_caller_created_registry_never_produces_governance_authority_effect(tmp_path: Path) -> None:
    args = _prepare(tmp_path)
    _, manifest = _finalize(tmp_path, args)
    assert manifest["authority_effect"] == "NONE"
    assert manifest["reason_codes"] == ["EXTERNAL_REGISTRY_GOVERNANCE_NOT_CONTENT_VERIFIED"]
    assert "APPROVED" not in manifest["objective_effect"]


def test_complete_artifact_rehash_cannot_promote_ceiling(tmp_path: Path) -> None:
    args = _prepare(tmp_path)
    output, _ = _finalize(tmp_path, args)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["authority_effect"] = "APPROVED_OBJECTIVE_CONTRACT"
    manifest["manifest_hash"] = canonical_hash({k: v for k, v in manifest.items() if k != "manifest_hash"})
    new_anchor = _write(manifest_path, manifest)
    with pytest.raises(ObjectiveContractAuthorityError, match="G101B_SOURCE_SEMANTICS_MISMATCH"):
        load_validated_objective_authority_directory(
            output, expected_manifest_sha256=new_anchor, **args
        )


def test_external_symlink_hardlink_oversize_and_output_replace_fail_closed(tmp_path: Path) -> None:
    args = _prepare(tmp_path / "base")
    proposal = args["proposal_file"]
    link = tmp_path / "proposal-link.json"
    link.symlink_to(proposal)
    args["proposal_file"] = link
    with pytest.raises(ObjectiveContractAuthorityError, match="G101B_PROPOSAL_UNREADABLE"):
        write_objective_authority_request_artifact(tmp_path / "link-output", **{
            key: args[key] for key in (
                "proposal_file", "expected_proposal_sha256", "request_id", "requested_at", "evaluated_at",
                "trusted_key_registry_file", "expected_key_registry_sha256", "expected_key_registry_hash",
            )
        })

    hard = tmp_path / "proposal-hard.json"
    os.link(proposal, hard)
    args["proposal_file"] = hard
    with pytest.raises(ObjectiveContractAuthorityError, match="G101B_PROPOSAL_UNSAFE"):
        write_objective_authority_request_artifact(tmp_path / "hard-output", **{
            key: args[key] for key in (
                "proposal_file", "expected_proposal_sha256", "request_id", "requested_at", "evaluated_at",
                "trusted_key_registry_file", "expected_key_registry_sha256", "expected_key_registry_hash",
            )
        })

    huge = tmp_path / "huge.json"
    huge.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    huge.chmod(0o600)
    args["proposal_file"] = huge
    args["expected_proposal_sha256"] = hashlib.sha256(huge.read_bytes()).hexdigest()
    with pytest.raises(ObjectiveContractAuthorityError, match="G101B_PROPOSAL_UNSAFE"):
        write_objective_authority_request_artifact(tmp_path / "huge-output", **{
            key: args[key] for key in (
                "proposal_file", "expected_proposal_sha256", "request_id", "requested_at", "evaluated_at",
                "trusted_key_registry_file", "expected_key_registry_sha256", "expected_key_registry_hash",
            )
        })

    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(ObjectiveContractAuthorityError, match="G101B_OUTPUT_INVALID"):
        _finalize(tmp_path, _prepare(tmp_path / "fresh"), "existing")


def test_artifact_duplicate_json_key_is_rejected(tmp_path: Path) -> None:
    args = _prepare(tmp_path)
    request_path = args["request_dir"] / "authority-request.json"
    raw = request_path.read_text()
    request_path.write_text(raw.replace('{"authority_contract":', '{"request_hash":"' + '0' * 64 + '","authority_contract":', 1))
    request_path.chmod(0o600)
    # The raw manifest anchor is deliberately updated to prove duplicate-key parsing,
    # not just the outer transport hash, rejects the artifact.
    manifest = json.loads((args["request_dir"] / "manifest.json").read_text())
    descriptor = next(item for item in manifest["files"] if item["path"] == "authority-request.json")
    descriptor["sha256"] = hashlib.sha256(request_path.read_bytes()).hexdigest()
    descriptor["size_bytes"] = request_path.stat().st_size
    manifest["manifest_hash"] = canonical_hash({k: v for k, v in manifest.items() if k != "manifest_hash"})
    args["expected_request_manifest_sha256"] = _write(args["request_dir"] / "manifest.json", manifest)
    with pytest.raises(ObjectiveContractAuthorityError, match="G101B_DUPLICATE_JSON_KEY"):
        _finalize(tmp_path, args)


def test_artifact_directory_name_swap_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _prepare(tmp_path)
    output, _ = _finalize(tmp_path, args)
    manifest_anchor = hashlib.sha256((output / "manifest.json").read_bytes()).hexdigest()
    moved = tmp_path / "moved-final"
    original_read = authority_module.os.read
    swapped = False

    def swapping_read(fd: int, count: int) -> bytes:
        nonlocal swapped
        data = original_read(fd, count)
        if not swapped:
            swapped = True
            output.rename(moved)
            output.mkdir(mode=0o700)
        return data

    monkeypatch.setattr(authority_module.os, "read", swapping_read)
    with pytest.raises(ObjectiveContractAuthorityError, match="G101B_ARTIFACT_CHANGED_DURING_READ"):
        load_validated_objective_authority_directory(
            output,
            expected_manifest_sha256=manifest_anchor,
            **args,
        )


def test_runtime_has_no_database_network_meta_or_wallclock_path() -> None:
    source = Path("app/growth/objective_contract_authority.py").read_text()
    forbidden = ("sqlite3", "requests", "urllib", "socket", "Meta", "datetime.now", "time.time")
    assert not any(token in source for token in forbidden)
