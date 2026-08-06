from __future__ import annotations

from typing import Any, Dict, Optional


RULE_VERSION = "ad_reco_rules_v4.2.3"
BENCHMARK_VERSION = "ad_benchmark_7d_v4.2.0_20260515_20260729"
THRESHOLD_SOURCE = "production_76d_896_paid_windows_2026-05-15_2026-07-29"
CPA_THRESHOLD_SOURCE = "bindsuccess_complete_windows_through_2026-07-23_ID_n70_BR_n50"
MX_CPA_THRESHOLD_SOURCE = "latest_incomplete_low_sample_windows_provisional"
MIDDLE_FUNNEL_HISTORY_COVERAGE = 3 / 76

WEIGHTS = {
    "real_join_cpa": 39,
    "real_joins": 22,
    "cpi": 17,
    "installs": 11,
    "ctr": 11,
}

BENCHMARKS = {
    "ID": {
        "real_join_cpa": (0.99, 1.42, 2.41), "cpi": (0.16, 0.18, 0.20),
        "ctr": (0.0256, 0.0222, 0.0172), "cpm": (0.61, 0.93, 1.28),
        "install_to_registration": (0.412, 0.368, 0.326),
        "apply_to_real_join": (0.24, 0.19, 0.10),
        "registration_to_apply": (0.55, 0.436, 0.35),
        "apply_to_valid_im": (0.50, 0.420, 0.32),
    },
    "BR": {
        "real_join_cpa": (1.62, 2.47, 3.40), "cpi": (0.22, 0.28, 0.37),
        "ctr": (0.0437, 0.0375, 0.0313), "cpm": (3.38, 4.04, 4.82),
        "install_to_registration": (0.513, 0.460, 0.407),
        "apply_to_real_join": (0.12, 0.09, 0.05),
        "registration_to_apply": (0.65, 0.551, 0.44),
        "apply_to_valid_im": (0.50, 0.393, 0.30),
    },
    "MX": {
        "real_join_cpa": (2.60, 2.85, 3.10), "cpi": (0.39, 0.43, 0.50),
        "ctr": (0.0230, 0.0164, 0.0122), "cpm": (2.88, 3.72, 4.42),
        "install_to_registration": (0.444, 0.345, 0.320),
        "apply_to_real_join": (0.12, 0.08, 0.04),
        "registration_to_apply": (0.60, 0.483, 0.38),
        "apply_to_valid_im": (0.65, 0.565, 0.42),
    },
}


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _lower_score(value: float, lines: tuple[float, float, float]) -> float:
    excellent, good, bad = lines
    if value <= excellent:
        return 100.0
    if value <= good:
        return 75.0 + 25.0 * (good - value) / (good - excellent)
    if value < bad:
        return 75.0 * (bad - value) / (bad - good)
    return 0.0


def _higher_score(value: float, lines: tuple[float, float, float]) -> float:
    excellent, good, bad = lines
    if value >= excellent:
        return 100.0
    if value >= good:
        return 75.0 + 25.0 * (value - good) / (excellent - good)
    if value > bad:
        return 75.0 * (value - bad) / (good - bad)
    return 0.0


def _maturity(value: float, initial: float, strong: float, *, high: Optional[float] = None) -> Dict[str, Any]:
    state = "unready"
    if value >= strong:
        state = "strong"
    elif value >= initial:
        state = "initial"
    if high is not None and value >= high:
        state = "high_confidence"
    return {"value": round(float(value), 4), "initial_threshold": initial, "strong_threshold": strong, "high_confidence_threshold": high, "state": state}


def _metric_readiness(value: Optional[float]) -> Dict[str, Any]:
    return {
        "value": None if value is None else round(float(value), 6),
        "state": "strong" if value is not None else "unready",
    }


