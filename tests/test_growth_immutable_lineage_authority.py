from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from app.growth.canonical_evaluation_contracts import canonical_hash
from app.growth.historical_asof_audit import (
    build_audit,
    make_request,
    open_readonly_snapshot,
    write_audit_bundle,
)
from app.growth.historical_lineage_candidates import (
    derive_lineage_candidates_from_audit_directory,
    write_lineage_candidate_bundle,
)
from app.growth.immutable_lineage_authority import (
    CONTRACT_VERSION,
    REGISTRY_VERSION,
    REQUIRED_ROLES,
    RESPONSE_VERSION,
    SIGNATURE_ALGORITHM,
    SIGNATURE_PURPOSE,
    ImmutableLineageAuthorityError,
    build_authority_request,
    evaluate_authority_response,
    lineage_id_for_root,
    load_validated_authority_directory,
    load_validated_candidate_directory,
    signature_message,
    validate_authority_request,
    write_authority_artifacts,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _database(path: Path, *, include_unresolved: bool = False) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE ad_experiment (
          experiment_id TEXT PRIMARY KEY, account_id TEXT, country TEXT, platform TEXT,
          source_report_id TEXT, source_campaign_id TEXT, source_adset_id TEXT,
          source_ad_id TEXT, source_creative_id TEXT, hypothesis_json TEXT,
          control_definition_json TEXT, state TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE ad_experiment_evaluation (
          evaluation_id TEXT PRIMARY KEY, experiment_id TEXT, episode_id TEXT, checkpoint TEXT,
          baseline_window_json TEXT, post_window_json TEXT, baseline_metrics_json TEXT,
          post_metrics_json TEXT, data_quality_status TEXT, dedupe_version TEXT,
          attribution_version TEXT, evaluation_status TEXT, evaluated_at TEXT
        );
        CREATE TABLE ad_creative_group_evaluation (
          group_evaluation_id TEXT PRIMARY KEY, launch_id TEXT, checkpoint TEXT,
          window_json TEXT, metrics_by_experiment_json TEXT, ranking_json TEXT,
          winner_experiment_id TEXT, decision_status TEXT, actual_days INTEGER,
          data_quality_status TEXT, evidence_json TEXT, evaluated_at TEXT
        );
        CREATE TABLE ad_audience_pair_evaluation (
          pair_evaluation_id TEXT PRIMARY KEY, launch_id TEXT, checkpoint TEXT,
          baseline_experiment_id TEXT, challenger_experiment_id TEXT, metrics_json TEXT,
          winner_experiment_id TEXT, decision_status TEXT, evidence_json TEXT, evaluated_at TEXT
        );
        CREATE TABLE ad_experiment_events (
          event_id TEXT PRIMARY KEY, experiment_id TEXT, from_state TEXT, to_state TEXT,
          event_type TEXT, actor TEXT, reason TEXT, evidence_json TEXT, created_at TEXT
        );
        CREATE TABLE ad_daily_report (
          report_id TEXT PRIMARY KEY, report_date TEXT, data_mode TEXT, snapshot_version TEXT,
          rule_version TEXT, window_start_utc TEXT, window_end_utc TEXT,
          generated_at_utc TEXT, payload_json TEXT
        );
        CREATE TABLE ad_creative_group_evaluation_history (
          history_id TEXT PRIMARY KEY, group_evaluation_id TEXT, launch_id TEXT,
          checkpoint TEXT, snapshot_json TEXT, archived_reason TEXT, archived_at TEXT
        );
        """
    )
    rows = [
        ("exp-1", "act-private", "MX", "meta", "launch-1", "campaign-1", "set-1", "ad-1", "creative-1", "{}", "{}", "MATURING", "2026-08-01T00:00:00Z", "2026-08-06T00:00:00Z"),
        ("exp-2", "act-private", "MX", "meta", "launch-1", "campaign-1", "set-2", "ad-2", "creative-2", "{}", "{}", "MATURING", "2026-08-01T00:00:00Z", "2026-08-06T00:00:00Z"),
    ]
    conn.executemany("INSERT INTO ad_experiment VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute(
        "INSERT INTO ad_creative_group_evaluation VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("group-1", "launch-1", "D3", "{}", '{"exp-1":{},"exp-2":{}}', '["exp-1","exp-2"]', "exp-1", "PROVISIONAL", 3, "PASS", "{}", "2026-08-05T02:00:00Z"),
    )
    if include_unresolved:
        conn.execute(
            "INSERT INTO ad_experiment_evaluation VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "eval-unresolved", "exp-1", None, "D1", "{}", "{}", "{}", "{}",
                "UNKNOWN", None, None, "PENDING", "2026-08-05T01:00:00Z",
            ),
        )
    conn.commit()
    conn.close()


def _source_artifacts(tmp_path: Path, *, include_unresolved: bool = False) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "source.db"
    _database(database, include_unresolved=include_unresolved)
    audit_request = make_request(
        audit_id="audit-authority",
        data_cutoff_at="2026-08-07T00:00:00Z",
        captured_at="2026-08-07T01:00:00Z",
        source_logical_id="fixture-growth",
    )
    conn = open_readonly_snapshot(database)
    audit_bundle = build_audit(conn, audit_request, source_path=database)
    conn.close()
    audit_dir = tmp_path / "audit"
    write_audit_bundle(audit_bundle, audit_dir)
    audit_sha = _sha(audit_dir / "manifest.json")
    candidate = derive_lineage_candidates_from_audit_directory(
        audit_dir,
        expected_manifest_sha256=audit_sha,
        derivation_id="derive-authority",
        derived_at="2026-08-07T02:00:00Z",
    )
    candidate_dir = tmp_path / "candidate"
    write_lineage_candidate_bundle(
        candidate,
        candidate_dir,
        audit_dir=audit_dir,
        expected_manifest_sha256=audit_sha,
    )
    candidate_sha = _sha(candidate_dir / "manifest.json")
    request = build_authority_request(
        candidate_dir=candidate_dir,
        expected_candidate_manifest_sha256=candidate_sha,
        audit_dir=audit_dir,
        expected_audit_manifest_sha256=audit_sha,
        request_id="authority-request-1",
        requested_at="2026-08-07T03:00:00Z",
        evaluated_at="2026-08-07T05:00:00Z",
    )
    return {
        "audit_dir": audit_dir,
        "audit_sha": audit_sha,
        "candidate_dir": candidate_dir,
        "candidate_sha": candidate_sha,
        "request": request,
    }


def _public_key(private_key: Path) -> str:
    result = subprocess.run(
        ["openssl", "rsa", "-in", str(private_key), "-pubout"],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout


def _sign(private_key: Path, message: bytes) -> str:
    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(private_key)],
        input=message,
        capture_output=True,
        check=True,
    )
    return base64.b64encode(result.stdout).decode()


def _registry(tmp_path: Path, *, bits: int = 2048) -> tuple[dict, dict[str, Path]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    keys = []
    private_keys = {}
    for index, role in enumerate(REQUIRED_ROLES, start=1):
        key_path = tmp_path / f"key-{index}.pem"
        subprocess.run(
            ["openssl", "genrsa", "-out", str(key_path), str(bits)],
            capture_output=True,
            check=True,
        )
        key_id = f"key-{role.lower()}"
        private_keys[key_id] = key_path
        keys.append({
            "key_id": key_id,
            "signer_id": f"signer-{index}",
            "role": role,
            "purposes": [SIGNATURE_PURPOSE],
            "algorithm": SIGNATURE_ALGORITHM,
            "status": "ACTIVE",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_until": "2027-01-01T00:00:00Z",
            "public_key_pem": _public_key(key_path),
        })
    registry = {
        "schema_version": REGISTRY_VERSION,
        "registry_id": "lineage-authority-test-keys",
        "keys": keys,
    }
    registry["registry_hash"] = canonical_hash(registry)
    return registry, private_keys


def _valid_response(request: dict, registry: dict, private_keys: dict[str, Path]) -> dict:
    component = request["denominator"]["components"][0]
    entry = request["denominator"]["legacy_entries"][0]
    spec_hash = "a" * 64
    canonical_experiment_id = "canonical-study-001"
    node = {
        "lineage_id": lineage_id_for_root(canonical_experiment_id, spec_hash),
        "canonical_experiment_id": canonical_experiment_id,
        "component_id": component["component_id"],
        "member_legacy_experiment_ids": component["subject_experiment_ids"],
        "parent_canonical_experiment_id": None,
        "parent_component_id": None,
        "iteration_no": 1,
        "spec_hash": spec_hash,
        "parent_spec_hash": None,
        "candidate_entry_refs": [{
            "source_table": entry["source_table"],
            "source_id": entry["source_id"],
            "entry_hash": entry["entry_hash"],
        }],
        "authority_evidence_refs": [{
            "artifact_type": "signed-study-registry",
            "manifest_sha256": "b" * 64,
            "record_id": "study-root-001",
            "record_hash": "c" * 64,
            "evidence_class": "EXPLICIT_ROOT_DECLARATION",
        }],
    }
    response = {
        "schema_version": RESPONSE_VERSION,
        "contract_version": CONTRACT_VERSION,
        "authority_id": "authority-001",
        "request_hash": request["request_hash"],
        "authorized_at": "2026-08-07T04:00:00Z",
        "lineage_nodes": [node],
        "exclusions": [],
    }
    response["authority_payload_hash"] = canonical_hash(response)
    response["signatures"] = [{
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": key["key_id"],
        "signer_id": key["signer_id"],
        "role": key["role"],
        "purpose": SIGNATURE_PURPOSE,
        "object_hash": response["authority_payload_hash"],
        "key_registry_hash": registry["registry_hash"],
        "signed_at": response["authorized_at"],
        "signature_base64": _sign(
            private_keys[key["key_id"]],
            signature_message(
                response["authority_payload_hash"],
                key_registry_hash=registry["registry_hash"],
                key_id=key["key_id"],
                signer_id=key["signer_id"],
                role=key["role"],
            ),
        ),
    } for key in registry["keys"]]
    return response


def _evaluate(source: dict[str, object], response: dict | None, registry: dict | None = None) -> dict:
    return evaluate_authority_response(
        source["request"],
        response,
        trusted_key_registry=registry,
        expected_key_registry_hash=registry["registry_hash"] if registry else None,
        candidate_dir=source["candidate_dir"],
        expected_candidate_manifest_sha256=source["candidate_sha"],
        audit_dir=source["audit_dir"],
        expected_audit_manifest_sha256=source["audit_sha"],
    )


def _write(
    source: dict[str, object],
    fragment: dict,
    output: Path,
    *,
    response: dict | None = None,
    registry: dict | None = None,
) -> dict:
    return write_authority_artifacts(
        source["request"],
        fragment,
        output,
        response=response,
        trusted_key_registry=registry,
        expected_key_registry_hash=registry["registry_hash"] if registry else None,
        candidate_dir=source["candidate_dir"],
        expected_candidate_manifest_sha256=source["candidate_sha"],
        audit_dir=source["audit_dir"],
        expected_audit_manifest_sha256=source["audit_sha"],
    )


def test_unsigned_request_is_source_bound_and_has_no_dataset_effect(tmp_path: Path) -> None:
    source = _source_artifacts(tmp_path)
    request = source["request"]
    assert request["trust_status"] == "UNSIGNED_AUTHORITY_REQUEST"
    assert request["authority_effect"] == "NONE"
    assert request["denominator"]["legacy_entry_count"] == 1
    assert request["denominator"]["component_count"] == 1
    assert request["authority_contract"]["forbidden_inferences"] == [
        "ACCOUNT_MATCH", "CREATED_AT_PROXIMITY", "LAUNCH_TOKEN", "NAME_SIMILARITY",
        "OBJECT_ID_REUSE", "WINNER_RELATION",
    ]
    fragment = _evaluate(source, None)
    assert fragment["status"] == "MISSING"
    assert fragment["authority_effect"] == "NONE"
    assert fragment["split_assignments"] == []
    assert fragment["holdout_status"] == "LOCKED_NOT_ASSIGNED"
    assert fragment["gate1_effect"] == "NONE"


def test_request_rejects_rehashed_denominator_or_replaced_candidate_anchor(tmp_path: Path) -> None:
    source = _source_artifacts(tmp_path)
    forged = deepcopy(source["request"])
    forged["denominator"]["legacy_entries"][0]["subject_experiment_ids"] = ["exp-forged"]
    forged["denominator"]["subject_experiment_ids"] = ["exp-forged"]
    forged["denominator"]["denominator_hash"] = canonical_hash({
        key: value for key, value in forged["denominator"].items() if key != "denominator_hash"
    })
    forged["request_hash"] = canonical_hash({
        key: value for key, value in forged.items() if key != "request_hash"
    })
    with pytest.raises(ImmutableLineageAuthorityError, match="G102B2_REQUEST_SOURCE_SEMANTICS_MISMATCH"):
        validate_authority_request(
            forged,
            candidate_dir=source["candidate_dir"],
            expected_candidate_manifest_sha256=source["candidate_sha"],
            audit_dir=source["audit_dir"],
            expected_audit_manifest_sha256=source["audit_sha"],
        )
    with pytest.raises(ImmutableLineageAuthorityError, match="G102B2_CANDIDATE_MANIFEST_ANCHOR_MISMATCH"):
        load_validated_candidate_directory(
            source["candidate_dir"],
            expected_candidate_manifest_sha256="0" * 64,
            audit_dir=source["audit_dir"],
            expected_audit_manifest_sha256=source["audit_sha"],
        )


def test_three_purpose_bound_signatures_verify_authority_but_not_split(tmp_path: Path) -> None:
    source = _source_artifacts(tmp_path / "source")
    registry, private_keys = _registry(tmp_path / "keys")
    response = _valid_response(source["request"], registry, private_keys)
    fragment = _evaluate(source, response, registry)
    assert fragment["status"] == "VERIFIED"
    assert fragment["verified_roles"] == list(REQUIRED_ROLES)
    assert fragment["trust_status"] == "EXTERNALLY_SIGNED_AUTHORITY_ATTESTATION"
    assert fragment["authority_effect"] == "LINEAGE_AUTHORITY_ATTESTATION_VERIFIED"
    assert fragment["split_assignments"] == []
    assert fragment["replay_eligible"] is False
    assert fragment["golden_eligible"] is False
    assert fragment["not_gate_receipt"] is True


def test_signature_tamper_wrong_purpose_and_unpinned_registry_are_invalid(tmp_path: Path) -> None:
    source = _source_artifacts(tmp_path / "source")
    registry, private_keys = _registry(tmp_path / "keys")
    response = _valid_response(source["request"], registry, private_keys)
    tampered = deepcopy(response)
    tampered["signatures"][0]["signature_base64"] = base64.b64encode(b"not-a-signature").decode()
    assert _evaluate(source, tampered, registry)["status"] == "INVALID"
    wrong_purpose = deepcopy(response)
    wrong_purpose["signatures"][0]["purpose"] = "SPLIT_POLICY_APPROVAL"
    assert _evaluate(source, wrong_purpose, registry)["status"] == "INVALID"
    forged_registry = deepcopy(registry)
    forged_registry["registry_id"] = "inline-attacker-registry"
    forged_registry["registry_hash"] = canonical_hash({
        key: value for key, value in forged_registry.items() if key != "registry_hash"
    })
    fragment = evaluate_authority_response(
        source["request"], response,
        trusted_key_registry=forged_registry,
        expected_key_registry_hash=registry["registry_hash"],
        candidate_dir=source["candidate_dir"],
        expected_candidate_manifest_sha256=source["candidate_sha"],
        audit_dir=source["audit_dir"],
        expected_audit_manifest_sha256=source["audit_sha"],
    )
    assert fragment["status"] == "INVALID"
    weak_registry, weak_private_keys = _registry(tmp_path / "weak-keys", bits=1024)
    weak_response = _valid_response(source["request"], weak_registry, weak_private_keys)
    assert _evaluate(source, weak_response, weak_registry)["status"] == "INVALID"
    shared_registry, shared_private_keys = _registry(tmp_path / "shared-keys")
    first_key_id = shared_registry["keys"][0]["key_id"]
    first_public_key = shared_registry["keys"][0]["public_key_pem"]
    first_private_key = shared_private_keys[first_key_id]
    for key in shared_registry["keys"]:
        key["public_key_pem"] = first_public_key
        shared_private_keys[key["key_id"]] = first_private_key
    shared_registry["registry_hash"] = canonical_hash({
        key: value for key, value in shared_registry.items() if key != "registry_hash"
    })
    shared_response = _valid_response(source["request"], shared_registry, shared_private_keys)
    assert _evaluate(source, shared_response, shared_registry)["status"] == "INVALID"
    future_key_registry, future_private_keys = _registry(tmp_path / "future-keys")
    for key in future_key_registry["keys"]:
        key["valid_from"] = "2026-08-07T04:30:00Z"
    future_key_registry["registry_hash"] = canonical_hash({
        key: value for key, value in future_key_registry.items() if key != "registry_hash"
    })
    future_key_response = _valid_response(
        source["request"], future_key_registry, future_private_keys,
    )
    assert _evaluate(source, future_key_response, future_key_registry)["status"] == "INVALID"


def test_external_evaluation_clock_rejects_future_request_or_authorization(tmp_path: Path) -> None:
    source = _source_artifacts(tmp_path / "source")
    with pytest.raises(ImmutableLineageAuthorityError, match="G102B2_REQUEST_BEFORE_CUTOFF"):
        build_authority_request(
            candidate_dir=source["candidate_dir"],
            expected_candidate_manifest_sha256=source["candidate_sha"],
            audit_dir=source["audit_dir"],
            expected_audit_manifest_sha256=source["audit_sha"],
            request_id="authority-request-future",
            requested_at="2026-08-07T06:00:00Z",
            evaluated_at="2026-08-07T05:00:00Z",
        )
    registry, private_keys = _registry(tmp_path / "keys")
    response = _valid_response(source["request"], registry, private_keys)
    response["authorized_at"] = "2026-08-07T06:00:00Z"
    response["authority_payload_hash"] = canonical_hash({
        key: value for key, value in response.items() if key not in {"authority_payload_hash", "signatures"}
    })
    response["signatures"] = [{
        **signature,
        "object_hash": response["authority_payload_hash"],
        "signed_at": response["authorized_at"],
        "signature_base64": _sign(
            private_keys[signature["key_id"]],
            signature_message(
                response["authority_payload_hash"],
                key_registry_hash=registry["registry_hash"],
                key_id=signature["key_id"],
                signer_id=signature["signer_id"],
                role=signature["role"],
            ),
        ),
    } for signature in response["signatures"]]
    assert _evaluate(source, response, registry)["status"] == "INVALID"


def test_denominator_omission_and_component_drift_are_conflicts(tmp_path: Path) -> None:
    source = _source_artifacts(tmp_path / "source")
    registry, private_keys = _registry(tmp_path / "keys")
    response = _valid_response(source["request"], registry, private_keys)
    omitted = deepcopy(response)
    omitted["lineage_nodes"][0]["candidate_entry_refs"] = []
    omitted["authority_payload_hash"] = canonical_hash({
        key: value for key, value in omitted.items() if key not in {"authority_payload_hash", "signatures"}
    })
    omitted["signatures"] = []
    assert _evaluate(source, omitted, registry)["status"] == "CONFLICT"
    drifted = deepcopy(response)
    drifted["lineage_nodes"][0]["member_legacy_experiment_ids"] = ["exp-1"]
    drifted["authority_payload_hash"] = canonical_hash({
        key: value for key, value in drifted.items() if key not in {"authority_payload_hash", "signatures"}
    })
    drifted["signatures"] = []
    assert _evaluate(source, drifted, registry)["status"] == "CONFLICT"


def test_unresolved_entry_requires_named_exclusion_and_stays_in_denominator(tmp_path: Path) -> None:
    source = _source_artifacts(tmp_path / "source", include_unresolved=True)
    registry, private_keys = _registry(tmp_path / "keys")
    response = _valid_response(source["request"], registry, private_keys)
    unresolved = next(
        item for item in source["request"]["denominator"]["legacy_entries"]
        if item["component_id"] is None
    )
    exclusion = {
        "exclusion_id": "",
        "component_ids": [],
        "candidate_entry_refs": [{
            "source_table": unresolved["source_table"],
            "source_id": unresolved["source_id"],
            "entry_hash": unresolved["entry_hash"],
        }],
        "reason_codes": ["LINEAGE_UNRESOLVED"],
        "authority_evidence_refs": [{
            "artifact_type": "signed-study-registry",
            "manifest_sha256": "d" * 64,
            "record_id": "named-exclusion-001",
            "record_hash": "e" * 64,
            "evidence_class": "NAMED_EXCLUSION",
        }],
    }
    exclusion["exclusion_id"] = "exclusion_" + canonical_hash({
        "component_ids": exclusion["component_ids"],
        "candidate_entry_refs": exclusion["candidate_entry_refs"],
        "reason_codes": exclusion["reason_codes"],
    })[:24]
    response["exclusions"] = [exclusion]
    response["authority_payload_hash"] = canonical_hash({
        key: value for key, value in response.items() if key not in {"authority_payload_hash", "signatures"}
    })
    response["signatures"] = [{
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": key["key_id"],
        "signer_id": key["signer_id"],
        "role": key["role"],
        "purpose": SIGNATURE_PURPOSE,
        "object_hash": response["authority_payload_hash"],
        "key_registry_hash": registry["registry_hash"],
        "signed_at": response["authorized_at"],
        "signature_base64": _sign(
            private_keys[key["key_id"]],
            signature_message(
                response["authority_payload_hash"],
                key_registry_hash=registry["registry_hash"],
                key_id=key["key_id"],
                signer_id=key["signer_id"],
                role=key["role"],
            ),
        ),
    } for key in registry["keys"]]
    assert source["request"]["denominator"]["legacy_entry_count"] == 2
    assert _evaluate(source, response, registry)["status"] == "VERIFIED"


def test_false_parent_and_inference_fields_cannot_be_smuggled(tmp_path: Path) -> None:
    source = _source_artifacts(tmp_path / "source")
    registry, private_keys = _registry(tmp_path / "keys")
    response = _valid_response(source["request"], registry, private_keys)
    false_parent = deepcopy(response)
    node = false_parent["lineage_nodes"][0]
    node["parent_canonical_experiment_id"] = "canonical-study-missing"
    node["parent_component_id"] = "component_missing"
    node["parent_spec_hash"] = "d" * 64
    node["iteration_no"] = 2
    node["authority_evidence_refs"][0]["evidence_class"] = "EXPLICIT_PARENT_EDGE"
    false_parent["authority_payload_hash"] = canonical_hash({
        key: value for key, value in false_parent.items() if key not in {"authority_payload_hash", "signatures"}
    })
    false_parent["signatures"] = []
    assert _evaluate(source, false_parent, registry)["status"] == "CONFLICT"
    inferred = deepcopy(response)
    inferred["lineage_nodes"][0]["launch_token"] = "launch_guess"
    inferred["authority_payload_hash"] = canonical_hash({
        key: value for key, value in inferred.items() if key not in {"authority_payload_hash", "signatures"}
    })
    inferred["signatures"] = []
    assert _evaluate(source, inferred, registry)["status"] == "INVALID"


def test_writer_is_new_directory_only_and_preserves_missing_ceiling(tmp_path: Path) -> None:
    source = _source_artifacts(tmp_path / "source")
    fragment = _evaluate(source, None)
    output = tmp_path / "output"
    manifest = _write(source, fragment, output)
    assert manifest["status"] == "MISSING"
    assert manifest["split_effect"] == "NONE"
    assert manifest["not_dataset_receipt"] is True
    assert json.loads((output / "authority-response.json").read_text()) is None
    assert json.loads((output / "trusted-key-registry.json").read_text()) is None
    with pytest.raises(ImmutableLineageAuthorityError, match="G102B2_OUTPUT_EXISTS"):
        _write(source, fragment, output)


def test_writer_rederives_fragment_and_persists_signature_evidence(tmp_path: Path) -> None:
    source = _source_artifacts(tmp_path / "source")
    registry, private_keys = _registry(tmp_path / "keys")
    response = _valid_response(source["request"], registry, private_keys)
    fragment = _evaluate(source, response, registry)
    forged = deepcopy(fragment)
    forged["response_hash"] = "f" * 64
    forged["fragment_hash"] = canonical_hash({
        key: value for key, value in forged.items() if key != "fragment_hash"
    })
    with pytest.raises(ImmutableLineageAuthorityError, match="G102B2_FRAGMENT_DERIVATION_MISMATCH"):
        _write(source, forged, tmp_path / "forged", response=response, registry=registry)
    output = tmp_path / "verified"
    manifest = _write(source, fragment, output, response=response, registry=registry)
    assert manifest["status"] == "VERIFIED"
    assert json.loads((output / "authority-response.json").read_text())["signatures"]
    assert json.loads((output / "trusted-key-registry.json").read_text())["registry_hash"] == registry["registry_hash"]
    loaded_request, loaded_fragment = load_validated_authority_directory(
        output,
        expected_authority_manifest_sha256=_sha(output / "manifest.json"),
        expected_key_registry_hash=registry["registry_hash"],
        candidate_dir=source["candidate_dir"],
        expected_candidate_manifest_sha256=source["candidate_sha"],
        audit_dir=source["audit_dir"],
        expected_audit_manifest_sha256=source["audit_sha"],
    )
    assert loaded_request == source["request"]
    assert loaded_fragment == fragment
    with pytest.raises(ImmutableLineageAuthorityError, match="G102B2_ARTIFACT_KEY_REGISTRY_ANCHOR_MISMATCH"):
        load_validated_authority_directory(
            output,
            expected_authority_manifest_sha256=_sha(output / "manifest.json"),
            expected_key_registry_hash="0" * 64,
            candidate_dir=source["candidate_dir"],
            expected_candidate_manifest_sha256=source["candidate_sha"],
            audit_dir=source["audit_dir"],
            expected_audit_manifest_sha256=source["audit_sha"],
        )
    tampered_manifest = json.loads((output / "manifest.json").read_text())
    tampered_manifest["gate1_effect"] = "PASS"
    tampered_manifest["manifest_hash"] = canonical_hash({
        key: value for key, value in tampered_manifest.items() if key != "manifest_hash"
    })
    (output / "manifest.json").write_text(
        json.dumps(tampered_manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ImmutableLineageAuthorityError, match="G102B2_ARTIFACT_MANIFEST_BINDING_MISMATCH"):
        load_validated_authority_directory(
            output,
            expected_authority_manifest_sha256=_sha(output / "manifest.json"),
            expected_key_registry_hash=registry["registry_hash"],
            candidate_dir=source["candidate_dir"],
            expected_candidate_manifest_sha256=source["candidate_sha"],
            audit_dir=source["audit_dir"],
            expected_audit_manifest_sha256=source["audit_sha"],
        )


def test_cli_emits_missing_artifact_and_exit_two(tmp_path: Path) -> None:
    source = _source_artifacts(tmp_path / "source")
    output = tmp_path / "cli-output"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/validate_gle_lineage_authority.py",
            "--audit-dir", str(source["audit_dir"]),
            "--expected-audit-manifest-sha256", source["audit_sha"],
            "--candidate-dir", str(source["candidate_dir"]),
            "--expected-candidate-manifest-sha256", source["candidate_sha"],
            "--request-id", "authority-request-cli",
            "--requested-at", "2026-08-07T03:00:00Z",
            "--evaluated-at", "2026-08-07T05:00:00Z",
            "--output-dir", str(output),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "MISSING"
    assert (output / "authority-request.json").is_file()
    assert (output / "authority-fragment.json").is_file()
    assert not (output / "assignments.ndjson").exists()


def test_cli_rejects_noncanonical_or_duplicate_key_json(tmp_path: Path) -> None:
    source = _source_artifacts(tmp_path / "source")
    response_path = tmp_path / "response.json"
    registry_path = tmp_path / "registry.json"
    response_path.write_text('{"schema_version":"x","schema_version":"y"}\n', encoding="utf-8")
    registry_path.write_text("{}\n", encoding="utf-8")
    output = tmp_path / "invalid-output"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/validate_gle_lineage_authority.py",
            "--audit-dir", str(source["audit_dir"]),
            "--expected-audit-manifest-sha256", source["audit_sha"],
            "--candidate-dir", str(source["candidate_dir"]),
            "--expected-candidate-manifest-sha256", source["candidate_sha"],
            "--request-id", "authority-request-invalid-json",
            "--requested-at", "2026-08-07T03:00:00Z",
            "--evaluated-at", "2026-08-07T05:00:00Z",
            "--response", str(response_path),
            "--trusted-key-registry", str(registry_path),
            "--expected-key-registry-hash", "0" * 64,
            "--output-dir", str(output),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 64
    assert result.stderr.strip() == "G102B2_CLI_JSON_INVALID"
    assert not output.exists()
