from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from app.growth.canonical_evaluation_contracts import (
    CanonicalEvaluationContractError,
    INVARIANT_FIELDS,
    canonical_hash,
    canonical_json,
    validate_sha256 as _validate_sha256_v1,
    validate_utc as _validate_utc_v1,
)


OBJECTIVE_VERSION_V2 = "gle-objective-contract-v2"
SPEC_VERSION_V2 = "gle-experiment-spec-v2"
SNAPSHOT_VERSION_V2 = "gle-evaluation-input-snapshot-v2"
INVARIANT_PROJECTION_VERSION_V2 = "gle-copy-only-invariant-projection-v2"
BUNDLE_VERSION_V2 = "gle-canonical-input-bundle-v2"

SPEC_STATUSES_V2 = frozenset({"DRAFT", "AUTHORITY_CANDIDATE", "APPROVED_SHAPE_CANDIDATE"})
CHECKPOINTS = frozenset({"D1", "D3", "INFORMATION_LOOK", "FINAL", "HARD_STOP"})
CEILING = {
    "contract_effect": "V2_SCHEMA_AND_SYNTHETIC_VALIDATION_ONLY",
    "objective_approval_semantics": "CALLER_ASSERTED_SHAPE_ONLY",
    "spec_status_semantics": "CALLER_ASSERTED_SHAPE_ONLY",
    "authority_reference_content_effect": "NONE",
    "metric_contract_content_status": "NOT_OPENED_NOT_VERIFIED",
    "source_content_effect": "NONE",
    "objective_authority_effect": "NONE",
    "spec_authority_effect": "NONE",
    "snapshot_effect": "NONE",
    "snapshot_emitted": False,
    "partition_effect": "NONE",
    "holdout_status": "LOCKED_NOT_ASSIGNED",
    "replay_executed": False,
    "replay_eligible": False,
    "golden_eligible": False,
    "gate0_effect": "NONE",
    "gate0_result_effect": "UNCHANGED",
    "gate1_effect": "NONE",
    "not_dataset_receipt": True,
    "not_snapshot_receipt": True,
    "not_replay_receipt": True,
    "not_gate_receipt": True,
}

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class CanonicalEvaluationContractV2Error(ValueError):
    pass


def _fail(code: str) -> None:
    raise CanonicalEvaluationContractV2Error(code)


def validate_sha256(value: Any, *, code: str) -> str:
    try:
        return _validate_sha256_v1(value, code=code)
    except CanonicalEvaluationContractError:
        _fail(code)


def validate_utc(value: Any, *, nullable: bool = False, code: str) -> str | None:
    try:
        return _validate_utc_v1(value, nullable=nullable, code=code)
    except CanonicalEvaluationContractError:
        _fail(code)


def content_hash(value: Mapping[str, Any], field: str) -> str:
    return canonical_hash({key: item for key, item in value.items() if key != field})


