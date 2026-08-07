"""Deterministic, fail-closed compiler for the GLE Copy-only golden path.

The compiler is deliberately pure: it neither reads a database nor calls Meta.
It accepts only the existing two-cell ``CREATE_PAUSED_AD`` plan shape and proves
that the sole business-variable difference is Creative primary text.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Dict, Iterable, Mapping

from app.growth.common import decode_json, payload_hash
from app.growth.errors import GrowthStateConflict, GrowthValidationError
from app.growth.phase1_governance import effective_permissions, load_governance


COMPILER_VERSION = "gle-primary-text-only-compiler-v1"
PLAN_SCHEMA_VERSION = "gle-copy-only-plan-schema-v1"
CANONICALIZATION_VERSION = "gle-canonical-json-v1"
MAX_APPROVED_ASSET_BYTES = 25 * 1024 * 1024

_TOP_LEVEL_KEYS = {
    "plan_id", "plan_version", "launch_id", "experiment_id", "experiment_ids",
    "experiment_type", "unique_variable", "action_type", "target_account_id",
    "target_object_type", "target_object_id", "campaign", "cells",
    "baseline_experiment_id", "test_variable", "sdk_contract_version",
    "copy_benchmark_versions", "frozen_creative_id", "study",
    "audience_preflight", "invariants", "delivery_guardrails",
    "max_write_requests", "execution_policy", "evaluation_window", "expires_at",
    "compiler_receipt",
}
_CELL_KEYS = {
    "cell_key", "experiment_id", "experiment_code", "role", "creative_direction",
    "audience_strategy", "allocation_percent", "study_cell_name",
    "frozen_creative_id", "copy_version_id", "copy_benchmark_version",
    "copy_hypothesis", "steps", "asset_sha256",
}
_STEP_KEYS = {"IMAGE_UPLOAD", "CREATIVE_CREATE", "ADSET_CREATE", "AD_CREATE"}
_IMAGE_KEYS = {"image_id", "image_path"}
_CREATIVE_KEYS = {"name", "object_story_spec"}
_STORY_KEYS = {"page_id", "link_data"}
_LINK_KEYS = {"link", "message", "name", "description", "call_to_action"}
_CTA_KEYS = {"type", "value"}
_CTA_VALUE_KEYS = {"link"}
_ADSET_KEYS = {
    "name", "daily_budget", "optimization_goal", "billing_event", "bid_strategy",
    "bid_amount", "targeting", "promoted_object", "attribution_spec", "status",
}
_AD_KEYS = {"name", "status"}
_CAMPAIGN_KEYS = {"name", "objective", "buying_type", "special_ad_categories", "status"}
_STUDY_KEYS = {"business_id", "name", "type", "start_time", "end_time"}
_AUDIENCE_STRATEGY_KEYS = {
    "strategy_key", "label", "detailed_targeting", "meta_targeting_ids",
    "verification_status",
}
_TARGETING_KEYS = {
    "geo_locations", "genders", "age_min", "age_max", "locales",
    "app_install_state", "user_os", "user_device", "targeting_automation",
}
_PROMOTED_OBJECT_KEYS = {"application_id", "object_store_url"}
_ATTRIBUTION_KEYS = {"event_type", "window_days"}
_EXECUTION_POLICY_KEYS = {"live_creation_allowed", "blocked_reason", "required_readback"}
_INVARIANT_KEYS = {
    "base_conditions", "audience_strategy", "advantage_audience",
    "gender_as_suggestion", "age_as_suggestion", "frozen_creative_id",
    "single_variable", "randomization", "copy_versions", "budget_mode",
    "equal_daily_budget_usd", "bid_strategy", "cost_cap_usd",
    "optimization_goal", "placement", "attribution",
}
_BASE_CONDITION_KEYS = {
    "country", "country_label", "gender", "gender_label", "age_min", "age_max",
    "language", "language_label",
}
_GUARDRAIL_KEYS = {"version", "ctr_floor", "zero_install_spend", "high_cpi"}
_CTR_RULE_KEYS = {"minimum_impressions", "minimum_ctr", "action"}
_ZERO_INSTALL_RULE_KEYS = {
    "minimum_attribution_hours", "spend_limit_usd", "maximum_installs", "action",
}
_HIGH_CPI_RULE_KEYS = {"minimum_installs", "maximum_cpi_usd", "action"}
_PREFLIGHT_KEYS = {
    "preflight_id", "launch_id", "source", "status", "account_id", "account_name",
    "business_id", "country", "test_variable", "strategy_keys", "targeting_ids",
    "delivery_estimates", "intersection_estimate", "overlap_ratio", "checked_at",
    "expires_at", "start_time", "end_time", "meta_writes_performed",
}

# These fields identify a Cell or a Meta object but do not change its business
# configuration.  Only these and primary text may differ between the two cells.
_CELL_IDENTITY_KEYS = {
    "cell_key", "experiment_id", "experiment_code", "role", "study_cell_name",
    "copy_version_id", "copy_hypothesis",
}
_NAME_PATHS = (
    ("steps", "CREATIVE_CREATE", "name"),
    ("steps", "ADSET_CREATE", "name"),
    ("steps", "AD_CREATE", "name"),
)
_MESSAGE_PATH = ("steps", "CREATIVE_CREATE", "object_story_spec", "link_data", "message")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_AUTOMATION_ACTORS = {
    "growth-autopilot", "growth-autopilot-recovery", "internal-system", "system",
}


class _CompileFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> None:
    raise _CompileFailure(code)


def _aware_datetime(value: Any, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        _fail(code)
    if parsed.tzinfo is None:
        _fail(code)
    return parsed.astimezone(timezone.utc)


def _strict_value(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail("CANONICAL_VALUE_INVALID")
        return
    if isinstance(value, str):
        try:
            value.encode("utf-8", "strict")
        except UnicodeError:
            _fail("CANONICAL_STRING_INVALID")
        if value != value.strip() or "\r" in value or any(ord(char) < 32 for char in value):
            _fail("CANONICAL_STRING_INVALID")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _strict_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key != key.strip() or not key or any(ord(char) < 32 for char in key):
                _fail("CANONICAL_KEY_INVALID")
            _strict_value(item, f"{path}.{key}")
        return
    _fail("CANONICAL_VALUE_INVALID")


def canonical_json(value: Any) -> str:
    _strict_value(value)
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError):
        _fail("CANONICAL_VALUE_INVALID")
    raise AssertionError("unreachable")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def is_primary_text_only_plan(plan: Mapping[str, Any] | None) -> bool:
    value = dict(plan or {})
    raw_receipt = value.get("compiler_receipt")
    receipt = raw_receipt if isinstance(raw_receipt, dict) else {}
    return bool(
        str(value.get("experiment_type") or "").upper() == "COPY_ONLY"
        or str(value.get("test_variable") or "").lower() == "copy_variant"
        or str(value.get("plan_version") or "") == "NEW_ACCOUNT_COPY_BATCH_V1"
        or str(receipt.get("compiler_version") or "") == COMPILER_VERSION
    )


def _exact_keys(value: Any, expected: set[str], code: str) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        _fail(code)
    return value


def _get_path(value: Mapping[str, Any], path: Iterable[str]) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict) or key not in current:
            _fail("PRIMARY_TEXT_PATH_MISSING")
        current = current[key]
    return current


def _remove_path(value: Dict[str, Any], path: Iterable[str]) -> None:
    parts = tuple(path)
    current: Any = value
    for key in parts[:-1]:
        if not isinstance(current, dict) or key not in current:
            _fail("PLAN_SCHEMA_INVALID")
        current = current[key]
    if not isinstance(current, dict) or parts[-1] not in current:
        _fail("PLAN_SCHEMA_INVALID")
    del current[parts[-1]]


def _validate_cell_schema(cell: Any) -> Dict[str, Any]:
    value = _exact_keys(cell, _CELL_KEYS, "UNKNOWN_OR_MISSING_CELL_FIELD")
    steps = _exact_keys(value.get("steps"), _STEP_KEYS, "UNKNOWN_OR_MISSING_STEP")
    _exact_keys(steps.get("IMAGE_UPLOAD"), _IMAGE_KEYS, "IMAGE_UPLOAD_SCHEMA_INVALID")
    creative = _exact_keys(
        steps.get("CREATIVE_CREATE"), _CREATIVE_KEYS, "CREATIVE_CREATE_SCHEMA_INVALID",
    )
    story = _exact_keys(
        creative.get("object_story_spec"), _STORY_KEYS, "OBJECT_STORY_SPEC_SCHEMA_INVALID",
    )
    link = _exact_keys(story.get("link_data"), _LINK_KEYS, "LINK_DATA_SCHEMA_INVALID")
    cta = _exact_keys(link.get("call_to_action"), _CTA_KEYS, "CALL_TO_ACTION_SCHEMA_INVALID")
    _exact_keys(cta.get("value"), _CTA_VALUE_KEYS, "CALL_TO_ACTION_VALUE_SCHEMA_INVALID")
    audience = _exact_keys(
        value.get("audience_strategy"), _AUDIENCE_STRATEGY_KEYS,
        "AUDIENCE_STRATEGY_SCHEMA_INVALID",
    )
    if str(audience.get("strategy_key") or "").upper() != "BROAD":
        _fail("AUDIENCE_NOT_BROAD")
    adset = _exact_keys(steps.get("ADSET_CREATE"), _ADSET_KEYS, "ADSET_CREATE_SCHEMA_INVALID")
    targeting = _exact_keys(adset.get("targeting"), _TARGETING_KEYS, "TARGETING_SCHEMA_INVALID")
    _exact_keys(targeting.get("geo_locations"), {"countries", "location_types"}, "GEO_SCHEMA_INVALID")
    _exact_keys(targeting.get("targeting_automation"), {"advantage_audience"}, "TARGETING_AUTOMATION_SCHEMA_INVALID")
    _exact_keys(adset.get("promoted_object"), _PROMOTED_OBJECT_KEYS, "PROMOTED_OBJECT_SCHEMA_INVALID")
    attribution = adset.get("attribution_spec")
    if not isinstance(attribution, list) or not attribution:
        _fail("ATTRIBUTION_SPEC_INVALID")
    for item in attribution:
        _exact_keys(item, _ATTRIBUTION_KEYS, "ATTRIBUTION_SPEC_INVALID")
    _exact_keys(steps.get("AD_CREATE"), _AD_KEYS, "AD_CREATE_SCHEMA_INVALID")
    return value


def _projection(cell: Dict[str, Any]) -> Dict[str, Any]:
    projected = deepcopy(cell)
    for key in _CELL_IDENTITY_KEYS:
        projected.pop(key, None)
    for path in (*_NAME_PATHS, _MESSAGE_PATH):
        _remove_path(projected, path)
    return projected


def _base_receipt(plan: Mapping[str, Any]) -> Dict[str, Any]:
    core = deepcopy(dict(plan or {}))
    core.pop("compiler_receipt", None)
    return {
        "status": "FAIL",
        "compiler_version": COMPILER_VERSION,
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "plan_core_hash": canonical_hash(core),
        "invariant_projection_hash": "",
        "cell_primary_text_hashes": {},
        "changed_paths": [],
        "reason_codes": [],
    }


def compile_primary_text_only_plan(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a deterministic PASS/FAIL receipt without mutating ``plan``."""

    receipt = {
        "status": "FAIL", "compiler_version": COMPILER_VERSION,
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "plan_core_hash": "", "invariant_projection_hash": "",
        "cell_primary_text_hashes": {}, "changed_paths": [],
        "reason_codes": [],
    }
    try:
        if not isinstance(plan, Mapping):
            _fail("PLAN_NOT_OBJECT")
        receipt = _base_receipt(plan)
        core = deepcopy(dict(plan or {}))
        core.pop("compiler_receipt", None)
        _exact_keys(core, _TOP_LEVEL_KEYS - {"compiler_receipt"}, "UNKNOWN_OR_MISSING_PLAN_FIELD")
        if str(core.get("action_type") or "").upper() != "CREATE_PAUSED_AD":
            _fail("ACTION_TYPE_NOT_ALLOWED")
        if str(core.get("experiment_type") or "").upper() != "COPY_ONLY":
            _fail("EXPERIMENT_TYPE_NOT_COPY_ONLY")
        if str(core.get("unique_variable") or "").upper() != "PRIMARY_TEXT":
            _fail("UNIQUE_VARIABLE_NOT_PRIMARY_TEXT")
        if str(core.get("test_variable") or "").lower() != "copy_variant":
            _fail("TEST_VARIABLE_NOT_COPY_VARIANT")
        if str(core.get("plan_version") or "") != "NEW_ACCOUNT_COPY_BATCH_V1":
            _fail("PLAN_VERSION_INVALID")
        if str(core.get("sdk_contract_version") or "") != "gle-meta-sdk-v1":
            _fail("SDK_CONTRACT_VERSION_INVALID")
        if core.get("max_write_requests") != 10:
            _fail("WRITE_BUDGET_INVALID")
        if str(core.get("target_object_type") or "").upper() != "LAUNCH":
            _fail("TARGET_TYPE_NOT_LAUNCH")
        if (
            not str(core.get("plan_id") or "")
            or not str(core.get("launch_id") or "")
            or str(core.get("target_object_id") or "") != str(core.get("launch_id") or "")
            or not str(core.get("target_account_id") or "")
        ):
            _fail("PLAN_IDENTITY_INVALID")
        campaign = _exact_keys(core.get("campaign"), _CAMPAIGN_KEYS, "CAMPAIGN_SCHEMA_INVALID")
        if (
            str(campaign.get("status") or "").upper() != "PAUSED"
            or str(campaign.get("objective") or "") != "OUTCOME_APP_PROMOTION"
            or str(campaign.get("buying_type") or "") != "AUCTION"
            or campaign.get("special_ad_categories") != []
        ):
            _fail("OBJECTS_NOT_PAUSED")
        study = _exact_keys(core.get("study"), _STUDY_KEYS, "STUDY_SCHEMA_INVALID")
        if (
            str(study.get("type") or "").upper() != "SPLIT_TEST"
            or not all(str(study.get(key) or "").strip() for key in ("business_id", "name", "start_time", "end_time"))
        ):
            _fail("STUDY_NOT_SPLIT_TEST")
        study_start = _aware_datetime(study.get("start_time"), "STUDY_TIME_INVALID")
        study_end = _aware_datetime(study.get("end_time"), "STUDY_TIME_INVALID")
        if study_start >= study_end:
            _fail("STUDY_TIME_INVALID")
        policy = _exact_keys(
            core.get("execution_policy"), _EXECUTION_POLICY_KEYS,
            "EXECUTION_POLICY_SCHEMA_INVALID",
        )
        if type(policy.get("live_creation_allowed")) is not bool:
            _fail("EXECUTION_POLICY_INVALID")
        invariants = _exact_keys(core.get("invariants"), _INVARIANT_KEYS, "INVARIANTS_SCHEMA_INVALID")
        _exact_keys(
            invariants.get("base_conditions"), _BASE_CONDITION_KEYS,
            "BASE_CONDITIONS_SCHEMA_INVALID",
        )
        _exact_keys(
            invariants.get("audience_strategy"), _AUDIENCE_STRATEGY_KEYS,
            "AUDIENCE_STRATEGY_SCHEMA_INVALID",
        )
        if (
            str(invariants.get("single_variable") or "") != "copy_variant"
            or str(invariants.get("randomization") or "") != "META_SPLIT_TEST_REQUIRED"
        ):
            _fail("INVARIANTS_INVALID")
        guardrails = _exact_keys(
            core.get("delivery_guardrails"), _GUARDRAIL_KEYS,
            "DELIVERY_GUARDRAILS_SCHEMA_INVALID",
        )
        _exact_keys(guardrails.get("ctr_floor"), _CTR_RULE_KEYS, "CTR_RULE_SCHEMA_INVALID")
        _exact_keys(
            guardrails.get("zero_install_spend"), _ZERO_INSTALL_RULE_KEYS,
            "ZERO_INSTALL_RULE_SCHEMA_INVALID",
        )
        _exact_keys(guardrails.get("high_cpi"), _HIGH_CPI_RULE_KEYS, "HIGH_CPI_RULE_SCHEMA_INVALID")
        _exact_keys(core.get("evaluation_window"), {"checkpoints"}, "EVALUATION_WINDOW_SCHEMA_INVALID")
        preflight = _exact_keys(
            core.get("audience_preflight"), _PREFLIGHT_KEYS,
            "AUDIENCE_PREFLIGHT_SCHEMA_INVALID",
        )
        delivery_estimates = preflight.get("delivery_estimates")
        if (
            not isinstance(preflight.get("strategy_keys"), list)
            or not isinstance(preflight.get("targeting_ids"), list)
            or not isinstance(delivery_estimates, dict)
            or not isinstance(preflight.get("intersection_estimate"), dict)
        ):
            _fail("AUDIENCE_PREFLIGHT_SCHEMA_INVALID")
        if (
            str(preflight.get("status") or "").upper() != "VERIFIED"
            or str(preflight.get("source") or "") != "meta_graph_read_only"
            or str(preflight.get("test_variable") or "") != "copy_variant"
            or list(preflight.get("strategy_keys") or []) != ["BROAD", "BROAD"]
            or preflight.get("meta_writes_performed") is not False
            or set(delivery_estimates) != {"C1", "C2"}
            or not all(
                str(preflight.get(key) or "").strip()
                for key in ("preflight_id", "business_id", "checked_at", "expires_at", "start_time", "end_time")
            )
        ):
            _fail("AUDIENCE_PREFLIGHT_NOT_VERIFIED")
        expires_at = str(core.get("expires_at") or "")
        try:
            parsed_expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            _fail("APPROVAL_TTL_REQUIRED")
        if parsed_expiry.tzinfo is None:
            _fail("APPROVAL_TTL_REQUIRED")
        cells = list(core.get("cells") or [])
        if len(cells) != 2:
            _fail("EXACTLY_TWO_CELLS_REQUIRED")
        expected_cells = (("C1", "BASELINE"), ("C2", "CHALLENGER"))
        projections = []
        texts: Dict[str, str] = {}
        changed_paths = []
        cell_experiment_ids = []
        experiment_codes = set()
        study_cell_names = set()
        copy_version_ids = set()
        cell_copy_versions = []
        cell_benchmark_versions = set()
        frozen_creative_id = str(core.get("frozen_creative_id") or "").strip()
        if not frozen_creative_id or str(invariants.get("frozen_creative_id") or "").strip() != frozen_creative_id:
            _fail("FROZEN_CREATIVE_ID_MISMATCH")
        baseline_cell_id = ""
        for index, raw_cell in enumerate(cells):
            cell = _validate_cell_schema(raw_cell)
            key = str(cell.get("cell_key") or "").strip().upper()
            role = str(cell.get("role") or "").strip().upper()
            if (key, role) != expected_cells[index]:
                _fail("CELL_KEY_ROLE_BINDING_INVALID")
            if cell.get("allocation_percent") != 50:
                _fail("ALLOCATION_NOT_50_50")
            if str(dict(cell["steps"]["ADSET_CREATE"]).get("status") or "").upper() != "PAUSED":
                _fail("OBJECTS_NOT_PAUSED")
            adset = dict(cell["steps"]["ADSET_CREATE"])
            if (
                str(adset.get("optimization_goal") or "") != "APP_INSTALLS"
                or str(adset.get("billing_event") or "") != "IMPRESSIONS"
                or str(adset.get("bid_strategy") or "") != "COST_CAP"
                or dict(adset.get("targeting") or {}).get("targeting_automation")
                != {"advantage_audience": 0}
            ):
                _fail("ADSET_GOLDEN_PATH_INVALID")
            if str(dict(cell["steps"]["AD_CREATE"]).get("status") or "").upper() != "PAUSED":
                _fail("OBJECTS_NOT_PAUSED")
            text = _get_path(cell, _MESSAGE_PATH)
            if not isinstance(text, str) or not text:
                _fail("PRIMARY_TEXT_REQUIRED")
            texts[key] = canonical_hash(text)
            experiment_id = str(cell.get("experiment_id") or "")
            if not experiment_id or experiment_id in cell_experiment_ids:
                _fail("CELL_EXPERIMENT_ID_INVALID")
            cell_experiment_ids.append(experiment_id)
            experiment_code = str(cell.get("experiment_code") or "").strip()
            study_cell_name = str(cell.get("study_cell_name") or "").strip()
            copy_version_id = str(cell.get("copy_version_id") or "").strip()
            if not experiment_code or experiment_code in experiment_codes:
                _fail("CELL_EXPERIMENT_CODE_INVALID")
            if not study_cell_name or study_cell_name in study_cell_names:
                _fail("STUDY_CELL_NAME_INVALID")
            if not copy_version_id or copy_version_id in copy_version_ids:
                _fail("COPY_VERSION_ID_INVALID")
            experiment_codes.add(experiment_code)
            study_cell_names.add(study_cell_name)
            copy_version_ids.add(copy_version_id)
            cell_copy_versions.append(copy_version_id)
            benchmark_version = str(cell.get("copy_benchmark_version") or "").strip()
            if not benchmark_version:
                _fail("COPY_BENCHMARK_VERSION_INVALID")
            cell_benchmark_versions.add(benchmark_version)
            if (
                str(cell.get("frozen_creative_id") or "").strip() != frozen_creative_id
                or str(dict(cell.get("steps") or {}).get("IMAGE_UPLOAD", {}).get("image_id") or "").strip()
                != frozen_creative_id
            ):
                _fail("FROZEN_CREATIVE_ID_MISMATCH")
            asset_sha256 = str(cell.get("asset_sha256") or "").strip().lower()
            if not _HASH_RE.fullmatch(asset_sha256):
                _fail("ASSET_SHA256_INVALID")
            if role == "BASELINE":
                baseline_cell_id = experiment_id
            changed_paths.append(
                f"cells[{index}].steps.CREATIVE_CREATE.object_story_spec.link_data.message"
            )
            projections.append(_projection(cell))
        if len(set(texts.values())) != 2:
            _fail("PRIMARY_TEXT_NOT_DISTINCT")
        if list(invariants.get("copy_versions") or []) != cell_copy_versions:
            _fail("COPY_VERSIONS_MISMATCH")
        if list(core.get("copy_benchmark_versions") or []) != sorted(cell_benchmark_versions):
            _fail("COPY_BENCHMARK_VERSIONS_MISMATCH")
        base_country = str(dict(invariants.get("base_conditions") or {}).get("country") or "").upper()
        cell_countries = {
            str(country or "").upper()
            for cell in cells
            for country in list(
                dict(dict(dict(cell).get("steps") or {}).get("ADSET_CREATE") or {})
                .get("targeting", {}).get("geo_locations", {}).get("countries", [])
            )
        }
        if not base_country or cell_countries != {base_country}:
            _fail("COUNTRY_REFERENCE_MISMATCH")
        if preflight:
            if (
                str(preflight.get("launch_id") or "") != str(core.get("launch_id") or "")
                or str(preflight.get("account_id") or "").removeprefix("act_")
                != str(core.get("target_account_id") or "").removeprefix("act_")
                or str(preflight.get("country") or "").upper() != base_country
                or str(preflight.get("business_id") or "") != str(study.get("business_id") or "")
                or str(preflight.get("start_time") or "") != str(study.get("start_time") or "")
                or str(preflight.get("end_time") or "") != str(study.get("end_time") or "")
            ):
                _fail("PREFLIGHT_REFERENCE_MISMATCH")
        if list(core.get("experiment_ids") or []) != cell_experiment_ids:
            _fail("EXPERIMENT_IDS_MISMATCH")
        if str(core.get("baseline_experiment_id") or "") != baseline_cell_id:
            _fail("BASELINE_EXPERIMENT_ID_MISMATCH")
        if str(core.get("experiment_id") or "") != baseline_cell_id:
            _fail("PLAN_EXPERIMENT_ID_MISMATCH")
        projection_hashes = [canonical_hash(item) for item in projections]
        if len(set(projection_hashes)) != 1:
            _fail("INVARIANT_PROJECTION_MISMATCH")
        receipt.update({
            "status": "PASS",
            "invariant_projection_hash": projection_hashes[0],
            "cell_primary_text_hashes": texts,
            "changed_paths": changed_paths,
            "reason_codes": [],
        })
    except _CompileFailure as exc:
        receipt["status"] = "FAIL"
        receipt["reason_codes"] = [exc.code]
    unsigned = dict(receipt)
    receipt["receipt_hash"] = canonical_hash(unsigned)
    return receipt


