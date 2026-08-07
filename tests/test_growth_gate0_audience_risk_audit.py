from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.growth.gate0_audience_risk_audit import (
    G004AAudienceRiskError, artifact_manifest, build_artifacts,
)
from app.growth.gate0_topology_audit import _evidence_safe, canonical_json, hash_json


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 64


def _sha_line(value: dict) -> str:
    import hashlib
    return hashlib.sha256((canonical_json(value) + "\n").encode()).hexdigest()


def _g004() -> tuple[dict, dict, dict]:
    adset_projection = {
        "id": "adset-placeholder", "account_id": "act_1012060198097836",
        "campaign_id": "campaign-1", "status": "PAUSED", "effective_status": "PAUSED",
        "daily_budget": "2000", "lifetime_budget": "0", "bid_strategy": "COST_CAP",
        "bid_amount": "55", "billing_event": "IMPRESSIONS", "optimization_goal": "APP_INSTALLS",
        "promoted_object": {"application_id": "app-1", "object_store_url": "https://example.com/app"},
        "targeting": {"geo_locations": {"countries": ["MX"]}, "age_min": 18, "age_max": 40},
        "attribution_spec": [{"event_type": "CLICK_THROUGH", "window_days": 1}],
        "updated_time": "2026-08-07T11:00:00+00:00",
    }
    graph = {
        "study_cells": {"data": [
            {"id": "sc-1", "treatment_percentage": 50, "control_percentage": 0, "ad_entities_count": 1, "ad_ids": ["ad-1"]},
            {"id": "sc-2", "treatment_percentage": 50, "control_percentage": 0, "ad_entities_count": 1, "ad_ids": ["ad-2"]},
        ], "pagination_complete": True, "page_count": 1},
        "cell_C1_adsets": {"data": [{"id": "adset-1"}], "pagination_complete": True},
        "cell_C2_adsets": {"data": [{"id": "adset-2"}], "pagination_complete": True},
        "first_adset_C1": {**adset_projection, "id": "adset-1"},
        "first_adset_C2": {**adset_projection, "id": "adset-2"},
        "first_ad_C1": {"id": "ad-1", "adset_id": "adset-1"},
        "first_ad_C2": {"id": "ad-2", "adset_id": "adset-2"},
    }
    target = {"ad_account_id": "1012060198097836", "market": "MX", "study_id": "study-1", "campaign_id": "campaign-1"}
    evidence = {
        "schema_version": "gle-g0-04-redacted-evidence-bundle-v1", "request_hash": "b" * 64,
        "source_snapshot_sha256": SHA, "local_evidence_hash": "c" * 64,
        "target": target, "graph": _evidence_safe(graph), "transport_journal": [],
    }
    evidence["evidence_bundle_hash"] = hash_json(evidence)
    receipt = {
        "schema_version": "gle-g0-04-audit-receipt-v1", "source_snapshot_sha256": SHA,
        "target": target, "evidence_bundle_hash": evidence["evidence_bundle_hash"],
        "not_gate_receipt": True, "gate0_result_ceiling": "QUASI_ONLY",
        "expires_at": "2026-08-08T00:00:00+00:00",
        "graph_evidence_hash": "7" * 64,
        "checks": {
            key: {"status": "PASS", "reason_codes": [], "evidence_refs": []}
            for key in ("graph_completeness", "plan_binding", "topology", "freshness", "zero_write")
        },
    }
    receipt["receipt_body_hash"] = hash_json(receipt)
    manifest = {
        "schema_version": "gle-g0-04-artifact-manifest-v1",
        "receipt_file": "g004-receipt.json", "receipt_sha256": _sha_line(receipt),
        "evidence_file": "g004-evidence.json", "evidence_sha256": _sha_line(evidence),
        "committed": True,
    }
    return manifest, receipt, evidence


def _request(receipt: dict, evidence: dict) -> dict:
    return {
        "schema_version": "gle-g0-04a-audit-request-v1", "audit_id": "g004a-1",
        "requested_at": NOW.isoformat(), "request_nonce": "nonce-1",
        "source_snapshot_sha256": SHA, "g004_receipt_body_hash": receipt["receipt_body_hash"],
        "g004_evidence_bundle_hash": evidence["evidence_bundle_hash"], "receipt_ttl_seconds": 300,
    }


class _Response:
    history = []
    status_code = 200
    headers: dict = {}
    def __init__(self, body: dict) -> None:
        self._body = deepcopy(body)
    def json(self) -> dict:
        return deepcopy(self._body)


