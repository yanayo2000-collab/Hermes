from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Dict, Iterable, Mapping, Sequence

from app.growth.errors import GrowthValidationError


META_SDK_PACKAGE_VERSION = "25.0.1"
META_GRAPH_API_VERSION = "v25.0"
META_SDK_CONTRACT_VERSION = "gle-meta-sdk-v1"

_SECRET_KEY = re.compile(
    r"(?:access[_-]?token|app[_-]?secret|client[_-]?secret|password|authorization|signature|sig)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)(bearer\s+)[a-z0-9._~-]+")
_TOKEN_QUERY = re.compile(r"(?i)(access_token|appsecret_proof|token)=([^&\s]+)")


SDK_OBJECT_CONTRACT: Dict[str, Dict[str, Any]] = {
    "AdAccount": {
        "module": "facebook_business.adobjects.adaccount",
        "class": "AdAccount",
        "read_fields": [
            "id", "account_id", "name", "account_status", "disable_reason", "currency",
            "timezone_name", "user_tasks", "capabilities", "all_capabilities",
            "can_create_brand_lift_study", "ad_account_promotable_objects",
        ],
        "write_fields": [],
        "methods": [
            "api_get", "get_activities", "get_ad_studies", "get_campaigns", "get_ad_sets",
            "get_ads", "get_ad_creatives", "get_advertisable_applications",
        ],
        "enums": [],
    },
    "Campaign": {
        "module": "facebook_business.adobjects.campaign",
        "class": "Campaign",
        "read_fields": [
            "id", "account_id", "name", "objective", "buying_type", "status",
            "configured_status", "effective_status", "daily_budget", "lifetime_budget",
            "bid_strategy", "special_ad_categories", "is_adset_budget_sharing_enabled",
            "promoted_object", "issues_info", "created_time", "updated_time",
        ],
        "write_fields": [
            "name", "objective", "buying_type", "status", "daily_budget", "lifetime_budget",
            "bid_strategy", "special_ad_categories", "is_adset_budget_sharing_enabled",
        ],
        "methods": ["api_get", "api_update", "get_ad_sets", "get_ads", "get_ad_studies", "get_insights_async", "create_copy"],
        "enums": ["BidStrategy", "Objective", "Status"],
    },
    "AdSet": {
        "module": "facebook_business.adobjects.adset",
        "class": "AdSet",
        "read_fields": [
            "id", "account_id", "campaign_id", "name", "status", "configured_status",
            "effective_status", "daily_budget", "lifetime_budget", "bid_strategy", "bid_amount",
            "billing_event", "optimization_goal", "promoted_object", "targeting",
            "attribution_spec", "regional_regulation_identities", "learning_stage_info",
            "issues_info", "created_time", "updated_time",
        ],
        "write_fields": [
            "campaign_id", "name", "status", "daily_budget", "lifetime_budget", "bid_strategy",
            "bid_amount", "billing_event", "optimization_goal", "promoted_object", "targeting",
            "attribution_spec", "regional_regulation_identities", "start_time", "end_time",
        ],
        "methods": ["api_get", "api_update", "get_activities", "get_ads", "get_insights_async", "get_ad_studies", "create_copy"],
        "enums": ["BidStrategy", "BillingEvent", "OptimizationGoal", "Status"],
    },
    "AdCreative": {
        "module": "facebook_business.adobjects.adcreative",
        "class": "AdCreative",
        "read_fields": [
            "id", "account_id", "name", "status", "object_story_spec", "object_story_id",
            "image_hash", "title", "body", "call_to_action_type", "url_tags",
            "effective_object_story_id", "instagram_user_id",
        ],
        "write_fields": [
            "name", "object_story_spec", "image_hash", "title", "body", "call_to_action_type",
            "url_tags", "instagram_user_id",
        ],
        "methods": ["api_get", "get_previews", "get_creative_insights"],
        "enums": ["CallToActionType", "Status"],
    },
    "Ad": {
        "module": "facebook_business.adobjects.ad",
        "class": "Ad",
        "read_fields": [
            "id", "account_id", "campaign_id", "adset_id", "name", "status",
            "configured_status", "effective_status", "creative", "tracking_specs",
            "conversion_specs", "ad_review_feedback", "issues_info", "failed_delivery_checks",
            "created_time", "updated_time",
        ],
        "write_fields": ["name", "adset_id", "creative", "status", "tracking_specs"],
        "methods": ["api_get", "api_update", "get_insights_async", "get_previews", "create_copy"],
        "enums": ["Status"],
    },
    "AdStudy": {
        "module": "facebook_business.adobjects.adstudy",
        "class": "AdStudy",
        "read_fields": [
            "id", "name", "type", "description", "start_time", "end_time",
            "observation_end_time", "cooldown_start_time", "results_first_available_date",
            "created_time", "updated_time",
        ],
        "write_fields": [
            "name", "type", "description", "start_time", "end_time", "observation_end_time",
            "cooldown_start_time", "cells",
        ],
        "methods": ["api_get", "api_update", "get_cells", "get_objectives"],
        "enums": ["Type"],
    },
    "AdStudyCell": {
        "module": "facebook_business.adobjects.adstudycell",
        "class": "AdStudyCell",
        "read_fields": ["id", "name", "ad_ids", "ad_entities_count", "treatment_percentage", "control_percentage"],
        "write_fields": ["name", "treatment_percentage", "control_percentage"],
        "methods": ["api_get", "get_ad_sets", "get_campaigns", "get_ad_accounts"],
        "enums": [],
    },
    "AdReportRun": {
        "module": "facebook_business.adobjects.adreportrun",
        "class": "AdReportRun",
        "read_fields": [
            "id", "account_id", "async_status", "async_percent_completion", "is_running",
            "date_start", "date_stop", "time_completed", "error_code", "error_subcode",
            "error_message", "error_user_title", "error_user_msg",
        ],
        "write_fields": [],
        "methods": ["api_get", "get_insights", "get_result"],
        "enums": [],
    },
}


