from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Sequence


SCHEMA_VERSION = "gle-ad-account-coverage-v1"
# Keep coverage readiness on the same rolling window used by the operating
# report.  A fact from several weeks ago must not make an ad look score-ready
# when the current seven-day report has no usable input for it.
FACT_WINDOW_DAYS = 7
NEW_AD_ZERO_DELIVERY_HOURS = 48
MAX_ADS_PER_ACCOUNT = 1_000
MAX_META_PAGES = 5

GLE_AD_ACCOUNT_SCOPE_V1: tuple[dict[str, str], ...] = (
    {"account_id": "1012060198097836", "account_name": "自投-MX-TM", "market": "MX"},
    {"account_id": "1053439070674646", "account_name": "TUGAO自投-MX-TM", "market": "MX"},
    {"account_id": "1457588552349197", "account_name": "自投-BR-TM", "market": "BR"},
    {"account_id": "2282907019174017", "account_name": "测试户", "market": "TEST"},
    {"account_id": "1250000910496826", "account_name": "自投-ID-TM", "market": "ID"},
)


class AdAccountCoverageError(ValueError):
    pass


def _fail(code: str) -> None:
    raise AdAccountCoverageError(code)


def _identifier(value: Any, code: str) -> str:
    result = str(value or "").strip().removeprefix("act_")
    if not result.isdigit() or len(result) > 32:
        _fail(code)
    return result


def _status(value: Any, code: str) -> str:
    result = str(value or "").strip().upper()
    if not result or len(result) > 64:
        _fail(code)
    return result


def _scope_by_id() -> dict[str, dict[str, str]]:
    return {item["account_id"]: dict(item) for item in GLE_AD_ACCOUNT_SCOPE_V1}