def validate_objective_contract_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    body = _exact(value, {
        "schema_version", "objective_contract_id", "version", "contract_hash", "ad_account_id",
        "market", "currency", "business_goal", "primary_metric", "secondary_metrics",
        "guardrails", "risk_boundary", "created_by", "approved_by", "approved_at",
        "approval_claim_status", "authority_effect",
    }, "G101C_OBJECTIVE_SCHEMA_INVALID")
    if body["schema_version"] != OBJECTIVE_VERSION_V2:
        _fail("G101C_OBJECTIVE_VERSION_MISMATCH")
    for field in ("objective_contract_id", "ad_account_id", "market", "created_by", "approved_by"):
        _identifier(body[field], "G101C_OBJECTIVE_IDENTIFIER_INVALID")
    if type(body["version"]) is not int or body["version"] <= 0:
        _fail("G101C_OBJECTIVE_VERSION_NUMBER_INVALID")
    if body["currency"] != "USD" or body["business_goal"] != "REDUCE_QUALIFIED_JOIN_CPA":
        _fail("G101C_OBJECTIVE_GOAL_INVALID")
    if (
        body["approval_claim_status"] != "CALLER_DECLARED_NOT_GOVERNANCE_VERIFIED"
        or body["authority_effect"] != "NONE"
    ):
        _fail("G101C_OBJECTIVE_AUTHORITY_CEILING_INVALID")
    primary = _exact(body["primary_metric"], {
        "metric_key", "definition_version", "metric_contract_hash", "attribution_version",
        "attribution_window", "dedup_version", "qualification_rule_version",
        "min_business_improvement", "direction",
    }, "G101C_PRIMARY_METRIC_SCHEMA_INVALID")
    if primary["metric_key"] != "QUALIFIED_JOIN_CPA" or primary["direction"] != "LOWER_IS_BETTER":
        _fail("G101C_PRIMARY_METRIC_INVALID")
    for field in (
        "definition_version", "attribution_version", "attribution_window", "dedup_version",
        "qualification_rule_version",
    ):
        _frozen_identifier(primary[field], "G101C_PRIMARY_METRIC_VERSION_UNFROZEN")
    validate_sha256(primary["metric_contract_hash"], code="G101C_METRIC_CONTRACT_HASH_INVALID")
    _positive(primary["min_business_improvement"], "G101C_MIN_IMPROVEMENT_INVALID")

    secondary = body["secondary_metrics"]
    if not isinstance(secondary, list):
        _fail("G101C_SECONDARY_METRICS_INVALID")
    keys: list[str] = []
    for raw in secondary:
        item = _exact(raw, {"metric_key", "definition_version", "purpose"}, "G101C_SECONDARY_SCHEMA_INVALID")
        if item["metric_key"] not in {"CTR", "CPI", "INSTALL_TO_JOIN_CVR"} or item["purpose"] not in {"DIAGNOSTIC", "TREND_ONLY"}:
            _fail("G101C_SECONDARY_INVALID")
        _frozen_identifier(item["definition_version"], "G101C_SECONDARY_VERSION_INVALID")
        keys.append(item["metric_key"])
    if keys != sorted(set(keys)):
        _fail("G101C_SECONDARY_ORDER_INVALID")

    guardrail_keys: list[tuple[str, str]] = []
    if not isinstance(body["guardrails"], list):
        _fail("G101C_GUARDRAILS_INVALID")
    for raw in body["guardrails"]:
        item = _exact(raw, {"metric_key", "operator", "threshold", "severity"}, "G101C_GUARDRAIL_SCHEMA_INVALID")
        _identifier(item["metric_key"], "G101C_GUARDRAIL_METRIC_INVALID")
        if item["operator"] not in {"LTE", "GTE", "DELTA_LTE", "DELTA_GTE"} or item["severity"] not in {"HARD_STOP", "BLOCK_WINNER", "WARN"}:
            _fail("G101C_GUARDRAIL_INVALID")
        _number(item["threshold"], "G101C_GUARDRAIL_THRESHOLD_INVALID")
        guardrail_keys.append((item["metric_key"], item["operator"]))
    if guardrail_keys != sorted(set(guardrail_keys)):
        _fail("G101C_GUARDRAIL_ORDER_INVALID")

    risk = _exact(body["risk_boundary"], {
        "max_test_budget", "max_daily_budget", "max_write_requests_per_action",
        "hard_deadline_at", "approval_ttl_seconds",
    }, "G101C_RISK_SCHEMA_INVALID")
    _positive(risk["max_test_budget"], "G101C_MAX_BUDGET_INVALID")
    _positive(risk["max_daily_budget"], "G101C_DAILY_BUDGET_INVALID")
    if float(risk["max_daily_budget"]) > float(risk["max_test_budget"]):
        _fail("G101C_DAILY_BUDGET_EXCEEDS_TOTAL")
    for field in ("max_write_requests_per_action", "approval_ttl_seconds"):
        if type(risk[field]) is not int or risk[field] <= 0:
            _fail("G101C_RISK_INTEGER_INVALID")
    approved = _instant(validate_utc(body["approved_at"], code="G101C_OBJECTIVE_APPROVED_AT_INVALID"))
    deadline = _instant(validate_utc(risk["hard_deadline_at"], code="G101C_OBJECTIVE_DEADLINE_INVALID"))
    if deadline <= approved:
        _fail("G101C_OBJECTIVE_DEADLINE_BEFORE_APPROVAL")
    _self_hash(body, "contract_hash", "G101C_OBJECTIVE_HASH_MISMATCH")
    return body


