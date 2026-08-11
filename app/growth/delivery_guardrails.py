from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List


NEW_ACCOUNT_DELIVERY_GUARDRAILS: Dict[str, Any] = {
    "version": "mx_cold_start_stop_v1",
    "ctr_floor": {
        "minimum_impressions": 800,
        "minimum_ctr": 0.012,
        "action": "PAUSE_AD",
    },
    "zero_install_spend": {
        "minimum_attribution_hours": 24,
        "spend_limit_usd": 1.20,
        "maximum_installs": 0,
        "action": "PAUSE_AD",
    },
    "high_cpi": {
        "minimum_installs": 10,
        "maximum_cpi_usd": 0.55,
        "action": "PAUSE_AD",
    },
}


def new_account_delivery_guardrails() -> Dict[str, Any]:
    return deepcopy(NEW_ACCOUNT_DELIVERY_GUARDRAILS)


def evaluate_delivery_stop_loss(
    metrics: Dict[str, Any], rules: Dict[str, Any], *, checkpoint: str,
) -> List[Dict[str, Any]]:
    """Return exact stop-loss breaches without performing a Meta write."""
    normalized_checkpoint = str(checkpoint or "").strip().upper()
    impressions = float(metrics.get("impressions") or 0)
    spend = float(metrics.get("spend") or 0)
    installs = float(metrics.get("installs") or 0)
    ctr = metrics.get("ctr")
    cpi = metrics.get("cpi")
    breaches: List[Dict[str, Any]] = []

    ctr_rule = dict(rules.get("ctr_floor") or {})
    if (
        ctr_rule
        and ctr is not None
        and impressions >= float(ctr_rule.get("minimum_impressions") or 0)
        and float(ctr) < float(ctr_rule.get("minimum_ctr") or 0)
    ):
        breaches.append({
            "rule": "ctr_floor",
            "summary": (
                f"展示已达 {int(impressions)}，CTR {float(ctr):.2%} 低于 "
                f"{float(ctr_rule.get('minimum_ctr') or 0):.2%}"
            ),
        })

    zero_rule = dict(rules.get("zero_install_spend") or {})
    attribution_ready = normalized_checkpoint in {"D1", "D3", "D7"}
    if (
        zero_rule
        and attribution_ready
        and spend >= float(zero_rule.get("spend_limit_usd") or 0)
        and installs <= float(zero_rule.get("maximum_installs") or 0)
    ):
        breaches.append({
            "rule": "zero_install_spend",
            "summary": (
                f"归因缓冲后已消耗 ${spend:.2f}，安装仍为 {int(installs)}"
            ),
        })

    cpi_rule = dict(rules.get("high_cpi") or {})
    if (
        cpi_rule
        and cpi is not None
        and installs >= float(cpi_rule.get("minimum_installs") or 0)
        and float(cpi) > float(cpi_rule.get("maximum_cpi_usd") or 0)
    ):
        breaches.append({
            "rule": "high_cpi",
            "summary": (
                f"安装已达 {int(installs)}，CPI ${float(cpi):.2f} 高于 "
                f"${float(cpi_rule.get('maximum_cpi_usd') or 0):.2f}"
            ),
        })
    return breaches