def fetch_scoped_meta_ads(
    session: Any,
    *,
    access_token: str,
    graph_root: str,
) -> List[Dict[str, Any]]:
    """GET the complete bounded ad roster for the five operator-selected accounts."""
    if session is None or not str(access_token or "").strip() or not str(graph_root or "").strip():
        _fail("GLE_AD_COVERAGE_META_READ_UNAVAILABLE")
    root = str(graph_root).rstrip("/")
    rows: List[Dict[str, Any]] = []
    seen_global: set[str] = set()
    for account in GLE_AD_ACCOUNT_SCOPE_V1:
        account_id = account["account_id"]
        after = ""
        seen_cursors: set[str] = set()
        account_rows = 0
        for _ in range(MAX_META_PAGES):
            params: Dict[str, Any] = {
                "fields": (
                    "id,name,account_id,campaign_id,adset_id,status,effective_status,"
                    "created_time,updated_time,"
                    "insights.date_preset(maximum).limit(1){impressions,spend}"
                ),
                "limit": 200,
            }
            if after:
                params["after"] = after
            try:
                response = session.get(
                    f"{root}/act_{account_id}/ads",
                    params=params,
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=25,
                )
                if hasattr(response, "raise_for_status"):
                    response.raise_for_status()
                body = response.json() if hasattr(response, "json") else {}
            except Exception as exc:
                raise AdAccountCoverageError("GLE_AD_COVERAGE_META_READ_FAILED") from exc
            if not isinstance(body, dict) or body.get("error") or not isinstance(body.get("data"), list):
                _fail("GLE_AD_COVERAGE_META_RESPONSE_INVALID")
            for raw in body["data"]:
                if not isinstance(raw, dict):
                    _fail("GLE_AD_COVERAGE_META_ROW_INVALID")
                ad_id = _identifier(raw.get("id"), "GLE_AD_COVERAGE_AD_ID_INVALID")
                raw_account_id = _identifier(
                    raw.get("account_id"), "GLE_AD_COVERAGE_ACCOUNT_ID_INVALID"
                )
                if raw_account_id != account_id or ad_id in seen_global:
                    _fail("GLE_AD_COVERAGE_META_IDENTITY_CONFLICT")
                campaign_id = _identifier(
                    raw.get("campaign_id"), "GLE_AD_COVERAGE_CAMPAIGN_ID_INVALID"
                )
                adset_id = _identifier(
                    raw.get("adset_id"), "GLE_AD_COVERAGE_ADSET_ID_INVALID"
                )
                configured_status = _status(
                    raw.get("status"), "GLE_AD_COVERAGE_STATUS_INVALID"
                )
                effective_status = _status(
                    raw.get("effective_status") or configured_status,
                    "GLE_AD_COVERAGE_EFFECTIVE_STATUS_INVALID",
                )
                insights = raw.get("insights")
                if not isinstance(insights, dict) or not isinstance(insights.get("data"), list):
                    _fail("GLE_AD_COVERAGE_LIFETIME_INSIGHTS_INVALID")
                insight_rows = insights["data"]
                if len(insight_rows) > 1 or any(not isinstance(row, dict) for row in insight_rows):
                    _fail("GLE_AD_COVERAGE_LIFETIME_INSIGHTS_INVALID")
                lifetime = insight_rows[0] if insight_rows else {}
                try:
                    lifetime_impressions = int(lifetime.get("impressions") or 0)
                    lifetime_spend = float(lifetime.get("spend") or 0)
                except (TypeError, ValueError):
                    _fail("GLE_AD_COVERAGE_LIFETIME_INSIGHTS_INVALID")
                if lifetime_impressions < 0 or lifetime_spend < 0:
                    _fail("GLE_AD_COVERAGE_LIFETIME_INSIGHTS_INVALID")
                seen_global.add(ad_id)
                account_rows += 1
                if account_rows > MAX_ADS_PER_ACCOUNT:
                    _fail("GLE_AD_COVERAGE_ACCOUNT_AD_LIMIT_EXCEEDED")
                rows.append(
                    {
                        "account_id": account_id,
                        "account_name": account["account_name"],
                        "market": account["market"],
                        "ad_id": ad_id,
                        "ad_name": str(raw.get("name") or "").strip()[:500],
                        "campaign_id": campaign_id,
                        "adset_id": adset_id,
                        "configured_status": configured_status,
                        "effective_status": effective_status,
                        "created_time": str(raw.get("created_time") or "").strip(),
                        "updated_time": str(raw.get("updated_time") or "").strip(),
                        "lifetime_delivery": {
                            "impressions": lifetime_impressions,
                            "spend": lifetime_spend,
                            "source": "META_AD_INSIGHTS_MAXIMUM",
                        },
                    }
                )
            paging = body.get("paging")
            if paging is None:
                break
            if not isinstance(paging, dict):
                _fail("GLE_AD_COVERAGE_PAGING_INVALID")
            next_url = paging.get("next")
            cursors = paging.get("cursors") or {}
            if not next_url:
                break
            if not isinstance(cursors, dict):
                _fail("GLE_AD_COVERAGE_PAGING_INVALID")
            next_after = str(cursors.get("after") or "").strip()
            if not next_after or next_after == after or next_after in seen_cursors:
                _fail("GLE_AD_COVERAGE_PAGING_CURSOR_INVALID")
            seen_cursors.add(next_after)
            after = next_after
        else:
            _fail("GLE_AD_COVERAGE_PAGE_LIMIT_EXCEEDED")
    return rows


def _placeholders(values: Sequence[str]) -> str:
    if not values:
        _fail("GLE_AD_COVERAGE_EMPTY_ROSTER")
    return ",".join("?" for _ in values)