def validate_experiment_spec_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    body = _exact(value, {
        "schema_version", "experiment_id", "study_id", "lineage_id", "parent_experiment_id",
        "iteration_no", "objective_contract_id", "objective_contract_hash", "experiment_type",
        "evidence_target", "unique_variable", "invariant_fields", "invariant_config_hash",
        "assignment", "cells", "power_plan", "evaluation_plan", "status", "authority_attestation_ref",
        "authority_validation_status", "authority_effect", "spec_hash", "created_at", "approved_at",
    }, "G101C_SPEC_SCHEMA_INVALID")
    if body["schema_version"] != SPEC_VERSION_V2:
        _fail("G101C_SPEC_VERSION_MISMATCH")
    for field in ("experiment_id", "study_id", "lineage_id", "objective_contract_id"):
        _identifier(body[field], "G101C_SPEC_IDENTIFIER_INVALID")
    validate_sha256(body["objective_contract_hash"], code="G101C_SPEC_OBJECTIVE_HASH_INVALID")
    if body["parent_experiment_id"] is not None:
        _identifier(body["parent_experiment_id"], "G101C_PARENT_INVALID")
        if body["parent_experiment_id"] == body["experiment_id"]:
            _fail("G101C_PARENT_SELF_REFERENCE")
    if type(body["iteration_no"]) is not int or body["iteration_no"] <= 0:
        _fail("G101C_ITERATION_INVALID")
    if (body["iteration_no"] == 1) != (body["parent_experiment_id"] is None):
        _fail("G101C_LINEAGE_PARENT_INVALID")
    if body["experiment_type"] != "COPY_ONLY" or body["evidence_target"] != "CONTROLLED" or body["unique_variable"] != "PRIMARY_TEXT":
        _fail("G101C_SPEC_SCOPE_INVALID")
    if body["invariant_fields"] != INVARIANT_FIELDS:
        _fail("G101C_INVARIANTS_INVALID")
    validate_sha256(body["invariant_config_hash"], code="G101C_INVARIANT_HASH_INVALID")
    if body["status"] not in SPEC_STATUSES_V2:
        _fail("G101C_SPEC_STATUS_INVALID")
    if (
        body["authority_validation_status"] != "UNVERIFIED_REFERENCE_ONLY"
        or body["authority_effect"] != "NONE"
    ):
        _fail("G101C_SPEC_AUTHORITY_CEILING_INVALID")

    cells = body["cells"]
    if not isinstance(cells, list) or len(cells) != 2:
        _fail("G101C_CELL_SET_INVALID")
    normalized = [_validate_cell(item) for item in cells]
    if [item["role"] for item in normalized] != ["CHAMPION", "CHALLENGER"]:
        _fail("G101C_CELL_ROLE_ORDER_INVALID")
    if len({item["cell_id"] for item in normalized}) != 2 or len({item["copy_version_id"] for item in normalized}) != 2:
        _fail("G101C_CELL_IDENTITY_DUPLICATE")
    if len({item["image_sha"] for item in normalized}) != 1 or len({item["config_hash"] for item in normalized}) != 2:
        _fail("G101C_COPY_ONLY_INVARIANT_INVALID")
    for field in ("meta_adset_id", "meta_creative_id", "meta_ad_id", "meta_assignment_cell_id"):
        values = [item[field] for item in normalized]
        if values != [None, None] and (None in values or len(set(values)) != 2):
            _fail("G101C_CELL_META_ID_INVALID")
    campaigns = [item["meta_campaign_id"] for item in normalized]
    if campaigns != [None, None] and (None in campaigns or len(set(campaigns)) != 1):
        _fail("G101C_CAMPAIGN_INVARIANT_INVALID")

    assignment = _exact(body["assignment"], {
        "mechanism", "capability_assessment_id", "target_allocation",
        "allowed_allocation_deviation", "readback_required", "readback_evidence_sha256",
    }, "G101C_ASSIGNMENT_SCHEMA_INVALID")
    _frozen_identifier(assignment["mechanism"], "G101C_ASSIGNMENT_MECHANISM_UNFROZEN")
    _identifier(assignment["capability_assessment_id"], "G101C_CAPABILITY_ID_INVALID")
    if assignment["readback_required"] is not True:
        _fail("G101C_ASSIGNMENT_READBACK_REQUIRED")
    cell_ids = [item["cell_id"] for item in normalized]
    targets = assignment["target_allocation"]
    if not isinstance(targets, Mapping) or set(targets) != set(cell_ids):
        _fail("G101C_TARGET_ALLOCATION_KEYS_INVALID")
    if any(type(targets[cell_id]) not in {int, float} or float(targets[cell_id]) != 0.5 for cell_id in cell_ids):
        _fail("G101C_TARGET_ALLOCATION_INVALID")
    deviation = _nonnegative(assignment["allowed_allocation_deviation"], "G101C_ALLOCATION_DEVIATION_INVALID")
    if deviation > 0.5:
        _fail("G101C_ALLOCATION_DEVIATION_INVALID")

    power = _validate_power(body["power_plan"])
    evaluation = _validate_evaluation_plan(body["evaluation_plan"])
    created = _instant(validate_utc(body["created_at"], code="G101C_SPEC_CREATED_AT_INVALID"))
    approved_at = validate_utc(body["approved_at"], nullable=True, code="G101C_SPEC_APPROVED_AT_INVALID")
    authority_ref = body["authority_attestation_ref"]
    if body["status"] == "APPROVED_SHAPE_CANDIDATE":
        if approved_at is None or authority_ref is None:
            _fail("G101C_APPROVED_SPEC_AUTHORITY_SHAPE_INVALID")
        if evaluation["method_version"].upper() == "UNFROZEN" or evaluation["policy_version"].upper() == "UNFROZEN":
            _fail("G101C_APPROVED_SPEC_VERSION_UNFROZEN")
    elif body["status"] == "AUTHORITY_CANDIDATE":
        if approved_at is not None or authority_ref is None:
            _fail("G101C_AUTHORITY_CANDIDATE_SHAPE_INVALID")
        if evaluation["method_version"].upper() == "UNFROZEN" or evaluation["policy_version"].upper() == "UNFROZEN":
            _fail("G101C_AUTHORITY_CANDIDATE_VERSION_UNFROZEN")
    else:
        if approved_at is not None or authority_ref is not None:
            _fail("G101C_NONAPPROVED_SPEC_AUTHORITY_FORBIDDEN")
        if evaluation["method_version"] != "UNFROZEN" or evaluation["policy_version"] != "UNFROZEN":
            _fail("G101C_DRAFT_VERSION_STATE_INVALID")
    if approved_at is not None and _instant(approved_at) < created:
        _fail("G101C_SPEC_APPROVAL_BEFORE_CREATION")
    if authority_ref is not None:
        ref = _exact(authority_ref, {
            "authority_id", "authority_manifest_sha256", "objective_attestation_manifest_sha256",
        }, "G101C_AUTHORITY_REF_INVALID")
        _identifier(ref["authority_id"], "G101C_AUTHORITY_REF_INVALID")
        validate_sha256(ref["authority_manifest_sha256"], code="G101C_AUTHORITY_REF_INVALID")
        validate_sha256(ref["objective_attestation_manifest_sha256"], code="G101C_AUTHORITY_REF_INVALID")
    if _instant(power["hard_deadline_at"]) <= created:
        _fail("G101C_SPEC_DEADLINE_BEFORE_CREATION")

    actuals = [item["actual_allocation"] for item in normalized]
    readback_times = [item["allocation_verified_at"] for item in normalized]
    has_actual = actuals != [None, None]
    if has_actual:
        if None in actuals or None in readback_times or assignment["readback_evidence_sha256"] is None:
            _fail("G101C_ACTUAL_ALLOCATION_READBACK_INVALID")
        validate_sha256(assignment["readback_evidence_sha256"], code="G101C_READBACK_EVIDENCE_INVALID")
        if len(set(readback_times)) != 1:
            _fail("G101C_READBACK_TIME_MISMATCH")
        for cell in normalized:
            if any(cell[field] is None for field in (
                "meta_campaign_id", "meta_adset_id", "meta_creative_id", "meta_ad_id",
                "meta_assignment_cell_id",
            )):
                _fail("G101C_READBACK_PHYSICAL_IDENTITY_MISSING")
        if not math.isclose(sum(float(item) for item in actuals), 1.0, rel_tol=0, abs_tol=1e-12):
            _fail("G101C_ACTUAL_ALLOCATION_INVALID")
        for cell, actual, verified_at in zip(normalized, actuals, readback_times):
            if abs(float(actual) - float(targets[cell["cell_id"]])) > deviation:
                _fail("G101C_ACTUAL_ALLOCATION_DEVIATION_EXCEEDED")
            if _instant(verified_at) < created:
                _fail("G101C_READBACK_BEFORE_SPEC")
            if approved_at is not None and _instant(verified_at) < _instant(approved_at):
                _fail("G101C_READBACK_BEFORE_APPROVAL")
    elif readback_times != [None, None] or assignment["readback_evidence_sha256"] is not None:
        _fail("G101C_ACTUAL_ALLOCATION_READBACK_INVALID")
    if body["status"] != "APPROVED_SHAPE_CANDIDATE" and has_actual:
        _fail("G101C_UNAPPROVED_ACTUAL_ALLOCATION_FORBIDDEN")
    _self_hash(body, "spec_hash", "G101C_SPEC_HASH_MISMATCH")
    return body


