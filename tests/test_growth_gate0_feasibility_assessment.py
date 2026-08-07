from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.growth.common import canonical_json
from app.growth.gate0_feasibility_assessment import (
    G005ContractError,
    INPUT_VERSION,
    assess_gate0,
    hash_json,
)
from scripts.assess_gle_gate0_feasibility import _collect_observations
from scripts.assess_gle_gate0_feasibility import _validate_transport_release


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 64


def _sha_line(value: dict) -> str:
    return hashlib.sha256((canonical_json(value) + "\n").encode()).hexdigest()


def _governance(*, allowed: bool = True) -> dict:
    return {
        "baseline": "FINAL_EXECUTION_PLAN_v1.1",
        "contract_version": "gle-phase1-governance-v1",
        "global_enabled": False,
        "mode": "OFF",
        "golden_path": {"experiment_type": "COPY_ONLY", "unique_variable": "PRIMARY_TEXT"},
        "canary": {
            "account_ids": ["1012060198097836"] if allowed else [],
            "markets": ["MX"] if allowed else [],
        },
        "action_allowlist": ["CREATE_CANARY_PAUSED", "ACTIVATE_CANARY"] if allowed else [],
        "gates": {f"gate_{index}": {"status": "NOT_STARTED", "receipt_hash": None} for index in range(4)},
        "canonical_versions": {
            key: {"version": "UNFROZEN", "hash": None}
            for key in ("schema", "evaluator", "policy", "dataset")
        },
        "kill_switches": {
            "block_all_actions": True, "block_all_meta_writes": True,
            "block_account_writes": True, "block_action_writes": True,
            "disable_evaluation_scheduler": True,
            "block_new_experiment_activation": True,
            "force_manual_review_for_uncertain_post": True,
        },
        "owners": {
            key: {"name": "UNASSIGNED", "signed_at": None, "signature_hash": None}
            for key in ("gate_owner", "business_signer", "technical_signer", "data_signer")
        },
    }


def _subject() -> dict:
    return {
        "ad_account_id": "1012060198097836", "market": "MX", "study_id": "study-1",
        "cells": [
            {"cell_id": "C1", "experiment_id": "experiment-1", "study_cell_id": "sc-1", "campaign_id": "campaign-1", "adset_id": "adset-1", "ad_id": "ad-1", "target_share": 0.5},
            {"cell_id": "C2", "experiment_id": "experiment-2", "study_cell_id": "sc-2", "campaign_id": "campaign-1", "adset_id": "adset-2", "ad_id": "ad-2", "target_share": 0.5},
        ],
    }


def _policy(*, golden: bool = False) -> dict:
    return {
        "policy_version": "gle-g0-05-mx-policy-v1",
        "qualification_version": "tugaofunnel-guild-join-success-v1",
        "source_contract": "tugao_funnel_daily_metrics_api_v1",
        "source_metric": "guild_join_success_users",
        "minimum_attribution_coverage": 0.8,
        "maximum_allocation_deviation": 0.1,
        "minimum_total_impressions": 1000,
        "minimum_total_spend_usd": 5,
        "minimum_complete_days": 3,
        "reporting_settlement_hours": 48,
        "source_freshness_hours": 6,
        "baseline_window_days": 14,
        "alpha_two_sided": 0.05,
        "desired_power": 0.8,
        "mde_relative": 0.3,
        "maximum_test_days": 14,
        "maximum_test_budget_usd": 20,
        "maximum_daily_budget_usd": 2,
        "expected_daily_spend_usd": 1.428571,
        "estimator_version": "gle-two-sample-poisson-rate-obf-v1",
        "golden_vectors_approved": golden,
        "governance_model": "SOLE_OWNER",
        "sole_owner": "Chauncey",
    }


