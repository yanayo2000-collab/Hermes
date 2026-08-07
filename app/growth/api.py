from __future__ import annotations

import copy
import hashlib
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.ad_daily_report import ensure_ad_daily_report_tables
from app.growth.adaptive_agent_service import AdaptiveGrowthAgentService
from app.growth.ad_experiment_evaluator import AdExperimentEvaluator
from app.growth.ad_experiment_service import AdExperimentService
from app.growth.audience_strategy import (
    AUDIENCE_DELIVERY_ESTIMATE_SNAPSHOT,
    INITIAL_AUDIENCE_EXPERIMENT_POLICY,
    audience_contract,
    audience_strategy,
    country_audience_policy,
    ensure_audience_strategy_registry,
)
from app.growth.audience_experiment_evaluator import AudienceExperimentEvaluator
from app.growth.autonomy_service import GrowthAutonomyService
from app.growth.meta_audience_preflight import MetaAudiencePreflightService
from app.growth.approval_service import OperationApprovalService
from app.growth.common import canonical_json, decode_json, payload_hash, utc_now
from app.growth.creative_naming import compact_launch_ad_name
from app.growth.decision_service import DecisionService
from app.growth.delivery_guardrails import new_account_delivery_guardrails
from app.growth.episode_service import EpisodeService
from app.growth.errors import GrowthError, GrowthValidationError
from app.growth.execution_service import ExecutionTaskService
from app.growth.meta_graph_adapter import configured_regional_regulation_identities
from app.growth.knowledge_service import KnowledgeService
from app.growth.new_account_launch_retention import (
    ensure_new_account_launch_retention_tables,
    launch_retention_status,
    purge_new_account_launch,
)
from app.growth.new_account_launch_meta_delete import (
    DELETE_MODE,
    LaunchMetaDeleteConflict,
    LaunchMetaDeleteManualReview,
    NewAccountLaunchMetaDeleteService,
)
from app.growth.pattern_mining_service import PatternMiningService
from app.growth.read_service import GrowthReadService
from app.growth.similar_episode_service import SimilarEpisodeService


def _enabled(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _read_meta_delivery_status(
    *, session: Any, access_token: str, graph_root: str,
    experiments: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Read exact current Meta status for one launch without performing writes."""
    if not session or not str(access_token or "").strip() or not str(graph_root or "").strip():
        raise GrowthValidationError("meta_delivery_status_readback_unavailable")
    normalized = [dict(item or {}) for item in list(experiments or [])]
    campaign_ids = {str(item.get("campaign_id") or item.get("source_campaign_id") or "").strip() for item in normalized}
    if not normalized or len(campaign_ids) != 1 or "" in campaign_ids:
        raise GrowthValidationError("launch_shared_campaign_readback_required")
    object_ids: List[str] = [next(iter(campaign_ids))]
    for item in normalized:
        object_ids.extend((
            str(item.get("adset_id") or item.get("source_adset_id") or "").strip(),
            str(item.get("ad_id") or item.get("source_ad_id") or "").strip(),
        ))
    if any(not value for value in object_ids) or len(set(object_ids)) != len(object_ids):
        raise GrowthValidationError("launch_delivery_lineage_incomplete")
    response = session.get(
        str(graph_root).rstrip("/"),
        params={
            "access_token": access_token,
            "ids": ",".join(object_ids),
            "fields": "id,name,status,effective_status,updated_time",
        },
        timeout=25,
    )
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    payload = response.json() if hasattr(response, "json") else {}
    if not isinstance(payload, dict) or payload.get("error"):
        raise GrowthValidationError("meta_delivery_status_readback_failed")
    statuses: Dict[str, Dict[str, str]] = {}
    for object_id in object_ids:
        item = dict(payload.get(object_id) or {})
        if str(item.get("id") or "").strip() != object_id:
            raise GrowthValidationError("meta_delivery_status_object_missing")
        configured = str(item.get("status") or "").strip().upper()
        effective = str(item.get("effective_status") or configured).strip().upper()
        if not configured or not effective:
            raise GrowthValidationError("meta_delivery_status_value_missing")
        statuses[object_id] = {
            "id": object_id,
            "name": str(item.get("name") or ""),
            "configured_status": configured,
            "effective_status": effective,
            "updated_time": str(item.get("updated_time") or ""),
        }
    campaign = statuses[object_ids[0]]
    paths: List[Dict[str, Any]] = []
    for item in normalized:
        adset = statuses[str(item.get("adset_id") or item.get("source_adset_id") or "")]
        ad = statuses[str(item.get("ad_id") or item.get("source_ad_id") or "")]
        configured_active = all(value["configured_status"] == "ACTIVE" for value in (campaign, adset, ad))
        effective = ad["effective_status"]
        if campaign["configured_status"] != "ACTIVE":
            delivery_state = "CAMPAIGN_PAUSED"
        elif adset["configured_status"] != "ACTIVE":
            delivery_state = "ADSET_PAUSED"
        elif ad["configured_status"] != "ACTIVE":
            delivery_state = "AD_PAUSED"
        elif effective == "ACTIVE":
            delivery_state = "ACTIVE"
        elif effective in {"PENDING_REVIEW", "IN_PROCESS", "WITH_ISSUES", "PREAPPROVED"}:
            delivery_state = "REVIEW_PENDING"
        else:
            delivery_state = effective or "UNKNOWN"
        paths.append({
            "experiment_id": str(item.get("experiment_id") or ""),
            "campaign_id": campaign["id"], "adset_id": adset["id"], "ad_id": ad["id"],
            "campaign_status": campaign["configured_status"],
            "campaign_effective_status": campaign["effective_status"],
            "adset_status": adset["configured_status"],
            "adset_effective_status": adset["effective_status"],
            "ad_status": ad["configured_status"],
            "ad_effective_status": ad["effective_status"],
            "configured_active": configured_active,
            "delivery_state": delivery_state,
            "updated_time": max(filter(None, (campaign["updated_time"], adset["updated_time"], ad["updated_time"])), default=""),
        })
    if campaign["configured_status"] != "ACTIVE":
        overall_state = "CAMPAIGN_PAUSED"
    elif all(item["delivery_state"] in {"ACTIVE", "REVIEW_PENDING"} for item in paths):
        overall_state = "ACTIVE"
    elif all(item["delivery_state"] in {"ADSET_PAUSED", "AD_PAUSED"} for item in paths):
        overall_state = "PAUSED"
    else:
        overall_state = "PARTIAL"
    return {
        "source": "META_LIVE_GET", "checked_at": utc_now(), "stale_after_seconds": 30,
        "overall_state": overall_state, "campaign": campaign, "paths": paths,
        "configured_active_count": sum(1 for item in paths if item["configured_active"]),
        "path_count": len(paths), "meta_writes_performed": False,
    }


def _meta_live_execution_available(action_type: str = "", account_id: str = "") -> bool:
    allowed_accounts = {
        item.strip().removeprefix("act_")
        for item in str(os.getenv("GROWTH_META_ALLOWED_ACCOUNT_IDS") or "").split(",")
        if item.strip()
    }
    allowed_actions = {
        item.strip().upper()
        for item in str(os.getenv("GROWTH_META_ALLOWED_ACTION_TYPES") or "").split(",")
        if item.strip()
    }
    requested_action = str(action_type or "").strip().upper()
    requested_account = str(account_id or "").strip().removeprefix("act_")
    return bool(
        _enabled(os.getenv("GROWTH_META_LIVE_EXECUTION_AVAILABLE", ""))
        and _enabled(os.getenv("GROWTH_META_WRITES_ENABLED", ""))
        and allowed_accounts
        and allowed_actions
        and (not requested_action or requested_action in allowed_actions)
        and (not requested_account or requested_account in allowed_accounts)
    )


def _recovery_approval_current(recovery: Dict[str, Any], plan: Dict[str, Any]) -> bool:
    if (
        str(recovery.get("status") or "").upper() != "APPROVED"
        or not str(recovery.get("confirmed_by") or "").strip()
        or str(recovery.get("plan_hash") or "") != payload_hash(plan)
    ):
        return False
    expires_at = str(recovery.get("expires_at") or "").strip()
    if not expires_at:
        return True
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) > datetime.now(timezone.utc)


class DecisionCreateRequest(BaseModel):
    recommendation_id: str
    selected_action: str
    rejected_actions: List[str] = Field(default_factory=list)
    decision_reason: Dict[str, Any]
    confidence: float
    context_snapshot_id: str = ""


class EpisodeUpdateRequest(BaseModel):
    status: str
    outcome_json: Optional[Dict[str, Any]] = None
    lesson_json: Optional[Dict[str, Any]] = None
    action_json: Optional[Dict[str, Any]] = None
    reason: str = ""


class DecisionTargetRequest(BaseModel):
    target_type: str
    target_id: str


class KnowledgeCreateRequest(BaseModel):
    episode_id: str
    pattern_type: str
    pattern_json: Dict[str, Any]


class KnowledgeTransitionRequest(BaseModel):
    status: str


class OperationActionCreateRequest(BaseModel):
    decision_id: str
    episode_id: str = ""
    action_type: str
    action_scope: str = "BUSINESS_PROTECTION"
    target_type: str
    target_id: str
    payload_json: Dict[str, Any] = Field(default_factory=dict)


class ExecutionTaskCreateRequest(BaseModel):
    operation_action_id: str
    payload_json: Dict[str, Any] = Field(default_factory=dict)


class OperationApprovalCreateRequest(BaseModel):
    plan_json: Dict[str, Any]
    expires_at: str = ""


class StatusTransitionRequest(BaseModel):
    status: str


class PatternMineRequest(BaseModel):
    minimum_support: int = Field(default=2, ge=2, le=100)


class StrategyRecommendationCreateRequest(BaseModel):
    context_snapshot_id: str


class SimulationCreateRequest(BaseModel):
    context_snapshot_id: str
    proposed_action: str


class LowRiskExecuteRequest(BaseModel):
    decision_id: str


class AutonomyPolicyRequest(BaseModel):
    level: str
    allowed_action_types: List[str] = Field(default_factory=list)
    max_daily_budget_usd: float = Field(default=0, ge=0)
    max_budget_change_pct: float = Field(default=0, ge=0, le=100)
    minimum_installs: int = Field(default=100, ge=0)
    minimum_real_joins: int = Field(default=10, ge=0)
    require_real_join_attribution: bool = True
    reason: str = ""


class StrictAdExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdExperimentDraftRequest(StrictAdExperimentRequest):
    target_app: str
    experiment_type: str
    experiment_code: str = ""
    country: str = ""
    platform: str = "meta"
    account_id: str = ""
    source_report_id: str = ""
    source_recommendation_id: str = ""
    source_campaign_id: str = ""
    source_adset_id: str = ""
    source_ad_id: str = ""
    source_creative_id: str = ""
    hypothesis_json: Dict[str, Any] = Field(default_factory=dict)
    primary_metric: str = ""
    guardrail_metrics_json: List[str] = Field(default_factory=list)
    maturity_rule_json: Dict[str, Any] = Field(default_factory=dict)
    stop_rule_json: Dict[str, Any] = Field(default_factory=dict)
    control_definition_json: Dict[str, Any] = Field(default_factory=dict)
    variant_definition_json: Dict[str, Any] = Field(default_factory=dict)


class AdExperimentPlanRequest(StrictAdExperimentRequest):
    decision_id: str
    episode_id: str = ""
    action_type: str
    target_account_id: str = ""
    target_object_type: str = "AD"
    target_object_id: str = ""
    recommendation_id: str = ""
    before_json: Dict[str, Any] = Field(default_factory=dict)
    after_json: Dict[str, Any] = Field(default_factory=dict)
    steps: Dict[str, Any] = Field(default_factory=dict)
    creative: Dict[str, Any] = Field(default_factory=dict)
    asset_sha256: str = ""
    copy_version_id: str = ""
    max_write_requests: int = Field(default=5, ge=1, le=5)
    preflight_snapshot_json: Dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    evidence_window: Dict[str, Any] = Field(default_factory=dict)
    expected_effect: Dict[str, Any] = Field(default_factory=dict)
    evaluation_window: Dict[str, Any] = Field(default_factory=dict)
    expires_at: str = ""


class NewAccountBatchCellRequest(StrictAdExperimentRequest):
    experiment_id: str
    role: str
    adset_name: str
    daily_budget_usd: float = Field(ge=5, le=100)
    ad_name: str
    primary_text: str
    headline: str
    description: str = ""
    call_to_action: str = "INSTALL_MOBILE_APP"
    audience_strategy: str = "BROAD"
    copy_benchmark_version: str = ""
    copy_hypothesis: str = ""


class NewAccountBatchPlanRequest(StrictAdExperimentRequest):
    campaign_name: str
    audience_strategy: str = "BROAD"
    test_variable: str = "creative_direction"
    frozen_creative_id: str = ""
    cells: List[NewAccountBatchCellRequest]
    audience_preflight_id: str = ""
    evaluation_window: Dict[str, Any] = Field(default_factory=lambda: {"checkpoints": ["D1", "D3", "D7"]})


class AdExperimentExecuteRequest(StrictAdExperimentRequest):
    execution_mode: str = "dry_run"
    confirmation: str = ""


class AdExperimentActivateRequest(StrictAdExperimentRequest):
    decision_id: str
    episode_id: str = ""
    confirmation: str


class NewAccountLaunchActivateRequest(StrictAdExperimentRequest):
    confirmation: str


class NewAccountLaunchPermanentDeleteRequest(StrictAdExperimentRequest):
    mode: str = "ORDER_ONLY"
    confirmation: str = ""
    plan_hash: str = ""


class AdExperimentResumeRequest(StrictAdExperimentRequest):
    confirmation: str = ""


class AdExperimentPageRepairRequest(StrictAdExperimentRequest):
    page_id: str
    confirmation: str = ""


class AdExperimentRepairRequest(StrictAdExperimentRequest):
    target_page_id: str
    confirmation: str = ""


class AdExperimentApproveRequest(StrictAdExperimentRequest):
    confirmation: str = ""


class AdExperimentEvaluationRequest(StrictAdExperimentRequest):
    checkpoint: str
    episode_id: str = ""
    action_type: str = ""
    execution_status: str = "CLEAN_EXECUTED"
    baseline_window: Dict[str, Any] = Field(default_factory=dict)
    post_window: Dict[str, Any] = Field(default_factory=dict)
    baseline_metrics: Dict[str, Any]
    post_metrics: Dict[str, Any]
    data_quality_status: str = "PASS"
    dedupe_version: str = ""
    attribution_version: str = ""


class AudiencePairEvaluationRequest(StrictAdExperimentRequest):
    checkpoint: str
    metrics_by_experiment: Dict[str, Dict[str, Any]]
    data_quality_status: str = "PASS"
    target_cpa: Optional[float] = None
    mixed_change: bool = False


class AdExperimentMetaReviewRequest(StrictAdExperimentRequest):
    review_status: str
    reason: str = ""
    evidence_json: Dict[str, Any] = Field(default_factory=dict)


class CreativeDirectionSelection(StrictAdExperimentRequest):
    direction_id: str
    key: str = ""
    code: str
    title: str
    hypothesis: str
    rationale: str = ""
    source: str = "audience_fit"
    initial_daily_budget: float = Field(default=20, gt=0, le=100)


class NewAccountDirectionPreviewRequest(StrictAdExperimentRequest):
    target_app: str = "tugao"
    country: str = "BR"
    daily_spend_target: float = Field(gt=0)
    cpi_target: float = Field(gt=0)
    gender: Optional[str] = None
    age_min: Optional[int] = Field(default=None, ge=13, le=65)
    age_max: Optional[int] = Field(default=None, ge=13, le=65)
    language: Optional[str] = None
    regeneration_round: int = Field(default=0, ge=0, le=20)


class NewAccountLaunchRequest(StrictAdExperimentRequest):
    target_app: str
    country: str = "BR"
    account_id: str
    account_name: str = Field(default="", max_length=120)
    daily_spend_target: float = Field(gt=0)
    cpi_target: float = Field(gt=0)
    page_id: str = ""
    destination_url: str = ""
    gender: Optional[str] = None
    age_min: Optional[int] = Field(default=None, ge=13, le=65)
    age_max: Optional[int] = Field(default=None, ge=13, le=65)
    language: Optional[str] = None
    naming_date: str = ""
    creative_directions: List[CreativeDirectionSelection] = Field(default_factory=list)


class NewAccountAudienceLaunchRequest(StrictAdExperimentRequest):
    target_app: str = "tugao"
    country: str = "BR"
    account_id: str
    account_name: str = Field(default="", max_length=120)
    daily_spend_target: float = Field(gt=0)
    cpi_target: float = Field(gt=0)
    page_id: str
    naming_date: str = ""
    frozen_creative_id: str
    audience_strategies: List[str] = Field(default_factory=lambda: ["BROAD", "DIGITAL_SELLER"])
    initial_daily_budget: float = Field(default=20, ge=5, le=100)


class CopyVariantSelection(StrictAdExperimentRequest):
    primary_text: str
    headline: str
    description: str = ""
    hypothesis: str
    benchmark_version: str = "gle_copy_benchmark_v1_20260803"


class NewAccountCopyLaunchRequest(StrictAdExperimentRequest):
    target_app: str = "tugao"
    country: str = "BR"
    account_id: str
    account_name: str = Field(default="", max_length=120)
    daily_spend_target: float = Field(gt=0)
    cpi_target: float = Field(gt=0)
    page_id: str
    naming_date: str = ""
    frozen_creative_id: str
    copy_variants: List[CopyVariantSelection]
    initial_daily_budget: float = Field(default=20, ge=5, le=100)


_COUNTRY_ALIASES = {
    "BR": ("BR", "Brazil", "Brasil"),
    "ID": ("ID", "Indonesia"),
    "MX": ("MX", "Mexico", "México"),
    "CO": ("CO", "Colombia"),
}
_LANGUAGE_CODES = {"pt_BR": "PT", "es_419": "ES", "id_ID": "ID", "en_US": "EN"}
_DIRECTION_CATALOG = (
    {
        "key": "points_reward",
        "code": "PR",
        "title": "网赚效率",
        "summary": "突出任务、积分与奖励进度，验证效率诉求。",
        "hypothesis": "突出应用内任务、积分和奖励进度，验证效率诉求能否提升点击与安装。",
    },
    {
        "key": "safe_compliance",
        "code": "SC",
        "title": "安全合规",
        "summary": "讲清产品流程与应用内奖励，不使用现金或就业承诺。",
        "hypothesis": "用产品流程、任务进度和合规表达建立信任，验证能否提升安装质量。",
    },
    {
        "key": "easy_start",
        "code": "ES",
        "title": "流程透明",
        "summary": "用清晰的三步路径降低理解与开始门槛。",
        "hypothesis": "用清晰的三步开始路径降低理解成本，验证能否改善点击到安装的转化。",
    },
    {
        "key": "guided_trust",
        "code": "GT",
        "title": "私聊顾问",
        "summary": "用一对一应用内指导回应新手疑问并建立信任。",
        "hypothesis": "用一对一应用内顾问指导回应新手疑问，验证信任表达能否提升安装质量。",
    },
)
_DIRECTION_BY_KEY = {str(item["key"]): item for item in _DIRECTION_CATALOG}
_DIRECTION_BY_CODE = {str(item["code"]): item for item in _DIRECTION_CATALOG}


def _resolve_new_account_audience(
    *, country: str, gender: Optional[str], language: Optional[str],
    age_min: Optional[int], age_max: Optional[int],
) -> Dict[str, Any]:
    try:
        policy = country_audience_policy(country)
    except GrowthError:
        raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "new_account_country_not_configured"})
    supplied = {
        "gender": gender, "language": language, "age_min": age_min, "age_max": age_max,
    }
    for key, value in supplied.items():
        if value is not None and str(value) != str(policy[key]):
            raise HTTPException(
                status_code=400,
                detail={"code": "VALIDATION_ERROR", "message": f"base_audience_override_forbidden:{key}"},
            )
    return {
        "gender": policy["gender"], "language": policy["language"],
        "age_min": policy["age_min"], "age_max": policy["age_max"],
    }


def _compact_meta_names(
    *,
    country: str,
    gender: str,
    age_min: int,
    age_max: int,
    language: str,
    naming_date: str,
    direction_code: str,
    cell_index: int,
) -> Dict[str, str]:
    gender_code = {"all": "A", "female": "F", "male": "M"}[gender]
    language_code = _LANGUAGE_CODES[language]
    short_date = naming_date[2:]
    return {
        "campaign": f"TG_{country}_INS_CS_{short_date}",
        "adset": f"{gender_code}{age_min}-{age_max}_{language_code}_BD_C{cell_index}",
        "ad": compact_launch_ad_name(direction_code, naming_date, cell_index),
    }


def _ensure_creative_direction_mapping_table(conn: sqlite3.Connection) -> None:
    """Persist direction lineage by stable Meta Ad id; never infer it from a name."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_creative_direction_mapping (
            ad_id TEXT PRIMARY KEY,
            direction_key TEXT NOT NULL,
            experiment_id TEXT NOT NULL DEFAULT '',
            launch_id TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def _historical_creative_evidence(conn: sqlite3.Connection, country: str) -> Dict[str, Any]:
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_dashboard_fact_rows'",
    ).fetchone()
    if not table_exists:
        return {"available": False, "window_days": 0, "top_ads": []}
    _ensure_creative_direction_mapping_table(conn)
    aliases = _COUNTRY_ALIASES.get(country, (country,))
    placeholders = ",".join("?" for _ in aliases)
    summary = conn.execute(
        f"""
        WITH country_rows AS (
            SELECT * FROM ad_dashboard_fact_rows
            WHERE country IN ({placeholders})
        ), bounds AS (
            SELECT MAX(date) AS max_date FROM country_rows
        )
        SELECT
            MIN(date) AS date_from,
            MAX(date) AS date_to,
            COUNT(DISTINCT CASE WHEN data_source='Meta' AND length(trim(ad))>0 THEN ad END) AS creative_count,
            SUM(CASE WHEN data_source='Meta' THEN cost ELSE 0 END) AS spend,
            SUM(CASE WHEN data_source='Meta' THEN installs ELSE 0 END) AS installs,
            SUM(CASE WHEN data_source='Meta' THEN clicks ELSE 0 END) AS clicks,
            SUM(CASE WHEN data_source='Meta' THEN impressions ELSE 0 END) AS impressions,
            SUM(CASE WHEN data_source='BindSuccess' THEN promotion_guild_joins ELSE 0 END) AS real_joins
        FROM country_rows
        WHERE date >= date((SELECT max_date FROM bounds), '-89 day')
        """,
        aliases,
    ).fetchone()
    if not summary or not str(summary["date_to"] or ""):
        return {"available": False, "window_days": 0, "top_ads": []}
    spend = float(summary["spend"] or 0)
    installs = float(summary["installs"] or 0)
    clicks = float(summary["clicks"] or 0)
    impressions = float(summary["impressions"] or 0)
    real_joins = float(summary["real_joins"] or 0)
    top_rows = conn.execute(
        f"""
        WITH country_rows AS (
            SELECT * FROM ad_dashboard_fact_rows
            WHERE country IN ({placeholders})
        ), bounds AS (
            SELECT MAX(date) AS max_date FROM country_rows
        ), ad_rollup AS (
            SELECT
                f.ad_id AS ad_id,f.ad AS ad,
                COALESCE(
                    MAX(NULLIF(m.direction_key,'')),
                    MAX(CASE WHEN json_valid(f.payload_json) THEN NULLIF(json_extract(f.payload_json,'$.creative_direction.key'),'') END),
                    MAX(CASE WHEN json_valid(f.payload_json) THEN NULLIF(json_extract(f.payload_json,'$.creative_direction'),'') END),
                    ''
                ) AS direction_key,
                SUM(CASE WHEN data_source='Meta' THEN cost ELSE 0 END) AS spend,
                SUM(CASE WHEN data_source='Meta' THEN installs ELSE 0 END) AS installs,
                SUM(CASE WHEN data_source='Meta' THEN clicks ELSE 0 END) AS clicks,
                SUM(CASE WHEN data_source='Meta' THEN impressions ELSE 0 END) AS impressions,
                SUM(CASE WHEN data_source='BindSuccess' THEN promotion_guild_joins ELSE 0 END) AS real_joins
            FROM country_rows f
            LEFT JOIN ad_creative_direction_mapping m ON m.ad_id=f.ad_id AND length(trim(f.ad_id))>0
            WHERE date >= date((SELECT max_date FROM bounds), '-89 day')
              AND length(trim(ad))>0
            GROUP BY f.ad_id,f.ad
        )
        SELECT ad_id,ad,direction_key,spend,installs,clicks,impressions,real_joins
        FROM ad_rollup
        WHERE spend>=10 AND installs>=30
        ORDER BY
            CASE WHEN real_joins>0 THEN spend/real_joins ELSE 999999 END ASC,
            spend/installs ASC,
            spend DESC
        LIMIT 3
        """,
        aliases,
    ).fetchall()
    top_ads = []
    for row in top_rows:
        row_spend = float(row["spend"] or 0)
        row_installs = float(row["installs"] or 0)
        row_impressions = float(row["impressions"] or 0)
        row_clicks = float(row["clicks"] or 0)
        row_joins = float(row["real_joins"] or 0)
        direction_key = str(row["direction_key"] or "").strip()
        top_ads.append({
            "ad_id": str(row["ad_id"] or ""),
            "name": str(row["ad"] or "")[:48],
            "direction_key": direction_key,
            "direction_mapping_status": "mapped" if direction_key else "unmapped",
            "cpi": round(row_spend / row_installs, 3) if row_installs > 0 else None,
            "ctr": round(100 * row_clicks / row_impressions, 2) if row_impressions > 0 else None,
            "real_join_cpa": round(row_spend / row_joins, 2) if row_joins > 0 else None,
        })
    mapped_count = sum(1 for row in top_ads if row["direction_key"])
    return {
        "available": spend > 0 and installs > 0,
        "window_days": 90,
        "date_from": str(summary["date_from"] or ""),
        "date_to": str(summary["date_to"] or ""),
        "creative_count": int(summary["creative_count"] or 0),
        "spend": round(spend, 2),
        "installs": int(installs),
        "cpi": round(spend / installs, 3) if installs > 0 else None,
        "ctr": round(100 * clicks / impressions, 2) if impressions > 0 else None,
        "real_joins": int(real_joins),
        "real_join_cpa": round(spend / real_joins, 2) if real_joins > 0 else None,
        "top_ads": top_ads,
        "mapped_top_ad_count": mapped_count,
        "direction_mapping_coverage": round(mapped_count / len(top_ads), 3) if top_ads else 0.0,
        "direction_mapping_source": "stable_ad_id_or_explicit_payload",
    }


def _generate_creative_direction_preview(
    conn: sqlite3.Connection,
    body: NewAccountDirectionPreviewRequest,
) -> Dict[str, Any]:
    ensure_audience_strategy_registry(conn)
    country = str(body.country or "BR").strip().upper()
    audience = _resolve_new_account_audience(
        country=country, gender=body.gender, language=body.language,
        age_min=body.age_min, age_max=body.age_max,
    )
    gender = str(audience["gender"])
    language = str(audience["language"])
    history = _historical_creative_evidence(conn, country)
    recommended_test_count = 3
    raw_budget = float(body.daily_spend_target) * 0.30 / recommended_test_count
    initial_daily_budget = max(5.0, min(20.0, round(raw_budget / 5.0) * 5.0))
    historical_direction_keys = [
        str(row.get("direction_key") or "")
        for row in history.get("top_ads") or []
        if str(row.get("direction_key") or "")
    ]
    historical_rank = {key: index for index, key in enumerate(historical_direction_keys)}
    default_rank = {str(item["key"]): index for index, item in enumerate(_DIRECTION_CATALOG)}
    ordered = sorted(
        _DIRECTION_CATALOG,
        key=lambda item: (
            0 if str(item["key"]) in historical_rank else 1,
            historical_rank.get(str(item["key"]), default_rank[str(item["key"])]),
        ),
    )
    naming_date = datetime.now(timezone.utc).strftime("%Y%m%d")
    gender_label = {"all": "不限性别", "female": "女性", "male": "男性"}[gender]
    audience_label = f"{country} · {gender_label} · {audience['age_min']}-{audience['age_max']}岁 · {language}"
    history_signal = (
        f"近90天历史 CPI ${history['cpi']:.3f}、CTR {history['ctr']:.2f}%"
        if history.get("available") and history.get("cpi") is not None and history.get("ctr") is not None
        else "当前缺少可稳定复用的国家级素材历史"
    )
    directions = []
    for index, item in enumerate(ordered, start=1):
        key = str(item["key"])
        source = "historical_winner" if key in historical_rank else (
            "core_catalog" if index <= recommended_test_count else "controlled_exploration"
        )
        rationale = f"{history_signal}；面向 {audience_label}，在固定方向“{item['title']}”内验证一个子假设。"
        names = _compact_meta_names(
            country=country,
            gender=gender,
            age_min=int(audience["age_min"]),
            age_max=int(audience["age_max"]),
            language=language,
            naming_date=naming_date,
            direction_code=str(item["code"]),
            cell_index=index,
        )
        directions.append({
            "direction_id": key,
            "key": key,
            "code": item["code"],
            "title": item["title"],
            "summary": item["summary"],
            "hypothesis": item["hypothesis"],
            "rationale": rationale,
            "source": source,
            "selected": index <= recommended_test_count,
            "initial_daily_budget": initial_daily_budget,
            "meta_names": names,
        })
    return {
        "ok": True,
        "generation_method": "fixed_direction_catalog_v1",
        "regeneration_round": int(body.regeneration_round),
        "recommended_test_count": recommended_test_count,
        "naming_date": naming_date,
        "naming_rule": {
            "campaign": "TG_{国家}_INS_CS_{日期}",
            "adset": "{性别年龄}_{语言}_BD_C{实验格}",
            "ad": "{方向码}_ST_H1_V1",
        },
        "direction_catalog_version": "tugao_creative_directions_v1",
        "audience_contract": audience_contract(country, "BROAD"),
        "history_evidence": history,
        "directions": directions,
        "meta_writes_performed": False,
    }


def _approved_frozen_creative(conn: sqlite3.Connection, image_id: str) -> Dict[str, Any]:
    normalized_image_id = str(image_id or "").strip()
    if not normalized_image_id:
        raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "frozen_creative_required"})
    row = conn.execute(
        """
        SELECT i.* FROM creative_generated_images i
        WHERE i.image_id=? AND lower(i.review_status) IN ('approved','used_in_ad')
          AND EXISTS (
              SELECT 1 FROM creative_review_records r
              WHERE r.image_id=i.image_id AND upper(r.review_status)='APPROVED'
          )
        LIMIT 1
        """,
        (normalized_image_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "approved_frozen_creative_required"})
    image = dict(row)
    image_path = Path(str(image.get("image_ref") or "")).expanduser().resolve()
    if not image_path.is_file():
        raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "approved_creative_file_missing"})
    metadata = decode_json(image.get("metadata_json"), {})
    return {
        "image_id": normalized_image_id,
        "image_ref": str(image_path),
        "image_hash": str(image.get("image_hash") or ""),
        "creative_direction": str(image.get("creative_direction") or metadata.get("creative_direction") or "frozen_winner"),
        "market": str(image.get("market") or ""),
        "brand": str(image.get("brand") or "Tugao"),
        "created_at": str(image.get("created_at") or ""),
    }