def validate_invariant_projection_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    body = _exact(value, {
        "schema_version", "experiment_id", "invariant_field_hashes", "projection_hash",
    }, "G101C_INVARIANT_SCHEMA_INVALID")
    if body["schema_version"] != INVARIANT_PROJECTION_VERSION_V2:
        _fail("G101C_INVARIANT_VERSION_MISMATCH")
    _identifier(body["experiment_id"], "G101C_INVARIANT_EXPERIMENT_INVALID")
    field_hashes = body["invariant_field_hashes"]
    if not isinstance(field_hashes, Mapping) or set(field_hashes) != set(INVARIANT_FIELDS):
        _fail("G101C_INVARIANT_FIELDS_INVALID")
    for field in INVARIANT_FIELDS:
        validate_sha256(field_hashes[field], code="G101C_INVARIANT_FIELD_HASH_INVALID")
    _self_hash(body, "projection_hash", "G101C_INVARIANT_HASH_MISMATCH")
    return body


def validate_input_snapshot_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    body = _exact(value, {
        "schema_version", "snapshot_id", "experiment_id", "checkpoint", "data_cutoff_at",
        "created_at", "experiment_spec_hash", "objective_contract_hash", "evaluator_version",
        "policy_version", "metric_definition_version", "metric_contract_hash",
        "attribution_version", "attribution_window", "dedup_version",
        "qualification_rule_version", "allocation_basis", "allocation_readback_evidence_sha256",
        "allocation_verified_at", "cell_metrics", "data_quality", "mutation_events",
        "source_validation_status", "snapshot_effect", "snapshot_hash",
    }, "G101C_SNAPSHOT_SCHEMA_INVALID")
    if body["schema_version"] != SNAPSHOT_VERSION_V2:
        _fail("G101C_SNAPSHOT_VERSION_MISMATCH")
    _identifier(body["snapshot_id"], "G101C_SNAPSHOT_ID_INVALID")
    _identifier(body["experiment_id"], "G101C_SNAPSHOT_EXPERIMENT_INVALID")
    if body["checkpoint"] not in CHECKPOINTS:
        _fail("G101C_SNAPSHOT_CHECKPOINT_INVALID")
    cutoff = _instant(validate_utc(body["data_cutoff_at"], code="G101C_CUTOFF_INVALID"))
    created = _instant(validate_utc(body["created_at"], code="G101C_SNAPSHOT_CREATED_INVALID"))
    if cutoff > created:
        _fail("G101C_CUTOFF_AFTER_CREATED")
    if body["source_validation_status"] != "SYNTHETIC_FIXTURE_ONLY" or body["snapshot_effect"] != "NONE":
        _fail("G101C_SNAPSHOT_EFFECT_CEILING_INVALID")
    for field in ("experiment_spec_hash", "objective_contract_hash", "metric_contract_hash"):
        validate_sha256(body[field], code="G101C_SNAPSHOT_HASH_BINDING_INVALID")
    for field in (
        "evaluator_version", "policy_version", "metric_definition_version", "attribution_version",
        "attribution_window", "dedup_version", "qualification_rule_version",
    ):
        _frozen_identifier(body[field], "G101C_SNAPSHOT_VERSION_INVALID")
    allocation_evidence = body["allocation_readback_evidence_sha256"]
    allocation_verified_at = validate_utc(
        body["allocation_verified_at"], nullable=True, code="G101C_SNAPSHOT_ALLOCATION_TIME_INVALID",
    )
    if body["allocation_basis"] == "ACTUAL_READBACK":
        if allocation_evidence is None or allocation_verified_at is None:
            _fail("G101C_SNAPSHOT_ALLOCATION_BINDING_INVALID")
        validate_sha256(allocation_evidence, code="G101C_SNAPSHOT_ALLOCATION_BINDING_INVALID")
    elif body["allocation_basis"] == "SYNTHETIC_TARGET_FIXTURE":
        if allocation_evidence is not None or allocation_verified_at is not None:
            _fail("G101C_SNAPSHOT_ALLOCATION_BINDING_INVALID")
    else:
        _fail("G101C_SNAPSHOT_ALLOCATION_BASIS_INVALID")
    if not isinstance(body["cell_metrics"], Mapping) or len(body["cell_metrics"]) != 2:
        _fail("G101C_CELL_METRICS_INVALID")
    for cell_id, raw in body["cell_metrics"].items():
        _identifier(cell_id, "G101C_METRIC_CELL_ID_INVALID")
        item = _exact(raw, {
            "spend", "impressions", "clicks", "installs", "qualified_joins",
            "invalid_users", "allocation_share",
        }, "G101C_CELL_METRIC_SCHEMA_INVALID")
        _nonnegative(item["spend"], "G101C_CELL_SPEND_INVALID")
        for field in ("impressions", "clicks", "installs", "qualified_joins", "invalid_users"):
            if type(item[field]) is not int or item[field] < 0:
                _fail("G101C_CELL_COUNT_INVALID")
        share = _nonnegative(item["allocation_share"], "G101C_CELL_SHARE_INVALID")
        if share > 1:
            _fail("G101C_CELL_SHARE_INVALID")
    quality = _exact(body["data_quality"], {
        "freshness_ok", "attribution_coverage", "missing_sources", "duplicate_rate",
    }, "G101C_DATA_QUALITY_SCHEMA_INVALID")
    if type(quality["freshness_ok"]) is not bool:
        _fail("G101C_FRESHNESS_INVALID")
    for field in ("attribution_coverage", "duplicate_rate"):
        if _nonnegative(quality[field], "G101C_DATA_QUALITY_INVALID") > 1:
            _fail("G101C_DATA_QUALITY_INVALID")
    if not isinstance(quality["missing_sources"], list):
        _fail("G101C_MISSING_SOURCES_INVALID")
    for source in quality["missing_sources"]:
        _identifier(source, "G101C_MISSING_SOURCES_INVALID")
    if quality["missing_sources"] != sorted(set(quality["missing_sources"])):
        _fail("G101C_MISSING_SOURCES_INVALID")
    if not isinstance(body["mutation_events"], list):
        _fail("G101C_MUTATION_EVENTS_INVALID")
    order: list[tuple[str, str, str]] = []
    for raw in body["mutation_events"]:
        event = _exact(raw, {"object_id", "field", "before", "after", "changed_at", "source"}, "G101C_MUTATION_SCHEMA_INVALID")
        _identifier(event["object_id"], "G101C_MUTATION_ID_INVALID")
        _identifier(event["field"], "G101C_MUTATION_FIELD_INVALID")
        changed = validate_utc(event["changed_at"], code="G101C_MUTATION_TIME_INVALID")
        if _instant(changed) > cutoff or event["source"] not in {"GLE", "EXTERNAL", "UNKNOWN"}:
            _fail("G101C_MUTATION_INVALID")
        order.append((changed, event["object_id"], event["field"]))
    if order != sorted(order):
        _fail("G101C_MUTATION_ORDER_INVALID")
    _self_hash(body, "snapshot_hash", "G101C_SNAPSHOT_HASH_MISMATCH")
    return body