def _load_fact_observations(
    conn: sqlite3.Connection,
    ad_ids: Sequence[str],
) -> tuple[str | None, str | None, bool, dict[str, str], dict[str, dict[str, Any]]]:
    try:
        row = conn.execute(
            """
            SELECT MAX(date)
            FROM ad_dashboard_sync_state
            WHERE source='all' AND status='ok' AND row_count>0
            """
        ).fetchone()
        cutoff = str((row[0] if row else "") or "").strip()
        cutoff_date = date.fromisoformat(cutoff) if cutoff else None
    except (sqlite3.Error, ValueError):
        cutoff_date = None
    if cutoff_date is None:
        return None, None, False, {}, {}
    window_start = cutoff_date - timedelta(days=FACT_WINDOW_DAYS - 1)
    complete_dates = {
        str(row[0] or "").strip()
        for row in conn.execute(
            """
            SELECT DISTINCT date
            FROM ad_dashboard_sync_state
            WHERE source='all' AND status='ok' AND row_count>0
              AND date BETWEEN ? AND ?
            """,
            (window_start.isoformat(), cutoff_date.isoformat()),
        )
    }
    complete_window = len(complete_dates) == FACT_WINDOW_DAYS
    historical_latest = {
        str(row[0] or "").strip(): str(row[1] or "").strip()
        for row in conn.execute(
            f"""
            SELECT ad_id, MAX(date)
            FROM ad_dashboard_fact_rows
            WHERE date < ? AND ad_id IN ({_placeholders(ad_ids)})
            GROUP BY ad_id
            """,
            (window_start.isoformat(), *ad_ids),
        )
        if str(row[0] or "").strip()
    }
    sql = f"""
        SELECT ad_id, MIN(date) AS first_date, MAX(date) AS latest_date,
               COUNT(*) AS fact_row_count,
               MIN(NULLIF(TRIM(account_id), '')) AS min_account_id,
               MAX(NULLIF(TRIM(account_id), '')) AS max_account_id
        FROM ad_dashboard_fact_rows
        WHERE date BETWEEN ? AND ?
          AND ad_id IN ({_placeholders(ad_ids)})
        GROUP BY ad_id
    """
    observations: dict[str, dict[str, Any]] = {}
    for raw in conn.execute(sql, [window_start.isoformat(), cutoff_date.isoformat(), *ad_ids]):
        item = dict(raw) if isinstance(raw, sqlite3.Row) else {
            "ad_id": raw[0], "first_date": raw[1], "latest_date": raw[2],
            "fact_row_count": raw[3], "min_account_id": raw[4], "max_account_id": raw[5],
        }
        ad_id = str(item["ad_id"] or "").strip()
        observations[ad_id] = item
    return (
        window_start.isoformat(), cutoff_date.isoformat(), complete_window,
        historical_latest, observations,
    )


def _created_before_window(value: Any, window_start: str | None) -> bool:
    """Require a full prior calendar day; boundary-day creations stay pending."""
    if not window_start:
        return False
    try:
        return date.fromisoformat(str(value or "").strip()[:10]) < date.fromisoformat(window_start)
    except ValueError:
        return False


