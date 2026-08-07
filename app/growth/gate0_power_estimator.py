"""Deterministic fixed-endpoint Power diagnostics for GLE Gate 0.

This module intentionally does not implement group-sequential or O'Brien-
Fleming boundaries.  It provides a lower-bound, fixed-endpoint feasibility
calculation while that separate Gate 1 contract remains unfrozen.
"""

from __future__ import annotations

import hashlib
import math
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
from typing import Any, Dict, Mapping

from app.growth.common import canonical_json


INPUT_VERSION = "gle-g0-fixed-endpoint-power-input-v1"
ESTIMATOR_VERSION = "gle-two-sample-poisson-log-rate-ratio-fixed-endpoint-v1"
ESTIMAND = "QUALIFIED_JOINS_PER_USD"
OFFSET = "SPEND_USD"
ALTERNATIVE = "TWO_SIDED"
APPROXIMATION = "WALD_NORMAL_APPROXIMATION"
OBF_BOUNDARY_STATUS = "UNFROZEN"

_Z_TWO_SIDED_005 = Decimal("1.9599639845400534")
_Z_POWER_080 = Decimal("0.8416212335729144")


class PowerContractError(ValueError):
    """The fixed-endpoint input is not the frozen Gate 0 contract."""


def _hash_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _decimal(value: Any, code: str, *, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PowerContractError(code)
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PowerContractError(code) from None
    if not number.is_finite() or (minimum is not None and number < minimum):
        raise PowerContractError(code)
    return number


def _integer(value: Any, code: str, *, minimum: int = 0) -> int:
    number = _decimal(value, code, minimum=Decimal(minimum))
    if number != number.to_integral_value():
        raise PowerContractError(code)
    return int(number)


def _string(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PowerContractError(code)
    return value


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _normal_cdf(value: Decimal) -> Decimal:
    result = Decimal(str(0.5 * (1.0 + math.erf(float(value) / math.sqrt(2.0)))))
    return result


def target_information(*, alpha_two_sided: Decimal, desired_power: Decimal, rate_ratio: Decimal) -> Decimal:
    if alpha_two_sided != Decimal("0.05") or desired_power != Decimal("0.8"):
        raise PowerContractError("G0_POWER_UNSUPPORTED_ALPHA_OR_POWER")
    if rate_ratio != Decimal("1.3"):
        raise PowerContractError("G0_POWER_UNSUPPORTED_MDE")
    with localcontext() as context:
        context.prec = 40
        theta = rate_ratio.ln()
        return ((_Z_TWO_SIDED_005 + _Z_POWER_080) / theta) ** 2


def information_from_total_events(total_events: Decimal, *, rate_ratio: Decimal) -> Decimal:
    events = _decimal(total_events, "G0_POWER_EVENTS_INVALID", minimum=Decimal("0"))
    if rate_ratio <= 0:
        raise PowerContractError("G0_POWER_RATE_RATIO_INVALID")
    with localcontext() as context:
        context.prec = 40
        return events * rate_ratio / ((Decimal("1") + rate_ratio) ** 2)


def two_sided_power(information: Decimal, *, rate_ratio: Decimal) -> Decimal:
    info = _decimal(information, "G0_POWER_INFORMATION_INVALID", minimum=Decimal("0"))
    with localcontext() as context:
        context.prec = 40
        delta = info.sqrt() * abs(rate_ratio.ln())
    upper = Decimal("1") - _normal_cdf(_Z_TWO_SIDED_005 - delta)
    lower = _normal_cdf(-_Z_TWO_SIDED_005 - delta)
    return upper + lower


def _contract_body() -> Dict[str, Any]:
    return {
        "schema_version": INPUT_VERSION,
        "estimator_version": ESTIMATOR_VERSION,
        "estimand": ESTIMAND,
        "offset": OFFSET,
        "alternative": ALTERNATIVE,
        "approximation": APPROXIMATION,
        "control_allocation": "0.5",
        "treatment_allocation": "0.5",
        "alpha_two_sided": "0.05",
        "desired_power": "0.8",
        "mde_relative": "0.3",
        "rate_ratio": "1.3",
        "obf_boundary_status": OBF_BOUNDARY_STATUS,
    }


CONTRACT_HASH = _hash_json(_contract_body())


def golden_vectors() -> Dict[str, Any]:
    rate_ratio = Decimal("1.3")
    target = target_information(
        alpha_two_sided=Decimal("0.05"), desired_power=Decimal("0.8"), rate_ratio=rate_ratio,
    )
    vectors = []
    for total in (Decimal("463"), Decimal("464"), Decimal("45")):
        information = information_from_total_events(total, rate_ratio=rate_ratio)
        power = two_sided_power(information, rate_ratio=rate_ratio)
        vectors.append({
            "total_events": _decimal_text(total),
            "information": _decimal_text(information.quantize(Decimal("0.000000000001"))),
            "power": _decimal_text(power.quantize(Decimal("0.000000000001"))),
            "meets_fixed_endpoint_target": information >= target,
        })
    body = {
        "schema_version": "gle-g0-fixed-endpoint-power-golden-v1",
        "contract_hash": CONTRACT_HASH,
        "target_information": _decimal_text(target.quantize(Decimal("0.000000000001"))),
        "required_total_events_ceiling": int(
            (target * (Decimal("2.3") ** 2) / Decimal("1.3")).to_integral_value(rounding=ROUND_CEILING)
        ),
        "vectors": vectors,
    }
    body["golden_vector_hash"] = _hash_json(body)
    return body


GOLDEN_VECTOR_HASH = "53efdf45d131f6a02bd17580823120f30458def9b5fd7cd2cc6813c9ad9278b3"


def validate_golden_vectors(raw: Mapping[str, Any]) -> Dict[str, Any]:
    expected = golden_vectors()
    if expected["golden_vector_hash"] != GOLDEN_VECTOR_HASH:
        raise PowerContractError("G0_POWER_GOLDEN_VECTOR_IMPLEMENTATION_DRIFT")
    if not isinstance(raw, Mapping) or dict(raw) != expected:
        raise PowerContractError("G0_POWER_GOLDEN_VECTOR_MISMATCH")
    return expected


def assess_fixed_endpoint_power(raw: Mapping[str, Any]) -> Dict[str, Any]:
    keys = {
        "schema_version", "estimator_version", "estimand", "offset", "alternative",
        "approximation", "control_allocation", "treatment_allocation", "alpha_two_sided", "desired_power",
        "mde_relative", "baseline_qualified_joins", "baseline_spend_usd", "evidence_status",
        "incomplete_reasons", "expected_daily_spend_usd", "maximum_test_days",
        "maximum_test_budget_usd",
    }
    if not isinstance(raw, Mapping) or set(raw) != keys:
        raise PowerContractError("G0_POWER_INPUT_SCHEMA_INVALID")
    data = dict(raw)
    frozen = _contract_body()
    for key in (
        "schema_version", "estimator_version", "estimand", "offset", "alternative",
        "approximation", "control_allocation", "treatment_allocation", "alpha_two_sided", "desired_power",
        "mde_relative",
    ):
        expected_key = "schema_version" if key == "schema_version" else key
        if str(data[key]) != str(frozen[expected_key]):
            raise PowerContractError("G0_POWER_FROZEN_CONTRACT_MISMATCH")

    events = _integer(data["baseline_qualified_joins"], "G0_POWER_BASELINE_INVALID")
    spend = _decimal(data["baseline_spend_usd"], "G0_POWER_BASELINE_INVALID", minimum=Decimal("0"))
    daily_spend = _decimal(
        data["expected_daily_spend_usd"], "G0_POWER_BUDGET_INVALID", minimum=Decimal("0.000001"),
    )
    max_days = _integer(data["maximum_test_days"], "G0_POWER_BUDGET_INVALID", minimum=1)
    max_budget = _decimal(
        data["maximum_test_budget_usd"], "G0_POWER_BUDGET_INVALID", minimum=Decimal("0.000001"),
    )
    status = _string(data["evidence_status"], "G0_POWER_EVIDENCE_STATUS_INVALID")
    reasons_raw = data["incomplete_reasons"]
    if not isinstance(reasons_raw, list) or any(not isinstance(item, str) or not item for item in reasons_raw):
        raise PowerContractError("G0_POWER_EVIDENCE_STATUS_INVALID")
    reasons = sorted(set(reasons_raw))
    if status not in {"READY", "INCOMPLETE"} or (status == "READY") != (not reasons):
        raise PowerContractError("G0_POWER_EVIDENCE_STATUS_INVALID")

    rate_ratio = Decimal("1.3")
    target = target_information(
        alpha_two_sided=Decimal("0.05"), desired_power=Decimal("0.8"), rate_ratio=rate_ratio,
    )
    required_total = target * ((Decimal("1") + rate_ratio) ** 2) / rate_ratio
    result: Dict[str, Any] = {
        "schema_version": "gle-g0-fixed-endpoint-power-assessment-v1",
        "estimator_version": ESTIMATOR_VERSION,
        "contract_hash": CONTRACT_HASH,
        "golden_vector_hash": GOLDEN_VECTOR_HASH,
        "estimand": ESTIMAND,
        "offset": OFFSET,
        "alternative": ALTERNATIVE,
        "approximation": APPROXIMATION,
        "obf_boundary_status": OBF_BOUNDARY_STATUS,
        "target_information": _decimal_text(target),
        "required_total_events": _decimal_text(required_total),
        "required_total_events_ceiling": int(required_total.to_integral_value(rounding=ROUND_CEILING)),
        "baseline_rate_per_usd": None,
        "projected_daily_control_events": None,
        "projected_daily_treatment_events": None,
        "projected_daily_total_events": None,
        "projected_information_per_day": None,
        "projected_power_at_max_budget": None,
        "expected_days_to_maturity": None,
        "expected_total_spend_usd": None,
        "fixed_endpoint_status": "UNKNOWN",
        "feasible": False,
        "reason_codes": reasons,
    }
    if status == "INCOMPLETE":
        return result
    if events == 0:
        result["reason_codes"] = ["BASELINE_EVENT_RATE_UNKNOWN"]
        return result
    if spend == 0:
        result["reason_codes"] = ["BASELINE_SPEND_DENOMINATOR_ZERO"]
        return result

    with localcontext() as context:
        context.prec = 40
        baseline_rate = Decimal(events) / spend
        daily_control_events = baseline_rate * daily_spend * Decimal("0.5")
        daily_treatment_events = daily_control_events * rate_ratio
        daily_events = daily_control_events + daily_treatment_events
        daily_information = daily_control_events * daily_treatment_events / daily_events
        expected_days = target / daily_information
        expected_spend = expected_days * daily_spend
        control_events_at_max_budget = baseline_rate * max_budget * Decimal("0.5")
        treatment_events_at_max_budget = control_events_at_max_budget * rate_ratio
        events_at_max_budget = control_events_at_max_budget + treatment_events_at_max_budget
    power_at_max_budget = two_sided_power(
        information_from_total_events(events_at_max_budget, rate_ratio=rate_ratio), rate_ratio=rate_ratio,
    )
    failures = []
    if expected_days > Decimal(max_days):
        failures.append("EXPECTED_MATURITY_EXCEEDS_LIMIT")
    if expected_spend > max_budget:
        failures.append("EXPECTED_SPEND_EXCEEDS_BUDGET")
    result.update({
        "baseline_rate_per_usd": _decimal_text(baseline_rate),
        "projected_daily_control_events": _decimal_text(daily_control_events),
        "projected_daily_treatment_events": _decimal_text(daily_treatment_events),
        "projected_daily_total_events": _decimal_text(daily_events),
        "projected_information_per_day": _decimal_text(daily_information),
        "projected_power_at_max_budget": _decimal_text(power_at_max_budget),
        "expected_days_to_maturity": _decimal_text(expected_days),
        "expected_total_spend_usd": _decimal_text(expected_spend),
        "fixed_endpoint_status": "FAIL" if failures else "PASS",
        "feasible": not failures,
        "reason_codes": failures,
    })
    return result
