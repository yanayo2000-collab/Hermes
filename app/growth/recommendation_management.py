from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, Tuple


SYSTEM_MANAGED_MODE = "SYSTEM_MANAGED"
SYSTEM_MANAGED_SOURCE = "NEW_ACCOUNT_ORDER"


def _experiment_ad_name_expression() -> str:
    return """
        COALESCE(
            NULLIF(json_extract(hypothesis_json, '$.meta_names.ad'), ''),
            NULLIF(json_extract(hypothesis_json, '$.creative_direction.meta_names.ad'), ''),
            NULLIF(json_extract(variant_definition_json, '$.meta_names.ad'), ''),
            NULLIF(json_extract(variant_definition_json, '$.creative_direction.meta_names.ad'), '')
        )
    """


def _recommendation_locations(payload: Dict[str, Any]) -> Dict[str, Tuple[str, str]]:
    locations: Dict[str, Tuple[str, str]] = {}
    for row in payload.get("ad_objects") or []:
        if not isinstance(row, dict):
            continue
        object_id = str(row.get("object_id") or "").strip()
        ad_name = str(row.get("ad") or row.get("object_name") or "").strip()
        account_id = str(row.get("account_id") or "").strip()
        if object_id and ad_name:
            locations[object_id] = (account_id, ad_name)
    return locations


def _system_management_state(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "mode": SYSTEM_MANAGED_MODE,
        "source": SYSTEM_MANAGED_SOURCE,
        "user_confirmation_required": False,
        "launch_id": str(row["source_report_id"] or ""),
        "experiment_id": str(row["experiment_id"] or ""),
        "experiment_state": str(row["state"] or ""),
        "meta_ad_id": str(row["source_ad_id"] or ""),
        "standing_authorization_scope": [
            "ANALYZE", "OBSERVE", "CHECK_DATA", "CREATE_PAUSED_PLAN",
        ],
        "separate_confirmation_actions": ["ACTIVATE", "SCALE", "CHANGE_BUDGET"],
    }


def enrich_system_managed_recommendations(
    conn: sqlite3.Connection, payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Attach order-lineage management state without inferring from ad naming alone."""
    recommendations = [
        row for row in payload.get("recommendations") or [] if isinstance(row, dict)
    ]
    locations = _recommendation_locations(payload)
    names = sorted({
        locations.get(str(row.get("object_id") or "").strip(), ("", ""))[1]
        or str(row.get("object_name") or "").strip()
        for row in recommendations
    } - {""})
    if not names:
        return payload

    placeholders = ",".join("?" for _ in names)
    ad_name_expression = _experiment_ad_name_expression()
    try:
        rows: Iterable[sqlite3.Row] = conn.execute(
            f"""
            SELECT experiment_id,source_report_id,source_ad_id,account_id,state,
                   {ad_name_expression} AS ad_name
            FROM ad_experiment
            WHERE source_report_id LIKE 'newacct_%'
              AND source_ad_id<>''
              AND state<>'ARCHIVED'
              AND {ad_name_expression} IN ({placeholders})
            ORDER BY updated_at DESC,experiment_id DESC
            """,
            names,
        ).fetchall()
    except sqlite3.OperationalError:
        return payload

    by_account_and_name: Dict[Tuple[str, str], sqlite3.Row] = {}
    by_name: Dict[str, sqlite3.Row] = {}
    ambiguous_names = set()
    for row in rows:
        ad_name = str(row["ad_name"] or "").strip()
        account_id = str(row["account_id"] or "").strip()
        if not ad_name:
            continue
        by_account_and_name.setdefault((account_id, ad_name), row)
        if ad_name in by_name and str(by_name[ad_name]["account_id"] or "") != account_id:
            ambiguous_names.add(ad_name)
        else:
            by_name.setdefault(ad_name, row)

    for item in recommendations:
        object_id = str(item.get("object_id") or "").strip()
        account_id, mapped_name = locations.get(object_id, ("", ""))
        ad_name = mapped_name or str(item.get("object_name") or "").strip()
        experiment = by_account_and_name.get((account_id, ad_name)) if account_id else None
        if experiment is None and ad_name not in ambiguous_names:
            experiment = by_name.get(ad_name)
        if experiment is None:
            continue
        item["system_managed"] = True
        item["management_state"] = _system_management_state(experiment)
    return payload