def validate_canonical_input_bundle_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    bundle = _exact(value, {
        "schema_version", "bundle_purpose", "objective_contract", "invariant_projection",
        "experiment_spec", "input_snapshot", "validation_ceiling", "bundle_hash",
    }, "G101C_BUNDLE_SCHEMA_INVALID")
    if bundle["schema_version"] != BUNDLE_VERSION_V2 or bundle["bundle_purpose"] != "SYNTHETIC_CONTRACT_VALIDATION":
        _fail("G101C_BUNDLE_VERSION_OR_PURPOSE_INVALID")
    objective = validate_objective_contract_v2(bundle["objective_contract"])
    invariant = validate_invariant_projection_v2(bundle["invariant_projection"])
    spec = validate_experiment_spec_v2(bundle["experiment_spec"])
    snapshot = validate_input_snapshot_v2(bundle["input_snapshot"])
    primary = objective["primary_metric"]
    if (
        spec["objective_contract_id"] != objective["objective_contract_id"]
        or spec["objective_contract_hash"] != objective["contract_hash"]
        or snapshot["objective_contract_hash"] != objective["contract_hash"]
        or snapshot["experiment_spec_hash"] != spec["spec_hash"]
        or snapshot["experiment_id"] != spec["experiment_id"]
        or invariant["experiment_id"] != spec["experiment_id"]
        or invariant["projection_hash"] != spec["invariant_config_hash"]
        or invariant["invariant_field_hashes"]["IMAGE_SHA"] != spec["cells"][0]["image_sha"]
    ):
        _fail("G101C_BUNDLE_OBJECT_BINDING_INVALID")
    if (
        snapshot["metric_definition_version"] != primary["definition_version"]
        or snapshot["metric_contract_hash"] != primary["metric_contract_hash"]
        or snapshot["attribution_version"] != primary["attribution_version"]
        or snapshot["attribution_window"] != primary["attribution_window"]
        or snapshot["dedup_version"] != primary["dedup_version"]
        or snapshot["qualification_rule_version"] != primary["qualification_rule_version"]
    ):
        _fail("G101C_BUNDLE_METRIC_BINDING_INVALID")
    if (
        snapshot["evaluator_version"] != spec["evaluation_plan"]["method_version"]
        or snapshot["policy_version"] != spec["evaluation_plan"]["policy_version"]
    ):
        _fail("G101C_BUNDLE_EVALUATION_VERSION_INVALID")
    cells = {item["cell_id"]: item for item in spec["cells"]}
    if set(snapshot["cell_metrics"]) != set(cells):
        _fail("G101C_BUNDLE_CELL_SET_INVALID")
    shares = [float(snapshot["cell_metrics"][cell_id]["allocation_share"]) for cell_id in cells]
    if not math.isclose(sum(shares), 1.0, rel_tol=0, abs_tol=1e-12):
        _fail("G101C_BUNDLE_ALLOCATION_INVALID")
    deviation = float(spec["assignment"]["allowed_allocation_deviation"])
    targets = spec["assignment"]["target_allocation"]
    actuals = {item["cell_id"]: item["actual_allocation"] for item in spec["cells"]}
    if any(item is not None for item in actuals.values()):
        if (
            snapshot["allocation_basis"] != "ACTUAL_READBACK"
            or snapshot["allocation_readback_evidence_sha256"] != spec["assignment"]["readback_evidence_sha256"]
            or snapshot["allocation_verified_at"] != spec["cells"][0]["allocation_verified_at"]
        ):
            _fail("G101C_BUNDLE_ALLOCATION_READBACK_BINDING_INVALID")
        for cell_id, actual in actuals.items():
            if float(snapshot["cell_metrics"][cell_id]["allocation_share"]) != float(actual):
                _fail("G101C_BUNDLE_ALLOCATION_READBACK_BINDING_INVALID")
    elif snapshot["allocation_basis"] != "SYNTHETIC_TARGET_FIXTURE":
        _fail("G101C_BUNDLE_ALLOCATION_READBACK_BINDING_INVALID")
    for cell_id in cells:
        if abs(float(snapshot["cell_metrics"][cell_id]["allocation_share"]) - float(targets[cell_id])) > deviation:
            _fail("G101C_BUNDLE_ALLOCATION_DEVIATION_EXCEEDED")
    if _instant(spec["created_at"]) < _instant(objective["approved_at"]):
        _fail("G101C_BUNDLE_SPEC_BEFORE_OBJECTIVE")
    if _instant(snapshot["data_cutoff_at"]) < _instant(spec["created_at"]):
        _fail("G101C_BUNDLE_CUTOFF_BEFORE_SPEC")
    if _instant(snapshot["created_at"]) < _instant(spec["created_at"]):
        _fail("G101C_BUNDLE_SNAPSHOT_BEFORE_SPEC")
    if spec["approved_at"] is not None and _instant(spec["approved_at"]) > _instant(snapshot["data_cutoff_at"]):
        _fail("G101C_BUNDLE_APPROVAL_AFTER_CUTOFF")
    for cell in spec["cells"]:
        verified_at = cell["allocation_verified_at"]
        if verified_at is not None and _instant(verified_at) > _instant(snapshot["data_cutoff_at"]):
            _fail("G101C_BUNDLE_READBACK_AFTER_CUTOFF")
    risk = objective["risk_boundary"]
    power = spec["power_plan"]
    if float(power["max_test_budget"]) > float(risk["max_test_budget"]):
        _fail("G101C_BUNDLE_BUDGET_EXCEEDS_OBJECTIVE")
    if _instant(power["hard_deadline_at"]) > _instant(risk["hard_deadline_at"]):
        _fail("G101C_BUNDLE_DEADLINE_EXCEEDS_OBJECTIVE")
    if _instant(snapshot["data_cutoff_at"]) > _instant(power["hard_deadline_at"]):
        _fail("G101C_BUNDLE_CUTOFF_AFTER_HARD_DEADLINE")
    if bundle["validation_ceiling"] != CEILING:
        _fail("G101C_BUNDLE_CEILING_INVALID")
    _self_hash(bundle, "bundle_hash", "G101C_BUNDLE_HASH_MISMATCH")
    return bundle