def attach_compiler_receipt(plan: Mapping[str, Any]) -> Dict[str, Any]:
    compiled = deepcopy(dict(plan or {}))
    compiled.pop("compiler_receipt", None)
    receipt = compile_primary_text_only_plan(compiled)
    if receipt["status"] != "PASS":
        raise GrowthValidationError(
            f"gle_primary_text_only_compile_failed:{receipt['reason_codes'][0]}"
        )
    compiled["compiler_receipt"] = receipt
    return compiled


def verify_compiler_receipt(plan: Mapping[str, Any]) -> Dict[str, Any]:
    value = dict(plan or {})
    raw_supplied = value.get("compiler_receipt")
    supplied = dict(raw_supplied) if isinstance(raw_supplied, dict) else {}
    if not supplied:
        raise GrowthStateConflict("gle_primary_text_only_compiler_receipt_missing")
    expected = compile_primary_text_only_plan(value)
    if expected["status"] != "PASS":
        raise GrowthStateConflict(
            f"gle_primary_text_only_compile_failed:{expected['reason_codes'][0]}"
        )
    if supplied != expected or not _HASH_RE.fullmatch(str(supplied.get("receipt_hash") or "")):
        raise GrowthStateConflict("gle_primary_text_only_compiler_receipt_mismatch")
    return expected