def create_growth_router(
    *,
    db: Any,
    require_admin: Callable[[Request], Dict[str, Any]],
) -> APIRouter:
    router = APIRouter(prefix="/api/ops/growth", tags=["growth-loop-v2"])

    def operator(request: Request) -> Dict[str, Any]:
        return dict(require_admin(request) or {})

    def execute(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except GrowthError as exc:
            raise HTTPException(
                status_code=exc.http_status,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "STATE_CONFLICT", "message": "database_constraint_conflict"},
            ) from exc

    @router.post("/decisions", status_code=201)
    def create_decision(
        body: DecisionCreateRequest,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)

        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                result = DecisionService(conn).create_decision(
                    recommendation_id=body.recommendation_id,
                    selected_action=body.selected_action,
                    rejected_actions=body.rejected_actions,
                    decision_reason=body.decision_reason,
                    confidence=body.confidence,
                    idempotency_key=idempotency_key,
                    decided_by=str(user.get("user_id") or user.get("username") or ""),
                    context_snapshot_id=body.context_snapshot_id,
                )
                result["request_id"] = request_id
                return result

        return execute(action)

    @router.get("/recommendations/{recommendation_id}/decision-preview")
    def preview_decision(recommendation_id: str, request: Request) -> Dict[str, Any]:
        operator(request)

        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                return DecisionService(conn).preview_recommendation(recommendation_id)

        return execute(action)

    @router.patch("/episodes/{episode_id}")
    def update_episode(episode_id: str, body: EpisodeUpdateRequest, request: Request) -> Dict[str, Any]:
        user = operator(request)
        return execute(lambda: _update_episode(db, episode_id, body, user))

    @router.get("/episodes")
    def list_episodes(
        request: Request,
        status: str = Query(""),
        limit: int = Query(50, ge=1, le=200),
    ) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _list_episodes(db, status=status, limit=limit))

    @router.get("/episodes/{episode_id}")
    def get_episode(episode_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _get_episode_detail(db, episode_id))

    @router.patch("/decisions/{decision_id}/target")
    def bind_decision_target(decision_id: str, body: DecisionTargetRequest, request: Request) -> Dict[str, Any]:
        user = operator(request)

        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                return DecisionService(conn).bind_target(
                    decision_id, target_type=body.target_type, target_id=body.target_id,
                    actor=str(user.get("user_id") or user.get("username") or ""),
                )

        return execute(action)

    @router.get("/episodes/{episode_id}/similar")
    def similar_episodes(episode_id: str, request: Request, limit: int = Query(5, ge=1, le=20)) -> Dict[str, Any]:
        operator(request)

        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                episode = EpisodeService(conn).get_episode(episode_id)
                items = SimilarEpisodeService(conn).find_similar(episode["context_snapshot_id"], limit=limit)
                return {"episode_id": episode_id, "items": items, "count": len(items)}

        return execute(action)

    @router.post("/knowledge", status_code=201)
    def create_knowledge(
        body: KnowledgeCreateRequest,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _create_knowledge(db, body, idempotency_key, request_id))

    @router.get("/knowledge")
    def list_knowledge(
        request: Request,
        status: str = Query(""),
        limit: int = Query(50, ge=1, le=200),
    ) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _list_knowledge(db, status=status, limit=limit))

    @router.get("/knowledge/{knowledge_id}")
    def get_knowledge(knowledge_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _get_knowledge_detail(db, knowledge_id))

    @router.patch("/knowledge/{knowledge_id}")
    def transition_knowledge(knowledge_id: str, body: KnowledgeTransitionRequest, request: Request) -> Dict[str, Any]:
        user = operator(request)

        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                return KnowledgeService(conn).transition(
                    knowledge_id, body.status,
                    reviewer=str(user.get("user_id") or user.get("username") or ""),
                )

        return execute(action)

    @router.post("/operation-actions", status_code=201)
    def create_operation_action(
        body: OperationActionCreateRequest,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)

        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                result = ExecutionTaskService(conn).create_operation_action(
                    decision_id=body.decision_id, episode_id=body.episode_id,
                    action_type=body.action_type, action_scope=body.action_scope,
                    target_type=body.target_type, target_id=body.target_id,
                    payload=body.payload_json,
                    created_by=str(user.get("user_id") or user.get("username") or ""),
                    idempotency_key=idempotency_key,
                )
                result["request_id"] = request_id
                return result

        return execute(action)

    @router.post("/execution-tasks", status_code=201)
    def create_execution_task(
        body: ExecutionTaskCreateRequest,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        operator(request)

        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                result = ExecutionTaskService(conn).enqueue_task(
                    body.operation_action_id,
                    idempotency_key=idempotency_key,
                    payload=body.payload_json,
                )
                result["request_id"] = request_id
                return result

        return execute(action)

    @router.get("/execution-tasks/{task_id}")
    def get_execution_task(task_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _get_execution_task(db, task_id))

    @router.get("/experiments/{experiment_id}")
    def get_experiment(experiment_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _get_experiment_detail(db, experiment_id))

    @router.post("/operation-actions/{operation_action_id}/approvals", status_code=201)
    def propose_operation_approval(
        operation_action_id: str,
        body: OperationApprovalCreateRequest,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)

        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                result = OperationApprovalService(conn).propose(
                    operation_action_id, body.plan_json,
                    proposed_by=str(user.get("user_id") or user.get("username") or ""),
                    idempotency_key=idempotency_key,
                    expires_at=body.expires_at,
                )
                result["request_id"] = request_id
                return result

        return execute(action)

    @router.patch("/approvals/{approval_id}")
    def transition_operation_approval(
        approval_id: str, body: StatusTransitionRequest, request: Request,
    ) -> Dict[str, Any]:
        user = operator(request)
        return execute(lambda: _transition_operation_approval(db, approval_id, body, user))

    @router.post("/patterns/mine", status_code=201)
    def mine_patterns(
        body: PatternMineRequest,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _mine_patterns(
            db, body.minimum_support, idempotency_key, request_id,
        ))

    @router.post("/strategy-recommendations", status_code=201)
    def create_strategy_recommendation(
        body: StrategyRecommendationCreateRequest,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)
        return execute(lambda: _create_strategy_recommendation(
            db, body, user, idempotency_key, request_id,
        ))

    @router.get("/strategy-recommendations/{strategy_recommendation_id}")
    def get_strategy_recommendation(
        strategy_recommendation_id: str, request: Request,
    ) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _get_strategy_recommendation(db, strategy_recommendation_id))

    @router.patch("/strategy-recommendations/{strategy_recommendation_id}")
    def transition_strategy_recommendation(
        strategy_recommendation_id: str, body: StatusTransitionRequest, request: Request,
    ) -> Dict[str, Any]:
        user = operator(request)
        return execute(lambda: _transition_strategy_recommendation(
            db, strategy_recommendation_id, body, user,
        ))

    @router.post("/strategy-recommendations/{strategy_recommendation_id}/execute", status_code=201)
    def execute_low_risk_strategy(
        strategy_recommendation_id: str,
        body: LowRiskExecuteRequest,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)
        return execute(lambda: _execute_low_risk_strategy(
            db, strategy_recommendation_id, body, user, idempotency_key, request_id,
        ))

    @router.post("/simulations", status_code=201)
    def create_simulation(
        body: SimulationCreateRequest,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _create_simulation(
            db, body, idempotency_key, request_id,
        ))

    @router.get("/simulations/{simulation_id}")
    def get_simulation(simulation_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _get_simulation(db, simulation_id))

    @router.get("/autonomy/policies/{account_id}")
    def get_autonomy_policy(account_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _get_autonomy_policy(db, account_id))

    @router.put("/autonomy/policies/{account_id}")
    def set_autonomy_policy(
        account_id: str, body: AutonomyPolicyRequest, request: Request,
    ) -> Dict[str, Any]:
        user = operator(request)
        return execute(lambda: _set_autonomy_policy(db, account_id, body, user))

    @router.get("/autonomy/capabilities/{account_id}")
    def get_autonomy_capabilities(account_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _get_autonomy_capabilities(db, account_id))

    @router.post("/next-actions/sync")
    def sync_next_actions(request: Request, account_id: str = Query("")) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _sync_next_actions(db, account_id))

    @router.get("/next-actions")
    def list_next_actions(
        request: Request, account_id: str = Query(""), status: str = Query(""),
        limit: int = Query(100, ge=1, le=500),
    ) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _list_next_actions(db, account_id, status, limit))

    return router


