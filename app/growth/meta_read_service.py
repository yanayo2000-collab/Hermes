from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
from typing import Any, Dict, Iterable, Mapping

from app.growth.errors import GrowthValidationError
from app.growth.meta_sdk_contract import relevant_meta_error_evidence


DEFAULT_INSIGHT_FIELDS = (
    "account_id", "campaign_id", "adset_id", "ad_id", "date_start", "date_stop",
    "spend", "impressions", "clicks", "ctr", "actions", "cost_per_action_type",
)


@dataclass(frozen=True)
class MetaReadPolicy:
    max_poll_attempts: int = 24
    async_range_days: int = 28
    max_range_days: int = 93
    activity_limit: int = 100


class MetaGraphReadService:
    """GET/read-job-only operating surface; it cannot create or mutate ad objects."""

    def __init__(
        self, *, session: Any, access_token: str, base_url: str,
        api_version: str, timeout_seconds: float = 25.0, policy: MetaReadPolicy | None = None,
    ) -> None:
        self.session = session
        self.access_token = str(access_token or "").strip()
        self.base_url = str(base_url or "").rstrip("/")
        self.api_version = str(api_version or "").strip().lstrip("/")
        self.timeout_seconds = max(1.0, min(float(timeout_seconds or 25), 29.0))
        self.policy = policy or MetaReadPolicy()
        if self.base_url != "https://graph.facebook.com" or not self.api_version.startswith("v"):
            raise GrowthValidationError("meta_read_endpoint_invalid")

    def submit_async_insights(
        self, *, account_id: str, since: str, until: str,
        level: str = "ad", fields: Iterable[str] = DEFAULT_INSIGHT_FIELDS,
        time_increment: int = 1,
    ) -> Dict[str, Any]:
        start, stop = self._validated_range(since, until)
        normalized_level = str(level or "").lower()
        if normalized_level not in {"account", "campaign", "adset", "ad"}:
            raise GrowthValidationError("meta_insights_level_invalid")
        body = {
            "access_token": self.access_token,
            "level": normalized_level,
            "fields": ",".join(dict.fromkeys(str(field) for field in fields if str(field))),
            "time_range": json.dumps({"since": start.isoformat(), "until": stop.isoformat()}, separators=(",", ":")),
            "time_increment": int(time_increment),
            "async": "true",
        }
        response = self.session.post(
            self._url(f"act_{str(account_id).removeprefix('act_')}/insights"),
            data=body, timeout=self.timeout_seconds,
        )
        payload = self._response_json(response)
        report_run_id = str(payload.get("report_run_id") or payload.get("id") or "").strip()
        if not report_run_id:
            raise GrowthValidationError("meta_async_report_id_missing")
        return {
            "report_run_id": report_run_id,
            "mode": "ASYNC",
            "since": start.isoformat(),
            "until": stop.isoformat(),
            "level": normalized_level,
            "meta_object_writes": 0,
            "rate_usage": self._rate_usage(response),
        }

    def read_async_status(self, report_run_id: str) -> Dict[str, Any]:
        payload, response = self._get(
            str(report_run_id),
            "id,account_id,async_status,async_percent_completion,is_running,date_start,date_stop,time_completed,error_code,error_subcode,error_message,error_user_title,error_user_msg",
        )
        status = str(payload.get("async_status") or "").strip()
        terminal = status in {"Job Completed", "Job Failed", "Completed", "Failed"}
        return {
            "report_run_id": str(payload.get("id") or report_run_id),
            "status": status,
            "percent_complete": int(payload.get("async_percent_completion") or 0),
            "terminal": terminal,
            "success": status in {"Job Completed", "Completed"},
            "error": relevant_meta_error_evidence({"error": {
                "code": payload.get("error_code"), "error_subcode": payload.get("error_subcode"),
                "message": payload.get("error_message"), "error_user_title": payload.get("error_user_title"),
                "error_user_msg": payload.get("error_user_msg"),
            }}),
            "rate_usage": self._rate_usage(response),
        }

    def read_async_result(self, report_run_id: str, *, limit: int = 500) -> Dict[str, Any]:
        response = self.session.get(
            self._url(f"{str(report_run_id)}/insights"),
            params={"access_token": self.access_token, "limit": max(1, min(int(limit), 1000))},
            timeout=self.timeout_seconds,
        )
        payload = self._response_json(response)
        return {
            "report_run_id": str(report_run_id),
            "data": list(payload.get("data") or []),
            "paging": dict(payload.get("paging") or {}),
            "meta_object_writes": 0,
            "rate_usage": self._rate_usage(response),
        }

    def read_activities(self, *, account_id: str, since: str = "", until: str = "") -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "access_token": self.access_token,
            "fields": "id,event_time,event_type,object_id,object_name,translated_event_type,extra_data,actor_id,actor_name",
            "limit": max(1, min(self.policy.activity_limit, 500)),
        }
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        response = self.session.get(
            self._url(f"act_{str(account_id).removeprefix('act_')}/activities"),
            params=params, timeout=self.timeout_seconds,
        )
        payload = self._response_json(response)
        return {
            "activities": list(payload.get("data") or []),
            "paging": dict(payload.get("paging") or {}),
            "meta_object_writes": 0,
            "rate_usage": self._rate_usage(response),
        }

    def detect_activity_drift(
        self, *, activities: Iterable[Mapping[str, Any]], target_object_ids: Iterable[str],
        approved_actor_ids: Iterable[str], cutoff_at: str,
    ) -> Dict[str, Any]:
        targets = {str(item) for item in target_object_ids if str(item)}
        actors = {str(item) for item in approved_actor_ids if str(item)}
        cutoff = self._parse_datetime(cutoff_at)
        drift = []
        for raw in activities:
            item = dict(raw or {})
            object_id = str(item.get("object_id") or "")
            if targets and object_id not in targets:
                continue
            event_at = self._parse_datetime(str(item.get("event_time") or ""))
            if event_at <= cutoff:
                continue
            actor_id = str(item.get("actor_id") or "")
            if actor_id not in actors:
                drift.append({
                    "activity_id": str(item.get("id") or ""),
                    "object_id": object_id,
                    "event_type": str(item.get("event_type") or ""),
                    "event_time": str(item.get("event_time") or ""),
                    "actor_id": actor_id,
                    "actor_name": str(item.get("actor_name") or ""),
                })
        return {
            "status": "DRIFT_DETECTED" if drift else "CLEAN",
            "drift_count": len(drift),
            "activities": drift,
            "meta_object_writes": 0,
        }

    def read_ad_preview(self, *, ad_id: str, ad_format: str = "DESKTOP_FEED_STANDARD") -> Dict[str, Any]:
        normalized_format = str(ad_format or "").upper()
        allowed = {
            "DESKTOP_FEED_STANDARD", "MOBILE_FEED_STANDARD", "INSTAGRAM_STANDARD",
            "INSTAGRAM_STORY", "INSTAGRAM_REELS", "FACEBOOK_REELS",
        }
        if normalized_format not in allowed:
            raise GrowthValidationError("meta_ad_preview_format_invalid")
        response = self.session.get(
            self._url(f"{str(ad_id)}/previews"),
            params={"access_token": self.access_token, "ad_format": normalized_format},
            timeout=self.timeout_seconds,
        )
        payload = self._response_json(response)
        return {
            "ad_id": str(ad_id), "ad_format": normalized_format,
            "previews": list(payload.get("data") or []), "meta_object_writes": 0,
            "rate_usage": self._rate_usage(response),
        }

    def read_delivery_state(self, *, object_id: str, object_type: str) -> Dict[str, Any]:
        normalized_type = str(object_type or "").lower()
        fields = {
            "campaign": "id,name,status,effective_status,issues_info,recommendations,updated_time",
            "adset": "id,name,status,effective_status,learning_stage_info,issues_info,recommendations,updated_time",
            "ad": "id,name,status,effective_status,ad_review_feedback,failed_delivery_checks,issues_info,recommendations,updated_time",
        }.get(normalized_type)
        if not fields:
            raise GrowthValidationError("meta_delivery_object_type_invalid")
        payload, response = self._get(str(object_id), fields)
        return {
            "object_type": normalized_type,
            "object_id": str(payload.get("id") or object_id),
            "configured_status": str(payload.get("status") or "").upper(),
            "effective_status": str(payload.get("effective_status") or payload.get("status") or "").upper(),
            "learning_stage": dict(payload.get("learning_stage_info") or {}),
            "review_feedback": dict(payload.get("ad_review_feedback") or {}),
            "failed_delivery_checks": list(payload.get("failed_delivery_checks") or []),
            "issues": list(payload.get("issues_info") or []),
            "recommendations": list(payload.get("recommendations") or []),
            "updated_time": str(payload.get("updated_time") or ""),
            "meta_object_writes": 0,
            "rate_usage": self._rate_usage(response),
        }

    def read_account_capabilities(self, *, account_id: str) -> Dict[str, Any]:
        payload, response = self._get(
            f"act_{str(account_id).removeprefix('act_')}",
            "id,account_id,name,account_status,disable_reason,currency,timezone_name,user_tasks,capabilities,all_capabilities,can_create_brand_lift_study,ad_account_promotable_objects",
        )
        tasks = {str(item).upper() for item in list(payload.get("user_tasks") or [])}
        account_status = int(payload.get("account_status") or 0)
        can_manage = bool(tasks & {"MANAGE", "ADVERTISE"})
        return {
            "account_id": str(payload.get("account_id") or payload.get("id") or "").removeprefix("act_"),
            "account_name": str(payload.get("name") or ""),
            "account_status": account_status,
            "disable_reason": payload.get("disable_reason"),
            "currency": str(payload.get("currency") or ""),
            "timezone_name": str(payload.get("timezone_name") or ""),
            "user_tasks": sorted(tasks),
            "eligible_for_read": account_status == 1,
            "eligible_for_write_plan": account_status == 1 and can_manage,
            "capabilities": list(payload.get("capabilities") or []),
            "all_capabilities": list(payload.get("all_capabilities") or []),
            "promotable_objects": dict(payload.get("ad_account_promotable_objects") or {}),
            "meta_object_writes": 0,
            "rate_usage": self._rate_usage(response),
        }

    def read_study_result_surface(self, study_id: str) -> Dict[str, Any]:
        study, study_response = self._get(
            str(study_id),
            "id,name,type,start_time,end_time,observation_end_time,results_first_available_date,created_time,updated_time",
        )
        cells_response = self.session.get(
            self._url(f"{study_id}/cells"),
            params={"access_token": self.access_token, "fields": "id,name,treatment_percentage,control_percentage,ad_entities_count,ad_ids"},
            timeout=self.timeout_seconds,
        )
        cells = self._response_json(cells_response)
        objectives_response = self.session.get(
            self._url(f"{study_id}/objectives"),
            params={"access_token": self.access_token, "fields": "id,name,type"},
            timeout=self.timeout_seconds,
        )
        objectives = self._response_json(objectives_response)
        return {
            "study": study,
            "cells": list(cells.get("data") or []),
            "objectives": list(objectives.get("data") or []),
            "meta_object_writes": 0,
            "rate_usage": {
                "study": self._rate_usage(study_response),
                "cells": self._rate_usage(cells_response),
                "objectives": self._rate_usage(objectives_response),
            },
        }

    def _validated_range(self, since: str, until: str) -> tuple[date, date]:
        try:
            start = date.fromisoformat(str(since))
            stop = date.fromisoformat(str(until))
        except ValueError as exc:
            raise GrowthValidationError("meta_insights_date_invalid") from exc
        days = (stop - start).days + 1
        if days < 1 or days > self.policy.max_range_days:
            raise GrowthValidationError("meta_insights_range_invalid")
        return start, stop

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError as exc:
            raise GrowthValidationError("meta_activity_time_invalid") from exc
        if parsed.tzinfo is None:
            raise GrowthValidationError("meta_activity_timezone_required")
        return parsed

    def _get(self, object_id: str, fields: str) -> tuple[Dict[str, Any], Any]:
        response = self.session.get(
            self._url(object_id), params={"access_token": self.access_token, "fields": fields},
            timeout=self.timeout_seconds,
        )
        return self._response_json(response), response

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{self.api_version}/{str(path).lstrip('/')}"

    @staticmethod
    def _response_json(response: Any) -> Dict[str, Any]:
        payload = response.json() if hasattr(response, "json") else {}
        if not isinstance(payload, dict):
            raise GrowthValidationError("meta_response_invalid")
        if payload.get("error"):
            evidence = relevant_meta_error_evidence(payload, http_status=getattr(response, "status_code", None))
            raise GrowthValidationError("meta_read_error:" + json.dumps(evidence, ensure_ascii=False, sort_keys=True))
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        return payload

    @staticmethod
    def _rate_usage(response: Any) -> Dict[str, Any]:
        headers = getattr(response, "headers", {}) or {}
        result = {}
        for header in ("x-app-usage", "x-ad-account-usage", "x-business-use-case-usage"):
            value = headers.get(header) or headers.get(header.title())
            if not value:
                continue
            try:
                result[header] = json.loads(value)
            except (TypeError, ValueError):
                result[header] = str(value)[:500]
        return result