class _Session:
    def __init__(self, *, drift: bool = False, extra_cell: bool = False, during_run_drift: bool = False, topology_drift: bool = False, country: str = "MX", daily_budget: str = "2000", ad_entities_count: int = 1, estimate_ready=True) -> None:
        self.drift, self.extra_cell, self.during_run_drift = drift, extra_cell, during_run_drift
        self.topology_drift, self.country = topology_drift, country
        self.daily_budget, self.ad_entities_count = daily_budget, ad_entities_count
        self.estimate_ready = estimate_ready
        self.calls: list[tuple[str, dict]] = []
        self.adset_reads: dict[str, int] = {}
        self.edge_reads = 0
    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        path = url.split("/v25.0/")[-1]
        if path == "study-1":
            return _Response({"id": "study-1", "type": "SPLIT_TEST"})
        if path == "study-1/cells":
            self.edge_reads += 1
            rows = [
                {"id": "sc-1", "treatment_percentage": 50, "control_percentage": 0, "ad_entities_count": self.ad_entities_count, "ad_ids": ["ad-1"]},
                {"id": "sc-2", "treatment_percentage": 50, "control_percentage": 0, "ad_entities_count": self.ad_entities_count, "ad_ids": ["ad-2"]},
            ]
            if self.extra_cell:
                rows.append({"id": "sc-3", "treatment_percentage": 0, "ad_ids": []})
            if self.topology_drift and self.edge_reads > 1:
                rows[1]["treatment_percentage"] = 49
            return _Response({"data": rows})
        if path in {"sc-1/adsets", "sc-2/adsets"}:
            index = "1" if path.startswith("sc-1") else "2"
            return _Response({"data": [{"id": f"adset-{index}", "campaign_id": "campaign-1"}]})
        if path in {"adset-1", "adset-2"}:
            self.adset_reads[path] = self.adset_reads.get(path, 0) + 1
            targeting = {"geo_locations": {"countries": [self.country]}, "age_min": 18, "age_max": 40}
            if self.drift and path == "adset-2":
                targeting["age_max"] = 41
            if self.during_run_drift and path == "adset-2" and self.adset_reads[path] > 1:
                targeting["age_max"] = 41
            return _Response({
                "id": path, "account_id": "act_1012060198097836", "campaign_id": "campaign-1", "status": "PAUSED", "effective_status": "PAUSED",
                "daily_budget": self.daily_budget, "lifetime_budget": "0", "bid_strategy": "COST_CAP", "bid_amount": "55",
                "billing_event": "IMPRESSIONS", "optimization_goal": "APP_INSTALLS",
                "promoted_object": {"application_id": "app-1", "object_store_url": "https://example.com/app"},
                "targeting": targeting, "attribution_spec": [{"event_type": "CLICK_THROUGH", "window_days": 1}],
                "updated_time": "2026-08-07T11:00:00+00:00",
            })
        if path in {"ad-1", "ad-2"}:
            index = "1" if path == "ad-1" else "2"
            return _Response({
                "id": path, "account_id": "act_1012060198097836",
                "campaign_id": "campaign-1", "adset_id": f"adset-{index}",
                "status": "PAUSED", "effective_status": "PAUSED",
                "updated_time": "2026-08-07T11:00:00+00:00",
            })
        if path == "act_1012060198097836/delivery_estimate":
            row = {"estimate_mau_lower_bound": 1000, "estimate_mau_upper_bound": 2000}
            if self.estimate_ready != "missing":
                row["estimate_ready"] = self.estimate_ready
            return _Response({"data": [row]})
        raise AssertionError(path)


def _run(*, drift: bool = False, extra_cell: bool = False):
    manifest, receipt, evidence = _g004()
    session = _Session(drift=drift, extra_cell=extra_cell)
    result = build_artifacts(
        request=_request(receipt, evidence), g004_manifest=manifest, g004_receipt=receipt,
        g004_evidence=evidence, session=session, access_token="secret-test-token", now=NOW,
    )
    return result, session


def test_copy_only_split_test_proves_subject_bound_configuration_get_only():
    result, session = _run()
    receipt = result["receipt"]
    assert receipt["outcome"] == "INCOMPLETE"
    assert receipt["audience_overlap_classification"] == "TARGETING_CONFIG_EQUIVALENT"
    assert receipt["internal_auction_classification"] == "UNKNOWN"
    assert "INTERNAL_AUCTION_CONTAMINATION_UNKNOWN" in receipt["blocking_reasons"]
    assert result["evidence"]["transport_proof"]["allowed_methods"] == ["GET"]
    assert result["evidence"]["transport_proof"]["post_count"] == 0
    assert "secret-test-token" not in canonical_json(result)
    assert artifact_manifest(receipt, result["evidence"], receipt_file="r.json", evidence_file="e.json")["committed"] is True


def test_targeting_drift_or_extra_cell_fails_closed():
    result, _ = _run(drift=True)
    assert result["receipt"]["outcome"] == "FAIL"
    assert "AUDIENCE_OR_DELIVERY_CONFIG_DRIFT" in result["receipt"]["blocking_reasons"]
    result, _ = _run(extra_cell=True)
    assert result["receipt"]["outcome"] != "PASS"
    assert "CELL_ALLOCATION_MISMATCH" in result["receipt"]["blocking_reasons"]