def _validate_cell(value: Any) -> dict[str, Any]:
    cell = _exact(value, {
        "cell_id", "role", "copy_version_id", "image_sha", "config_hash", "meta_campaign_id",
        "meta_adset_id", "meta_creative_id", "meta_ad_id", "meta_assignment_cell_id",
        "target_allocation", "actual_allocation", "allocation_verified_at",
    }, "G101C_CELL_SCHEMA_INVALID")
    for field in ("cell_id", "copy_version_id"):
        _identifier(cell[field], "G101C_CELL_IDENTIFIER_INVALID")
    if cell["role"] not in {"CHAMPION", "CHALLENGER"}:
        _fail("G101C_CELL_ROLE_INVALID")
    validate_sha256(cell["image_sha"], code="G101C_CELL_IMAGE_HASH_INVALID")
    validate_sha256(cell["config_hash"], code="G101C_CELL_CONFIG_HASH_INVALID")
    for field in ("meta_campaign_id", "meta_adset_id", "meta_creative_id", "meta_ad_id", "meta_assignment_cell_id"):
        if cell[field] is not None:
            _identifier(cell[field], "G101C_CELL_META_ID_INVALID")
    if type(cell["target_allocation"]) not in {int, float} or float(cell["target_allocation"]) != 0.5:
        _fail("G101C_CELL_TARGET_ALLOCATION_INVALID")
    if cell["actual_allocation"] is not None:
        actual = _nonnegative(cell["actual_allocation"], "G101C_CELL_ACTUAL_ALLOCATION_INVALID")
        if actual > 1:
            _fail("G101C_CELL_ACTUAL_ALLOCATION_INVALID")
    validate_utc(cell["allocation_verified_at"], nullable=True, code="G101C_CELL_READBACK_TIME_INVALID")
    return cell


