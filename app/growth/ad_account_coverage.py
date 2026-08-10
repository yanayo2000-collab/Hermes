from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Sequence


SCHEMA_VERSION = "gle-ad-account-coverage-v1"
FACT_WINDOW_DAYS = 31
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
                    "created_time,updated_time"
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
) -> tuple[str | None, str | None, dict[str, dict[str, Any]]]:
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
        return None, None, {}
    window_start = cutoff_date - timedelta(days=FACT_WINDOW_DAYS - 1)
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
    return window_start.isoformat(), cutoff_date.isoformat(), observations


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
) -> Dict[str, Any]:
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
    window_start, cutoff, facts = _load_fact_observations(conn, ad_ids)
    memberships = _experiment_memberships(conn, ad_ids)
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
            membership = memberships.get(ad_id)
            is_experiment = membership is not None
            if is_experiment and fact_bound:
                evaluation_status = "EXPERIMENT_OPERATING_EVALUATION_AVAILABLE"
            elif is_experiment:
                evaluation_status = "EXPERIMENT_REGISTERED_WAITING_FOR_FACTS"
            elif fact_bound:
                evaluation_status = "SINGLE_AD_OPERATING_OBSERVATION_AVAILABLE"
            else:
                evaluation_status = "OBSERVATION_REGISTERED_WAITING_FOR_FACTS"
            blockers = ["MISSING_EXACT_CELL_LINEAGE", "GATE0_QUASI_ONLY", "GATE1_NOT_READY"]
            if not fact_bound:
                blockers.insert(0, "DASHBOARD_FACTS_NOT_AVAILABLE_IN_WINDOW")
            if not is_experiment:
                blockers.insert(0, "NOT_REGISTERED_AS_MULTI_CELL_EXPERIMENT")
            projected_item = {
                **item,
                "coverage_status": "COVERED_READ_ONLY",
                "coverage_mode": "MULTI_CELL_EXPERIMENT" if is_experiment else "SINGLE_AD_OBSERVATION",
                "monitoring_status": (
                    "METRIC_OBSERVATION_AVAILABLE" if fact_bound else "WAITING_FOR_DASHBOARD_FACTS"
                ),
                "evaluation_status": evaluation_status,
                "experiment_binding": membership,
                "fact_window": {
                    "start_date": window_start,
                    "cutoff_date": cutoff,
                    "latest_fact_date": str((fact or {}).get("latest_date") or "") or None,
                    "row_count": int((fact or {}).get("fact_row_count") or 0),
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
        "fact_window": {"start_date": window_start, "cutoff_date": cutoff, "days": FACT_WINDOW_DAYS},
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
