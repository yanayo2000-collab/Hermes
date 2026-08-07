from __future__ import annotations

import copy

import pytest

from app.growth.canonical_evaluation_contracts import (
    CanonicalEvaluationContractError,
    EVALUATION_VERSION,
    INVARIANT_FIELDS,
    INVARIANT_PROJECTION_VERSION,
    OBJECTIVE_VERSION,
    SNAPSHOT_VERSION,
    SPEC_VERSION,
    canonical_hash,
    content_hash,
    validate_canonical_bundle,
    validate_evaluation_record,
    validate_experiment_spec,
    validate_invariant_projection,
    validate_input_snapshot,
    validate_objective_contract,
)


H = "a" * 64


def objective():
    value = {
        "schema_version": OBJECTIVE_VERSION,
        "objective_contract_id": "objective-mx-v1", "version": 1, "contract_hash": H,
        "ad_account_id": "1012060198097836", "market": "MX", "currency": "USD",
        "business_goal": "REDUCE_QUALIFIED_JOIN_CPA",
        "primary_metric": {
            "metric_key": "QUALIFIED_JOIN_CPA", "definition_version": "tugaofunnel-guild-join-success-v1",
            "attribution_window": "14d-click", "dedup_version": "first-success-v1",
            "qualification_rule_version": "tugaofunnel-success-v1", "min_business_improvement": 0.3,
            "direction": "LOWER_IS_BETTER",
        },
        "secondary_metrics": [
            {"metric_key": "CPI", "definition_version": "meta-cpi-v1", "purpose": "DIAGNOSTIC"},
            {"metric_key": "CTR", "definition_version": "meta-ctr-v1", "purpose": "TREND_ONLY"},
        ],
        "guardrails": [{"metric_key": "SPEND_USD", "operator": "LTE", "threshold": 20, "severity": "HARD_STOP"}],
        "risk_boundary": {
            "max_test_budget": 20, "max_daily_budget": 2, "max_write_requests_per_action": 4,
            "hard_deadline_at": "2026-08-21T00:00:00Z", "approval_ttl_seconds": 3600,
        },
        "created_by": "Chauncey", "approved_by": "Chauncey", "approved_at": "2026-08-07T00:00:00Z",
    }
    value["contract_hash"] = content_hash(value, "contract_hash")
    return value


def cell(cell_id, role, suffix):
    return {
        "cell_id": cell_id, "role": role, "copy_version_id": f"copy-{suffix}", "image_sha": H,
        "config_hash": ("b" if suffix == "1" else "c") * 64,
        "meta_campaign_id": "campaign-1", "meta_adset_id": f"adset-{suffix}",
        "meta_creative_id": f"creative-{suffix}", "meta_ad_id": f"ad-{suffix}",
        "meta_assignment_cell_id": f"study-cell-{suffix}", "target_allocation": 0.5,
        "actual_allocation": None, "allocation_verified_at": None,
    }


def invariant_projection(experiment_id="experiment-1"):
    value = {
        "schema_version": INVARIANT_PROJECTION_VERSION,
        "experiment_id": experiment_id,
        "invariant_field_hashes": {field: H for field in INVARIANT_FIELDS},
        "projection_hash": H,
    }
    value["projection_hash"] = content_hash(value, "projection_hash")
    return value


def spec(obj=None, invariant=None):
    obj = obj or objective()
    invariant = invariant or invariant_projection()
    value = {
        "schema_version": SPEC_VERSION, "experiment_id": "experiment-1", "lineage_id": "lineage-1",
        "parent_experiment_id": None, "iteration_no": 1, "objective_contract_id": obj["objective_contract_id"],
        "experiment_type": "COPY_ONLY", "evidence_target": "CONTROLLED", "unique_variable": "PRIMARY_TEXT",
        "invariant_fields": INVARIANT_FIELDS, "invariant_config_hash": invariant["projection_hash"],
        "assignment": {
            "mechanism": "META_SPLIT_TEST", "capability_assessment_id": "capability-1",
            "target_allocation": {"cell-c1": 0.5, "cell-c2": 0.5},
            "allowed_allocation_deviation": 0.1, "readback_required": True,
        },
        "cells": [cell("cell-c1", "CHAMPION", "1"), cell("cell-c2", "CHALLENGER", "2")],
        "power_plan": {
            "power_assessment_id": "power-1", "alpha": 0.05, "power": 0.8, "mde": 0.3,
            "target_information": 114.024535562432, "earliest_binding_information_fraction": 1.0,
            "hard_deadline_at": "2026-08-21T00:00:00Z", "max_test_budget": 20,
        },
        "evaluation_plan": {
            "method": "GROUP_SEQUENTIAL_FREQUENTIST", "boundary_family": "OBRIEN_FLEMING",
            "method_version": "UNFROZEN", "d1_role": "SAFETY_CHECK", "d3_role": "TREND_ONLY",
            "final_role": "BINDING_EFFECT_DECISION", "policy_version": "UNFROZEN",
        },
        "status": "DRAFT", "spec_hash": H, "created_at": "2026-08-07T00:00:00Z", "approved_at": None,
    }
    value["spec_hash"] = content_hash(value, "spec_hash")
    return value


