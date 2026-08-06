from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import re
from typing import Any, Dict, FrozenSet, Optional
from urllib.parse import urlsplit

from app.growth.errors import GrowthValidationError
from app.growth.audience_strategy import assert_strict_targeting
from app.growth.common import payload_hash


@dataclass(frozen=True)
class MetaGraphWritePolicy:
    enabled: bool
    allowed_account_ids: FrozenSet[str]
    allowed_action_types: FrozenSet[str] = frozenset({
        "CREATE_EXPERIMENT", "CREATE_PAUSED_AD", "REPLACE_CREATIVE",
        "INCREASE_BUDGET", "DECREASE_BUDGET", "PAUSE", "PAUSE_AD",
        "PAUSE_ADSET", "REACTIVATE_AD", "SCALE_UP", "REDUCE_BUDGET", "SET_COST_CAP",
    })
    max_budget_change_percent: float = 20.0
    image_root: str = ""
    regional_identity_account_id: str = ""
    regional_beneficiary_id: str = ""
    regional_payer_id: str = ""


_META_GRAPH_API_VERSION = re.compile(r"v[1-9]\d*\.\d+", re.IGNORECASE)


def normalize_meta_graph_endpoint(*, base_url: str, api_version: str) -> tuple[str, str]:
    """Return one canonical Meta Graph endpoint or fail before any network call."""
    version = str(api_version or "").strip().lstrip("/").lower()
    if not _META_GRAPH_API_VERSION.fullmatch(version):
        raise GrowthValidationError("meta_graph_endpoint_version_invalid")
    parsed = urlsplit(str(base_url or "").strip().rstrip("/"))
    if parsed.scheme.lower() != "https":
        raise GrowthValidationError("meta_graph_endpoint_scheme_invalid")
    if parsed.hostname != "graph.facebook.com" or parsed.username or parsed.password:
        raise GrowthValidationError("meta_graph_endpoint_host_invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise GrowthValidationError("meta_graph_endpoint_options_invalid") from exc
    if port not in (None, 443) or parsed.query or parsed.fragment:
        raise GrowthValidationError("meta_graph_endpoint_options_invalid")
    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts and path_parts != [version]:
        raise GrowthValidationError("meta_graph_endpoint_path_invalid")
    return "https://graph.facebook.com", version


def configured_regional_regulation_identities(
    *,
    account_id: str,
    targeting: Dict[str, Any],
    configured_account_id: str,
    beneficiary_id: str,
    payer_id: str,
) -> Dict[str, str]:
    """Return the verified advertiser identity required for BR Ad Sets.

    This is a wire-level regulatory field. It is deliberately injected after
    Plan approval so the immutable business Plan and its hash do not change.
    """
    countries = {
        str(item or "").strip().upper()
        for item in list(dict(targeting or {}).get("geo_locations", {}).get("countries") or [])
    }
    if "BR" not in countries:
        return {}
    normalized_account = str(account_id or "").strip().removeprefix("act_")
    configured_account = str(configured_account_id or "").strip().removeprefix("act_")
    beneficiary = str(beneficiary_id or "").strip()
    payer = str(payer_id or "").strip()
    if (
        normalized_account != configured_account
        or not beneficiary.isdigit()
        or not payer.isdigit()
    ):
        raise GrowthValidationError("meta_regional_regulation_identity_required_for_br")
    return {
        "universal_beneficiary": beneficiary,
        "universal_payer": payer,
    }


class MetaGraphExecutionAdapter:
    """Fail-closed Graph adapter used only after an explicit approved plan.

    The adapter performs each POST once. Timeout and uncertainty handling stays
    in MetaExecutionWorker, which reconciles with GET and never replays a write.
    """

    def __init__(
        self,
        *,
        session: Any,
        access_token: str,
        policy: MetaGraphWritePolicy,
        api_version: str = "v23.0",
        base_url: str = "https://graph.facebook.com",
        timeout_seconds: float = 25.0,
    ) -> None:
        self.session = session
        self.access_token = str(access_token or "").strip()
        self.policy = policy
        self.base_url, self.api_version = normalize_meta_graph_endpoint(
            base_url=base_url or "https://graph.facebook.com",
            api_version=api_version or "v23.0",
        )
        self._endpoint_contract_hash = payload_hash({
            "base_url": self.base_url,
            "api_version": self.api_version,
        })
        self.timeout_seconds = max(1.0, min(float(timeout_seconds or 25.0), 29.0))
        self.live_writes_enabled = bool(
            policy.enabled and self.access_token and policy.allowed_account_ids and session is not None
        )

    def validate_runtime_configuration(self) -> str:
        base_url, api_version = normalize_meta_graph_endpoint(
            base_url=self.base_url, api_version=self.api_version,
        )
        current_hash = payload_hash({"base_url": base_url, "api_version": api_version})
        if current_hash != self._endpoint_contract_hash:
            raise GrowthValidationError("meta_graph_endpoint_runtime_drift")
        return f"{base_url}/{api_version}"

    def execute_step(
        self, step: str, payload: Dict[str, Any], object_ids: Dict[str, Any],
    ) -> Dict[str, Any]:
        normalized_step = str(step or "").strip().upper()
        account_id = self._validate_approved_payload(payload)
        plan = dict(payload.get("plan") or {})
        base_step, cell_key, step_payload = self._step_context(normalized_step, plan)
        prefix = f"{cell_key.lower()}_" if cell_key else ""
        if base_step == "IMAGE_UPLOAD":
            result = self._upload_image(account_id, step_payload)
            return {"status": "SUCCESS", "meta_object_ids": {f"{prefix}image_hash": result["image_hash"]}}
        if base_step == "CAMPAIGN_CREATE":
            step_payload.setdefault("is_adset_budget_sharing_enabled", False)
            result = self._post_object(account_id, "campaigns", step_payload, force_paused=True)
            return {"status": "SUCCESS", "meta_object_ids": {"campaign_id": result["id"]}}
        if base_step == "CREATIVE_CREATE":
            step_payload = self._creative_payload(step_payload, object_ids, prefix=prefix)
            result = self._post_object(account_id, "adcreatives", step_payload, force_paused=False)
            return {"status": "SUCCESS", "meta_object_ids": {f"{prefix}creative_id": result["id"]}}
        if base_step == "ADSET_CREATE":
            if str(dict(plan.get("invariants") or {}).get("advantage_audience") or "").upper() == "DISABLED":
                country = str(dict(step_payload.get("targeting") or {}).get("geo_locations", {}).get("countries", [""])[0] or "").upper()
                plan_cell = next((
                    dict(item) for item in list(plan.get("cells") or [])
                    if str(dict(item).get("cell_key") or "").upper() == cell_key
                ), {})
                strategy_key = str(dict(plan_cell.get("audience_strategy") or {}).get("strategy_key") or "BROAD")
                assert_strict_targeting(dict(step_payload.get("targeting") or {}), country, strategy_key)
            if not dict(step_payload.get("regional_regulation_identities") or {}):
                identities = configured_regional_regulation_identities(
                    account_id=account_id,
                    targeting=dict(step_payload.get("targeting") or {}),
                    configured_account_id=self.policy.regional_identity_account_id,
                    beneficiary_id=self.policy.regional_beneficiary_id,
                    payer_id=self.policy.regional_payer_id,
                )
                if identities:
                    step_payload["regional_regulation_identities"] = identities
            step_payload["campaign_id"] = self._required_object_id(object_ids, "campaign_id")
            result = self._post_object(account_id, "adsets", step_payload, force_paused=True)
            return {"status": "SUCCESS", "meta_object_ids": {f"{prefix}adset_id": result["id"]}}
        if base_step == "AD_CREATE":
            step_payload["adset_id"] = self._required_object_id(object_ids, f"{prefix}adset_id")
            creative_prefix = (
                "c1_" if str(plan.get("test_variable") or "").lower() == "audience_strategy"
                else prefix
            )
            step_payload["creative"] = {"creative_id": self._required_object_id(object_ids, f"{creative_prefix}creative_id")}
            result = self._post_object(account_id, "ads", step_payload, force_paused=True)
            return {"status": "SUCCESS", "meta_object_ids": {f"{prefix}ad_id": result["id"]}}
        if normalized_step == "STUDY_CREATE":
            study = dict(plan.get("study") or {})
            business_id = str(study.pop("business_id", "") or "").strip()
            if not business_id:
                raise GrowthValidationError("meta_business_id_required_for_split_test")
            for field in ("start_time", "end_time", "observation_end_time", "cooldown_start_time"):
                if study.get(field) not in (None, ""):
                    study[field] = self._study_unix_timestamp(study[field], field=field)
            study_cells = []
            for index, raw_cell in enumerate(list(plan.get("cells") or []), start=1):
                cell = dict(raw_cell or {})
                key = str(cell.get("cell_key") or f"C{index}").strip().lower()
                study_cells.append({
                    "name": str(cell.get("study_cell_name") or f"{key.upper()} audience cell"),
                    "treatment_percentage": int(cell.get("allocation_percent") or 50),
                    "control_percentage": 0,
                    "adsets": [self._required_object_id(object_ids, f"{key}_adset_id")],
                })
            study["cells"] = study_cells
            result = self._post_business_study(business_id, study)
            return {"status": "SUCCESS", "meta_object_ids": {"study_id": result["id"]}}
        if normalized_step == "BUDGET_UPDATE":
            target_id = self._target_id(payload, plan)
            before = dict(plan.get("before_json") or {})
            after = dict(plan.get("after_json") or {})
            budget_field = str(after.get("budget_field") or before.get("budget_field") or "daily_budget")
            if budget_field not in {"daily_budget", "lifetime_budget"}:
                raise GrowthValidationError("unsupported_budget_field")
            self._assert_before_value(target_id, "budget", before.get("budget"), graph_field=budget_field)
            result = self._post_existing(target_id, {budget_field: after.get("budget")})
            return {"status": "SUCCESS", "meta_object_ids": {"target_id": target_id}, "result": result}
        if normalized_step == "BID_STRATEGY_UPDATE":
            target_id = self._target_id(payload, plan)
            before = dict(plan.get("before_json") or {})
            after = dict(plan.get("after_json") or {})
            expected_before = str(before.get("bid_strategy") or "").strip().upper()
            response = self.session.get(
                self._url(target_id),
                params={"access_token": self.access_token, "fields": "id,bid_strategy,bid_amount"},
                timeout=self.timeout_seconds,
            )
            current = self._response_json(response)
            if str(current.get("id") or "") != target_id:
                raise GrowthValidationError("meta_object_not_confirmed")
            if str(current.get("bid_strategy") or "").strip().upper() != expected_before:
                raise GrowthValidationError("meta_bid_strategy_drift")
            expected_before_amount = before.get("bid_amount")
            if expected_before_amount not in (None, "") and str(current.get("bid_amount")) != str(expected_before_amount):
                raise GrowthValidationError("meta_bid_amount_drift")
            bid_strategy = str(after.get("bid_strategy") or "").strip().upper()
            bid_amount = int(after.get("bid_amount") or 0)
            if bid_strategy != "COST_CAP" or bid_amount < 1:
                raise GrowthValidationError("invalid_cost_cap_contract")
            result = self._post_existing(target_id, {"bid_strategy": bid_strategy, "bid_amount": bid_amount})
            return {"status": "SUCCESS", "meta_object_ids": {"adset_id": target_id}, "result": result}
        if base_step == "STATUS_UPDATE" or base_step in {
            "CAMPAIGN_STATUS_UPDATE", "ADSET_STATUS_UPDATE", "AD_STATUS_UPDATE",
        }:
            target_id = str(step_payload.get("target_id") or self._target_id(payload, plan)).strip()
            before = dict(plan.get("before_json") or {})
            after = dict(plan.get("after_json") or {})
            before_status = step_payload.get("before_status", before.get("status"))
            status = str(step_payload.get("status") or after.get("status") or "").strip().upper()
            if status not in {"ACTIVE", "PAUSED"}:
                raise GrowthValidationError("unsupported_target_status")
            object_key = str(step_payload.get("object_key") or "target_id").strip()
            if (
                object_key not in {"target_id", "campaign_id", "adset_id", "ad_id"}
                and not re.fullmatch(r"c\d+_(?:adset|ad)_id", object_key)
            ):
                raise GrowthValidationError("unsupported_status_object_key")
            current_response = self.session.get(
                self._url(target_id),
                params={"access_token": self.access_token, "fields": "id,status"},
                timeout=self.timeout_seconds,
            )
            current = self._response_json(current_response)
            current_status = str(current.get("status") or "").strip().upper()
            if str(current.get("id") or "").strip() != target_id:
                raise GrowthValidationError("meta_object_not_confirmed")
            if current_status == status:
                return {
                    "status": "SUCCESS", "meta_object_ids": {object_key: target_id},
                    "result": {"id": target_id, "status": status, "already_target_status": True},
                }
            if current_status != str(before_status or "").strip().upper():
                raise GrowthValidationError("meta_before_value_changed")
            result = self._post_existing(target_id, {"status": status})
            return {"status": "SUCCESS", "meta_object_ids": {object_key: target_id}, "result": result}
        if normalized_step == "AD_CREATIVE_UPDATE":
            target_id = self._target_id(payload, plan)
            creative_id = str(object_ids.get("creative_id") or "").strip()
            if not creative_id:
                raise GrowthValidationError("replacement_creative_id_missing")
            before = dict(plan.get("before_json") or {})
            self._assert_before_value(target_id, "creative_id", before.get("creative_id"))
            result = self._post_existing(target_id, {"creative": {"creative_id": creative_id}})
            return {"status": "SUCCESS", "meta_object_ids": {"target_id": target_id, "creative_id": creative_id}, "result": result}
        raise GrowthValidationError("unsupported_meta_execution_step")

    def verify_step(
        self, step: str, payload: Dict[str, Any], object_ids: Dict[str, Any],
    ) -> Dict[str, Any]:
        self._validate_approved_payload(payload)
        normalized_step = str(step or "").strip().upper()
        plan = dict(payload.get("plan") or {})
        base_step, cell_key, step_payload = self._step_context(normalized_step, plan)
        prefix = f"{cell_key.lower()}_" if cell_key else ""
        delivery_steps = {
            "CAMPAIGN_STATUS_UPDATE", "ADSET_STATUS_UPDATE", "AD_STATUS_UPDATE",
        }
        if normalized_step == "VERIFY" and str(payload.get("action_type") or "").upper() == "REACTIVATE_AD":
            statuses: Dict[str, str] = {}
            effective_statuses: Dict[str, str] = {}
            for step_name, detail in self._delivery_step_contexts(plan):
                object_key = str(detail.get("object_key") or "").strip()
                object_id = str(object_ids.get(object_key) or detail.get("target_id") or "").strip()
                expected = str(detail.get("status") or "").strip().upper()
                if not object_key or not object_id or expected not in {"ACTIVE", "PAUSED"}:
                    return {"status": "UNKNOWN", "error": "delivery_path_verification_contract_invalid"}
                response = self.session.get(
                    self._url(object_id),
                    params={"access_token": self.access_token, "fields": "id,status,effective_status"},
                    timeout=self.timeout_seconds,
                )
                body = self._response_json(response)
                actual = str(body.get("status") or "").upper()
                effective = str(body.get("effective_status") or actual).upper()
                statuses[object_key] = actual
                effective_statuses[object_key] = effective
                if str(body.get("id") or "").strip() != object_id or actual != expected:
                    return {
                        "status": "UNKNOWN", "error": "meta_status_verification_mismatch",
                        "object_type": object_key, "actual": actual,
                    }
            return {
                "status": "SUCCESS",
                "meta_object_ids": dict(object_ids),
                "object_statuses": statuses,
                "effective_object_statuses": effective_statuses,
            }
        if base_step in delivery_steps:
            object_key = str(step_payload.get("object_key") or "").strip()
            object_id = str(object_ids.get(object_key) or step_payload.get("target_id") or "").strip()
            expected = str(step_payload.get("status") or "").strip().upper()
            if not object_key or not object_id:
                return {"status": "UNKNOWN", "error": "meta_object_id_missing"}
            response = self.session.get(
                self._url(object_id),
                params={"access_token": self.access_token, "fields": "id,status,effective_status"},
                timeout=self.timeout_seconds,
            )
            body = self._response_json(response)
            actual = str(body.get("status") or "").upper()
            effective = str(body.get("effective_status") or actual).upper()
            if str(body.get("id") or "").strip() != object_id or actual != expected:
                return {"status": "UNKNOWN", "error": "meta_status_verification_mismatch", "actual": actual}
            return {
                "status": "SUCCESS",
                "meta_object_ids": {object_key: object_id},
                "object_statuses": {object_key: actual},
                "effective_object_statuses": {object_key: effective},
            }
        if (
            normalized_step == "VERIFY"
            and str(payload.get("action_type") or "").upper() in {"PAUSE", "PAUSE_AD", "PAUSE_ADSET"}
        ):
            detail = dict(dict(plan.get("steps") or {}).get("STATUS_UPDATE") or {})
            object_key = str(detail.get("object_key") or "target_id").strip()
            object_id = str(object_ids.get(object_key) or detail.get("target_id") or "").strip()
            expected = str(detail.get("status") or dict(plan.get("after_json") or {}).get("status") or "").strip().upper()
            if not object_id or expected not in {"ACTIVE", "PAUSED"}:
                return {"status": "UNKNOWN", "error": "delivery_status_verification_contract_invalid"}
            response = self.session.get(
                self._url(object_id),
                params={"access_token": self.access_token, "fields": "id,status,effective_status"},
                timeout=self.timeout_seconds,
            )
            body = self._response_json(response)
            actual = str(body.get("status") or "").upper()
            if str(body.get("id") or "").strip() != object_id or actual != expected:
                return {"status": "UNKNOWN", "error": "meta_status_verification_mismatch", "actual": actual}
            return {
                "status": "SUCCESS",
                "meta_object_ids": {object_key: object_id},
                "object_statuses": {object_key: actual},
            }
        if normalized_step == "VERIFY" and list(plan.get("cells") or []):
            strict_country = str(dict(plan.get("invariants") or {}).get("base_conditions", {}).get("country") or "")
            return self._verify_created_objects(
                object_ids, cells=list(plan.get("cells") or []), strict_country=strict_country,
                require_study=str(plan.get("test_variable") or "").lower() in {"audience_strategy", "copy_variant"},
            )
        keys = {
            "IMAGE_UPLOAD": (f"{prefix}image_hash",),
            "CAMPAIGN_CREATE": ("campaign_id",),
            "CREATIVE_CREATE": (f"{prefix}creative_id",),
            "ADSET_CREATE": (f"{prefix}adset_id",),
            "AD_CREATE": (f"{prefix}ad_id",),
            "BUDGET_UPDATE": ("target_id",),
            "BID_STRATEGY_UPDATE": ("adset_id", "target_id"),
            "STATUS_UPDATE": ("target_id",),
            "AD_CREATIVE_UPDATE": ("target_id",),
            "VERIFY": ("target_id", "ad_id", "adset_id", "campaign_id", "creative_id"),
        }.get(base_step if normalized_step != "VERIFY" else normalized_step, ())
        if normalized_step == "VERIFY" and str(payload.get("action_type") or "").upper() in {"CREATE_EXPERIMENT", "CREATE_PAUSED_AD"}:
            strict_country = str(dict(plan.get("invariants") or {}).get("base_conditions", {}).get("country") or "")
            return self._verify_created_objects(object_ids, strict_country=strict_country)
        object_key = next((key for key in keys if str(object_ids.get(key) or "").strip()), "")
        if not object_key:
            return {"status": "UNKNOWN", "error": "meta_object_id_missing"}
        if object_key.endswith("image_hash"):
            return {"status": "SUCCESS", "meta_object_ids": {object_key: object_ids[object_key]}}
        object_id = str(object_ids[object_key]).strip()
        fields = {
            "CAMPAIGN_CREATE": "id,status,effective_status",
            "ADSET_CREATE": "id,status,effective_status,daily_budget,lifetime_budget,targeting,bid_strategy,bid_amount",
            "AD_CREATE": "id,status,effective_status,creative",
            "CREATIVE_CREATE": "id",
        }.get(base_step, "id,status,effective_status,daily_budget,lifetime_budget,creative,bid_strategy,bid_amount")
        response = self.session.get(
            self._url(object_id),
            params={"access_token": self.access_token, "fields": fields},
            timeout=self.timeout_seconds,
        )
        body = self._response_json(response)
        if base_step == "ADSET_CREATE" and step_payload.get("bid_strategy"):
            if str(body.get("bid_strategy") or "").upper() != str(step_payload.get("bid_strategy") or "").upper():
                return {"status": "UNKNOWN", "error": "meta_bid_strategy_verification_mismatch", "actual": body.get("bid_strategy")}
            if step_payload.get("bid_amount") not in (None, "") and str(body.get("bid_amount")) != str(step_payload.get("bid_amount")):
                return {"status": "UNKNOWN", "error": "meta_bid_amount_verification_mismatch", "actual": body.get("bid_amount")}
        if str(body.get("id") or "").strip() != object_id:
            return {"status": "UNKNOWN", "error": "meta_object_not_confirmed"}
        if base_step == "AD_CREATIVE_UPDATE" or (
            normalized_step == "VERIFY"
            and str(payload.get("action_type") or "").upper() == "REPLACE_CREATIVE"
        ):
            actual_creative_id = str(dict(body.get("creative") or {}).get("id") or "")
            expected_creative_id = str(object_ids.get("creative_id") or "")
            if not expected_creative_id or actual_creative_id != expected_creative_id:
                return {
                    "status": "UNKNOWN", "error": "meta_creative_verification_mismatch",
                    "actual": actual_creative_id,
                }
        if base_step in {"CAMPAIGN_CREATE", "ADSET_CREATE", "AD_CREATE"}:
            status = str(body.get("status") or body.get("effective_status") or "").upper()
            if status != "PAUSED":
                return {
                    "status": "UNKNOWN", "error": "meta_object_not_paused",
                    "object_type": object_key, "actual": status,
                }
        after = dict(plan.get("after_json") or {})
        if normalized_step in {"BID_STRATEGY_UPDATE", "VERIFY"} and after.get("bid_strategy"):
            if str(body.get("bid_strategy") or "").upper() != str(after.get("bid_strategy") or "").upper():
                return {"status": "UNKNOWN", "error": "meta_bid_strategy_verification_mismatch", "actual": body.get("bid_strategy")}
            if str(body.get("bid_amount")) != str(after.get("bid_amount")):
                return {"status": "UNKNOWN", "error": "meta_bid_amount_verification_mismatch", "actual": body.get("bid_amount")}
        if normalized_step in {"BUDGET_UPDATE", "VERIFY"} and after.get("budget") is not None:
            budget_field = str(after.get("budget_field") or dict(plan.get("before_json") or {}).get("budget_field") or "daily_budget")
            if str(body.get(budget_field)) != str(after.get("budget")):
                return {"status": "UNKNOWN", "error": "meta_budget_verification_mismatch", "actual": body.get(budget_field)}
        if normalized_step in {"STATUS_UPDATE", "VERIFY"} and after.get("status"):
            if str(body.get("status") or "").upper() != str(after.get("status") or "").upper():
                return {"status": "UNKNOWN", "error": "meta_status_verification_mismatch", "actual": body.get("status")}
        return {"status": "SUCCESS", "meta_object_ids": {object_key: object_id}}

    def _validate_approved_payload(self, payload: Dict[str, Any]) -> str:
        if not self.live_writes_enabled:
            raise GrowthValidationError("real_meta_writes_disabled")
        account_id = str(payload.get("account_id") or "").strip().removeprefix("act_")
        if account_id not in self.policy.allowed_account_ids:
            raise GrowthValidationError("meta_account_not_allowlisted")
        approval = dict(payload.get("approval") or {})
        if (
            str(approval.get("status") or "").upper() != "APPROVED"
            or not str(approval.get("approval_id") or "").strip()
            or not str(approval.get("approved_by") or "").strip()
            or not str(approval.get("approved_at") or "").strip()
        ):
            raise GrowthValidationError("meta_write_approval_required")
        plan = dict(payload.get("plan") or {})
        recovery = dict(payload.get("recovery_approval") or {})
        recovery_current = self._recovery_approval_current(recovery, plan)
        action_type = str(payload.get("action_type") or "").strip().upper()
        if action_type not in self.policy.allowed_action_types:
            raise GrowthValidationError("meta_action_not_allowed")
        self._validate_budget_limit(payload)
        if not isinstance(payload.get("plan"), dict):
            raise GrowthValidationError("meta_execution_plan_required")
        max_writes = int(plan.get("max_write_requests") or 4)
        cells = list(plan.get("cells") or [])
        expected_writes = {
            "CREATE_EXPERIMENT": 5, "CREATE_PAUSED_AD": 5, "REPLACE_CREATIVE": 2,
            "REACTIVATE_AD": 3,
        }.get(action_type, 1)
        if action_type == "REACTIVATE_AD" and cells:
            if not 2 <= len(cells) <= 4:
                raise GrowthValidationError("meta_delivery_path_count_invalid")
            expected_writes = 1 + 2 * len(cells)
        if action_type == "CREATE_PAUSED_AD" and cells:
            if not 2 <= len(cells) <= 4:
                raise GrowthValidationError("meta_batch_cell_count_invalid")
            if str(plan.get("test_variable") or "").lower() == "audience_strategy":
                if len(cells) != 2:
                    raise GrowthValidationError("meta_audience_split_test_requires_two_cells")
                expected_writes = 4 + 2 * len(cells)
            elif str(plan.get("test_variable") or "").lower() == "copy_variant":
                if len(cells) != 2:
                    raise GrowthValidationError("meta_copy_split_test_requires_two_cells")
                expected_writes = 2 + 4 * len(cells)
            else:
                expected_writes = 1 + 4 * len(cells)
        if action_type == "REPLACE_CREATIVE" and all(
            name in dict(plan.get("steps") or {})
            for name in ("IMAGE_UPLOAD", "CREATIVE_CREATE", "AD_CREATIVE_UPDATE")
        ):
            expected_writes = 3
        if max_writes != expected_writes or max_writes > 17:
            raise GrowthValidationError("meta_write_request_limit_invalid")
        return account_id

    def read_ad_review(self, ad_id: str) -> Dict[str, Any]:
        """Read one ad's delivery/review truth without performing a Meta write."""
        normalized = str(ad_id or "").strip()
        if not normalized:
            raise GrowthValidationError("meta_ad_id_required")
        response = self.session.get(
            self._url(normalized),
            params={
                "access_token": self.access_token,
                "fields": "id,name,status,effective_status,creative,ad_review_feedback",
            },
            timeout=self.timeout_seconds,
        )
        body = self._response_json(response)
        if str(body.get("id") or "").strip() != normalized:
            raise GrowthValidationError("meta_object_not_confirmed")
        return {
            "ad_id": normalized,
            "name": str(body.get("name") or ""),
            "configured_status": str(body.get("status") or "").upper(),
            "effective_status": str(body.get("effective_status") or body.get("status") or "").upper(),
            "creative_id": str(dict(body.get("creative") or {}).get("id") or ""),
            "review_feedback": dict(body.get("ad_review_feedback") or {}),
        }

    @classmethod
    def _recovery_approval_current(cls, recovery: Dict[str, Any], plan: Dict[str, Any]) -> bool:
        if (
            str(recovery.get("status") or "").upper() != "APPROVED"
            or not str(recovery.get("source_plan_id") or "").strip()
            or not str(recovery.get("source_execution_task_id") or "").strip()
            or not str(recovery.get("confirmed_by") or "").strip()
            or not str(recovery.get("confirmed_at") or "").strip()
            or str(recovery.get("plan_hash") or "") != payload_hash(plan)
        ):
            return False
        expires_at = str(recovery.get("expires_at") or "").strip()
        return bool(not expires_at or not cls._is_expired(expires_at))

    @staticmethod
    def _is_expired(value: str) -> bool:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GrowthValidationError("meta_execution_expiry_invalid") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc)

    def _validate_budget_limit(self, payload: Dict[str, Any]) -> None:
        plan = dict(payload.get("plan") or {})
        before_json = dict(plan.get("before_json") or {})
        after_json = dict(plan.get("after_json") or {})
        before = payload.get("before_budget", before_json.get("budget"))
        after = payload.get("new_budget", after_json.get("budget"))
        if before in (None, "") or after in (None, ""):
            return
        before_value = float(before)
        after_value = float(after)
        if before_value <= 0:
            raise GrowthValidationError("invalid_before_budget")
        change = abs(after_value - before_value) / before_value * 100.0
        if change > float(self.policy.max_budget_change_percent):
            raise GrowthValidationError("meta_budget_change_limit_exceeded")

    def _upload_image(self, account_id: str, step_payload: Dict[str, Any]) -> Dict[str, Any]:
        image_path = Path(str(step_payload.get("image_path") or "")).expanduser().resolve()
        if not image_path.is_file():
            raise GrowthValidationError("meta_image_file_not_found")
        if self.policy.image_root:
            root = Path(self.policy.image_root).expanduser().resolve()
            if root not in image_path.parents:
                raise GrowthValidationError("meta_image_path_not_allowed")
        with image_path.open("rb") as handle:
            response = self.session.post(
                self._url(f"act_{account_id}/adimages"),
                data={"access_token": self.access_token},
                files={"filename": (image_path.name, handle)},
                timeout=self.timeout_seconds,
            )
        body = self._response_json(response)
        images = dict(body.get("images") or {})
        first = next(iter(images.values()), {})
        image_hash = str(first.get("hash") or body.get("hash") or "").strip()
        if not image_hash:
            raise GrowthValidationError("meta_image_hash_missing")
        return {"image_hash": image_hash}

    def _post_object(
        self, account_id: str, edge: str, step_payload: Dict[str, Any], *, force_paused: bool,
    ) -> Dict[str, Any]:
        body = self._form_payload(step_payload)
        body["access_token"] = self.access_token
        if force_paused:
            body["status"] = "PAUSED"
        response = self.session.post(
            self._url(f"act_{account_id}/{edge}"), data=body, timeout=self.timeout_seconds,
        )
        result = self._response_json(response)
        if not str(result.get("id") or "").strip():
            raise GrowthValidationError("meta_create_id_missing")
        return result

    def _post_business_study(self, business_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = self._form_payload(payload)
        body["access_token"] = self.access_token
        response = self.session.post(
            self._url(f"{business_id}/ad_studies"), data=body, timeout=self.timeout_seconds,
        )
        result = self._response_json(response)
        if not str(result.get("id") or "").strip():
            raise GrowthValidationError("meta_study_id_missing")
        return result

    @staticmethod
    def _study_unix_timestamp(value: Any, *, field: str) -> int:
        """Normalize the immutable Plan timestamp to Meta's integer wire contract."""
        if isinstance(value, bool):
            raise GrowthValidationError(f"meta_study_{field}_invalid")
        if isinstance(value, (int, float)):
            timestamp = int(value)
        else:
            text = str(value or "").strip()
            if text.isdigit():
                timestamp = int(text)
            else:
                try:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise GrowthValidationError(f"meta_study_{field}_invalid") from exc
                if parsed.tzinfo is None:
                    raise GrowthValidationError(f"meta_study_{field}_timezone_required")
                timestamp = int(parsed.astimezone(timezone.utc).timestamp())
        if timestamp <= 0:
            raise GrowthValidationError(f"meta_study_{field}_invalid")
        return timestamp

    def _creative_payload(self, step_payload: Dict[str, Any], object_ids: Dict[str, Any], *, prefix: str = "") -> Dict[str, Any]:
        payload = dict(step_payload)
        story = dict(payload.get("object_story_spec") or {})
        link_data = dict(story.get("link_data") or {})
        link_data["image_hash"] = self._required_object_id(object_ids, f"{prefix}image_hash")
        story["link_data"] = link_data
        payload["object_story_spec"] = story
        return payload

    def _verify_created_objects(
        self, object_ids: Dict[str, Any], *, cells: list[Dict[str, Any]] | None = None,
        strict_country: str = "", require_study: bool = False,
    ) -> Dict[str, Any]:
        required = ["campaign_id"]
        if cells:
            for index, cell in enumerate(cells, start=1):
                prefix = f"{str(dict(cell).get('cell_key') or f'C{index}').lower()}_"
                required.extend((f"{prefix}adset_id", f"{prefix}ad_id"))
                if not require_study or index == 1:
                    required.append(f"{prefix}creative_id")
        else:
            required.extend(("adset_id", "creative_id", "ad_id"))
        if require_study:
            required.append("study_id")
        missing = [key for key in required if not str(object_ids.get(key) or "").strip()]
        if missing:
            return {"status": "UNKNOWN", "error": "meta_object_id_missing", "missing": missing}
        statuses: Dict[str, str] = {}
        for key in required:
            object_id = str(object_ids[key]).strip()
            if key == "study_id":
                fields = "id,name,type,start_time,end_time"
            elif key.endswith("creative_id"):
                fields = "id"
            elif key.endswith("adset_id"):
                fields = "id,status,effective_status,targeting,regional_regulation_identities"
            else:
                fields = "id,status,effective_status"
            response = self.session.get(
                self._url(object_id),
                params={"access_token": self.access_token, "fields": fields},
                timeout=self.timeout_seconds,
            )
            body = self._response_json(response)
            if str(body.get("id") or "").strip() != object_id:
                return {"status": "UNKNOWN", "error": "meta_object_not_confirmed", "object_type": key}
            if key == "study_id":
                continue
            if not key.endswith("creative_id"):
                status = str(body.get("status") or body.get("effective_status") or "").upper()
                statuses[key] = status
                if status != "PAUSED":
                    return {
                        "status": "UNKNOWN", "error": "meta_object_not_paused",
                        "object_type": key, "actual": status,
                    }
            if key.endswith("adset_id") and strict_country:
                targeting = dict(body.get("targeting") or {})
                key_prefix = key.removesuffix("adset_id")
                plan_cell = next((
                    dict(item) for index, item in enumerate(cells or [], start=1)
                    if f"{str(dict(item).get('cell_key') or f'C{index}').lower()}_" == key_prefix
                ), {})
                strategy_key = str(dict(plan_cell.get("audience_strategy") or {}).get("strategy_key") or "BROAD")
                try:
                    assert_strict_targeting(targeting, strict_country, strategy_key)
                except GrowthValidationError as exc:
                    return {
                        "status": "UNKNOWN", "error": str(exc),
                        "object_type": key, "actual_targeting": targeting,
                    }
                if str(strict_country or "").upper() == "BR":
                    expected_identities = {
                        "universal_beneficiary": str(self.policy.regional_beneficiary_id or "").strip(),
                        "universal_payer": str(self.policy.regional_payer_id or "").strip(),
                    }
                    actual_identities = dict(body.get("regional_regulation_identities") or {})
                    if not all(expected_identities.values()) or actual_identities != expected_identities:
                        return {
                            "status": "UNKNOWN",
                            "error": "meta_regional_regulation_identity_verification_mismatch",
                            "object_type": key,
                        }
        if require_study:
            study_id = str(object_ids["study_id"]).strip()
            cell_response = self.session.get(
                self._url(f"{study_id}/cells"),
                params={"access_token": self.access_token, "fields": "id,name,treatment_percentage,control_percentage"},
                timeout=self.timeout_seconds,
            )
            study_cells = list(self._response_json(cell_response).get("data") or [])
            if len(study_cells) != len(cells or []):
                return {"status": "UNKNOWN", "error": "meta_study_cell_count_mismatch"}
            verified_ids = dict(object_ids)
            cells_by_name = {
                str(dict(item).get("name") or "").strip(): dict(item)
                for item in study_cells if str(dict(item).get("name") or "").strip()
            }
            for index, plan_cell in enumerate(cells or [], start=1):
                expected_name = str(dict(plan_cell).get("study_cell_name") or "").strip()
                study_cell = cells_by_name.get(expected_name)
                if not study_cell:
                    return {"status": "UNKNOWN", "error": "meta_study_cell_name_mismatch", "cell": expected_name}
                study_cell_id = str(dict(study_cell).get("id") or "").strip()
                key = str(dict(plan_cell).get("cell_key") or f"C{index}").strip().lower()
                expected_adset = str(object_ids.get(f"{key}_adset_id") or "").strip()
                if not study_cell_id or int(dict(study_cell).get("treatment_percentage") or 0) != int(dict(plan_cell).get("allocation_percent") or 50):
                    return {"status": "UNKNOWN", "error": "meta_study_allocation_mismatch", "cell": key}
                edge_response = self.session.get(
                    self._url(f"{study_cell_id}/adsets"),
                    params={"access_token": self.access_token, "fields": "id"},
                    timeout=self.timeout_seconds,
                )
                bound_ids = {str(item.get("id") or "") for item in list(self._response_json(edge_response).get("data") or [])}
                if bound_ids != {expected_adset}:
                    return {"status": "UNKNOWN", "error": "meta_study_adset_binding_mismatch", "cell": key}
                verified_ids[f"{key}_study_cell_id"] = study_cell_id
            object_ids = verified_ids
        return {"status": "SUCCESS", "meta_object_ids": dict(object_ids), "object_statuses": statuses}

    @staticmethod
    def _delivery_step_contexts(plan: Dict[str, Any]) -> list[tuple[str, Dict[str, Any]]]:
        contexts: list[tuple[str, Dict[str, Any]]] = []
        campaign = dict(dict(plan.get("steps") or {}).get("CAMPAIGN_STATUS_UPDATE") or {})
        if campaign:
            contexts.append(("CAMPAIGN_STATUS_UPDATE", campaign))
        cells = list(plan.get("cells") or [])
        if cells:
            for index, raw_cell in enumerate(cells, start=1):
                cell = dict(raw_cell or {})
                cell_key = str(cell.get("cell_key") or f"C{index}").strip().upper()
                steps = dict(cell.get("steps") or {})
                for base_step in ("ADSET_STATUS_UPDATE", "AD_STATUS_UPDATE"):
                    detail = dict(steps.get(base_step) or {})
                    if detail:
                        contexts.append((f"{cell_key}_{base_step}", detail))
        else:
            steps = dict(plan.get("steps") or {})
            for step_name in ("ADSET_STATUS_UPDATE", "AD_STATUS_UPDATE"):
                detail = dict(steps.get(step_name) or {})
                if detail:
                    contexts.append((step_name, detail))
        return contexts

    @staticmethod
    def _step_context(normalized_step: str, plan: Dict[str, Any]) -> tuple[str, str, Dict[str, Any]]:
        if normalized_step == "CAMPAIGN_CREATE" and list(plan.get("cells") or []):
            return normalized_step, "", dict(plan.get("campaign") or {})
        for index, raw_cell in enumerate(list(plan.get("cells") or []), start=1):
            cell = dict(raw_cell or {})
            cell_key = str(cell.get("cell_key") or f"C{index}").strip().upper()
            prefix = f"{cell_key}_"
            if normalized_step.startswith(prefix):
                base_step = normalized_step[len(prefix):]
                return base_step, cell_key, dict(dict(cell.get("steps") or {}).get(base_step) or {})
        return normalized_step, "", dict(plan.get(normalized_step) or dict(plan.get("steps") or {}).get(normalized_step) or {})

    @staticmethod
    def _required_object_id(object_ids: Dict[str, Any], key: str) -> str:
        value = str(object_ids.get(key) or "").strip()
        if not value:
            raise GrowthValidationError(f"meta_dependency_missing:{key}")
        return value

    @staticmethod
    def _form_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: json.dumps(value, separators=(",", ":"), ensure_ascii=False)
            if isinstance(value, (dict, list)) else value
            for key, value in dict(payload or {}).items()
        }

    def _post_existing(self, object_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        request_body = dict(body)
        request_body["access_token"] = self.access_token
        response = self.session.post(self._url(object_id), data=request_body, timeout=self.timeout_seconds)
        result = self._response_json(response)
        if result.get("success") is not True and str(result.get("id") or object_id).strip() != object_id:
            raise GrowthValidationError("meta_update_not_acknowledged")
        return result

    def _assert_before_value(self, object_id: str, field: str, expected: Any, *, graph_field: str = "") -> None:
        if expected in (None, ""):
            raise GrowthValidationError("meta_before_value_required")
        graph_field = graph_field or ("daily_budget" if field == "budget" else ("creative" if field == "creative_id" else field))
        response = self.session.get(
            self._url(object_id),
            params={"access_token": self.access_token, "fields": f"id,{graph_field}"},
            timeout=self.timeout_seconds,
        )
        body = self._response_json(response)
        actual = body.get(graph_field)
        if field == "creative_id" and isinstance(actual, dict):
            actual = actual.get("id") or actual.get("creative_id")
        if str(actual) != str(expected):
            raise GrowthValidationError("meta_before_value_changed")

    @staticmethod
    def _target_id(payload: Dict[str, Any], plan: Dict[str, Any]) -> str:
        target_id = str(plan.get("target_object_id") or payload.get("target_id") or "").strip()
        if not target_id:
            raise GrowthValidationError("meta_target_id_required")
        return target_id

    def _url(self, path: str) -> str:
        return f"{self.validate_runtime_configuration()}/{str(path).lstrip('/')}"

    @staticmethod
    def _response_json(response: Any) -> Dict[str, Any]:
        body = response.json() if hasattr(response, "json") else {}
        if not isinstance(body, dict):
            raise GrowthValidationError("meta_response_invalid")
        if body.get("error"):
            error = dict(body.get("error") or {})
            code = str(error.get("code") or "unknown")
            subcode = str(error.get("error_subcode") or "unknown")
            raise GrowthValidationError(f"meta_graph_error:{code}:{subcode}")
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        return body
