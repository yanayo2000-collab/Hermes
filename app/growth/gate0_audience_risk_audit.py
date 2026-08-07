"""Subject-bound, GET-only audience-risk evidence for GLE Gate 0.

This fragment proves a narrow configuration claim: two identical Copy-only
targeting projections are bound to one exact Meta SPLIT_TEST topology with two
50/50 cells. It does not prove auction isolation or an observed intersection,
and it never grants Gate 0 by itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any, Dict, Mapping, Optional

from app.growth.gate0_topology_audit import (
    GetOnlyGraphClient,
    G004ContractError,
    _evidence_safe,
    canonical_json,
    hash_json,
)


ENGINE_VERSION = "gle-g0-04a-audience-risk-audit-v1"
RECEIPT_VERSION = "gle-g0-04a-audience-risk-receipt-v1"
EVIDENCE_VERSION = "gle-g0-04a-audience-risk-evidence-v1"
MANIFEST_VERSION = "gle-g0-04a-artifact-manifest-v1"
REQUEST_VERSION = "gle-g0-04a-audit-request-v1"
MAX_RECEIPT_TTL_SECONDS = 900
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REQUIRED_G004_CHECKS = {
    "graph_completeness", "plan_binding", "topology", "freshness", "zero_write",
}


class G004AAudienceRiskError(ValueError):
    pass


def _utc(value: Any, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise G004AAudienceRiskError(code) from exc
    if parsed.tzinfo is None:
        raise G004AAudienceRiskError(code)
    return parsed.astimezone(timezone.utc)


def _exact(value: Any, keys: set[str], code: str) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise G004AAudienceRiskError(code)
    return dict(value)


def _serialized_sha(value: Any) -> str:
    import hashlib

    return hashlib.sha256((canonical_json(value) + "\n").encode("utf-8")).hexdigest()


def _check(status: str, reasons: list[str], refs: list[str]) -> Dict[str, Any]:
    return {
        "status": status,
        "reason_codes": sorted(set(item for item in reasons if item)),
        "evidence_refs": sorted(set(item for item in refs if item)),
    }


def validate_g004_artifacts(
    manifest_raw: Any, receipt_raw: Any, evidence_raw: Any,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    manifest = _exact(
        manifest_raw,
        {
            "schema_version", "receipt_file", "receipt_sha256",
            "evidence_file", "evidence_sha256", "committed",
        },
        "G004A_G004_ARTIFACT_INVALID",
    )
    receipt = dict(receipt_raw) if isinstance(receipt_raw, dict) else {}
    evidence = dict(evidence_raw) if isinstance(evidence_raw, dict) else {}
    if (
        manifest["schema_version"] != "gle-g0-04-artifact-manifest-v1"
        or manifest["committed"] is not True
        or manifest["receipt_sha256"] != _serialized_sha(receipt)
        or manifest["evidence_sha256"] != _serialized_sha(evidence)
    ):
        raise G004AAudienceRiskError("G004A_G004_ARTIFACT_INVALID")
    evidence_hash = str(evidence.get("evidence_bundle_hash") or "")
    unsigned_evidence = dict(evidence)
    unsigned_evidence.pop("evidence_bundle_hash", None)
    receipt_hash = str(receipt.get("receipt_body_hash") or "")
    unsigned_receipt = dict(receipt)
    unsigned_receipt.pop("receipt_body_hash", None)
    if (
        evidence.get("schema_version") != "gle-g0-04-redacted-evidence-bundle-v1"
        or receipt.get("schema_version") != "gle-g0-04-audit-receipt-v1"
        or hash_json(unsigned_evidence) != evidence_hash
        or hash_json(unsigned_receipt) != receipt_hash
        or receipt.get("evidence_bundle_hash") != evidence_hash
        or receipt.get("target") != evidence.get("target")
        or receipt.get("not_gate_receipt") is not True
        or receipt.get("gate0_result_ceiling") != "QUASI_ONLY"
        or receipt.get("source_snapshot_sha256") != evidence.get("source_snapshot_sha256")
        or not isinstance(receipt.get("graph_evidence_hash"), str)
        or not _SHA256_RE.fullmatch(receipt["graph_evidence_hash"])
    ):
        raise G004AAudienceRiskError("G004A_G004_ARTIFACT_INVALID")
    checks = receipt.get("checks")
    if not isinstance(checks, dict) or not _REQUIRED_G004_CHECKS.issubset(checks):
        raise G004AAudienceRiskError("G004A_G004_ARTIFACT_INVALID")
    if any(dict(checks.get(key) or {}).get("status") != "PASS" for key in _REQUIRED_G004_CHECKS):
        raise G004AAudienceRiskError("G004A_G004_ARTIFACT_INVALID")
    return receipt, evidence


def normalize_request(raw: Mapping[str, Any]) -> Dict[str, Any]:
    request = _exact(
        raw,
        {
            "schema_version", "audit_id", "requested_at", "request_nonce",
            "source_snapshot_sha256", "g004_receipt_body_hash",
            "g004_evidence_bundle_hash", "receipt_ttl_seconds",
        },
        "G004A_INPUT_INVALID",
    )
    if request["schema_version"] != REQUEST_VERSION:
        raise G004AAudienceRiskError("G004A_INPUT_INVALID")
    for key in ("audit_id", "request_nonce"):
        if not isinstance(request[key], str) or not request[key].strip():
            raise G004AAudienceRiskError("G004A_INPUT_INVALID")
    for key in (
        "source_snapshot_sha256", "g004_receipt_body_hash", "g004_evidence_bundle_hash",
    ):
        if not isinstance(request[key], str) or not _SHA256_RE.fullmatch(request[key]):
            raise G004AAudienceRiskError("G004A_INPUT_INVALID")
    _utc(request["requested_at"], "G004A_INPUT_INVALID")
    ttl = request["receipt_ttl_seconds"]
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 60 <= ttl <= MAX_RECEIPT_TTL_SECONDS:
        raise G004AAudienceRiskError("G004A_INPUT_INVALID")
    return request


def _subject_from_g004(receipt: Mapping[str, Any], evidence: Mapping[str, Any]) -> Dict[str, Any]:
    target = dict(receipt.get("target") or {})
    graph = dict(evidence.get("graph") or {})
    study_cells = list(dict(graph.get("study_cells") or {}).get("data") or [])
    if len(study_cells) != 2:
        raise G004AAudienceRiskError("G004A_TOPOLOGY_UNBOUND")
    cells = []
    for key in ("C1", "C2"):
        edge = list(dict(graph.get(f"cell_{key}_adsets") or {}).get("data") or [])
        adset = dict(graph.get(f"first_adset_{key}") or {})
        ad = dict(graph.get(f"first_ad_{key}") or {})
        ad_id = str(ad.get("id") or "")
        adset_id = str(adset.get("id") or "")
        matching = [
            item for item in study_cells
            if ad_id in [str(value) for value in list(dict(item).get("ad_ids") or [])]
        ]
        if (
            len(edge) != 1
            or len(matching) != 1
            or str(dict(edge[0]).get("id") or "") != adset_id
            or str(ad.get("adset_id") or "") != adset_id
            or not ad_id
            or not adset_id
        ):
            raise G004AAudienceRiskError("G004A_TOPOLOGY_UNBOUND")
        cells.append({
            "cell_id": key,
            "study_cell_id": str(matching[0].get("id") or ""),
            "adset_id": adset_id,
            "ad_id": ad_id,
        })
    required = {"ad_account_id", "market", "study_id", "campaign_id"}
    if not required.issubset(target) or any(not str(target.get(key) or "") for key in required):
        raise G004AAudienceRiskError("G004A_TOPOLOGY_UNBOUND")
    if len({cell["study_cell_id"] for cell in cells}) != 2:
        raise G004AAudienceRiskError("G004A_TOPOLOGY_UNBOUND")
    return {**{key: str(target[key]) for key in sorted(required)}, "cells": cells}


def allowed_graph_paths(subject: Mapping[str, Any]) -> set[str]:
    return {
        str(subject["study_id"]),
        f"{subject['study_id']}/cells",
        f"act_{subject['ad_account_id']}/delivery_estimate",
        *[str(cell["adset_id"]) for cell in subject["cells"]],
        *[str(cell["ad_id"]) for cell in subject["cells"]],
        *[f"{cell['study_cell_id']}/adsets" for cell in subject["cells"]],
    }


def _graph_get(client: GetOnlyGraphClient, path: str, fields: str, **params: Any) -> Dict[str, Any]:
    try:
        return client.get(path, fields=fields, params=params)
    except Exception as exc:
        return {"error": type(exc).__name__}


def _graph_edge(client: GetOnlyGraphClient, path: str, fields: str) -> Dict[str, Any]:
    try:
        return client.get_edge(path, fields=fields)
    except Exception as exc:
        return {"error": type(exc).__name__, "pagination_complete": False}


def collect_graph_evidence(client: GetOnlyGraphClient, subject: Mapping[str, Any]) -> Dict[str, Any]:
    graph: Dict[str, Any] = {
        "study": _graph_get(client, str(subject["study_id"]), "id,type,start_time,end_time"),
        "study_cells": _graph_edge(
            client, f"{subject['study_id']}/cells",
            "id,treatment_percentage,control_percentage,ad_entities_count,ad_ids",
        ),
    }
    fields = (
        "id,account_id,campaign_id,status,effective_status,daily_budget,lifetime_budget,bid_strategy,"
        "bid_amount,billing_event,optimization_goal,promoted_object,targeting,attribution_spec,updated_time"
    )
    for cell in subject["cells"]:
        key = cell["cell_id"]
        graph[f"cell_{key}_adsets"] = _graph_edge(
            client, f"{cell['study_cell_id']}/adsets", "id,campaign_id,status,effective_status",
        )
        graph[f"adset_{key}"] = _graph_get(client, str(cell["adset_id"]), fields)
        graph[f"ad_{key}"] = _graph_get(
            client, str(cell["ad_id"]),
            "id,account_id,campaign_id,adset_id,status,effective_status,updated_time",
        )
    for key in ("C1", "C2"):
        adset = dict(graph.get(f"adset_{key}") or {})
        targeting = adset.get("targeting")
        promoted = adset.get("promoted_object")
        optimization = str(adset.get("optimization_goal") or "")
        if not isinstance(targeting, dict) or not isinstance(promoted, dict) or not optimization:
            graph[f"delivery_{key}"] = {"error": "G004A_DELIVERY_INPUT_INVALID"}
            continue
        graph[f"delivery_{key}"] = _graph_get(
            client, f"act_{subject['ad_account_id']}/delivery_estimate", "",
            optimization_goal=optimization,
            promoted_object=canonical_json(promoted),
            targeting_spec=canonical_json(targeting),
        )
    first_c1 = dict(graph.get("adset_C1") or {})
    first_c2 = dict(graph.get("adset_C2") or {})
    if (
        isinstance(first_c1.get("targeting"), dict)
        and isinstance(first_c2.get("targeting"), dict)
        and hash_json(first_c1["targeting"]) == hash_json(first_c2["targeting"])
        and first_c1.get("promoted_object") == first_c2.get("promoted_object")
        and str(first_c1.get("optimization_goal") or "")
    ):
        graph["delivery_reference"] = _graph_get(
            client, f"act_{subject['ad_account_id']}/delivery_estimate", "",
            optimization_goal=str(first_c1["optimization_goal"]),
            promoted_object=canonical_json(first_c1["promoted_object"]),
            targeting_spec=canonical_json(first_c1["targeting"]),
        )
    else:
        graph["delivery_reference"] = {"error": "G004A_DELIVERY_INPUT_INVALID"}
    for cell in subject["cells"]:
        key = cell["cell_id"]
        graph[f"adset_{key}_final"] = _graph_get(client, str(cell["adset_id"]), fields)
        graph[f"ad_{key}_final"] = _graph_get(
            client, str(cell["ad_id"]),
            "id,account_id,campaign_id,adset_id,status,effective_status,updated_time",
        )
        graph[f"cell_{key}_adsets_final"] = _graph_edge(
            client, f"{cell['study_cell_id']}/adsets", "id,campaign_id,status,effective_status",
        )
    graph["study_final"] = _graph_get(
        client, str(subject["study_id"]), "id,type,start_time,end_time",
    )
    graph["study_cells_final"] = _graph_edge(
        client, f"{subject['study_id']}/cells",
        "id,treatment_percentage,control_percentage,ad_entities_count,ad_ids",
    )
    graph["graph_hash"] = hash_json(graph)
    return graph


def _delivery_projection(body: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    rows = body.get("data")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    if row.get("estimate_ready") is not True:
        return None
    try:
        lower = int(row.get("estimate_mau_lower_bound") or 0)
        upper = int(row.get("estimate_mau_upper_bound") or 0)
    except (TypeError, ValueError):
        return None
    if lower <= 0 or upper < lower:
        return None
    return {"lower": lower, "upper": upper, "estimate_ready": True}


def _targeting_matches_market(targeting: Any, market: str) -> bool:
    if not isinstance(targeting, dict):
        return False
    geo = targeting.get("geo_locations")
    if not isinstance(geo, dict):
        return False
    countries = geo.get("countries")
    return (
        isinstance(countries, list)
        and [str(value).upper() for value in countries] == [market.upper()]
    )


_ADSET_CONFIG_KEYS = (
    "campaign_id", "daily_budget", "lifetime_budget", "bid_strategy", "bid_amount",
    "billing_event", "optimization_goal", "promoted_object", "targeting", "attribution_spec",
)


def _adset_config_projection(value: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value.get(key) for key in _ADSET_CONFIG_KEYS}


def build_artifacts(
    *, request: Mapping[str, Any], g004_manifest: Mapping[str, Any],
    g004_receipt: Mapping[str, Any], g004_evidence: Mapping[str, Any],
    session: Any, access_token: str, now: Optional[datetime] = None,
) -> Dict[str, Any]:
    normalized = normalize_request(request)
    receipt, evidence = validate_g004_artifacts(g004_manifest, g004_receipt, g004_evidence)
    if (
        normalized["source_snapshot_sha256"] != receipt.get("source_snapshot_sha256")
        or normalized["g004_receipt_body_hash"] != receipt.get("receipt_body_hash")
        or normalized["g004_evidence_bundle_hash"] != evidence.get("evidence_bundle_hash")
    ):
        raise G004AAudienceRiskError("G004A_G004_BINDING_MISMATCH")
    subject = _subject_from_g004(receipt, evidence)
    checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if abs((_utc(normalized["requested_at"], "G004A_INPUT_INVALID") - checked_at).total_seconds()) > 60:
        raise G004AAudienceRiskError("G004A_INPUT_INVALID")
    if _utc(receipt.get("expires_at"), "G004A_G004_RECEIPT_EXPIRED") <= checked_at:
        raise G004AAudienceRiskError("G004A_G004_RECEIPT_EXPIRED")
    client = GetOnlyGraphClient(
        session=session, access_token=access_token, now=checked_at,
        allowed_paths=allowed_graph_paths(subject), max_pages=5, max_items=100,
    )
    graph = collect_graph_evidence(client, subject)
    g004_graph = dict(evidence.get("graph") or {})
    reasons: list[str] = []
    study = dict(graph.get("study") or {})
    study_cells_body = dict(graph.get("study_cells") or {})
    live_cells = list(study_cells_body.get("data") or [])
    if study.get("id") != subject["study_id"] or study.get("type") != "SPLIT_TEST":
        reasons.append("STUDY_NOT_SPLIT_TEST")
    expected_cell_ids = {cell["study_cell_id"] for cell in subject["cells"]}
    actual_cell_ids = {str(item.get("id") or "") for item in live_cells if isinstance(item, dict)}
    allocations = sorted(int(item.get("treatment_percentage") or 0) for item in live_cells if isinstance(item, dict))
    if (
        study_cells_body.get("pagination_complete") is not True
        or actual_cell_ids != expected_cell_ids
        or allocations != [50, 50]
    ):
        reasons.append("CELL_ALLOCATION_MISMATCH")
    projections = []
    for cell in subject["cells"]:
        key = cell["cell_id"]
        edge = dict(graph.get(f"cell_{key}_adsets") or {})
        rows = list(edge.get("data") or [])
        adset = dict(graph.get(f"adset_{key}") or {})
        final_adset = dict(graph.get(f"adset_{key}_final") or {})
        ad = dict(graph.get(f"ad_{key}") or {})
        final_ad = dict(graph.get(f"ad_{key}_final") or {})
        live_cell = next(
            (dict(item) for item in live_cells if str(dict(item).get("id") or "") == cell["study_cell_id"]),
            {},
        )
        parent_cells = list(dict(g004_graph.get("study_cells") or {}).get("data") or [])
        parent_cell = next(
            (dict(item) for item in parent_cells if str(dict(item).get("id") or "") == cell["study_cell_id"]),
            {},
        )
        live_cell_projection = {
            field: live_cell.get(field)
            for field in (
                "id", "treatment_percentage", "control_percentage",
                "ad_entities_count", "ad_ids",
            )
        }
        parent_cell_projection = {
            field: parent_cell.get(field)
            for field in live_cell_projection
        }
        if (
            edge.get("pagination_complete") is not True
            or len(rows) != 1
            or str(rows[0].get("id") or "") != cell["adset_id"]
            or str(adset.get("id") or "") != cell["adset_id"]
            or str(adset.get("account_id") or "").removeprefix("act_") != subject["ad_account_id"]
            or str(adset.get("campaign_id") or "") != subject["campaign_id"]
            or [str(value) for value in list(live_cell.get("ad_ids") or [])] != [cell["ad_id"]]
            or str(ad.get("id") or "") != cell["ad_id"]
            or str(ad.get("account_id") or "").removeprefix("act_") != subject["ad_account_id"]
            or str(ad.get("campaign_id") or "") != subject["campaign_id"]
            or str(ad.get("adset_id") or "") != cell["adset_id"]
            or live_cell.get("control_percentage") != 0
            or live_cell.get("ad_entities_count") != 1
            or live_cell_projection != parent_cell_projection
        ):
            reasons.append("CELL_ADSET_BINDING_MISMATCH")
        if (
            final_adset != adset
            or final_ad != ad
            or dict(graph.get(f"cell_{key}_adsets_final") or {}) != edge
        ):
            reasons.append("OBJECT_DRIFT_DURING_AUDIT")
        projection = _adset_config_projection(adset)
        projections.append(projection)
        expected_projection = _adset_config_projection(
            dict(g004_graph.get(f"first_adset_{key}") or {})
        )
        if hash_json(_evidence_safe(projection)) != hash_json(expected_projection):
            reasons.append("G004_PLAN_PROJECTION_DRIFT")
        if not _targeting_matches_market(adset.get("targeting"), subject["market"]):
            reasons.append("TARGET_MARKET_MISMATCH")
        if _delivery_projection(dict(graph.get(f"delivery_{key}") or {})) is None:
            reasons.append("DELIVERY_ESTIMATE_UNAVAILABLE")
    if _delivery_projection(dict(graph.get("delivery_reference") or {})) is None:
        reasons.append("DELIVERY_ESTIMATE_UNAVAILABLE")
    if (
        dict(graph.get("study_final") or {}) != study
        or dict(graph.get("study_cells_final") or {}) != study_cells_body
    ):
        reasons.append("OBJECT_DRIFT_DURING_AUDIT")
    if len(projections) != 2 or hash_json(projections[0]) != hash_json(projections[1]):
        reasons.append("AUDIENCE_OR_DELIVERY_CONFIG_DRIFT")
    zero_write = client.proof()
    zero_write["local_db_writes"] = 0
    transport_journal = [entry.__dict__ for entry in client.journal]
    if any(int(zero_write.get(key) or 0) for key in (
        "post_count", "put_count", "patch_count", "delete_count", "redirect_count",
        "batch_count", "async_job_count", "meta_object_writes",
    )):
        reasons.append("TRANSPORT_METHOD_FORBIDDEN")
    checks = {
        "g004_binding": _check("PASS", [], [receipt["receipt_body_hash"]]),
        "topology": _check("PASS" if not set(reasons) & {"STUDY_NOT_SPLIT_TEST", "CELL_ALLOCATION_MISMATCH", "CELL_ADSET_BINDING_MISMATCH"} else "FAIL", [r for r in reasons if r in {"STUDY_NOT_SPLIT_TEST", "CELL_ALLOCATION_MISMATCH", "CELL_ADSET_BINDING_MISMATCH"}], [graph["graph_hash"]]),
        "targeting_equivalence": _check("PASS" if not set(reasons) & {"AUDIENCE_OR_DELIVERY_CONFIG_DRIFT", "TARGET_MARKET_MISMATCH", "G004_PLAN_PROJECTION_DRIFT"} else "FAIL", [r for r in reasons if r in {"AUDIENCE_OR_DELIVERY_CONFIG_DRIFT", "TARGET_MARKET_MISMATCH", "G004_PLAN_PROJECTION_DRIFT"}], [hash_json(projections)]),
        "delivery_estimate": _check("PASS" if "DELIVERY_ESTIMATE_UNAVAILABLE" not in reasons else "INCOMPLETE", [r for r in reasons if r == "DELIVERY_ESTIMATE_UNAVAILABLE"], [graph["graph_hash"]]),
        "freshness": _check("PASS" if "OBJECT_DRIFT_DURING_AUDIT" not in reasons else "INCOMPLETE", [r for r in reasons if r == "OBJECT_DRIFT_DURING_AUDIT"], [graph["graph_hash"]]),
        "split_test_topology": _check("PASS" if not reasons else "INCOMPLETE", reasons, [graph["graph_hash"]]),
        "zero_write": _check("PASS" if "TRANSPORT_METHOD_FORBIDDEN" not in reasons else "FAIL", [r for r in reasons if r == "TRANSPORT_METHOD_FORBIDDEN"], [zero_write["request_journal_hash"]]),
    }
    statuses = {item["status"] for item in checks.values()}
    technical_outcome = "FAIL" if "FAIL" in statuses else ("INCOMPLETE" if "INCOMPLETE" in statuses else "PASS")
    outcome = "INCOMPLETE" if technical_outcome == "PASS" else technical_outcome
    evidence_body = {
        "schema_version": EVIDENCE_VERSION,
        "request_hash": hash_json(normalized),
        "source_snapshot_sha256": normalized["source_snapshot_sha256"],
        "g004_receipt_body_hash": receipt["receipt_body_hash"],
        "g004_evidence_bundle_hash": evidence["evidence_bundle_hash"],
        "subject": subject,
        "live_projection_hashes": {
            "C1": hash_json(projections[0]), "C2": hash_json(projections[1]),
            "graph": graph["graph_hash"],
        },
        "delivery_estimates": {
            key: _delivery_projection(dict(graph.get(f"delivery_{key}") or {}))
            for key in ("C1", "C2")
        },
        "configured_targeting_similarity": "IDENTICAL" if len(projections) == 2 and hash_json(projections[0]) == hash_json(projections[1]) else "UNKNOWN",
        "inference_basis": "CONFIGURATION_AND_SPLIT_TEST_TOPOLOGY_ONLY",
        "transport_proof": zero_write,
        "transport_journal": transport_journal,
        "checked_at": checked_at.isoformat(),
    }
    evidence_body["delivery_estimates"]["reference"] = _delivery_projection(
        dict(graph.get("delivery_reference") or {})
    )
    evidence_body["evidence_body_hash"] = hash_json(evidence_body)
    expires_at = min(
        checked_at + timedelta(seconds=normalized["receipt_ttl_seconds"]),
        _utc(receipt.get("expires_at"), "G004A_G004_RECEIPT_EXPIRED"),
    )
    receipt_body = {
        "schema_version": RECEIPT_VERSION,
        "engine_version": ENGINE_VERSION,
        "audit_id": normalized["audit_id"],
        "request_hash": hash_json(normalized),
        "source_snapshot_sha256": normalized["source_snapshot_sha256"],
        "g004_receipt_body_hash": receipt["receipt_body_hash"],
        "g004_evidence_bundle_hash": evidence["evidence_bundle_hash"],
        "subject": subject,
        "checked_at": checked_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "checks": checks,
        "outcome": outcome,
        "audience_overlap_classification": "TARGETING_CONFIG_EQUIVALENT" if checks["targeting_equivalence"]["status"] == "PASS" else "UNKNOWN",
        "internal_auction_classification": "UNKNOWN",
        "evidence_body_hash": evidence_body["evidence_body_hash"],
        "not_gate_receipt": True,
        "gate0_result_ceiling": "QUASI_ONLY",
        "blocking_reasons": sorted(set(reasons + [
            "AUDIENCE_OVERLAP_UNKNOWN", "INTERNAL_AUCTION_CONTAMINATION_UNKNOWN",
        ])),
    }
    receipt_body["receipt_body_hash"] = hash_json(receipt_body)
    return {"receipt": receipt_body, "evidence": evidence_body}


def artifact_manifest(receipt: Mapping[str, Any], evidence: Mapping[str, Any], *, receipt_file: str, evidence_file: str) -> Dict[str, Any]:
    return {
        "schema_version": MANIFEST_VERSION,
        "receipt_file": receipt_file,
        "receipt_sha256": _serialized_sha(receipt),
        "evidence_file": evidence_file,
        "evidence_sha256": _serialized_sha(evidence),
        "committed": True,
    }