def test_live_targeting_must_match_the_frozen_market():
    manifest, receipt, evidence = _g004()
    result = build_artifacts(
        request=_request(receipt, evidence), g004_manifest=manifest, g004_receipt=receipt,
        g004_evidence=evidence, session=_Session(country="BR"), access_token="token", now=NOW,
    )
    assert result["receipt"]["outcome"] == "FAIL"
    assert "TARGET_MARKET_MISMATCH" in result["receipt"]["blocking_reasons"]


def test_identical_current_cells_cannot_hide_drift_from_g004_plan_projection():
    manifest, receipt, evidence = _g004()
    result = build_artifacts(
        request=_request(receipt, evidence), g004_manifest=manifest, g004_receipt=receipt,
        g004_evidence=evidence, session=_Session(daily_budget="3000"), access_token="token", now=NOW,
    )
    assert result["receipt"]["outcome"] == "FAIL"
    assert "G004_PLAN_PROJECTION_DRIFT" in result["receipt"]["blocking_reasons"]


def test_borrowed_g004_snapshot_is_rejected():
    manifest, receipt, evidence = _g004()
    request = _request(receipt, evidence)
    request["source_snapshot_sha256"] = "f" * 64
    with pytest.raises(G004AAudienceRiskError, match="G004A_G004_BINDING_MISMATCH"):
        build_artifacts(request=request, g004_manifest=manifest, g004_receipt=receipt, g004_evidence=evidence, session=_Session(), access_token="token", now=NOW)


def test_g004_plan_binding_and_expiry_are_hard_prerequisites():
    manifest, receipt, evidence = _g004()
    receipt["checks"]["plan_binding"]["status"] = "FAIL"
    receipt.pop("receipt_body_hash")
    receipt["receipt_body_hash"] = hash_json(receipt)
    manifest["receipt_sha256"] = _sha_line(receipt)
    with pytest.raises(G004AAudienceRiskError, match="G004A_G004_ARTIFACT_INVALID"):
        build_artifacts(request=_request(receipt, evidence), g004_manifest=manifest, g004_receipt=receipt, g004_evidence=evidence, session=_Session(), access_token="token", now=NOW)


def test_derived_receipt_never_outlives_parent_g004_receipt():
    manifest, receipt, evidence = _g004()
    receipt["expires_at"] = "2026-08-07T12:02:00+00:00"
    receipt.pop("receipt_body_hash")
    receipt["receipt_body_hash"] = hash_json(receipt)
    manifest["receipt_sha256"] = _sha_line(receipt)
    result = build_artifacts(
        request=_request(receipt, evidence), g004_manifest=manifest, g004_receipt=receipt,
        g004_evidence=evidence, session=_Session(), access_token="token", now=NOW,
    )
    assert result["receipt"]["expires_at"] == receipt["expires_at"]

    manifest, receipt, evidence = _g004()
    receipt["expires_at"] = "2026-08-07T11:59:59+00:00"
    receipt.pop("receipt_body_hash")
    receipt["receipt_body_hash"] = hash_json(receipt)
    manifest["receipt_sha256"] = _sha_line(receipt)
    with pytest.raises(G004AAudienceRiskError, match="G004A_G004_RECEIPT_EXPIRED"):
        build_artifacts(request=_request(receipt, evidence), g004_manifest=manifest, g004_receipt=receipt, g004_evidence=evidence, session=_Session(), access_token="token", now=NOW)


def test_adset_drift_during_get_only_audit_is_incomplete():
    manifest, receipt, evidence = _g004()
    result = build_artifacts(
        request=_request(receipt, evidence), g004_manifest=manifest, g004_receipt=receipt,
        g004_evidence=evidence, session=_Session(during_run_drift=True), access_token="token", now=NOW,
    )
    assert result["receipt"]["outcome"] == "INCOMPLETE"
    assert "OBJECT_DRIFT_DURING_AUDIT" in result["receipt"]["blocking_reasons"]


def test_topology_drift_and_missing_estimate_ready_fail_closed():
    manifest, receipt, evidence = _g004()
    drifted = build_artifacts(
        request=_request(receipt, evidence), g004_manifest=manifest, g004_receipt=receipt,
        g004_evidence=evidence, session=_Session(topology_drift=True), access_token="token", now=NOW,
    )
    assert "OBJECT_DRIFT_DURING_AUDIT" in drifted["receipt"]["blocking_reasons"]
    unavailable = build_artifacts(
        request=_request(receipt, evidence), g004_manifest=manifest, g004_receipt=receipt,
        g004_evidence=evidence, session=_Session(estimate_ready="missing"), access_token="token", now=NOW,
    )
    assert "DELIVERY_ESTIMATE_UNAVAILABLE" in unavailable["receipt"]["blocking_reasons"]


def test_stable_cell_semantic_drift_from_parent_g004_is_rejected():
    manifest, receipt, evidence = _g004()
    result = build_artifacts(
        request=_request(receipt, evidence), g004_manifest=manifest, g004_receipt=receipt,
        g004_evidence=evidence, session=_Session(ad_entities_count=2), access_token="token", now=NOW,
    )
    assert "CELL_ADSET_BINDING_MISMATCH" in result["receipt"]["blocking_reasons"]
