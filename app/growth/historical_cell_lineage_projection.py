"""Frozen read-only projection for the August 2026 GLE historical comparison.

The record is deliberately narrower than a Gate or causal result.  It binds
one exact Meta Study and its two local experiment records to an externally
SHA-pinned AppsFlyer export, then derives descriptive rates and exact Fisher
tests.  The source bytes are not opened by the request path, so source
authority and current-window settlement remain unverified.
"""

from __future__ import annotations

from fractions import Fraction
from math import comb
from typing import Any


SCHEMA_VERSION = "gle-historical-cell-lineage-projection-v1"

_ACCOUNT_ID = "1012060198097836"
_STUDY_ID = "1755195762483275"
_CAMPAIGN_ID = "120250588944820544"
_APPSFLYER_RAW_SHA256 = "f25f09cb6dbd75de5b40eddf2fa38289873800a49bf7448441cf50e1125ad812"
_EVIDENCE_ARTIFACT_SHA256 = "d919b200debd6c3a61bfdca910da999af00a66425c95ef686ecf37f389e97868"

_CELLS = (
    {
        "cell_key": "C1",
        "role": "BASELINE",
        "experiment_id": "adexp_1c90797d13d04928aa0a74e487d21fd1",
        "study_cell_id": "1657983691931915",
        "adset_id": "120250588945530544",
        "ad_id": "120250588945870544",
        "impressions": 505,
        "clicks": 10,
        "installs": 2,
    },
    {
        "cell_key": "C2",
        "role": "CHALLENGER",
        "experiment_id": "adexp_f9dd3e87bca6415b94b62ebfdf45fdf9",
        "study_cell_id": "1587562426321061",
        "adset_id": "120250588946480544",
        "ad_id": "120250588946840544",
        "impressions": 793,
        "clicks": 22,
        "installs": 9,
    },
)

CEILING = {
    "evidence_effect": "HISTORICAL_DIRECTIONAL_REFERENCE_ONLY",
    "source_content_authority": "NOT_VERIFIED",
    "current_natural_window_effect": "NONE",
    "causal_claim": False,
    "snapshot_effect": "NONE",
    "partition_effect": "NONE",
    "replay_effect": "NONE",
    "golden_effect": "NONE",
    "gate0_effect": "NONE",
    "gate0_result_effect": "UNCHANGED",
    "gate1_effect": "NONE",
    "meta_write_allowed": False,
    "not_gate_receipt": True,
}


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _relative_lift(challenger: float, baseline: float) -> float | None:
    if baseline == 0:
        return None
    return challenger / baseline - 1.0


def _fisher_two_sided(success_a: int, failure_a: int, success_b: int, failure_b: int) -> float:
    """Return the exact two-sided Fisher probability without SciPy."""
    row_a = success_a + failure_a
    row_b = success_b + failure_b
    successes = success_a + success_b
    total = row_a + row_b
    denominator = comb(total, row_a)

    def probability(value: int) -> Fraction:
        return Fraction(comb(successes, value) * comb(total - successes, row_a - value), denominator)

    lower = max(0, row_a - (total - successes))
    upper = min(row_a, successes)
    observed = probability(success_a)
    result = sum(
        (probability(value) for value in range(lower, upper + 1) if probability(value) <= observed),
        Fraction(0, 1),
    )
    return float(min(result, Fraction(1, 1)))


