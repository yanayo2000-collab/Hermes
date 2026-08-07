from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Mapping


OBJECTIVE_VERSION = "gle-objective-contract-v1"
SPEC_VERSION = "gle-experiment-spec-v1"
SNAPSHOT_VERSION = "gle-evaluation-input-snapshot-v1"
EVALUATION_VERSION = "gle-evaluation-record-v1"
INVARIANT_PROJECTION_VERSION = "gle-copy-only-invariant-projection-v1"

EXPERIMENT_STATUSES = frozenset({
    "DRAFT", "PREFLIGHTING", "FEASIBLE", "READY_FOR_APPROVAL", "CREATED_PAUSED", "ACTIVE",
    "COLLECTING_EVIDENCE", "EVALUATING", "CONCLUDED", "CLOSED", "QUASI_REHEARSAL",
    "NOT_FEASIBLE", "SAFETY_STOPPED", "INVALIDATED", "MANUAL_REVIEW",
})
EVALUATION_RESULTS = frozenset({
    "SAFETY_STOP", "DATA_INCOMPLETE", "INVALIDATED", "NOT_ATTRIBUTABLE", "WAITING_EVIDENCE",
    "TREND_POSITIVE", "TREND_NEGATIVE", "EFFECTIVE", "INEFFECTIVE", "NEUTRAL", "CLOSE_FUTILE",
    "CLOSE_UNDERPOWERED",
})
INVARIANT_FIELDS = [
    "IMAGE_SHA", "HEADLINE", "DESCRIPTION", "CTA", "AUDIENCE", "PLACEMENT", "BUDGET",
    "BID_STRATEGY", "OPTIMIZATION_GOAL", "ATTRIBUTION_SETTING",
]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class CanonicalEvaluationContractError(ValueError):
    pass


def _fail(code: str) -> None:
    raise CanonicalEvaluationContractError(code)