def require_human_approver(actor: str) -> None:
    normalized = str(actor or "").strip().lower()
    if not re.fullmatch(r"operator:[a-z0-9][a-z0-9._@-]{0,127}", normalized):
        raise GrowthStateConflict("gle_primary_text_only_human_approval_required")


def require_unexpired_approval(expires_at: Any, *, plan_expires_at: Any) -> None:
    approval_value = str(expires_at or "").strip()
    plan_value = str(plan_expires_at or "").strip()
    if not approval_value or approval_value != plan_value:
        raise GrowthStateConflict("gle_primary_text_only_approval_ttl_mismatch")
    try:
        parsed = datetime.fromisoformat(approval_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GrowthStateConflict("gle_primary_text_only_approval_ttl_invalid") from exc
    if parsed.tzinfo is None or parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise GrowthStateConflict("gle_primary_text_only_approval_expired")


def verify_action_binding(
    plan: Mapping[str, Any], *, action_type: Any, target_type: Any,
    target_id: Any, action_scope: Any,
) -> None:
    if (
        str(action_type or "").strip().upper() != str(plan.get("action_type") or "").strip().upper()
        or str(target_type or "").strip().upper()
        != str(plan.get("target_object_type") or "").strip().upper()
        or str(target_id or "").strip() != str(plan.get("target_object_id") or "").strip()
        or str(action_scope or "").strip().upper() != "EXPERIMENT"
    ):
        raise GrowthStateConflict("gle_primary_text_only_action_binding_mismatch")


def assert_server_owned_preflight(conn: Any, plan: Mapping[str, Any]) -> None:
    embedded = dict(plan.get("audience_preflight") or {})
    preflight_id = str(embedded.get("preflight_id") or "").strip()
    row = conn.execute(
        """SELECT preflight_id,launch_id,account_id,business_id,country,
                  strategy_keys_json,evidence_json,evidence_hash,status,checked_at,expires_at
        FROM ad_audience_preflight WHERE preflight_id=?""",
        (preflight_id,),
    ).fetchone()
    stored = dict(row or {})
    stored_evidence = decode_json(stored.get("evidence_json"), {})
    if (
        not stored
        or stored.get("status") != "VERIFIED"
        or not isinstance(stored_evidence, dict)
        or stored_evidence != embedded
        or str(stored.get("evidence_hash") or "") != payload_hash(embedded)
        or str(stored.get("launch_id") or "") != str(embedded.get("launch_id") or "")
        or str(stored.get("account_id") or "").removeprefix("act_")
        != str(embedded.get("account_id") or "").removeprefix("act_")
        or str(stored.get("business_id") or "") != str(embedded.get("business_id") or "")
        or str(stored.get("country") or "").upper() != str(embedded.get("country") or "").upper()
        or str(stored.get("checked_at") or "") != str(embedded.get("checked_at") or "")
        or str(stored.get("expires_at") or "") != str(embedded.get("expires_at") or "")
        or decode_json(stored.get("strategy_keys_json"), []) != embedded.get("strategy_keys")
    ):
        raise GrowthStateConflict("gle_primary_text_only_server_preflight_mismatch")


def _bounded_asset_sha256(image_path: Path) -> str:
    try:
        with image_path.open("rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
                raise GrowthStateConflict("gle_primary_text_only_asset_not_immutable")
            if metadata.st_size > MAX_APPROVED_ASSET_BYTES:
                raise GrowthStateConflict("gle_primary_text_only_asset_too_large")
            digest = hashlib.sha256()
            total = 0
            for chunk in iter(lambda: handle.read(256 * 1024), b""):
                total += len(chunk)
                if total > MAX_APPROVED_ASSET_BYTES:
                    raise GrowthStateConflict("gle_primary_text_only_asset_too_large")
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise GrowthStateConflict("gle_primary_text_only_asset_not_immutable") from exc


def assert_approved_assets_unchanged(plan: Mapping[str, Any], *, conn: Any = None) -> None:
    checked = set()
    for raw_cell in list(plan.get("cells") or []):
        cell = dict(raw_cell or {})
        expected = str(cell.get("asset_sha256") or "").strip().lower()
        image_id = str(cell.get("frozen_creative_id") or "").strip()
        image_path = Path(
            str(dict(cell.get("steps") or {}).get("IMAGE_UPLOAD", {}).get("image_path") or "")
        ).expanduser().resolve()
        identity = (image_id, str(image_path), expected)
        if identity in checked:
            continue
        checked.add(identity)
        if not image_id or not _HASH_RE.fullmatch(expected):
            raise GrowthStateConflict("gle_primary_text_only_asset_not_immutable")
        if conn is not None:
            row = conn.execute(
                """SELECT image_ref,image_hash,review_status
                FROM creative_generated_images i
                WHERE image_id=? AND lower(review_status) IN ('approved','used_in_ad')
                  AND EXISTS (
                    SELECT 1 FROM creative_review_records r
                    WHERE r.image_id=i.image_id AND upper(r.review_status)='APPROVED'
                  ) LIMIT 1""",
                (image_id,),
            ).fetchone()
            approved = dict(row or {})
            approved_path = Path(str(approved.get("image_ref") or "")).expanduser().resolve()
            approved_hash = str(approved.get("image_hash") or "").strip().lower()
            if (
                not approved
                or approved_path != image_path
                or not _HASH_RE.fullmatch(approved_hash)
                or approved_hash != expected
            ):
                raise GrowthStateConflict("gle_primary_text_only_asset_provenance_mismatch")
        actual = _bounded_asset_sha256(image_path)
        if actual != expected:
            raise GrowthStateConflict("gle_primary_text_only_asset_not_immutable")


def assert_phase1_live_permission(plan: Mapping[str, Any], *, conn: Any = None) -> None:
    """Dynamically reload E00 and fail closed before every strict Meta write."""

    if dict(plan.get("execution_policy") or {}).get("live_creation_allowed") is not True:
        raise GrowthStateConflict("gle_primary_text_only_preflight_not_live_ready")
    preflight = dict(plan.get("audience_preflight") or {})
    try:
        checked_at = datetime.fromisoformat(str(preflight.get("checked_at") or "").replace("Z", "+00:00"))
        expires_at = datetime.fromisoformat(str(preflight.get("expires_at") or "").replace("Z", "+00:00"))
        study_start = datetime.fromisoformat(str(dict(plan.get("study") or {}).get("start_time") or "").replace("Z", "+00:00"))
        study_end = datetime.fromisoformat(str(dict(plan.get("study") or {}).get("end_time") or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise GrowthStateConflict("gle_primary_text_only_preflight_not_live_ready") from exc
    now = datetime.now(timezone.utc)
    if (
        checked_at.tzinfo is None
        or expires_at.tzinfo is None
        or study_start.tzinfo is None
        or study_end.tzinfo is None
        or checked_at.astimezone(timezone.utc) < now - timedelta(hours=1)
        or checked_at.astimezone(timezone.utc) > now + timedelta(minutes=5)
        or expires_at.astimezone(timezone.utc) <= now
        or study_start.astimezone(timezone.utc) < now + timedelta(minutes=5)
        or study_end.astimezone(timezone.utc) <= study_start.astimezone(timezone.utc)
    ):
        raise GrowthStateConflict("gle_primary_text_only_preflight_not_live_ready")
    config_path = Path(
        str(os.environ.get("GLE_PHASE1_GOVERNANCE_PATH") or "").strip()
        or Path(__file__).resolve().parents[2] / "config" / "gle_phase1_governance_v1.json"
    )
    try:
        contract = load_governance(config_path)
        permissions = effective_permissions(contract)
    except Exception as exc:
        raise GrowthStateConflict("gle_primary_text_only_governance_unavailable") from exc
    if "CREATE_CANARY_PAUSED" not in permissions.allowed_actions or not permissions.meta_write_allowed:
        raise GrowthStateConflict("gle_primary_text_only_gate_not_pass")
    if conn is not None:
        assert_server_owned_preflight(conn, plan)
    assert_approved_assets_unchanged(plan, conn=conn)
    account_id = str(plan.get("target_account_id") or "").removeprefix("act_")
    allowed_accounts = {
        str(item or "").removeprefix("act_") for item in contract.data["canary"]["account_ids"]
    }
    if account_id not in allowed_accounts:
        raise GrowthStateConflict("gle_primary_text_only_account_not_allowlisted")
    cells = list(plan.get("cells") or [])
    countries = {
        str(country or "").upper()
        for cell in cells
        for country in list(
            dict(dict(dict(cell or {}).get("steps") or {}).get("ADSET_CREATE") or {})
            .get("targeting", {}).get("geo_locations", {}).get("countries", [])
        )
    }
    allowed_markets = {str(item or "").upper() for item in contract.data["canary"]["markets"]}
    if len(countries) != 1 or not countries.issubset(allowed_markets):
        raise GrowthStateConflict("gle_primary_text_only_market_not_allowlisted")