def _validate_power(value: Any) -> dict[str, Any]:
    power = _exact(value, {
        "power_assessment_id", "alpha", "power", "mde", "target_information",
        "earliest_binding_information_fraction", "hard_deadline_at", "max_test_budget",
    }, "G101C_POWER_SCHEMA_INVALID")
    _identifier(power["power_assessment_id"], "G101C_POWER_ID_INVALID")
    for field in ("alpha", "power", "mde", "target_information", "max_test_budget"):
        _positive(power[field], "G101C_POWER_VALUE_INVALID")
    fraction = _positive(power["earliest_binding_information_fraction"], "G101C_POWER_FRACTION_INVALID")
    if float(power["alpha"]) >= 1 or float(power["power"]) >= 1 or fraction > 1:
        _fail("G101C_POWER_RANGE_INVALID")
    validate_utc(power["hard_deadline_at"], code="G101C_POWER_DEADLINE_INVALID")
    return power


def _validate_evaluation_plan(value: Any) -> dict[str, Any]:
    plan = _exact(value, {
        "method", "boundary_family", "method_version", "d1_role", "d3_role", "final_role",
        "policy_version",
    }, "G101C_EVALUATION_PLAN_SCHEMA_INVALID")
    if (
        plan["method"] != "GROUP_SEQUENTIAL_FREQUENTIST"
        or plan["boundary_family"] != "OBRIEN_FLEMING"
        or plan["d1_role"] != "SAFETY_CHECK"
        or plan["d3_role"] != "TREND_ONLY"
        or plan["final_role"] != "BINDING_EFFECT_DECISION"
    ):
        _fail("G101C_EVALUATION_PLAN_INVALID")
    for field in ("method_version", "policy_version"):
        version = _identifier(plan[field], "G101C_EVALUATION_VERSION_INVALID")
        normalized = version.upper()
        if normalized in {"UNKNOWN", "TBD", "PENDING", "UNSET"}:
            _fail("G101C_EVALUATION_VERSION_INVALID")
        if normalized == "UNFROZEN" and version != "UNFROZEN":
            _fail("G101C_EVALUATION_VERSION_INVALID")
    return plan


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail(code)
    return dict(value)


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        _fail(code)
    return value


