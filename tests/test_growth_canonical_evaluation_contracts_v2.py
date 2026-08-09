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

from app.growth.canonical_evaluation_contracts import INVARIANT_FIELDS
from app.growth.canonical_evaluation_contracts_v2 import (
    BUNDLE_VERSION_V2,
    CEILING,
    INVARIANT_PROJECTION_VERSION_V2,
    OBJECTIVE_VERSION_V2,
    SNAPSHOT_VERSION_V2,
    SPEC_VERSION_V2,
    CanonicalEvaluationContractV2Error,
    canonical_json,
    content_hash,
    validate_canonical_input_bundle_v2,
    validate_experiment_spec_v2,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_gle_canonical_contracts_v2.py"
H = "a" * 64


def _objective() -> dict:
    value = {
        "schema_version": OBJECTIVE_VERSION_V2,
        "objective_contract_id": "objective-mx-v2",
        "version": 2,
        "contract_hash": H,
        "ad_account_id": "1012060198097836",
        "market": "MX",
        "currency": "USD",
        "business_goal": "REDUCE_QUALIFIED_JOIN_CPA",
        "primary_metric": {
            "metric_key": "QUALIFIED_JOIN_CPA",
            "definition_version": "gle-qualified-join-cpa-v1",
            "metric_contract_hash": "b" * 64,
            "attribution_version": "gle-exact-meta-canonical-attribution-v1",
            "attribution_window": "click-14d",
            "dedup_version": "gle-exact-event-canonical-identity-v1",
            "qualification_rule_version": "tugaofunnel-guild-join-success-v1",
            "min_business_improvement": 0.3,
            "direction": "LOWER_IS_BETTER",
        },
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
        "approved_by": "Chauncey",
        "approved_at": "2026-08-07T00:00:00Z",
        "approval_claim_status": "CALLER_DECLARED_NOT_GOVERNANCE_VERIFIED",
        "authority_effect": "NONE",
    }
    value["contract_hash"] = content_hash(value, "contract_hash")
    return value


def _invariant() -> dict:
    value = {
        "schema_version": INVARIANT_PROJECTION_VERSION_V2,
        "experiment_id": "experiment-v2-1",
        "invariant_field_hashes": {field: H for field in INVARIANT_FIELDS},
        "projection_hash": H,
    }
    value["projection_hash"] = content_hash(value, "projection_hash")
    return value


def _cell(cell_id: str, role: str, suffix: str) -> dict:
    return {
        "cell_id": cell_id,
        "role": role,
        "copy_version_id": f"copy-{suffix}",
        "image_sha": H,
        "config_hash": ("c" if suffix == "1" else "d") * 64,
        "meta_campaign_id": "campaign-1",
        "meta_adset_id": f"adset-{suffix}",
        "meta_creative_id": f"creative-{suffix}",
        "meta_ad_id": f"ad-{suffix}",
        "meta_assignment_cell_id": f"study-cell-{suffix}",
        "target_allocation": 0.5,
        "actual_allocation": 0.5,
        "allocation_verified_at": "2026-08-07T01:00:00Z",
    }


def _spec(obj: dict | None = None, invariant: dict | None = None) -> dict:
    obj = obj or _objective()
    invariant = invariant or _invariant()
    value = {
        "schema_version": SPEC_VERSION_V2,
        "experiment_id": "experiment-v2-1",
        "study_id": "study-v2-1",
        "lineage_id": "lineage-v2-1",
        "parent_experiment_id": None,
        "iteration_no": 1,
        "objective_contract_id": obj["objective_contract_id"],
        "objective_contract_hash": obj["contract_hash"],
        "experiment_type": "COPY_ONLY",
        "evidence_target": "CONTROLLED",
        "unique_variable": "PRIMARY_TEXT",
        "invariant_fields": INVARIANT_FIELDS,
        "invariant_config_hash": invariant["projection_hash"],
        "assignment": {
            "mechanism": "META_SPLIT_TEST",
            "capability_assessment_id": "capability-v2-1",
            "target_allocation": {"cell-c1": 0.5, "cell-c2": 0.5},
            "allowed_allocation_deviation": 0.1,
            "readback_required": True,
            "readback_evidence_sha256": "e" * 64,
        },
        "cells": [_cell("cell-c1", "CHAMPION", "1"), _cell("cell-c2", "CHALLENGER", "2")],
        "power_plan": {
            "power_assessment_id": "power-v2-1",
            "alpha": 0.05,
            "power": 0.8,
            "mde": 0.3,
            "target_information": 114.024535562432,
            "earliest_binding_information_fraction": 1.0,
            "hard_deadline_at": "2026-08-21T00:00:00Z",
            "max_test_budget": 20,
        },
        "evaluation_plan": {
            "method": "GROUP_SEQUENTIAL_FREQUENTIST",
            "boundary_family": "OBRIEN_FLEMING",
            "method_version": "gle-obf-v1",
            "d1_role": "SAFETY_CHECK",
            "d3_role": "TREND_ONLY",
            "final_role": "BINDING_EFFECT_DECISION",
            "policy_version": "gle-evaluation-policy-v1",
        },
        "status": "APPROVED_SHAPE_CANDIDATE",
        "authority_attestation_ref": {
            "authority_id": "spec-authority-v2-1",
            "authority_manifest_sha256": "f" * 64,
            "objective_attestation_manifest_sha256": "1" * 64,
        },
        "authority_validation_status": "UNVERIFIED_REFERENCE_ONLY",
        "authority_effect": "NONE",
        "spec_hash": H,
        "created_at": "2026-08-07T00:30:00Z",
        "approved_at": "2026-08-07T00:45:00Z",
    }
    value["spec_hash"] = content_hash(value, "spec_hash")
    return value


def _snapshot(obj: dict | None = None, spec: dict | None = None) -> dict:
    obj = obj or _objective()
    spec = spec or _spec(obj)
    metric = obj["primary_metric"]
    facts = {
        "spend": 10,
        "impressions": 1000,
        "clicks": 20,
        "installs": 5,
        "qualified_joins": 2,
        "invalid_users": 0,
        "allocation_share": 0.5,
    }
    value = {
        "schema_version": SNAPSHOT_VERSION_V2,
        "snapshot_id": "synthetic-snapshot-v2-1",
        "experiment_id": spec["experiment_id"],
        "checkpoint": "D1",
        "data_cutoff_at": "2026-08-08T00:00:00Z",
        "created_at": "2026-08-08T00:00:01Z",
        "experiment_spec_hash": spec["spec_hash"],
        "objective_contract_hash": obj["contract_hash"],
        "evaluator_version": spec["evaluation_plan"]["method_version"],
        "policy_version": spec["evaluation_plan"]["policy_version"],
        "metric_definition_version": metric["definition_version"],
        "metric_contract_hash": metric["metric_contract_hash"],
        "attribution_version": metric["attribution_version"],
        "attribution_window": metric["attribution_window"],
        "dedup_version": metric["dedup_version"],
        "qualification_rule_version": metric["qualification_rule_version"],
        "allocation_basis": "ACTUAL_READBACK",
        "allocation_readback_evidence_sha256": spec["assignment"]["readback_evidence_sha256"],
        "allocation_verified_at": spec["cells"][0]["allocation_verified_at"],
        "cell_metrics": {"cell-c1": dict(facts), "cell-c2": dict(facts)},
        "data_quality": {
            "freshness_ok": True,
            "attribution_coverage": 1,
            "missing_sources": [],
            "duplicate_rate": 0,
        },
        "mutation_events": [],
        "source_validation_status": "SYNTHETIC_FIXTURE_ONLY",
        "snapshot_effect": "NONE",
        "snapshot_hash": H,
    }
    value["snapshot_hash"] = content_hash(value, "snapshot_hash")
    return value


def _bundle() -> dict:
    obj = _objective()
    invariant = _invariant()
    spec = _spec(obj, invariant)
    snapshot = _snapshot(obj, spec)
    value = {
        "schema_version": BUNDLE_VERSION_V2,
        "bundle_purpose": "SYNTHETIC_CONTRACT_VALIDATION",
        "objective_contract": obj,
        "invariant_projection": invariant,
        "experiment_spec": spec,
        "input_snapshot": snapshot,
        "validation_ceiling": copy.deepcopy(CEILING),
        "bundle_hash": H,
    }
    value["bundle_hash"] = content_hash(value, "bundle_hash")
    return value


def _rehash(value: dict) -> None:
    value["bundle_hash"] = content_hash(value, "bundle_hash")


def _write_bundle(path: Path, value: dict | None = None) -> str:
    raw = (canonical_json(value or _bundle()) + "\n").encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return hashlib.sha256(raw).hexdigest()


def _cli(path: Path, digest: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--bundle", str(path), "--expected-sha256", digest],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_v2_separates_metric_definition_attribution_and_keeps_all_effects_closed() -> None:
    value = _bundle()
    assert value["objective_contract"]["primary_metric"]["definition_version"] != value["input_snapshot"]["attribution_version"]
    assert validate_canonical_input_bundle_v2(value) == value
    assert value["validation_ceiling"] == CEILING
    assert not any(value["validation_ceiling"][field] for field in (
        "snapshot_emitted", "replay_executed", "replay_eligible", "golden_eligible",
    ))


@pytest.mark.parametrize("field", [
    "metric_definition_version", "metric_contract_hash", "attribution_version",
    "attribution_window", "dedup_version", "qualification_rule_version",
])
def test_metric_binding_tamper_with_full_rehash_is_rejected(field: str) -> None:
    value = _bundle()
    value["input_snapshot"][field] = "2" * 64 if field == "metric_contract_hash" else "wrong-v2"
    value["input_snapshot"]["snapshot_hash"] = content_hash(value["input_snapshot"], "snapshot_hash")
    _rehash(value)
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_BUNDLE_METRIC_BINDING_INVALID"):
        validate_canonical_input_bundle_v2(value)


def test_v1_objects_and_borrowed_object_hashes_do_not_implicitly_upgrade() -> None:
    from tests.test_growth_canonical_evaluation_contracts import (
        invariant_projection as invariant_v1,
        objective as objective_v1,
        snapshot as snapshot_v1,
        spec as spec_v1,
    )

    value = _bundle()
    value["objective_contract"] = objective_v1()
    _rehash(value)
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_OBJECTIVE_SCHEMA_INVALID|G101C_OBJECTIVE_VERSION_MISMATCH"):
        validate_canonical_input_bundle_v2(value)
    for field, old_value, error in (
        ("invariant_projection", invariant_v1(), "G101C_INVARIANT_VERSION_MISMATCH"),
        ("experiment_spec", spec_v1(), "G101C_SPEC_SCHEMA_INVALID"),
        ("input_snapshot", snapshot_v1(), "G101C_SNAPSHOT_SCHEMA_INVALID"),
    ):
        value = _bundle()
        value[field] = old_value
        _rehash(value)
        with pytest.raises(CanonicalEvaluationContractV2Error, match=error):
            validate_canonical_input_bundle_v2(value)
    value = _bundle()
    value["experiment_spec"]["objective_contract_hash"] = "3" * 64
    value["experiment_spec"]["spec_hash"] = content_hash(value["experiment_spec"], "spec_hash")
    value["input_snapshot"]["experiment_spec_hash"] = value["experiment_spec"]["spec_hash"]
    value["input_snapshot"]["snapshot_hash"] = content_hash(value["input_snapshot"], "snapshot_hash")
    _rehash(value)
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_BUNDLE_OBJECT_BINDING_INVALID"):
        validate_canonical_input_bundle_v2(value)


def test_spec_status_lattice_represents_shapes_without_granting_authority() -> None:
    approved = _spec()
    approved["evaluation_plan"]["method_version"] = "UNFROZEN"
    approved["spec_hash"] = content_hash(approved, "spec_hash")
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_APPROVED_SPEC_VERSION_UNFROZEN"):
        validate_experiment_spec_v2(approved)
    draft = _spec()
    draft.update({"status": "DRAFT", "approved_at": None, "authority_attestation_ref": None})
    draft["evaluation_plan"].update({"method_version": "UNFROZEN", "policy_version": "UNFROZEN"})
    for cell in draft["cells"]:
        cell.update({"actual_allocation": None, "allocation_verified_at": None})
    draft["assignment"]["readback_evidence_sha256"] = None
    draft["spec_hash"] = content_hash(draft, "spec_hash")
    assert validate_experiment_spec_v2(draft) == draft
    noncanonical_draft = copy.deepcopy(draft)
    noncanonical_draft["evaluation_plan"]["method_version"] = "unfrozen"
    noncanonical_draft["spec_hash"] = content_hash(noncanonical_draft, "spec_hash")
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_EVALUATION_VERSION_INVALID"):
        validate_experiment_spec_v2(noncanonical_draft)
    frozen_draft = copy.deepcopy(draft)
    frozen_draft["evaluation_plan"].update({
        "method_version": "gle-obf-v1", "policy_version": "gle-policy-v1",
    })
    frozen_draft["spec_hash"] = content_hash(frozen_draft, "spec_hash")
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_DRAFT_VERSION_STATE_INVALID"):
        validate_experiment_spec_v2(frozen_draft)
    candidate = copy.deepcopy(draft)
    candidate.update({"status": "AUTHORITY_CANDIDATE", "authority_attestation_ref": _spec()["authority_attestation_ref"]})
    candidate["evaluation_plan"].update({"method_version": "gle-obf-v1", "policy_version": "gle-policy-v1"})
    candidate["spec_hash"] = content_hash(candidate, "spec_hash")
    assert validate_experiment_spec_v2(candidate) == candidate
    candidate["cells"] = copy.deepcopy(_spec()["cells"])
    candidate["assignment"]["readback_evidence_sha256"] = "e" * 64
    candidate["spec_hash"] = content_hash(candidate, "spec_hash")
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_UNAPPROVED_ACTUAL_ALLOCATION_FORBIDDEN"):
        validate_experiment_spec_v2(candidate)


@pytest.mark.parametrize("mutation,error", [
    ("missing_evidence", "G101C_ACTUAL_ALLOCATION_READBACK_INVALID"),
    ("time_mismatch", "G101C_READBACK_TIME_MISMATCH"),
    ("identity_missing", "G101C_CELL_META_ID_INVALID"),
    ("sum_invalid", "G101C_ACTUAL_ALLOCATION_INVALID"),
    ("deviation", "G101C_ACTUAL_ALLOCATION_DEVIATION_EXCEEDED"),
])
def test_actual_allocation_requires_complete_physical_readback(mutation: str, error: str) -> None:
    value = _spec()
    if mutation == "missing_evidence":
        value["assignment"]["readback_evidence_sha256"] = None
    elif mutation == "time_mismatch":
        value["cells"][1]["allocation_verified_at"] = "2026-08-07T01:00:01Z"
    elif mutation == "identity_missing":
        value["cells"][0]["meta_ad_id"] = None
    elif mutation == "sum_invalid":
        value["cells"][0]["actual_allocation"] = 0.6
    else:
        value["cells"][0]["actual_allocation"] = 0.7
        value["cells"][1]["actual_allocation"] = 0.3
    value["spec_hash"] = content_hash(value, "spec_hash")
    with pytest.raises(CanonicalEvaluationContractV2Error, match=error):
        validate_experiment_spec_v2(value)


def test_snapshot_allocation_readback_time_and_invariant_binding_are_cross_checked() -> None:
    value = _bundle()
    value["input_snapshot"]["cell_metrics"]["cell-c1"]["allocation_share"] = 0.8
    value["input_snapshot"]["cell_metrics"]["cell-c2"]["allocation_share"] = 0.2
    value["input_snapshot"]["snapshot_hash"] = content_hash(value["input_snapshot"], "snapshot_hash")
    _rehash(value)
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_BUNDLE_ALLOCATION_READBACK_BINDING_INVALID"):
        validate_canonical_input_bundle_v2(value)
    value = _bundle()
    value["experiment_spec"].update({
        "status": "AUTHORITY_CANDIDATE", "approved_at": None,
    })
    for cell in value["experiment_spec"]["cells"]:
        cell.update({"actual_allocation": None, "allocation_verified_at": None})
    value["experiment_spec"]["assignment"]["readback_evidence_sha256"] = None
    value["experiment_spec"]["spec_hash"] = content_hash(value["experiment_spec"], "spec_hash")
    value["input_snapshot"].update({
        "experiment_spec_hash": value["experiment_spec"]["spec_hash"],
        "allocation_basis": "SYNTHETIC_TARGET_FIXTURE",
        "allocation_readback_evidence_sha256": None,
        "allocation_verified_at": None,
    })
    value["input_snapshot"]["cell_metrics"]["cell-c1"]["allocation_share"] = 0.8
    value["input_snapshot"]["cell_metrics"]["cell-c2"]["allocation_share"] = 0.2
    value["input_snapshot"]["snapshot_hash"] = content_hash(value["input_snapshot"], "snapshot_hash")
    _rehash(value)
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_BUNDLE_ALLOCATION_DEVIATION_EXCEEDED"):
        validate_canonical_input_bundle_v2(value)
    value = _bundle()
    value["experiment_spec"]["cells"][0]["allocation_verified_at"] = "2026-08-08T00:00:01Z"
    value["experiment_spec"]["cells"][1]["allocation_verified_at"] = "2026-08-08T00:00:01Z"
    value["experiment_spec"]["spec_hash"] = content_hash(value["experiment_spec"], "spec_hash")
    value["input_snapshot"]["experiment_spec_hash"] = value["experiment_spec"]["spec_hash"]
    value["input_snapshot"]["allocation_verified_at"] = "2026-08-08T00:00:01Z"
    value["input_snapshot"]["snapshot_hash"] = content_hash(value["input_snapshot"], "snapshot_hash")
    _rehash(value)
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_BUNDLE_READBACK_AFTER_CUTOFF"):
        validate_canonical_input_bundle_v2(value)
    value = _bundle()
    value["invariant_projection"]["invariant_field_hashes"]["IMAGE_SHA"] = "9" * 64
    value["invariant_projection"]["projection_hash"] = content_hash(value["invariant_projection"], "projection_hash")
    value["experiment_spec"]["invariant_config_hash"] = value["invariant_projection"]["projection_hash"]
    value["experiment_spec"]["spec_hash"] = content_hash(value["experiment_spec"], "spec_hash")
    value["input_snapshot"]["experiment_spec_hash"] = value["experiment_spec"]["spec_hash"]
    value["input_snapshot"]["snapshot_hash"] = content_hash(value["input_snapshot"], "snapshot_hash")
    _rehash(value)
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_BUNDLE_OBJECT_BINDING_INVALID"):
        validate_canonical_input_bundle_v2(value)
    value = _bundle()
    value["experiment_spec"]["cells"][0]["actual_allocation"] = 0.6
    value["experiment_spec"]["cells"][1]["actual_allocation"] = 0.4
    value["experiment_spec"]["spec_hash"] = content_hash(value["experiment_spec"], "spec_hash")
    value["input_snapshot"]["experiment_spec_hash"] = value["experiment_spec"]["spec_hash"]
    value["input_snapshot"]["snapshot_hash"] = content_hash(value["input_snapshot"], "snapshot_hash")
    _rehash(value)
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_BUNDLE_ALLOCATION_READBACK_BINDING_INVALID"):
        validate_canonical_input_bundle_v2(value)


def test_readback_cannot_predate_approval_and_cutoff_cannot_exceed_hard_deadline() -> None:
    value = _spec()
    for cell in value["cells"]:
        cell["allocation_verified_at"] = "2026-08-07T00:40:00Z"
    value["spec_hash"] = content_hash(value, "spec_hash")
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_READBACK_BEFORE_APPROVAL"):
        validate_experiment_spec_v2(value)
    bundle = _bundle()
    bundle["input_snapshot"]["data_cutoff_at"] = "2026-08-21T00:00:01Z"
    bundle["input_snapshot"]["created_at"] = "2026-08-21T00:00:01Z"
    bundle["input_snapshot"]["snapshot_hash"] = content_hash(bundle["input_snapshot"], "snapshot_hash")
    _rehash(bundle)
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_BUNDLE_CUTOFF_AFTER_HARD_DEADLINE"):
        validate_canonical_input_bundle_v2(bundle)


def test_unfrozen_primary_versions_and_untyped_missing_sources_fail_closed() -> None:
    value = _bundle()
    value["objective_contract"]["primary_metric"]["attribution_version"] = "UNFROZEN"
    value["objective_contract"]["contract_hash"] = content_hash(value["objective_contract"], "contract_hash")
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_PRIMARY_METRIC_VERSION_UNFROZEN"):
        validate_canonical_input_bundle_v2(value)
    value = _bundle()
    value["input_snapshot"]["data_quality"]["missing_sources"] = [1]
    value["input_snapshot"]["snapshot_hash"] = content_hash(value["input_snapshot"], "snapshot_hash")
    _rehash(value)
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_MISSING_SOURCES_INVALID"):
        validate_canonical_input_bundle_v2(value)


@pytest.mark.parametrize(("target", "reserved", "error"), [
    ("primary", "UNKNOWN", "G101C_PRIMARY_METRIC_VERSION_UNFROZEN"),
    ("primary", "unknown", "G101C_PRIMARY_METRIC_VERSION_UNFROZEN"),
    ("secondary", "TbD", "G101C_SECONDARY_VERSION_INVALID"),
    ("mechanism", "pending", "G101C_ASSIGNMENT_MECHANISM_UNFROZEN"),
    ("evaluation", "UNKNOWN", "G101C_EVALUATION_VERSION_INVALID"),
    ("evaluation", "unfrozen", "G101C_EVALUATION_VERSION_INVALID"),
    ("evaluation", "UnSeT", "G101C_EVALUATION_VERSION_INVALID"),
])
def test_unknown_reserved_versions_fail_after_complete_rehash(target: str, reserved: str, error: str) -> None:
    value = _bundle()
    if target == "primary":
        value["objective_contract"]["primary_metric"]["attribution_version"] = reserved
        value["input_snapshot"]["attribution_version"] = reserved
    elif target == "secondary":
        value["objective_contract"]["secondary_metrics"][0]["definition_version"] = reserved
    elif target == "mechanism":
        value["experiment_spec"]["assignment"]["mechanism"] = reserved
    else:
        value["experiment_spec"]["evaluation_plan"]["method_version"] = reserved
        value["input_snapshot"]["evaluator_version"] = reserved
    value["objective_contract"]["contract_hash"] = content_hash(value["objective_contract"], "contract_hash")
    value["experiment_spec"]["objective_contract_hash"] = value["objective_contract"]["contract_hash"]
    value["experiment_spec"]["spec_hash"] = content_hash(value["experiment_spec"], "spec_hash")
    value["input_snapshot"]["objective_contract_hash"] = value["objective_contract"]["contract_hash"]
    value["input_snapshot"]["experiment_spec_hash"] = value["experiment_spec"]["spec_hash"]
    value["input_snapshot"]["snapshot_hash"] = content_hash(value["input_snapshot"], "snapshot_hash")
    _rehash(value)
    with pytest.raises(CanonicalEvaluationContractV2Error, match=error):
        validate_canonical_input_bundle_v2(value)


def test_ceiling_promotion_and_risk_expansion_fail_after_full_rehash() -> None:
    value = _bundle()
    value["validation_ceiling"]["replay_eligible"] = True
    _rehash(value)
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_BUNDLE_CEILING_INVALID"):
        validate_canonical_input_bundle_v2(value)
    value = _bundle()
    value["experiment_spec"]["power_plan"]["max_test_budget"] = 21
    value["experiment_spec"]["spec_hash"] = content_hash(value["experiment_spec"], "spec_hash")
    value["input_snapshot"]["experiment_spec_hash"] = value["experiment_spec"]["spec_hash"]
    value["input_snapshot"]["snapshot_hash"] = content_hash(value["input_snapshot"], "snapshot_hash")
    _rehash(value)
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_BUNDLE_BUDGET_EXCEEDS_OBJECTIVE"):
        validate_canonical_input_bundle_v2(value)


@pytest.mark.parametrize(("field", "replacement"), [
    ("gate0_effect", "CONTROLLED_FEASIBLE"),
    ("gate0_result_effect", "PROMOTED"),
    ("metric_contract_content_status", "VERIFIED"),
    ("source_content_effect", "VERIFIED"),
    ("spec_status_semantics", "GOVERNANCE_APPROVED"),
])
def test_machine_readable_trust_ceiling_cannot_be_rehashed_upward(field: str, replacement: str) -> None:
    value = _bundle()
    value["validation_ceiling"][field] = replacement
    _rehash(value)
    with pytest.raises(CanonicalEvaluationContractV2Error, match="G101C_BUNDLE_CEILING_INVALID"):
        validate_canonical_input_bundle_v2(value)


def test_cli_accepts_only_external_anchor_and_returns_non_promoting_exit_2(tmp_path: Path) -> None:
    path = tmp_path / "bundle.json"
    digest = _write_bundle(path)
    result = _cli(path, digest)
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["status"] == "SCHEMA_VALIDATED_NO_AUTHORITY_EFFECT"
    assert payload["validation_ceiling"] == CEILING
    assert _cli(path, "0" * 64).returncode == 64


@pytest.mark.parametrize(("mutate", "expected_code"), [
    (lambda value: value["objective_contract"]["primary_metric"].__setitem__("metric_contract_hash", "x"), "G101C_METRIC_CONTRACT_HASH_INVALID"),
    (lambda value: value["input_snapshot"].__setitem__("created_at", "not-a-time"), "G101C_SNAPSHOT_CREATED_INVALID"),
])
def test_cli_semantic_sha_and_time_errors_are_exit64_without_traceback(
    tmp_path: Path, mutate, expected_code: str,
) -> None:
    value = _bundle()
    mutate(value)
    path = tmp_path / "invalid.json"
    digest = _write_bundle(path, value)
    result = _cli(path, digest)
    assert result.returncode == 64
    assert result.stderr.strip() == expected_code
    assert "Traceback" not in result.stderr


def test_cli_rejects_duplicate_keys_noncanonical_and_unsafe_files(tmp_path: Path) -> None:
    path = tmp_path / "bundle.json"
    digest = _write_bundle(path)
    raw = path.read_text()
    path.write_text(raw.replace('{"bundle_hash":', '{"bundle_hash":"0","bundle_hash":', 1))
    path.chmod(0o600)
    assert _cli(path, hashlib.sha256(path.read_bytes()).hexdigest()).returncode == 64
    digest = _write_bundle(path)
    path.write_bytes(b" " + path.read_bytes())
    path.chmod(0o600)
    assert _cli(path, hashlib.sha256(path.read_bytes()).hexdigest()).returncode == 64
    digest = _write_bundle(path)
    hardlink = tmp_path / "hardlink.json"
    os.link(path, hardlink)
    assert _cli(path, digest).returncode == 64
    hardlink.unlink()
    target = tmp_path / "target.json"
    digest = _write_bundle(target)
    path.unlink()
    path.symlink_to(target)
    assert _cli(path, digest).returncode == 64


def test_cli_rejects_oversize_and_module_has_no_runtime_or_wall_clock_dependencies(tmp_path: Path) -> None:
    path = tmp_path / "bundle.json"
    path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    path.chmod(0o600)
    assert _cli(path, hashlib.sha256(path.read_bytes()).hexdigest()).returncode == 64
    tree = ast.parse((ROOT / "app/growth/canonical_evaluation_contracts_v2.py").read_text())
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not imported.intersection({"sqlite3", "socket", "requests", "urllib", "time"})
