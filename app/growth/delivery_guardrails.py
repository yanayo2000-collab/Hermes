from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional


DEFAULT_CPI_TARGET_USD = 0.30
GUARDRAIL_VERSION = "cold_start_operating_v2"


def new_account_delivery_guardrails(cpi_target_usd: float = DEFAULT_CPI_TARGET_USD) -> Dict[str, Any]:
    """Build target-aware operating guardrails.

    These thresholds support reversible operating decisions only.  They do not
    replace the 100-install / 10-real-join maturity contract and never perform
    a Meta write.
    """
    target = float(cpi_target_usd or 0)
    if target <= 0:
        target = DEFAULT_CPI_TARGET_USD
    return {
        "version": GUARDRAIL_VERSION,
        "cpi_target_usd": round(target, 4),
        "ctr_floor": {
            "minimum_impressions": 800,
            "minimum_ctr": 0.012,
            "action": "PAUSE_AD",
        },
        "zero_install_spend": {
            "minimum_attribution_hours": 24,
            "spend_limit_usd": round(target * 4, 2),
            "maximum_installs": 0,
            "action": "PAUSE_AD",
        },
        "high_cpi": {
            "minimum_installs": 3,
            "maximum_cpi_usd": round(target * 2, 2),
            "action": "PAUSE_AD",
        },
        "relative_loser": {
            "minimum_installs": 2,
            "maximum_peer_cpi_ratio": 2.0,
            "minimum_target_cpi_ratio": 1.5,
            "action": "PAUSE_AD",
        },
        "spend_cap": {
            "minimum_checkpoint": "D3",
            "maximum_spend_usd": round(target * 20, 2),
            "action": "PAUSE_AD",
        },
        "decision_scope": "REVERSIBLE_OPERATING_GUARDRAIL",
        "requires_approval": True,
        "causal_claim": False,
    }


# Compatibility export for older callers.  New code should call the builder so
# that BR/MX/ID orders use their own target instead of a shared MX threshold.
NEW_ACCOUNT_DELIVERY_GUARDRAILS: Dict[str, Any] = new_account_delivery_guardrails()


def effective_delivery_guardrails(
    persisted_rules: Optional[Dict[str, Any]], *, cpi_target_usd: float,
) -> Dict[str, Any]:
    """Resolve legacy/missing rules without mutating historical rows.

    v1 was a shared MX constant.  Missing and v1 rows are therefore upgraded at
    read/evaluation time from the immutable experiment CPI target.  Unknown
    future rule versions are preserved fail-closed instead of being rewritten.
    """
    stored = deepcopy(dict(persisted_rules or {}))
    version = str(stored.get("version") or "").strip()
    if not stored or version == "mx_cold_start_stop_v1":
        if float(cpi_target_usd or 0) <= 0:
            return {
                "version": "guardrail_unavailable",
                "reason": "cpi_target_missing",
                "decision_scope": "NO_OPERATING_DECISION",
                "requires_approval": False,
                "causal_claim": False,
            }
        return new_account_delivery_guardrails(cpi_target_usd)
    return stored


def evaluate_delivery_stop_loss(
    metrics: Dict[str, Any], rules: Dict[str, Any], *, checkpoint: str,
    peer_best_cpi: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Return exact operating breaches without performing a Meta write."""
    normalized_checkpoint = str(checkpoint or "").strip().upper()
    impressions = float(metrics.get("impressions") or 0)
    spend = float(metrics.get("spend") or 0)
    installs = float(metrics.get("installs") or 0)
    ctr = metrics.get("ctr")
    cpi = metrics.get("cpi")
    breaches: List[Dict[str, Any]] = []

    ctr_rule = dict(rules.get("ctr_floor") or {})
    if (
        ctr is not None
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
    attribution_ready = normalized_checkpoint in {"D1", "D3", "D5", "D7"}
    if (
        attribution_ready
        and spend >= float(zero_rule.get("spend_limit_usd") or 0)
        and installs <= float(zero_rule.get("maximum_installs") or 0)
    ):
        breaches.append({
            "rule": "zero_install_spend",
            "summary": f"归因缓冲后已消耗 ${spend:.2f}，安装仍为 {int(installs)}",
        })

    cpi_rule = dict(rules.get("high_cpi") or {})
    if (
        cpi is not None
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

    relative_rule = dict(rules.get("relative_loser") or {})
    target = float(rules.get("cpi_target_usd") or 0)
    if (
        cpi is not None and peer_best_cpi is not None and float(peer_best_cpi) > 0
        and installs >= float(relative_rule.get("minimum_installs") or 0)
        and float(cpi) > float(peer_best_cpi) * float(relative_rule.get("maximum_peer_cpi_ratio") or 0)
        and (target <= 0 or float(cpi) > target * float(relative_rule.get("minimum_target_cpi_ratio") or 0))
    ):
        breaches.append({
            "rule": "relative_loser",
            "summary": (
                f"CPI ${float(cpi):.2f} 已超过同组较优广告 "
                f"${float(peer_best_cpi):.2f} 的 {float(relative_rule.get('maximum_peer_cpi_ratio') or 0):g} 倍"
            ),
        })

    spend_rule = dict(rules.get("spend_cap") or {})
    checkpoint_order = {"D0": 0, "D1": 1, "D3": 3, "D5": 5, "D7": 7}
    minimum_checkpoint = str(spend_rule.get("minimum_checkpoint") or "D3").upper()
    if (
        checkpoint_order.get(normalized_checkpoint, 0) >= checkpoint_order.get(minimum_checkpoint, 3)
        and spend >= float(spend_rule.get("maximum_spend_usd") or 0)
    ):
        breaches.append({
            "rule": "spend_cap",
            "summary": (
                f"本轮已消耗 ${spend:.2f}，达到小样本经营预算上限 "
                f"${float(spend_rule.get('maximum_spend_usd') or 0):.2f}"
            ),
        })
    return breaches


def operating_assessment(
    metrics: Dict[str, Any], rules: Dict[str, Any], *, checkpoint: str,
    peer_best_cpi: Optional[float] = None,
) -> Dict[str, Any]:
    if str(rules.get("version") or "") == "guardrail_unavailable":
        return {
            "status": "UNAVAILABLE",
            "recommended_action": "CHECK_DATA",
            "breaches": [],
            "remaining_operating_budget_usd": None,
            "requires_approval": False,
            "decision_scope": "NO_OPERATING_DECISION",
            "reason": str(rules.get("reason") or "guardrail_unavailable"),
            "causal_claim": False,
        }
    breaches = evaluate_delivery_stop_loss(
        metrics, rules, checkpoint=checkpoint, peer_best_cpi=peer_best_cpi,
    )
    spend_cap = float(dict(rules.get("spend_cap") or {}).get("maximum_spend_usd") or 0)
    spend = float(metrics.get("spend") or 0)
    installs = float(metrics.get("installs") or 0)
    status = "ACTION_REQUIRED" if breaches else ("PROVISIONAL_KEEP" if installs >= 2 else "COLLECTING")
    return {
        "status": status,
        "recommended_action": "PAUSE_AD" if breaches else "OBSERVE",
        "breaches": breaches,
        "remaining_operating_budget_usd": round(max(0.0, spend_cap - spend), 2) if spend_cap else None,
        "requires_approval": bool(breaches),
        "decision_scope": "REVERSIBLE_OPERATING_GUARDRAIL",
        "causal_claim": False,
    }