def create_ad_experiment_router(
    *, db: Any, require_admin: Callable[[Request], Dict[str, Any]],
    meta_session: Any = None, meta_access_token: str = "",
    meta_graph_root: str = "", meta_business_ids: Optional[List[str]] = None,
    meta_application_id: str = "1684703062404662",
    meta_store_url: str = "http://play.google.com/store/apps/details?id=com.timetrade.duitan",
    meta_regional_identity_account_id: str = "",
    meta_regional_beneficiary_id: str = "",
    meta_regional_payer_id: str = "",
) -> APIRouter:
    """Dashboard-facing aliases backed by the canonical Growth services."""
    router = APIRouter(prefix="/api/ops/ad-data-dashboard", tags=["ad-experiment-closed-loop"])

    def operator(request: Request) -> Dict[str, Any]:
        return dict(require_admin(request) or {})

    def actor(user: Dict[str, Any]) -> str:
        return str(user.get("user_id") or user.get("username") or "")

    def execute(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except GrowthError as exc:
            raise HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": str(exc)}) from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail={"code": "STATE_CONFLICT", "message": "database_constraint_conflict"}) from exc

    def exact_campaign_matches(account_id: str, campaign_name: str) -> List[Dict[str, Any]]:
        normalized_account = str(account_id or "").strip().removeprefix("act_")
        normalized_name = str(campaign_name or "").strip()
        if not meta_session or not meta_access_token or not meta_graph_root or not normalized_account or not normalized_name:
            raise HTTPException(
                status_code=503,
                detail={"code": "EXECUTION_UNAVAILABLE", "message": "meta_campaign_reconciliation_unavailable"},
            )
        url = f"{str(meta_graph_root).rstrip('/')}/act_{normalized_account}/campaigns"
        matches: List[Dict[str, Any]] = []
        after = ""
        for _ in range(10):
            params = {
                "access_token": meta_access_token,
                "fields": "id,name,status,effective_status,created_time,updated_time",
                "limit": 200,
            }
            if after:
                params["after"] = after
            response = meta_session.get(url, params=params, timeout=25)
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            body = response.json() if hasattr(response, "json") else {}
            if not isinstance(body, dict) or body.get("error"):
                raise HTTPException(
                    status_code=503,
                    detail={"code": "EXECUTION_UNAVAILABLE", "message": "meta_campaign_reconciliation_failed"},
                )
            matches.extend(
                dict(item) for item in list(body.get("data") or [])
                if str(dict(item).get("name") or "").strip() == normalized_name
            )
            paging = dict(body.get("paging") or {})
            cursors = dict(paging.get("cursors") or {})
            next_after = str(cursors.get("after") or "").strip()
            if not paging.get("next") or not next_after or next_after == after:
                break
            after = next_after
        return matches

    def exact_study_matches(business_id: str, study_name: str) -> List[Dict[str, Any]]:
        normalized_business = str(business_id or "").strip()
        normalized_name = str(study_name or "").strip()
        if not meta_session or not meta_access_token or not meta_graph_root or not normalized_business or not normalized_name:
            raise HTTPException(
                status_code=503,
                detail={"code": "EXECUTION_UNAVAILABLE", "message": "meta_study_reconciliation_unavailable"},
            )
        response = meta_session.get(
            f"{str(meta_graph_root).rstrip('/')}/{normalized_business}/ad_studies",
            params={
                "access_token": meta_access_token,
                "fields": "id,name,type,start_time,end_time",
                "limit": 100,
            },
            timeout=25,
        )
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        body = response.json() if hasattr(response, "json") else {}
        if not isinstance(body, dict) or body.get("error"):
            raise HTTPException(
                status_code=503,
                detail={"code": "EXECUTION_UNAVAILABLE", "message": "meta_study_reconciliation_failed"},
            )
        return [
            dict(item) for item in list(body.get("data") or [])
            if str(dict(item).get("name") or "").strip() == normalized_name
        ]

    def verified_account_page(account_id: str, page_id: str) -> Dict[str, Any]:
        normalized_account = str(account_id or "").strip().removeprefix("act_")
        normalized_page = str(page_id or "").strip()
        if (
            not meta_session or not meta_access_token or not meta_graph_root
            or not normalized_account or not normalized_page.isdigit()
        ):
            raise HTTPException(
                status_code=503,
                detail={"code": "EXECUTION_UNAVAILABLE", "message": "meta_page_verification_unavailable"},
            )
        url = f"{str(meta_graph_root).rstrip('/')}/act_{normalized_account}/ads"
        counts: Dict[str, int] = {}
        after = ""
        for _ in range(3):
            params: Dict[str, Any] = {
                "access_token": meta_access_token,
                "fields": "creative{object_story_spec}",
                "limit": 100,
            }
            if after:
                params["after"] = after
            response = meta_session.get(url, params=params, timeout=25)
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            body = response.json() if hasattr(response, "json") else {}
            if not isinstance(body, dict) or body.get("error"):
                raise HTTPException(
                    status_code=503,
                    detail={"code": "EXECUTION_UNAVAILABLE", "message": "meta_account_page_history_failed"},
                )
            for raw in list(body.get("data") or []):
                creative = dict(dict(raw or {}).get("creative") or {})
                story = dict(creative.get("object_story_spec") or {})
                observed = str(story.get("page_id") or "").strip()
                if observed:
                    counts[observed] = counts.get(observed, 0) + 1
            paging = dict(body.get("paging") or {})
            cursors = dict(paging.get("cursors") or {})
            next_after = str(cursors.get("after") or "").strip()
            if not paging.get("next") or not next_after or next_after == after:
                break
            after = next_after
        if counts.get(normalized_page, 0) <= 0:
            raise HTTPException(
                status_code=409,
                detail={"code": "STATE_CONFLICT", "message": "page_not_verified_for_account_history"},
            )
        response = meta_session.get(
            f"{str(meta_graph_root).rstrip('/')}/{normalized_page}",
            params={"access_token": meta_access_token, "fields": "id,name,is_published"},
            timeout=25,
        )
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        page = response.json() if hasattr(response, "json") else {}
        if (
            not isinstance(page, dict)
            or str(page.get("id") or "").strip() != normalized_page
            or page.get("is_published") is False
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "STATE_CONFLICT", "message": "page_not_currently_available"},
            )
        return {
            "account_id": normalized_account,
            "page_id": normalized_page,
            "page_name": str(page.get("name") or "").strip(),
            "historical_ad_count": int(counts.get(normalized_page) or 0),
            "verification": "account_ad_history_and_live_page_readback",
        }

    @router.get("/autonomy/{account_id}")
    def dashboard_autonomy(account_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _get_autonomy_capabilities(db, account_id))

    @router.get("/next-actions")
    def dashboard_next_actions(
        request: Request, account_id: str = Query(""), status: str = Query(""),
        limit: int = Query(100, ge=1, le=500),
    ) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _list_next_actions(db, account_id, status, limit))

    def validate_rejected_adset(plan: Dict[str, Any], task: Dict[str, Any]) -> str:
        current_step = str(task.get("current_step") or "").upper()
        if not current_step.endswith("_ADSET_CREATE"):
            return ""
        cell_key = current_step.removesuffix("_ADSET_CREATE")
        cell = next((
            dict(item or {}) for item in list(plan.get("cells") or [])
            if str(dict(item or {}).get("cell_key") or "").upper() == cell_key
        ), {})
        body = dict(dict(cell.get("steps") or {}).get("ADSET_CREATE") or {})
        object_ids = dict(decode_json(task.get("meta_object_ids_json"), {}))
        campaign_id = str(object_ids.get("campaign_id") or "").strip()
        if not body or not campaign_id:
            raise HTTPException(
                status_code=409,
                detail={"code": "STATE_CONFLICT", "message": "same_plan_continuation_evidence_missing"},
            )
        try:
            identities = configured_regional_regulation_identities(
                account_id=str(plan.get("target_account_id") or ""),
                targeting=dict(body.get("targeting") or {}),
                configured_account_id=meta_regional_identity_account_id,
                beneficiary_id=meta_regional_beneficiary_id,
                payer_id=meta_regional_payer_id,
            )
        except GrowthValidationError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "EXECUTION_UNAVAILABLE", "message": str(exc)},
            ) from exc
        if identities and not dict(body.get("regional_regulation_identities") or {}):
            body["regional_regulation_identities"] = identities
        request_body = {
            key: canonical_json(value) if isinstance(value, (dict, list)) else value
            for key, value in body.items()
        }
        request_body.update({
            "campaign_id": campaign_id,
            "status": "PAUSED",
            "execution_options": canonical_json(["validate_only"]),
            "access_token": meta_access_token,
        })
        response = meta_session.post(
            f"{str(meta_graph_root).rstrip('/')}/act_{str(plan.get('target_account_id') or '').removeprefix('act_')}/adsets",
            data=request_body, timeout=25,
        )
        result = response.json() if hasattr(response, "json") else {}
        error = dict(result.get("error") or {}) if isinstance(result, dict) else {}
        if error:
            subcode = str(error.get("error_subcode") or "unknown")
            message = "meta_advertiser_verification_required" if subcode == "3858634" else f"meta_adset_validation_failed:{subcode}"
            raise HTTPException(
                status_code=409,
                detail={"code": "EXTERNAL_PREREQUISITE", "message": message},
            )
        return current_step

    def ensure_new_account_launch_archive_table(conn: sqlite3.Connection) -> None:
        ensure_new_account_launch_retention_tables(conn)

    def new_account_launch_detail(conn: sqlite3.Connection, launch_id: str) -> Dict[str, Any]:
        normalized_launch_id = str(launch_id or "").strip()
        if not re.fullmatch(r"newacct_[a-z0-9]{20}", normalized_launch_id):
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "new_account_launch_not_found"})
        rows = conn.execute(
            """
            SELECT * FROM ad_experiment
            WHERE source_report_id=?
            ORDER BY created_at,experiment_code
            """,
            (normalized_launch_id,),
        ).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "new_account_launch_not_found"})
        ensure_new_account_launch_archive_table(conn)
        archive_row = conn.execute(
            "SELECT * FROM ad_new_account_launch_archive WHERE launch_id=?",
            (normalized_launch_id,),
        ).fetchone()
        if archive_row and str(archive_row["status"] or "").upper() == "PURGED":
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "new_account_launch_not_found"})
        archived = bool(archive_row and str(archive_row["status"] or "").upper() == "ARCHIVED")

        experiments: List[Dict[str, Any]] = []
        variants: List[Dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            hypothesis = decode_json(row["hypothesis_json"], {})
            variant = decode_json(row["variant_definition_json"], {})
            creative_direction = dict(hypothesis.get("creative_direction") or variant.get("creative_direction") or {})
            strategy = dict(hypothesis.get("audience_strategy") or variant.get("audience_strategy") or {})
            frozen_creative = dict(hypothesis.get("frozen_creative") or variant.get("frozen_creative") or {})
            copy_variant = dict(hypothesis.get("copy_variant") or variant.get("copy_variant") or {})
            meta_names = dict(hypothesis.get("meta_names") or variant.get("meta_names") or {})
            experiments.append({
                "experiment_id": str(row["experiment_id"]),
                "experiment_code": str(row["experiment_code"]),
                "launch_id": normalized_launch_id,
                "country": str(row["country"] or "").upper(),
                "campaign_name": str(meta_names.get("campaign") or ""),
                "state": str(row["state"]),
                "state_reason": str(row["state_reason"] or ""),
                "campaign_id": str(row["source_campaign_id"] or ""),
                "adset_id": str(row["source_adset_id"] or ""),
                "ad_id": str(row["source_ad_id"] or ""),
                "updated_at": str(row["updated_at"]),
            })
            variants.append({
                "variant": int(hypothesis.get("variant") or index),
                "creative_angle": str(hypothesis.get("creative_angle") or creative_direction.get("title") or ""),
                "creative_direction": creative_direction,
                "audience_strategy": strategy,
                "frozen_creative": frozen_creative,
                "copy_variant": copy_variant,
                "role": str(hypothesis.get("role") or variant.get("role") or ""),
                "test_variable": str(hypothesis.get("test_variable") or variant.get("test_variable") or "creative_direction"),
                "meta_names": meta_names,
                "initial_daily_budget": float(
                    variant.get("initial_daily_budget")
                    or creative_direction.get("initial_daily_budget")
                    or hypothesis.get("initial_daily_budget")
                    or 0
                ),
                "recommendation_id": str(row["source_recommendation_id"] or ""),
                "experiment_id": str(row["experiment_id"]),
                "experiment_code": str(row["experiment_code"]),
            })

        first_hypothesis = decode_json(rows[0]["hypothesis_json"], {})
        audience = dict(first_hypothesis.get("audience") or {})
        target = {
            "target_app": "tugao",
            "country": str(rows[0]["country"] or ""),
            "account_id": str(rows[0]["account_id"] or ""),
            "account_name": str(first_hypothesis.get("account_name") or ""),
            "daily_spend_target": float(first_hypothesis.get("daily_spend_target") or 0),
            "cpi_target": float(first_hypothesis.get("cpi_target") or 0),
            "page_id": str(first_hypothesis.get("page_id") or ""),
            "audience": audience,
            "experiment_mode": str(first_hypothesis.get("experiment_mode") or "creative_direction"),
            "test_variable": str(first_hypothesis.get("test_variable") or "creative_direction"),
            "frozen_creative": dict(first_hypothesis.get("frozen_creative") or {}),
        }

        performance_rows: List[sqlite3.Row] = []
        performance_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_creative_performance_daily'",
        ).fetchone()
        ad_ids = [item["ad_id"] for item in experiments if item["ad_id"]]
        if performance_table and ad_ids:
            placeholders = ",".join("?" for _ in ad_ids)
            performance_rows = conn.execute(
                f"""
                SELECT report_date_london,ad_id,spend,impressions,clicks,installs,
                       tugao_real_bind_count,data_quality_status,attribution_level
                FROM ad_creative_performance_daily
                WHERE ad_id IN ({placeholders})
                ORDER BY report_date_london,ad_id
                """,
                tuple(ad_ids),
            ).fetchall()

        def aggregate_performance(metric_rows: List[sqlite3.Row]) -> Dict[str, Any]:
            spend = sum(float(item["spend"] or 0) for item in metric_rows)
            impressions = sum(float(item["impressions"] or 0) for item in metric_rows)
            clicks = sum(float(item["clicks"] or 0) for item in metric_rows)
            installs = sum(float(item["installs"] or 0) for item in metric_rows)
            real_joins = sum(float(item["tugao_real_bind_count"] or 0) for item in metric_rows)
            dates = sorted({str(item["report_date_london"] or "") for item in metric_rows if item["report_date_london"]})
            return {
                "available": bool(metric_rows),
                "spend": round(spend, 4),
                "impressions": round(impressions, 4),
                "clicks": round(clicks, 4),
                "ctr": clicks / impressions if impressions else None,
                "installs": round(installs, 4),
                "cpi": spend / installs if installs else None,
                "real_bind_count": round(real_joins, 4),
                "real_bind_cpa": spend / real_joins if real_joins else None,
                "first_data_date": dates[0] if dates else "",
                "latest_data_date": dates[-1] if dates else "",
                "day_count": len(dates),
                "quality_statuses": sorted({str(item["data_quality_status"] or "") for item in metric_rows if item["data_quality_status"]}),
                "attribution_levels": sorted({str(item["attribution_level"] or "") for item in metric_rows if item["attribution_level"]}),
            }

        performance_by_ad: Dict[str, List[sqlite3.Row]] = {}
        for metric_row in performance_rows:
            performance_by_ad.setdefault(str(metric_row["ad_id"] or ""), []).append(metric_row)
        for experiment in experiments:
            experiment["performance"] = aggregate_performance(
                performance_by_ad.get(str(experiment["ad_id"] or ""), []),
            )

        delivery_performance = aggregate_performance(performance_rows)
        maturity_rule = decode_json(rows[0]["maturity_rule_json"], {})
        minimum_installs = int(maturity_rule.get("minimum_installs") or 100)
        minimum_real_joins = int(maturity_rule.get("minimum_real_joins") or 10)
        current_installs = float(delivery_performance["installs"] or 0)
        current_real_joins = float(delivery_performance["real_bind_count"] or 0)
        current_cpi = delivery_performance["cpi"]
        cpi_target = float(target["cpi_target"] or 0)
        sample_mature = current_installs >= minimum_installs and current_real_joins >= minimum_real_joins
        if current_cpi is None:
            cpi_status = "NO_INSTALLS"
        elif cpi_target > 0 and float(current_cpi) <= cpi_target:
            cpi_status = "ON_TARGET"
        else:
            cpi_status = "ABOVE_TARGET"
        if not delivery_performance["available"]:
            conclusion = "数据尚未回流"
            next_step = "保持当前状态，等待广告数据完成首轮同步。"
        elif not sample_mature:
            conclusion = "样本不足，暂不判断优胜素材"
            next_step = f"继续采集；达到 {minimum_installs} 次安装和 {minimum_real_joins} 次真实入会，或到 D1 / D3 / D7 检查点后再评估。"
        elif cpi_status == "ON_TARGET":
            conclusion = "样本已成熟，当前 CPI 达标"
            next_step = "结合真实入会成本比较三条广告，保留优胜素材并逐步放量。"
        else:
            conclusion = "样本已成熟，当前 CPI 未达目标"
            next_step = "检查各广告差异，暂停持续落后的素材并生成下一轮调整建议。"
        delivery_performance.update({
            "cpi_target": cpi_target,
            "cpi_status": cpi_status,
            "cpi_delta": (float(current_cpi) - cpi_target) if current_cpi is not None and cpi_target > 0 else None,
            "sample_status": "MATURE" if sample_mature else "IMMATURE",
            "minimum_installs": minimum_installs,
            "minimum_real_joins": minimum_real_joins,
            "conclusion_zh": conclusion,
            "next_step_zh": next_step,
        })

        jobs: List[Dict[str, Any]] = []
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='creative_pro_work_queue'",
        ).fetchone()
        if table_exists:
            experiment_service = AdExperimentService(conn)
            job_rows = conn.execute(
                """
                SELECT job_id,status,experiment_id,experiment_code,material_refs_json,
                       generation_plan_json,created_at,completed_at,error_code,error_message
                FROM creative_pro_work_queue
                WHERE json_extract(material_refs_json,'$.launch_id')=?
                ORDER BY created_at DESC,job_id DESC
                """,
                (normalized_launch_id,),
            ).fetchall()
            latest_by_experiment: Dict[str, Dict[str, Any]] = {}
            for row in job_rows:
                refs = decode_json(row["material_refs_json"], {})
                generation_plan = decode_json(row["generation_plan_json"], {})
                growth_experiment_id = str(refs.get("growth_experiment_id") or row["experiment_id"] or "")
                if not growth_experiment_id or growth_experiment_id in latest_by_experiment:
                    continue
                approved_creative = experiment_service.latest_approved_creative(growth_experiment_id)
                generation_request_id = str(generation_plan.get("generation_request_id") or "").strip()
                latest_image_row = conn.execute(
                    """
                    SELECT image_id,request_id,review_status,created_at
                    FROM creative_generated_images
                    WHERE COALESCE(LOWER(review_status),'') NOT IN ('deleted','archived')
                      AND (json_extract(metadata_json,'$.job_id')=? OR (? != '' AND request_id=?))
                    ORDER BY created_at DESC,image_id DESC LIMIT 1
                    """,
                    (str(row["job_id"]), generation_request_id, generation_request_id),
                ).fetchone()
                latest_image = {
                    "image_id": str(latest_image_row["image_id"]),
                    "request_id": str(latest_image_row["request_id"]),
                    "review_status": str(latest_image_row["review_status"] or ""),
                    "created_at": str(latest_image_row["created_at"] or ""),
                    "preview_url": f"/api/ops/ad-data-dashboard/creative-images/{str(latest_image_row['image_id'])}",
                } if latest_image_row else {}
                latest_by_experiment[growth_experiment_id] = {
                    "job_id": str(row["job_id"]),
                    "status": str(row["status"]),
                    "experiment_id": growth_experiment_id,
                    "experiment_code": str(row["experiment_code"] or ""),
                    "material_refs": refs,
                    "created_at": str(row["created_at"]),
                    "completed_at": str(row["completed_at"] or ""),
                    "error_code": str(row["error_code"] or ""),
                    "error_message": str(row["error_message"] or ""),
                    "latest_image": latest_image,
                    "approved_creative": approved_creative,
                }
            jobs = [
                latest_by_experiment[variant["experiment_id"]]
                for variant in variants
                if variant["experiment_id"] in latest_by_experiment
            ]

        job_statuses = [str(job.get("status") or "") for job in jobs]
        experiment_states = [str(item["state"]) for item in experiments]
        batch_plan_row = conn.execute(
            """
            SELECT operation_action_id,status,payload_json,created_at,updated_at FROM growth_operation_action
            WHERE json_extract(payload_json,'$.launch_id')=?
              AND json_extract(payload_json,'$.plan.plan_version') IN ('NEW_ACCOUNT_BATCH_V1','NEW_ACCOUNT_AUDIENCE_BATCH_V1')
            ORDER BY created_at DESC LIMIT 1
            """,
            (normalized_launch_id,),
        ).fetchone()
        batch_plan_payload = decode_json(batch_plan_row["payload_json"], {}) if batch_plan_row else {}
        batch_plan_snapshot = batch_plan_payload.get("plan") if isinstance(batch_plan_payload.get("plan"), dict) else {}
        batch_plan_campaign = batch_plan_snapshot.get("campaign") if isinstance(batch_plan_snapshot.get("campaign"), dict) else {}
        batch_task_row = conn.execute(
            """
            SELECT execution_task_id,status,current_step,error_code,error_message
            FROM meta_execution_task WHERE operation_action_id=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (str(batch_plan_row["operation_action_id"]),),
        ).fetchone() if batch_plan_row else None
        batch_task = dict(batch_task_row) if batch_task_row else {}
        batch_cells = list(batch_plan_snapshot.get("cells") or [])
        batch_page_id = str(
            dict(dict(dict(batch_cells[0] or {}).get("steps") or {}).get("CREATIVE_CREATE") or {})
            .get("object_story_spec", {}).get("page_id")
        ).strip() if batch_cells else ""
        page_repair_available = bool(
            str(batch_plan_row["status"] if batch_plan_row else "").upper() == "MANUAL_REVIEW"
            and str(batch_task.get("status") or "").upper() == "MANUAL_REVIEW"
            and str(batch_task.get("current_step") or "").upper() == "C1_AD_CREATE"
            and str(batch_task.get("error_code") or "") == "meta_result_uncertain"
            and "meta_graph_error:100:1815645" in str(batch_task.get("error_message") or "")
        )
        batch_plan = {
            "plan_id": str(batch_plan_row["operation_action_id"]),
            "status": str(batch_plan_row["status"]),
            "campaign_name": str(batch_plan_campaign.get("name") or "").strip(),
            "created_at": str(batch_plan_row["created_at"]),
            "updated_at": str(batch_plan_row["updated_at"]),
            "current_page_id": batch_page_id,
            "execution_status": str(batch_task.get("status") or ""),
            "current_step": str(batch_task.get("current_step") or ""),
            "page_repair_available": page_repair_available,
        } if batch_plan_row else {}
        retention = launch_retention_status(conn, normalized_launch_id)
        latest_delivery_action = conn.execute(
            """
            SELECT action_type FROM growth_operation_action
            WHERE action_type IN ('CREATE_PAUSED_AD','REACTIVATE_AD','PAUSE_AD','PAUSE_ADSET')
              AND (
                json_extract(payload_json,'$.launch_id')=?
                OR json_extract(payload_json,'$.plan.launch_id')=?
                OR json_extract(payload_json,'$.experiment_id') IN (
                    SELECT experiment_id FROM ad_experiment WHERE source_report_id=?
                )
              )
            ORDER BY updated_at DESC,created_at DESC LIMIT 1
            """,
            (normalized_launch_id, normalized_launch_id, normalized_launch_id),
        ).fetchone()
        latest_delivery_action_type = str(latest_delivery_action["action_type"] or "") if latest_delivery_action else ""
        failed_statuses = {"failed", "rejected", "cancelled", "expired"}
        advanced_states = {
            "WAITING_CREATE_APPROVAL", "CREATING_PAUSED_OBJECTS", "CREATION_PARTIAL_FAILURE",
            "META_REVIEW_PENDING", "READY_FOR_ACTIVATION", "RUNNING", "MATURING",
            "RECOMMENDATION_READY", "WAITING_ADJUSTMENT_APPROVAL", "ADJUSTING",
            "EVALUATING_ADJUSTMENT", "EFFECTIVE", "INEFFECTIVE", "INCONCLUSIVE",
            "DATA_INCOMPLETE", "MIXED_CHANGE", "PAUSED", "ARCHIVED",
        }
        if experiment_states and all(state == "PAUSED" for state in experiment_states):
            phase = "PAUSED"
            status_zh = "广告已暂停 · 等待启用"
        elif any(state in {"CREATION_PARTIAL_FAILURE", "DATA_INCOMPLETE", "MIXED_CHANGE"} for state in experiment_states):
            phase = "ATTENTION_REQUIRED"
            status_zh = (
                "开启投放失败 · 广告仍保持暂停"
                if latest_delivery_action_type == "REACTIVATE_AD"
                else "广告创建结果需要核对"
            )
        elif any(state == "META_REVIEW_PENDING" for state in experiment_states):
            phase = "META_REVIEW_PENDING"
            status_zh = "广告已创建并暂停 · 可管理投放"
        elif any(state == "READY_FOR_ACTIVATION" for state in experiment_states):
            phase = "READY_FOR_ACTIVATION"
            status_zh = "审核已通过 · 等待启用"
        elif any(state in {"RUNNING", "MATURING", "EVALUATING_ADJUSTMENT"} for state in experiment_states):
            phase = "RUNNING"
            status_zh = "投放观察中"
        elif any(state in advanced_states for state in experiment_states):
            phase = "AD_WORKFLOW"
            status_zh = "广告创建处理中"
        elif target["experiment_mode"] == "audience_strategy":
            phase = "READY_FOR_PLAN"
            status_zh = "AI 正在校验受众实验资格"
        elif jobs and len(jobs) == len(variants) and all(
            bool((job.get("approved_creative") or {}).get("image_id")) for job in jobs
        ):
            phase = "READY_FOR_PLAN"
            status_zh = "AI 正在生成并校验创建方案"
        elif any(status == "pending_review" for status in job_statuses):
            phase = "CREATIVE_REVIEW"
            status_zh = "AI 正在审核素材"
        elif any(
            status == "completed" and not (job.get("approved_creative") or {}).get("image_id")
            for status, job in zip(job_statuses, jobs)
        ):
            phase = "ATTENTION_REQUIRED"
            status_zh = "素材需要处理"
        elif any(status in failed_statuses for status in job_statuses):
            phase = "ATTENTION_REQUIRED"
            status_zh = "素材异常"
        elif jobs:
            phase = "CREATIVE_GENERATING"
            status_zh = "素材生成中"
        else:
            phase = "CREATIVE_SETUP_REQUIRED"
            status_zh = "AI 正在创建素材任务"

        return {
            "launch_id": normalized_launch_id,
            "created_at": str(rows[0]["created_at"]),
            "updated_at": max(str(row["updated_at"]) for row in rows),
            "phase": phase,
            "status_zh": status_zh,
            "latest_delivery_action_type": latest_delivery_action_type,
            "archived": archived,
            "archived_at": str(archive_row["archived_at"] or "") if archive_row else "",
            "can_permanently_delete": retention["can_permanently_delete"],
            "permanent_delete_blocked_reason": retention["permanent_delete_blocked_reason"],
            "permanent_delete_mode": retention["permanent_delete_mode"],
            "protected_audit_present": retention["protected_audit_present"],
            "purge_after": retention["purge_after"],
            "purge_due": retention["purge_due"],
            "retention_days": retention["retention_days"],
            "target": target,
            "experiment_count": len(variants),
            "experiments": experiments,
            "delivery_performance": delivery_performance,
            "jobs": jobs,
            "job_count": len(jobs),
            "batch_plan": batch_plan,
            "launch": {
                "launch_id": normalized_launch_id,
                "status": phase,
                "target": target,
                "variants": variants,
                "meta_writes_performed": False,
            },
            "meta_writes_performed": False,
        }

    @router.post("/experiments/draft", status_code=201)
    def create_draft(
        body: AdExperimentDraftRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)
        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                result = AdExperimentService(conn).create_draft(body.model_dump(), actor=actor(user), idempotency_key=idempotency_key)
                result["request_id"] = request_id
                return result
        return execute(action)

    @router.post("/new-account-launches/directions/preview")
    def preview_new_account_directions(
        body: NewAccountDirectionPreviewRequest,
        request: Request,
    ) -> Dict[str, Any]:
        operator(request)
        with db.connect() as conn:
            return execute(lambda: _generate_creative_direction_preview(conn, body))

    @router.get("/new-account-launches/audiences/preview")
    def preview_new_account_audiences(
        request: Request,
        country: str = Query("BR"),
    ) -> Dict[str, Any]:
        operator(request)
        country_code = str(country or "BR").strip().upper()
        policy = country_audience_policy(country_code)
        rounds = list(INITIAL_AUDIENCE_EXPERIMENT_POLICY.get("initial_br_rounds") or []) if country_code == "BR" else []
        strategies = []
        for key in ("BROAD", "DIGITAL_SELLER", "FAMILY_HOME", "SIDE_HUSTLE"):
            strategy = audience_strategy(key)
            strategies.append({
                **strategy,
                "delivery_estimate": dict(AUDIENCE_DELIVERY_ESTIMATE_SNAPSHOT.get(country_code, {}).get(key) or {}),
                "selectable": any(key in {str(item.get("baseline")), str(item.get("challenger"))} for item in rounds),
                "disabled_reason": str(dict(INITIAL_AUDIENCE_EXPERIMENT_POLICY.get("held_strategies") or {}).get(key) or ""),
            })
        return {
            "country": country_code,
            "base_conditions": audience_contract(country_code, "BROAD")["base_conditions"],
            "experiment_policy": {**INITIAL_AUDIENCE_EXPERIMENT_POLICY, "rounds": rounds},
            "strategies": strategies,
            "meta_writes_performed": False,
            "policy_note": "同一张已审核素材；每轮仅比较一组广泛受众和一组挑战受众。",
            "country_label": policy["country_label"],
        }

    @router.post("/new-account-launches/audience", status_code=201)
    def create_new_account_audience_launch(
        body: NewAccountAudienceLaunchRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)
        account_id = str(body.account_id or "").strip().removeprefix("act_")
        if not account_id.isdigit():
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "meta_account_id_must_be_numeric"})
        country = str(body.country or "BR").strip().upper()
        if country != "BR":
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "initial_audience_experiment_country_must_be_br"})
        requested_keys = [str(item or "").strip().upper() for item in body.audience_strategies]
        if len(requested_keys) != 2 or len(set(requested_keys)) != 2:
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "audience_experiment_requires_two_distinct_strategies"})
        allowed_rounds = [
            [str(item.get("baseline") or "").upper(), str(item.get("challenger") or "").upper()]
            for item in INITIAL_AUDIENCE_EXPERIMENT_POLICY.get("initial_br_rounds") or []
        ]
        matching_round = next((item for item in allowed_rounds if set(item) == set(requested_keys)), None)
        if not matching_round:
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "audience_experiment_pair_not_allowed"})
        requested_keys = matching_round
        page_id = str(body.page_id or "").strip()
        if not page_id.isdigit():
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "meta_page_id_must_be_numeric"})
        naming_date = str(body.naming_date or "").strip() or datetime.now(timezone.utc).strftime("%Y%m%d")
        if not re.fullmatch(r"\d{8}", naming_date):
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "invalid_naming_date"})
        audience = _resolve_new_account_audience(country=country, gender=None, language=None, age_min=None, age_max=None)
        with db.connect() as conn:
            frozen_creative = _approved_frozen_creative(conn, body.frozen_creative_id)
        launch_payload = {
            "target_app": "tugao", "country": country, "account_id": account_id,
            "account_name": str(body.account_name or "").strip(),
            "daily_spend_target": float(body.daily_spend_target), "cpi_target": float(body.cpi_target),
            "page_id": page_id, "audience": audience, "experiment_mode": "audience_strategy",
            "test_variable": "audience_strategy", "frozen_creative": frozen_creative,
            "audience_strategies": requested_keys, "naming_date": naming_date,
            "execution_policy": "PAIRWISE_RANDOMIZED_FAIL_CLOSED",
        }
        launch_id = f"newacct_{payload_hash(launch_payload)[:20]}"
        account_name = str(body.account_name or "").strip()[:120]
        if account_name:
            launch_payload["account_name"] = account_name
        strategy_codes = {"BROAD": "BD", "DIGITAL_SELLER": "DS", "FAMILY_HOME": "FH"}

        def action() -> Dict[str, Any]:
            variants = []
            with db.connect() as conn:
                ensure_ad_daily_report_tables(conn)
                _approved_frozen_creative(conn, body.frozen_creative_id)
                for index, strategy_key in enumerate(requested_keys, start=1):
                    strategy = audience_strategy(strategy_key)
                    names = _compact_meta_names(
                        country=country, gender=str(audience["gender"]), age_min=int(audience["age_min"]),
                        age_max=int(audience["age_max"]), language=str(audience["language"]),
                        naming_date=naming_date, direction_code=strategy_codes[strategy_key], cell_index=index,
                    )
                    recommendation_id = f"{launch_id}_v{index}"
                    role = "BASELINE" if strategy_key == "BROAD" else "CHALLENGER"
                    recommendation_payload = {
                        "recommendation_id": recommendation_id, "object_id": account_id,
                        "object_level": "account", "country": country, "project": "tugao",
                        "primary_action": "create_experiment", "primary_layer": "new_account_launch",
                        "diagnosis_type": "new_account_audience_test", "data_origin": "NATIVE_V2",
                        "decision_context": {
                            "platform": "meta", "business_goal": "acquisition",
                            "test_variable": "audience_strategy", "audience_strategy": strategy,
                            "frozen_creative": frozen_creative, "audience": audience,
                            "budget": float(body.initial_daily_budget), "bid_strategy": "COST_CAP",
                            "bid_amount": int(round(float(body.cpi_target) * 100)),
                            "cost_cap_usd": float(body.cpi_target),
                        },
                        "evidence": {"country_cap": float(body.cpi_target), "funnel_metrics": {"target_app": "tugao"}},
                        "new_account_launch": {**launch_payload, "launch_id": launch_id, "variant": index},
                    }
                    stored = canonical_json(recommendation_payload)
                    existing = conn.execute("SELECT payload_json FROM ad_recommendation WHERE recommendation_id=?", (recommendation_id,)).fetchone()
                    if existing and str(existing["payload_json"] or "") != stored:
                        raise HTTPException(status_code=409, detail={"code": "STATE_CONFLICT", "message": "new_account_launch_identity_conflict"})
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO ad_recommendation
                        (recommendation_id,report_id,object_id,primary_action,primary_action_zh,
                         confidence,status_tag,decision_context_json,data_origin,payload_json)
                        VALUES (?,?,?,'create_experiment','创建受众实验','medium','new_account_launch',?,'NATIVE_V2',?)
                        """,
                        (recommendation_id, launch_id, account_id, canonical_json(recommendation_payload["decision_context"]), stored),
                    )
                    decision = DecisionService(conn).create_decision(
                        recommendation_id=recommendation_id, selected_action="CREATE_PAUSED_AD",
                        rejected_actions=["INCREASE_BUDGET", "REACTIVATE_AD"],
                        decision_reason={
                            "type": "NEW_ACCOUNT_AUDIENCE_TEST", "launch_id": launch_id,
                            "test_variable": "audience_strategy", "audience_strategy": strategy,
                            "frozen_creative_id": frozen_creative["image_id"], "safety": "create_paused_only",
                        },
                        confidence=0.8, idempotency_key=f"{idempotency_key}:decision:{index}", decided_by=actor(user),
                    )
                    experiment = AdExperimentService(conn).create_draft(
                        {
                            "target_app": "tugao", "experiment_type": "NEW_AD_TEST",
                            "experiment_code": f"{country}-AS-{launch_id[-5:].upper()}-{index}",
                            "country": country, "platform": "meta", "account_id": account_id,
                            "source_report_id": launch_id, "source_recommendation_id": recommendation_id,
                            "source_creative_id": frozen_creative["image_id"],
                            "hypothesis_json": {
                                "mode": "new_account_launch", "experiment_mode": "audience_strategy",
                                "test_variable": "audience_strategy", "launch_id": launch_id, "variant": index,
                                "role": role, "audience_strategy": strategy, "frozen_creative": frozen_creative,
                                "meta_names": names, "daily_spend_target": float(body.daily_spend_target),
                                "cpi_target": float(body.cpi_target), "page_id": page_id, "audience": audience,
                                "account_name": account_name,
                            },
                            "primary_metric": "cpi",
                            "guardrail_metrics_json": ["installs", "cpi", "ctr", "real_bind_count", "real_bind_cpa"],
                            "maturity_rule_json": {"minimum_installs": 100, "minimum_real_joins": 10, "checkpoints": ["D1", "D3", "D7"]},
                            "stop_rule_json": {
                                "order_confirmed_auto_create_paused": True,
                                "requires_manual_approval": False,
                                "activation_requires_manual_approval": True,
                                "meta_objects_initial_status": "PAUSED",
                                "delivery_guardrails": new_account_delivery_guardrails(),
                            },
                            "variant_definition_json": {
                                "test_variable": "audience_strategy", "role": role,
                                "audience_strategy": strategy, "frozen_creative": frozen_creative,
                                "meta_names": names, "initial_daily_budget": float(body.initial_daily_budget),
                            },
                        },
                        actor=actor(user), idempotency_key=f"{idempotency_key}:experiment:{index}",
                    )
                    persisted = conn.execute("SELECT status,target_id FROM growth_decision WHERE decision_id=?", (decision["decision_id"],)).fetchone()
                    if persisted and persisted["status"] == "CREATED":
                        DecisionService(conn).bind_target(decision["decision_id"], target_type="EXPERIMENT", target_id=experiment["experiment_id"], actor=actor(user))
                    elif not persisted or str(persisted["target_id"] or "") != experiment["experiment_id"]:
                        raise HTTPException(status_code=409, detail={"code": "STATE_CONFLICT", "message": "new_account_launch_target_conflict"})
                    variants.append({
                        "variant": index, "role": role, "test_variable": "audience_strategy",
                        "audience_strategy": strategy, "frozen_creative": frozen_creative, "meta_names": names,
                        "initial_daily_budget": float(body.initial_daily_budget), "recommendation_id": recommendation_id,
                        "decision_id": decision["decision_id"], "episode_id": decision["episode_id"],
                        "experiment_id": experiment["experiment_id"], "experiment_code": experiment["experiment_code"],
                    })
                conn.commit()
            return {
                "launch_id": launch_id, "status": "READY_FOR_PLAN", "experiment_mode": "audience_strategy",
                "target": launch_payload, "variants": variants, "meta_writes_performed": False, "request_id": request_id,
            }

        return execute(action)

    @router.post("/new-account-launches/copy", status_code=201)
    def create_new_account_copy_launch(
        body: NewAccountCopyLaunchRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)
        account_id = str(body.account_id or "").strip().removeprefix("act_")
        page_id = str(body.page_id or "").strip()
        country = str(body.country or "BR").strip().upper()
        if not account_id.isdigit():
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "meta_account_id_must_be_numeric"})
        if not page_id.isdigit():
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "meta_page_id_must_be_numeric"})
        audience = _resolve_new_account_audience(country=country, gender=None, language=None, age_min=None, age_max=None)
        variants_input = [item.model_dump() for item in body.copy_variants]
        signatures = {
            canonical_json({
                "primary_text": str(item.get("primary_text") or "").strip(),
                "headline": str(item.get("headline") or "").strip(),
                "description": str(item.get("description") or "").strip(),
            }) for item in variants_input
        }
        if len(variants_input) != 2 or len(signatures) != 2:
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "copy_experiment_requires_two_distinct_versions"})
        if any(not all((str(item.get("primary_text") or "").strip(), str(item.get("headline") or "").strip(), str(item.get("hypothesis") or "").strip())) for item in variants_input):
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "copy_experiment_copy_and_hypothesis_required"})
        naming_date = str(body.naming_date or "").strip() or datetime.now(timezone.utc).strftime("%Y%m%d")
        if not re.fullmatch(r"\d{8}", naming_date):
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "invalid_naming_date"})
        with db.connect() as conn:
            frozen_creative = _approved_frozen_creative(conn, body.frozen_creative_id)
        launch_payload = {
            "target_app": "tugao", "country": country, "account_id": account_id,
            "daily_spend_target": float(body.daily_spend_target), "cpi_target": float(body.cpi_target),
            "page_id": page_id, "audience": audience, "experiment_mode": "copy_variant",
            "test_variable": "copy_variant", "frozen_creative": frozen_creative,
            "copy_variants": variants_input, "naming_date": naming_date,
            "execution_policy": "PAIRWISE_RANDOMIZED_FAIL_CLOSED",
        }
        launch_id = f"newacct_{payload_hash(launch_payload)[:20]}"

        def action() -> Dict[str, Any]:
            variants = []
            with db.connect() as conn:
                ensure_ad_daily_report_tables(conn)
                _approved_frozen_creative(conn, body.frozen_creative_id)
                for index, copy_item in enumerate(variants_input, start=1):
                    role = "BASELINE" if index == 1 else "CHALLENGER"
                    names = _compact_meta_names(
                        country=country, gender=str(audience["gender"]), age_min=int(audience["age_min"]),
                        age_max=int(audience["age_max"]), language=str(audience["language"]),
                        naming_date=naming_date, direction_code="CP", cell_index=index,
                    )
                    recommendation_id = f"{launch_id}_v{index}"
                    creative_direction = {
                        "key": "copy_variant", "code": f"CV{index}",
                        "title": "文案基准" if index == 1 else "文案挑战",
                        "summary": str(copy_item["hypothesis"]),
                    }
                    decision_context = {
                        "platform": "meta", "business_goal": "acquisition", "test_variable": "copy_variant",
                        "frozen_creative": frozen_creative, "audience": audience,
                        "copy_variant": copy_item, "budget": float(body.initial_daily_budget),
                        "bid_strategy": "COST_CAP",
                        "bid_amount": int(round(float(body.cpi_target) * 100)),
                        "cost_cap_usd": float(body.cpi_target),
                    }
                    recommendation_payload = {
                        "recommendation_id": recommendation_id, "object_id": account_id,
                        "object_level": "account", "country": country, "project": "tugao",
                        "primary_action": "create_experiment", "primary_layer": "new_account_launch",
                        "diagnosis_type": "new_account_copy_test", "data_origin": "NATIVE_V2",
                        "decision_context": decision_context,
                        "evidence": {"country_cap": float(body.cpi_target), "funnel_metrics": {"target_app": "tugao"}},
                        "new_account_launch": {**launch_payload, "launch_id": launch_id, "variant": index},
                    }
                    stored = canonical_json(recommendation_payload)
                    existing = conn.execute("SELECT payload_json FROM ad_recommendation WHERE recommendation_id=?", (recommendation_id,)).fetchone()
                    if existing and str(existing["payload_json"] or "") != stored:
                        raise HTTPException(status_code=409, detail={"code": "STATE_CONFLICT", "message": "new_account_launch_identity_conflict"})
                    conn.execute(
                        """INSERT OR IGNORE INTO ad_recommendation
                        (recommendation_id,report_id,object_id,primary_action,primary_action_zh,
                         confidence,status_tag,decision_context_json,data_origin,payload_json)
                        VALUES (?,?,?,'create_experiment','创建文案实验','medium','new_account_launch',?,'NATIVE_V2',?)""",
                        (recommendation_id, launch_id, account_id, canonical_json(decision_context), stored),
                    )
                    decision = DecisionService(conn).create_decision(
                        recommendation_id=recommendation_id, selected_action="CREATE_PAUSED_AD",
                        rejected_actions=["INCREASE_BUDGET", "REACTIVATE_AD"],
                        decision_reason={
                            "type": "NEW_ACCOUNT_COPY_TEST", "launch_id": launch_id,
                            "test_variable": "copy_variant", "copy_hypothesis": str(copy_item["hypothesis"]),
                            "frozen_creative_id": frozen_creative["image_id"], "safety": "create_paused_only",
                        },
                        confidence=0.8, idempotency_key=f"{idempotency_key}:decision:{index}", decided_by=actor(user),
                    )
                    experiment = AdExperimentService(conn).create_draft(
                        {
                            "target_app": "tugao", "experiment_type": "NEW_AD_TEST",
                            "experiment_code": f"{country}-CV-{launch_id[-5:].upper()}-{index}",
                            "country": country, "platform": "meta", "account_id": account_id,
                            "source_report_id": launch_id, "source_recommendation_id": recommendation_id,
                            "source_creative_id": frozen_creative["image_id"],
                            "hypothesis_json": {
                                "mode": "new_account_launch", "experiment_mode": "copy_variant",
                                "test_variable": "copy_variant", "launch_id": launch_id, "variant": index,
                                "role": role, "audience_strategy": audience_strategy("BROAD"),
                                "frozen_creative": frozen_creative, "creative_direction": creative_direction,
                                "copy_variant": copy_item, "meta_names": names,
                                "daily_spend_target": float(body.daily_spend_target),
                                "cpi_target": float(body.cpi_target), "page_id": page_id, "audience": audience,
                                "account_name": str(body.account_name or "").strip(),
                            },
                            "primary_metric": "real_bind_cpa",
                            "guardrail_metrics_json": ["installs", "cpi", "ctr", "real_bind_count", "real_bind_cpa"],
                            "maturity_rule_json": {"minimum_installs": 100, "minimum_real_joins": 10, "checkpoints": ["D1", "D3", "D7"]},
                            "stop_rule_json": {
                                "order_confirmed_auto_create_paused": True,
                                "requires_manual_approval": False,
                                "activation_requires_manual_approval": True,
                                "meta_objects_initial_status": "PAUSED",
                                "delivery_guardrails": new_account_delivery_guardrails(),
                            },
                            "variant_definition_json": {
                                "test_variable": "copy_variant", "role": role, "copy_variant": copy_item,
                                "frozen_creative": frozen_creative, "creative_direction": creative_direction,
                                "audience_strategy": audience_strategy("BROAD"), "meta_names": names,
                                "initial_daily_budget": float(body.initial_daily_budget),
                            },
                        },
                        actor=actor(user), idempotency_key=f"{idempotency_key}:experiment:{index}",
                    )
                    persisted = conn.execute("SELECT status,target_id FROM growth_decision WHERE decision_id=?", (decision["decision_id"],)).fetchone()
                    if persisted and persisted["status"] == "CREATED":
                        DecisionService(conn).bind_target(decision["decision_id"], target_type="EXPERIMENT", target_id=experiment["experiment_id"], actor=actor(user))
                    elif not persisted or str(persisted["target_id"] or "") != experiment["experiment_id"]:
                        raise HTTPException(status_code=409, detail={"code": "STATE_CONFLICT", "message": "new_account_launch_target_conflict"})
                    variants.append({
                        "variant": index, "role": role, "test_variable": "copy_variant",
                        "copy_variant": copy_item, "creative_direction": creative_direction,
                        "audience_strategy": audience_strategy("BROAD"), "frozen_creative": frozen_creative,
                        "meta_names": names, "initial_daily_budget": float(body.initial_daily_budget),
                        "recommendation_id": recommendation_id, "decision_id": decision["decision_id"],
                        "episode_id": decision["episode_id"], "experiment_id": experiment["experiment_id"],
                        "experiment_code": experiment["experiment_code"],
                    })
                conn.commit()
            return {
                "launch_id": launch_id, "status": "READY_FOR_PLAN", "experiment_mode": "copy_variant",
                "target": launch_payload, "variants": variants, "meta_writes_performed": False, "request_id": request_id,
            }

        return execute(action)

    @router.post("/new-account-launches", status_code=201)
    def create_new_account_launch(
        body: NewAccountLaunchRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)
        account_id = str(body.account_id or "").strip().removeprefix("act_")
        if not account_id.isdigit():
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "meta_account_id_must_be_numeric"})
        target_app = "tugao"
        country = str(body.country or "BR").strip().upper()
        audience = _resolve_new_account_audience(
            country=country, gender=body.gender, language=body.language,
            age_min=body.age_min, age_max=body.age_max,
        )
        gender = str(audience["gender"])
        language = str(audience["language"])
        naming_date = str(body.naming_date or "").strip() or datetime.now(timezone.utc).strftime("%Y%m%d")
        if not re.fullmatch(r"\d{8}", naming_date):
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "invalid_naming_date"})
        requested_directions = [item.model_dump() for item in body.creative_directions]
        if not requested_directions:
            with db.connect() as conn:
                generated = _generate_creative_direction_preview(
                    conn,
                    NewAccountDirectionPreviewRequest(
                        target_app=target_app,
                        country=country,
                        daily_spend_target=float(body.daily_spend_target),
                        cpi_target=float(body.cpi_target),
                        gender=gender,
                        age_min=int(audience["age_min"]),
                        age_max=int(audience["age_max"]),
                        language=language,
                    ),
                )
            requested_directions = [
                {
                    key: value
                    for key, value in item.items()
                    if key in CreativeDirectionSelection.model_fields
                }
                for item in generated["directions"]
                if item.get("selected")
            ]
            naming_date = str(generated["naming_date"])
        if len(requested_directions) < 2 or len(requested_directions) > len(_DIRECTION_CATALOG):
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "creative_direction_count_must_be_2_to_4"})
        normalized_directions = []
        seen_keys = set()
        for index, item in enumerate(requested_directions, start=1):
            requested_key = str(item.get("key") or item.get("direction_id") or "").strip().lower()
            requested_code = str(item.get("code") or "").strip().upper()
            catalog_item = _DIRECTION_BY_KEY.get(requested_key) or _DIRECTION_BY_CODE.get(requested_code)
            if not catalog_item:
                raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "invalid_fixed_creative_direction"})
            key = str(catalog_item["key"])
            code = str(catalog_item["code"])
            title = str(catalog_item["title"])
            hypothesis = str(item.get("hypothesis") or "").strip()
            if key in seen_keys:
                raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "duplicate_creative_direction"})
            if not hypothesis or len(hypothesis) > 180:
                raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "invalid_creative_direction_content"})
            seen_keys.add(key)
            normalized_directions.append({
                "direction_id": key,
                "key": key,
                "code": code,
                "title": title,
                "summary": str(catalog_item["summary"]),
                "hypothesis": hypothesis,
                "rationale": str(item.get("rationale") or "").strip()[:240],
                "source": str(item.get("source") or "audience_fit").strip()[:32],
                "initial_daily_budget": round(float(item.get("initial_daily_budget") or 20), 2),
                "meta_names": _compact_meta_names(
                    country=country,
                    gender=gender,
                    age_min=int(audience["age_min"]),
                    age_max=int(audience["age_max"]),
                    language=language,
                    naming_date=naming_date,
                    direction_code=code,
                    cell_index=index,
                ),
            })
        launch_payload = {
            "target_app": target_app,
            "country": country,
            "account_id": account_id,
            "daily_spend_target": float(body.daily_spend_target),
            "cpi_target": float(body.cpi_target),
            "page_id": str(body.page_id or "").strip(),
            "destination_url": str(body.destination_url or "").strip(),
            "audience": audience,
            "audience_contract": audience_contract(country, "BROAD"),
            "naming_date": naming_date,
            "creative_directions": normalized_directions,
        }
        launch_id = f"newacct_{payload_hash(launch_payload)[:20]}"
        # Keep the launch identity based on the legacy order payload while
        # making every persisted ad name unique to this specific order.
        for index, direction in enumerate(normalized_directions, start=1):
            names = dict(direction.get("meta_names") or {})
            names["ad"] = compact_launch_ad_name(
                str(direction.get("code") or "EXP"), naming_date, index,
                launch_id=launch_id,
            )
            direction["meta_names"] = names
        account_name = str(body.account_name or "").strip()[:120]
        if account_name:
            launch_payload["account_name"] = account_name

        def action() -> Dict[str, Any]:
            variants = []
            with db.connect() as conn:
                ensure_ad_daily_report_tables(conn)
                for index, direction in enumerate(normalized_directions, start=1):
                    creative_angle = str(direction["title"])
                    recommendation_id = f"{launch_id}_v{index}"
                    recommendation_payload = {
                        "recommendation_id": recommendation_id,
                        "object_id": account_id,
                        "object_level": "account",
                        "country": country,
                        "project": target_app,
                        "primary_action": "create_experiment",
                        "primary_layer": "new_account_launch",
                        "diagnosis_type": "new_account_cold_start",
                        "data_origin": "NATIVE_V2",
                        "decision_context": {
                            "platform": "meta", "business_goal": "acquisition",
                            "creative_type": "feed_static", "creative_angle": creative_angle,
                            "creative_direction": direction,
                            "audience": audience,
                            "budget": float(direction["initial_daily_budget"]), "bid_strategy": "COST_CAP",
                            "bid_amount": int(round(float(body.cpi_target) * 100)),
                            "cost_cap_usd": float(body.cpi_target),
                        },
                        "evidence": {
                            "country_cap": float(body.cpi_target),
                            "funnel_metrics": {"target_app": target_app},
                        },
                        "new_account_launch": {**launch_payload, "launch_id": launch_id, "variant": index},
                    }
                    stored = canonical_json(recommendation_payload)
                    existing = conn.execute(
                        "SELECT payload_json FROM ad_recommendation WHERE recommendation_id=?",
                        (recommendation_id,),
                    ).fetchone()
                    if existing and str(existing["payload_json"] or "") != stored:
                        raise HTTPException(status_code=409, detail={"code": "STATE_CONFLICT", "message": "new_account_launch_identity_conflict"})
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO ad_recommendation
                        (recommendation_id,report_id,object_id,primary_action,primary_action_zh,
                         confidence,status_tag,decision_context_json,data_origin,payload_json)
                        VALUES (?,?,?,'create_experiment','创建冷启动实验','medium','new_account_launch',?,'NATIVE_V2',?)
                        """,
                        (recommendation_id, launch_id, account_id, canonical_json(recommendation_payload["decision_context"]), stored),
                    )
                    decision = DecisionService(conn).create_decision(
                        recommendation_id=recommendation_id,
                        selected_action="CREATE_PAUSED_AD",
                        rejected_actions=["INCREASE_BUDGET", "REACTIVATE_AD"],
                        decision_reason={
                            "type": "NEW_ACCOUNT_COLD_START",
                            "launch_id": launch_id,
                            "creative_angle": creative_angle,
                            "creative_direction": direction,
                            "safety": "create_paused_only",
                        },
                        confidence=0.8,
                        idempotency_key=f"{idempotency_key}:decision:{index}",
                        decided_by=actor(user),
                    )
                    experiment = AdExperimentService(conn).create_draft(
                        {
                            "target_app": target_app, "experiment_type": "NEW_AD_TEST",
                            "experiment_code": f"{country}-CS-{launch_id[-5:].upper()}-{index}",
                            "country": country, "platform": "meta", "account_id": account_id,
                            "source_report_id": launch_id, "source_recommendation_id": recommendation_id,
                            "hypothesis_json": {
                                "mode": "new_account_launch", "launch_id": launch_id,
                                "variant": index, "creative_angle": creative_angle,
                                "creative_direction": direction,
                                "meta_names": direction["meta_names"],
                                "daily_spend_target": float(body.daily_spend_target),
                                "cpi_target": float(body.cpi_target),
                                "page_id": str(body.page_id or "").strip(),
                                "destination_url": str(body.destination_url or "").strip(),
                                "audience": audience,
                                "account_name": account_name,
                            },
                            "primary_metric": "cpi",
                            "guardrail_metrics_json": ["installs", "cpi", "ctr", "real_bind_count", "real_bind_cpa"],
                            "maturity_rule_json": {"minimum_installs": 100, "minimum_real_joins": 10},
                            "stop_rule_json": {
                                "order_confirmed_auto_create_paused": True,
                                "requires_manual_approval": False,
                                "activation_requires_manual_approval": True,
                                "meta_objects_initial_status": "PAUSED",
                                "delivery_guardrails": new_account_delivery_guardrails(),
                            },
                            "variant_definition_json": {
                                "creative_angle": creative_angle,
                                "creative_direction": direction,
                                "meta_names": direction["meta_names"],
                                "initial_daily_budget": float(direction["initial_daily_budget"]),
                            },
                        },
                        actor=actor(user), idempotency_key=f"{idempotency_key}:experiment:{index}",
                    )
                    persisted_decision = conn.execute(
                        "SELECT status,target_type,target_id FROM growth_decision WHERE decision_id=?",
                        (decision["decision_id"],),
                    ).fetchone()
                    if persisted_decision and persisted_decision["status"] == "CREATED":
                        DecisionService(conn).bind_target(
                            decision["decision_id"], target_type="EXPERIMENT",
                            target_id=experiment["experiment_id"], actor=actor(user),
                        )
                    elif not persisted_decision or str(persisted_decision["target_id"] or "") != experiment["experiment_id"]:
                        raise HTTPException(status_code=409, detail={"code": "STATE_CONFLICT", "message": "new_account_launch_target_conflict"})
                    variants.append({
                        "variant": index, "creative_angle": creative_angle,
                        "creative_direction": direction,
                        "meta_names": direction["meta_names"],
                        "initial_daily_budget": float(direction["initial_daily_budget"]),
                        "recommendation_id": recommendation_id,
                        "decision_id": decision["decision_id"], "episode_id": decision["episode_id"],
                        "experiment_id": experiment["experiment_id"], "experiment_code": experiment["experiment_code"],
                    })
                conn.commit()
            return {
                "launch_id": launch_id, "status": "CREATIVE_PREPARATION",
                "target": launch_payload, "variants": variants,
                "meta_writes_performed": False, "request_id": request_id,
            }

        return execute(action)

    @router.get("/new-account-launches")
    def list_new_account_launches(
        request: Request,
        limit: int = Query(50, ge=1, le=100),
        include_archived: bool = Query(False),
    ) -> Dict[str, Any]:
        operator(request)

        def action(conn: sqlite3.Connection) -> Dict[str, Any]:
            ensure_new_account_launch_archive_table(conn)
            launch_rows = conn.execute(
                """
                SELECT source_report_id,MAX(updated_at) AS updated_at
                FROM ad_experiment
                WHERE source_report_id LIKE 'newacct_%'
                  AND NOT EXISTS (
                    SELECT 1 FROM ad_new_account_launch_archive archive
                    WHERE archive.launch_id=ad_experiment.source_report_id
                      AND archive.status='PURGED'
                  )
                GROUP BY source_report_id
                ORDER BY updated_at DESC,source_report_id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            launches = [
                new_account_launch_detail(conn, str(row["source_report_id"]))
                for row in launch_rows
            ]
            archived_count = sum(1 for item in launches if item["archived"])
            if not include_archived:
                launches = [item for item in launches if not item["archived"]]
            return {
                "launches": launches,
                "count": len(launches),
                "archived_count": archived_count,
                "meta_writes_performed": False,
            }

        return execute(lambda: _with_connection(db, action))

    @router.get("/new-account-launches/{launch_id}")
    def get_new_account_launch(launch_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _with_connection(db, lambda conn: new_account_launch_detail(conn, launch_id)))

    @router.get("/new-account-launches/{launch_id}/delivery-status")
    def get_new_account_launch_delivery_status(launch_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                detail = new_account_launch_detail(conn, launch_id)
                try:
                    result = _read_meta_delivery_status(
                        session=meta_session, access_token=meta_access_token, graph_root=meta_graph_root,
                        experiments=[dict(item) for item in list(detail.get("experiments") or [])],
                    )
                except Exception as exc:
                    if isinstance(exc, HTTPException):
                        raise
                    raise HTTPException(status_code=503, detail={
                        "code": "EXECUTION_UNAVAILABLE", "message": "meta_delivery_status_readback_failed",
                    }) from exc
                result["launch_id"] = str(detail.get("launch_id") or launch_id)
                return result
        return execute(action)

    @router.post("/new-account-launches/{launch_id}/activate", status_code=201)
    def activate_new_account_launch(
        launch_id: str, body: NewAccountLaunchActivateRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        """Enable one shared Campaign and every delivery path in the order once."""
        user = operator(request)
        if str(body.confirmation or "").strip().upper() != "ENABLE_LAUNCH_DELIVERY":
            raise HTTPException(
                status_code=409,
                detail={"code": "STATE_CONFLICT", "message": "enable_launch_delivery_confirmation_required"},
            )

        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                detail = new_account_launch_detail(conn, launch_id)
                experiments = [dict(item) for item in list(detail.get("experiments") or [])]
                if not 2 <= len(experiments) <= 4:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "STATE_CONFLICT", "message": "launch_delivery_path_count_invalid"},
                    )
                account_ids = {str(dict(detail.get("target") or {}).get("account_id") or "").removeprefix("act_")}
                campaign_ids = {str(item.get("source_campaign_id") or item.get("campaign_id") or "") for item in experiments}
                if len(account_ids) != 1 or "" in account_ids or len(campaign_ids) != 1 or "" in campaign_ids:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "STATE_CONFLICT", "message": "launch_shared_campaign_readback_required"},
                    )
                account_id = next(iter(account_ids))
                if not _meta_live_execution_available("REACTIVATE_AD", account_id):
                    raise HTTPException(
                        status_code=503,
                        detail={"code": "EXECUTION_UNAVAILABLE", "message": "meta_reactivate_ad_not_available"},
                    )
                try:
                    live_delivery = _read_meta_delivery_status(
                        session=meta_session, access_token=meta_access_token, graph_root=meta_graph_root,
                        experiments=experiments,
                    )
                except Exception as exc:
                    raise HTTPException(status_code=503, detail={
                        "code": "EXECUTION_UNAVAILABLE", "message": "meta_delivery_status_readback_failed",
                    }) from exc
                allowed_states = {
                    "META_REVIEW_PENDING", "READY_FOR_ACTIVATION", "PAUSED", "WAITING_ADJUSTMENT_APPROVAL",
                    "RUNNING", "MATURING", "RECOMMENDATION_READY", "EVALUATING_ADJUSTMENT",
                    "EFFECTIVE", "INEFFECTIVE", "INCONCLUSIVE",
                }
                if any(str(item.get("state") or "") not in allowed_states for item in experiments):
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "STATE_CONFLICT", "message": "launch_delivery_not_ready"},
                    )
                if str(live_delivery.get("overall_state") or "") == "ACTIVE":
                    raise HTTPException(status_code=409, detail={
                        "code": "STATE_CONFLICT", "message": "launch_delivery_already_active",
                    })
                live_paths = {
                    str(item.get("experiment_id") or ""): dict(item)
                    for item in list(live_delivery.get("paths") or [])
                }
                delivery_paths = []
                for item in experiments:
                    live_path = live_paths.get(str(item.get("experiment_id") or "")) or {}
                    path = {
                        "experiment_id": str(item.get("experiment_id") or ""),
                        "campaign_id": str(item.get("source_campaign_id") or item.get("campaign_id") or ""),
                        "adset_id": str(item.get("source_adset_id") or item.get("adset_id") or ""),
                        "ad_id": str(item.get("source_ad_id") or item.get("ad_id") or ""),
                        "campaign_status": str(live_path.get("campaign_status") or ""),
                        "adset_status": str(live_path.get("adset_status") or ""),
                        "ad_status": str(live_path.get("ad_status") or ""),
                    }
                    if not all(path.values()) or any(
                        path[key] not in {"ACTIVE", "PAUSED"}
                        for key in ("campaign_status", "adset_status", "ad_status")
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail={"code": "STATE_CONFLICT", "message": "activation_object_readback_required"},
                        )
                    delivery_paths.append(path)
                baseline_id = str(experiments[0]["experiment_id"])
                decision = conn.execute(
                    """
                    SELECT d.decision_id,e.episode_id FROM growth_decision d
                    LEFT JOIN growth_decision_episode e ON e.decision_id=d.decision_id
                    WHERE d.target_type='EXPERIMENT' AND d.target_id=?
                    ORDER BY d.created_at DESC,e.created_at DESC LIMIT 1
                    """,
                    (baseline_id,),
                ).fetchone()
                if not decision:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "STATE_CONFLICT", "message": "launch_baseline_decision_required"},
                    )
                service = AdExperimentService(conn)
                plan_result = service.preview_plan(
                    baseline_id,
                    {
                        "decision_id": str(decision["decision_id"]),
                        "episode_id": str(decision["episode_id"] or ""),
                        "action_type": "REACTIVATE_AD",
                        "target_account_id": account_id,
                        "target_object_type": "LAUNCH",
                        "target_object_id": launch_id,
                        "launch_id": launch_id,
                        "delivery_paths": delivery_paths,
                        "before_json": {"status": "PAUSED"},
                        "after_json": {"status": "ACTIVE"},
                        "evaluation_window": {"checkpoints": ["D1", "D3", "D7"]},
                    },
                    actor=actor(user), idempotency_key=f"{idempotency_key}:plan",
                )
                plan_id = str(plan_result["plan_id"])
                current = service.plan_detail(plan_id)
                plan = dict(current["plan"])
                approval = dict(current.get("approval") or {})
                approval_id = str(approval.get("approval_id") or "")
                if str(approval.get("status") or "") == "PROPOSED":
                    approval = OperationApprovalService(conn).transition(
                        approval_id, "APPROVED", actor=actor(user),
                        single_operator_confirmation="APPROVE_EXACT_PLAN",
                    )
                dry_run = _idempotent_api_mutation(
                    conn, "ad_experiment.plan_dry_run", f"{idempotency_key}:dry-run",
                    {"plan_id": plan_id, "execution_mode": "dry_run", "plan_hash": payload_hash(plan)},
                    lambda: _build_dry_run_receipt(plan_id, plan, approval, "dry_run"),
                )
                task = ExecutionTaskService(conn).enqueue_task(
                    plan_id, idempotency_key=f"{idempotency_key}:live",
                    payload={
                        "execution_mode": "live", "approval_id": approval_id,
                        "account_id": account_id, "plan": plan,
                        "experiment_id": baseline_id,
                        "experiment_ids": [item["experiment_id"] for item in delivery_paths],
                        "launch_id": launch_id,
                    },
                )
                return {
                    "launch_id": launch_id, "plan_id": plan_id,
                    "execution_task": task,
                    "dry_run_verified": dry_run.get("status") == "DRY_RUN_VERIFIED",
                    "delivery_path_count": len(delivery_paths),
                    "meta_writes_performed": False, "request_id": request_id,
                }

        return execute(action)

    @router.post("/new-account-launches/{launch_id}/create-plan/preview", status_code=201)
    def preview_new_account_batch_create_plan(
        launch_id: str, body: NewAccountBatchPlanRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)
        if not 2 <= len(body.cells) <= 4:
            raise HTTPException(
                status_code=400,
                detail={"code": "VALIDATION_ERROR", "message": "launch_plan_cell_count_must_be_between_2_and_4"},
            )
        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                result = AdExperimentService(conn).preview_launch_create_plan(
                    launch_id, body.model_dump(), actor=actor(user), idempotency_key=idempotency_key,
                )
                result["request_id"] = request_id
                return result
        return execute(action)

    @router.post("/new-account-launches/{launch_id}/archive")
    def archive_new_account_launch(launch_id: str, request: Request) -> Dict[str, Any]:
        user = operator(request)

        def action(conn: sqlite3.Connection) -> Dict[str, Any]:
            detail = new_account_launch_detail(conn, launch_id)
            now = utc_now()
            with conn:
                conn.execute(
                    """
                    INSERT INTO ad_new_account_launch_archive
                    (launch_id,status,archived_at,archived_by,restored_at,restored_by,updated_at)
                    VALUES (?,'ARCHIVED',?,?, '', '',?)
                    ON CONFLICT(launch_id) DO UPDATE SET
                        status='ARCHIVED',archived_at=excluded.archived_at,
                        archived_by=excluded.archived_by,updated_at=excluded.updated_at
                    """,
                    (detail["launch_id"], now, actor(user), now),
                )
            return {
                "launch_id": detail["launch_id"],
                "status": "ARCHIVED",
                "archived": True,
                "meta_writes_performed": False,
            }

        return execute(lambda: _with_connection(db, action))

    @router.post("/new-account-launches/{launch_id}/restore")
    def restore_new_account_launch(launch_id: str, request: Request) -> Dict[str, Any]:
        user = operator(request)

        def action(conn: sqlite3.Connection) -> Dict[str, Any]:
            detail = new_account_launch_detail(conn, launch_id)
            now = utc_now()
            with conn:
                conn.execute(
                    """
                    INSERT INTO ad_new_account_launch_archive
                    (launch_id,status,archived_at,archived_by,restored_at,restored_by,updated_at)
                    VALUES (?,'ACTIVE','', '',?,?,?)
                    ON CONFLICT(launch_id) DO UPDATE SET
                        status='ACTIVE',restored_at=excluded.restored_at,
                        restored_by=excluded.restored_by,updated_at=excluded.updated_at
                    """,
                    (detail["launch_id"], now, actor(user), now),
                )
            return {
                "launch_id": detail["launch_id"],
                "status": "ACTIVE",
                "archived": False,
                "meta_writes_performed": False,
            }

        return execute(lambda: _with_connection(db, action))

    @router.get("/new-account-launches/{launch_id}/permanent-delete-preview")
    def preview_permanent_delete_new_account_launch(
        launch_id: str, request: Request,
    ) -> Dict[str, Any]:
        operator(request)

        def action(conn: sqlite3.Connection) -> Dict[str, Any]:
            detail = new_account_launch_detail(conn, launch_id)
            account_id = str(dict(detail.get("target") or {}).get("account_id") or "")
            return NewAccountLaunchMetaDeleteService(
                conn, session=meta_session, access_token=meta_access_token,
                graph_root=meta_graph_root,
                live_delete_enabled=_meta_live_execution_available(
                    "DELETE_LAUNCH_META_OBJECTS", account_id,
                ),
            ).preview(detail["launch_id"])

        try:
            return execute(lambda: _with_connection(db, action))
        except LaunchMetaDeleteConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "STATE_CONFLICT", "message": str(exc)},
            ) from exc

    @router.post("/new-account-launches/{launch_id}/permanent-delete")
    def permanently_delete_new_account_launch(
        launch_id: str, request: Request,
        body: Optional[NewAccountLaunchPermanentDeleteRequest] = None,
        idempotency_key: str = Header("", alias="Idempotency-Key"),
    ) -> Dict[str, Any]:
        user = operator(request)

        def action(conn: sqlite3.Connection) -> Dict[str, Any]:
            detail = new_account_launch_detail(conn, launch_id)
            if not detail["can_permanently_delete"]:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "STATE_CONFLICT",
                        "message": detail["permanent_delete_blocked_reason"] or "launch_cannot_be_permanently_deleted",
                    },
                )
            request_body = body or NewAccountLaunchPermanentDeleteRequest()
            mode = str(request_body.mode or "ORDER_ONLY").strip().upper()
            if mode == DELETE_MODE:
                account_id = str(dict(detail.get("target") or {}).get("account_id") or "")
                service = NewAccountLaunchMetaDeleteService(
                    conn, session=meta_session, access_token=meta_access_token,
                    graph_root=meta_graph_root,
                    live_delete_enabled=_meta_live_execution_available(
                        "DELETE_LAUNCH_META_OBJECTS", account_id,
                    ),
                )
                try:
                    return service.execute(
                        detail["launch_id"], actor=actor(user),
                        confirmation=request_body.confirmation,
                        plan_hash_value=request_body.plan_hash,
                        idempotency_key=idempotency_key,
                    )
                except LaunchMetaDeleteConflict as exc:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "STATE_CONFLICT", "message": str(exc)},
                    ) from exc
                except LaunchMetaDeleteManualReview as exc:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "MANUAL_REVIEW_REQUIRED", "message": str(exc)},
                    ) from exc
            if mode != "ORDER_ONLY":
                raise HTTPException(
                    status_code=400,
                    detail={"code": "VALIDATION_ERROR", "message": "invalid_permanent_delete_mode"},
                )
            try:
                result = purge_new_account_launch(
                    conn,
                    detail["launch_id"],
                    actor=actor(user),
                    reason="manual_permanent_delete",
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "STATE_CONFLICT", "message": str(exc)},
                ) from exc
            return {**result, "meta_writes_performed": False}

        return execute(lambda: _with_connection(db, action))

    @router.get("/experiments")
    def list_experiments(
        request: Request,
        state: str = Query(""),
        limit: int = Query(50, ge=1, le=200),
        task_index: bool = Query(False),
    ) -> Dict[str, Any]:
        operator(request)
        def action(conn: sqlite3.Connection) -> Dict[str, Any]:
            excluded_states = (
                ["META_REVIEW_PENDING", "RUNNING", "MATURING", "EVALUATING_ADJUSTMENT", "ARCHIVED"]
                if task_index and not str(state or "").strip()
                else None
            )
            result = AdExperimentService(conn).list(
                state=state, limit=limit, exclude_states=excluded_states,
            )
            result["items"] = [
                {**item, "workflow": _ad_experiment_workflow(conn, item)}
                for item in result["items"]
            ]
            buckets: Dict[str, int] = {}
            for item in result["items"]:
                bucket = str(item["workflow"].get("bucket") or "all")
                buckets[bucket] = buckets.get(bucket, 0) + 1
            result["work_queue"] = buckets
            result["task_index"] = bool(task_index)
            return result
        return execute(lambda: _with_connection(db, action))

    @router.get("/experiments/{experiment_id}")
    def get_experiment(experiment_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _with_connection(db, lambda conn: _ad_experiment_detail(conn, experiment_id)))

    @router.post("/experiments/{experiment_id}/delivery-incident/reconcile")
    def reconcile_delivery_incident(
        experiment_id: str, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        """Read the real Meta delivery path before allowing a fresh activation."""
        user = operator(request)

        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                service = AdExperimentService(conn)
                experiment = service.get(experiment_id)
                request_payload = {
                    "experiment_id": experiment_id,
                    "state": str(experiment.get("state") or ""),
                    "object_ids": {
                        "campaign": str(experiment.get("source_campaign_id") or ""),
                        "adset": str(experiment.get("source_adset_id") or ""),
                        "ad": str(experiment.get("source_ad_id") or ""),
                    },
                }

                def reconcile() -> Dict[str, Any]:
                    current_state = str(experiment.get("state") or "").upper()
                    current_reason = str(experiment.get("state_reason") or "")
                    if current_state not in {"DATA_INCOMPLETE", "PAUSED"}:
                        raise HTTPException(status_code=409, detail={"code": "STATE_CONFLICT", "message": "delivery_incident_not_reconcilable"})
                    if current_state == "PAUSED" and not current_reason.startswith("incident_reconciled_"):
                        raise HTTPException(status_code=409, detail={"code": "STATE_CONFLICT", "message": "delivery_incident_not_reconcilable"})
                    if not meta_session or not meta_access_token or not meta_graph_root:
                        raise HTTPException(status_code=503, detail={"code": "EXECUTION_UNAVAILABLE", "message": "meta_delivery_readback_unavailable"})
                    required_ids = dict(request_payload["object_ids"])
                    if not all(required_ids.values()):
                        raise HTTPException(status_code=409, detail={"code": "STATE_CONFLICT", "message": "delivery_object_readback_required"})

                    labels = {"campaign": "广告系列", "adset": "广告组", "ad": "广告"}
                    objects: List[Dict[str, Any]] = []
                    for kind in ("campaign", "adset", "ad"):
                        object_id = str(required_ids[kind])
                        try:
                            response = meta_session.get(
                                f"{str(meta_graph_root).rstrip('/')}/{object_id}",
                                params={"access_token": meta_access_token, "fields": "id,name,status,effective_status"},
                                timeout=25,
                            )
                            if hasattr(response, "raise_for_status"):
                                response.raise_for_status()
                            body = response.json() if hasattr(response, "json") else {}
                        except Exception as exc:
                            raise HTTPException(status_code=503, detail={"code": "EXECUTION_UNAVAILABLE", "message": "meta_delivery_readback_failed"}) from exc
                        if not isinstance(body, dict) or body.get("error") or str(body.get("id") or "") != object_id:
                            raise HTTPException(status_code=503, detail={"code": "EXECUTION_UNAVAILABLE", "message": "meta_delivery_readback_failed"})
                        objects.append({
                            "kind": kind,
                            "label": labels[kind],
                            "name": str(body.get("name") or "")[:160],
                            "object_tail": object_id[-6:],
                            "status": str(body.get("status") or "UNKNOWN").upper(),
                            "effective_status": str(body.get("effective_status") or "UNKNOWN").upper(),
                        })

                    all_paused = all(item["status"] == "PAUSED" for item in objects)
                    any_active = any(item["status"] == "ACTIVE" or item["effective_status"] == "ACTIVE" for item in objects)
                    if all_paused and current_state == "DATA_INCOMPLETE":
                        experiment_result = service.transition(
                            experiment_id, "PAUSED", actor=actor(user),
                            reason="incident_reconciled_delivery_all_paused",
                            event_type="DELIVERY_INCIDENT_RECONCILED",
                            evidence={"objects": objects, "old_write_replayed": False, "meta_writes_performed": False},
                        )
                    else:
                        experiment_result = service.get(experiment_id)
                    return {
                        "ok": True,
                        "experiment_id": experiment_id,
                        "resolution": "ALL_PAUSED_RETRY_ALLOWED" if all_paused else ("DELIVERY_ALREADY_ACTIVE" if any_active else "MIXED_STATUS_MANUAL_REVIEW"),
                        "safe_to_retry": all_paused,
                        "objects": objects,
                        "experiment": experiment_result,
                        "old_write_replayed": False,
                        "meta_writes_performed": False,
                        "request_id": request_id,
                    }

                return _idempotent_api_mutation(
                    conn, "ad_experiment.delivery_incident_reconcile", idempotency_key,
                    request_payload, reconcile,
                )

        return execute(action)

    @router.get("/experiments/{experiment_id}/timeline")
    def get_timeline(experiment_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _with_connection(db, lambda conn: AdExperimentService(conn).timeline(experiment_id)))

    @router.post("/experiments/{experiment_id}/meta-review/refresh")
    def refresh_meta_review(
        experiment_id: str, body: AdExperimentMetaReviewRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)
        review_status = str(body.review_status or "").strip().upper()
        if review_status not in {"PENDING", "APPROVED", "REJECTED", "DATA_INCOMPLETE"}:
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "invalid_meta_review_status"})
        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                request_payload = {"experiment_id": experiment_id, **body.model_dump()}
                def mutate() -> Dict[str, Any]:
                    service = AdExperimentService(conn)
                    if review_status == "PENDING":
                        return service.get(experiment_id)
                    target = "READY_FOR_ACTIVATION" if review_status == "APPROVED" else ("ARCHIVED" if review_status == "REJECTED" else "DATA_INCOMPLETE")
                    current = service.get(experiment_id)
                    if current["state"] == target:
                        return current
                    return service.transition(
                            experiment_id, target, actor=actor(user), reason=body.reason or review_status,
                            event_type="META_REVIEW_REFRESHED", evidence=body.evidence_json,
                        )
                result = _idempotent_api_mutation(conn, "ad_experiment.meta_review", idempotency_key, request_payload, mutate)
                result["request_id"] = request_id
                return result
        return execute(action)

    def preview_plan(experiment_id: str, body: AdExperimentPlanRequest, request: Request, idempotency_key: str, request_id: str) -> Dict[str, Any]:
        user = operator(request)
        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                result = AdExperimentService(conn).preview_plan(
                    experiment_id, body.model_dump(), actor=actor(user), idempotency_key=idempotency_key,
                )
                result["request_id"] = request_id
                return result
        return execute(action)

    @router.post("/experiments/{experiment_id}/create-plan/preview", status_code=201)
    def preview_create_plan(
        experiment_id: str, body: AdExperimentPlanRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        if str(body.action_type).upper() != "CREATE_PAUSED_AD":
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "create_plan_requires_create_paused_ad"})
        return preview_plan(experiment_id, body, request, idempotency_key, request_id)

    @router.post("/experiments/{experiment_id}/activation-plan/preview", status_code=201)
    def preview_activation_plan(
        experiment_id: str, body: AdExperimentPlanRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        if str(body.action_type).upper() != "REACTIVATE_AD":
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "activation_plan_requires_reactivate_ad"})
        return preview_plan(experiment_id, body, request, idempotency_key, request_id)

    @router.post("/experiments/{experiment_id}/activate", status_code=201)
    def activate_experiment(
        experiment_id: str, body: AdExperimentActivateRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        """Confirm once, then reuse the existing objects and governed Plan."""
        user = operator(request)
        if str(body.confirmation or "").strip().upper() != "ENABLE_DELIVERY":
            raise HTTPException(
                status_code=409,
                detail={"code": "STATE_CONFLICT", "message": "enable_delivery_confirmation_required"},
            )

        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                service = AdExperimentService(conn)
                experiment = service.get(experiment_id)
                account_id = str(experiment.get("account_id") or "").removeprefix("act_")
                if not _meta_live_execution_available("REACTIVATE_AD", account_id):
                    raise HTTPException(
                        status_code=503,
                        detail={"code": "EXECUTION_UNAVAILABLE", "message": "meta_reactivate_ad_not_available"},
                    )
                required_ids = {
                    "campaign": str(experiment.get("source_campaign_id") or ""),
                    "adset": str(experiment.get("source_adset_id") or ""),
                    "ad": str(experiment.get("source_ad_id") or ""),
                }
                if not account_id or not all(required_ids.values()):
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "STATE_CONFLICT", "message": "activation_object_readback_required"},
                    )

                replay_task = conn.execute(
                    "SELECT * FROM meta_execution_task WHERE idempotency_key=?",
                    (f"{idempotency_key}:live",),
                ).fetchone()
                if replay_task:
                    replay_action = ExecutionTaskService(conn).get_operation_action(
                        str(replay_task["operation_action_id"])
                    )
                    replay_payload = dict(replay_action.get("payload_json") or {})
                    if str(replay_payload.get("experiment_id") or "") != experiment_id:
                        raise HTTPException(
                            status_code=409,
                            detail={"code": "STATE_CONFLICT", "message": "idempotency_key_payload_conflict"},
                        )
                    return {
                        "experiment_id": experiment_id,
                        "plan_id": str(replay_task["operation_action_id"]),
                        "execution_task": ExecutionTaskService._serialize_task(replay_task),
                        "dry_run_verified": True,
                        "meta_writes_performed": False,
                        "request_id": request_id,
                    }

                existing_detail: Dict[str, Any] = {}
                rows = conn.execute(
                    "SELECT operation_action_id,payload_json,status FROM growth_operation_action "
                    "WHERE action_type='REACTIVATE_AD' ORDER BY created_at DESC"
                ).fetchall()
                for row in rows:
                    payload = decode_json(row["payload_json"], {})
                    if (
                        str(payload.get("experiment_id") or "") == experiment_id
                        and str(row["status"] or "") == "CREATED"
                    ):
                        candidate = service.plan_detail(str(row["operation_action_id"]))
                        approval = dict(candidate.get("approval") or {})
                        if not str(approval.get("consumed_at") or "").strip():
                            existing_detail = candidate
                            break

                if existing_detail:
                    plan_result = {
                        "plan_id": str(existing_detail["plan_id"]),
                        "plan": dict(existing_detail["plan"]),
                    }
                else:
                    plan_result = service.preview_plan(
                        experiment_id,
                        {
                            "decision_id": body.decision_id,
                            "episode_id": body.episode_id,
                            "action_type": "REACTIVATE_AD",
                            "target_account_id": account_id,
                            "target_object_type": "DELIVERY_PATH",
                            "target_object_id": required_ids["ad"],
                            "before_json": {"status": "PAUSED"},
                            "after_json": {"status": "ACTIVE"},
                            "steps": {
                                "CAMPAIGN_STATUS_UPDATE": {"target_id": required_ids["campaign"], "status": "ACTIVE"},
                                "ADSET_STATUS_UPDATE": {"target_id": required_ids["adset"], "status": "ACTIVE"},
                                "AD_STATUS_UPDATE": {"target_id": required_ids["ad"], "status": "ACTIVE"},
                            },
                            "max_write_requests": 3,
                            "evaluation_window": {"checkpoints": ["D1", "D3", "D7"]},
                        },
                        actor=actor(user), idempotency_key=f"{idempotency_key}:plan",
                    )

                plan_id = str(plan_result["plan_id"])
                current_detail = service.plan_detail(plan_id)
                plan = dict(current_detail["plan"])
                approval = dict(current_detail.get("approval") or {})
                approval_id = str(approval.get("approval_id") or "")
                if str(approval.get("status") or "") == "PROPOSED":
                    approval = OperationApprovalService(conn).transition(
                        approval_id, "APPROVED", actor=actor(user),
                        single_operator_confirmation="APPROVE_EXACT_PLAN",
                    )
                dry_run = _idempotent_api_mutation(
                    conn, "ad_experiment.plan_dry_run", f"{idempotency_key}:dry-run",
                    {"plan_id": plan_id, "execution_mode": "dry_run", "plan_hash": payload_hash(plan)},
                    lambda: _build_dry_run_receipt(plan_id, plan, approval, "dry_run"),
                )
                task = ExecutionTaskService(conn).enqueue_task(
                    plan_id, idempotency_key=f"{idempotency_key}:live",
                    payload={
                        "execution_mode": "live", "approval_id": approval_id,
                        "account_id": account_id, "plan": plan, "experiment_id": experiment_id,
                    },
                )
                return {
                    "experiment_id": experiment_id,
                    "plan_id": plan_id,
                    "execution_task": task,
                    "dry_run_verified": dry_run.get("status") == "DRY_RUN_VERIFIED",
                    "meta_writes_performed": False,
                    "request_id": request_id,
                }

        return execute(action)

    @router.post("/experiments/{experiment_id}/adjustment-plan/preview", status_code=201)
    def preview_adjustment_plan(
        experiment_id: str, body: AdExperimentPlanRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        if str(body.action_type).upper() == "CREATE_PAUSED_AD":
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "adjustment_plan_cannot_create_ad"})
        return preview_plan(experiment_id, body, request, idempotency_key, request_id)

    @router.post("/meta-plans/{plan_id}/approve")
    def approve_plan(
        plan_id: str, body: AdExperimentApproveRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)
        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                def mutate() -> Dict[str, Any]:
                    detail = AdExperimentService(conn).plan_detail(plan_id)
                    approval_id = str(detail["approval"].get("approval_id") or "")
                    if detail["approval"].get("status") == "APPROVED":
                        return detail["approval"]
                    return OperationApprovalService(conn).transition(
                        approval_id, "APPROVED", actor=actor(user),
                        single_operator_confirmation=body.confirmation,
                    )
                result = _idempotent_api_mutation(
                    conn, "ad_experiment.plan_approve", idempotency_key,
                    {
                        "plan_id": plan_id, "status": "APPROVED",
                        "confirmation": body.confirmation,
                    }, mutate,
                )
                result.update({"plan_id": plan_id, "request_id": request_id})
                return result
        return execute(action)

    @router.post("/meta-plans/{plan_id}/execute", status_code=201)
    def execute_plan(
        plan_id: str, body: AdExperimentExecuteRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)
        mode = str(body.execution_mode or "dry_run").lower()
        if mode not in {"fake", "dry_run", "live"}:
            raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "invalid_execution_mode"})
        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                detail = AdExperimentService(conn).plan_detail(plan_id)
                approval_id = str(detail["approval"].get("approval_id") or "")
                plan = dict(detail["plan"])
                if mode in {"fake", "dry_run"}:
                    result = _idempotent_api_mutation(
                        conn, "ad_experiment.plan_dry_run", idempotency_key,
                        {"plan_id": plan_id, "execution_mode": mode, "plan_hash": payload_hash(plan)},
                        lambda: _build_dry_run_receipt(plan_id, plan, detail["approval"], mode),
                    )
                    result["request_id"] = request_id
                    return result
                execution_policy = dict(plan.get("execution_policy") or {})
                if execution_policy.get("live_creation_allowed") is False:
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "code": "EXECUTION_UNAVAILABLE",
                            "message": str(execution_policy.get("blocked_reason") or "meta_randomized_experiment_not_available"),
                        },
                    )
                dry_run_receipt = _latest_dry_run_receipt(conn, plan_id)
                if (
                    str(dry_run_receipt.get("status") or "") != "DRY_RUN_VERIFIED"
                    or str(dry_run_receipt.get("plan_hash") or "") != payload_hash(plan)
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "STATE_CONFLICT", "message": "matching_dry_run_required_before_live_execution"},
                    )
                action_type = str(plan.get("action_type") or "").upper()
                required_confirmation = {
                    "CREATE_PAUSED_AD": "CREATE_PAUSED_OBJECTS",
                    "REACTIVATE_AD": "ENABLE_DELIVERY",
                    "PAUSE_AD": "PAUSE_DELIVERY",
                    "PAUSE_ADSET": "PAUSE_DELIVERY",
                    "SET_COST_CAP": "APPLY_COST_CAP",
                }.get(action_type, "APPLY_APPROVED_CHANGE")
                if str(body.confirmation or "").strip().upper() != required_confirmation:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "STATE_CONFLICT", "message": "matching_live_execution_confirmation_required"},
                    )
                if not _meta_live_execution_available(
                    str(plan.get("action_type") or ""), str(plan.get("target_account_id") or "")
                ):
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "code": "EXECUTION_UNAVAILABLE",
                            "message": "meta_live_execution_not_available",
                        },
                    )
                payload = {
                    "execution_mode": mode, "approval_id": approval_id,
                    "account_id": plan.get("target_account_id"), "plan": plan,
                    "experiment_id": plan.get("experiment_id"),
                    "experiment_ids": list(plan.get("experiment_ids") or []),
                    "launch_id": str(plan.get("launch_id") or ""),
                }
                result = ExecutionTaskService(conn).enqueue_task(plan_id, idempotency_key=idempotency_key, payload=payload)
                experiment_ids = list(plan.get("experiment_ids") or []) or [str(plan.get("experiment_id") or "")]
                if action_type == "CREATE_PAUSED_AD":
                    experiments = AdExperimentService(conn)
                    for experiment_id in [str(item) for item in experiment_ids if str(item)]:
                        experiment = experiments.get(experiment_id)
                        if str(experiment.get("state") or "") == "WAITING_CREATE_APPROVAL":
                            experiments.transition(
                                experiment_id,
                                "CREATING_PAUSED_OBJECTS",
                                actor=actor(user),
                                reason="approved_plan_submitted_after_dry_run",
                                event_type="LIVE_EXECUTION_SUBMITTED",
                                evidence={"plan_id": plan_id, "execution_task_id": result["execution_task_id"]},
                            )
                result.update({"plan_id": plan_id, "request_id": request_id})
                return result
        return execute(action)

    @router.post("/meta-plans/{plan_id}/resume-same-plan", status_code=201)
    def resume_same_plan(
        plan_id: str, body: AdExperimentResumeRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)
        if str(body.confirmation or "").strip().upper() != "CONTINUE_SAME_PLAN":
            raise HTTPException(
                status_code=409,
                detail={"code": "STATE_CONFLICT", "message": "same_plan_resume_confirmation_required"},
            )
        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                detail = AdExperimentService(conn).plan_detail(plan_id)
                plan = dict(detail.get("plan") or {})
                if not _meta_live_execution_available(
                    str(plan.get("action_type") or ""), str(plan.get("target_account_id") or "")
                ):
                    raise HTTPException(
                        status_code=503,
                        detail={"code": "EXECUTION_UNAVAILABLE", "message": "meta_live_execution_not_available"},
                    )
                campaign = dict(plan.get("campaign") or dict(plan.get("steps") or {}).get("CAMPAIGN_CREATE") or {})
                matches = exact_campaign_matches(
                    str(plan.get("target_account_id") or ""),
                    str(campaign.get("name") or ""),
                )
                task_row = conn.execute(
                    "SELECT * FROM meta_execution_task WHERE operation_action_id=?",
                    (plan_id,),
                ).fetchone()
                source_task = dict(task_row) if task_row else {}
                validated_rejected_step = ""
                validated_missing_study = False
                if (
                    matches
                    and str(source_task.get("error_message") or "").startswith("meta_graph_error:")
                ):
                    if str(source_task.get("current_step") or "").upper() == "STUDY_CREATE":
                        study = dict(plan.get("study") or {})
                        validated_missing_study = not exact_study_matches(
                            str(study.get("business_id") or ""), str(study.get("name") or ""),
                        )
                    else:
                        validated_rejected_step = validate_rejected_adset(plan, source_task)
                result = _idempotent_api_mutation(
                    conn, "ad_experiment.resume_same_plan", idempotency_key,
                    {
                        "plan_id": plan_id,
                        "confirmation": "CONTINUE_SAME_PLAN",
                        "plan_hash": payload_hash(plan),
                        "campaign_match_count": len(matches),
                    },
                    lambda: (
                        _continue_same_plan_after_created_campaign(
                            conn, plan_id, confirmed_by=actor(user),
                            recovery_key=idempotency_key, campaign_matches=matches,
                            validated_rejected_step=validated_rejected_step,
                            validated_missing_study=validated_missing_study,
                        )
                        if matches else
                        _resume_same_plan_execution(
                            conn, plan_id, confirmed_by=actor(user),
                            recovery_key=idempotency_key,
                        )
                    ),
                )
                result.update({"request_id": request_id, "meta_writes_performed": False})
                return result

        return execute(action)

    @router.post("/meta-plans/{plan_id}/repair-page", status_code=201)
    def repair_plan_page(
        plan_id: str, body: AdExperimentPageRepairRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)
        page_id = str(body.page_id or "").strip()
        if str(body.confirmation or "").strip().upper() != "SAVE_PAGE_AND_CONTINUE_ORDER":
            raise HTTPException(
                status_code=409,
                detail={"code": "STATE_CONFLICT", "message": "page_repair_confirmation_required"},
            )
        if not page_id.isdigit():
            raise HTTPException(
                status_code=400,
                detail={"code": "VALIDATION_ERROR", "message": "meta_page_id_must_be_numeric"},
            )
        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                detail = AdExperimentService(conn).plan_detail(plan_id)
                plan = dict(detail.get("plan") or {})
                if not _meta_live_execution_available(
                    str(plan.get("action_type") or ""), str(plan.get("target_account_id") or "")
                ):
                    raise HTTPException(
                        status_code=503,
                        detail={"code": "EXECUTION_UNAVAILABLE", "message": "meta_live_execution_not_available"},
                    )
                evidence = verified_account_page(
                    str(plan.get("target_account_id") or ""), page_id,
                )
                result = _idempotent_api_mutation(
                    conn, "ad_experiment.repair_page", idempotency_key,
                    {
                        "plan_id": plan_id,
                        "page_id": page_id,
                        "confirmation": "SAVE_PAGE_AND_CONTINUE_ORDER",
                        "plan_hash": payload_hash(plan),
                        "page_verification": evidence.get("verification"),
                    },
                    lambda: _repair_page_and_continue_order(
                        conn, plan_id, page_id=page_id,
                        confirmed_by=actor(user), recovery_key=idempotency_key,
                        page_evidence=evidence,
                    ),
                )
                result.update({"request_id": request_id, "meta_writes_performed": False})
                return result

        return execute(action)

    @router.post("/meta-plans/{plan_id}/repair-page-plan", status_code=201)
    def repair_page_plan(
        plan_id: str, body: AdExperimentRepairRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)
        if str(body.confirmation or "").strip().upper() != "APPROVE_REPAIR_PLAN":
            raise HTTPException(
                status_code=409,
                detail={"code": "STATE_CONFLICT", "message": "repair_plan_confirmation_required"},
            )

        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                detail = AdExperimentService(conn).plan_detail(plan_id)
                plan = dict(detail.get("plan") or {})
                if not _meta_live_execution_available(
                    str(plan.get("action_type") or ""), str(plan.get("target_account_id") or "")
                ):
                    raise HTTPException(
                        status_code=503,
                        detail={"code": "EXECUTION_UNAVAILABLE", "message": "meta_live_execution_not_available"},
                    )
                result = _idempotent_api_mutation(
                    conn, "ad_experiment.repair_page_plan", idempotency_key,
                    {
                        "source_plan_id": plan_id,
                        "target_page_id": str(body.target_page_id or ""),
                        "confirmation": "APPROVE_REPAIR_PLAN",
                        "source_plan_hash": payload_hash(plan),
                    },
                    lambda: _repair_plan_after_page_rejection(
                        conn, plan_id, target_page_id=body.target_page_id,
                        confirmed_by=actor(user), repair_key=idempotency_key,
                        campaign_matches=exact_campaign_matches(
                            str(plan.get("target_account_id") or ""),
                            str(dict(plan.get("campaign") or {}).get("name") or ""),
                        ),
                    ),
                )
                result["request_id"] = request_id
                return result

        return execute(action)

    @router.post("/meta-plans/{plan_id}/invalidate-expired")
    def invalidate_expired_plan(
        plan_id: str, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        operator(request)
        raise HTTPException(
            status_code=409,
            detail={"code": "STATE_CONFLICT", "message": "plan_is_not_expired"},
        )

    @router.get("/meta-plans/{plan_id}/receipt")
    def get_plan_receipt(plan_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        def action(conn: sqlite3.Connection) -> Dict[str, Any]:
            detail = AdExperimentService(conn).plan_detail(plan_id)
            task = conn.execute("SELECT * FROM meta_execution_task WHERE operation_action_id=?", (plan_id,)).fetchone()
            receipts = []
            if task:
                rows = conn.execute("SELECT * FROM meta_execution_task_receipt WHERE execution_task_id=? ORDER BY created_at", (task["execution_task_id"],)).fetchall()
                receipts = [GrowthReadService._decode_row(dict(row)) for row in rows]
            dry_run = _latest_dry_run_receipt(conn, plan_id)
            plan = dict(detail.get("plan") or {})
            action_type = str(plan.get("action_type") or "")
            dry_run_verified = bool(
                str(dry_run.get("status") or "") == "DRY_RUN_VERIFIED"
                and str(dry_run.get("plan_hash") or "") == payload_hash(plan)
            )
            task_payload = decode_json(task["payload_json"], {}) if task else {}
            task_status = str(task["status"] or "") if task else ""
            task_mode = str(task_payload.get("execution_mode") or "") if task else ""
            recovery_approval = dict(task_payload.get("recovery_approval") or {})
            recovery_current = _recovery_approval_current(recovery_approval, plan)
            plan_expired = False
            task_error = str(task["error_code"] or "") if task else ""
            live_available = _meta_live_execution_available(
                str(plan.get("action_type") or ""), str(plan.get("target_account_id") or "")
            )
            write_steps = {
                "IMAGE_UPLOAD", "CAMPAIGN_CREATE", "CREATIVE_CREATE",
                "ADSET_CREATE", "AD_CREATE", "BUDGET_UPDATE",
                "STATUS_UPDATE", "AD_CREATIVE_UPDATE",
            }
            write_receipts = [
                row for row in receipts
                if any(
                    str(row.get("step_name") or "").upper() == step
                    or str(row.get("step_name") or "").upper().endswith(f"_{step}")
                    for step in write_steps
                )
            ]
            confirmed_write_receipts = [
                row for row in write_receipts
                if str(row.get("step_status") or "").upper() in {"SUCCESS", "VERIFIED"}
                and bool(dict(row.get("meta_object_ids_json") or {}))
            ]
            can_retry_uncreated_plan = bool(
                task_status == "MANUAL_REVIEW"
                and task_error == "meta_result_uncertain"
                and str(task["current_step"] or "") == "CAMPAIGN_CREATE"
                and not dict(decode_json(task["meta_object_ids_json"], {}))
                and len(receipts) == 1
                and str(receipts[0].get("step_name") or "") == "CAMPAIGN_CREATE"
                and str(receipts[0].get("step_status") or "") == "UNKNOWN"
                and not dict(receipts[0].get("meta_object_ids_json") or {})
                and int(
                    recovery_approval.get("recovery_generation")
                    or (1 if recovery_approval else 0)
                ) < 2
            )
            task_object_ids = dict(decode_json(task["meta_object_ids_json"], {})) if task else {}
            can_continue_after_image_preflight = bool(
                task_status == "MANUAL_REVIEW"
                and task_error == "meta_result_uncertain"
                and str(task["current_step"] or "").upper().endswith("_IMAGE_UPLOAD")
                and set(task_object_ids) == {"campaign_id"}
                and len(receipts) == 2
                and str(receipts[0].get("step_name") or "") == "CAMPAIGN_CREATE"
                and str(receipts[0].get("step_status") or "").upper() == "SUCCESS"
                and str(receipts[1].get("step_status") or "").upper() == "UNKNOWN"
                and not dict(task_payload.get("continuation") or {})
            )
            prior_continuation = dict(task_payload.get("continuation") or {})
            can_repair_campaign_readback = bool(
                task_status == "MANUAL_REVIEW"
                and task_error == "continuation_verification_uncertain"
                and str(task["current_step"] or "").upper() == "CAMPAIGN_CREATE"
                and set(task_object_ids) == {"campaign_id"}
                and len(receipts) == 1
                and str(receipts[0].get("step_name") or "") == "CAMPAIGN_CREATE"
                and str(receipts[0].get("step_status") or "").upper() == "UNKNOWN"
                and prior_continuation.get("completed_steps") == ["CAMPAIGN_CREATE"]
                and int(prior_continuation.get("verification_retry_count") or 0) < 1
            )
            can_recheck_external_prerequisite = bool(
                task_status == "MANUAL_REVIEW"
                and task_error == "meta_result_uncertain"
                and str(task["error_message"] or "").startswith("meta_graph_error:")
                and str(task["current_step"] or "").upper().endswith("_ADSET_CREATE")
                and int(prior_continuation.get("write_rejection_retry_count") or 0) < 1
            )
            successful_prefix = [
                str(receipt.get("step_name") or "").upper()
                for receipt in receipts[:-1]
                if str(receipt.get("step_status") or "").upper() in {"SUCCESS", "VERIFIED"}
            ]
            can_repair_final_readback = bool(
                task_status == "MANUAL_REVIEW"
                and task_error == "final_verification_uncertain"
                and str(task["current_step"] or "").upper() == "VERIFY"
                and receipts
                and str(receipts[-1].get("step_name") or "").upper() == "VERIFY"
                and str(receipts[-1].get("step_status") or "").upper() == "UNKNOWN"
                and bool(successful_prefix)
                and int(prior_continuation.get("final_verification_retry_count") or 0) < 1
            )
            can_continue_created_plan = bool(
                can_continue_after_image_preflight
                or can_repair_campaign_readback
                or can_recheck_external_prerequisite
                or can_repair_final_readback
            )
            can_resume_same_plan = can_retry_uncreated_plan or can_continue_created_plan
            decoded_task = GrowthReadService._decode_row(dict(task)) if task else {}
            incident_resolution = _creation_incident_resolution(
                plan, decoded_task, receipts, can_resume_same_plan=can_resume_same_plan,
            ) if task else {}
            if plan_expired and (not task or (task_status in {"QUEUED", "MANUAL_REVIEW"} and not receipts)):
                next_step = "PLAN_EXPIRED_REPLAN"
                next_step_zh = "方案已过期，重新生成创建方案"
            elif not task:
                next_step = (
                    "SUBMIT_PAUSED_OBJECT_CREATION"
                    if dry_run_verified and action_type == "CREATE_PAUSED_AD"
                    else "RUN_DRY_RUN"
                )
                next_step_zh = (
                    "确认创建暂停态广告"
                    if next_step == "SUBMIT_PAUSED_OBJECT_CREATION"
                    else "先完成安全检查"
                )
            elif task_status == "QUEUED" and not receipts:
                next_step = (
                    "LIVE_EXECUTION_UNAVAILABLE"
                    if task_mode == "live" and not live_available
                    else "WAIT_FOR_EXECUTOR"
                )
                next_step_zh = (
                    "真实创建通道尚未开启，广告还没有创建"
                    if next_step == "LIVE_EXECUTION_UNAVAILABLE"
                    else "等待系统开始创建"
                )
            elif task_status == "SUCCESS":
                next_step = "REVIEW_PAUSED_OBJECTS"
                next_step_zh = "核对已创建的暂停态广告"
            elif task_status == "MANUAL_REVIEW":
                next_step = "RESUME_SAME_PLAN" if can_resume_same_plan else "REVIEW_UNCERTAIN_RESULT"
                next_step_zh = (
                    "核对 Meta 后继续当前方案"
                    if can_resume_same_plan else "人工核对 Meta 实际结果"
                )
            else:
                next_step = "WAIT_FOR_PAUSED_OBJECTS"
                next_step_zh = "正在创建并回读暂停态广告"
            return {
                "plan": detail,
                "execution_task": decoded_task,
                "receipts": receipts,
                "dry_run_receipt": dry_run,
                "next_step": next_step,
                "next_step_zh": next_step_zh,
                "plan_expired": plan_expired,
                "task_error_code": task_error,
                "live_execution_available": live_available,
                "can_resume_same_plan": can_resume_same_plan,
                "can_continue_created_plan": can_continue_created_plan,
                "external_prerequisite_required": can_recheck_external_prerequisite,
                "incident_resolution": incident_resolution,
                "meta_write_attempted": bool(write_receipts),
                "meta_writes_performed": bool(confirmed_write_receipts),
            }
        return execute(lambda: _with_connection(db, action))

    @router.post("/experiments/{experiment_id}/performance/refresh", status_code=201)
    @router.post("/experiments/{experiment_id}/evaluate", status_code=201)
    def evaluate_experiment(
        experiment_id: str, body: AdExperimentEvaluationRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)
        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                result = AdExperimentEvaluator(conn).record_checkpoint(
                    experiment_id, body.model_dump(), actor=actor(user) or "system", idempotency_key=idempotency_key,
                )
                result["request_id"] = request_id
                return result
        return execute(action)

    @router.get("/experiments/{experiment_id}/performance")
    def get_performance(experiment_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _with_connection(db, lambda conn: AdExperimentEvaluator(conn).list(experiment_id)))

    @router.post("/new-account-launches/{launch_id}/audience-checkpoints", status_code=201)
    def evaluate_audience_pair(
        launch_id: str, body: AudiencePairEvaluationRequest, request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        request_id: str = Header(..., alias="X-Request-ID"),
    ) -> Dict[str, Any]:
        user = operator(request)
        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                result = AudienceExperimentEvaluator(conn).record_checkpoint(
                    launch_id, body.model_dump(), actor=actor(user) or "system",
                    idempotency_key=idempotency_key,
                )
                result["request_id"] = request_id
                return result
        return execute(action)

    @router.get("/new-account-launches/{launch_id}/audience-checkpoints")
    def get_audience_pair_evaluations(launch_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _with_connection(
            db, lambda conn: AudienceExperimentEvaluator(conn).list(launch_id),
        ))

    @router.post("/new-account-launches/{launch_id}/audience-preflight", status_code=201)
    def run_audience_preflight(launch_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        def action() -> Dict[str, Any]:
            with db.connect() as conn:
                return MetaAudiencePreflightService(
                    conn,
                    session=meta_session,
                    access_token=meta_access_token,
                    graph_root=meta_graph_root,
                    business_ids=meta_business_ids or [],
                    application_id=meta_application_id,
                    store_url=meta_store_url,
                ).run(launch_id)
        return execute(action)

    @router.get("/experiments/{experiment_id}/recommendation")
    def get_recommendation(experiment_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        def action(conn: sqlite3.Connection) -> Dict[str, Any]:
            experiment = AdExperimentService(conn).get(experiment_id)
            evaluations = AdExperimentEvaluator(conn).list(experiment_id)["items"]
            latest = evaluations[-1] if evaluations else {}
            status = str(latest.get("evaluation_status") or "PENDING")
            next_action = {
                "EFFECTIVE": "INCREASE_BUDGET", "INEFFECTIVE": "REPLACE_CREATIVE",
                "NEUTRAL": "OBSERVE", "INSUFFICIENT_SAMPLE": "OBSERVE",
                "DATA_INCOMPLETE": "CHECK_DATA", "MIXED_CHANGE": "CREATE_PAUSED_AD",
            }.get(status, "OBSERVE")
            return {"experiment": experiment, "latest_evaluation": latest, "recommended_action": next_action, "causal_claim": False, "requires_approval": next_action not in {"OBSERVE", "CHECK_DATA"}}
        return execute(lambda: _with_connection(db, action))

    @router.get("/experiments/{experiment_id}/adjustment-review")
    def adjustment_review(experiment_id: str, request: Request) -> Dict[str, Any]:
        operator(request)
        return execute(lambda: _with_connection(db, lambda conn: {
            "experiment": AdExperimentService(conn).get(experiment_id),
            "performance": AdExperimentEvaluator(conn).list(experiment_id),
            "timeline": AdExperimentService(conn).timeline(experiment_id),
        }))

    return router


def _build_dry_run_receipt(
    plan_id: str, plan: Dict[str, Any], approval: Dict[str, Any], mode: str,
) -> Dict[str, Any]:
    verified_at = utc_now()
    planned_steps = list(dict(plan.get("steps") or {}).keys())
    cells = list(plan.get("cells") or [])
    if cells:
        if str(plan.get("test_variable") or "") == "audience_strategy":
            planned_steps = ["RANDOMIZATION_PREFLIGHT", "CAMPAIGN_CREATE", "C1_IMAGE_UPLOAD", "C1_CREATIVE_CREATE"]
            for index, cell in enumerate(cells, start=1):
                cell_key = str(dict(cell).get("cell_key") or f"C{index}").upper()
                planned_steps.extend((f"{cell_key}_ADSET_CREATE", f"{cell_key}_AD_CREATE"))
            planned_steps.append("STUDY_CREATE")
        elif str(plan.get("test_variable") or "") == "copy_variant":
            planned_steps = ["RANDOMIZATION_PREFLIGHT", "CAMPAIGN_CREATE"]
            for index, cell in enumerate(cells, start=1):
                cell_key = str(dict(cell).get("cell_key") or f"C{index}").upper()
                planned_steps.extend(
                    f"{cell_key}_{step}"
                    for step in ("IMAGE_UPLOAD", "CREATIVE_CREATE", "ADSET_CREATE", "AD_CREATE")
                )
            planned_steps.append("STUDY_CREATE")
        else:
            planned_steps = ["CAMPAIGN_CREATE"]
            for index, cell in enumerate(cells, start=1):
                cell_key = str(dict(cell).get("cell_key") or f"C{index}").upper()
                planned_steps.extend(
                    f"{cell_key}_{step}"
                    for step in ("IMAGE_UPLOAD", "CREATIVE_CREATE", "ADSET_CREATE", "AD_CREATE")
                )
    if not planned_steps:
        planned_steps = [str(plan.get("action_type") or "PLAN_CHECK")]
    receipts = [
        {
            "step_name": step,
            "step_status": "VERIFIED",
            "write_performed": False,
            "verification": "dry_run_contract_valid",
        }
        for step in [*planned_steps, "VERIFY", "RECEIPT"]
    ]
    return {
        "plan_id": plan_id,
        "status": "DRY_RUN_VERIFIED",
        "execution_mode": mode,
        "write_count": 0,
        "meta_writes_performed": False,
        "plan_hash": payload_hash(plan),
        "approval_status": approval.get("status") or "PROPOSED",
        "verified_at": verified_at,
        "receipts": receipts,
    }


def _latest_dry_run_receipt(conn: sqlite3.Connection, plan_id: str) -> Dict[str, Any]:
    rows = conn.execute(
        """
        SELECT response_json
        FROM growth_idempotency_record
        WHERE route_key='ad_experiment.plan_dry_run'
        ORDER BY created_at DESC
        """
    ).fetchall()
    for row in rows:
        candidate = decode_json(row["response_json"], {})
        if str(candidate.get("plan_id") or "") == str(plan_id or ""):
            return candidate
    return {}


def _plan_cell_creative_unchanged(
    conn: sqlite3.Connection,
    experiments: AdExperimentService,
    plan: Dict[str, Any],
    cell: Dict[str, Any],
) -> bool:
    """Verify the immutable creative snapshot used by a resumable Plan.

    Copy-only batches intentionally reuse one approved image across multiple
    experiments, so they do not create a per-experiment generation queue link.
    In that case the Plan's frozen image id and SHA-256 are the authority.
    Other Plan types retain the existing experiment-linkage check.
    """
    image_step = dict(dict(cell.get("steps") or {}).get("IMAGE_UPLOAD") or {})
    frozen_image_id = str(
        cell.get("frozen_creative_id") or image_step.get("image_id") or ""
    ).strip()
    if str(plan.get("test_variable") or "").lower() != "copy_variant":
        current = experiments.latest_approved_creative(str(cell.get("experiment_id") or ""))
        return str(current.get("image_id") or "") == frozen_image_id
    expected_sha256 = str(cell.get("asset_sha256") or "").strip().lower()
    if not frozen_image_id or not expected_sha256:
        return False
    row = conn.execute(
        """
        SELECT i.image_id,i.image_hash,i.image_ref,i.file_path
        FROM creative_generated_images i
        WHERE i.image_id=?
          AND lower(i.review_status) IN ('approved','used_in_ad')
          AND EXISTS (
              SELECT 1 FROM creative_review_records r
              WHERE r.image_id=i.image_id AND upper(r.review_status)='APPROVED'
          )
        LIMIT 1
        """,
        (frozen_image_id,),
    ).fetchone()
    if not row or str(row["image_hash"] or "").strip().lower() != expected_sha256:
        return False
    image_path = Path(str(row["file_path"] or row["image_ref"] or "")).expanduser().resolve()
    if not image_path.is_file():
        return False
    digest = hashlib.sha256()
    with image_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest().lower() == expected_sha256


def _creation_incident_resolution(
    plan: Dict[str, Any], task: Dict[str, Any], receipts: List[Dict[str, Any]],
    *, can_resume_same_plan: bool,
) -> Dict[str, Any]:
    """Return one business-facing resolution for an order-level creation incident."""
    if str(task.get("status") or "") != "MANUAL_REVIEW":
        return {}
    current_step = str(task.get("current_step") or "").upper()
    error_message = str(task.get("error_message") or "")
    page_rejected = bool(
        current_step.endswith("_CREATIVE_CREATE")
        and error_message.startswith("meta_graph_error:10:1341012")
    )
    object_ids = dict(task.get("meta_object_ids_json") or {})
    cells = [dict(item or {}) for item in list(plan.get("cells") or [])]
    current_page_ids = {
        str(dict(dict(cell.get("steps") or {}).get("CREATIVE_CREATE") or {})
            .get("object_story_spec", {}).get("page_id") or "").strip()
        for cell in cells
    }
    current_page_ids.discard("")
    completed: List[str] = []
    if object_ids.get("campaign_id"):
        completed.append("广告系列（已暂停）")
    completed_adsets = sum(1 for key, value in object_ids.items() if key.endswith("_adset_id") and value)
    if completed_adsets:
        completed.append(f"{completed_adsets} 个广告组（已暂停）")
    completed_ads = sum(1 for key, value in object_ids.items() if key.endswith("_ad_id") and value)
    if completed_ads:
        completed.append(f"{completed_ads} 条广告（已暂停）")
    completed_creatives = sum(1 for key, value in object_ids.items() if key.endswith("_creative_id") and value)
    if completed_creatives:
        completed.append(f"{completed_creatives} 个广告素材")
    incomplete: List[str] = []
    missing_creatives = max(0, len(cells) - completed_creatives)
    missing_adsets = max(0, len(cells) - completed_adsets)
    missing_ads = max(0, len(cells) - completed_ads)
    if missing_creatives:
        incomplete.append(f"{missing_creatives} 个广告素材")
    if missing_adsets:
        incomplete.append(f"{missing_adsets} 个广告组")
    if missing_ads:
        incomplete.append(f"{missing_ads} 条广告")
    successful_prefix: List[str] = []
    for receipt in receipts:
        step = str(receipt.get("step_name") or "").upper()
        if str(receipt.get("step_status") or "").upper() not in {"SUCCESS", "VERIFIED"}:
            break
        successful_prefix.append(step)
    planned_steps = ["CAMPAIGN_CREATE"]
    for index, cell in enumerate(cells, start=1):
        cell_key = str(cell.get("cell_key") or f"C{index}").upper()
        planned_steps.extend([
            f"{cell_key}_IMAGE_UPLOAD", f"{cell_key}_CREATIVE_CREATE",
            f"{cell_key}_ADSET_CREATE", f"{cell_key}_AD_CREATE",
        ])
    prefix_is_exact = bool(
        successful_prefix
        and successful_prefix == planned_steps[:len(successful_prefix)]
        and len(successful_prefix) < len(planned_steps)
        and current_step == planned_steps[len(successful_prefix)]
    )
    repair_supported = bool(
        page_rejected
        and not can_resume_same_plan
        and prefix_is_exact
        and str(object_ids.get("campaign_id") or "").strip()
    )
    return {
        "status": "REPAIR_PLAN_REQUIRED" if repair_supported else "MANUAL_REVIEW_REQUIRED",
        "title": "广告创建未完成",
        "root_cause_zh": (
            "当前广告账户与公共主页的组合没有创建广告素材的权限。"
            if page_rejected else "系统无法确认本次创建的完整结果。"
        ),
        "completed": completed,
        "incomplete": incomplete,
        "current_page_id": next(iter(current_page_ids), "") if len(current_page_ids) == 1 else "",
        "old_plan_replay_allowed": False,
        "requires_new_plan": repair_supported,
        "repair_supported": repair_supported,
        "completed_steps": successful_prefix,
        "primary_action_zh": "确认修复方案并继续" if repair_supported else "刷新核对结果",
        "guidance_zh": (
            "系统将改用已验证的公共主页生成新方案，只复用已回读确认的对象；旧失败任务不会重放。"
            if repair_supported else "系统会先自动核对；只有结果仍不确定时，才需要你在 Meta 确认同名对象是否存在且保持暂停。"
        ),
    }


def _repair_plan_after_page_rejection(
    conn: sqlite3.Connection,
    source_plan_id: str,
    *,
    target_page_id: str,
    confirmed_by: str,
    repair_key: str,
    campaign_matches: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Create a new immutable Plan and continue after a confirmed Page rejection."""
    from app.growth.errors import GrowthStateConflict

    experiments = AdExperimentService(conn)
    detail = experiments.plan_detail(source_plan_id)
    source_action = dict(detail.get("operation_action") or {})
    source_plan = dict(detail.get("plan") or {})
    source_task_row = conn.execute(
        "SELECT * FROM meta_execution_task WHERE operation_action_id=?",
        (source_plan_id,),
    ).fetchone()
    if not source_task_row:
        raise GrowthStateConflict("repair_source_task_missing")
    source_task = GrowthReadService._decode_row(dict(source_task_row))
    if (
        str(source_action.get("action_type") or "").upper() != "CREATE_PAUSED_AD"
        or str(source_action.get("status") or "").upper() != "MANUAL_REVIEW"
        or str(source_task.get("status") or "").upper() != "MANUAL_REVIEW"
    ):
        raise GrowthStateConflict("page_repair_source_not_manual_review")
    receipts = [GrowthReadService._decode_row(dict(row)) for row in conn.execute(
        "SELECT * FROM meta_execution_task_receipt WHERE execution_task_id=? ORDER BY created_at,receipt_id",
        (source_task["execution_task_id"],),
    ).fetchall()]
    incident = _creation_incident_resolution(
        source_plan, source_task, receipts, can_resume_same_plan=False,
    )
    if not incident.get("repair_supported"):
        raise GrowthStateConflict("page_repair_not_supported_for_source_task")
    normalized_page_id = str(target_page_id or "").strip()
    if not normalized_page_id or not normalized_page_id.isdigit():
        raise GrowthValidationError("verified_repair_page_required")
    if normalized_page_id == str(incident.get("current_page_id") or ""):
        raise GrowthStateConflict("repair_page_must_differ_from_rejected_page")
    source_ids = dict(source_task.get("meta_object_ids_json") or {})
    campaign_id = str(source_ids.get("campaign_id") or "").strip()
    matches = [dict(item or {}) for item in campaign_matches]
    if not (
        len(matches) == 1
        and str(matches[0].get("id") or "").strip() == campaign_id
        and str(matches[0].get("status") or matches[0].get("effective_status") or "").upper() == "PAUSED"
    ):
        raise GrowthStateConflict("repair_campaign_not_uniquely_confirmed_paused")

    repaired_plan = copy.deepcopy(source_plan)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    repaired_plan["expires_at"] = expires_at
    repaired_plan["repair"] = {
        "source_plan_id": source_plan_id,
        "source_execution_task_id": source_task["execution_task_id"],
        "reason": "PAGE_CREATIVE_PERMISSION_REJECTED",
        "target_page_id": normalized_page_id,
        "old_plan_replay_allowed": False,
    }
    for raw_cell in list(repaired_plan.get("cells") or []):
        creative = dict(dict(raw_cell.get("steps") or {}).get("CREATIVE_CREATE") or {})
        story = dict(creative.get("object_story_spec") or {})
        story["page_id"] = normalized_page_id
        creative["object_story_spec"] = story
        raw_cell["steps"]["CREATIVE_CREATE"] = creative

    new_action_payload = copy.deepcopy(source_action.get("payload_json") or {})
    new_action_payload["plan"] = repaired_plan
    new_action_payload["repair_of_operation_action_id"] = source_plan_id
    new_action_payload["repair_of_execution_task_id"] = source_task["execution_task_id"]
    actions = ExecutionTaskService(conn)
    new_action = actions.create_operation_action(
        decision_id=str(source_action.get("decision_id") or ""),
        episode_id=str(source_action.get("episode_id") or ""),
        action_type="CREATE_PAUSED_AD",
        action_scope=str(source_action.get("action_scope") or "EXPERIMENT"),
        target_type=str(source_action.get("target_type") or "LAUNCH"),
        target_id=str(source_action.get("target_id") or ""),
        payload=new_action_payload,
        created_by=confirmed_by,
        idempotency_key=f"page-repair-action:{repair_key}",
    )
    new_plan_id = str(new_action["operation_action_id"])
    approvals = OperationApprovalService(conn)
    approval = approvals.propose(
        new_plan_id, repaired_plan, proposed_by=confirmed_by,
        idempotency_key=f"page-repair-approval:{repair_key}", expires_at=expires_at,
    )
    if approval["status"] == "PROPOSED":
        approval = approvals.transition(
            str(approval["approval_id"]), "APPROVED", actor=confirmed_by,
            single_operator_confirmation="APPROVE_EXACT_PLAN",
        )
    dry_run = _build_dry_run_receipt(new_plan_id, repaired_plan, approval, "dry_run")
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO growth_idempotency_record
            (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
            VALUES ('ad_experiment.plan_dry_run',?,?,200,?,?)""",
            (
                f"page-repair-dry:{repair_key}",
                payload_hash({"source_plan_id": source_plan_id, "plan_hash": payload_hash(repaired_plan)}),
                canonical_json(dry_run), utc_now(),
            ),
        )
    continuation = {
        "source_plan_id": source_plan_id,
        "source_execution_task_id": source_task["execution_task_id"],
        "plan_hash": payload_hash(repaired_plan),
        "completed_steps": list(incident.get("completed_steps") or []),
        "meta_object_ids": source_ids,
        "campaign_reconciled_at": utc_now(),
        "page_repair": True,
    }
    experiment_ids = list(repaired_plan.get("experiment_ids") or []) or [
        str(cell.get("experiment_id") or "") for cell in list(repaired_plan.get("cells") or [])
    ]
    for experiment_id in [str(item) for item in experiment_ids if str(item)]:
        current = experiments.get(experiment_id)
        if str(current.get("state") or "") == "CREATION_PARTIAL_FAILURE":
            experiments.transition(
                experiment_id, "WAITING_CREATE_APPROVAL", actor=confirmed_by,
                reason="page_repair_plan_confirmed", event_type="PAGE_REPAIR_PLAN_CONFIRMED",
                evidence={"source_plan_id": source_plan_id, "repair_plan_id": new_plan_id},
            )
    task = actions.enqueue_task(
        new_plan_id,
        idempotency_key=f"page-repair-live:{repair_key}",
        payload={
            "execution_mode": "live", "approval_id": str(approval["approval_id"]),
            "account_id": repaired_plan.get("target_account_id"), "plan": repaired_plan,
            "experiment_ids": experiment_ids, "launch_id": str(repaired_plan.get("launch_id") or ""),
            "continuation": continuation,
        },
    )
    for experiment_id in [str(item) for item in experiment_ids if str(item)]:
        current = experiments.get(experiment_id)
        if str(current.get("state") or "") == "WAITING_CREATE_APPROVAL":
            experiments.transition(
                experiment_id, "CREATING_PAUSED_OBJECTS", actor=confirmed_by,
                reason="page_repair_queued", event_type="PAGE_REPAIR_QUEUED",
                evidence={"source_plan_id": source_plan_id, "repair_plan_id": new_plan_id,
                          "execution_task_id": task["execution_task_id"]},
            )
    return {
        "ok": True, "source_plan_id": source_plan_id, "repair_plan_id": new_plan_id,
        "execution_task_id": task["execution_task_id"], "status": task["status"],
        "target_page_id": normalized_page_id, "old_plan_replayed": False,
        "meta_writes_performed": False, "next_step": "WAIT_FOR_PAUSED_OBJECTS",
    }


def _repair_page_and_continue_order(
    conn: sqlite3.Connection,
    source_plan_id: str,
    *,
    page_id: str,
    confirmed_by: str,
    recovery_key: str,
    page_evidence: Dict[str, Any],
) -> Dict[str, Any]:
    """Repair the Page inside an existing launch and continue its uncreated objects.

    The source order, campaign, first image and first Ad Set are retained. The
    rejected creative is not reused; a new creative is created with the verified
    Page before the missing Ad and remaining cells continue.
    """
    from app.growth.errors import GrowthStateConflict

    experiments = AdExperimentService(conn)
    detail = experiments.plan_detail(source_plan_id)
    source_action = dict(detail.get("operation_action") or {})
    source_plan = dict(detail.get("plan") or {})
    source_task_row = conn.execute(
        "SELECT * FROM meta_execution_task WHERE operation_action_id=?",
        (source_plan_id,),
    ).fetchone()
    if not source_task_row:
        raise GrowthStateConflict("page_repair_source_task_missing")
    source_task = GrowthReadService._decode_row(dict(source_task_row))
    receipts = [GrowthReadService._decode_row(dict(row)) for row in conn.execute(
        "SELECT * FROM meta_execution_task_receipt WHERE execution_task_id=? ORDER BY created_at,receipt_id",
        (source_task["execution_task_id"],),
    ).fetchall()]
    source_ids = dict(source_task.get("meta_object_ids_json") or {})
    expected_receipts = [
        ("CAMPAIGN_CREATE", "SUCCESS"),
        ("C1_IMAGE_UPLOAD", "SUCCESS"),
        ("C1_CREATIVE_CREATE", "SUCCESS"),
        ("C1_ADSET_CREATE", "SUCCESS"),
        ("C1_AD_CREATE", "UNKNOWN"),
    ]
    actual_receipts = [
        (str(row.get("step_name") or "").upper(), str(row.get("step_status") or "").upper())
        for row in receipts
    ]
    expected_keys = {"campaign_id", "c1_image_hash", "c1_creative_id", "c1_adset_id"}
    if not (
        source_action.get("action_type") == "CREATE_PAUSED_AD"
        and str(source_action.get("status") or "").upper() == "MANUAL_REVIEW"
        and str(source_action.get("target_type") or "").upper() == "LAUNCH"
        and str(source_task.get("status") or "").upper() == "MANUAL_REVIEW"
        and str(source_task.get("current_step") or "").upper() == "C1_AD_CREATE"
        and str(source_task.get("error_code") or "") == "meta_result_uncertain"
        and "meta_graph_error:100:1815645" in str(source_task.get("error_message") or "")
        and actual_receipts == expected_receipts
        and set(source_ids) == expected_keys
        and all(str(source_ids.get(key) or "").strip() for key in expected_keys)
    ):
        raise GrowthStateConflict("page_repair_source_not_safely_continuable")

    cells = list(source_plan.get("cells") or [])
    if not cells:
        raise GrowthStateConflict("page_repair_batch_plan_required")
    source_page_ids = {
        str(
            dict(dict(dict(cell or {}).get("steps") or {}).get("CREATIVE_CREATE") or {})
            .get("object_story_spec", {}).get("page_id") or ""
        ).strip()
        for cell in cells
    }
    normalized_page = str(page_id or "").strip()
    if len(source_page_ids) != 1 or not next(iter(source_page_ids), ""):
        raise GrowthStateConflict("page_repair_source_page_inconsistent")
    if normalized_page in source_page_ids:
        raise GrowthStateConflict("page_repair_requires_different_page")
    if (
        str(page_evidence.get("account_id") or "").strip()
        != str(source_plan.get("target_account_id") or "").strip().removeprefix("act_")
        or str(page_evidence.get("page_id") or "").strip() != normalized_page
        or int(page_evidence.get("historical_ad_count") or 0) <= 0
    ):
        raise GrowthStateConflict("page_repair_account_page_evidence_invalid")
    for raw_cell in cells:
        cell = dict(raw_cell or {})
        image = dict(dict(cell.get("steps") or {}).get("IMAGE_UPLOAD") or {})
        current = experiments.latest_approved_creative(str(cell.get("experiment_id") or ""))
        if str(current.get("image_id") or "") != str(image.get("image_id") or ""):
            raise GrowthStateConflict("page_repair_approved_creative_changed")

    repaired_plan = copy.deepcopy(source_plan)
    for raw_cell in list(repaired_plan.get("cells") or []):
        steps = dict(raw_cell.get("steps") or {})
        creative = dict(steps.get("CREATIVE_CREATE") or {})
        story = dict(creative.get("object_story_spec") or {})
        story["page_id"] = normalized_page
        creative["object_story_spec"] = story
        steps["CREATIVE_CREATE"] = creative
        raw_cell["steps"] = steps
    repaired_plan["page_repair"] = {
        "source_plan_id": source_plan_id,
        "source_execution_task_id": source_task["execution_task_id"],
        "previous_page_id": next(iter(source_page_ids)),
        "page_id": normalized_page,
        "page_name": str(page_evidence.get("page_name") or ""),
        "verification": str(page_evidence.get("verification") or ""),
        "verified_historical_ad_count": int(page_evidence.get("historical_ad_count") or 0),
        "confirmed_by": confirmed_by,
        "confirmed_at": utc_now(),
    }

    new_action_payload = copy.deepcopy(dict(source_action.get("payload_json") or {}))
    new_action_payload["plan"] = repaired_plan
    new_action_payload["plan_request_hash"] = payload_hash(repaired_plan)
    new_action_payload["page_repair_of_operation_action_id"] = source_plan_id
    new_action_payload["page_repair_of_execution_task_id"] = source_task["execution_task_id"]
    actions = ExecutionTaskService(conn)
    new_action = actions.create_operation_action(
        decision_id=str(source_action.get("decision_id") or ""),
        episode_id=str(source_action.get("episode_id") or ""),
        action_type="CREATE_PAUSED_AD",
        action_scope=str(source_action.get("action_scope") or "EXPERIMENT"),
        target_type="LAUNCH",
        target_id=str(source_action.get("target_id") or repaired_plan.get("launch_id") or ""),
        payload=new_action_payload,
        created_by=confirmed_by,
        idempotency_key=f"page-repair-action:{recovery_key}",
    )
    new_plan_id = str(new_action["operation_action_id"])
    approvals = OperationApprovalService(conn)
    approval = approvals.propose(
        new_plan_id, repaired_plan, proposed_by=confirmed_by,
        idempotency_key=f"page-repair-approval:{recovery_key}", expires_at="",
    )
    if approval["status"] == "PROPOSED":
        approval = approvals.transition(
            str(approval["approval_id"]), "APPROVED", actor=confirmed_by,
            single_operator_confirmation="APPROVE_EXACT_PLAN",
        )
    continuation_approval = {
        "status": "APPROVED",
        "source_plan_id": source_plan_id,
        "source_execution_task_id": source_task["execution_task_id"],
        "plan_hash": payload_hash(repaired_plan),
        "confirmed_by": confirmed_by,
        "confirmed_at": str(approval.get("approved_at") or utc_now()),
        "expires_at": str(approval.get("expires_at") or ""),
        "recovery_generation": 1,
    }
    continuation = {
        "source_plan_id": source_plan_id,
        "source_execution_task_id": source_task["execution_task_id"],
        "plan_hash": payload_hash(repaired_plan),
        "completed_steps": ["CAMPAIGN_CREATE", "C1_IMAGE_UPLOAD"],
        "reused_steps": ["C1_ADSET_CREATE"],
        "meta_object_ids": {
            "campaign_id": source_ids["campaign_id"],
            "c1_image_hash": source_ids["c1_image_hash"],
            "c1_adset_id": source_ids["c1_adset_id"],
        },
        "replaced_object_ids": {"c1_creative_id": source_ids["c1_creative_id"]},
        "page_repair": True,
    }
    experiment_ids = list(repaired_plan.get("experiment_ids") or []) or [
        str(cell.get("experiment_id") or "") for cell in list(repaired_plan.get("cells") or [])
    ]
    for experiment_id in [str(item) for item in experiment_ids if str(item)]:
        current = experiments.get(experiment_id)
        if str(current.get("state") or "") == "CREATION_PARTIAL_FAILURE":
            experiments.transition(
                experiment_id, "WAITING_CREATE_APPROVAL", actor=confirmed_by,
                reason="repair_page_in_existing_order",
                event_type="PAGE_REPAIR_CONFIRMED",
                evidence={
                    "source_plan_id": source_plan_id, "resumed_plan_id": new_plan_id,
                    "previous_page_id": next(iter(source_page_ids)), "page_id": normalized_page,
                },
            )
    task = actions.enqueue_task(
        new_plan_id,
        idempotency_key=f"page-repair-live:{recovery_key}",
        payload={
            "execution_mode": "live",
            "approval_id": str(approval["approval_id"]),
            "account_id": repaired_plan.get("target_account_id"),
            "plan": repaired_plan,
            "experiment_id": repaired_plan.get("experiment_id"),
            "experiment_ids": experiment_ids,
            "launch_id": str(repaired_plan.get("launch_id") or ""),
            "recovery_approval": continuation_approval,
            "continuation": continuation,
        },
    )
    dry_run = _build_dry_run_receipt(new_plan_id, repaired_plan, approval, "dry_run")
    dry_run["repair_source_plan_id"] = source_plan_id
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO growth_idempotency_record
            (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
            VALUES ('ad_experiment.plan_dry_run',?,?,200,?,?)""",
            (
                f"page-repair-dry:{recovery_key}",
                payload_hash({"source_plan_id": source_plan_id, "plan_hash": payload_hash(repaired_plan)}),
                canonical_json(dry_run), utc_now(),
            ),
        )
    for experiment_id in [str(item) for item in experiment_ids if str(item)]:
        current = experiments.get(experiment_id)
        if str(current.get("state") or "") == "WAITING_CREATE_APPROVAL":
            experiments.transition(
                experiment_id, "CREATING_PAUSED_OBJECTS", actor=confirmed_by,
                reason="page_repair_queued_for_existing_order",
                event_type="PAGE_REPAIR_EXECUTION_QUEUED",
                evidence={
                    "source_plan_id": source_plan_id, "resumed_plan_id": new_plan_id,
                    "execution_task_id": task["execution_task_id"],
                },
            )
    return {
        "ok": True,
        "launch_id": str(repaired_plan.get("launch_id") or ""),
        "source_plan_id": source_plan_id,
        "resumed_plan_id": new_plan_id,
        "execution_task_id": task["execution_task_id"],
        "previous_page_id": next(iter(source_page_ids)),
        "page_id": normalized_page,
        "page_name": str(page_evidence.get("page_name") or ""),
        "order_reused": True,
        "materials_reused": True,
        "status": task["status"],
        "next_step": "WAIT_FOR_PAUSED_OBJECTS",
    }


def _resume_same_plan_execution(
    conn: sqlite3.Connection,
    source_plan_id: str,
    *,
    confirmed_by: str,
    recovery_key: str,
) -> Dict[str, Any]:
    from app.growth.errors import GrowthStateConflict

    experiments = AdExperimentService(conn)
    detail = experiments.plan_detail(source_plan_id)
    source_action = dict(detail.get("operation_action") or {})
    plan = dict(detail.get("plan") or {})
    source_task_row = conn.execute(
        "SELECT * FROM meta_execution_task WHERE operation_action_id=?",
        (source_plan_id,),
    ).fetchone()
    if not source_task_row:
        raise GrowthStateConflict("same_plan_source_task_missing")
    source_task = GrowthReadService._decode_row(dict(source_task_row))
    receipts = [GrowthReadService._decode_row(dict(row)) for row in conn.execute(
        "SELECT * FROM meta_execution_task_receipt WHERE execution_task_id=? ORDER BY created_at,receipt_id",
        (source_task["execution_task_id"],),
    ).fetchall()]
    source_ids = dict(source_task.get("meta_object_ids_json") or {})
    source_payload = dict(source_task.get("payload_json") or {})
    source_recovery = dict(source_payload.get("recovery_approval") or {})
    previous_recovery_generation = int(
        source_recovery.get("recovery_generation")
        or (1 if source_recovery else 0)
    )
    recoverable = bool(
        source_action.get("action_type") == "CREATE_PAUSED_AD"
        and source_action.get("status") == "MANUAL_REVIEW"
        and source_task.get("status") == "MANUAL_REVIEW"
        and source_task.get("error_code") == "meta_result_uncertain"
        and source_task.get("current_step") == "CAMPAIGN_CREATE"
        and not source_ids
        and len(receipts) == 1
        and receipts[0].get("step_name") == "CAMPAIGN_CREATE"
        and receipts[0].get("step_status") == "UNKNOWN"
        and not dict(receipts[0].get("meta_object_ids_json") or {})
    )
    if not recoverable:
        raise GrowthStateConflict("same_plan_result_not_safely_resumable")
    if previous_recovery_generation >= 2:
        raise GrowthStateConflict("same_plan_recovery_limit_reached")
    dry_run = _latest_dry_run_receipt(conn, source_plan_id)
    if (
        str(dry_run.get("status") or "") != "DRY_RUN_VERIFIED"
        or str(dry_run.get("plan_hash") or "") != payload_hash(plan)
    ):
        raise GrowthStateConflict("matching_dry_run_required_before_same_plan_resume")
    for raw_cell in list(plan.get("cells") or []):
        cell = dict(raw_cell or {})
        if not _plan_cell_creative_unchanged(conn, experiments, plan, cell):
            raise GrowthStateConflict("same_plan_approved_creative_changed")

    new_action_payload = dict(source_action.get("payload_json") or {})
    new_action_payload["recovery_of_operation_action_id"] = source_plan_id
    new_action_payload["recovery_of_execution_task_id"] = source_task["execution_task_id"]
    actions = ExecutionTaskService(conn)
    new_action = actions.create_operation_action(
        decision_id=str(source_action.get("decision_id") or ""),
        episode_id=str(source_action.get("episode_id") or ""),
        action_type="CREATE_PAUSED_AD",
        action_scope=str(source_action.get("action_scope") or "EXPERIMENT"),
        target_type=str(source_action.get("target_type") or "LAUNCH"),
        target_id=str(source_action.get("target_id") or ""),
        payload=new_action_payload,
        created_by=confirmed_by,
        idempotency_key=f"same-plan-recovery-action:{recovery_key}",
    )
    new_plan_id = str(new_action["operation_action_id"])
    expires_at = ""
    approvals = OperationApprovalService(conn)
    approval = approvals.propose(
        new_plan_id, plan, proposed_by=confirmed_by,
        idempotency_key=f"same-plan-recovery-approval:{recovery_key}",
        expires_at=expires_at,
    )
    if approval["status"] == "PROPOSED":
        approval = approvals.transition(
            str(approval["approval_id"]), "APPROVED", actor=confirmed_by,
            single_operator_confirmation="APPROVE_EXACT_PLAN",
        )
    recovery_approval = {
        "status": "APPROVED",
        "source_plan_id": source_plan_id,
        "source_execution_task_id": source_task["execution_task_id"],
        "plan_hash": payload_hash(plan),
        "confirmed_by": confirmed_by,
        "confirmed_at": str(approval.get("approved_at") or utc_now()),
        "expires_at": str(approval.get("expires_at") or expires_at),
        "recovery_generation": previous_recovery_generation + 1,
    }
    experiment_ids = list(plan.get("experiment_ids") or [])
    if not experiment_ids:
        experiment_ids = [
            str(cell.get("experiment_id") or "") for cell in list(plan.get("cells") or [])
        ]
    if not any(str(item) for item in experiment_ids):
        experiment_ids = [str(plan.get("experiment_id") or "")]
    for experiment_id in [str(item) for item in experiment_ids if str(item)]:
        current = experiments.get(experiment_id)
        if str(current.get("state") or "") == "CREATION_PARTIAL_FAILURE":
            experiments.transition(
                experiment_id, "WAITING_CREATE_APPROVAL", actor=confirmed_by,
                reason="continue_same_immutable_plan",
                event_type="SAME_PLAN_RECOVERY_CONFIRMED",
                evidence={"source_plan_id": source_plan_id, "resumed_plan_id": new_plan_id},
            )
    task = actions.enqueue_task(
        new_plan_id,
        idempotency_key=f"same-plan-recovery-live:{recovery_key}",
        payload={
            "execution_mode": "live",
            "approval_id": str(approval["approval_id"]),
            "account_id": plan.get("target_account_id"),
            "plan": plan,
            "experiment_id": plan.get("experiment_id"),
            "experiment_ids": experiment_ids,
            "launch_id": str(plan.get("launch_id") or ""),
            "recovery_approval": recovery_approval,
        },
    )
    cloned_dry_run = dict(dry_run)
    cloned_dry_run.update({
        "plan_id": new_plan_id,
        "recovery_source_plan_id": source_plan_id,
        "verified_at": utc_now(),
    })
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO growth_idempotency_record
            (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
            VALUES ('ad_experiment.plan_dry_run',?,?,200,?,?)""",
            (
                f"same-plan-recovery-dry:{recovery_key}",
                payload_hash({"source_plan_id": source_plan_id, "plan_hash": payload_hash(plan)}),
                canonical_json(cloned_dry_run), utc_now(),
            ),
        )
    for experiment_id in [str(item) for item in experiment_ids if str(item)]:
        current = experiments.get(experiment_id)
        if str(current.get("state") or "") == "WAITING_CREATE_APPROVAL":
            experiments.transition(
                experiment_id, "CREATING_PAUSED_OBJECTS", actor=confirmed_by,
                reason="same_plan_recovery_queued",
                event_type="LIVE_EXECUTION_RESUBMITTED",
                evidence={"source_plan_id": source_plan_id, "resumed_plan_id": new_plan_id, "execution_task_id": task["execution_task_id"]},
            )
    return {
        "ok": True,
        "source_plan_id": source_plan_id,
        "resumed_plan_id": new_plan_id,
        "execution_task_id": task["execution_task_id"],
        "plan_hash": payload_hash(plan),
        "plan_reused": True,
        "campaign_match_count": 0,
        "recovery_generation": previous_recovery_generation + 1,
        "status": task["status"],
        "next_step": "WAIT_FOR_PAUSED_OBJECTS",
    }


def _continue_same_plan_after_created_campaign(
    conn: sqlite3.Connection,
    source_plan_id: str,
    *,
    confirmed_by: str,
    recovery_key: str,
    campaign_matches: List[Dict[str, Any]],
    validated_rejected_step: str = "",
    validated_missing_study: bool = False,
) -> Dict[str, Any]:
    """Continue an immutable Plan after its campaign was confirmed created.

    This path never replays CAMPAIGN_CREATE. The new audited task GET-verifies
    the existing paused campaign, then starts at the first uncompleted write.
    """
    from app.growth.errors import GrowthStateConflict

    experiments = AdExperimentService(conn)
    detail = experiments.plan_detail(source_plan_id)
    source_action = dict(detail.get("operation_action") or {})
    plan = dict(detail.get("plan") or {})
    source_task_row = conn.execute(
        "SELECT * FROM meta_execution_task WHERE operation_action_id=?",
        (source_plan_id,),
    ).fetchone()
    if not source_task_row:
        raise GrowthStateConflict("same_plan_source_task_missing")
    source_task = GrowthReadService._decode_row(dict(source_task_row))
    receipts = [GrowthReadService._decode_row(dict(row)) for row in conn.execute(
        "SELECT * FROM meta_execution_task_receipt WHERE execution_task_id=? ORDER BY created_at,receipt_id",
        (source_task["execution_task_id"],),
    ).fetchall()]
    source_ids = dict(source_task.get("meta_object_ids_json") or {})
    campaign_id = str(source_ids.get("campaign_id") or "").strip()
    current_step = str(source_task.get("current_step") or "").upper()
    matches = [dict(item or {}) for item in campaign_matches]
    campaign_confirmed = bool(
        len(matches) == 1
        and str(matches[0].get("id") or "").strip() == campaign_id
        and str(matches[0].get("status") or matches[0].get("effective_status") or "").upper() == "PAUSED"
    )
    first_continuation = bool(
        source_action.get("action_type") == "CREATE_PAUSED_AD"
        and source_action.get("status") == "MANUAL_REVIEW"
        and source_task.get("status") == "MANUAL_REVIEW"
        and source_task.get("error_code") == "meta_result_uncertain"
        and current_step.endswith("_IMAGE_UPLOAD")
        and campaign_id
        and set(source_ids) == {"campaign_id"}
        and len(receipts) == 2
        and str(receipts[0].get("step_name") or "") == "CAMPAIGN_CREATE"
        and str(receipts[0].get("step_status") or "").upper() == "SUCCESS"
        and str(receipts[1].get("step_name") or "").upper() == current_step
        and str(receipts[1].get("step_status") or "").upper() == "UNKNOWN"
        and campaign_confirmed
    )
    source_payload = dict(source_task.get("payload_json") or {})
    prior_continuation = dict(source_payload.get("continuation") or {})
    verification_retry_count = int(prior_continuation.get("verification_retry_count") or 0)
    final_verification_retry_count = int(prior_continuation.get("final_verification_retry_count") or 0)
    write_rejection_retry_count = int(prior_continuation.get("write_rejection_retry_count") or 0)
    study_write_retry_count = int(prior_continuation.get("study_write_retry_count") or 0)
    readback_repair = bool(
        source_action.get("action_type") == "CREATE_PAUSED_AD"
        and source_action.get("status") == "MANUAL_REVIEW"
        and source_task.get("status") == "MANUAL_REVIEW"
        and source_task.get("error_code") == "continuation_verification_uncertain"
        and current_step == "CAMPAIGN_CREATE"
        and set(source_ids) == {"campaign_id"}
        and len(receipts) == 1
        and str(receipts[0].get("step_name") or "") == "CAMPAIGN_CREATE"
        and str(receipts[0].get("step_status") or "").upper() == "UNKNOWN"
        and prior_continuation.get("completed_steps") == ["CAMPAIGN_CREATE"]
        and str(dict(prior_continuation.get("meta_object_ids") or {}).get("campaign_id") or "") == campaign_id
        and verification_retry_count < 1
        and campaign_confirmed
    )
    successful_prefix = [
        str(receipt.get("step_name") or "").upper()
        for receipt in receipts[:-1]
        if str(receipt.get("step_status") or "").upper() in {"SUCCESS", "VERIFIED"}
    ]
    deterministic_rejection = bool(
        source_action.get("action_type") == "CREATE_PAUSED_AD"
        and source_action.get("status") == "MANUAL_REVIEW"
        and source_task.get("status") == "MANUAL_REVIEW"
        and source_task.get("error_code") == "meta_result_uncertain"
        and str(source_task.get("error_message") or "").startswith("meta_graph_error:")
        and current_step.endswith("_ADSET_CREATE")
        and validated_rejected_step == current_step
        and receipts
        and str(receipts[-1].get("step_name") or "").upper() == current_step
        and str(receipts[-1].get("step_status") or "").upper() == "UNKNOWN"
        and successful_prefix
        and successful_prefix[0] == "CAMPAIGN_CREATE"
        and write_rejection_retry_count < 1
        and campaign_confirmed
    )
    expected_completed_steps = ["CAMPAIGN_CREATE"]
    expected_object_keys = {"campaign_id"}
    plan_cells = list(plan.get("cells") or [])
    for index, raw_cell in enumerate(plan_cells, start=1):
        cell_key = str(dict(raw_cell or {}).get("cell_key") or f"C{index}").upper()
        prefix = cell_key.lower()
        expected_completed_steps.extend([
            f"{cell_key}_IMAGE_UPLOAD", f"{cell_key}_CREATIVE_CREATE",
            f"{cell_key}_ADSET_CREATE", f"{cell_key}_AD_CREATE",
        ])
        expected_object_keys.update({
            f"{prefix}_image_hash", f"{prefix}_creative_id",
            f"{prefix}_adset_id", f"{prefix}_ad_id",
        })
    if str(plan.get("test_variable") or "").lower() in {"audience_strategy", "copy_variant"}:
        expected_pre_study_steps = list(expected_completed_steps)
        expected_pre_study_object_keys = set(expected_object_keys)
        expected_completed_steps.append("STUDY_CREATE")
        expected_object_keys.add("study_id")
    else:
        expected_pre_study_steps = []
        expected_pre_study_object_keys = set()
    if not plan_cells:
        expected_completed_steps.extend(["IMAGE_UPLOAD", "CREATIVE_CREATE", "ADSET_CREATE", "AD_CREATE"])
        expected_object_keys.update({"image_hash", "creative_id", "adset_id", "ad_id"})
    final_verification_repair = bool(
        source_action.get("action_type") == "CREATE_PAUSED_AD"
        and source_action.get("status") == "MANUAL_REVIEW"
        and source_task.get("status") == "MANUAL_REVIEW"
        and source_task.get("error_code") == "final_verification_uncertain"
        and current_step == "VERIFY"
        and receipts
        and str(receipts[-1].get("step_name") or "").upper() == "VERIFY"
        and str(receipts[-1].get("step_status") or "").upper() == "UNKNOWN"
        and successful_prefix == expected_completed_steps
        and set(source_ids) == expected_object_keys
        and final_verification_retry_count < 1
        and campaign_confirmed
    )
    study_rejection = bool(
        source_action.get("action_type") == "CREATE_PAUSED_AD"
        and source_action.get("status") == "MANUAL_REVIEW"
        and source_task.get("status") == "MANUAL_REVIEW"
        and source_task.get("error_code") == "meta_result_uncertain"
        and str(source_task.get("error_message") or "").startswith("meta_graph_error:100:")
        and current_step == "STUDY_CREATE"
        and validated_missing_study
        and receipts
        and str(receipts[-1].get("step_name") or "").upper() == "STUDY_CREATE"
        and str(receipts[-1].get("step_status") or "").upper() == "UNKNOWN"
        and successful_prefix == expected_pre_study_steps
        and set(source_ids) == expected_pre_study_object_keys
        and study_write_retry_count < 1
        and campaign_confirmed
    )
    if not (first_continuation or readback_repair or deterministic_rejection or final_verification_repair or study_rejection):
        raise GrowthStateConflict("same_plan_created_campaign_not_safely_continuable")
    recovery = dict(source_payload.get("recovery_approval") or {})
    expected_recovery_generation = 1 if study_rejection else 2
    if int(recovery.get("recovery_generation") or 0) != expected_recovery_generation:
        raise GrowthStateConflict("same_plan_continuation_generation_invalid")
    dry_run = _latest_dry_run_receipt(conn, source_plan_id)
    if (
        str(dry_run.get("status") or "") != "DRY_RUN_VERIFIED"
        or str(dry_run.get("plan_hash") or "") != payload_hash(plan)
    ):
        raise GrowthStateConflict("matching_dry_run_required_before_same_plan_resume")
    for raw_cell in list(plan.get("cells") or []):
        cell = dict(raw_cell or {})
        if not _plan_cell_creative_unchanged(conn, experiments, plan, cell):
            raise GrowthStateConflict("same_plan_approved_creative_changed")

    new_action_payload = dict(source_action.get("payload_json") or {})
    new_action_payload["continuation_of_operation_action_id"] = source_plan_id
    new_action_payload["continuation_of_execution_task_id"] = source_task["execution_task_id"]
    actions = ExecutionTaskService(conn)
    new_action = actions.create_operation_action(
        decision_id=str(source_action.get("decision_id") or ""),
        episode_id=str(source_action.get("episode_id") or ""),
        action_type="CREATE_PAUSED_AD",
        action_scope=str(source_action.get("action_scope") or "EXPERIMENT"),
        target_type=str(source_action.get("target_type") or "LAUNCH"),
        target_id=str(source_action.get("target_id") or ""),
        payload=new_action_payload,
        created_by=confirmed_by,
        idempotency_key=f"same-plan-continuation-action:{recovery_key}",
    )
    new_plan_id = str(new_action["operation_action_id"])
    expires_at = ""
    approvals = OperationApprovalService(conn)
    approval = approvals.propose(
        new_plan_id, plan, proposed_by=confirmed_by,
        idempotency_key=f"same-plan-continuation-approval:{recovery_key}",
        expires_at=expires_at,
    )
    if approval["status"] == "PROPOSED":
        approval = approvals.transition(
            str(approval["approval_id"]), "APPROVED", actor=confirmed_by,
            single_operator_confirmation="APPROVE_EXACT_PLAN",
        )
    continuation_approval = {
        "status": "APPROVED",
        "source_plan_id": source_plan_id,
        "source_execution_task_id": source_task["execution_task_id"],
        "plan_hash": payload_hash(plan),
        "confirmed_by": confirmed_by,
        "confirmed_at": str(approval.get("approved_at") or utc_now()),
        "expires_at": str(approval.get("expires_at") or expires_at),
        "recovery_generation": 2,
    }
    completed_steps = successful_prefix if (deterministic_rejection or final_verification_repair or study_rejection) else ["CAMPAIGN_CREATE"]
    continuation = {
        "source_plan_id": source_plan_id,
        "source_execution_task_id": source_task["execution_task_id"],
        "plan_hash": payload_hash(plan),
        "completed_steps": completed_steps,
        "meta_object_ids": source_ids if (deterministic_rejection or final_verification_repair or study_rejection) else {"campaign_id": campaign_id},
        "campaign_reconciled_at": utc_now(),
        "verification_retry_count": verification_retry_count + (1 if readback_repair else 0),
        "final_verification_retry_count": final_verification_retry_count + (1 if final_verification_repair else 0),
        "write_rejection_retry_count": write_rejection_retry_count + (1 if deterministic_rejection else 0),
        "study_write_retry_count": study_write_retry_count + (1 if study_rejection else 0),
        "verification_only": final_verification_repair,
    }
    experiment_ids = list(plan.get("experiment_ids") or []) or [
        str(cell.get("experiment_id") or "") for cell in list(plan.get("cells") or [])
    ]
    for experiment_id in [str(item) for item in experiment_ids if str(item)]:
        current = experiments.get(experiment_id)
        if str(current.get("state") or "") == "CREATION_PARTIAL_FAILURE":
            experiments.transition(
                experiment_id, "WAITING_CREATE_APPROVAL", actor=confirmed_by,
                reason="continue_same_plan_from_confirmed_campaign",
                event_type="SAME_PLAN_CONTINUATION_CONFIRMED",
                evidence={"source_plan_id": source_plan_id, "resumed_plan_id": new_plan_id},
            )
    task = actions.enqueue_task(
        new_plan_id,
        idempotency_key=f"same-plan-continuation-live:{recovery_key}",
        payload={
            "execution_mode": "live",
            "approval_id": str(approval["approval_id"]),
            "account_id": plan.get("target_account_id"),
            "plan": plan,
            "experiment_id": plan.get("experiment_id"),
            "experiment_ids": experiment_ids,
            "launch_id": str(plan.get("launch_id") or ""),
            "recovery_approval": continuation_approval,
            "continuation": continuation,
        },
    )
    cloned_dry_run = dict(dry_run)
    cloned_dry_run.update({
        "plan_id": new_plan_id,
        "recovery_source_plan_id": source_plan_id,
        "verified_at": utc_now(),
    })
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO growth_idempotency_record
            (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
            VALUES ('ad_experiment.plan_dry_run',?,?,200,?,?)""",
            (
                f"same-plan-continuation-dry:{recovery_key}",
                payload_hash({"source_plan_id": source_plan_id, "plan_hash": payload_hash(plan)}),
                canonical_json(cloned_dry_run), utc_now(),
            ),
        )
    for experiment_id in [str(item) for item in experiment_ids if str(item)]:
        current = experiments.get(experiment_id)
        if str(current.get("state") or "") == "WAITING_CREATE_APPROVAL":
            experiments.transition(
                experiment_id, "CREATING_PAUSED_OBJECTS", actor=confirmed_by,
                reason="same_plan_continuation_queued",
                event_type="LIVE_EXECUTION_CONTINUED",
                evidence={"source_plan_id": source_plan_id, "resumed_plan_id": new_plan_id, "execution_task_id": task["execution_task_id"]},
            )
    return {
        "ok": True,
        "source_plan_id": source_plan_id,
        "resumed_plan_id": new_plan_id,
        "execution_task_id": task["execution_task_id"],
        "plan_hash": payload_hash(plan),
        "plan_reused": True,
        "campaign_match_count": 1,
        "campaign_reused": True,
        "campaign_id": campaign_id,
        "recovery_generation": 2,
        "status": task["status"],
        "next_step": "WAIT_FOR_PAUSED_OBJECTS",
    }


def _with_connection(db: Any, callback: Callable[[sqlite3.Connection], Any]) -> Any:
    with db.connect() as conn:
        return callback(conn)


def _idempotent_api_mutation(
    conn: sqlite3.Connection, route_key: str, idempotency_key: str,
    request_payload: Dict[str, Any], callback: Callable[[], Dict[str, Any]],
) -> Dict[str, Any]:
    digest = payload_hash(request_payload)
    existing = conn.execute(
        "SELECT request_hash,response_json FROM growth_idempotency_record WHERE route_key=? AND idempotency_key=?",
        (route_key, idempotency_key),
    ).fetchone()
    if existing:
        if existing["request_hash"] != digest:
            from app.growth.errors import GrowthStateConflict
            raise GrowthStateConflict("idempotency_key_payload_conflict")
        return decode_json(existing["response_json"], {})
    result = callback()
    with conn:
        conn.execute(
            """INSERT INTO growth_idempotency_record
            (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
            VALUES (?,?,?,200,?,?)""",
            (route_key, idempotency_key, digest, canonical_json(result), utc_now()),
        )
    return result


def _ad_experiment_detail(conn: sqlite3.Connection, experiment_id: str) -> Dict[str, Any]:
    service = AdExperimentService(conn)
    experiment = service.get(experiment_id)
    try:
        lineage = GrowthReadService(conn).get_experiment_detail(experiment_id)
    except GrowthError:
        lineage = {}
    performance = AdExperimentEvaluator(conn).list(experiment_id)
    timeline = service.timeline(experiment_id)
    return {
        "experiment": experiment,
        "approved_creative": service.latest_approved_creative(experiment_id),
        "timeline": timeline,
        "performance": performance,
        "growth_lineage": lineage,
        "workflow": _ad_experiment_workflow(
            conn, experiment, lineage=lineage, performance=performance,
        ),
    }


def _latest_effective_plan_action(
    actions: List[Dict[str, Any]], tasks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Choose the plan with the latest real execution activity, not creation order."""
    plan_actions = [
        item for item in actions if dict(item.get("payload_json") or {}).get("plan")
    ]

    def activity_key(item: Dict[str, Any]) -> tuple[str, str]:
        action_id = str(item.get("operation_action_id") or "")
        action_tasks = [
            task for task in tasks if str(task.get("operation_action_id") or "") == action_id
        ]
        if action_tasks:
            latest = max(
                action_tasks,
                key=lambda task: (
                    str(task.get("updated_at") or task.get("created_at") or ""),
                    str(task.get("execution_task_id") or ""),
                ),
            )
            activity_at = str(latest.get("updated_at") or latest.get("created_at") or "")
        else:
            activity_at = str(item.get("updated_at") or item.get("created_at") or "")
        return activity_at, action_id

    return max(plan_actions, key=activity_key) if plan_actions else {}


def _execution_failure_guidance(task: Dict[str, Any]) -> Dict[str, Any]:
    step = str(task.get("current_step") or "").upper()
    error_code = str(task.get("error_code") or "")
    error_message = str(task.get("error_message") or "")
    receipts = list(task.get("receipts") or [])
    latest_verification = (
        dict(receipts[-1].get("verification_result_json") or receipts[-1].get("verification_json") or {})
        if receipts else {}
    )
    rate_limited_readback = bool(
        error_code == "meta_result_uncertain"
        and str(latest_verification.get("error") or "") == "adapter_verify_exception"
        and str(latest_verification.get("exception_type") or "") == "MetaRateLimitBlocked"
    )
    stage = {
        "CAMPAIGN_STATUS_UPDATE": "开启广告系列",
        "ADSET_STATUS_UPDATE": "开启广告组",
        "AD_STATUS_UPDATE": "开启广告",
        "VERIFY": "核对 Meta 状态",
        "RECONCILE": "再次核对 Meta 状态",
    }.get(step, "执行 Meta 操作")
    if rate_limited_readback:
        reason = "开启请求已提交，但 Meta 暂时无法返回最新状态，系统正在自动核对"
        next_step = "无需操作；核对成功后会从尚未执行的步骤继续，不会重复已成功的写入"
    elif "meta_graph_endpoint_" in error_message:
        reason = "系统的 Meta 连接配置未通过安全检查，本次没有提交开启请求"
        next_step = "系统修复配置并确认广告仍为暂停后，可重新开启整单投放"
    elif "meta_graph_error:2500:" in error_message:
        reason = "系统连接 Meta 的地址配置错误"
        next_step = "系统修复连接并确认对象仍为暂停后，可重新开启整单投放"
    elif "meta_rate_limit" in error_message or "rate_limit" in error_code:
        reason = "Meta API 当前限流，系统已停止继续请求"
        next_step = "等待限流窗口恢复，系统确认当前状态后再开放重试"
    elif error_code == "meta_result_uncertain":
        reason = "系统没有取得足够的 Meta 回读证据，无法确认操作结果"
        next_step = "先只读核对广告系列、广告组和广告状态，再决定是否重试"
    else:
        reason = "系统未能完成本次 Meta 操作"
        next_step = "查看失败步骤并完成状态核对后继续"
    return {
        "stage": stage, "reason": reason, "next_step": next_step,
        "error_code": error_code, "error_message": error_message,
        "auto_reconcilable": rate_limited_readback,
    }


def _ad_experiment_account_name(
    conn: sqlite3.Connection, experiment: Dict[str, Any],
) -> str:
    """Resolve a stable human-readable account name without requiring Meta live access."""
    hypothesis = dict(experiment.get("hypothesis_json") or {})
    variant = dict(experiment.get("variant_definition_json") or {})
    snapshot = str(hypothesis.get("account_name") or variant.get("account_name") or "").strip()
    if snapshot:
        return snapshot[:120]
    account_id = str(experiment.get("account_id") or "").strip().removeprefix("act_")
    if not account_id:
        return ""
    try:
        row = conn.execute(
            """
            SELECT COALESCE(
                NULLIF(TRIM(account_name), ''),
                CASE WHEN json_valid(payload_json)
                     THEN NULLIF(TRIM(json_extract(payload_json, '$.account_name')), '')
                     ELSE '' END,
                ''
            ) AS account_name
            FROM ad_dashboard_fact_rows
            WHERE account_id IN (?, ?)
              AND (
                LENGTH(TRIM(COALESCE(account_name, ''))) > 0
                OR (
                  json_valid(payload_json)
                  AND LENGTH(TRIM(COALESCE(json_extract(payload_json, '$.account_name'), ''))) > 0
                )
              )
            ORDER BY date DESC, updated_at DESC
            LIMIT 1
            """,
            (account_id, f"act_{account_id}"),
        ).fetchone()
    except sqlite3.OperationalError:
        return ""
    return str(row["account_name"] or "").strip()[:120] if row else ""


def _ad_experiment_workflow(
    conn: sqlite3.Connection,
    experiment: Dict[str, Any],
    *,
    lineage: Optional[Dict[str, Any]] = None,
    performance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Business-facing projection for the dashboard; never mutates state."""
    launch_id = str(experiment.get("source_report_id") or "").strip()
    launch_archived = False
    launch_purged = False
    if launch_id.startswith("newacct_"):
        try:
            archive_row = conn.execute(
                "SELECT status FROM ad_new_account_launch_archive WHERE launch_id=?",
                (launch_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            archive_row = None
        archive_status = str(archive_row["status"] or "").upper() if archive_row else ""
        launch_archived = archive_status == "ARCHIVED"
        launch_purged = archive_status == "PURGED"
    if lineage is None:
        try:
            lineage = GrowthReadService(conn).get_experiment_detail(experiment["experiment_id"])
        except GrowthError:
            lineage = {}
    if performance is None:
        performance = AdExperimentEvaluator(conn).list(experiment["experiment_id"])
    episode_detail = dict((lineage or {}).get("episode_detail") or {})
    actions = list(episode_detail.get("actions") or [])
    tasks = list(episode_detail.get("execution_tasks") or [])
    plan_action = _latest_effective_plan_action(actions, tasks)
    plan_id = str(plan_action.get("operation_action_id") or "")
    approval = dict(plan_action.get("approval") or {})
    plan_tasks = [item for item in tasks if item.get("operation_action_id") == plan_id]
    latest_task = max(
        plan_tasks,
        key=lambda item: (
            str(item.get("updated_at") or item.get("created_at") or ""),
            str(item.get("execution_task_id") or ""),
        ),
    ) if plan_tasks else {}
    failure_guidance = (
        _execution_failure_guidance(latest_task)
        if str(latest_task.get("status") or "") == "MANUAL_REVIEW"
        else {}
    )
    receipts = list(latest_task.get("receipts") or [])
    plan_payload = dict(dict(plan_action.get("payload_json") or {}).get("plan") or {})
    live_execution_available = _meta_live_execution_available(
        str(plan_payload.get("action_type") or ""), str(plan_payload.get("target_account_id") or "")
    )
    dry_run_receipt = _latest_dry_run_receipt(conn, plan_id) if plan_id else {}
    plan_expired = False
    dry_run_verified = bool(
        plan_id
        and str(dry_run_receipt.get("status") or "") == "DRY_RUN_VERIFIED"
        and str(dry_run_receipt.get("plan_hash") or "") == payload_hash(plan_payload)
    )
    evaluations = list((performance or {}).get("items") or [])
    latest = evaluations[-1] if evaluations else {}
    maturity_rule = dict(experiment.get("maturity_rule_json") or {})
    minimum = max(1, int(maturity_rule.get("minimum_conversions") or 10))
    baseline = dict(latest.get("baseline_metrics_json") or {})
    post = dict(latest.get("post_metrics_json") or {})
    before_count = float(baseline.get("real_bind_count", baseline.get("conversions", 0)) or 0)
    after_count = float(post.get("real_bind_count", post.get("conversions", 0)) or 0)
    sample_count = min(before_count, after_count) if before_count and after_count else max(before_count, after_count)
    quality_status = str(latest.get("data_quality_status") or "PENDING").upper()
    sample_pct = min(100, round(sample_count * 100 / minimum)) if quality_status == "PASS" else 0
    status = str(latest.get("evaluation_status") or "PENDING").upper()
    evidence_mature = bool(
        latest.get("checkpoint") == "D7"
        and quality_status == "PASS"
        and sample_count >= minimum
        and status not in {"DATA_INCOMPLETE", "INSUFFICIENT_SAMPLE", "MIXED_CHANGE", "NOT_ATTRIBUTABLE"}
    )
    maturity_pct = 100 if evidence_mature else sample_pct
    state = str(experiment.get("state") or "DRAFT").upper()
    try:
        meta_review_row = conn.execute(
            "SELECT * FROM ad_meta_review_state WHERE experiment_id=?",
            (str(experiment.get("experiment_id") or ""),),
        ).fetchone()
    except sqlite3.OperationalError:
        meta_review_row = None
    meta_review = dict(meta_review_row) if meta_review_row else {}
    if meta_review:
        meta_review["review_feedback_json"] = decode_json(meta_review.get("review_feedback_json"), {})
    remediation_status = str(meta_review.get("remediation_status") or "NONE").upper()
    incident_reconciled = str(experiment.get("state_reason") or "").startswith("incident_reconciled_")
    activation_readback_pending = bool(
        str(plan_payload.get("action_type") or "").upper() == "REACTIVATE_AD"
        and (
            str(latest_task.get("status") or "").upper() in {"QUEUED", "RUNNING", "VERIFYING"}
            or bool(failure_guidance.get("auto_reconcilable"))
        )
    )
    error_states = {"CREATION_PARTIAL_FAILURE", "DATA_INCOMPLETE", "MIXED_CHANGE", "CREATIVE_REJECTED"}
    system_work_states = {
        "DRAFT", "CREATIVE_GENERATING", "CREATIVE_REVIEW", "CREATIVE_APPROVED",
        "WAITING_CREATE_APPROVAL", "CREATING_PAUSED_OBJECTS",
    }
    observing_states = {"META_REVIEW_PENDING", "RUNNING", "MATURING", "EVALUATING_ADJUSTMENT"}
    completed_states = {"ARCHIVED"}
    if activation_readback_pending:
        bucket, current_action = "system_work", "系统正在核对开启状态"
    elif plan_expired and str(plan_payload.get("action_type") or "") == "CREATE_PAUSED_AD" and not receipts:
        bucket, current_action = "system_work", "AI 正在重新生成创建方案"
    elif state == "CREATIVE_REJECTED" and remediation_status in {"DETECTED", "GENERATING"}:
        bucket, current_action = "system_work", "AI 正在根据 Meta 拒审原因生成合规替代素材"
    elif state in error_states or (str(latest_task.get("status") or "") == "MANUAL_REVIEW" and not incident_reconciled):
        bucket, current_action = "exception", "处理异常并核对回执"
    elif state in observing_states:
        bucket, current_action = "observing", "查看观察进度"
    elif state in system_work_states:
        bucket = "system_work"
        current_action = {
            "DRAFT": "AI 正在准备创建方案",
            "CREATIVE_GENERATING": "AI 正在生成素材",
            "CREATIVE_REVIEW": "AI 正在审核素材",
            "CREATIVE_APPROVED": "AI 正在生成创建方案",
            "WAITING_CREATE_APPROVAL": "AI 正在完成安全演练",
            "CREATING_PAUSED_OBJECTS": "AI 正在创建并回读暂停态广告",
        }.get(state, "AI 正在继续订单")
    elif state in completed_states:
        bucket, current_action = "completed", "查看已归档结果"
    else:
        bucket = "action_required"
        current_action = {
            "DRAFT": "完善并生成执行方案" if episode_detail.get("decision") else "补全实验来源",
            "WAITING_CREATE_APPROVAL": "复核并确认创建计划",
            "WAITING_ADJUSTMENT_APPROVAL": "复核并确认调整计划",
            "CREATING_PAUSED_OBJECTS": (
                "广告尚未创建，等待真实创建通道"
                if latest_task and not receipts and not live_execution_available
                else "查看创建进度与回执"
            ),
            "READY_FOR_ACTIVATION": "复核启用计划",
            "RECOMMENDATION_READY": "确认下一轮建议",
            "EFFECTIVE": "确认放量或扩展方案",
            "INEFFECTIVE": "确认回滚或素材修复",
            "INCONCLUSIVE": "确认继续观察或结束",
            "PAUSED": "确认保持暂停或重新启用",
        }.get(state, "查看当前任务")
        if dry_run_verified and not latest_task and str(plan_payload.get("action_type") or "") == "CREATE_PAUSED_AD":
            current_action = "确认创建暂停态广告"
    if incident_reconciled and state == "PAUSED":
        bucket, current_action = "action_required", "真实状态已核对为暂停，可重新开启广告"
    required = ["D1", "D3", "D7"]
    completed = {str(item.get("checkpoint") or "") for item in evaluations}
    next_checkpoint = next((item for item in required if item not in completed), "")
    blockers = []
    passive_observation = dict(experiment.get("hypothesis_json") or {}).get("mode") == "passive_observation"
    observation_snapshot = dict(
        dict(experiment.get("hypothesis_json") or {}).get("latest_observation") or {}
    )
    if passive_observation:
        five_metric_keys = {"installs", "cpi", "ctr", "real_joins", "real_join_cpa"}
        core_maturity = {
            key: detail for key, detail in dict(observation_snapshot.get("maturity") or {}).items()
            if key in five_metric_keys
        }
        mature_states = {"strong", "high_confidence"}
        mature_count = sum(
            1 for detail in core_maturity.values()
            if dict(detail or {}).get("state") in mature_states
        )
        maturity_pct = round(mature_count * 100 / len(core_maturity)) if core_maturity else 0
        evidence_mature = bool(
            latest.get("checkpoint") == "D7"
            and core_maturity
            and mature_count == len(core_maturity)
            and quality_status == "PASS"
        )
    if passive_observation and state == "MATURING" and latest.get("checkpoint") == "D7" and not evidence_mature:
        maturity = core_maturity
        pending = [name for name, detail in maturity.items() if dict(detail or {}).get("state") not in {"strong", "high_confidence"}]
        labels = {
            "installs": "安装数", "cpi": "安装单价",
            "ctr": "CTR", "real_joins": "真实入会", "real_join_cpa": "入会单价",
        }
        current_action = "继续积累未成熟维度" + (f"：{'、'.join(labels.get(name, name) for name in pending)}" if pending else "")
    if launch_archived:
        bucket, current_action = "completed", "查看已归档订单"
    if launch_purged:
        bucket, current_action = "completed", "订单已删除，仅保留审计记录"
    if not episode_detail.get("decision") and not passive_observation:
        blockers.append("missing_decision")
    if not experiment.get("account_id"):
        blockers.append("missing_account")
    if state == "DRAFT" and not plan_id and not launch_id.startswith("newacct_"):
        blockers.append("plan_not_ready")
    if quality_status not in {"PASS", "PENDING"}:
        blockers.append("data_quality_not_passed")
    if latest_task.get("status") == "MANUAL_REVIEW" and not incident_reconciled and not activation_readback_pending:
        blockers.append("manual_review_required")
    if plan_expired and not receipts and not launch_id.startswith("newacct_"):
        blockers.append("plan_expired_replan_required")
    if launch_archived:
        blockers.append("launch_archived")
    if launch_purged:
        blockers.append("launch_purged")
    if (
        str(latest_task.get("status") or "") == "QUEUED"
        and not receipts
        and not live_execution_available
    ):
        blockers.append("meta_live_execution_unavailable")
    return {
        "bucket": bucket,
        "current_action": current_action,
        "account_name": _ad_experiment_account_name(conn, experiment),
        "launch_id": launch_id if launch_id.startswith("newacct_") else "",
        "launch_archived": launch_archived,
        "launch_purged": launch_purged,
        "plan_id": "" if incident_reconciled else plan_id,
        "plan_action_type": "" if incident_reconciled else str(plan_action.get("action_type") or ""),
        "approval_status": "" if incident_reconciled else str(approval.get("status") or ""),
        "approval_expires_at": "" if incident_reconciled else str(approval.get("expires_at") or ""),
        "plan_expired": False if incident_reconciled else plan_expired,
        "execution_task_id": "" if incident_reconciled else str(latest_task.get("execution_task_id") or ""),
        "execution_status": "" if incident_reconciled else str(latest_task.get("status") or ""),
        "execution_error_code": "" if incident_reconciled else str(latest_task.get("error_code") or ""),
        "execution_error_message": "" if incident_reconciled else str(latest_task.get("error_message") or ""),
        "execution_failed_step": "" if incident_reconciled else str(latest_task.get("current_step") or ""),
        "failure": {} if incident_reconciled else failure_guidance,
        "activation_readback_pending": False if incident_reconciled else activation_readback_pending,
        "meta_review": meta_review,
        "dry_run_verified": dry_run_verified,
        "dry_run_verified_at": str(dry_run_receipt.get("verified_at") or ""),
        "dry_run_receipt_count": len(list(dry_run_receipt.get("receipts") or [])),
        "receipt_count": 0 if incident_reconciled else len(receipts),
        "live_execution_available": live_execution_available,
        "passive_observation": passive_observation,
        "observation_snapshot": observation_snapshot,
        "receipts": receipts,
        "minimum_conversions": minimum,
        "sample_count": sample_count,
        "maturity_pct": maturity_pct,
        "evidence_mature": evidence_mature,
        "data_quality_status": quality_status,
        "next_checkpoint": next_checkpoint,
        "completed_checkpoints": sorted(completed, key=lambda item: required.index(item) if item in required else 99),
        "blockers": blockers,
    }


def _update_episode(db: Any, episode_id: str, body: EpisodeUpdateRequest, user: Dict[str, Any]) -> Dict[str, Any]:
    with db.connect() as conn:
        return EpisodeService(conn).transition(
            episode_id,
            body.status,
            outcome=body.outcome_json,
            lesson=body.lesson_json,
            action=body.action_json,
            actor=str(user.get("user_id") or user.get("username") or ""),
            reason=body.reason,
        )


def _create_knowledge(
    db: Any, body: KnowledgeCreateRequest, idempotency_key: str, request_id: str,
) -> Dict[str, Any]:
    with db.connect() as conn:
        result = KnowledgeService(conn).create_candidate(
            body.episode_id, body.pattern_type, body.pattern_json,
            idempotency_key=idempotency_key,
        )
        result["request_id"] = request_id
        return result


def _get_execution_task(db: Any, task_id: str) -> Dict[str, Any]:
    with db.connect() as conn:
        return ExecutionTaskService(conn).get_task(task_id)


def _list_episodes(db: Any, *, status: str, limit: int) -> Dict[str, Any]:
    with db.connect() as conn:
        return GrowthReadService(conn).list_episodes(status=status, limit=limit)


def _get_episode_detail(db: Any, episode_id: str) -> Dict[str, Any]:
    with db.connect() as conn:
        return GrowthReadService(conn).get_episode_detail(episode_id)


def _get_experiment_detail(db: Any, experiment_id: str) -> Dict[str, Any]:
    with db.connect() as conn:
        return GrowthReadService(conn).get_experiment_detail(experiment_id)


def _list_knowledge(db: Any, *, status: str, limit: int) -> Dict[str, Any]:
    with db.connect() as conn:
        return GrowthReadService(conn).list_knowledge(status=status, limit=limit)


def _get_knowledge_detail(db: Any, knowledge_id: str) -> Dict[str, Any]:
    with db.connect() as conn:
        return GrowthReadService(conn).get_knowledge_detail(knowledge_id)


def _transition_operation_approval(
    db: Any, approval_id: str, body: StatusTransitionRequest, user: Dict[str, Any],
) -> Dict[str, Any]:
    with db.connect() as conn:
        return OperationApprovalService(conn).transition(
            approval_id, body.status,
            actor=str(user.get("user_id") or user.get("username") or ""),
        )


def _mine_patterns(
    db: Any, minimum_support: int, idempotency_key: str, request_id: str,
) -> Dict[str, Any]:
    with db.connect() as conn:
        items = PatternMiningService(conn).mine(
            minimum_support=minimum_support, idempotency_key=idempotency_key,
        )
        return {"items": items, "count": len(items), "request_id": request_id}


def _create_strategy_recommendation(
    db: Any, body: StrategyRecommendationCreateRequest, user: Dict[str, Any],
    idempotency_key: str, request_id: str,
) -> Dict[str, Any]:
    with db.connect() as conn:
        result = AdaptiveGrowthAgentService(conn).recommend(
            body.context_snapshot_id,
            created_by=str(user.get("user_id") or user.get("username") or "growth-agent"),
            idempotency_key=idempotency_key,
        )
        result["request_id"] = request_id
        return result


def _get_strategy_recommendation(db: Any, strategy_recommendation_id: str) -> Dict[str, Any]:
    with db.connect() as conn:
        return AdaptiveGrowthAgentService(conn).get_recommendation(strategy_recommendation_id)


def _transition_strategy_recommendation(
    db: Any, strategy_recommendation_id: str, body: StatusTransitionRequest,
    user: Dict[str, Any],
) -> Dict[str, Any]:
    with db.connect() as conn:
        return AdaptiveGrowthAgentService(conn).transition_recommendation(
            strategy_recommendation_id, body.status,
            actor=str(user.get("user_id") or user.get("username") or ""),
        )


def _execute_low_risk_strategy(
    db: Any, strategy_recommendation_id: str, body: LowRiskExecuteRequest,
    user: Dict[str, Any], idempotency_key: str, request_id: str,
) -> Dict[str, Any]:
    with db.connect() as conn:
        result = AdaptiveGrowthAgentService(conn).execute_low_risk(
            strategy_recommendation_id, decision_id=body.decision_id,
            actor=str(user.get("user_id") or user.get("username") or "growth-agent"),
            idempotency_key=idempotency_key,
        )
        result["request_id"] = request_id
        return result


def _create_simulation(
    db: Any, body: SimulationCreateRequest, idempotency_key: str, request_id: str,
) -> Dict[str, Any]:
    with db.connect() as conn:
        result = AdaptiveGrowthAgentService(conn).simulate(
            body.context_snapshot_id, body.proposed_action,
            idempotency_key=idempotency_key,
        )
        result["request_id"] = request_id
        return result


def _get_simulation(db: Any, simulation_id: str) -> Dict[str, Any]:
    with db.connect() as conn:
        return AdaptiveGrowthAgentService(conn).get_simulation(simulation_id)


def _get_autonomy_policy(db: Any, account_id: str) -> Dict[str, Any]:
    with db.connect() as conn:
        return GrowthAutonomyService(conn).get_policy(account_id)


def _set_autonomy_policy(
    db: Any, account_id: str, body: AutonomyPolicyRequest, user: Dict[str, Any],
) -> Dict[str, Any]:
    with db.connect() as conn:
        return GrowthAutonomyService(conn).set_policy(
            account_id, level=body.level, allowed_action_types=body.allowed_action_types,
            actor=str(user.get("user_id") or user.get("username") or "operator"),
            reason=body.reason, max_daily_budget_usd=body.max_daily_budget_usd,
            max_budget_change_pct=body.max_budget_change_pct,
            minimum_installs=body.minimum_installs,
            minimum_real_joins=body.minimum_real_joins,
            require_real_join_attribution=body.require_real_join_attribution,
        )


def _get_autonomy_capabilities(db: Any, account_id: str) -> Dict[str, Any]:
    with db.connect() as conn:
        return GrowthAutonomyService(conn).capability_catalog(account_id)


def _sync_next_actions(db: Any, account_id: str) -> Dict[str, Any]:
    with db.connect() as conn:
        return GrowthAutonomyService(conn).sync_evaluations(account_id=account_id)


def _list_next_actions(db: Any, account_id: str, status: str, limit: int) -> Dict[str, Any]:
    with db.connect() as conn:
        return GrowthAutonomyService(conn).list_next_actions(
            account_id=account_id, status=status, limit=limit,
        )
