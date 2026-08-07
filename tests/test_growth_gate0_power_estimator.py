from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from app.growth.gate0_power_estimator import (
    CONTRACT_HASH,
    ESTIMATOR_VERSION,
    GOLDEN_VECTOR_HASH,
    INPUT_VERSION,
    PowerContractError,
    assess_fixed_endpoint_power,
    golden_vectors,
    information_from_total_events,
    target_information,
    two_sided_power,
    validate_golden_vectors,
)


def _input(*, events: int = 464, spend: str = "20", ready: bool = True) -> dict:
    return {
        "schema_version": INPUT_VERSION,
        "estimator_version": ESTIMATOR_VERSION,
        "estimand": "QUALIFIED_JOINS_PER_USD",
        "offset": "SPEND_USD",
        "alternative": "TWO_SIDED",
        "approximation": "WALD_NORMAL_APPROXIMATION",
        "control_allocation": "0.5",
        "treatment_allocation": "0.5",
        "alpha_two_sided": "0.05",
        "desired_power": "0.8",
        "mde_relative": "0.3",
        "baseline_qualified_joins": events,
        "baseline_spend_usd": spend,
        "evidence_status": "READY" if ready else "INCOMPLETE",
        "incomplete_reasons": [] if ready else ["SOURCE_STALE"],
        "expected_daily_spend_usd": "1.428571",
        "maximum_test_days": 14,
        "maximum_test_budget_usd": "20",
    }


def test_frozen_information_and_golden_event_boundary():
    target = target_information(
        alpha_two_sided=Decimal("0.05"),
        desired_power=Decimal("0.8"),
        rate_ratio=Decimal("1.3"),
    )
    assert target.quantize(Decimal("0.000000000001")) == Decimal("114.024535562432")
    info_463 = information_from_total_events(Decimal("463"), rate_ratio=Decimal("1.3"))
    info_464 = information_from_total_events(Decimal("464"), rate_ratio=Decimal("1.3"))
    assert info_463 < target <= info_464
    assert two_sided_power(info_463, rate_ratio=Decimal("1.3")).quantize(
        Decimal("0.000000000001"),
    ) == Decimal("0.799160899290")
    assert two_sided_power(info_464, rate_ratio=Decimal("1.3")).quantize(
        Decimal("0.000000000001"),
    ) == Decimal("0.800007596435")


def test_45_event_vector_is_low_information_not_silently_zero():
    information = information_from_total_events(Decimal("45"), rate_ratio=Decimal("1.3"))
    assert information.quantize(Decimal("0.000000000001")) == Decimal("11.058601134216")
    assert two_sided_power(information, rate_ratio=Decimal("1.3")).quantize(
        Decimal("0.000000000001"),
    ) == Decimal("0.140720868010")


def test_complete_control_baseline_45_events_per_20_dollars_is_structurally_over_limits():
    result = assess_fixed_endpoint_power(_input(events=45, spend="20"))
    assert result["fixed_endpoint_status"] == "FAIL"
    assert result["feasible"] is False
    assert set(result["reason_codes"]) == {
        "EXPECTED_MATURITY_EXCEEDS_LIMIT",
        "EXPECTED_SPEND_EXCEEDS_BUDGET",
    }
    assert Decimal(result["expected_days_to_maturity"]).quantize(Decimal("0.01")) == Decimal("125.52")
    assert Decimal(result["expected_total_spend_usd"]).quantize(Decimal("0.01")) == Decimal("179.32")


def test_incomplete_45_event_history_remains_unknown():
    result = assess_fixed_endpoint_power(_input(events=45, spend="20", ready=False))
    assert result["fixed_endpoint_status"] == "UNKNOWN"
    assert result["expected_days_to_maturity"] is None
    assert result["reason_codes"] == ["SOURCE_STALE"]


@pytest.mark.parametrize(
    ("events", "spend", "reason"),
    ((0, "20", "BASELINE_EVENT_RATE_UNKNOWN"), (45, "0", "BASELINE_SPEND_DENOMINATOR_ZERO")),
)
def test_zero_denominators_are_unknown(events, spend, reason):
    result = assess_fixed_endpoint_power(_input(events=events, spend=spend))
    assert result["fixed_endpoint_status"] == "UNKNOWN"
    assert result["reason_codes"] == [reason]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("estimand", "QUALIFIED_JOINS_PER_IMPRESSION"),
        ("offset", "IMPRESSIONS"),
        ("alternative", "ONE_SIDED"),
        ("approximation", "EXACT"),
        ("control_allocation", "0.4"),
        ("treatment_allocation", "0.6"),
        ("mde_relative", "0.2"),
    ),
)
def test_frozen_contract_cannot_be_reinterpreted(field, value):
    raw = _input()
    raw[field] = value
    with pytest.raises(PowerContractError, match="G0_POWER_FROZEN_CONTRACT_MISMATCH"):
        assess_fixed_endpoint_power(raw)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", True])
def test_invalid_numeric_values_fail_closed(value):
    raw = _input()
    raw["baseline_spend_usd"] = value
    with pytest.raises(PowerContractError, match="G0_POWER_BASELINE_INVALID"):
        assess_fixed_endpoint_power(raw)


def test_evidence_state_and_unknown_keys_fail_closed():
    raw = _input()
    raw["incomplete_reasons"] = ["SOURCE_STALE"]
    with pytest.raises(PowerContractError, match="G0_POWER_EVIDENCE_STATUS_INVALID"):
        assess_fixed_endpoint_power(raw)
    raw = _input()
    raw["caller_feasible"] = True
    with pytest.raises(PowerContractError, match="G0_POWER_INPUT_SCHEMA_INVALID"):
        assess_fixed_endpoint_power(raw)


def test_golden_vector_manifest_is_content_addressed_and_tamper_evident():
    vectors = golden_vectors()
    assert vectors["contract_hash"] == CONTRACT_HASH
    assert vectors["golden_vector_hash"] == GOLDEN_VECTOR_HASH
    assert validate_golden_vectors(vectors) == vectors
    tampered = deepcopy(vectors)
    tampered["vectors"][1]["meets_fixed_endpoint_target"] = False
    with pytest.raises(PowerContractError, match="G0_POWER_GOLDEN_VECTOR_MISMATCH"):
        validate_golden_vectors(tampered)


def test_fixed_endpoint_output_never_claims_obf_is_frozen():
    result = assess_fixed_endpoint_power(_input())
    assert result["obf_boundary_status"] == "UNFROZEN"
    assert "obf" not in result["estimator_version"].lower()