def _capability() -> tuple[dict, dict, dict]:
    graph = {
        "study_cells": {
            "data": [
                {"id": "sc-1", "ad_ids": ["ad-1"]},
                {"id": "sc-2", "ad_ids": ["ad-2"]},
            ],
            "pagination_complete": True, "page_count": 1,
        },
        "cell_C1_adsets": {
            "data": [{"id": "adset-1"}], "pagination_complete": True, "page_count": 1,
        },
        "cell_C2_adsets": {
            "data": [{"id": "adset-2"}], "pagination_complete": True, "page_count": 1,
        },
        "first_adset_C1": {"id": "adset-1"},
        "first_adset_C2": {"id": "adset-2"},
        "first_ad_C1": {"id": "ad-1", "adset_id": "adset-1"},
        "first_ad_C2": {"id": "ad-2", "adset_id": "adset-2"},
    }
    evidence = {
        "schema_version": "gle-g0-04-redacted-evidence-bundle-v1",
        "request_hash": "b" * 64,
        "source_snapshot_sha256": SHA,
        "local_evidence_hash": "d" * 64,
        "target": {"ad_account_id": "1012060198097836", "market": "MX", "study_id": "study-1", "campaign_id": "campaign-1"},
        "graph": graph, "transport_journal": [],
    }
    evidence["evidence_bundle_hash"] = hash_json(evidence)
    receipt = {
        "schema_version": "gle-g0-04-audit-receipt-v1",
        "source_snapshot_sha256": SHA,
        "expires_at": "2026-08-08T00:00:00+00:00",
        "target": {"ad_account_id": "1012060198097836", "market": "MX", "study_id": "study-1", "campaign_id": "campaign-1"},
        "blocking_reasons": [], "outcome": "PASS",
        "gate0_fragment": "PERMISSION_TOPOLOGY_PROVEN", "not_gate_receipt": True,
        "gate0_result_ceiling": "QUASI_ONLY", "attestation_status": "PENDING_ATTESTATION",
        "checks": {
            key: {"status": "PASS", "reason_codes": [], "evidence_refs": []}
            for key in (
                "graph_completeness", "plan_binding", "token_permission",
                "business_ownership", "capability_semantics", "topology",
                "activation_provenance", "freshness", "zero_write",
            )
        },
        "graph_evidence_hash": "7" * 64,
        "evidence_bundle_hash": evidence["evidence_bundle_hash"],
    }
    receipt["receipt_body_hash"] = hash_json(receipt)
    manifest = {
        "schema_version": "gle-g0-04-artifact-manifest-v1",
        "receipt_file": "receipt.json", "receipt_sha256": _sha_line(receipt),
        "evidence_file": "evidence.json", "evidence_sha256": _sha_line(evidence),
        "committed": True,
    }
    return manifest, receipt, evidence


def _attribution() -> tuple[dict, dict]:
    input_contract = {
        "account_id": "1012060198097836", "market": "MX",
        "experiment_ids": ["experiment-1", "experiment-2"],
        "window_start": "2026-07-24T00:00:00+00:00",
        "window_end": "2026-08-07T00:00:00+00:00",
        "project": "TUGAO", "max_events": 10000,
        "source_snapshot_sha256": SHA,
    }
    report = {
        "schema_version": "gle-g0-01-exact-id-attribution-audit-v1",
        "status": "BLOCKED",
        "blocking_reasons": ["QUALIFICATION_RULE_UNFROZEN", "READBACK_PROVENANCE_UNAUDITED"],
        "input_contract_hash": hash_json(input_contract),
        "source_snapshot_sha256": SHA,
        "source_schema_hash": "4" * 64,
        "versions": {
            "attribution": "gle-exact-id-attribution-v1",
            "dedupe": "gle-canonical-identity-dedupe-v1",
            "qualification_rule": "UNFROZEN",
        },
        "counts": {
            "candidate_event_count": 100, "exact_meta_event_count": 90,
            "exact_identity_event_count": 80, "deduped_canonical_identity_count": 80,
        },
        "coverage": {"exact_meta": 0.9, "exact_identity": 0.8888888889},
        "reason_counts": {}, "missing_reason_counts": {}, "ambiguous_reason_counts": {},
        "crm_verification_latency_seconds": {
            "count": 80, "p50": 60, "p90": 120, "p95": 180, "max": 300,
        },
        "row_evidence_hash": "5" * 64,
    }
    report["report_hash"] = hash_json(report)
    return input_contract, report


