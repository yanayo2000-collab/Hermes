"""Pure, fail-closed GLE Gate 0 feasibility aggregation.

The module consumes immutable evidence objects supplied by an offline caller.
It performs no network, Meta SDK, SQLite, or production operations.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from app.growth.common import canonical_json
from app.growth.gate0_power_estimator import (
    ALTERNATIVE as POWER_ALTERNATIVE,
    APPROXIMATION as POWER_APPROXIMATION,
    ESTIMAND as POWER_ESTIMAND,
    ESTIMATOR_VERSION,
    INPUT_VERSION as POWER_INPUT_VERSION,
    OFFSET as POWER_OFFSET,
    assess_fixed_endpoint_power,
)
from app.growth.phase1_governance import validate_contract as validate_governance_contract


INPUT_VERSION = "gle-g0-05-assessment-input-v2"
ENGINE_VERSION = "gle-g0-05-feasibility-engine-v2"
CANDIDATE_VERSION = "gle-g0-05-gate0-candidate-v2"
POLICY_VERSION = "gle-g0-05-mx-policy-v2"
QUALIFICATION_VERSION = "tugaofunnel-guild-join-success-v1"
SOURCE_CONTRACT = "tugao_funnel_daily_metrics_api_v1"
SOURCE_METRIC = "guild_join_success_users"
G002B_SOURCE_COMMIT = "c2bdc06bb4926bb22de573e7967d4f4f5effa719"

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class G005ContractError(ValueError):
    """The offline aggregation input violates the frozen contract."""


def hash_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _exact_object(value: Any, keys: Sequence[str], code: str) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise G005ContractError(code)
    return dict(value)


def _text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise G005ContractError(code)
    return value


def _utc(value: Any, code: str) -> datetime:
    text = _text(value, code)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise G005ContractError(code) from exc
    if parsed.tzinfo is None:
        raise G005ContractError(code)
    return parsed.astimezone(timezone.utc)


def _decimal(value: Any, code: str, *, minimum: Optional[Decimal] = None) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise G005ContractError(code)
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise G005ContractError(code) from None
    if not number.is_finite() or (minimum is not None and number < minimum):
        raise G005ContractError(code)
    return number


def _integer(value: Any, code: str, *, minimum: int = 0) -> int:
    number = _decimal(value, code, minimum=Decimal(minimum))
    if number != number.to_integral_value():
        raise G005ContractError(code)
    return int(number)


def _ratio(value: Any, code: str, *, inclusive_zero: bool = True) -> Decimal:
    minimum = Decimal("0") if inclusive_zero else Decimal("0.000000000000000001")
    number = _decimal(value, code, minimum=minimum)
    if number > 1:
        raise G005ContractError(code)
    return number


def _check(status: str, reasons: Sequence[str], evidence: Sequence[str]) -> Dict[str, Any]:
    return {
        "status": status,
        "reason_codes": sorted(set(str(item) for item in reasons if item)),
        "evidence_refs": sorted(set(str(item) for item in evidence if item)),
    }


def _serialized_sha256(value: Any) -> str:
    return hashlib.sha256((canonical_json(value) + "\n").encode("utf-8")).hexdigest()


def _validate_capability_artifacts(
    manifest_raw: Any,
    receipt_raw: Any,
    evidence_raw: Any,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    manifest = _exact_object(
        manifest_raw,
        ("schema_version", "receipt_file", "receipt_sha256", "evidence_file", "evidence_sha256", "committed"),
        "G005_CAPABILITY_MANIFEST_INVALID",
    )
    if manifest["schema_version"] != "gle-g0-04-artifact-manifest-v1" or manifest["committed"] is not True:
        raise G005ContractError("G005_CAPABILITY_MANIFEST_INVALID")
    receipt = dict(receipt_raw) if isinstance(receipt_raw, dict) else {}
    evidence = dict(evidence_raw) if isinstance(evidence_raw, dict) else {}
    if (
        manifest["receipt_sha256"] != _serialized_sha256(receipt)
        or manifest["evidence_sha256"] != _serialized_sha256(evidence)
    ):
        raise G005ContractError("G005_CAPABILITY_MANIFEST_HASH_MISMATCH")
    evidence_hash = evidence.get("evidence_bundle_hash")
    unsigned_evidence = dict(evidence)
    unsigned_evidence.pop("evidence_bundle_hash", None)
    if not isinstance(evidence_hash, str) or hash_json(unsigned_evidence) != evidence_hash:
        raise G005ContractError("G005_CAPABILITY_EVIDENCE_HASH_MISMATCH")
    if receipt.get("evidence_bundle_hash") != evidence_hash:
        raise G005ContractError("G005_CAPABILITY_EVIDENCE_HASH_MISMATCH")
    if evidence.get("target") != receipt.get("target"):
        raise G005ContractError("G005_CAPABILITY_SUBJECT_MISMATCH")
    return receipt, evidence


def _validate_audience_artifacts(
    manifest_raw: Any,
    receipt_raw: Any,
    evidence_raw: Any,
    *,
    subject: Mapping[str, Any],
    source_snapshot_sha256: str,
    capability_receipt_hash: str,
    capability_evidence_hash: str,
    capability_expires_at: str,
    requested_at: datetime,
    assessment_clock: datetime,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    manifest = _exact_object(
        manifest_raw,
        ("schema_version", "receipt_file", "receipt_sha256", "evidence_file", "evidence_sha256", "committed"),
        "G005_AUDIENCE_MANIFEST_INVALID",
    )
    receipt = dict(receipt_raw) if isinstance(receipt_raw, dict) else {}
    evidence = dict(evidence_raw) if isinstance(evidence_raw, dict) else {}
    if (
        manifest["schema_version"] != "gle-g0-04a-artifact-manifest-v1"
        or manifest["committed"] is not True
        or manifest["receipt_sha256"] != _serialized_sha256(receipt)
        or manifest["evidence_sha256"] != _serialized_sha256(evidence)
    ):
        raise G005ContractError("G005_AUDIENCE_MANIFEST_INVALID")
    receipt_hash = str(receipt.get("receipt_body_hash") or "")
    evidence_hash = str(evidence.get("evidence_body_hash") or "")
    unsigned_receipt = dict(receipt)
    unsigned_receipt.pop("receipt_body_hash", None)
    unsigned_evidence = dict(evidence)
    unsigned_evidence.pop("evidence_body_hash", None)
    if (
        receipt.get("schema_version") != "gle-g0-04a-audience-risk-receipt-v1"
        or evidence.get("schema_version") != "gle-g0-04a-audience-risk-evidence-v1"
        or hash_json(unsigned_receipt) != receipt_hash
        or hash_json(unsigned_evidence) != evidence_hash
        or receipt.get("evidence_body_hash") != evidence_hash
        or receipt.get("subject") != evidence.get("subject")
    ):
        raise G005ContractError("G005_AUDIENCE_HASH_MISMATCH")
    expected_subject = {
        "ad_account_id": subject["ad_account_id"],
        "campaign_id": subject["cells"][0]["campaign_id"],
        "market": subject["market"],
        "study_id": subject["study_id"],
        "cells": [
            {
                "cell_id": cell["cell_id"],
                "study_cell_id": cell["study_cell_id"],
                "adset_id": cell["adset_id"],
                "ad_id": cell["ad_id"],
            }
            for cell in subject["cells"]
        ],
    }
    if (
        receipt.get("subject") != expected_subject
        or receipt.get("source_snapshot_sha256") != source_snapshot_sha256
        or evidence.get("source_snapshot_sha256") != source_snapshot_sha256
        or receipt.get("g004_receipt_body_hash") != capability_receipt_hash
        or evidence.get("g004_receipt_body_hash") != capability_receipt_hash
        or receipt.get("g004_evidence_bundle_hash") != capability_evidence_hash
        or evidence.get("g004_evidence_bundle_hash") != capability_evidence_hash
    ):
        raise G005ContractError("G005_AUDIENCE_SUBJECT_MISMATCH")
    try:
        expires = _utc(receipt.get("expires_at"), "G005_AUDIENCE_RECEIPT_EXPIRED")
    except G005ContractError:
        expires = datetime.min.replace(tzinfo=timezone.utc)
    reasons = list(receipt.get("blocking_reasons") or [])
    # G0-04A v1 proves configuration/topology only.  The consumer owns these
    # blockers; a self-hashed producer artifact cannot remove them.
    reasons.extend([
        "AUDIENCE_OVERLAP_UNKNOWN", "INTERNAL_AUCTION_CONTAMINATION_UNKNOWN",
    ])
    expected_checks = {
        "g004_binding", "topology", "targeting_equivalence", "delivery_estimate",
        "freshness", "split_test_topology", "zero_write",
    }
    checks = receipt.get("checks")
    proof = dict(evidence.get("transport_proof") or {})
    journal = evidence.get("transport_journal")
    estimates = evidence.get("delivery_estimates")
    projections = evidence.get("live_projection_hashes")
    try:
        checked_at = _utc(receipt.get("checked_at"), "G005_AUDIENCE_RECEIPT_EXPIRED")
    except G005ContractError:
        checked_at = datetime.max.replace(tzinfo=timezone.utc)
    expected_endpoints = [
        f"/v25.0/{subject['study_id']}", f"/v25.0/{subject['study_id']}/cells",
        *[
            endpoint
            for cell in subject["cells"]
            for endpoint in (
                f"/v25.0/{cell['study_cell_id']}/adsets",
                f"/v25.0/{cell['adset_id']}", f"/v25.0/{cell['ad_id']}",
            )
        ],
        *[f"/v25.0/act_{subject['ad_account_id']}/delivery_estimate"] * 3,
        *[
            endpoint
            for cell in subject["cells"]
            for endpoint in (
                f"/v25.0/{cell['adset_id']}", f"/v25.0/{cell['ad_id']}",
                f"/v25.0/{cell['study_cell_id']}/adsets",
            )
        ],
        f"/v25.0/{subject['study_id']}", f"/v25.0/{subject['study_id']}/cells",
    ]
    time_chain_valid = checked_at <= requested_at <= assessment_clock < expires
    valid_config_fragment = (
        receipt.get("engine_version") == "gle-g0-04a-audience-risk-audit-v1"
        and isinstance(checks, dict)
        and set(checks) == expected_checks
        and all(
            isinstance(checks.get(key), dict)
            and checks[key].get("status") == "PASS"
            and checks[key].get("reason_codes") == []
            for key in expected_checks
        )
        and evidence.get("configured_targeting_similarity") == "IDENTICAL"
        and evidence.get("inference_basis") == "CONFIGURATION_AND_SPLIT_TEST_TOPOLOGY_ONLY"
        and receipt.get("request_hash") == evidence.get("request_hash")
        and receipt.get("checked_at") == evidence.get("checked_at")
        and isinstance(estimates, dict)
        and set(estimates) == {"C1", "C2", "reference"}
        and all(
            isinstance(estimates[key], dict)
            and set(estimates[key]) == {"lower", "upper", "estimate_ready"}
            and estimates[key].get("estimate_ready") is True
            and isinstance(estimates[key].get("lower"), int)
            and isinstance(estimates[key].get("upper"), int)
            and not isinstance(estimates[key].get("lower"), bool)
            and estimates[key]["lower"] > 0
            and estimates[key]["upper"] >= estimates[key]["lower"]
            for key in ("C1", "C2", "reference")
        )
        and isinstance(projections, dict)
        and set(projections) == {"C1", "C2", "graph"}
        and all(isinstance(value, str) and bool(_SHA_RE.fullmatch(value)) for value in projections.values())
        and projections["C1"] == projections["C2"]
        and time_chain_valid
        and (expires - checked_at).total_seconds() <= 900
        and isinstance(proof.get("get_count"), int)
        and proof.get("get_count", 0) > 0
        and isinstance(proof.get("request_journal_hash"), str)
        and bool(_SHA_RE.fullmatch(proof["request_journal_hash"]))
        and isinstance(journal, list)
        and proof["get_count"] == len(journal)
        and proof["request_journal_hash"] == hash_json(journal)
        and sorted(str(item.get("endpoint") or "") for item in journal if isinstance(item, dict))
        == sorted(expected_endpoints)
        and all(
            isinstance(item, dict)
            and set(item) == {
                "endpoint", "fields", "page", "http_status", "response_hash",
                "response_size", "observed_at",
            }
            and item.get("page") == 1
            and item.get("http_status") == 200
            and isinstance(item.get("response_hash"), str)
            and bool(_SHA_RE.fullmatch(item["response_hash"]))
            and isinstance(item.get("response_size"), int)
            and 0 < item["response_size"] <= 2 * 1024 * 1024
            and item.get("observed_at") == receipt.get("checked_at")
            for item in journal
        )
    )
    try:
        capability_expires = _utc(capability_expires_at, "G005_AUDIENCE_RECEIPT_EXPIRED")
    except G005ContractError:
        capability_expires = datetime.min.replace(tzinfo=timezone.utc)
    if expires > capability_expires or expires <= assessment_clock:
        reasons.append("AUDIENCE_RECEIPT_EXPIRED")
    if not time_chain_valid:
        reasons.append("AUDIENCE_TIME_CHAIN_INVALID")
    if (
        not valid_config_fragment
        or receipt.get("outcome") != "INCOMPLETE"
        or receipt.get("audience_overlap_classification") != "TARGETING_CONFIG_EQUIVALENT"
        or receipt.get("internal_auction_classification") != "UNKNOWN"
        or receipt.get("not_gate_receipt") is not True
        or receipt.get("gate0_result_ceiling") != "QUASI_ONLY"
        or proof.get("allowed_methods") != ["GET"]
        or any(int(proof.get(key) or 0) for key in (
            "post_count", "put_count", "patch_count", "delete_count", "redirect_count",
            "batch_count", "async_job_count", "meta_object_writes", "local_db_writes",
        ))
    ):
        reasons.extend([
            "AUDIENCE_OVERLAP_UNKNOWN", "INTERNAL_AUCTION_CONTAMINATION_UNKNOWN",
        ])
    return receipt, _check(
        "UNKNOWN",
        reasons,
        [receipt_hash, evidence_hash],
    )


def _validate_attribution_artifact(
    report_raw: Any,
    input_raw: Any,
    subject: Mapping[str, Any],
    source_snapshot_sha256: str,
) -> Dict[str, Any]:
    report = dict(report_raw) if isinstance(report_raw, dict) else {}
    if report.get("schema_version") != "gle-g0-01-exact-id-attribution-audit-v1":
        raise G005ContractError("G005_ATTRIBUTION_REPORT_INVALID")
    report_hash = report.get("report_hash")
    unsigned = dict(report)
    unsigned.pop("report_hash", None)
    if not isinstance(report_hash, str) or not _SHA_RE.fullmatch(report_hash) or hash_json(unsigned) != report_hash:
        raise G005ContractError("G005_ATTRIBUTION_REPORT_HASH_MISMATCH")
    input_contract = _exact_object(
        input_raw,
        ("account_id", "market", "experiment_ids", "window_start", "window_end", "project", "max_events", "source_snapshot_sha256"),
        "G005_ATTRIBUTION_INPUT_INVALID",
    )
    experiment_ids = input_contract["experiment_ids"]
    if (
        not isinstance(experiment_ids, list)
        or len(experiment_ids) != 2
        or len(set(str(item or "") for item in experiment_ids)) != 2
        or any(not str(item or "").strip() for item in experiment_ids)
    ):
        raise G005ContractError("G005_ATTRIBUTION_INPUT_INVALID")
    if (
        input_contract["account_id"] != subject["ad_account_id"]
        or str(input_contract["market"] or "").upper() != subject["market"]
        or input_contract["source_snapshot_sha256"] != source_snapshot_sha256
        or report.get("source_snapshot_sha256") != source_snapshot_sha256
        or report.get("input_contract_hash") != hash_json(input_contract)
        or sorted(experiment_ids) != sorted(cell["experiment_id"] for cell in subject["cells"])
    ):
        raise G005ContractError("G005_ATTRIBUTION_SUBJECT_MISMATCH")
    allowed_external = {"QUALIFICATION_RULE_UNFROZEN", "READBACK_PROVENANCE_UNAUDITED"}
    required_report_fields = {
        "status", "source_schema_hash", "versions", "counts", "coverage",
        "reason_counts", "missing_reason_counts", "ambiguous_reason_counts",
        "crm_verification_latency_seconds", "row_evidence_hash",
    }
    if not required_report_fields.issubset(report):
        raise G005ContractError("G005_ATTRIBUTION_REPORT_INVALID")
    if report.get("status") not in {"BLOCKED", "COMPLETE"}:
        raise G005ContractError("G005_ATTRIBUTION_REPORT_INVALID")
    reasons = set(str(item) for item in (report.get("blocking_reasons") or []))
    sticky = sorted(reasons - allowed_external)
    return {
        "report_hash": report_hash,
        "status": "PASS" if not sticky else "INCOMPLETE",
        "reason_codes": sticky,
        "externally_closed_reasons": sorted(reasons & allowed_external),
        "window_start": input_contract["window_start"],
        "window_end": input_contract["window_end"],
    }


def _normalize_subject(raw: Any) -> Dict[str, Any]:
    subject = _exact_object(
        raw, ("ad_account_id", "market", "study_id", "cells"),
        "G005_SUBJECT_INVALID",
    )
    subject["ad_account_id"] = _text(subject["ad_account_id"], "G005_SUBJECT_INVALID")
    subject["market"] = _text(subject["market"], "G005_SUBJECT_INVALID").upper()
    subject["study_id"] = _text(subject["study_id"], "G005_SUBJECT_INVALID")
    if subject["ad_account_id"] != "1012060198097836" or subject["market"] != "MX":
        raise G005ContractError("G005_FROZEN_SUBJECT_MISMATCH")
    cells = subject["cells"]
    if not isinstance(cells, list) or len(cells) != 2:
        raise G005ContractError("G005_CELL_SET_INVALID")
    normalized = []
    for cell in cells:
        item = _exact_object(
            cell,
            ("cell_id", "experiment_id", "study_cell_id", "campaign_id", "adset_id", "ad_id", "target_share"),
            "G005_CELL_SET_INVALID",
        )
        normalized.append({
            "cell_id": _text(item["cell_id"], "G005_CELL_SET_INVALID"),
            "experiment_id": _text(item["experiment_id"], "G005_CELL_SET_INVALID"),
            "study_cell_id": _text(item["study_cell_id"], "G005_CELL_SET_INVALID"),
            "campaign_id": _text(item["campaign_id"], "G005_CELL_SET_INVALID"),
            "adset_id": _text(item["adset_id"], "G005_CELL_SET_INVALID"),
            "ad_id": _text(item["ad_id"], "G005_CELL_SET_INVALID"),
            "target_share": str(_ratio(item["target_share"], "G005_CELL_SET_INVALID", inclusive_zero=False)),
        })
    if (
        {item["cell_id"] for item in normalized} != {"C1", "C2"}
        or any(Decimal(item["target_share"]) != Decimal("0.5") for item in normalized)
        or
        len({item["cell_id"] for item in normalized}) != 2
        or len({item["study_cell_id"] for item in normalized}) != 2
        or len({item["experiment_id"] for item in normalized}) != 2
        or len({item["adset_id"] for item in normalized}) != 2
        or len({item["ad_id"] for item in normalized}) != 2
        or len({item["campaign_id"] for item in normalized}) != 1
    ):
        raise G005ContractError("G005_CELL_SET_INVALID")
    if sum(Decimal(item["target_share"]) for item in normalized) != Decimal("1"):
        raise G005ContractError("G005_TARGET_ALLOCATION_INVALID")
    subject["cells"] = sorted(normalized, key=lambda item: item["cell_id"])
    return subject


def _validate_experiment_binding(
    raw: Any, subject: Mapping[str, Any], source_snapshot_sha256: str,
) -> Dict[str, Any]:
    observation = _exact_object(
        raw,
        ("source_snapshot_sha256", "bindings", "evidence_hash"),
        "G005_EXPERIMENT_BINDING_INVALID",
    )
    evidence_hash = str(observation["evidence_hash"] or "")
    unsigned = dict(observation)
    unsigned.pop("evidence_hash", None)
    if (
        not _SHA_RE.fullmatch(evidence_hash)
        or hash_json(unsigned) != evidence_hash
        or observation["source_snapshot_sha256"] != source_snapshot_sha256
    ):
        raise G005ContractError("G005_EXPERIMENT_BINDING_HASH_MISMATCH")
    bindings = observation["bindings"]
    if not isinstance(bindings, list) or len(bindings) != 2:
        raise G005ContractError("G005_EXPERIMENT_BINDING_INVALID")
    expected = sorted(
        (
            cell["experiment_id"], subject["study_id"], cell["study_cell_id"],
            cell["campaign_id"], cell["adset_id"], cell["ad_id"], True,
        )
        for cell in subject["cells"]
    )
    actual = []
    for raw_binding in bindings:
        binding = _exact_object(
            raw_binding,
            ("experiment_id", "study_id", "study_cell_id", "campaign_id", "adset_id", "ad_id", "readback_verified"),
            "G005_EXPERIMENT_BINDING_INVALID",
        )
        actual.append(tuple(binding[key] for key in (
            "experiment_id", "study_id", "study_cell_id", "campaign_id", "adset_id", "ad_id", "readback_verified",
        )))
    if sorted(actual) != expected:
        raise G005ContractError("G005_EXPERIMENT_BINDING_MISMATCH")
    return {"evidence_hash": evidence_hash, "bindings": bindings}


def _normalize_policy(raw: Any) -> Dict[str, Any]:
    keys = (
        "policy_version", "qualification_version", "source_contract", "source_metric",
        "qualified_country", "qualified_media_source", "qualified_external_app",
        "minimum_attribution_coverage", "maximum_allocation_deviation",
        "minimum_total_impressions", "minimum_total_spend_usd", "minimum_complete_days",
        "reporting_settlement_hours", "source_freshness_hours", "baseline_window_days",
        "alpha_two_sided", "desired_power", "mde_relative", "maximum_test_days",
        "maximum_test_budget_usd", "maximum_daily_budget_usd", "expected_daily_spend_usd",
        "estimator_version", "golden_vectors_approved", "governance_model", "sole_owner",
    )
    policy = _exact_object(raw, keys, "G005_POLICY_INVALID")
    expected_text = {
        "policy_version": POLICY_VERSION,
        "qualification_version": QUALIFICATION_VERSION,
        "source_contract": SOURCE_CONTRACT,
        "source_metric": SOURCE_METRIC,
        "qualified_country": "Mexico",
        "qualified_media_source": "Meta",
        "qualified_external_app": "TUGAO",
        "estimator_version": ESTIMATOR_VERSION,
        "governance_model": "SOLE_OWNER",
        "sole_owner": "Chauncey",
    }
    if any(policy[key] != value for key, value in expected_text.items()):
        raise G005ContractError("G005_POLICY_VERSION_MISMATCH")
    if type(policy["golden_vectors_approved"]) is not bool:
        raise G005ContractError("G005_POLICY_INVALID")
    normalized = dict(policy)
    for key in ("minimum_attribution_coverage", "maximum_allocation_deviation", "alpha_two_sided", "desired_power", "mde_relative"):
        normalized[key] = str(_ratio(policy[key], "G005_POLICY_INVALID", inclusive_zero=False))
    for key in ("minimum_total_impressions", "minimum_complete_days", "reporting_settlement_hours", "source_freshness_hours", "baseline_window_days", "maximum_test_days"):
        normalized[key] = _integer(policy[key], "G005_POLICY_INVALID", minimum=1)
    for key in ("minimum_total_spend_usd", "maximum_test_budget_usd", "maximum_daily_budget_usd", "expected_daily_spend_usd"):
        normalized[key] = str(_decimal(policy[key], "G005_POLICY_INVALID", minimum=Decimal("0.000001")))
    if Decimal(normalized["expected_daily_spend_usd"]) > Decimal(normalized["maximum_daily_budget_usd"]):
        raise G005ContractError("G005_POLICY_INVALID")
    frozen = {
        "minimum_attribution_coverage": "0.8",
        "maximum_allocation_deviation": "0.1",
        "minimum_total_impressions": 1000,
        "minimum_total_spend_usd": "5",
        "minimum_complete_days": 3,
        "reporting_settlement_hours": 48,
        "source_freshness_hours": 6,
        "baseline_window_days": 14,
        "alpha_two_sided": "0.05",
        "desired_power": "0.8",
        "mde_relative": "0.3",
        "maximum_test_days": 14,
        "maximum_test_budget_usd": "20",
        "maximum_daily_budget_usd": "2",
        "expected_daily_spend_usd": "1.428571",
        "golden_vectors_approved": False,
    }
    if any(normalized[key] != value for key, value in frozen.items()):
        raise G005ContractError("G005_FROZEN_POLICY_MISMATCH")
    return normalized


def _validate_transport_evidence(raw: Any) -> Dict[str, Any]:
    evidence = _exact_object(raw, (
        "schema_version", "source_commit", "release_id", "manifest_sha256", "receipt_sha256",
        "deployed_artifact_sha256", "backend_invocation_id", "receipt_status",
        "deployed_at", "natural_evidence_not_before_date",
        "evidence_hash",
    ), "G005_TRANSPORT_EVIDENCE_INVALID")
    supplied_hash = str(evidence.pop("evidence_hash") or "")
    if (
        evidence["schema_version"] != "gle-g0-02b-qualified-transport-deployment-v1"
        or evidence["source_commit"] != G002B_SOURCE_COMMIT
        or evidence["receipt_status"] != "passed"
        or not _SHA_RE.fullmatch(str(evidence["manifest_sha256"] or ""))
        or not _SHA_RE.fullmatch(str(evidence["receipt_sha256"] or ""))
        or not _SHA_RE.fullmatch(str(evidence["deployed_artifact_sha256"] or ""))
        or not _text(evidence["backend_invocation_id"], "G005_TRANSPORT_EVIDENCE_INVALID")
        or not str(evidence["release_id"] or "").startswith("gle-g0-02b-")
        or hash_json(evidence) != supplied_hash
    ):
        raise G005ContractError("G005_TRANSPORT_EVIDENCE_INVALID")
    deployed_at = _utc(evidence["deployed_at"], "G005_TRANSPORT_EVIDENCE_INVALID")
    natural_date = _text(
        evidence["natural_evidence_not_before_date"],
        "G005_TRANSPORT_EVIDENCE_INVALID",
    )
    natural_at = _utc(natural_date + "T00:00:00+00:00", "G005_TRANSPORT_EVIDENCE_INVALID")
    if natural_at.date() <= deployed_at.date():
        raise G005ContractError("G005_TRANSPORT_EVIDENCE_INVALID")
    evidence["evidence_hash"] = supplied_hash
    return evidence


def _validate_capability(raw: Any, subject: Mapping[str, Any], requested_at: datetime) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if not isinstance(raw, dict):
        raise G005ContractError("G005_CAPABILITY_INVALID")
    receipt = dict(raw)
    body_hash = receipt.get("receipt_body_hash")
    if not isinstance(body_hash, str) or not _SHA_RE.fullmatch(body_hash):
        raise G005ContractError("G005_CAPABILITY_HASH_INVALID")
    unsigned = dict(receipt)
    unsigned.pop("receipt_body_hash", None)
    if hash_json(unsigned) != body_hash:
        raise G005ContractError("G005_CAPABILITY_HASH_INVALID")
    target = dict(receipt.get("target") or {})
    if (
        str(target.get("ad_account_id") or target.get("account_id") or "") != subject["ad_account_id"]
        or str(target.get("market") or "").upper() != subject["market"]
        or str(target.get("study_id") or "") != subject["study_id"]
        or str(target.get("campaign_id") or "") != subject["cells"][0]["campaign_id"]
    ):
        raise G005ContractError("G005_SUBJECT_MISMATCH")
    try:
        expires = _utc(receipt.get("expires_at"), "G005_CAPABILITY_EXPIRED")
    except G005ContractError:
        expires = datetime.min.replace(tzinfo=timezone.utc)
    reasons = list(receipt.get("blocking_reasons") or [])
    if expires < requested_at:
        reasons.append("CAPABILITY_RECEIPT_EXPIRED")
    if (
        receipt.get("schema_version") != "gle-g0-04-audit-receipt-v1"
        or receipt.get("not_gate_receipt") is not True
        or receipt.get("gate0_result_ceiling") != "QUASI_ONLY"
        or receipt.get("attestation_status") != "PENDING_ATTESTATION"
        or not isinstance(receipt.get("checks"), dict)
    ):
        reasons.append("CAPABILITY_FRAGMENT_CONTRACT_INVALID")
    if receipt.get("outcome") != "PASS" or receipt.get("gate0_fragment") != "PERMISSION_TOPOLOGY_PROVEN":
        reasons.append("CAPABILITY_NOT_CONTROLLED")
    status = "PASS" if not reasons else ("POLLUTED" if receipt.get("outcome") == "POLLUTED" else "INCOMPLETE")
    return receipt, _check(status, reasons, [body_hash])


def _validate_capability_topology(
    evidence: Mapping[str, Any],
    receipt: Mapping[str, Any],
    subject: Mapping[str, Any],
) -> None:
    required_checks = {
        "graph_completeness", "plan_binding", "token_permission",
        "business_ownership", "capability_semantics", "topology",
        "activation_provenance", "freshness", "zero_write",
    }
    checks = receipt.get("checks")
    if (
        not isinstance(checks, dict)
        or set(checks) != required_checks
        or any(not isinstance(checks[key], dict) for key in checks)
        or (
            receipt.get("outcome") == "PASS"
            and any(checks[key].get("status") != "PASS" for key in checks)
        )
        or not _SHA_RE.fullmatch(str(receipt.get("graph_evidence_hash") or ""))
    ):
        raise G005ContractError("G005_CAPABILITY_CHECKS_INVALID")
    graph = evidence.get("graph")
    if not isinstance(graph, dict):
        raise G005ContractError("G005_CAPABILITY_TOPOLOGY_MISMATCH")
    study_cells = dict(graph.get("study_cells") or {})
    cell_rows = study_cells.get("data")
    if study_cells.get("pagination_complete") is not True or not isinstance(cell_rows, list):
        raise G005ContractError("G005_CAPABILITY_TOPOLOGY_MISMATCH")
    expected_cells = {
        (cell["study_cell_id"], cell["ad_id"]) for cell in subject["cells"]
    }
    actual_cells = {
        (str(item.get("id") or ""), str((item.get("ad_ids") or [""])[0]))
        for item in cell_rows if isinstance(item, dict) and len(item.get("ad_ids") or []) == 1
    }
    if actual_cells != expected_cells:
        raise G005ContractError("G005_CAPABILITY_TOPOLOGY_MISMATCH")
    for cell in subject["cells"]:
        key = cell["cell_id"]
        edge = dict(graph.get(f"cell_{key}_adsets") or {})
        edge_ids = {
            str(item.get("id") or "") for item in (edge.get("data") or [])
            if isinstance(item, dict)
        }
        first_adset = dict(graph.get(f"first_adset_{key}") or {})
        first_ad = dict(graph.get(f"first_ad_{key}") or {})
        if (
            edge.get("pagination_complete") is not True
            or edge_ids != {cell["adset_id"]}
            or str(first_adset.get("id") or "") != cell["adset_id"]
            or str(first_ad.get("id") or "") != cell["ad_id"]
            or str(first_ad.get("adset_id") or "") != cell["adset_id"]
        ):
            raise G005ContractError("G005_CAPABILITY_TOPOLOGY_MISMATCH")


def _allocation_assessment(raw: Any, subject: Mapping[str, Any], policy: Mapping[str, Any]) -> Dict[str, Any]:
    data = _exact_object(raw, ("window_start", "window_end", "settled", "pagination_complete", "source_freshness_hours", "complete_days", "rows", "evidence_hash"), "G005_ALLOCATION_INVALID")
    if not isinstance(data["evidence_hash"], str) or not _SHA_RE.fullmatch(data["evidence_hash"]):
        raise G005ContractError("G005_ALLOCATION_INVALID")
    window_start = _utc(data["window_start"], "G005_ALLOCATION_INVALID")
    window_end = _utc(data["window_end"], "G005_ALLOCATION_INVALID")
    if window_start >= window_end:
        raise G005ContractError("G005_ALLOCATION_INVALID")
    rows = data["rows"]
    if not isinstance(rows, list):
        raise G005ContractError("G005_ALLOCATION_INVALID")
    expected = {(cell["cell_id"], cell["ad_id"]): cell for cell in subject["cells"]}
    totals = {key: {"impressions": 0, "spend": Decimal("0")} for key in expected}
    seen = set()
    reasons = []
    for row in rows:
        item = _exact_object(row, ("date", "cell_id", "ad_id", "impressions", "spend_usd"), "G005_ALLOCATION_INVALID")
        key = (_text(item["cell_id"], "G005_ALLOCATION_INVALID"), _text(item["ad_id"], "G005_ALLOCATION_INVALID"))
        date_key = _text(item["date"], "G005_ALLOCATION_INVALID")
        row_day = _utc(date_key + "T00:00:00+00:00", "G005_ALLOCATION_INVALID")
        if not window_start.date() <= row_day.date() <= window_end.date():
            reasons.append("ALLOCATION_ROW_OUTSIDE_WINDOW")
            continue
        if key not in expected:
            reasons.append("ALLOCATION_FOREIGN_OBJECT")
            continue
        if (date_key, key) in seen:
            reasons.append("ALLOCATION_DUPLICATE_ROW")
            continue
        seen.add((date_key, key))
        totals[key]["impressions"] += _integer(item["impressions"], "G005_ALLOCATION_INVALID")
        totals[key]["spend"] += _decimal(item["spend_usd"], "G005_ALLOCATION_INVALID", minimum=Decimal("0"))
    total_impressions = sum(item["impressions"] for item in totals.values())
    total_spend = sum((item["spend"] for item in totals.values()), Decimal("0"))
    complete_days = _integer(data["complete_days"], "G005_ALLOCATION_INVALID")
    freshness = _decimal(data["source_freshness_hours"], "G005_ALLOCATION_INVALID", minimum=Decimal("0"))
    if data["settled"] is not True:
        reasons.append("META_REPORTING_DELAY_OPEN")
    if data["pagination_complete"] is not True:
        reasons.append("ALLOCATION_SOURCE_INCOMPLETE")
    if complete_days < int(policy["minimum_complete_days"]):
        reasons.append("ALLOCATION_WINDOW_INCOMPLETE")
    if freshness > Decimal(str(policy["source_freshness_hours"])):
        reasons.append("SOURCE_STALE")
    if total_impressions == 0:
        reasons.extend(["ALLOCATION_DENOMINATOR_ZERO", "ACTUAL_ALLOCATION_UNKNOWN"])
    if total_spend == 0:
        reasons.append("ALLOCATION_SPEND_DENOMINATOR_ZERO")
    if total_impressions < int(policy["minimum_total_impressions"]):
        reasons.append("ALLOCATION_MINIMUM_IMPRESSIONS_NOT_MET")
    if total_spend < Decimal(str(policy["minimum_total_spend_usd"])):
        reasons.append("ALLOCATION_MINIMUM_SPEND_NOT_MET")
    cells = []
    for key, cell in sorted(expected.items()):
        values = totals[key]
        impression_share = Decimal(values["impressions"]) / Decimal(total_impressions) if total_impressions else None
        spend_share = values["spend"] / total_spend if total_spend else None
        target = Decimal(cell["target_share"])
        deviation = abs(impression_share - target) if impression_share is not None else None
        spend_deviation = abs(spend_share - target) if spend_share is not None else None
        if deviation is not None and deviation > Decimal(str(policy["maximum_allocation_deviation"])):
            reasons.append("ALLOCATION_DEVIATION_EXCEEDED")
        if spend_deviation is not None and spend_deviation > Decimal(str(policy["maximum_allocation_deviation"])):
            reasons.append("ALLOCATION_SPEND_DEVIATION_EXCEEDED")
        cells.append({
            "cell_id": key[0], "ad_id": key[1], "target_share": str(target),
            "impressions": values["impressions"], "spend_usd": str(values["spend"]),
            "impression_share": str(impression_share) if impression_share is not None else None,
            "spend_share": str(spend_share) if spend_share is not None else None,
            "absolute_impression_deviation": str(deviation) if deviation is not None else None,
            "absolute_spend_deviation": str(spend_deviation) if spend_deviation is not None else None,
        })
    hard_reasons = {
        "ALLOCATION_DEVIATION_EXCEEDED", "ALLOCATION_SPEND_DEVIATION_EXCEEDED",
    }
    return {
        "status": "FAIL" if hard_reasons & set(reasons) else ("PASS" if not reasons else "UNKNOWN"),
        "reason_codes": sorted(set(reasons)), "window_start": data["window_start"],
        "window_end": data["window_end"], "complete_days": complete_days,
        "total_impressions": total_impressions, "total_spend_usd": str(total_spend),
        "cells": cells, "evidence_hash": data["evidence_hash"],
    }


def _qualified_join_assessment(raw: Any, subject: Mapping[str, Any], policy: Mapping[str, Any]) -> Dict[str, Any]:
    data = _exact_object(raw, (
        "source_contract", "source_metric", "qualification_version", "complete",
        "source_freshness_hours", "eligible_qualified_joins",
        "exact_attributed_qualified_joins", "window_start", "window_end",
        "cells", "evidence_hash",
    ), "G005_QUALIFIED_JOIN_INVALID")
    if data["source_contract"] != SOURCE_CONTRACT or data["source_metric"] != SOURCE_METRIC or data["qualification_version"] != QUALIFICATION_VERSION:
        raise G005ContractError("G005_QUALIFICATION_VERSION_MISMATCH")
    if not isinstance(data["evidence_hash"], str) or not _SHA_RE.fullmatch(data["evidence_hash"]):
        raise G005ContractError("G005_QUALIFIED_JOIN_INVALID")
    if _utc(data["window_start"], "G005_QUALIFIED_JOIN_INVALID") >= _utc(data["window_end"], "G005_QUALIFIED_JOIN_INVALID"):
        raise G005ContractError("G005_QUALIFIED_JOIN_INVALID")
    eligible = _integer(data["eligible_qualified_joins"], "G005_QUALIFIED_JOIN_INVALID")
    exact = _integer(data["exact_attributed_qualified_joins"], "G005_QUALIFIED_JOIN_INVALID")
    if exact > eligible:
        raise G005ContractError("G005_QUALIFIED_JOIN_INVALID")
    expected = {(cell["cell_id"], cell["ad_id"]) for cell in subject["cells"]}
    observed = {}
    for row in data["cells"] if isinstance(data["cells"], list) else ():
        item = _exact_object(row, ("cell_id", "ad_id", "qualified_joins"), "G005_QUALIFIED_JOIN_INVALID")
        key = (_text(item["cell_id"], "G005_QUALIFIED_JOIN_INVALID"), _text(item["ad_id"], "G005_QUALIFIED_JOIN_INVALID"))
        if key not in expected or key in observed:
            raise G005ContractError("G005_QUALIFIED_JOIN_SUBJECT_MISMATCH")
        observed[key] = _integer(item["qualified_joins"], "G005_QUALIFIED_JOIN_INVALID")
    if sum(observed.values()) != exact:
        raise G005ContractError("G005_QUALIFIED_JOIN_INVALID")
    reasons = []
    if set(observed) != expected:
        reasons.append("QUALIFIED_JOIN_CELL_MISSING")
    if data["complete"] is not True:
        reasons.append("QUALIFIED_JOIN_SOURCE_INCOMPLETE")
    freshness = _decimal(data["source_freshness_hours"], "G005_QUALIFIED_JOIN_INVALID", minimum=Decimal("0"))
    if freshness > Decimal(str(policy["source_freshness_hours"])):
        reasons.append("SOURCE_STALE")
    coverage = Decimal(exact) / Decimal(eligible) if eligible else None
    if coverage is None:
        reasons.append("ATTRIBUTION_COVERAGE_UNKNOWN")
    elif coverage < Decimal(str(policy["minimum_attribution_coverage"])):
        reasons.append("ATTRIBUTION_COVERAGE_BELOW_THRESHOLD")
    return {
        "status": "PASS" if not reasons else "UNKNOWN", "reason_codes": sorted(set(reasons)),
        "eligible_qualified_joins": eligible,
        "exact_attributed_qualified_joins": exact,
        "attribution_coverage": str(coverage) if coverage is not None else None,
        "qualified_joins": sum(observed.values()),
        "cells": [
            {"cell_id": key[0], "ad_id": key[1], "qualified_joins": observed[key]}
            for key in sorted(observed)
        ],
        "window_start": data["window_start"], "window_end": data["window_end"],
        "evidence_hash": data["evidence_hash"],
    }


def _power_assessment(raw: Any, policy: Mapping[str, Any], subject: Mapping[str, Any]) -> Dict[str, Any]:
    data = _exact_object(raw, (
        "window_start", "window_end", "complete_days", "total_impressions",
        "qualified_joins", "total_spend_usd", "attribution_coverage",
        "event_attribution_coverage", "exposure_identity_coverage",
        "source_freshness_hours", "evidence_hash",
    ), "G005_BASELINE_INVALID")
    start = _utc(data["window_start"], "G005_BASELINE_INVALID")
    end = _utc(data["window_end"], "G005_BASELINE_INVALID")
    if start >= end:
        raise G005ContractError("G005_BASELINE_INVALID")
    if not _SHA_RE.fullmatch(str(data["evidence_hash"] or "")):
        raise G005ContractError("G005_BASELINE_INVALID")
    days = _integer(data["complete_days"], "G005_BASELINE_INVALID", minimum=1)
    impressions = _integer(data["total_impressions"], "G005_BASELINE_INVALID")
    events = _integer(data["qualified_joins"], "G005_BASELINE_INVALID")
    spend = _decimal(data["total_spend_usd"], "G005_BASELINE_INVALID", minimum=Decimal("0"))
    coverage = _ratio(data["attribution_coverage"], "G005_BASELINE_INVALID")
    event_coverage = _ratio(data["event_attribution_coverage"], "G005_BASELINE_INVALID")
    exposure_coverage = _ratio(data["exposure_identity_coverage"], "G005_BASELINE_INVALID")
    if coverage != min(event_coverage, exposure_coverage):
        raise G005ContractError("G005_BASELINE_COVERAGE_MISMATCH")
    freshness = _decimal(data["source_freshness_hours"], "G005_BASELINE_INVALID", minimum=Decimal("0"))
    reasons = []
    window_days = (end.date() - start.date()).days + 1
    if days != int(policy["baseline_window_days"]) or window_days != int(policy["baseline_window_days"]):
        reasons.append("BASELINE_WINDOW_INCOMPLETE")
    if impressions == 0:
        reasons.append("BASELINE_DENOMINATOR_ZERO")
    if events == 0:
        reasons.append("BASELINE_EVENT_RATE_UNKNOWN")
    if coverage < Decimal(str(policy["minimum_attribution_coverage"])):
        reasons.append("ATTRIBUTION_COVERAGE_BELOW_THRESHOLD")
    if freshness > Decimal(str(policy["source_freshness_hours"])):
        reasons.append("SOURCE_STALE")
    baseline_rate = Decimal(events) / spend if spend else None
    daily_events = (
        baseline_rate * Decimal(str(policy["expected_daily_spend_usd"]))
        if baseline_rate is not None else None
    )
    evidence_reasons = sorted(set(reasons))
    diagnostic = assess_fixed_endpoint_power({
        "schema_version": POWER_INPUT_VERSION,
        "estimator_version": ESTIMATOR_VERSION,
        "estimand": POWER_ESTIMAND,
        "offset": POWER_OFFSET,
        "alternative": POWER_ALTERNATIVE,
        "approximation": POWER_APPROXIMATION,
        "control_allocation": "0.5",
        "treatment_allocation": "0.5",
        "alpha_two_sided": str(policy["alpha_two_sided"]),
        "desired_power": str(policy["desired_power"]),
        "mde_relative": str(policy["mde_relative"]),
        "baseline_qualified_joins": events,
        "baseline_spend_usd": str(spend),
        "evidence_status": "READY" if not evidence_reasons else "INCOMPLETE",
        "incomplete_reasons": evidence_reasons,
        "expected_daily_spend_usd": str(policy["expected_daily_spend_usd"]),
        "maximum_test_days": int(policy["maximum_test_days"]),
        "maximum_test_budget_usd": str(policy["maximum_test_budget_usd"]),
    })
    reasons = list(diagnostic["reason_codes"])
    # Fixed-endpoint information is a necessary lower-bound diagnostic, not an
    # O'Brien-Fleming contract. A trusted FAIL may reject feasibility; a PASS
    # cannot promote Gate 0 until Gate 1's look schedule is frozen.
    reasons.extend(("OBF_BOUNDARY_UNFROZEN", "POWER_GOLDEN_VECTORS_UNAPPROVED"))
    fixed_status = diagnostic["fixed_endpoint_status"]
    return {
        "power_assessment_id": "g005_power_" + hash_json({"subject": subject, "baseline": data, "policy": policy})[:24],
        "objective_contract_id": QUALIFICATION_VERSION,
        "ad_account_id": subject["ad_account_id"], "market": subject["market"],
        "status": "FAIL" if fixed_status == "FAIL" else "UNKNOWN",
        "feasible": False,
        "failure_reasons": sorted(set(reasons)), "estimator_version": ESTIMATOR_VERSION,
        "baseline_window": {"start_at": data["window_start"], "end_at": data["window_end"]},
        "baseline_event_rate": str(baseline_rate) if baseline_rate is not None else None,
        "attribution_coverage": str(coverage),
        "event_attribution_coverage": str(event_coverage),
        "exposure_identity_coverage": str(exposure_coverage),
        "expected_daily_events": str(daily_events) if daily_events is not None else None,
        "expected_daily_spend_usd": str(policy["expected_daily_spend_usd"]),
        "alpha_two_sided": str(policy["alpha_two_sided"]),
        "desired_power": str(policy["desired_power"]),
        "mde_relative": str(policy["mde_relative"]),
        "target_information": diagnostic["target_information"],
        "expected_days_to_maturity": diagnostic["expected_days_to_maturity"],
        "expected_total_spend_usd": diagnostic["expected_total_spend_usd"],
        "max_allowed_days": int(policy["maximum_test_days"]),
        "max_test_budget_usd": str(policy["maximum_test_budget_usd"]),
        "fixed_endpoint_diagnostic": diagnostic,
        "obf_boundary_status": "UNFROZEN",
        "evidence_hash": data["evidence_hash"], "observed_total_spend_usd": str(spend),
    }


def _governance_check(raw: Any, subject: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        contract = validate_governance_contract(raw)
    except ValueError as exc:
        raise G005ContractError("G005_GOVERNANCE_CONTRACT_INVALID") from exc
    data = dict(contract.data)
    reasons = []
    canary = dict(data.get("canary") or {})
    if subject["ad_account_id"] not in list(canary.get("account_ids") or []):
        reasons.append("CANARY_ACCOUNT_NOT_ALLOWLISTED")
    if subject["market"] not in [str(item).upper() for item in list(canary.get("markets") or [])]:
        reasons.append("CANARY_MARKET_NOT_ALLOWLISTED")
    actions = set(data.get("action_allowlist") or [])
    if not {"CREATE_CANARY_PAUSED", "ACTIVATE_CANARY"}.issubset(actions):
        reasons.append("CANARY_ACTION_NOT_ALLOWLISTED")
    switches = dict(data.get("kill_switches") or {})
    required_switches = {
        "block_all_actions", "block_all_meta_writes", "block_account_writes",
        "block_action_writes", "disable_evaluation_scheduler",
        "block_new_experiment_activation",
    }
    if (
        data.get("global_enabled") is not False
        or data.get("mode") != "OFF"
        or any(switches.get(key) is not True for key in required_switches)
    ):
        reasons.append("E00_NOT_FAIL_CLOSED")
    return _check("PASS" if not reasons else "INCOMPLETE", reasons, [contract.canonical_hash])


def _study_integrity_check(capability: Mapping[str, Any]) -> Dict[str, Any]:
    reasons = set(str(item) for item in (capability.get("blocking_reasons") or []))
    polluted_codes = {
        "EXTERNAL_ACTIVATION_DETECTED", "LEGACY_STUDY_INADMISSIBLE",
        "CELL_OBJECT_BINDING_MISMATCH", "STRICT_COPY_INVARIANT_FAILED",
    }
    polluted = capability.get("outcome") == "POLLUTED" or bool(reasons & polluted_codes)
    if polluted:
        reasons.add("EXTERNAL_ACTIVATION_CONTAMINATION")
    return _check(
        "POLLUTED" if polluted else ("PASS" if capability.get("outcome") == "PASS" else "UNKNOWN"),
        sorted(reasons & polluted_codes | ({"EXTERNAL_ACTIVATION_CONTAMINATION"} if polluted else set())),
        [str(capability.get("receipt_body_hash") or "")],
    )


def assess_gate0(raw: Mapping[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    bundle = _exact_object(raw, (
        "schema_version", "assessment_id", "requested_at", "data_cutoff_at", "subject", "policy",
        "qualified_transport_evidence",
        "source_snapshot_sha256", "capability_manifest", "capability_receipt", "capability_evidence",
        "audience_manifest", "audience_receipt", "audience_evidence",
        "attribution_input_contract", "attribution_report", "allocation_observation",
        "experiment_binding_observation", "qualified_join_observation", "baseline_observation", "governance_contract",
    ), "G005_INPUT_SCHEMA_INVALID")
    if bundle["schema_version"] != INPUT_VERSION:
        raise G005ContractError("G005_INPUT_SCHEMA_INVALID")
    assessment_id = _text(bundle["assessment_id"], "G005_INPUT_SCHEMA_INVALID")
    requested_at = _utc(bundle["requested_at"], "G005_INPUT_SCHEMA_INVALID")
    cutoff = _utc(bundle["data_cutoff_at"], "G005_INPUT_SCHEMA_INVALID")
    transport_evidence = _validate_transport_evidence(bundle["qualified_transport_evidence"])
    natural_date = transport_evidence["natural_evidence_not_before_date"]
    natural_start = _utc(natural_date + "T00:00:00+00:00", "G005_INPUT_SCHEMA_INVALID")
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if cutoff > requested_at or requested_at > clock:
        raise G005ContractError("G005_TIME_ORDER_INVALID")
    subject = _normalize_subject(bundle["subject"])
    policy = _normalize_policy(bundle["policy"])
    source_snapshot_sha256 = str(bundle["source_snapshot_sha256"] or "")
    if not _SHA_RE.fullmatch(source_snapshot_sha256):
        raise G005ContractError("G005_SOURCE_HASH_INVALID")
    capability_raw, capability_evidence = _validate_capability_artifacts(
        bundle["capability_manifest"], bundle["capability_receipt"], bundle["capability_evidence"],
    )
    capability, capability_check = _validate_capability(capability_raw, subject, requested_at)
    _validate_capability_topology(capability_evidence, capability, subject)
    if capability_evidence.get("source_snapshot_sha256") != capability.get("source_snapshot_sha256"):
        raise G005ContractError("G005_CAPABILITY_EVIDENCE_HASH_MISMATCH")
    if capability.get("source_snapshot_sha256") != source_snapshot_sha256:
        raise G005ContractError("G005_CAPABILITY_SOURCE_SNAPSHOT_MISMATCH")
    audience_receipt, audience_check = _validate_audience_artifacts(
        bundle["audience_manifest"], bundle["audience_receipt"], bundle["audience_evidence"],
        subject=subject, source_snapshot_sha256=source_snapshot_sha256,
        capability_receipt_hash=str(capability.get("receipt_body_hash") or ""),
        capability_evidence_hash=str(capability_evidence.get("evidence_bundle_hash") or ""),
        capability_expires_at=str(capability.get("expires_at") or ""),
        requested_at=requested_at,
        assessment_clock=clock,
    )
    attribution = _validate_attribution_artifact(
        bundle["attribution_report"], bundle["attribution_input_contract"],
        subject, source_snapshot_sha256,
    )
    experiment_binding = _validate_experiment_binding(
        bundle["experiment_binding_observation"], subject, source_snapshot_sha256,
    )
    allocation = _allocation_assessment(bundle["allocation_observation"], subject, policy)
    if _utc(allocation["window_start"], "G005_NATURAL_EVIDENCE_WINDOW_INVALID").date() < natural_start.date():
        raise G005ContractError("G005_NATURAL_EVIDENCE_WINDOW_INVALID")
    qualified = _qualified_join_assessment(bundle["qualified_join_observation"], subject, policy)
    if (
        qualified["window_start"] != allocation["window_start"]
        or qualified["window_end"] != allocation["window_end"]
    ):
        raise G005ContractError("G005_QUALIFIED_JOIN_WINDOW_MISMATCH")
    power = _power_assessment(bundle["baseline_observation"], policy, subject)
    if (
        str(attribution["window_start"])[:10] != str(power["baseline_window"]["start_at"])[:10]
        or str(attribution["window_end"])[:10] != str(power["baseline_window"]["end_at"])[:10]
    ):
        raise G005ContractError("G005_ATTRIBUTION_WINDOW_MISMATCH")
    integrity_check = _study_integrity_check(capability)
    checks = {
        "governance_allowlist": _governance_check(bundle["governance_contract"], subject),
        "audience_risk": audience_check,
        "capability": capability_check,
        "canonical_attribution": _check(
            attribution["status"], attribution["reason_codes"], [attribution["report_hash"]],
        ),
        "experiment_binding": _check("PASS", [], [experiment_binding["evidence_hash"]]),
        "study_integrity": integrity_check,
        "actual_allocation": _check(allocation["status"], allocation["reason_codes"], [allocation["evidence_hash"]]),
        "qualified_join_attribution": _check(qualified["status"], qualified["reason_codes"], [qualified["evidence_hash"]]),
        "power": _check(power["status"], power["failure_reasons"], [power["evidence_hash"]]),
        "zero_write": _check("PASS", [], ["pure_offline_engine"]),
    }
    blockers = sorted({reason for check in checks.values() for reason in check["reason_codes"]})
    hard_failure = any(check["status"] in {"FAIL", "POLLUTED"} for check in checks.values())
    evidence_complete = all(check["status"] == "PASS" for check in checks.values())
    candidate = {
        "schema_version": CANDIDATE_VERSION, "assessment_id": assessment_id,
        "engine_version": ENGINE_VERSION, "requested_at": requested_at.isoformat(),
        "data_cutoff_at": cutoff.isoformat(),
        "natural_evidence_not_before_date": natural_date,
        "qualified_transport_evidence_hash": transport_evidence["evidence_hash"],
        "subject": subject, "policy_hash": hash_json(policy),
        "source_snapshot_sha256": source_snapshot_sha256,
        "capability_receipt_hash": capability["receipt_body_hash"],
        "audience_receipt_hash": audience_receipt["receipt_body_hash"],
        "attribution_report_hash": attribution["report_hash"],
        "checks": checks, "allocation_assessment": allocation,
        "qualified_join_assessment": qualified, "power_assessment": power,
        "blocking_reasons": blockers,
        "technical_candidate_result": "NOT_FEASIBLE" if hard_failure else ("CONTROLLED_FEASIBLE" if evidence_complete else "QUASI_ONLY"),
        "gate0_result_ceiling": "QUASI_ONLY",
        "attestation_status": "PENDING",
        "not_gate_receipt": True,
        "not_meta_write_receipt": True,
    }
    candidate["candidate_body_hash"] = hash_json(candidate)
    return candidate


def exit_code_for_candidate(candidate: Mapping[str, Any]) -> int:
    # G0-05 only publishes an unsigned candidate; never signal Gate PASS.
    return 2