def snapshot(obj=None, experiment=None):
    obj = obj or objective()
    experiment = experiment or spec(obj)
    metrics = {"spend": 0, "impressions": 0, "clicks": 0, "installs": 0, "qualified_joins": 0, "invalid_users": 0, "allocation_share": 0.5}
    value = {
        "schema_version": SNAPSHOT_VERSION, "snapshot_id": "snapshot-1",
        "experiment_id": experiment["experiment_id"], "checkpoint": "D1",
        "data_cutoff_at": "2026-08-08T00:00:00Z", "created_at": "2026-08-08T00:00:00Z",
        "experiment_spec_hash": experiment["spec_hash"], "objective_contract_hash": obj["contract_hash"],
        "evaluator_version": "UNFROZEN", "policy_version": "UNFROZEN",
        "attribution_version": obj["primary_metric"]["definition_version"],
        "dedup_version": obj["primary_metric"]["dedup_version"],
        "cell_metrics": {"cell-c1": dict(metrics), "cell-c2": dict(metrics)},
        "data_quality": {"freshness_ok": False, "attribution_coverage": 0, "missing_sources": ["NATURAL_QUALIFIED_JOIN"], "duplicate_rate": 0},
        "mutation_events": [], "snapshot_hash": H,
    }
    value["snapshot_hash"] = content_hash(value, "snapshot_hash")
    return value


def evaluation(experiment=None, snap=None):
    experiment = experiment or spec()
    snap = snap or snapshot(experiment=experiment)
    return {
        "schema_version": EVALUATION_VERSION, "evaluation_id": "evaluation-1",
        "experiment_id": experiment["experiment_id"], "snapshot_id": snap["snapshot_id"],
        "evaluation_version": 1, "checkpoint_role": "SAFETY_CHECK", "information_fraction": 0,
        "primary_effect_estimate": None, "confidence_interval": None, "alpha_boundary": None,
        "min_business_improvement": 0.3, "safety_status": "PASS", "data_status": "INCOMPLETE",
        "contamination_status": "CLEAN", "maturity_status": "IMMATURE", "guardrail_status": "PASS",
        "evidence_level": "OBSERVATIONAL", "result": "DATA_INCOMPLETE",
        "reason_codes": ["NATURAL_EVIDENCE_INCOMPLETE"], "blocking_reason": "NATURAL_EVIDENCE_INCOMPLETE",
        "next_evaluation_at": "2026-08-09T00:00:00Z", "evaluator_version": snap["evaluator_version"],
        "evaluated_at": snap["created_at"],
    }


def bundle(obj, invariant, experiment, snap, record):
    return {
        "bundle_purpose": "SYNTHETIC_CONTRACT_FIXTURE",
        "objective_contract": obj,
        "invariant_projection": invariant,
        "experiment_spec": experiment,
        "input_snapshot": snap,
        "evaluation_record": record,
    }


def test_full_v11_logical_contracts_and_bundle_bind_all_hashes():
    obj = objective(); invariant = invariant_projection(); experiment = spec(obj, invariant); snap = snapshot(obj, experiment); record = evaluation(experiment, snap)
    assert validate_objective_contract(obj) == obj
    assert validate_experiment_spec(experiment) == experiment
    assert validate_invariant_projection(invariant) == invariant
    assert validate_input_snapshot(snap) == snap
    assert validate_evaluation_record(record) == record
    assert validate_canonical_bundle(bundle(obj, invariant, experiment, snap, record))
    assert canonical_hash({"b": 2, "a": 1}) == canonical_hash({"a": 1, "b": 2})