def _metric(
    *, numerator: str, denominator: str, baseline: dict[str, Any], challenger: dict[str, Any],
) -> dict[str, Any]:
    baseline_numerator = int(baseline[numerator])
    baseline_denominator = int(baseline[denominator])
    challenger_numerator = int(challenger[numerator])
    challenger_denominator = int(challenger[denominator])
    baseline_rate = _rate(baseline_numerator, baseline_denominator)
    challenger_rate = _rate(challenger_numerator, challenger_denominator)
    return {
        "baseline": {
            "numerator": baseline_numerator,
            "denominator": baseline_denominator,
            "rate": baseline_rate,
        },
        "challenger": {
            "numerator": challenger_numerator,
            "denominator": challenger_denominator,
            "rate": challenger_rate,
        },
        "challenger_relative_lift": _relative_lift(challenger_rate, baseline_rate),
        "fisher_exact_two_sided_p_value": _fisher_two_sided(
            baseline_numerator,
            baseline_denominator - baseline_numerator,
            challenger_numerator,
            challenger_denominator - challenger_numerator,
        ),
    }


def historical_cell_lineage_projection(
    *, account_id: str = "", experiment_id: str = "",
) -> dict[str, Any] | None:
    """Project the frozen record only for its exact account and experiment."""
    normalized_account = str(account_id or "").strip().removeprefix("act_")
    normalized_experiment = str(experiment_id or "").strip()
    experiment_ids = {str(item["experiment_id"]) for item in _CELLS}
    if normalized_account != _ACCOUNT_ID or normalized_experiment not in experiment_ids:
        return None

    baseline = dict(_CELLS[0])
    challenger = dict(_CELLS[1])
    current_cell = next(dict(item) for item in _CELLS if item["experiment_id"] == normalized_experiment)
    metrics = {
        "ctr": _metric(
            numerator="clicks", denominator="impressions",
            baseline=baseline, challenger=challenger,
        ),
        "install_per_impression": _metric(
            numerator="installs", denominator="impressions",
            baseline=baseline, challenger=challenger,
        ),
        "click_to_install": _metric(
            numerator="installs", denominator="clicks",
            baseline=baseline, challenger=challenger,
        ),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "HISTORICAL_EXACT_CELL_LINEAGE_AVAILABLE",
        "decision": "DIRECTIONAL_C2_BETTER_STATISTICALLY_INCONCLUSIVE",
        "decision_strength": "DIRECTIONAL_ONLY",
        "preferred_cell": "C2",
        "summary_zh": "历史样本：C2 方向更优，但统计不充分",
        "natural_window_settlement_dates": ["2026-08-11", "2026-08-13"],
        "subject": {
            "account_id": _ACCOUNT_ID,
            "study_id": _STUDY_ID,
            "campaign_id": _CAMPAIGN_ID,
            "requested_experiment_id": normalized_experiment,
            "requested_cell_key": current_cell["cell_key"],
            "requested_role": current_cell["role"],
        },
        "cells": [dict(item) for item in _CELLS],
        "metrics": metrics,
        "sample": {
            "impressions": baseline["impressions"] + challenger["impressions"],
            "clicks": baseline["clicks"] + challenger["clicks"],
            "installs": baseline["installs"] + challenger["installs"],
        },
        "source": {
            "reporting_timezone": "Asia/Hong_Kong",
            "date_from": "2026-07-11",
            "date_to": "2026-08-09",
            "appsflyer_raw_sha256": _APPSFLYER_RAW_SHA256,
            "evidence_artifact_sha256": _EVIDENCE_ARTIFACT_SHA256,
            "content_authority": "NOT_VERIFIED",
        },
        "natural_window": {
            "status": "PENDING_NATURAL_WINDOW",
            "reporting_timezone": "Asia/Shanghai",
            "first_settlement_not_before": "2026-08-11T09:20:00+08:00",
            "three_complete_days_candidate_not_before": "2026-08-13T09:20:00+08:00",
            "historical_evidence_substitutes_natural_window": False,
        },
        "reason_codes": [
            "HISTORICAL_WINDOW_NOT_ADMISSIBLE_FOR_NATURAL_AUDIT",
            "SOURCE_CONTENT_AUTHORITY_NOT_VERIFIED",
            "LOW_INSTALL_COUNT",
            "NO_FISHER_TEST_REACHES_ALPHA_0_05",
        ],
        "ceiling": dict(CEILING),
    }