def _bundle(*, golden: bool = False, allowed: bool = True) -> dict:
    manifest, receipt, evidence = _capability()
    attr_input, attr_report = _attribution()
    transport = {
        "schema_version": "gle-g0-02b-qualified-transport-deployment-v1",
        "source_commit": "c2bdc06bb4926bb22de573e7967d4f4f5effa719",
        "release_id": "gle-g0-02b-test-release",
        "manifest_sha256": "5" * 64, "receipt_sha256": "6" * 64,
        "deployed_artifact_sha256": "7" * 64, "backend_invocation_id": "invocation-2",
        "receipt_status": "passed",
        "deployed_at": "2026-07-31T00:00:00+00:00",
        "natural_evidence_not_before_date": "2026-08-01",
    }
    transport["evidence_hash"] = hash_json(transport)
    experiment_binding = {
        "source_snapshot_sha256": SHA,
        "bindings": [
            {"experiment_id": "experiment-1", "study_id": "study-1", "study_cell_id": "sc-1", "campaign_id": "campaign-1", "adset_id": "adset-1", "ad_id": "ad-1", "readback_verified": True},
            {"experiment_id": "experiment-2", "study_id": "study-1", "study_cell_id": "sc-2", "campaign_id": "campaign-1", "adset_id": "adset-2", "ad_id": "ad-2", "readback_verified": True},
        ],
    }
    experiment_binding["evidence_hash"] = hash_json(experiment_binding)
    return {
        "schema_version": INPUT_VERSION,
        "assessment_id": "g005-test-1",
        "requested_at": "2026-08-07T11:00:00+00:00",
        "data_cutoff_at": "2026-08-07T10:00:00+00:00",
        "qualified_transport_evidence": transport,
        "subject": _subject(), "policy": _policy(golden=golden),
        "source_snapshot_sha256": SHA,
        "capability_manifest": manifest, "capability_receipt": receipt,
        "capability_evidence": evidence,
        "attribution_input_contract": attr_input, "attribution_report": attr_report,
        "experiment_binding_observation": experiment_binding,
        "allocation_observation": {
            "window_start": "2026-08-01T00:00:00+00:00", "window_end": "2026-08-04T00:00:00+00:00",
            "settled": True, "pagination_complete": True, "source_freshness_hours": 1,
            "complete_days": 3,
            "rows": [
                {"date": "2026-08-01", "cell_id": "C1", "ad_id": "ad-1", "impressions": 500, "spend_usd": 5},
                {"date": "2026-08-01", "cell_id": "C2", "ad_id": "ad-2", "impressions": 500, "spend_usd": 5},
            ],
            "evidence_hash": "e" * 64,
        },
        "qualified_join_observation": {
            "source_contract": "tugao_funnel_daily_metrics_api_v1",
            "source_metric": "guild_join_success_users",
            "qualification_version": "tugaofunnel-guild-join-success-v1",
            "window_start": "2026-08-01T00:00:00+00:00",
            "window_end": "2026-08-04T00:00:00+00:00",
            "complete": True, "source_freshness_hours": 1,
            "eligible_qualified_joins": 105,
            "exact_attributed_qualified_joins": 105,
            "cells": [
                {"cell_id": "C1", "ad_id": "ad-1", "qualified_joins": 50},
                {"cell_id": "C2", "ad_id": "ad-2", "qualified_joins": 55},
            ],
            "evidence_hash": "f" * 64,
        },
        "baseline_observation": {
            "window_start": "2026-07-24T00:00:00+00:00", "window_end": "2026-08-07T00:00:00+00:00",
            "complete_days": 14, "total_impressions": 100000, "qualified_joins": 5000,
            "total_spend_usd": 100, "attribution_coverage": 1,
            "event_attribution_coverage": 1, "exposure_identity_coverage": 1,
            "source_freshness_hours": 1, "evidence_hash": "1" * 64,
        },
        "governance_contract": _governance(allowed=allowed),
    }


def test_complete_available_inputs_still_build_only_unsigned_quasi_candidate():
    candidate = assess_gate0(_bundle(), now=NOW)
    assert candidate["technical_candidate_result"] == "QUASI_ONLY"
    assert candidate["gate0_result_ceiling"] == "QUASI_ONLY"
    assert candidate["not_gate_receipt"] is True
    assert candidate["attestation_status"] == "PENDING"
    assert "receipt_hash" not in candidate
    assert "POWER_GOLDEN_VECTORS_UNAPPROVED" in candidate["blocking_reasons"]
    assert "AUDIENCE_OVERLAP_UNKNOWN" in candidate["blocking_reasons"]
    unsigned = dict(candidate)
    digest = unsigned.pop("candidate_body_hash")
    assert hash_json(unsigned) == digest


def test_configured_fifty_fifty_never_substitutes_for_empty_actual():
    raw = _bundle()
    raw["allocation_observation"]["rows"] = []
    candidate = assess_gate0(raw, now=NOW)
    assert candidate["technical_candidate_result"] == "QUASI_ONLY"
    assert "ACTUAL_ALLOCATION_UNKNOWN" in candidate["blocking_reasons"]
    assert candidate["allocation_assessment"]["cells"][0]["impression_share"] is None


def test_polluted_study_is_not_feasible_and_cannot_be_signed_here():
    raw = _bundle()
    raw["capability_receipt"]["outcome"] = "POLLUTED"
    raw["capability_receipt"]["gate0_fragment"] = "INELIGIBLE"
    raw["capability_receipt"]["blocking_reasons"] = ["EXTERNAL_ACTIVATION_DETECTED"]
    raw["capability_receipt"].pop("receipt_body_hash")
    raw["capability_receipt"]["receipt_body_hash"] = hash_json(raw["capability_receipt"])
    raw["capability_manifest"]["receipt_sha256"] = _sha_line(raw["capability_receipt"])
    candidate = assess_gate0(raw, now=NOW)
    assert candidate["technical_candidate_result"] == "NOT_FEASIBLE"
    assert "EXTERNAL_ACTIVATION_CONTAMINATION" in candidate["blocking_reasons"]