def test_unknown_keys_and_borrowed_hashes_fail_closed():
    obj = objective(); obj["result"] = "PASS"
    with pytest.raises(CanonicalEvaluationContractError, match="G101_OBJECTIVE_SCHEMA_INVALID"):
        validate_objective_contract(obj)
    obj = objective(); invariant = invariant_projection(); experiment = spec(obj, invariant); snap = snapshot(obj, experiment); record = evaluation(experiment, snap)
    snap["objective_contract_hash"] = "b" * 64
    snap["snapshot_hash"] = content_hash(snap, "snapshot_hash")
    with pytest.raises(CanonicalEvaluationContractError, match="G101_BUNDLE_HASH_MISMATCH"):
        validate_canonical_bundle(bundle(obj, invariant, experiment, snap, record))


def test_self_hash_nonfinite_and_timestamp_aliases_fail_closed():
    obj = objective(); obj["primary_metric"]["min_business_improvement"] = float("nan")
    with pytest.raises(CanonicalEvaluationContractError, match="G101_NON_FINITE_NUMBER"):
        validate_objective_contract(obj)
    obj = objective(); obj["approved_at"] = "2026-08-07T00:00:00.000000Z"
    obj["contract_hash"] = content_hash(obj, "contract_hash")
    with pytest.raises(CanonicalEvaluationContractError, match="G101_TIMESTAMP_INVALID"):
        validate_objective_contract(obj)
    obj = objective(); obj["market"] = "MZ"
    with pytest.raises(CanonicalEvaluationContractError, match="G101_OBJECTIVE_HASH_MISMATCH"):
        validate_objective_contract(obj)
    assert canonical_hash({"amount": 20}) == canonical_hash({"amount": 20.0})


@pytest.mark.parametrize("field", ["cell_id", "copy_version_id", "meta_adset_id", "meta_ad_id", "meta_assignment_cell_id"])
def test_cell_physical_identity_cannot_collide(field):
    value = spec(); value["cells"][1][field] = value["cells"][0][field]; value["spec_hash"] = content_hash(value, "spec_hash")
    with pytest.raises(CanonicalEvaluationContractError, match="DUPLICATE"):
        validate_experiment_spec(value)


def test_exact_allocation_deadline_and_approval_method_boundaries():
    value = spec(); value["assignment"]["target_allocation"] = {"cell-c1": 0.5000000000005, "cell-c2": 0.4999999999995}; value["spec_hash"] = content_hash(value, "spec_hash")
    with pytest.raises(CanonicalEvaluationContractError, match="G101_ASSIGNMENT_ALLOCATION_INVALID"):
        validate_experiment_spec(value)
    value = spec(); value["power_plan"]["hard_deadline_at"] = value["created_at"]; value["spec_hash"] = content_hash(value, "spec_hash")
    with pytest.raises(CanonicalEvaluationContractError, match="G101_DEADLINE_BEFORE_CREATION"):
        validate_experiment_spec(value)
    value = spec(); value["approved_at"] = "2026-08-07T01:00:00Z"; value["spec_hash"] = content_hash(value, "spec_hash")
    with pytest.raises(CanonicalEvaluationContractError, match="G101_FOUNDATION_SPEC_MUST_REMAIN_DRAFT"):
        validate_experiment_spec(value)
    value = spec()
    for item in value["cells"]:
        for field in ("meta_adset_id", "meta_creative_id", "meta_ad_id", "meta_assignment_cell_id"):
            item[field] = None
    value["spec_hash"] = content_hash(value, "spec_hash")
    assert validate_experiment_spec(value) == value


def test_bundle_time_and_version_cannot_be_rewritten():
    obj = objective(); invariant = invariant_projection(); experiment = spec(obj, invariant); snap = snapshot(obj, experiment); record = evaluation(experiment, snap)
    record["evaluated_at"] = "2026-08-07T23:59:59Z"
    with pytest.raises(CanonicalEvaluationContractError, match="G101_BUNDLE_EVALUATED_BEFORE_SNAPSHOT"):
        validate_canonical_bundle(bundle(obj, invariant, experiment, snap, record))
    record = evaluation(experiment, snap); record["checkpoint_role"] = "TREND_ONLY"
    with pytest.raises(CanonicalEvaluationContractError, match="G101_BUNDLE_CHECKPOINT_ROLE_MISMATCH"):
        validate_canonical_bundle(bundle(obj, invariant, experiment, snap, record))


