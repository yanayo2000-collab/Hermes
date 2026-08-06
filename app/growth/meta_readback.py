from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Callable, Dict, Mapping

from app.growth.common import payload_hash
from app.growth.meta_sdk_contract import META_SDK_CONTRACT_VERSION, compare_readback_fields


COPY_ONLY_READ_FIELDS = {
    "campaign": "id,name,objective,buying_type,status,effective_status,special_ad_categories,is_adset_budget_sharing_enabled",
    "adset": "id,name,campaign_id,status,effective_status,daily_budget,lifetime_budget,bid_strategy,bid_amount,billing_event,optimization_goal,promoted_object,targeting,attribution_spec,regional_regulation_identities",
    "creative": "id,name,object_story_spec,image_hash,title,body,call_to_action_type,url_tags,instagram_user_id",
    "ad": "id,name,campaign_id,adset_id,status,effective_status,creative,tracking_specs",
    "study": "id,name,type,start_time,end_time,observation_end_time,cooldown_start_time",
    "study_cells": "id,name,treatment_percentage,control_percentage",
}


def _timestamp(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp())


def _planned_fields(payload: Mapping[str, Any], *, exclude: set[str] | None = None) -> list[str]:
    excluded = set(exclude or set())
    return sorted(str(key) for key in payload if str(key) not in excluded)


class MetaCopyOnlyReadback:
    """Strict GET-only verifier for newly compiled copy-only Plans."""

    def __init__(self, *, get_json: Callable[[str, str], Dict[str, Any]]) -> None:
        self.get_json = get_json

    def verify(self, *, plan: Mapping[str, Any], object_ids: Mapping[str, Any]) -> Dict[str, Any]:
        if str(plan.get("sdk_contract_version") or "") != META_SDK_CONTRACT_VERSION:
            return {"status": "SKIPPED", "reason": "legacy_plan_without_sdk_contract"}
        if str(plan.get("test_variable") or "").lower() != "copy_variant":
            return {"status": "SKIPPED", "reason": "not_copy_only"}

        checks = []
        campaign_id = str(object_ids.get("campaign_id") or "").strip()
        expected_campaign = dict(plan.get("campaign") or {})
        expected_campaign.update({"id": campaign_id, "status": "PAUSED"})
        actual_campaign = self.get_json(campaign_id, COPY_ONLY_READ_FIELDS["campaign"])
        checks.append(compare_readback_fields(
            object_type="campaign", expected=expected_campaign, actual=actual_campaign,
            fields=_planned_fields(expected_campaign),
        ))

        for index, raw_cell in enumerate(list(plan.get("cells") or []), start=1):
            cell = dict(raw_cell or {})
            key = str(cell.get("cell_key") or f"C{index}").strip().lower()
            steps = dict(cell.get("steps") or {})
            image_hash = str(object_ids.get(f"{key}_image_hash") or "").strip()
            creative_id = str(object_ids.get(f"{key}_creative_id") or "").strip()
            adset_id = str(object_ids.get(f"{key}_adset_id") or "").strip()
            ad_id = str(object_ids.get(f"{key}_ad_id") or "").strip()

            expected_creative = dict(steps.get("CREATIVE_CREATE") or {})
            story = dict(expected_creative.get("object_story_spec") or {})
            link_data = dict(story.get("link_data") or {})
            if image_hash:
                link_data["image_hash"] = image_hash
            story["link_data"] = link_data
            expected_creative.update({"id": creative_id, "object_story_spec": story})
            actual_creative = self.get_json(creative_id, COPY_ONLY_READ_FIELDS["creative"])
            checks.append(compare_readback_fields(
                object_type=f"{key}_creative", expected=expected_creative, actual=actual_creative,
                fields=_planned_fields(expected_creative),
            ))

            expected_adset = dict(steps.get("ADSET_CREATE") or {})
            expected_adset.update({"id": adset_id, "campaign_id": campaign_id, "status": "PAUSED"})
            actual_adset = self.get_json(adset_id, COPY_ONLY_READ_FIELDS["adset"])
            checks.append(compare_readback_fields(
                object_type=f"{key}_adset", expected=expected_adset, actual=actual_adset,
                fields=_planned_fields(expected_adset),
            ))

            expected_ad = dict(steps.get("AD_CREATE") or {})
            expected_ad.update({
                "id": ad_id, "adset_id": adset_id, "status": "PAUSED",
                "creative": {"id": creative_id},
            })
            actual_ad = self.get_json(ad_id, COPY_ONLY_READ_FIELDS["ad"])
            checks.append(compare_readback_fields(
                object_type=f"{key}_ad", expected=expected_ad, actual=actual_ad,
                fields=_planned_fields(expected_ad),
            ))

        study_id = str(object_ids.get("study_id") or "").strip()
        expected_study = dict(plan.get("study") or {})
        expected_study.pop("business_id", None)
        expected_study.pop("cells", None)
        expected_study["id"] = study_id
        actual_study = self.get_json(study_id, COPY_ONLY_READ_FIELDS["study"])
        for field in ("start_time", "end_time", "observation_end_time", "cooldown_start_time"):
            if field in expected_study:
                expected_study[field] = _timestamp(expected_study[field])
                actual_study[field] = _timestamp(actual_study.get(field))
        checks.append(compare_readback_fields(
            object_type="study", expected=expected_study, actual=actual_study,
            fields=_planned_fields(expected_study),
        ))

        actual_cells = list(self.get_json(f"{study_id}/cells", COPY_ONLY_READ_FIELDS["study_cells"]).get("data") or [])
        actual_cells_by_name = {str(item.get("name") or ""): dict(item) for item in actual_cells}
        for index, raw_cell in enumerate(list(plan.get("cells") or []), start=1):
            cell = dict(raw_cell or {})
            key = str(cell.get("cell_key") or f"C{index}").strip().lower()
            name = str(cell.get("study_cell_name") or "")
            expected_cell = {
                "name": name,
                "treatment_percentage": int(cell.get("allocation_percent") or 0),
                "control_percentage": 0,
            }
            check = compare_readback_fields(
                object_type=f"{key}_study_cell", expected=expected_cell,
                actual=actual_cells_by_name.get(name, {}), fields=expected_cell.keys(),
            )
            checks.append(check)

        mismatches = [check for check in checks if check["status"] != "VERIFIED"]
        return {
            "status": "SUCCESS" if not mismatches else "UNKNOWN",
            "contract_version": META_SDK_CONTRACT_VERSION,
            "plan_hash": payload_hash(dict(plan)),
            "checks": checks,
            "mismatch_count": len(mismatches),
            "error": "meta_sdk_contract_readback_mismatch" if mismatches else "",
        }