def test_golden_vectors_unapproved_blocks_power_without_declaring_study_not_feasible():
    candidate = assess_gate0(_bundle(), now=NOW)
    assert candidate["power_assessment"]["feasible"] is False
    assert "POWER_GOLDEN_VECTORS_UNAPPROVED" in candidate["blocking_reasons"]
    assert candidate["checks"]["power"]["status"] == "UNKNOWN"
    assert candidate["technical_candidate_result"] == "QUASI_ONLY"


def test_allowlist_and_audience_unknown_are_explicit_blockers():
    raw = _bundle(allowed=False)
    candidate = assess_gate0(raw, now=NOW)
    assert {"CANARY_ACCOUNT_NOT_ALLOWLISTED", "CANARY_MARKET_NOT_ALLOWLISTED", "CANARY_ACTION_NOT_ALLOWLISTED", "AUDIENCE_OVERLAP_UNKNOWN"}.issubset(candidate["blocking_reasons"])


def test_impression_balance_does_not_hide_spend_skew():
    raw = _bundle()
    raw["allocation_observation"]["rows"][0]["spend_usd"] = 9
    raw["allocation_observation"]["rows"][1]["spend_usd"] = 1
    candidate = assess_gate0(raw, now=NOW)
    assert "ALLOCATION_SPEND_DEVIATION_EXCEEDED" in candidate["blocking_reasons"]
    assert candidate["checks"]["actual_allocation"]["status"] == "FAIL"
    assert candidate["technical_candidate_result"] == "NOT_FEASIBLE"


def test_zero_qualified_denominator_is_unknown_not_zero_percent():
    raw = _bundle()
    raw["qualified_join_observation"]["eligible_qualified_joins"] = 0
    raw["qualified_join_observation"]["exact_attributed_qualified_joins"] = 0
    raw["qualified_join_observation"]["cells"][0]["qualified_joins"] = 0
    raw["qualified_join_observation"]["cells"][1]["qualified_joins"] = 0
    candidate = assess_gate0(raw, now=NOW)
    assert candidate["qualified_join_assessment"]["attribution_coverage"] is None
    assert "ATTRIBUTION_COVERAGE_UNKNOWN" in candidate["blocking_reasons"]


def test_capability_manifest_or_receipt_tamper_is_rejected():
    raw = _bundle()
    raw["capability_receipt"]["outcome"] = "POLLUTED"
    with pytest.raises(G005ContractError, match="G005_CAPABILITY_MANIFEST_HASH_MISMATCH"):
        assess_gate0(raw, now=NOW)


def test_cross_account_attribution_report_is_rejected():
    raw = _bundle()
    raw["attribution_input_contract"]["account_id"] = "other"
    with pytest.raises(G005ContractError, match="G005_ATTRIBUTION_SUBJECT_MISMATCH"):
        assess_gate0(raw, now=NOW)
    raw = _bundle()
    raw["subject"]["cells"][0]["experiment_id"] = "borrowed-1"
    raw["subject"]["cells"][1]["experiment_id"] = "borrowed-2"
    raw["attribution_input_contract"]["experiment_ids"] = ["borrowed-1", "borrowed-2"]
    raw["attribution_report"]["input_contract_hash"] = hash_json(raw["attribution_input_contract"])
    raw["attribution_report"].pop("report_hash")
    raw["attribution_report"]["report_hash"] = hash_json(raw["attribution_report"])
    with pytest.raises(G005ContractError, match="G005_EXPERIMENT_BINDING_MISMATCH"):
        assess_gate0(raw, now=NOW)


def test_extra_caller_result_or_attestation_field_is_rejected():
    raw = _bundle()
    raw["feasible"] = True
    with pytest.raises(G005ContractError, match="G005_INPUT_SCHEMA_INVALID"):
        assess_gate0(raw, now=NOW)


def test_frozen_subject_allocation_and_policy_cannot_be_relaxed_by_caller():
    raw = _bundle()
    raw["subject"]["cells"][0]["target_share"] = 0.9
    raw["subject"]["cells"][1]["target_share"] = 0.1
    with pytest.raises(G005ContractError, match="G005_CELL_SET_INVALID"):
        assess_gate0(raw, now=NOW)
    raw = _bundle()
    raw["policy"]["minimum_total_impressions"] = 1
    with pytest.raises(G005ContractError, match="G005_FROZEN_POLICY_MISMATCH"):
        assess_gate0(raw, now=NOW)


def test_capability_expiry_is_checked_against_requested_time_not_old_cutoff():
    raw = _bundle()
    raw["requested_at"] = "2026-08-09T00:00:00+00:00"
    candidate = assess_gate0(raw, now=datetime(2026, 8, 9, 1, tzinfo=timezone.utc))
    assert "CAPABILITY_RECEIPT_EXPIRED" in candidate["blocking_reasons"]