def _age_hours(value: Any, *, now: datetime) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        created = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None
    return max(0.0, (now.astimezone(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() / 3600)


def _experiment_memberships(
    conn: sqlite3.Connection,
    ad_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    sql = f"""
        SELECT experiment_id, account_id, source_report_id, source_campaign_id,
               source_adset_id, source_ad_id, state, control_definition_json
        FROM ad_experiment
        WHERE source_ad_id IN ({_placeholders(ad_ids)})
        ORDER BY source_report_id, experiment_id
    """
    matched = [dict(row) for row in conn.execute(sql, list(ad_ids))]
    report_ids = sorted({str(item.get("source_report_id") or "").strip() for item in matched})
    report_ids = [item for item in report_ids if item]
    if not report_ids:
        return {}
    group_sql = f"""
        SELECT experiment_id, account_id, source_report_id, source_campaign_id,
               source_adset_id, source_ad_id, state, control_definition_json
        FROM ad_experiment
        WHERE source_report_id IN ({_placeholders(report_ids)})
        ORDER BY source_report_id, experiment_id
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(group_sql, report_ids):
        item = dict(row)
        groups.setdefault(str(item.get("source_report_id") or ""), []).append(item)
    valid_groups: dict[str, dict[str, Any]] = {}
    for report_id, members in groups.items():
        if not 2 <= len(members) <= 4:
            continue
        account_ids = {str(item.get("account_id") or "").removeprefix("act_") for item in members}
        campaign_ids = {str(item.get("source_campaign_id") or "") for item in members}
        adset_ids = [str(item.get("source_adset_id") or "") for item in members]
        member_ad_ids = [str(item.get("source_ad_id") or "") for item in members]
        roles: List[str] = []
        for item in members:
            try:
                control = json.loads(str(item.get("control_definition_json") or "{}"))
            except (json.JSONDecodeError, TypeError, ValueError):
                control = {}
            roles.append(str(control.get("role") or "").strip().upper())
        if (
            len(account_ids) != 1 or "" in account_ids
            or len(campaign_ids) != 1 or "" in campaign_ids
            or len(set(adset_ids)) != len(members) or "" in adset_ids
            or len(set(member_ad_ids)) != len(members) or "" in member_ad_ids
            or roles.count("BASELINE") != 1
            or any(role not in {"BASELINE", "CHALLENGER"} for role in roles)
        ):
            continue
        valid_groups[report_id] = {
            "source_report_id": report_id,
            "member_count": len(members),
            "experiment_ids": [str(item.get("experiment_id") or "") for item in members],
        }
    result: dict[str, dict[str, Any]] = {}
    for item in matched:
        report_id = str(item.get("source_report_id") or "")
        group = valid_groups.get(report_id)
        if group:
            result[str(item.get("source_ad_id") or "")] = {
                **group,
                "experiment_id": str(item.get("experiment_id") or ""),
                "experiment_state": str(item.get("state") or ""),
            }
    return result


def build_gle_ad_account_coverage(
    conn: sqlite3.Connection,
    live_ads: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> Dict[str, Any]:
    current_time = now or datetime.now(timezone.utc)
    scope = _scope_by_id()
    normalized = [dict(item) for item in live_ads]
    if not normalized:
        _fail("GLE_AD_COVERAGE_EMPTY_ROSTER")
    seen: set[str] = set()
    for item in normalized:
        account_id = _identifier(item.get("account_id"), "GLE_AD_COVERAGE_ACCOUNT_ID_INVALID")
        ad_id = _identifier(item.get("ad_id"), "GLE_AD_COVERAGE_AD_ID_INVALID")
        if account_id not in scope or ad_id in seen:
            _fail("GLE_AD_COVERAGE_ROSTER_INVALID")
        if str(item.get("account_name") or "") != scope[account_id]["account_name"]:
            _fail("GLE_AD_COVERAGE_ACCOUNT_NAME_INVALID")
        seen.add(ad_id)
    ad_ids = sorted(seen)
    window_start, cutoff, complete_window, historical_latest, facts = (
        _load_fact_observations(conn, ad_ids)
    )
    memberships = _experiment_memberships(conn, ad_ids)
    roster_by_ad_id = {str(item["ad_id"]): item for item in normalized}
    delivered_ad_ids = {
        ad_id
        for ad_id, fact in facts.items()
        if int(fact.get("fact_row_count") or 0) > 0
        and str(fact.get("min_account_id") or "").strip().removeprefix("act_")
        == str(roster_by_ad_id.get(ad_id, {}).get("account_id") or "")
        and str(fact.get("max_account_id") or "").strip().removeprefix("act_")
        == str(roster_by_ad_id.get(ad_id, {}).get("account_id") or "")
    }
    delivered_by_adset: dict[str, int] = {}
    for delivered_ad_id in delivered_ad_ids:
        adset_id = str(roster_by_ad_id[delivered_ad_id].get("adset_id") or "")
        delivered_by_adset[adset_id] = delivered_by_adset.get(adset_id, 0) + 1
    account_results: list[dict[str, Any]] = []
    all_items: list[dict[str, Any]] = []
    for account in GLE_AD_ACCOUNT_SCOPE_V1:
        account_ads = sorted(
            (item for item in normalized if item["account_id"] == account["account_id"]),
            key=lambda item: (str(item.get("ad_name") or ""), str(item.get("ad_id") or "")),
        )
        projected: list[dict[str, Any]] = []
        for item in account_ads:
            ad_id = str(item["ad_id"])
            fact = facts.get(ad_id)
            fact_account = str((fact or {}).get("min_account_id") or "").strip().removeprefix("act_")
            fact_account_max = (
                str((fact or {}).get("max_account_id") or "").strip().removeprefix("act_")
            )
            fact_bound = bool(
                fact and fact_account == item["account_id"] and fact_account_max == item["account_id"]
            )
            existed_before_window = bool(
                historical_latest.get(ad_id)
                or _created_before_window(item.get("created_time"), window_start)
            )
            age_hours = _age_hours(item.get("created_time"), now=current_time)
            lifetime_delivery = dict(item.get("lifetime_delivery") or {})
            lifetime_delivery_exact = (
                str(lifetime_delivery.get("source") or "") == "META_AD_INSIGHTS_MAXIMUM"
            )
            lifetime_impressions = int(lifetime_delivery.get("impressions") or 0)
            lifetime_spend = float(lifetime_delivery.get("spend") or 0)
            zero_delivery_after_48h = bool(
                item.get("effective_status") == "ACTIVE"
                and lifetime_delivery_exact
                and age_hours is not None
                and age_hours >= NEW_AD_ZERO_DELIVERY_HOURS
                and lifetime_impressions == 0
                and lifetime_spend == 0
            )
            no_delivery = bool(
                item.get("effective_status") == "ACTIVE"
                and complete_window and existed_before_window and not fact_bound
            )
            if zero_delivery_after_48h:
                monitoring_status = "NO_LIFETIME_DELIVERY_AFTER_48H"
            elif fact_bound:
                monitoring_status = "METRIC_OBSERVATION_AVAILABLE"
            elif no_delivery:
                monitoring_status = "NO_DELIVERY_IN_COMPLETE_WINDOW"
            else:
                monitoring_status = "WAITING_FOR_DASHBOARD_FACTS"
            membership = memberships.get(ad_id)
            is_experiment = membership is not None
            if is_experiment and zero_delivery_after_48h:
                evaluation_status = "EXPERIMENT_ZERO_DELIVERY_AFTER_48H_REQUIRES_REBUILD"
            elif is_experiment and fact_bound:
                evaluation_status = "EXPERIMENT_OPERATING_EVALUATION_AVAILABLE"
            elif is_experiment:
                evaluation_status = (
                    "EXPERIMENT_ZERO_DELIVERY_REQUIRES_REVIEW"
                    if no_delivery else "EXPERIMENT_REGISTERED_WAITING_FOR_FACTS"
                )
            elif zero_delivery_after_48h:
                evaluation_status = "SINGLE_AD_ZERO_DELIVERY_AFTER_48H_REQUIRES_REBUILD"
            elif fact_bound:
                evaluation_status = "SINGLE_AD_OPERATING_OBSERVATION_AVAILABLE"
            else:
                evaluation_status = (
                    "SINGLE_AD_ZERO_DELIVERY_REQUIRES_REVIEW"
                    if no_delivery else "OBSERVATION_REGISTERED_WAITING_FOR_FACTS"
                )
            blockers = ["MISSING_EXACT_CELL_LINEAGE", "GATE0_QUASI_ONLY", "GATE1_NOT_READY"]
            if zero_delivery_after_48h:
                blockers.insert(0, "NO_LIFETIME_DELIVERY_AFTER_48H")
            elif no_delivery:
                blockers.insert(0, "NO_DELIVERY_IN_COMPLETE_WINDOW")
            elif not fact_bound:
                blockers.insert(0, "DASHBOARD_FACTS_NOT_AVAILABLE_IN_WINDOW")
            if not is_experiment:
                blockers.insert(0, "NOT_REGISTERED_AS_MULTI_CELL_EXPERIMENT")
            projected_item = {
                **item,
                "coverage_status": "COVERED_READ_ONLY",
                "coverage_mode": "MULTI_CELL_EXPERIMENT" if is_experiment else "SINGLE_AD_OBSERVATION",
                "monitoring_status": monitoring_status,
                "evaluation_status": evaluation_status,
                "experiment_binding": membership,
                "fact_window": {
                    "start_date": window_start,
                    "cutoff_date": cutoff,
                    "latest_fact_date": str((fact or {}).get("latest_date") or "") or None,
                    "row_count": int((fact or {}).get("fact_row_count") or 0),
                    "complete": complete_window,
                },
                "delivery_diagnosis": {
                    "status": (
                        "NO_LIFETIME_DELIVERY_AFTER_48H" if zero_delivery_after_48h else
                        "DELIVERY_OBSERVED" if fact_bound else
                        "NO_DELIVERY_CONFIRMED" if no_delivery else "PENDING_INITIAL_OR_SYNC"
                    ),
                    "reason_code": (
                        "NO_LIFETIME_DELIVERY_AFTER_48H" if zero_delivery_after_48h else
                        "EXACT_AD_FACTS_AVAILABLE" if fact_bound else
                        "NO_DELIVERY_IN_COMPLETE_WINDOW" if no_delivery else
                        "WINDOW_OR_AD_AGE_INCOMPLETE"
                    ),
                    "age_hours": round(age_hours, 1) if age_hours is not None else None,
                    "lifetime_impressions": lifetime_impressions,
                    "lifetime_spend": lifetime_spend,
                    "historical_latest_fact_date": historical_latest.get(ad_id) or None,
                    "same_adset_delivering_ads": int(
                        delivered_by_adset.get(str(item.get("adset_id") or ""), 0)
                    ),
                    "operator_review_required": zero_delivery_after_48h or no_delivery,
                    "review_focus": (
                        "NEW_AD_CREATIVE_AND_DELIVERY_REBUILD"
                        if zero_delivery_after_48h else
                        "AD_DELIVERY_ALLOCATION"
                        if no_delivery and delivered_by_adset.get(str(item.get("adset_id") or ""), 0)
                        else "ADSET_DELIVERY_CONFIGURATION" if no_delivery else None
                    ),
                },
                "current_natural_cell_lineage_status": "MISSING_EXACT_CELL_LINEAGE",
                "gate0_status": "QUASI_ONLY",
                "gate1_status": "NOT_READY",
                "causal_claim": False,
                "meta_write_allowed_by_gate": False,
                "blocking_reason_codes": blockers,
            }
            projected.append(projected_item)
            all_items.append(projected_item)
        active = [item for item in projected if item["effective_status"] == "ACTIVE"]
        account_results.append(
            {
                **account,
                "ads_total": len(projected),
                "effective_active_ads": len(active),
                "covered_ads": len(projected),
                "covered_active_ads": len(active),
                "ads_with_metric_observation": sum(
                    item["monitoring_status"] == "METRIC_OBSERVATION_AVAILABLE" for item in projected
                ),
                "active_ads_with_metric_observation": sum(
                    item["monitoring_status"] == "METRIC_OBSERVATION_AVAILABLE" for item in active
                ),
                "active_ads_zero_delivery": sum(
                    item["monitoring_status"] in {
                        "NO_LIFETIME_DELIVERY_AFTER_48H", "NO_DELIVERY_IN_COMPLETE_WINDOW",
                    }
                    for item in active
                ),
                "active_ads_zero_delivery_after_48h": sum(
                    item["monitoring_status"] == "NO_LIFETIME_DELIVERY_AFTER_48H"
                    for item in active
                ),
                "active_ads_waiting_for_facts": sum(
                    item["monitoring_status"] == "WAITING_FOR_DASHBOARD_FACTS" for item in active
                ),
                "multi_cell_experiment_ads": sum(
                    item["coverage_mode"] == "MULTI_CELL_EXPERIMENT" for item in projected
                ),
                "single_ad_observation_ads": sum(
                    item["coverage_mode"] == "SINGLE_AD_OBSERVATION" for item in projected
                ),
                "items": projected,
            }
        )
    summary = {
        "account_count": len(account_results),
        "ads_total": len(all_items),
        "effective_active_ads": sum(item["effective_status"] == "ACTIVE" for item in all_items),
        "covered_ads": len(all_items),
        "covered_active_ads": sum(item["effective_status"] == "ACTIVE" for item in all_items),
        "ads_with_metric_observation": sum(
            item["monitoring_status"] == "METRIC_OBSERVATION_AVAILABLE" for item in all_items
        ),
        "active_ads_with_metric_observation": sum(
            item["effective_status"] == "ACTIVE"
            and item["monitoring_status"] == "METRIC_OBSERVATION_AVAILABLE"
            for item in all_items
        ),
        "active_ads_zero_delivery": sum(
            item["effective_status"] == "ACTIVE"
            and item["monitoring_status"] in {
                "NO_LIFETIME_DELIVERY_AFTER_48H", "NO_DELIVERY_IN_COMPLETE_WINDOW",
            }
            for item in all_items
        ),
        "active_ads_zero_delivery_after_48h": sum(
            item["effective_status"] == "ACTIVE"
            and item["monitoring_status"] == "NO_LIFETIME_DELIVERY_AFTER_48H"
            for item in all_items
        ),
        "active_ads_waiting_for_facts": sum(
            item["effective_status"] == "ACTIVE"
            and item["monitoring_status"] == "WAITING_FOR_DASHBOARD_FACTS"
            for item in all_items
        ),
        "multi_cell_experiment_ads": sum(
            item["coverage_mode"] == "MULTI_CELL_EXPERIMENT" for item in all_items
        ),
        "single_ad_observation_ads": sum(
            item["coverage_mode"] == "SINGLE_AD_OBSERVATION" for item in all_items
        ),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "scope_status": "EXACT_FIVE_ACCOUNTS",
        "coverage_status": "ALL_META_ADS_ROSTERED_READ_ONLY",
        "fact_window": {
            "start_date": window_start, "cutoff_date": cutoff,
            "days": FACT_WINDOW_DAYS, "complete": complete_window,
        },
        "summary": summary,
        "accounts": account_results,
        "gate": {
            "gate0_status": "QUASI_ONLY",
            "gate0_result_effect": "UNCHANGED",
            "gate1_status": "NOT_READY",
            "causal_claim": False,
            "meta_write_allowed_by_gate": False,
        },
        "safety": {
            "read_only": True,
            "meta_writes_performed": False,
            "missing_values_render_as_zero": False,
            "single_ad_observation_is_multi_cell_experiment": False,
        },
    }