def canonical_json(value: Any) -> str:
    _json_value(value)
    return json.dumps(_normalized_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_sha256(value: Any, *, code: str = "G101_HASH_INVALID") -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        _fail(code)
    return value


def validate_utc(value: Any, *, nullable: bool = False, code: str = "G101_TIMESTAMP_INVALID") -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        _fail(code)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _fail(code)
    if parsed.tzinfo != timezone.utc:
        _fail(code)
    return value


def validate_objective_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    body = _exact(value, {
        "schema_version", "objective_contract_id", "version", "contract_hash", "ad_account_id", "market",
        "currency", "business_goal", "primary_metric", "secondary_metrics", "guardrails", "risk_boundary",
        "created_by", "approved_by", "approved_at",
    }, "G101_OBJECTIVE_SCHEMA_INVALID")
    _schema(body, OBJECTIVE_VERSION, "G101_OBJECTIVE_VERSION_MISMATCH")
    _identifier(body["objective_contract_id"], "G101_OBJECTIVE_ID_INVALID")
    _identifier(body["ad_account_id"], "G101_ACCOUNT_ID_INVALID")
    _identifier(body["market"], "G101_MARKET_INVALID")
    if body["currency"] != "USD" or body["business_goal"] != "REDUCE_QUALIFIED_JOIN_CPA":
        _fail("G101_OBJECTIVE_GOAL_INVALID")
    if type(body["version"]) is not int or body["version"] <= 0:
        _fail("G101_OBJECTIVE_NUMBER_INVALID")
    primary = _exact(body["primary_metric"], {
        "metric_key", "definition_version", "attribution_window", "dedup_version",
        "qualification_rule_version", "min_business_improvement", "direction",
    }, "G101_PRIMARY_METRIC_SCHEMA_INVALID")
    if primary["metric_key"] != "QUALIFIED_JOIN_CPA" or primary["direction"] != "LOWER_IS_BETTER":
        _fail("G101_PRIMARY_METRIC_INVALID")
    for field in ("definition_version", "attribution_window", "dedup_version", "qualification_rule_version"):
        _frozen_identifier(primary[field], f"G101_PRIMARY_{field.upper()}_INVALID")
    _positive(primary["min_business_improvement"], "G101_MIN_IMPROVEMENT_INVALID")
    secondary = body["secondary_metrics"]
    if not isinstance(secondary, list):
        _fail("G101_SECONDARY_METRICS_INVALID")
    secondary_keys: list[str] = []
    for item in secondary:
        metric = _exact(item, {"metric_key", "definition_version", "purpose"}, "G101_SECONDARY_METRIC_SCHEMA_INVALID")
        if metric["metric_key"] not in {"CTR", "CPI", "INSTALL_TO_JOIN_CVR"} or metric["purpose"] not in {"DIAGNOSTIC", "TREND_ONLY"}:
            _fail("G101_SECONDARY_METRIC_INVALID")
        _frozen_identifier(metric["definition_version"], "G101_SECONDARY_VERSION_INVALID")
        secondary_keys.append(metric["metric_key"])
    if secondary_keys != sorted(set(secondary_keys)):
        _fail("G101_SECONDARY_ORDER_INVALID")
    guardrail_keys: list[tuple[str, str]] = []
    if not isinstance(body["guardrails"], list):
        _fail("G101_GUARDRAILS_INVALID")
    for item in body["guardrails"]:
        guardrail = _exact(item, {"metric_key", "operator", "threshold", "severity"}, "G101_GUARDRAIL_SCHEMA_INVALID")
        _identifier(guardrail["metric_key"], "G101_GUARDRAIL_METRIC_INVALID")
        if guardrail["operator"] not in {"LTE", "GTE", "DELTA_LTE", "DELTA_GTE"} or guardrail["severity"] not in {"HARD_STOP", "BLOCK_WINNER", "WARN"}:
            _fail("G101_GUARDRAIL_INVALID")
        _number(guardrail["threshold"], "G101_GUARDRAIL_THRESHOLD_INVALID")
        guardrail_keys.append((guardrail["metric_key"], guardrail["operator"]))
    if guardrail_keys != sorted(set(guardrail_keys)):
        _fail("G101_GUARDRAIL_ORDER_INVALID")
    risk = _exact(body["risk_boundary"], {
        "max_test_budget", "max_daily_budget", "max_write_requests_per_action", "hard_deadline_at",
        "approval_ttl_seconds",
    }, "G101_RISK_BOUNDARY_SCHEMA_INVALID")
    _positive(risk["max_test_budget"], "G101_MAX_TEST_BUDGET_INVALID")
    _positive(risk["max_daily_budget"], "G101_MAX_DAILY_BUDGET_INVALID")
    if float(risk["max_daily_budget"]) > float(risk["max_test_budget"]):
        _fail("G101_DAILY_BUDGET_EXCEEDS_TOTAL")
    for field in ("max_write_requests_per_action", "approval_ttl_seconds"):
        if type(risk[field]) is not int or risk[field] <= 0:
            _fail(f"G101_{field.upper()}_INVALID")
    validate_utc(risk["hard_deadline_at"])
    _identifier(body["created_by"], "G101_OBJECTIVE_CREATOR_INVALID")
    _identifier(body["approved_by"], "G101_OBJECTIVE_APPROVER_INVALID")
    approved_at = validate_utc(body["approved_at"])
    if _parse_utc(risk["hard_deadline_at"]) <= _parse_utc(approved_at):
        _fail("G101_OBJECTIVE_DEADLINE_BEFORE_APPROVAL")
    _self_hash(body, "contract_hash", "G101_OBJECTIVE_HASH_MISMATCH")
    return body


def validate_experiment_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    body = _exact(value, {
        "schema_version", "experiment_id", "lineage_id", "parent_experiment_id", "iteration_no",
        "objective_contract_id", "experiment_type", "evidence_target", "unique_variable", "invariant_fields",
        "invariant_config_hash", "assignment", "cells", "power_plan", "evaluation_plan", "status", "spec_hash",
        "created_at", "approved_at",
    }, "G101_SPEC_SCHEMA_INVALID")
    _schema(body, SPEC_VERSION, "G101_SPEC_VERSION_MISMATCH")
    for field in ("experiment_id", "lineage_id", "objective_contract_id"):
        _identifier(body[field], f"G101_{field.upper()}_INVALID")
    if body["parent_experiment_id"] is not None:
        _identifier(body["parent_experiment_id"], "G101_PARENT_EXPERIMENT_ID_INVALID")
        if body["parent_experiment_id"] == body["experiment_id"]:
            _fail("G101_PARENT_SELF_REFERENCE")
    if type(body["iteration_no"]) is not int or body["iteration_no"] <= 0:
        _fail("G101_ITERATION_INVALID")
    if body["experiment_type"] != "COPY_ONLY" or body["evidence_target"] != "CONTROLLED" or body["unique_variable"] != "PRIMARY_TEXT":
        _fail("G101_EXPERIMENT_SCOPE_INVALID")
    if body["invariant_fields"] != INVARIANT_FIELDS:
        _fail("G101_INVARIANTS_INVALID")
    validate_sha256(body["invariant_config_hash"], code="G101_INVARIANT_HASH_INVALID")
    cells = body["cells"]
    if not isinstance(cells, list) or len(cells) != 2:
        _fail("G101_CELL_SET_INVALID")
    normalized_cells = [_validate_cell(item) for item in cells]
    if [item["role"] for item in normalized_cells] != ["CHAMPION", "CHALLENGER"]:
        _fail("G101_CELL_ROLE_ORDER_INVALID")
    if len({item["image_sha"] for item in normalized_cells}) != 1:
        _fail("G101_COPY_ONLY_IMAGE_DRIFT")
    if len({item["config_hash"] for item in normalized_cells}) != 2:
        _fail("G101_CELL_CONFIG_HASH_DUPLICATE")
    campaigns = [item["meta_campaign_id"] for item in normalized_cells]
    if campaigns != [None, None] and (None in campaigns or len(set(campaigns)) != 1):
        _fail("G101_COPY_ONLY_CAMPAIGN_DRIFT")
    for field in ("cell_id", "copy_version_id"):
        if len({item[field] for item in normalized_cells}) != 2:
            _fail(f"G101_CELL_{field.upper()}_DUPLICATE")
    for field in ("meta_adset_id", "meta_creative_id", "meta_ad_id", "meta_assignment_cell_id"):
        values = [item[field] for item in normalized_cells]
        if values != [None, None] and (None in values or len(set(values)) != 2):
            _fail(f"G101_CELL_{field.upper()}_DUPLICATE")
    assignment = _exact(body["assignment"], {
        "mechanism", "capability_assessment_id", "target_allocation", "allowed_allocation_deviation",
        "readback_required",
    }, "G101_ASSIGNMENT_SCHEMA_INVALID")
    _frozen_identifier(assignment["mechanism"], "G101_ASSIGNMENT_MECHANISM_INVALID")
    _identifier(assignment["capability_assessment_id"], "G101_CAPABILITY_ID_INVALID")
    if assignment["readback_required"] is not True:
        _fail("G101_ASSIGNMENT_READBACK_INVALID")
    allocations = assignment["target_allocation"]
    cell_ids = [item["cell_id"] for item in normalized_cells]
    if not isinstance(allocations, Mapping) or set(allocations) != set(cell_ids):
        _fail("G101_ASSIGNMENT_ALLOCATION_KEYS_INVALID")
    if any(type(allocations[cell_id]) not in {int, float} or float(allocations[cell_id]) != 0.5 for cell_id in cell_ids):
        _fail("G101_ASSIGNMENT_ALLOCATION_INVALID")
    deviation = _nonnegative(assignment["allowed_allocation_deviation"], "G101_ALLOCATION_DEVIATION_INVALID")
    if deviation > 0.5:
        _fail("G101_ALLOCATION_DEVIATION_INVALID")
    power = _exact(body["power_plan"], {
        "power_assessment_id", "alpha", "power", "mde", "target_information",
        "earliest_binding_information_fraction", "hard_deadline_at", "max_test_budget",
    }, "G101_POWER_PLAN_SCHEMA_INVALID")
    _identifier(power["power_assessment_id"], "G101_POWER_ASSESSMENT_ID_INVALID")
    for field in ("alpha", "power", "mde", "target_information", "max_test_budget"):
        _positive(power[field], f"G101_POWER_{field.upper()}_INVALID")
    fraction = _positive(power["earliest_binding_information_fraction"], "G101_INFORMATION_FRACTION_INVALID")
    if fraction > 1 or float(power["alpha"]) >= 1 or float(power["power"]) >= 1:
        _fail("G101_POWER_PLAN_RANGE_INVALID")
    validate_utc(power["hard_deadline_at"])
    evaluation = _exact(body["evaluation_plan"], {
        "method", "boundary_family", "method_version", "d1_role", "d3_role", "final_role", "policy_version",
    }, "G101_EVALUATION_PLAN_SCHEMA_INVALID")
    if evaluation["method"] != "GROUP_SEQUENTIAL_FREQUENTIST" or evaluation["boundary_family"] != "OBRIEN_FLEMING":
        _fail("G101_EVALUATION_METHOD_INVALID")
    if evaluation["d1_role"] != "SAFETY_CHECK" or evaluation["d3_role"] != "TREND_ONLY" or evaluation["final_role"] != "BINDING_EFFECT_DECISION":
        _fail("G101_EVALUATION_ROLES_INVALID")
    _identifier(evaluation["method_version"], "G101_METHOD_VERSION_INVALID")
    _identifier(evaluation["policy_version"], "G101_POLICY_VERSION_INVALID")
    if evaluation["method_version"] != "UNFROZEN" or evaluation["policy_version"] != "UNFROZEN":
        _fail("G101_METHOD_OR_POLICY_REQUIRES_LATER_GATE")
    if body["status"] not in EXPERIMENT_STATUSES:
        _fail("G101_EXPERIMENT_STATUS_INVALID")
    if body["status"] != "DRAFT" or body["approved_at"] is not None:
        _fail("G101_FOUNDATION_SPEC_MUST_REMAIN_DRAFT")
    created_at = _parse_utc(validate_utc(body["created_at"]))
    approved_at = validate_utc(body["approved_at"], nullable=True)
    if approved_at is not None and _parse_utc(approved_at) < created_at:
        _fail("G101_APPROVAL_BEFORE_CREATION")
    if _parse_utc(power["hard_deadline_at"]) <= created_at:
        _fail("G101_DEADLINE_BEFORE_CREATION")
    if body["iteration_no"] == 1 and body["parent_experiment_id"] is not None:
        _fail("G101_LINEAGE_PARENT_INVALID")
    if body["iteration_no"] > 1 and body["parent_experiment_id"] is None:
        _fail("G101_LINEAGE_PARENT_INVALID")
    actuals = [item["actual_allocation"] for item in normalized_cells]
    verified = [item["allocation_verified_at"] for item in normalized_cells]
    if actuals != [None, None] or verified != [None, None]:
        _fail("G101_DRAFT_ACTUAL_ALLOCATION_FORBIDDEN")
    if actuals == [None, None]:
        if verified != [None, None]:
            _fail("G101_ACTUAL_ALLOCATION_READBACK_INVALID")
    else:
        if None in actuals or None in verified:
            _fail("G101_ACTUAL_ALLOCATION_READBACK_INVALID")
        if not math.isclose(sum(float(item) for item in actuals), 1.0, rel_tol=0, abs_tol=1e-12):
            _fail("G101_ACTUAL_ALLOCATION_INVALID")
        if any(abs(float(item) - 0.5) > deviation for item in actuals):
            _fail("G101_ACTUAL_ALLOCATION_DEVIATION_EXCEEDED")
    _self_hash(body, "spec_hash", "G101_SPEC_HASH_MISMATCH")
    return body


def validate_invariant_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    body = _exact(value, {
        "schema_version", "experiment_id", "invariant_field_hashes", "projection_hash",
    }, "G101_INVARIANT_PROJECTION_SCHEMA_INVALID")
    _schema(body, INVARIANT_PROJECTION_VERSION, "G101_INVARIANT_PROJECTION_VERSION_MISMATCH")
    _identifier(body["experiment_id"], "G101_INVARIANT_EXPERIMENT_ID_INVALID")
    field_hashes = body["invariant_field_hashes"]
    if not isinstance(field_hashes, Mapping) or set(field_hashes) != set(INVARIANT_FIELDS):
        _fail("G101_INVARIANT_FIELD_HASHES_INVALID")
    for field in INVARIANT_FIELDS:
        validate_sha256(field_hashes[field], code="G101_INVARIANT_FIELD_HASH_INVALID")
    _self_hash(body, "projection_hash", "G101_INVARIANT_PROJECTION_HASH_MISMATCH")
    return body


def _normalized_json(value: Any) -> Any:
    if type(value) is float and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_normalized_json(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _normalized_json(item) for key, item in value.items()}
    return value


def validate_input_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    body = _exact(value, {
        "schema_version", "snapshot_id", "experiment_id", "checkpoint", "data_cutoff_at", "created_at",
        "experiment_spec_hash", "objective_contract_hash", "evaluator_version", "policy_version",
        "attribution_version", "dedup_version", "cell_metrics", "data_quality", "mutation_events", "snapshot_hash",
    }, "G101_SNAPSHOT_SCHEMA_INVALID")
    _schema(body, SNAPSHOT_VERSION, "G101_SNAPSHOT_VERSION_MISMATCH")
    _identifier(body["snapshot_id"], "G101_SNAPSHOT_ID_INVALID")
    _identifier(body["experiment_id"], "G101_SNAPSHOT_EXPERIMENT_ID_INVALID")
    if body["checkpoint"] not in {"D1", "D3", "INFORMATION_LOOK", "FINAL", "HARD_STOP"}:
        _fail("G101_SNAPSHOT_CHECKPOINT_INVALID")
    cutoff = _parse_utc(validate_utc(body["data_cutoff_at"]))
    created = _parse_utc(validate_utc(body["created_at"]))
    if cutoff > created:
        _fail("G101_CUTOFF_AFTER_CREATED_AT")
    for field in ("experiment_spec_hash", "objective_contract_hash"):
        validate_sha256(body[field], code=f"G101_{field.upper()}_INVALID")
    for field in ("evaluator_version", "policy_version", "attribution_version", "dedup_version"):
        _identifier(body[field], f"G101_SNAPSHOT_{field.upper()}_INVALID")
    if not isinstance(body["cell_metrics"], Mapping) or len(body["cell_metrics"]) != 2:
        _fail("G101_CELL_METRICS_INVALID")
    for cell_id, metrics in body["cell_metrics"].items():
        _identifier(cell_id, "G101_METRIC_CELL_ID_INVALID")
        item = _exact(metrics, {"spend", "impressions", "clicks", "installs", "qualified_joins", "invalid_users", "allocation_share"}, "G101_CELL_METRIC_SCHEMA_INVALID")
        _nonnegative(item["spend"], "G101_CELL_METRIC_SPEND_INVALID")
        for field in ("impressions", "clicks", "installs", "qualified_joins", "invalid_users"):
            if type(item[field]) is not int or item[field] < 0:
                _fail(f"G101_CELL_METRIC_{field.upper()}_INVALID")
        share = _nonnegative(item["allocation_share"], "G101_CELL_METRIC_ALLOCATION_SHARE_INVALID")
        if share > 1:
            _fail("G101_CELL_METRIC_ALLOCATION_SHARE_INVALID")
    quality = _exact(body["data_quality"], {"freshness_ok", "attribution_coverage", "missing_sources", "duplicate_rate"}, "G101_DATA_QUALITY_SCHEMA_INVALID")
    if type(quality["freshness_ok"]) is not bool:
        _fail("G101_FRESHNESS_FLAG_INVALID")
    for field in ("attribution_coverage", "duplicate_rate"):
        number = _nonnegative(quality[field], f"G101_{field.upper()}_INVALID")
        if number > 1:
            _fail(f"G101_{field.upper()}_INVALID")
    if not isinstance(quality["missing_sources"], list) or quality["missing_sources"] != sorted(set(quality["missing_sources"])):
        _fail("G101_MISSING_SOURCES_INVALID")
    if not isinstance(body["mutation_events"], list):
        _fail("G101_MUTATION_EVENTS_INVALID")
    mutation_keys: list[tuple[str, str, str]] = []
    for event in body["mutation_events"]:
        item = _exact(event, {"object_id", "field", "before", "after", "changed_at", "source"}, "G101_MUTATION_EVENT_SCHEMA_INVALID")
        _identifier(item["object_id"], "G101_MUTATION_OBJECT_INVALID")
        _identifier(item["field"], "G101_MUTATION_FIELD_INVALID")
        changed = validate_utc(item["changed_at"])
        if _parse_utc(changed) > cutoff:
            _fail("G101_MUTATION_AFTER_CUTOFF")
        if item["source"] not in {"GLE", "EXTERNAL", "UNKNOWN"}:
            _fail("G101_MUTATION_SOURCE_INVALID")
        mutation_keys.append((changed, item["object_id"], item["field"]))
    if mutation_keys != sorted(mutation_keys):
        _fail("G101_MUTATION_ORDER_INVALID")
    _self_hash(body, "snapshot_hash", "G101_SNAPSHOT_HASH_MISMATCH")
    return body


def validate_evaluation_record(value: Mapping[str, Any]) -> dict[str, Any]:
    body = _exact(value, {
        "schema_version", "evaluation_id", "experiment_id", "snapshot_id", "evaluation_version",
        "checkpoint_role", "information_fraction", "primary_effect_estimate", "confidence_interval",
        "alpha_boundary", "min_business_improvement", "safety_status", "data_status",
        "contamination_status", "maturity_status", "guardrail_status", "evidence_level", "result",
        "reason_codes", "blocking_reason", "next_evaluation_at", "evaluator_version", "evaluated_at",
    }, "G101_EVALUATION_SCHEMA_INVALID")
    _schema(body, EVALUATION_VERSION, "G101_EVALUATION_VERSION_MISMATCH")
    for field in ("evaluation_id", "experiment_id", "snapshot_id", "evaluator_version"):
        _identifier(body[field], f"G101_EVALUATION_{field.upper()}_INVALID")
    if type(body["evaluation_version"]) is not int or body["evaluation_version"] <= 0:
        _fail("G101_EVALUATION_NUMBER_INVALID")
    if body["checkpoint_role"] not in {"SAFETY_CHECK", "TREND_ONLY", "BINDING_EFFECT_DECISION"}:
        _fail("G101_CHECKPOINT_ROLE_INVALID")
    fraction = _nonnegative(body["information_fraction"], "G101_INFORMATION_FRACTION_INVALID")
    if fraction > 1:
        _fail("G101_INFORMATION_FRACTION_INVALID")
    for field in ("primary_effect_estimate", "alpha_boundary"):
        if body[field] is not None:
            _number(body[field], f"G101_{field.upper()}_INVALID")
    if body["confidence_interval"] is not None:
        interval = _exact(body["confidence_interval"], {"lower", "upper"}, "G101_CONFIDENCE_INTERVAL_INVALID")
        lower = _number(interval["lower"], "G101_CONFIDENCE_INTERVAL_INVALID")
        upper = _number(interval["upper"], "G101_CONFIDENCE_INTERVAL_INVALID")
        if lower > upper:
            _fail("G101_CONFIDENCE_INTERVAL_INVALID")
    _positive(body["min_business_improvement"], "G101_MIN_IMPROVEMENT_INVALID")
    enums = {
        "safety_status": {"PASS", "STOP"}, "data_status": {"COMPLETE", "INCOMPLETE", "UNRECOVERABLE"},
        "contamination_status": {"CLEAN", "MIXED_CHANGE", "SEVERE"},
        "maturity_status": {"IMMATURE", "MATURE", "UNDERPOWERED"},
        "guardrail_status": {"PASS", "WARN", "FAIL"},
        "evidence_level": {"OBSERVATIONAL", "QUASI", "CONTROLLED", "REPLICATED"},
    }
    for field, allowed in enums.items():
        if body[field] not in allowed:
            _fail(f"G101_{field.upper()}_INVALID")
    if body["result"] not in EVALUATION_RESULTS:
        _fail("G101_EVALUATION_RESULT_INVALID")
    _reason_codes(body["reason_codes"])
    if body["blocking_reason"] is not None:
        _identifier(body["blocking_reason"], "G101_BLOCKING_REASON_INVALID")
    next_at = validate_utc(body["next_evaluation_at"], nullable=True)
    evaluated_at = validate_utc(body["evaluated_at"])
    if next_at is not None and _parse_utc(next_at) <= _parse_utc(evaluated_at):
        _fail("G101_NEXT_EVALUATION_NOT_FUTURE")
    _evaluation_state_lattice(body)
    return body


def validate_canonical_bundle(value: Mapping[str, Any]) -> dict[str, Any]:
    bundle = _exact(value, {
        "bundle_purpose", "objective_contract", "invariant_projection", "experiment_spec",
        "input_snapshot", "evaluation_record",
    }, "G101_BUNDLE_SCHEMA_INVALID")
    if bundle["bundle_purpose"] != "SYNTHETIC_CONTRACT_FIXTURE":
        _fail("G101_BUNDLE_PURPOSE_INVALID")
    objective = validate_objective_contract(bundle["objective_contract"])
    invariant = validate_invariant_projection(bundle["invariant_projection"])
    spec = validate_experiment_spec(bundle["experiment_spec"])
    snapshot = validate_input_snapshot(bundle["input_snapshot"])
    evaluation = validate_evaluation_record(bundle["evaluation_record"])
    if spec["objective_contract_id"] != objective["objective_contract_id"]:
        _fail("G101_BUNDLE_OBJECTIVE_ID_MISMATCH")
    if invariant["experiment_id"] != spec["experiment_id"] or invariant["projection_hash"] != spec["invariant_config_hash"]:
        _fail("G101_BUNDLE_INVARIANT_MISMATCH")
    if invariant["invariant_field_hashes"]["IMAGE_SHA"] != spec["cells"][0]["image_sha"]:
        _fail("G101_BUNDLE_INVARIANT_MISMATCH")
    if snapshot["objective_contract_hash"] != objective["contract_hash"] or snapshot["experiment_spec_hash"] != spec["spec_hash"]:
        _fail("G101_BUNDLE_HASH_MISMATCH")
    if snapshot["experiment_id"] != spec["experiment_id"] or evaluation["experiment_id"] != spec["experiment_id"]:
        _fail("G101_BUNDLE_EXPERIMENT_MISMATCH")
    if evaluation["snapshot_id"] != snapshot["snapshot_id"] or evaluation["evaluator_version"] != snapshot["evaluator_version"]:
        _fail("G101_BUNDLE_EVALUATION_MISMATCH")
    if set(snapshot["cell_metrics"]) != {item["cell_id"] for item in spec["cells"]}:
        _fail("G101_BUNDLE_CELL_SET_MISMATCH")
    primary = objective["primary_metric"]
    if snapshot["policy_version"] != spec["evaluation_plan"]["policy_version"]:
        _fail("G101_BUNDLE_POLICY_MISMATCH")
    if snapshot["evaluator_version"] != spec["evaluation_plan"]["method_version"]:
        _fail("G101_BUNDLE_EVALUATOR_METHOD_MISMATCH")
    if snapshot["attribution_version"] != primary["definition_version"] or snapshot["dedup_version"] != primary["dedup_version"]:
        _fail("G101_BUNDLE_METRIC_VERSION_MISMATCH")
    if float(evaluation["min_business_improvement"]) != float(primary["min_business_improvement"]):
        _fail("G101_BUNDLE_IMPROVEMENT_MISMATCH")
    checkpoint_roles = {
        "D1": "SAFETY_CHECK", "D3": "TREND_ONLY", "INFORMATION_LOOK": "BINDING_EFFECT_DECISION",
        "FINAL": "BINDING_EFFECT_DECISION", "HARD_STOP": "SAFETY_CHECK",
    }
    if evaluation["checkpoint_role"] != checkpoint_roles[snapshot["checkpoint"]]:
        _fail("G101_BUNDLE_CHECKPOINT_ROLE_MISMATCH")
    if _parse_utc(evaluation["evaluated_at"]) < _parse_utc(snapshot["created_at"]):
        _fail("G101_BUNDLE_EVALUATED_BEFORE_SNAPSHOT")
    if _parse_utc(spec["created_at"]) < _parse_utc(objective["approved_at"]):
        _fail("G101_BUNDLE_SPEC_BEFORE_OBJECTIVE_APPROVAL")
    if _parse_utc(snapshot["data_cutoff_at"]) < _parse_utc(spec["created_at"]):
        _fail("G101_BUNDLE_CUTOFF_BEFORE_SPEC")
    if _parse_utc(snapshot["created_at"]) < _parse_utc(spec["created_at"]):
        _fail("G101_BUNDLE_SNAPSHOT_BEFORE_SPEC")
    risk = objective["risk_boundary"]
    power = spec["power_plan"]
    if float(power["max_test_budget"]) > float(risk["max_test_budget"]):
        _fail("G101_BUNDLE_BUDGET_EXCEEDS_OBJECTIVE")
    if _parse_utc(power["hard_deadline_at"]) > _parse_utc(risk["hard_deadline_at"]):
        _fail("G101_BUNDLE_DEADLINE_EXCEEDS_OBJECTIVE")
    if spec["evaluation_plan"]["method_version"] == "UNFROZEN" and snapshot["checkpoint"] in {"INFORMATION_LOOK", "FINAL"}:
        if evaluation["result"] not in {"WAITING_EVIDENCE", "DATA_INCOMPLETE", "INVALIDATED", "NOT_ATTRIBUTABLE"}:
            _fail("G101_BINDING_METHOD_UNFROZEN")
    quality = snapshot["data_quality"]
    if evaluation["data_status"] == "COMPLETE" and (
        not quality["freshness_ok"]
        or quality["missing_sources"]
        or float(quality["attribution_coverage"]) != 1.0
        or float(quality["duplicate_rate"]) != 0.0
    ):
        _fail("G101_BUNDLE_DATA_QUALITY_MISMATCH")
    events = snapshot["mutation_events"]
    if any(item["source"] in {"EXTERNAL", "UNKNOWN"} for item in events) and evaluation["contamination_status"] != "SEVERE":
        _fail("G101_BUNDLE_CONTAMINATION_MISMATCH")
    if events and evaluation["contamination_status"] == "CLEAN":
        _fail("G101_BUNDLE_CONTAMINATION_MISMATCH")
    shares = [float(item["allocation_share"]) for item in snapshot["cell_metrics"].values()]
    if not math.isclose(sum(shares), 1.0, rel_tol=0, abs_tol=1e-12):
        _fail("G101_BUNDLE_ALLOCATION_INVALID")
    if any(abs(item - 0.5) > float(spec["assignment"]["allowed_allocation_deviation"]) for item in shares):
        _fail("G101_BUNDLE_ALLOCATION_DEVIATION_EXCEEDED")
    if evaluation["evidence_level"] != "OBSERVATIONAL":
        _fail("G101_FOUNDATION_EVIDENCE_LEVEL_EXCEEDED")
    return bundle


def _evaluation_state_lattice(body: Mapping[str, Any]) -> None:
    role_results = {
        "SAFETY_CHECK": {"SAFETY_STOP", "DATA_INCOMPLETE", "INVALIDATED", "NOT_ATTRIBUTABLE", "WAITING_EVIDENCE"},
        "TREND_ONLY": {"SAFETY_STOP", "DATA_INCOMPLETE", "INVALIDATED", "NOT_ATTRIBUTABLE", "WAITING_EVIDENCE", "TREND_POSITIVE", "TREND_NEGATIVE"},
        "BINDING_EFFECT_DECISION": EVALUATION_RESULTS,
    }
    if body["result"] not in role_results[body["checkpoint_role"]]:
        _fail("G101_RESULT_NOT_ALLOWED_FOR_CHECKPOINT")
    safety_stop = body["safety_status"] == "STOP" or body["guardrail_status"] == "FAIL"
    if safety_stop:
        if body["result"] != "SAFETY_STOP":
            _fail("G101_SAFETY_RESULT_MISMATCH")
    else:
        if body["result"] == "SAFETY_STOP":
            _fail("G101_SAFETY_RESULT_MISMATCH")
        if body["result"] == "DATA_INCOMPLETE" and body["data_status"] != "INCOMPLETE":
            _fail("G101_DATA_RESULT_MISMATCH")
        if body["data_status"] == "UNRECOVERABLE" and body["result"] != "INVALIDATED":
            _fail("G101_DATA_RESULT_MISMATCH")
        if body["result"] == "INVALIDATED" and body["data_status"] != "UNRECOVERABLE" and body["contamination_status"] not in {"MIXED_CHANGE", "SEVERE"}:
            _fail("G101_INVALIDATION_RESULT_MISMATCH")
        if body["result"] == "NOT_ATTRIBUTABLE" and body["contamination_status"] == "CLEAN":
            _fail("G101_CONTAMINATION_RESULT_MISMATCH")
        if body["data_status"] in {"INCOMPLETE", "UNRECOVERABLE"} and body["result"] not in {"DATA_INCOMPLETE", "WAITING_EVIDENCE", "INVALIDATED"}:
            _fail("G101_DATA_RESULT_MISMATCH")
        if body["contamination_status"] in {"MIXED_CHANGE", "SEVERE"} and body["result"] not in {"INVALIDATED", "NOT_ATTRIBUTABLE"}:
            _fail("G101_CONTAMINATION_RESULT_MISMATCH")
    binding = {"EFFECTIVE", "INEFFECTIVE", "NEUTRAL", "CLOSE_FUTILE", "CLOSE_UNDERPOWERED"}
    if body["result"] in binding:
        expected_maturity = "UNDERPOWERED" if body["result"] == "CLOSE_UNDERPOWERED" else "MATURE"
        if (
            body["checkpoint_role"] != "BINDING_EFFECT_DECISION"
            or body["data_status"] != "COMPLETE"
            or body["contamination_status"] != "CLEAN"
            or body["maturity_status"] != expected_maturity
            or body["guardrail_status"] != "PASS"
            or body["evidence_level"] not in {"CONTROLLED", "REPLICATED"}
        ):
            _fail("G101_BINDING_RESULT_INELIGIBLE")
        if body["result"] != "CLOSE_UNDERPOWERED" and any(
            body[field] is None for field in ("primary_effect_estimate", "confidence_interval", "alpha_boundary")
        ):
            _fail("G101_BINDING_STATISTICS_MISSING")
        if body["blocking_reason"] is not None or body["next_evaluation_at"] is not None:
            _fail("G101_BINDING_RESULT_NOT_FINAL")
    if body["result"] in {"TREND_POSITIVE", "TREND_NEGATIVE"} and body["primary_effect_estimate"] is None:
        _fail("G101_TREND_EFFECT_MISSING")
    if not body["reason_codes"]:
        _fail("G101_RESULT_REASONS_REQUIRED")
    if body["result"] in {"DATA_INCOMPLETE", "WAITING_EVIDENCE", "INVALIDATED", "NOT_ATTRIBUTABLE", "SAFETY_STOP"} and body["blocking_reason"] is None:
        _fail("G101_BLOCKING_REASON_REQUIRED")


def content_hash(value: Mapping[str, Any], hash_field: str) -> str:
    return canonical_hash({key: item for key, item in value.items() if key != hash_field})


def _validate_cell(value: Any) -> dict[str, Any]:
    body = _exact(value, {
        "cell_id", "role", "copy_version_id", "image_sha", "config_hash", "meta_campaign_id",
        "meta_adset_id", "meta_creative_id", "meta_ad_id", "meta_assignment_cell_id", "target_allocation",
        "actual_allocation", "allocation_verified_at",
    }, "G101_CELL_SCHEMA_INVALID")
    for field in ("cell_id", "copy_version_id"):
        _identifier(body[field], f"G101_CELL_{field.upper()}_INVALID")
    if body["role"] not in {"CHAMPION", "CHALLENGER"}:
        _fail("G101_CELL_ROLE_INVALID")
    for field in ("image_sha", "config_hash"):
        validate_sha256(body[field], code=f"G101_CELL_{field.upper()}_INVALID")
    for field in ("meta_campaign_id", "meta_adset_id", "meta_creative_id", "meta_ad_id", "meta_assignment_cell_id"):
        if body[field] is not None:
            _identifier(body[field], f"G101_CELL_{field.upper()}_INVALID")
    if float(_positive(body["target_allocation"], "G101_CELL_ALLOCATION_INVALID")) != 0.5:
        _fail("G101_CELL_ALLOCATION_INVALID")
    if body["actual_allocation"] is not None:
        actual = _nonnegative(body["actual_allocation"], "G101_ACTUAL_ALLOCATION_INVALID")
        if actual > 1:
            _fail("G101_ACTUAL_ALLOCATION_INVALID")
    validate_utc(body["allocation_verified_at"], nullable=True)
    return body


def _exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail(code)
    body = dict(value)
    _json_value(body)
    return body


def _json_value(value: Any) -> None:
    if value is None or isinstance(value, (str, bool)) or type(value) is int:
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail("G101_NON_FINITE_NUMBER")
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item)
        return
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            _fail("G101_JSON_KEY_INVALID")
        for item in value.values():
            _json_value(item)
        return
    _fail("G101_JSON_TYPE_INVALID")