def test_attribution_window_must_match_power_baseline():
    raw = _bundle()
    raw["attribution_input_contract"]["window_start"] = "2026-07-25T00:00:00+00:00"
    raw["attribution_report"]["input_contract_hash"] = hash_json(raw["attribution_input_contract"])
    raw["attribution_report"].pop("report_hash")
    raw["attribution_report"]["report_hash"] = hash_json(raw["attribution_report"])
    with pytest.raises(G005ContractError, match="G005_ATTRIBUTION_WINDOW_MISMATCH"):
        assess_gate0(raw, now=NOW)


def test_transport_evidence_must_be_passed_and_natural_window_cannot_precede_it():
    raw = _bundle()
    raw["qualified_transport_evidence"]["receipt_status"] = "failed"
    raw["qualified_transport_evidence"].pop("evidence_hash")
    raw["qualified_transport_evidence"]["evidence_hash"] = hash_json(
        raw["qualified_transport_evidence"],
    )
    with pytest.raises(G005ContractError, match="G005_TRANSPORT_EVIDENCE_INVALID"):
        assess_gate0(raw, now=NOW)
    raw = _bundle()
    raw["qualified_transport_evidence"]["natural_evidence_not_before_date"] = "2026-08-02"
    raw["qualified_transport_evidence"].pop("evidence_hash")
    raw["qualified_transport_evidence"]["evidence_hash"] = hash_json(
        raw["qualified_transport_evidence"],
    )
    with pytest.raises(G005ContractError, match="G005_NATURAL_EVIDENCE_WINDOW_INVALID"):
        assess_gate0(raw, now=NOW)


def test_capability_evidence_and_receipt_targets_must_match_exactly():
    raw = _bundle()
    raw["capability_evidence"]["target"]["campaign_id"] = "borrowed-campaign"
    raw["capability_evidence"].pop("evidence_bundle_hash")
    raw["capability_evidence"]["evidence_bundle_hash"] = hash_json(
        raw["capability_evidence"],
    )
    raw["capability_receipt"]["evidence_bundle_hash"] = raw["capability_evidence"]["evidence_bundle_hash"]
    raw["capability_receipt"].pop("receipt_body_hash")
    raw["capability_receipt"]["receipt_body_hash"] = hash_json(raw["capability_receipt"])
    raw["capability_manifest"]["evidence_sha256"] = _sha_line(raw["capability_evidence"])
    raw["capability_manifest"]["receipt_sha256"] = _sha_line(raw["capability_receipt"])
    with pytest.raises(G005ContractError, match="G005_CAPABILITY_SUBJECT_MISMATCH"):
        assess_gate0(raw, now=NOW)


def test_capability_cell_topology_and_g001_experiments_cannot_be_borrowed():
    raw = _bundle()
    raw["subject"]["cells"][0]["ad_id"] = "borrowed-ad"
    raw["allocation_observation"]["rows"][0]["ad_id"] = "borrowed-ad"
    raw["qualified_join_observation"]["cells"][0]["ad_id"] = "borrowed-ad"
    with pytest.raises(G005ContractError, match="G005_CAPABILITY_TOPOLOGY_MISMATCH"):
        assess_gate0(raw, now=NOW)
    raw = _bundle()
    raw["attribution_input_contract"]["experiment_ids"] = ["borrowed-1", "borrowed-2"]
    raw["attribution_report"]["input_contract_hash"] = hash_json(raw["attribution_input_contract"])
    raw["attribution_report"].pop("report_hash")
    raw["attribution_report"]["report_hash"] = hash_json(raw["attribution_report"])
    with pytest.raises(G005ContractError, match="G005_ATTRIBUTION_SUBJECT_MISMATCH"):
        assess_gate0(raw, now=NOW)


def test_e00_must_remain_off_and_kill_switched_before_gate0():
    raw = _bundle()
    raw["governance_contract"]["global_enabled"] = True
    raw["governance_contract"]["mode"] = "BOUNDED_EXECUTION"
    for key in raw["governance_contract"]["kill_switches"]:
        raw["governance_contract"]["kill_switches"][key] = False
    candidate = assess_gate0(raw, now=NOW)
    assert "E00_NOT_FAIL_CLOSED" in candidate["blocking_reasons"]
    assert candidate["checks"]["governance_allowlist"]["status"] == "INCOMPLETE"
    raw = _bundle()
    raw["policy"]["golden_vectors_approved"] = True
    with pytest.raises(G005ContractError, match="G005_FROZEN_POLICY_MISMATCH"):
        assess_gate0(raw, now=NOW)
    raw = _bundle()
    raw["sole_owner_attestation"] = {"principal": "Chauncey"}
    with pytest.raises(G005ContractError, match="G005_INPUT_SCHEMA_INVALID"):
        assess_gate0(raw, now=NOW)