def score_ad_object(
    item: Any,
    *,
    country: str,
    attribution_coverage: Optional[float] = None,
    middle_funnel_coverage: Optional[float] = None,
) -> Dict[str, Any]:
    country = str(country or "").upper()
    thresholds = BENCHMARKS.get(country)
    if not thresholds:
        return {
            "rule_version": RULE_VERSION, "benchmark_version": BENCHMARK_VERSION,
            "threshold_source": THRESHOLD_SOURCE, "status": "data_insufficient",
            "band": "data_insufficient", "band_zh": "数据不足", "score": None,
            "available_weight": 0, "confidence": "low", "dimensions": {},
            "maturity": {}, "guardrails": {}, "provisional": False,
        }

    dq = getattr(item, "data_quality", None)
    attribution = str(getattr(dq, "attribution_quality", "unknown") or "unknown").lower()
    if attribution_coverage is None:
        attribution_coverage = 1.0 if attribution in {"tugao_funnel_fact", "verified_business"} else (0.8 if attribution in {"tugao_raw_event", "tugao_raw_shadow"} else 0.0)
    attribution_coverage = _clamp(float(attribution_coverage), 0.0, 1.0)
    middle_funnel_coverage = _clamp(float(MIDDLE_FUNNEL_HISTORY_COVERAGE if middle_funnel_coverage is None else middle_funnel_coverage), 0.0, 1.0)

    installs = float(getattr(item, "installs", 0) or 0)
    registrations = float(getattr(item, "registrations", 0) or 0)
    applies = float(getattr(item, "auto_apply_user_count", 0) or getattr(item, "im_entries", 0) or 0)
    valid_im = float(getattr(item, "user_engaged_im_users", 0) or 0)
    joins = float(getattr(item, "real_bind_count", 0) or 0)
    spend = float(getattr(item, "spend", 0) or 0)
    cpa = getattr(item, "real_bind_cpa", None)
    cpi = getattr(item, "cpi", None)
    ctr = getattr(item, "ctr", None)
    cpm = getattr(item, "cpm", None)
    impressions = float(getattr(item, "impressions", 0) or 0)
    business_available = attribution_coverage > 0 and attribution not in {"fixture", "simulated", "unknown", ""}
    business_trusted = attribution_coverage >= 0.8 and business_available

    technical_attribution_inconsistent = bool(
        (installs > 0 and registrations / installs > 1.0)
        or (applies > 0 and joins / applies > 1.0)
        or (registrations > 0 and applies / registrations > 1.0)
    )
    cpa_value = None if cpa is None or float(cpa) <= 0 or joins <= 0 or spend <= 0 else float(cpa)
    cpi_value = None if cpi is None or installs <= 0 or spend <= 0 else float(cpi)
    ctr_value = None if ctr is None or spend <= 0 or impressions <= 0 else float(ctr)
    raw = {
        "real_join_cpa": cpa_value,
        "real_joins": joins if business_available else None,
        "cpi": cpi_value,
        "installs": installs,
        "ctr": ctr_value,
    }
    dimensions: Dict[str, Dict[str, Any]] = {}
    weighted = 0.0
    available_weight = 0.0
    for name, weight in WEIGHTS.items():
        value = raw[name]
        if value is None:
            dimensions[name] = {"available": False, "weight": weight, "value": None, "score": None}
            continue
        if name == "real_joins":
            metric_score = _clamp(100.0 * float(value) / 20.0)
        elif name == "installs":
            metric_score = _clamp(100.0 * float(value) / 100.0)
        elif name in {"real_join_cpa", "cpi"}:
            metric_score = _lower_score(float(value), thresholds[name])
        else:
            metric_score = _higher_score(float(value), thresholds[name])
        source = MX_CPA_THRESHOLD_SOURCE if name == "real_join_cpa" and country == "MX" else (CPA_THRESHOLD_SOURCE if name == "real_join_cpa" else THRESHOLD_SOURCE)
        dimensions[name] = {"available": True, "weight": weight, "value": round(float(value), 6), "score": round(metric_score, 2), "thresholds": list(thresholds.get(name) or []), "threshold_source": source}
        weighted += metric_score * weight
        available_weight += weight

    score = round(weighted / available_weight, 2) if available_weight else None
    core_metrics_ready = cpa_value is not None and cpi_value is not None and ctr_value is not None
    strong_base = business_trusted and installs >= 100 and joins >= 10 and core_metrics_ready
    provisional = country == "MX"
    confidence = "low" if provisional or available_weight < 80 or joins < 10 else ("high" if joins >= 20 and installs >= 100 else "medium")
    bad_cpa = thresholds["real_join_cpa"][2]
    excellent_cpa = thresholds["real_join_cpa"][0]
    cpi_bad = bool(cpi_value is not None and cpi_value > thresholds["cpi"][2])
    ctr_bad = bool(ctr_value is not None and ctr_value < thresholds["ctr"][2])
    poor_candidate = bool(strong_base and not provisional and cpa_value is not None and cpa_value > bad_cpa)
    stop_loss_candidate = bool(
        not provisional and business_trusted and installs >= 100
        and joins == 0 and cpi_value is not None and ctr_value is not None
        and (cpi_bad or ctr_bad)
    )
    scale_candidate = bool(
        strong_base and not provisional and cpa_value is not None and cpa_value <= excellent_cpa
        and cpi_value <= thresholds["cpi"][1] and ctr_value >= thresholds["ctr"][1]
    )
    band = "data_insufficient"
    if available_weight >= 70 and business_available and score is not None:
        band = "excellent" if score >= 80 and scale_candidate else ("qualified" if score >= 65 else ("observe" if score >= 45 else "poor"))
    maturity = {
        "installs": _maturity(installs, 30, 100),
        "cpi": _metric_readiness(cpi_value),
        "ctr": _metric_readiness(ctr_value),
        "real_joins": _maturity(joins, 5, 10, high=20),
        "real_join_cpa": _metric_readiness(cpa_value),
    }
    technical_audit = {
        "registrations": registrations,
        "applies": applies,
        "valid_im": valid_im,
        "cpm": None if cpm is None else float(cpm),
        "middle_funnel_coverage": round(middle_funnel_coverage, 4),
        "attribution_quality": attribution,
        "attribution_inconsistent": technical_attribution_inconsistent,
    }
    return {
        "rule_version": RULE_VERSION, "benchmark_version": BENCHMARK_VERSION,
        "threshold_source": THRESHOLD_SOURCE, "cpa_threshold_source": MX_CPA_THRESHOLD_SOURCE if provisional else CPA_THRESHOLD_SOURCE,
        "country": country, "provisional": provisional, "score": score, "available_weight": round(available_weight, 2),
        "status": band, "band": band,
        "band_zh": {"excellent": "优秀", "qualified": "合格", "observe": "观察", "poor": "较差", "data_insufficient": "数据不足"}[band],
        "confidence": confidence, "business_result_available": business_available,
        "attribution_coverage": round(attribution_coverage, 4),
        "strong_action_eligible": bool(strong_base and not provisional),
        "dimensions": dimensions, "maturity": maturity, "technical_audit": technical_audit,
        "guardrails": {"poor_candidate": poor_candidate, "stop_loss_candidate": stop_loss_candidate, "scale_candidate": scale_candidate},
    }