def test_d1_incomplete_or_polluted_record_cannot_claim_winner():
    value = evaluation()
    value.update({"result": "EFFECTIVE", "reason_codes": [], "blocking_reason": None, "primary_effect_estimate": -0.3, "confidence_interval": {"lower": -0.5, "upper": -0.1}, "alpha_boundary": 0.05})
    with pytest.raises(CanonicalEvaluationContractError, match="G101_RESULT_NOT_ALLOWED_FOR_CHECKPOINT"):
        validate_evaluation_record(value)
    value = evaluation(); value["contamination_status"] = "SEVERE"
    with pytest.raises(CanonicalEvaluationContractError, match="G101_CONTAMINATION_RESULT_MISMATCH"):
        validate_evaluation_record(value)


def test_objective_risk_is_hard_ceiling_for_spec():
    obj = objective(); invariant = invariant_projection(); experiment = spec(obj, invariant); experiment["power_plan"]["max_test_budget"] = 200; experiment["spec_hash"] = content_hash(experiment, "spec_hash")
    snap = snapshot(obj, experiment); record = evaluation(experiment, snap)
    with pytest.raises(CanonicalEvaluationContractError, match="G101_BUNDLE_BUDGET_EXCEEDS_OBJECTIVE"):
        validate_canonical_bundle(bundle(obj, invariant, experiment, snap, record))


def test_actual_allocation_and_snapshot_quality_fail_closed():
    value = spec()
    value["cells"][0]["actual_allocation"] = 0.9
    value["cells"][1]["actual_allocation"] = 0.9
    value["spec_hash"] = content_hash(value, "spec_hash")
    with pytest.raises(CanonicalEvaluationContractError, match="G101_DRAFT_ACTUAL_ALLOCATION_FORBIDDEN"):
        validate_experiment_spec(value)
    obj = objective(); invariant = invariant_projection(); experiment = spec(obj, invariant); snap = snapshot(obj, experiment); record = evaluation(experiment, snap)
    record.update({"data_status": "COMPLETE", "result": "WAITING_EVIDENCE"})
    with pytest.raises(CanonicalEvaluationContractError, match="G101_BUNDLE_DATA_QUALITY_MISMATCH"):
        validate_canonical_bundle(bundle(obj, invariant, experiment, snap, record))


def test_invariant_projection_separates_shared_fields_from_full_cell_configs():
    obj = objective(); invariant = invariant_projection(); experiment = spec(obj, invariant)
    assert experiment["cells"][0]["config_hash"] != experiment["cells"][1]["config_hash"]
    assert experiment["invariant_config_hash"] == invariant["projection_hash"]
    tampered = dict(invariant); tampered["invariant_field_hashes"] = dict(invariant["invariant_field_hashes"])
    tampered["invariant_field_hashes"]["IMAGE_SHA"] = "d" * 64
    tampered["projection_hash"] = content_hash(tampered, "projection_hash")
    experiment = spec(obj, tampered); snap = snapshot(obj, experiment); record = evaluation(experiment, snap)
    with pytest.raises(CanonicalEvaluationContractError, match="G101_BUNDLE_INVARIANT_MISMATCH"):
        validate_canonical_bundle(bundle(obj, tampered, experiment, snap, record))
    reordered = dict(invariant)
    reordered["invariant_field_hashes"] = dict(reversed(list(invariant["invariant_field_hashes"].items())))
    assert validate_invariant_projection(reordered) == reordered