def test_candidate_is_deterministic_under_key_reordering():
    first = assess_gate0(_bundle(), now=NOW)
    reordered = dict(reversed(list(_bundle().items())))
    second = assess_gate0(reordered, now=NOW)
    assert first == second


def _snapshot(path) -> str:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE ad_dashboard_fact_rows(
          date TEXT,data_source TEXT,platform TEXT,account_id TEXT,country TEXT,
          media_source TEXT,campaign_id TEXT,adset_id TEXT,ad_id TEXT,impressions REAL,cost REAL,
          tugao_join_success_users REAL,payload_json TEXT,updated_at TEXT
        );
        CREATE TABLE ad_dashboard_sync_state(source TEXT,date TEXT,status TEXT);
        CREATE TABLE ad_experiment(
          experiment_id TEXT,account_id TEXT,country TEXT,source_campaign_id TEXT,
          source_adset_id TEXT,source_ad_id TEXT,control_definition_json TEXT
        );
        """
    )
    payload = json.dumps({
        "qualified_join_metric_observed": True,
        "qualified_join_exact_attribution": True,
        "qualified_join_attribution_status": "exact",
        "qualified_join_source_field": "guild_join_success_users",
        "source_metric_contract": "tugao_funnel_daily_metrics_api_v1",
        "external_app": "tugao-mx",
    })
    for index in (1, 2):
        conn.execute(
            "INSERT INTO ad_experiment VALUES(?,?,?,?,?,?,?)",
            (f"experiment-{index}", "1012060198097836", "MX", "campaign-1", f"adset-{index}", f"ad-{index}",
             json.dumps({"meta_randomization": {"study_id": "study-1", "study_cell_id": f"sc-{index}", "readback_verified": True}})),
        )
    for day_number in range(1, 15):
        day = f"2026-07-{day_number + 17:02d}"
        for index in (1, 2):
            conn.execute(
                "INSERT INTO ad_dashboard_fact_rows VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (day, "Meta", "Meta", "act_1012060198097836", "Mexico", "facebook", "campaign-1",
                 f"adset-{index}", f"ad-{index}", 100, 1, 0, "{}", "2026-08-07T09:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO ad_dashboard_fact_rows VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (day, "TugaoFunnel", "Meta", "", "Mexico", "facebook", "campaign-1",
                 f"adset-{index}", f"ad-{index}", 0, 0, 2, payload, "2026-08-07T09:00:00+00:00"),
            )
        conn.execute("INSERT INTO ad_dashboard_sync_state VALUES('all',?,'ok')", (day,))
        conn.execute("INSERT INTO ad_dashboard_sync_state VALUES('tugao_funnel',?,'ok')", (day,))
    # Large legacy total without the observation marker must never become qualified evidence.
    conn.execute(
        "INSERT INTO ad_dashboard_fact_rows VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-07-20", "TugaoFunnel", "Meta", "", "Mexico", "facebook", "campaign-1",
         "adset-1", "ad-1", 0, 0, 999, json.dumps({"guild_joins": 999}), "2026-08-07T09:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_snapshot_collector_derives_metrics_from_exact_rows_and_never_legacy_total(tmp_path):
    database = tmp_path / "snapshot.db"
    digest = _snapshot(database)
    request = {
        "data_cutoff_at": "2026-08-07T10:00:00+00:00",
        "natural_evidence_not_before_date": "2026-07-29",
        "subject": _subject(), "policy": _policy(),
        "windows": {
            "allocation_start": "2026-07-29", "allocation_end": "2026-07-31",
            "baseline_start": "2026-07-18", "baseline_end": "2026-07-31",
        },
    }
    allocation, qualified, baseline, experiment_binding = _collect_observations(database, request, digest)
    assert allocation["complete_days"] == 3
    assert sum(row["impressions"] for row in allocation["rows"]) == 600
    assert qualified["eligible_qualified_joins"] == 12
    assert qualified["exact_attributed_qualified_joins"] == 12
    assert sum(row["qualified_joins"] for row in qualified["cells"]) == 12
    assert baseline["total_impressions"] == 2800
    assert baseline["qualified_joins"] == 56
    assert {item["experiment_id"] for item in experiment_binding["bindings"]} == {
        "experiment-1", "experiment-2",
    }
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest


def test_snapshot_collector_rejects_wal_sidecar(tmp_path):
    database = tmp_path / "snapshot.db"
    digest = _snapshot(database)
    (tmp_path / "snapshot.db-wal").write_bytes(b"not-checkpointed")
    with pytest.raises(G005ContractError, match="G005_SOURCE_SIDECAR_PRESENT"):
        _collect_observations(
            database,
            {
                "data_cutoff_at": "2026-08-07T10:00:00+00:00",
                "natural_evidence_not_before_date": "2026-07-29",
                "subject": _subject(), "policy": _policy(),
                "windows": {
                    "allocation_start": "2026-07-29", "allocation_end": "2026-07-31",
                    "baseline_start": "2026-07-18", "baseline_end": "2026-07-31",
                },
            },
            digest,
        )


def test_snapshot_collector_rejects_source_hash_mismatch(tmp_path):
    database = tmp_path / "snapshot.db"
    _snapshot(database)
    with pytest.raises(G005ContractError, match="G005_SOURCE_HASH_MISMATCH"):
        _collect_observations(
            database,
            {
                "data_cutoff_at": "2026-08-07T10:00:00+00:00",
                "natural_evidence_not_before_date": "2026-07-29",
                "subject": _subject(), "policy": _policy(),
                "windows": {
                    "allocation_start": "2026-07-29", "allocation_end": "2026-07-31",
                    "baseline_start": "2026-07-18", "baseline_end": "2026-07-31",
                },
            },
            "0" * 64,
        )


def test_missing_exact_cell_day_is_incomplete_not_zero(tmp_path):
    database = tmp_path / "snapshot.db"
    _snapshot(database)
    conn = sqlite3.connect(database)
    conn.execute(
        "DELETE FROM ad_dashboard_fact_rows WHERE date='2026-07-30' AND ad_id='ad-2'",
    )
    conn.commit()
    conn.close()
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    allocation, qualified, _, _ = _collect_observations(
        database,
        {
            "data_cutoff_at": "2026-08-07T10:00:00+00:00",
            "natural_evidence_not_before_date": "2026-07-29",
            "subject": _subject(), "policy": _policy(),
            "windows": {
                "allocation_start": "2026-07-29", "allocation_end": "2026-07-31",
                "baseline_start": "2026-07-18", "baseline_end": "2026-07-31",
            },
        },
        digest,
    )
    assert allocation["pagination_complete"] is False
    assert qualified["complete"] is False


def test_nonfinite_or_fractional_source_metrics_fail_closed(tmp_path):
    database = tmp_path / "snapshot.db"
    _snapshot(database)
    conn = sqlite3.connect(database)
    conn.execute(
        "UPDATE ad_dashboard_fact_rows SET tugao_join_success_users=1.5 "
        "WHERE data_source='TugaoFunnel' AND date='2026-07-29' AND ad_id='ad-1'",
    )
    conn.commit()
    conn.close()
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(G005ContractError, match="G005_QUALIFIED_JOIN_INVALID"):
        _collect_observations(
            database,
            {
                "data_cutoff_at": "2026-08-07T10:00:00+00:00",
                "natural_evidence_not_before_date": "2026-07-29",
                "subject": _subject(), "policy": _policy(),
                "windows": {
                    "allocation_start": "2026-07-29", "allocation_end": "2026-07-31",
                    "baseline_start": "2026-07-18", "baseline_end": "2026-07-31",
                },
            },
            digest,
        )


def test_future_subject_timestamp_cannot_make_sources_look_fresh(tmp_path):
    database = tmp_path / "snapshot.db"
    _snapshot(database)
    conn = sqlite3.connect(database)
    conn.execute(
        "UPDATE ad_dashboard_fact_rows SET updated_at='2030-01-01T00:00:00+00:00' "
        "WHERE date='2026-07-31' AND ad_id='ad-1'",
    )
    conn.commit()
    conn.close()
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(G005ContractError, match="G005_SOURCE_FUTURE_TIMESTAMP"):
        _collect_observations(
            database,
            {
                "data_cutoff_at": "2026-08-07T10:00:00+00:00",
                "natural_evidence_not_before_date": "2026-07-29",
                "subject": _subject(), "policy": _policy(),
                "windows": {
                    "allocation_start": "2026-07-29", "allocation_end": "2026-07-31",
                    "baseline_start": "2026-07-18", "baseline_end": "2026-07-31",
                },
            },
            digest,
        )


def test_legacy_tugao_row_cannot_launder_qualified_source_freshness(tmp_path):
    database = tmp_path / "snapshot.db"
    _snapshot(database)
    conn = sqlite3.connect(database)
    conn.execute(
        "UPDATE ad_dashboard_fact_rows SET updated_at='2026-08-06T00:00:00+00:00' "
        "WHERE data_source='TugaoFunnel' AND payload_json LIKE '%qualified_join_metric_observed%'",
    )
    conn.execute(
        "INSERT INTO ad_dashboard_fact_rows VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-07-31", "TugaoFunnel", "Meta", "", "Mexico", "facebook", "campaign-1",
         "legacy-adset", "legacy-ad", 0, 0, 999, json.dumps({"guild_joins": 999}),
         "2026-08-07T09:30:00+00:00"),
    )
    conn.commit()
    conn.close()
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    allocation, qualified, baseline, _ = _collect_observations(
        database,
        {
            "data_cutoff_at": "2026-08-07T10:00:00+00:00",
            "natural_evidence_not_before_date": "2026-07-29",
            "subject": _subject(), "policy": _policy(),
            "windows": {
                "allocation_start": "2026-07-29", "allocation_end": "2026-07-31",
                "baseline_start": "2026-07-18", "baseline_end": "2026-07-31",
            },
        },
        digest,
    )
    assert Decimal(allocation["source_freshness_hours"]) == Decimal("34")
    assert Decimal(qualified["source_freshness_hours"]) == Decimal("34")
    assert Decimal(baseline["source_freshness_hours"]) == Decimal("34")


def test_transport_evidence_must_match_actual_receipt_bytes(tmp_path):
    def governed(value):
        result = dict(value)
        result["integrity"] = {
            "algorithm": "sha256",
            "payload_sha256": hashlib.sha256(json.dumps(
                result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest(),
        }
        return result

    runtime_paths = (
        "app/tugao_funnel_api.py", "app/ad_dashboard_repository.py", "app/main_shared.py",
        "app/schema_migrations.py", "app/sqlite_write_queue.py",
        "scripts/backfill_ad_dashboard_fact_rows.py",
    )
    runtime = [{"path": path, "sha256": hashlib.sha256(path.encode()).hexdigest(), "size_bytes": 10,
                "mode": 0o644, "mtime_ns": 1}
               for path in runtime_paths]
    manifest_payload = {
        "schema_version": 1, "record_type": "mcn_release_manifest", "release_id": "gle-g0-02b-r1",
        "created_at_utc": "2026-07-31T00:00:00+00:00",
        "environment": {"host": "test", "user": "codex", "repository_root": "/opt/mcn-ai-automation"},
        "change_source": {"kind": "codex_task", "reference": "c2bdc06bb4926bb22de573e7967d4f4f5effa719", "base_revision": "production-baseline"},
        "plan_sha256": "1" * 64,
        "artifacts": {"files": runtime},
        "systemd": {"units": [{"name": "mcn-backend.service"}]},
        "databases": [{"name": "automation", "path": "/var/lib/mcn/automation.db"}],
        "backup": {"required": True, "status": "verified", "artifacts": [{"path": "backup", "sha256": "2" * 64}]},
        "verification": {"tests": [{"status": "passed"}], "smokes": [{"status": "passed"}]},
        "rollback": {"status": "ready", "strategy": "restore preimage"},
    }
    manifest_value = governed(manifest_payload)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(manifest_value))
    receipt_value = governed({
        "schema_version": 1, "record_type": "mcn_controlled_restart_receipt",
        "receipt_id": "gle-g0-02b-r1-1", "receipt_path": "/var/lib/receipts/r1.json",
        "release_id": "gle-g0-02b-r1", "status": "passed", "unit": "mcn-backend.service",
        "started_at_utc": "2026-07-30T23:59:00+00:00", "error": None,
        "finished_at_utc": "2026-07-31T00:00:00+00:00",
        "manifest": {"path": str(manifest), "payload_sha256": manifest_value["integrity"]["payload_sha256"]},
        "before": {"state": {"InvocationID": "invocation-1"}},
        "after": {"state": {"InvocationID": "invocation-2", "ActiveState": "active"}},
        "validation": {"ok": True, "phase": "restart", "release_id": "gle-g0-02b-r1"},
        "command": {"result": {"returncode": 0, "timed_out": False}},
        "smokes": [{"kind": "systemd", "target": "mcn-backend.service", "status": "passed"}],
    })
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(receipt_value))
    evidence = {
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        "deployed_artifact_sha256": hash_json([
            {"path": item["path"], "sha256": item["sha256"], "size_bytes": item["size_bytes"]}
            for item in runtime
        ]),
        "backend_invocation_id": "invocation-2", "deployed_at": "2026-07-31T00:00:00+00:00",
        "release_id": "gle-g0-02b-r1",
    }
    _validate_transport_release(manifest, receipt, evidence)
    forged = json.loads(receipt.read_text())
    forged.pop("command")
    forged.pop("integrity")
    forged = governed(forged)
    receipt.write_text(json.dumps(forged))
    evidence["receipt_sha256"] = hashlib.sha256(receipt.read_bytes()).hexdigest()
    with pytest.raises(G005ContractError, match="G005_TRANSPORT_RECEIPT_MISMATCH"):
        _validate_transport_release(manifest, receipt, evidence)
    receipt.write_text(json.dumps(receipt_value))
    evidence["receipt_sha256"] = hashlib.sha256(receipt.read_bytes()).hexdigest()
    evidence["release_id"] = "gle-g0-02b-borrowed"
    with pytest.raises(G005ContractError, match="G005_TRANSPORT_RECEIPT_MISMATCH"):
        _validate_transport_release(manifest, receipt, evidence)