def _frozen_identifier(value: Any, code: str) -> str:
    result = _identifier(value, code)
    if result.upper() in {"UNKNOWN", "UNFROZEN", "TBD", "PENDING", "UNSET"}:
        _fail(code)
    return result


def _number(value: Any, code: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        _fail(code)
    return float(value)


def _positive(value: Any, code: str) -> float:
    number = _number(value, code)
    if number <= 0:
        _fail(code)
    return number


def _nonnegative(value: Any, code: str) -> float:
    number = _number(value, code)
    if number < 0:
        _fail(code)
    return number


def _self_hash(value: Mapping[str, Any], field: str, code: str) -> None:
    validate_sha256(value[field], code=code)
    if value[field] != content_hash(value, field):
        _fail(code)


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)


__all__ = [
    "BUNDLE_VERSION_V2", "CEILING", "INVARIANT_PROJECTION_VERSION_V2", "OBJECTIVE_VERSION_V2", "SNAPSHOT_VERSION_V2",
    "SPEC_VERSION_V2", "CanonicalEvaluationContractV2Error", "canonical_hash",
    "canonical_json", "content_hash", "validate_canonical_input_bundle_v2",
    "validate_experiment_spec_v2", "validate_input_snapshot_v2", "validate_invariant_projection_v2",
    "validate_objective_contract_v2",
]