def _schema(body: Mapping[str, Any], expected: str, code: str) -> None:
    if body.get("schema_version") != expected:
        _fail(code)


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(code)
    return value


def _frozen_identifier(value: Any, code: str) -> str:
    result = _identifier(value, code)
    if result in {"UNKNOWN", "UNFROZEN"}:
        _fail(code)
    return result


def _number(value: Any, code: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        _fail(code)
    return float(value)


def _nonnegative(value: Any, code: str) -> float:
    result = _number(value, code)
    if result < 0:
        _fail(code)
    return result


def _positive(value: Any, code: str) -> float:
    result = _number(value, code)
    if result <= 0:
        _fail(code)
    return result


def _reason_codes(value: Any) -> None:
    if not isinstance(value, list) or value != sorted(set(value)):
        _fail("G101_REASON_CODES_INVALID")
    if any(not isinstance(item, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", item) for item in value):
        _fail("G101_REASON_CODES_INVALID")


def _self_hash(body: Mapping[str, Any], field: str, code: str) -> None:
    validate_sha256(body[field], code=code)
    if body[field] != content_hash(body, field):
        _fail(code)


def _parse_utc(value: str | None) -> datetime:
    if value is None:
        _fail("G101_TIMESTAMP_INVALID")
    return datetime.fromisoformat(value[:-1] + "+00:00")