CONTROLLED_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "COPY_ONLY_SPLIT_TEST": {
        "state": "READY_FOR_REHEARSAL",
        "required_objects": ["Campaign", "AdSet", "AdCreative", "Ad", "AdStudy", "AdStudyCell"],
        "write_action": "CREATE_PAUSED_AD",
        "single_variable": "PRIMARY_TEXT",
        "requires_explicit_approval": True,
        "activation_separate": True,
    },
    "AD_PREVIEW": {"state": "READ_ONLY_READY", "required_objects": ["Ad", "AdCreative"]},
    "COPY_ADSET": {"state": "BLOCKED", "reason": "compiler_and_readback_not_qualified"},
    "COPY_SCALE": {"state": "BLOCKED", "reason": "gate_and_budget_policy_not_qualified"},
    "VIDEO": {"state": "BLOCKED", "reason": "video_upload_and_preview_contract_missing"},
    "CAROUSEL": {"state": "BLOCKED", "reason": "child_attachment_readback_missing"},
    "BUDGET_SCHEDULE": {"state": "BLOCKED", "reason": "schedule_guardrail_not_qualified"},
    "CBO": {"state": "BLOCKED", "reason": "campaign_budget_allocation_not_qualified"},
    "LEAD_ADS": {"state": "OUT_OF_SCOPE"},
    "CATALOG": {"state": "OUT_OF_SCOPE"},
    "MESSAGING": {"state": "OUT_OF_SCOPE"},
    "CUSTOM_AUDIENCE": {"state": "OUT_OF_SCOPE"},
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def contract_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def redact_meta_evidence(value: Any) -> Any:
    """Keep diagnostic structure while recursively removing credentials."""
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _SECRET_KEY.search(str(key)) else redact_meta_evidence(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_meta_evidence(item) for item in value]
    if isinstance(value, str):
        return _TOKEN_QUERY.sub(r"\1=[REDACTED]", _BEARER.sub(r"\1[REDACTED]", value))
    return value


def relevant_meta_error_evidence(body: Mapping[str, Any], *, http_status: int | None = None) -> Dict[str, Any]:
    error = dict(body.get("error") or {})
    evidence = {
        "http_status": http_status,
        "type": error.get("type"),
        "code": error.get("code"),
        "error_subcode": error.get("error_subcode"),
        "is_transient": error.get("is_transient"),
        "message": error.get("message"),
        "error_user_title": error.get("error_user_title"),
        "error_user_msg": error.get("error_user_msg"),
        "fbtrace_id": error.get("fbtrace_id"),
        "error_data": error.get("error_data"),
    }
    return {key: value for key, value in redact_meta_evidence(evidence).items() if value not in (None, "", {})}


def _normalize_contract_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _normalize_contract_value(item) for key, item in sorted(value.items())}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        normalized = [_normalize_contract_value(item) for item in value]
        if all(not isinstance(item, (dict, list)) for item in normalized):
            return sorted(normalized, key=str)
        return normalized
    if isinstance(value, bool) or value is None:
        return value
    return str(value)


def compare_readback_fields(
    *, object_type: str, expected: Mapping[str, Any], actual: Mapping[str, Any], fields: Iterable[str],
) -> Dict[str, Any]:
    mismatches = []
    compared = []
    for field in fields:
        if field not in expected:
            continue
        compared.append(field)
        left = _normalize_contract_value(expected.get(field))
        right = _normalize_contract_value(actual.get(field))
        if left != right:
            mismatches.append({"field": field, "expected": left, "actual": right})
    return {
        "object_type": object_type,
        "status": "VERIFIED" if not mismatches else "MISMATCH",
        "compared_fields": compared,
        "mismatches": mismatches,
        "expected_hash": contract_hash({field: expected.get(field) for field in compared}),
        "actual_hash": contract_hash({field: actual.get(field) for field in compared}),
    }


def assert_capability_state(capability: str, *, allowed_states: Iterable[str]) -> Dict[str, Any]:
    key = str(capability or "").strip().upper()
    contract = dict(CONTROLLED_CAPABILITIES.get(key) or {})
    if not contract:
        raise GrowthValidationError("meta_capability_unknown")
    if str(contract.get("state") or "") not in set(allowed_states):
        raise GrowthValidationError(f"meta_capability_blocked:{key}:{contract.get('reason') or contract.get('state')}")
    return contract