def test_bundle_time_causality_is_fail_closed():
    obj = objective(); invariant = invariant_projection(); experiment = spec(obj, invariant)
    experiment["created_at"] = "2026-08-06T23:59:59Z"; experiment["spec_hash"] = content_hash(experiment, "spec_hash")
    snap = snapshot(obj, experiment); record = evaluation(experiment, snap)
    with pytest.raises(CanonicalEvaluationContractError, match="G101_BUNDLE_SPEC_BEFORE_OBJECTIVE_APPROVAL"):
        validate_canonical_bundle(bundle(obj, invariant, experiment, snap, record))
    experiment = spec(obj, invariant); snap = snapshot(obj, experiment)
    snap["data_cutoff_at"] = "2026-08-06T23:59:59Z"; snap["created_at"] = "2026-08-07T00:00:00Z"
    snap["snapshot_hash"] = content_hash(snap, "snapshot_hash"); record = evaluation(experiment, snap)
    with pytest.raises(CanonicalEvaluationContractError, match="G101_BUNDLE_CUTOFF_BEFORE_SPEC"):
        validate_canonical_bundle(bundle(obj, invariant, experiment, snap, record))


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"result": "SAFETY_STOP", "blocking_reason": "UNSUPPORTED_STOP"}, "G101_SAFETY_RESULT_MISMATCH"),
        ({"result": "NOT_ATTRIBUTABLE", "blocking_reason": "UNSUPPORTED_ATTRIBUTION"}, "G101_CONTAMINATION_RESULT_MISMATCH"),
        ({"result": "INVALIDATED", "blocking_reason": "UNSUPPORTED_INVALIDATION"}, "G101_INVALIDATION_RESULT_MISMATCH"),
    ],
)
def test_negative_results_require_their_underlying_state(changes, error):
    value = evaluation()
    value.update({"data_status": "COMPLETE", "reason_codes": ["CALLER_ASSERTED"], **changes})
    with pytest.raises(CanonicalEvaluationContractError, match=error):
        validate_evaluation_record(value)


def test_unrecoverable_data_requires_invalidated():
    value = evaluation(); value.update({"data_status": "UNRECOVERABLE", "result": "WAITING_EVIDENCE"})
    with pytest.raises(CanonicalEvaluationContractError, match="G101_DATA_RESULT_MISMATCH"):
        validate_evaluation_record(value)


@pytest.mark.parametrize(("coverage", "duplicate_rate"), [(0, 0), (1, 1)])
def test_complete_data_requires_conservative_foundation_quality(coverage, duplicate_rate):
    obj = objective(); invariant = invariant_projection(); experiment = spec(obj, invariant); snap = snapshot(obj, experiment)
    snap["data_quality"] = {"freshness_ok": True, "attribution_coverage": coverage, "missing_sources": [], "duplicate_rate": duplicate_rate}
    snap["snapshot_hash"] = content_hash(snap, "snapshot_hash")
    record = evaluation(experiment, snap); record.update({"data_status": "COMPLETE", "result": "WAITING_EVIDENCE"})
    with pytest.raises(CanonicalEvaluationContractError, match="G101_BUNDLE_DATA_QUALITY_MISMATCH"):
        validate_canonical_bundle(bundle(obj, invariant, experiment, snap, record))


def test_foundation_bundle_cannot_claim_controlled_evidence():
    obj = objective(); invariant = invariant_projection(); experiment = spec(obj, invariant); snap = snapshot(obj, experiment); record = evaluation(experiment, snap)
    record["evidence_level"] = "CONTROLLED"
    with pytest.raises(CanonicalEvaluationContractError, match="G101_FOUNDATION_EVIDENCE_LEVEL_EXCEEDED"):
        validate_canonical_bundle(bundle(obj, invariant, experiment, snap, record))


def test_real_bundle_purpose_is_deferred_from_draft_foundation():
    obj = objective(); invariant = invariant_projection(); experiment = spec(obj, invariant); snap = snapshot(obj, experiment); record = evaluation(experiment, snap)
    value = bundle(obj, invariant, experiment, snap, record); value["bundle_purpose"] = "REAL_EVALUATION"
    with pytest.raises(CanonicalEvaluationContractError, match="G101_BUNDLE_PURPOSE_INVALID"):
        validate_canonical_bundle(value)


def test_binding_result_requires_machine_reason_codes():
    value = evaluation()
    value.update({
        "checkpoint_role": "BINDING_EFFECT_DECISION", "information_fraction": 1,
        "primary_effect_estimate": -0.3, "confidence_interval": {"lower": -0.5, "upper": -0.1},
        "alpha_boundary": 0.05, "data_status": "COMPLETE", "maturity_status": "MATURE",
        "evidence_level": "CONTROLLED", "result": "EFFECTIVE", "reason_codes": [],
        "blocking_reason": None, "next_evaluation_at": None,
    })
    with pytest.raises(CanonicalEvaluationContractError, match="G101_RESULT_REASONS_REQUIRED"):
        validate_evaluation_record(value)
