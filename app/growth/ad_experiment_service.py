from __future__ import annotations

import copy
import hashlib
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.growth.approval_service import OperationApprovalService
from app.growth.ad_copy_benchmark import copy_version_id
from app.growth.audience_strategy import audience_contract, assert_strict_targeting, audience_strategy, strict_meta_targeting
from app.growth.common import canonical_json, decode_json, new_id, payload_hash, utc_now
from app.growth.meta_sdk_contract import META_SDK_CONTRACT_VERSION
from app.growth.delivery_guardrails import new_account_delivery_guardrails
from app.growth.errors import GrowthNotFound, GrowthStateConflict, GrowthValidationError
from app.growth.execution_service import ExecutionTaskService
from app.growth.primary_text_only_compiler import (
    assert_phase1_live_permission,
    attach_compiler_receipt,
)
from app.growth.schema import ensure_growth_schema


EXPERIMENT_TYPES = {
    "NEW_AD_TEST", "WINNER_EXTENSION", "CREATIVE_REPAIR", "CREATIVE_REPLACEMENT",
    "BUDGET_SCALE_UP", "BUDGET_REDUCTION", "PAUSE_TEST", "REACTIVATION_TEST",
}

META_ACTION_TYPES = {
    "CREATE_PAUSED_AD", "REPLACE_CREATIVE", "INCREASE_BUDGET", "DECREASE_BUDGET",
    "PAUSE_AD", "PAUSE_ADSET", "REACTIVATE_AD", "SET_COST_CAP",
}


def _cost_cap_bid_amount(value: Any) -> int:
    try:
        amount = int(round(float(value) * 100))
    except (TypeError, ValueError) as exc:
        raise GrowthValidationError("cpi_cost_cap_required") from exc
    if amount < 1:
        raise GrowthValidationError("cpi_cost_cap_required")
    return amount


def resolve_rebuild_source_budget(
    adset: Dict[str, Any], campaign: Dict[str, Any],
) -> Dict[str, Any]:
    """Resolve the authoritative daily-budget owner for a rebuild source."""

    def minor_units(value: Any) -> int:
        if value in (None, ""):
            return 0
        try:
            amount = int(str(value))
        except (TypeError, ValueError) as exc:
            raise GrowthValidationError("rebuild_source_budget_invalid") from exc
        if amount < 0:
            raise GrowthValidationError("rebuild_source_budget_invalid")
        return amount

    adset_daily = minor_units(adset.get("daily_budget"))
    campaign_daily = minor_units(campaign.get("daily_budget"))
    adset_lifetime = minor_units(adset.get("lifetime_budget"))
    campaign_lifetime = minor_units(campaign.get("lifetime_budget"))
    if adset_daily and campaign_daily:
        raise GrowthValidationError("rebuild_source_budget_owner_ambiguous")
    if adset_daily:
        budget_mode = "ABO"
        daily_budget_minor = adset_daily
    elif campaign_daily:
        budget_mode = "CBO"
        daily_budget_minor = campaign_daily
    elif adset_lifetime or campaign_lifetime:
        raise GrowthValidationError("rebuild_source_lifetime_budget_not_supported")
    else:
        raise GrowthValidationError("rebuild_source_daily_budget_missing")
    daily_budget_usd = daily_budget_minor / 100.0
    if not 5 <= daily_budget_usd <= 100:
        raise GrowthValidationError("rebuild_source_daily_budget_out_of_range")
    return {
        "budget_mode": budget_mode,
        "daily_budget_usd": daily_budget_usd,
        "adset_daily_budget": adset_daily or None,
        "campaign_daily_budget": campaign_daily or None,
    }

EXPERIMENT_TRANSITIONS = {
    "DRAFT": {"CREATIVE_GENERATING", "CREATIVE_REVIEW", "WAITING_CREATE_APPROVAL", "ARCHIVED"},
    "CREATIVE_GENERATING": {"CREATIVE_REVIEW", "DATA_INCOMPLETE"},
    "CREATIVE_REVIEW": {"CREATIVE_REJECTED", "WAITING_CREATE_APPROVAL"},
    "CREATIVE_REJECTED": {"CREATIVE_GENERATING", "META_REVIEW_PENDING", "WAITING_ADJUSTMENT_APPROVAL", "RUNNING", "ARCHIVED"},
    "WAITING_CREATE_APPROVAL": {"CREATING_PAUSED_OBJECTS", "ARCHIVED"},
    "CREATING_PAUSED_OBJECTS": {"META_REVIEW_PENDING", "CREATION_PARTIAL_FAILURE"},
    "CREATION_PARTIAL_FAILURE": {"WAITING_CREATE_APPROVAL", "META_REVIEW_PENDING", "ARCHIVED"},
    "META_REVIEW_PENDING": {"CREATIVE_REJECTED", "READY_FOR_ACTIVATION", "WAITING_ADJUSTMENT_APPROVAL", "ADJUSTING", "RUNNING", "DATA_INCOMPLETE", "ARCHIVED"},
    "READY_FOR_ACTIVATION": {"WAITING_ADJUSTMENT_APPROVAL", "RUNNING", "ARCHIVED"},
    "RUNNING": {"CREATIVE_REJECTED", "MATURING", "RECOMMENDATION_READY", "WAITING_ADJUSTMENT_APPROVAL", "PAUSED", "ARCHIVED"},
    "MATURING": {"CREATIVE_REJECTED", "RECOMMENDATION_READY", "WAITING_ADJUSTMENT_APPROVAL", "ADJUSTING", "PAUSED", "EVALUATING_ADJUSTMENT", "DATA_INCOMPLETE", "MIXED_CHANGE"},
    "RECOMMENDATION_READY": {"WAITING_ADJUSTMENT_APPROVAL", "MATURING", "ARCHIVED"},
    "WAITING_ADJUSTMENT_APPROVAL": {"ADJUSTING", "RUNNING", "PAUSED", "ARCHIVED"},
    "ADJUSTING": {"META_REVIEW_PENDING", "RUNNING", "PAUSED", "EVALUATING_ADJUSTMENT", "CREATION_PARTIAL_FAILURE", "DATA_INCOMPLETE"},
    "EVALUATING_ADJUSTMENT": {"CREATIVE_REJECTED", "META_REVIEW_PENDING", "RUNNING", "PAUSED", "EFFECTIVE", "INEFFECTIVE", "INCONCLUSIVE", "DATA_INCOMPLETE", "MIXED_CHANGE"},
    "EFFECTIVE": {"RECOMMENDATION_READY", "WAITING_ADJUSTMENT_APPROVAL", "ARCHIVED"},
    "INEFFECTIVE": {"RECOMMENDATION_READY", "WAITING_ADJUSTMENT_APPROVAL", "PAUSED", "ARCHIVED"},
    "INCONCLUSIVE": {"MATURING", "RECOMMENDATION_READY", "ARCHIVED"},
    "DATA_INCOMPLETE": {"META_REVIEW_PENDING", "ADJUSTING", "RUNNING", "MATURING", "RECOMMENDATION_READY", "PAUSED", "ARCHIVED"},
    "MIXED_CHANGE": {"RECOMMENDATION_READY", "ARCHIVED"},
    "PAUSED": {"WAITING_ADJUSTMENT_APPROVAL", "ADJUSTING", "EVALUATING_ADJUSTMENT", "ARCHIVED"},
    "ARCHIVED": set(),
}


class AdExperimentService:
    """Advertising domain projection; Growth remains the execution/approval truth."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_growth_schema(conn)

    def create_draft(self, payload: Dict[str, Any], *, actor: str, idempotency_key: str) -> Dict[str, Any]:
        if not str(idempotency_key or "").strip():
            raise GrowthValidationError("idempotency_key_is_required")
        target_app = str(payload.get("target_app") or "").strip()
        experiment_type = str(payload.get("experiment_type") or "").strip().upper()
        if not target_app:
            raise GrowthValidationError("target_app_is_required")
        if experiment_type not in EXPERIMENT_TYPES:
            raise GrowthValidationError("invalid_experiment_type")
        digest = payload_hash(payload)
        existing = self.conn.execute(
            "SELECT request_hash, response_json FROM growth_idempotency_record WHERE route_key='ad_experiment.create' AND idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            if existing["request_hash"] != digest:
                raise GrowthStateConflict("idempotency_key_payload_conflict")
            return decode_json(existing["response_json"], {})
        experiment_id = new_id("adexp")
        experiment_code = str(payload.get("experiment_code") or experiment_id.replace("adexp_", "EXP-")[:24]).strip()
        now = utc_now()
        values = (
            experiment_id, experiment_code, target_app, str(payload.get("country") or ""),
            str(payload.get("platform") or "meta"), str(payload.get("account_id") or "").removeprefix("act_"),
            str(payload.get("source_report_id") or ""), str(payload.get("source_recommendation_id") or ""),
            str(payload.get("source_campaign_id") or ""), str(payload.get("source_adset_id") or ""),
            str(payload.get("source_ad_id") or ""), str(payload.get("source_creative_id") or ""),
            experiment_type, canonical_json(payload.get("hypothesis_json") or {}),
            str(payload.get("primary_metric") or ""), canonical_json(payload.get("guardrail_metrics_json") or []),
            canonical_json(payload.get("maturity_rule_json") or {}), canonical_json(payload.get("stop_rule_json") or {}),
            canonical_json(payload.get("control_definition_json") or {}), canonical_json(payload.get("variant_definition_json") or {}),
            actor, now, now,
        )
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO ad_experiment
                    (experiment_id, experiment_code, target_app, country, platform, account_id,
                     source_report_id, source_recommendation_id, source_campaign_id, source_adset_id,
                     source_ad_id, source_creative_id, experiment_type, hypothesis_json, primary_metric,
                     guardrail_metrics_json, maturity_rule_json, stop_rule_json, control_definition_json,
                     variant_definition_json, created_by, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, values,
                )
                self._event(experiment_id, "", "DRAFT", "EXPERIMENT_CREATED", actor, "", payload)
                result = self.get(experiment_id)
                self.conn.execute(
                    """
                    INSERT INTO growth_idempotency_record
                    (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
                    VALUES ('ad_experiment.create',?,?,201,?,?)
                    """, (idempotency_key, digest, canonical_json(result), now),
                )
            return result
        except sqlite3.IntegrityError as exc:
            raise GrowthStateConflict("ad_experiment_constraint_conflict") from exc

    def list(
        self, *, state: str = "", limit: int = 50,
        exclude_states: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        normalized = str(state or "").strip().upper()
        if normalized and normalized not in EXPERIMENT_TRANSITIONS:
            raise GrowthValidationError("invalid_experiment_state")
        normalized_excluded = [str(item or "").strip().upper() for item in (exclude_states or [])]
        if any(item not in EXPERIMENT_TRANSITIONS for item in normalized_excluded):
            raise GrowthValidationError("invalid_experiment_state")
        if normalized:
            where = "WHERE state=?"
            params: List[Any] = [normalized]
        elif normalized_excluded:
            placeholders = ",".join("?" for _ in normalized_excluded)
            where = f"WHERE state NOT IN ({placeholders})"
            params = list(normalized_excluded)
        else:
            where = ""
            params = []
        params.append(max(1, min(int(limit or 50), 200)))
        rows = self.conn.execute(
            f"SELECT * FROM ad_experiment {where} ORDER BY updated_at DESC, experiment_id DESC LIMIT ?", params,
        ).fetchall()
        return {"items": [self._serialize(row) for row in rows], "count": len(rows), "state": normalized}

    def get(self, experiment_id: str) -> Dict[str, Any]:
        row = self.conn.execute("SELECT * FROM ad_experiment WHERE experiment_id=?", (experiment_id,)).fetchone()
        if not row:
            raise GrowthNotFound("ad_experiment_not_found")
        result = self._serialize(row)
        result["allowed_next_states"] = sorted(EXPERIMENT_TRANSITIONS.get(result["state"], set()))
        return result

    def timeline(self, experiment_id: str) -> Dict[str, Any]:
        self.get(experiment_id)
        rows = self.conn.execute(
            "SELECT * FROM ad_experiment_events WHERE experiment_id=? ORDER BY created_at,event_id",
            (experiment_id,),
        ).fetchall()
        return {"experiment_id": experiment_id, "items": [self._serialize(row) for row in rows], "count": len(rows)}

    def latest_approved_creative(self, experiment_id: str) -> Dict[str, Any]:
        """Return the current approved image linked to this Growth experiment.

        Review history is audit evidence, not current truth.  The generated
        image row owns the current review state, which prevents an older
        APPROVED record from becoming usable again after that image is archived.
        """
        self.get(experiment_id)
        if not self._creative_linkage_tables_available():
            return {}
        row = self.conn.execute(
            """
            SELECT i.image_id,i.image_hash,i.review_status,q.job_id,r.created_at AS approved_at
            FROM creative_pro_work_queue q
            JOIN creative_generated_images i
              ON i.request_id=json_extract(q.generation_plan_json,'$.generation_request_id')
              OR json_extract(i.metadata_json,'$.job_id')=q.job_id
              OR json_extract(i.metadata_json,'$.creative_pro_job_id')=q.job_id
            JOIN creative_review_records r
              ON r.image_id=i.image_id AND upper(r.review_status)='APPROVED'
            WHERE json_extract(q.material_refs_json,'$.growth_experiment_id')=?
              AND lower(q.status)!='deleted'
              AND lower(i.review_status) IN ('approved','used_in_ad')
            ORDER BY r.created_at DESC,i.created_at DESC,i.image_id DESC
            LIMIT 1
            """,
            (experiment_id,),
        ).fetchone()
        if not row:
            return {}
        return {
            "image_id": str(row["image_id"]), "image_hash": str(row["image_hash"] or ""),
            "job_id": str(row["job_id"] or ""), "approved_at": str(row["approved_at"] or ""),
            "review_status": str(row["review_status"] or "").upper(),
        }

    def record_creative_review(
        self, experiment_id: str, review_status: str, *, actor: str,
        image_id: str, job_id: str = "", image_hash: str = "",
    ) -> Dict[str, Any]:
        """Synchronize every linked creative review into the experiment audit."""
        current = self.get(experiment_id)
        normalized = str(review_status or "").strip().upper()
        evidence = {
            "image_id": str(image_id or ""), "job_id": str(job_id or ""),
            "image_hash": str(image_hash or ""), "review_status": normalized,
        }
        if normalized == "APPROVED" and current["state"] in {"DRAFT", "CREATIVE_GENERATING"}:
            return self.transition(
                experiment_id, "CREATIVE_REVIEW", actor=actor,
                reason="creative_approved", event_type="CREATIVE_APPROVED", evidence=evidence,
            )
        event_type = {
            "APPROVED": "CREATIVE_APPROVED", "ARCHIVED": "CREATIVE_ARCHIVED",
            "REJECTED": "CREATIVE_REJECTED", "NEEDS_REVIEW": "CREATIVE_REVIEW_REQUESTED",
        }.get(normalized, "CREATIVE_REVIEW_UPDATED")
        now = utc_now()
        with self.conn:
            self.conn.execute(
                "UPDATE ad_experiment SET updated_at=? WHERE experiment_id=?",
                (now, experiment_id),
            )
            self._event(
                experiment_id, current["state"], current["state"], event_type,
                actor, f"creative_{normalized.lower()}", evidence,
            )
        return self.get(experiment_id)

    def transition(
        self, experiment_id: str, state: str, *, actor: str, reason: str = "",
        event_type: str = "STATE_CHANGED", evidence: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        current = self.get(experiment_id)
        target = str(state or "").strip().upper()
        if target not in EXPERIMENT_TRANSITIONS.get(current["state"], set()):
            raise GrowthStateConflict(f"illegal_ad_experiment_transition:{current['state']}:{target}")
        now = utc_now()
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE ad_experiment SET state=?,state_reason=?,updated_at=? WHERE experiment_id=? AND state=?",
                (target, reason, now, experiment_id, current["state"]),
            )
            if cursor.rowcount != 1:
                raise GrowthStateConflict("ad_experiment_changed_concurrently")
            self._event(experiment_id, current["state"], target, event_type, actor, reason, evidence or {})
            self.conn.execute(
                """INSERT INTO growth_state_transition
                (transition_id,entity_type,entity_id,from_status,to_status,reason,actor,created_at)
                VALUES (?,'AD_EXPERIMENT',?,?,?,?,?,?)""",
                (new_id("transition"), experiment_id, current["state"], target, reason, actor, now),
            )
        return self.get(experiment_id)

    def preview_plan(
        self, experiment_id: str, request: Dict[str, Any], *, actor: str, idempotency_key: str,
    ) -> Dict[str, Any]:
        experiment = self.get(experiment_id)
        request_digest = payload_hash({"experiment_id": experiment_id, "request": request})
        outer = self.conn.execute(
            "SELECT request_hash,response_json FROM growth_idempotency_record WHERE route_key='ad_experiment.plan_preview' AND idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if outer:
            if outer["request_hash"] != request_digest:
                raise GrowthStateConflict("idempotency_key_payload_conflict")
            return decode_json(outer["response_json"], {})
        partial = self.conn.execute(
            "SELECT response_json FROM growth_idempotency_record WHERE route_key='operation_action.create' AND idempotency_key=?",
            (f"plan-action:{idempotency_key}",),
        ).fetchone()
        if partial:
            action_id = str(decode_json(partial["response_json"], {}).get("operation_action_id") or "")
            detail = self.plan_detail(action_id)
            stored_digest = str(detail["operation_action"].get("payload_json", {}).get("plan_request_hash") or "")
            if stored_digest != request_digest:
                raise GrowthStateConflict("idempotency_key_payload_conflict")
            recovered = {
                "plan_id": action_id, "operation_action": detail["operation_action"],
                "approval": detail["approval"], "plan": detail["plan"],
                "plan_hash": payload_hash(detail["plan"]),
            }
            with self.conn:
                self.conn.execute(
                    """INSERT INTO growth_idempotency_record
                    (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
                    VALUES ('ad_experiment.plan_preview',?,?,201,?,?)""",
                    (idempotency_key, request_digest, canonical_json(recovered), utc_now()),
                )
            return recovered
        action_type = str(request.get("action_type") or "").strip().upper()
        if action_type not in META_ACTION_TYPES:
            raise GrowthValidationError("unsupported_ad_experiment_action")
        target_type = str(request.get("target_object_type") or "AD").strip().upper()
        target_id = str(request.get("target_object_id") or "").strip()
        if action_type != "CREATE_PAUSED_AD" and not target_id:
            raise GrowthValidationError("target_object_id_is_required")
        resolved_request = self._resolve_create_plan_request(experiment, request, action_type)
        plan = self._compile_plan(experiment, resolved_request, action_type, target_type, target_id)
        episode_id = str(request.get("episode_id") or "").strip()
        if episode_id:
            episode = self.conn.execute(
                "SELECT decision_id,context_snapshot_id,experiment_id FROM growth_decision_episode WHERE episode_id=?",
                (episode_id,),
            ).fetchone()
            if not episode:
                raise GrowthNotFound("episode_not_found")
            if str(episode["decision_id"]) != str(request.get("decision_id") or ""):
                raise GrowthStateConflict("episode_decision_mismatch")
            if str(episode["experiment_id"] or "") not in {"", experiment_id}:
                raise GrowthStateConflict("episode_experiment_mismatch")
            with self.conn:
                self.conn.execute(
                    "UPDATE growth_decision_episode SET experiment_id=?,updated_at=? WHERE episode_id=? AND experiment_id IN ('',?)",
                    (experiment_id, utc_now(), episode_id, experiment_id),
                )
                self.conn.execute(
                    """INSERT OR IGNORE INTO experiment_context_snapshots
                    (experiment_id,context_snapshot_id,relation_type,created_at) VALUES (?,?,'INITIAL',?)""",
                    (experiment_id, episode["context_snapshot_id"], utc_now()),
                )
        action = ExecutionTaskService(self.conn).create_operation_action(
            decision_id=str(request.get("decision_id") or ""),
            episode_id=episode_id,
            action_type=action_type,
            action_scope="EXPERIMENT",
            target_type=target_type,
            target_id=target_id or experiment_id,
            payload={
                "experiment_id": experiment_id,
                "experiment_ids": list(plan.get("experiment_ids") or []),
                "launch_id": str(plan.get("launch_id") or ""),
                "plan": plan,
                "plan_request_hash": request_digest,
            },
            created_by=actor,
            idempotency_key=f"plan-action:{idempotency_key}",
        )
        approval = OperationApprovalService(self.conn).propose(
            action["operation_action_id"], plan, proposed_by=actor,
            idempotency_key=f"plan-approval:{idempotency_key}", expires_at=str(plan["expires_at"]),
        )
        desired = "WAITING_CREATE_APPROVAL" if action_type == "CREATE_PAUSED_AD" else "WAITING_ADJUSTMENT_APPROVAL"
        if desired in EXPERIMENT_TRANSITIONS.get(experiment["state"], set()):
            self.transition(experiment_id, desired, actor=actor, reason=action_type, event_type="PLAN_PROPOSED", evidence={"plan_id": action["operation_action_id"]})
        result = {
            "plan_id": action["operation_action_id"], "operation_action": action,
            "approval": approval, "plan": plan, "plan_hash": payload_hash(plan),
        }
        with self.conn:
            self.conn.execute(
                """INSERT INTO growth_idempotency_record
                (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
                VALUES ('ad_experiment.plan_preview',?,?,201,?,?)""",
                (idempotency_key, request_digest, canonical_json(result), utc_now()),
            )
        return result

    def plan_detail(self, plan_id: str) -> Dict[str, Any]:
        action = ExecutionTaskService(self.conn).get_operation_action(plan_id)
        approval = self.conn.execute(
            "SELECT * FROM growth_operation_approval WHERE operation_action_id=?", (plan_id,),
        ).fetchone()
        return {
            "plan_id": plan_id, "operation_action": action,
            "approval": OperationApprovalService._serialize(approval) if approval else {},
            "plan": dict(action["payload_json"].get("plan") or {}),
        }

    def preview_launch_create_plan(
        self, launch_id: str, request: Dict[str, Any], *, actor: str, idempotency_key: str,
        target_account_id_override: str = "", recovery: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Compile one immutable create plan for every experiment cell in a launch.

        The plan owns one shared Campaign and N isolated ABO Ad Sets. A creative
        experiment changes only creative direction; audience and copy experiments
        freeze one approved image and change exactly one declared variable. Budget,
        placement, optimization and attribution stay invariant in every mode.
        """
        normalized_launch_id = str(launch_id or "").strip()
        rows = self.conn.execute(
            "SELECT * FROM ad_experiment WHERE source_report_id=? ORDER BY created_at,experiment_code",
            (normalized_launch_id,),
        ).fetchall()
        experiments = [self._serialize(row) for row in rows]
        if not 2 <= len(experiments) <= 4:
            raise GrowthValidationError("launch_experiment_count_must_be_between_2_and_4")
        recovery_evidence = dict(recovery or {})
        normalized_override = str(target_account_id_override or "").strip().removeprefix("act_")
        request_digest = payload_hash({"launch_id": normalized_launch_id, "request": request, "target_account_id_override": normalized_override, "recovery": recovery_evidence})
        existing = self.conn.execute(
            "SELECT request_hash,response_json FROM growth_idempotency_record WHERE route_key='new_account_batch.plan_preview' AND idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            if str(existing["request_hash"]) != request_digest:
                raise GrowthStateConflict("idempotency_key_payload_conflict")
            return decode_json(existing["response_json"], {})

        successful_creation = self.conn.execute(
            """SELECT 1
            FROM growth_operation_action action
            JOIN meta_execution_task task
              ON task.operation_action_id=action.operation_action_id
            WHERE action.action_type='CREATE_PAUSED_AD'
              AND action.status='VERIFIED'
              AND task.status='SUCCESS'
              AND (
                action.target_id=?
                OR json_extract(action.payload_json,'$.launch_id')=?
                OR json_extract(action.payload_json,'$.plan.launch_id')=?
              )
            LIMIT 1""",
            (normalized_launch_id, normalized_launch_id, normalized_launch_id),
        ).fetchone()
        if successful_creation:
            raise GrowthStateConflict("launch_already_created")

        claimed = self.conn.execute(
            """SELECT execution_task_id FROM growth_execution_resource_claim
            WHERE resource_type='NEW_ACCOUNT_LAUNCH' AND resource_id=?""",
            (normalized_launch_id,),
        ).fetchone()
        if claimed:
            raise GrowthStateConflict("launch_already_has_live_creation")

        cells_request = list(request.get("cells") or [])
        by_id = {str(item["experiment_id"]): item for item in experiments}
        if {str(item.get("experiment_id") or "") for item in cells_request} != set(by_id):
            raise GrowthValidationError("launch_plan_must_include_every_experiment")
        roles = [str(item.get("role") or "").strip().upper() for item in cells_request]
        if roles.count("BASELINE") != 1 or any(role not in {"BASELINE", "CHALLENGER"} for role in roles):
            raise GrowthValidationError("launch_plan_requires_one_baseline")
        budgets = {round(float(item.get("daily_budget_usd") or 0), 2) for item in cells_request}
        if len(budgets) != 1:
            raise GrowthValidationError("launch_plan_requires_equal_cell_budgets")
        campaign_name = str(request.get("campaign_name") or "").strip()
        if not campaign_name:
            raise GrowthValidationError("meta_object_names_required")
        test_variable = str(request.get("test_variable") or "creative_direction").strip()
        if test_variable not in {"creative_direction", "audience_strategy", "copy_variant"}:
            raise GrowthValidationError("launch_plan_test_variable_invalid")
        randomized_test = test_variable in {"audience_strategy", "copy_variant"}
        if randomized_test and len(cells_request) != 2:
            raise GrowthValidationError("randomized_experiment_requires_two_cells")
        default_audience_strategy = str(request.get("audience_strategy") or "BROAD").strip().upper()
        if test_variable in {"creative_direction", "copy_variant"} and default_audience_strategy != "BROAD":
            raise GrowthValidationError("creative_experiment_audience_must_be_broad")
        frozen_creative_id = str(request.get("frozen_creative_id") or "").strip()
        if randomized_test and not frozen_creative_id:
            raise GrowthValidationError("frozen_creative_required")

        account_ids = {str(item.get("account_id") or "").removeprefix("act_") for item in experiments}
        countries = {str(item.get("country") or "").upper() for item in experiments}
        if len(account_ids) != 1 or len(countries) != 1:
            raise GrowthValidationError("launch_experiments_must_share_account_and_country")
        account_id = next(iter(account_ids))
        country = next(iter(countries))
        if normalized_override:
            source_plan_id = str(recovery_evidence.get("source_plan_id") or "").strip()
            source_task_id = str(recovery_evidence.get("source_execution_task_id") or "").strip()
            source = self.conn.execute("""SELECT a.payload_json,t.status,t.current_step,t.error_message FROM growth_operation_action a JOIN meta_execution_task t ON t.operation_action_id=a.operation_action_id WHERE a.operation_action_id=? AND t.execution_task_id=?""", (source_plan_id, source_task_id)).fetchone()
            source_payload = decode_json(source["payload_json"], {}) if source else {}
            source_plan = dict(source_payload.get("plan") or {})
            if not (actor == "growth-autopilot-recovery" and source and str(source["status"] or "").upper() == "MANUAL_REVIEW" and str(source["current_step"] or "").upper() == "C1_AD_CREATE" and "meta_graph_error:100:1815645" in str(source["error_message"] or "") and str(source_plan.get("launch_id") or "") == normalized_launch_id and str(source_plan.get("target_account_id") or "").removeprefix("act_") == account_id and normalized_override != account_id):
                raise GrowthValidationError("account_recovery_evidence_invalid")
            account_id = normalized_override
        if test_variable == "audience_strategy":
            requested_strategy_keys = [str(item.get("audience_strategy") or "").strip().upper() for item in cells_request]
            if set(requested_strategy_keys) not in ({"BROAD", "DIGITAL_SELLER"}, {"BROAD", "FAMILY_HOME"}):
                raise GrowthValidationError("audience_experiment_pair_not_allowed")
            baseline_index = roles.index("BASELINE")
            if requested_strategy_keys[baseline_index] != "BROAD":
                raise GrowthValidationError("audience_experiment_baseline_must_be_broad")
        elif test_variable == "copy_variant":
            requested_strategy_keys = ["BROAD" for _ in cells_request]
        else:
            requested_strategy_keys = [default_audience_strategy for _ in cells_request]
        cpi_targets = {
            float(dict(item.get("hypothesis_json") or {}).get("cpi_target") or 0)
            for item in experiments
        }
        if len(cpi_targets) != 1 or next(iter(cpi_targets)) <= 0:
            raise GrowthValidationError("launch_cpi_cost_cap_required")
        cost_cap_usd = next(iter(cpi_targets))
        bid_amount = _cost_cap_bid_amount(cost_cap_usd)
        guardrails = {
            canonical_json(dict(dict(item.get("stop_rule_json") or {}).get("delivery_guardrails") or {}))
            for item in experiments
        }
        expected_guardrails = new_account_delivery_guardrails(cost_cap_usd)
        if guardrails != {canonical_json(expected_guardrails)}:
            raise GrowthValidationError("launch_delivery_guardrails_required")
        application_id = str(os.getenv("GROWTH_META_TUGAO_APPLICATION_ID") or "1684703062404662").strip()
        store_url = str(
            os.getenv("GROWTH_META_TUGAO_STORE_URL")
            or "http://play.google.com/store/apps/details?id=com.timetrade.duitan"
        ).strip()

        compiled_cells: List[Dict[str, Any]] = []
        baseline_experiment_id = ""
        frozen_image = self._approved_image_by_id(frozen_creative_id) if randomized_test else None
        frozen_copy_signature = ""
        frozen_copy_direction = ""
        copy_signatures = set()
        for index, requested in enumerate(cells_request, start=1):
            experiment_id = str(requested.get("experiment_id") or "")
            experiment = by_id[experiment_id]
            hypothesis = dict(experiment.get("hypothesis_json") or {})
            direction = dict(hypothesis.get("creative_direction") or {})
            role = str(requested.get("role") or "").strip().upper()
            if role == "BASELINE":
                baseline_experiment_id = experiment_id
            strategy_key = requested_strategy_keys[index - 1]
            strategy = audience_strategy(strategy_key)
            if test_variable == "audience_strategy":
                hypothesis_strategy = str(dict(hypothesis.get("audience_strategy") or {}).get("strategy_key") or "").strip().upper()
                hypothesis_creative_id = str(dict(hypothesis.get("frozen_creative") or {}).get("image_id") or "").strip()
                if hypothesis_strategy != strategy_key:
                    raise GrowthValidationError("audience_experiment_strategy_identity_mismatch")
                if hypothesis_creative_id != frozen_creative_id:
                    raise GrowthValidationError("audience_experiment_frozen_creative_identity_mismatch")
            if test_variable == "copy_variant":
                hypothesis_creative_id = str(dict(hypothesis.get("frozen_creative") or {}).get("image_id") or "").strip()
                if hypothesis_creative_id != frozen_creative_id:
                    raise GrowthValidationError("copy_experiment_frozen_creative_identity_mismatch")
                direction_signature = canonical_json(direction)
                if frozen_copy_direction and direction_signature != frozen_copy_direction:
                    raise GrowthValidationError("copy_experiment_creative_direction_must_be_identical")
                frozen_copy_direction = direction_signature
            targeting = strict_meta_targeting(country, strategy_key)
            assert_strict_targeting(targeting, country, strategy_key)
            image = frozen_image or self._approved_launch_image(normalized_launch_id, experiment_id)
            page_id = str(hypothesis.get("page_id") or "").strip()
            if not page_id:
                raise GrowthValidationError("meta_page_id_required")
            adset_name = str(requested.get("adset_name") or "").strip()
            ad_name = str(requested.get("ad_name") or "").strip()
            primary_text = str(requested.get("primary_text") or "").strip()
            headline = str(requested.get("headline") or "").strip()
            if not all((adset_name, ad_name, primary_text, headline)):
                raise GrowthValidationError("meta_object_names_and_copy_required")
            copy_signature = canonical_json({
                "primary_text": primary_text, "headline": headline,
                "description": str(requested.get("description") or "").strip(),
                "call_to_action": str(requested.get("call_to_action") or "INSTALL_MOBILE_APP"),
            })
            benchmark_version = str(requested.get("copy_benchmark_version") or "").strip()
            copy_hypothesis = str(requested.get("copy_hypothesis") or "").strip()
            direction_key = str(direction.get("key") or direction.get("direction_id") or "unknown").strip().lower()
            compiled_copy_version_id = copy_version_id(
                country, direction_key, primary_text, headline,
                str(requested.get("description") or "").strip(),
            )
            if test_variable == "audience_strategy":
                if frozen_copy_signature and copy_signature != frozen_copy_signature:
                    raise GrowthValidationError("audience_experiment_copy_must_be_identical")
                frozen_copy_signature = copy_signature
            copy_signatures.add(copy_signature)
            image_path = Path(str(image["image_ref"] or "")).expanduser().resolve()
            compiled_cells.append({
                "cell_key": f"C{index}", "experiment_id": experiment_id,
                "experiment_code": str(experiment.get("experiment_code") or ""),
                "role": role, "creative_direction": direction,
                "audience_strategy": strategy,
                "allocation_percent": 50 if randomized_test else 0,
                "study_cell_name": f"{campaign_name}_{'COPY' if test_variable == 'copy_variant' else strategy_key}_C{index}",
                "frozen_creative_id": str(image["image_id"]),
                "copy_version_id": compiled_copy_version_id,
                "copy_benchmark_version": benchmark_version,
                "copy_hypothesis": copy_hypothesis,
                "steps": {
                    "IMAGE_UPLOAD": {"image_id": str(image["image_id"]), "image_path": str(image_path)},
                    "CREATIVE_CREATE": {
                        "name": f"{ad_name}_CR",
                        "object_story_spec": {"page_id": page_id, "link_data": {
                            "link": store_url, "message": primary_text, "name": headline,
                            "description": str(requested.get("description") or "").strip(),
                            "call_to_action": {"type": str(requested.get("call_to_action") or "INSTALL_MOBILE_APP"), "value": {"link": store_url}},
                        }},
                    },
                    "ADSET_CREATE": {
                        "name": adset_name,
                        "daily_budget": int(round(float(requested["daily_budget_usd"]) * 100)),
                        "optimization_goal": "APP_INSTALLS", "billing_event": "IMPRESSIONS",
                        "bid_strategy": "COST_CAP", "bid_amount": bid_amount,
                        "targeting": targeting,
                        "promoted_object": {"application_id": application_id, "object_store_url": store_url},
                        "attribution_spec": [
                            {"event_type": "CLICK_THROUGH", "window_days": 1},
                            {"event_type": "VIEW_THROUGH", "window_days": 1},
                            {"event_type": "ENGAGED_VIDEO_VIEW", "window_days": 1},
                        ],
                        "status": "PAUSED",
                    },
                    "AD_CREATE": {"name": ad_name, "status": "PAUSED"},
                },
                "asset_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
            })

        if test_variable == "copy_variant" and len(copy_signatures) != 2:
            raise GrowthValidationError("copy_experiment_requires_two_distinct_versions")

        baseline = by_id[baseline_experiment_id]
        decision = self.conn.execute(
            """
            SELECT d.decision_id,e.episode_id FROM growth_decision d
            LEFT JOIN growth_decision_episode e ON e.decision_id=d.decision_id
            WHERE d.target_type='EXPERIMENT' AND d.target_id=?
            ORDER BY d.created_at DESC,e.created_at DESC LIMIT 1
            """,
            (baseline_experiment_id,),
        ).fetchone()
        if not decision:
            raise GrowthValidationError("launch_baseline_decision_required")
        expires_at = ""
        audience_preflight = {}
        business_id = ""
        audience_live_ready = False
        audience_blocked_reason = ""
        if randomized_test:
            preflight_id = str(request.get("audience_preflight_id") or "").strip()
            row = self.conn.execute(
                "SELECT * FROM ad_audience_preflight WHERE preflight_id=? AND launch_id=?",
                (preflight_id, normalized_launch_id),
            ).fetchone() if preflight_id else None
            if row:
                audience_preflight = decode_json(row["evidence_json"], {})
                if payload_hash(audience_preflight) != str(row["evidence_hash"] or ""):
                    audience_preflight = {}
            business_id = str(audience_preflight.get("business_id") or "")
            checked_at = str(audience_preflight.get("checked_at") or "").strip()
            expires_at_value = str(audience_preflight.get("expires_at") or "").strip()
            try:
                checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=timezone.utc)
                preflight_expires = datetime.fromisoformat(expires_at_value.replace("Z", "+00:00"))
                if preflight_expires.tzinfo is None:
                    preflight_expires = preflight_expires.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                fresh = checked >= now - timedelta(hours=1) and preflight_expires > now
            except ValueError:
                fresh = False
            expected_delivery_keys = {"C1", "C2"} if test_variable == "copy_variant" else set(requested_strategy_keys)
            audience_live_ready = bool(
                business_id
                and str(audience_preflight.get("status") or "").upper() == "VERIFIED"
                and str(audience_preflight.get("source") or "") == "meta_graph_read_only"
                and fresh
                and str(audience_preflight.get("launch_id") or "") == normalized_launch_id
                and str(audience_preflight.get("account_id") or "").removeprefix("act_") == account_id.removeprefix("act_")
                and set(audience_preflight.get("strategy_keys") or []) == set(requested_strategy_keys)
                and str(audience_preflight.get("country") or "").upper() == country
                and set(dict(audience_preflight.get("delivery_estimates") or {})) == expected_delivery_keys
                and audience_preflight.get("overlap_ratio") is not None
                and str(audience_preflight.get("start_time") or "").strip()
                and str(audience_preflight.get("end_time") or "").strip()
            )
            if not business_id:
                audience_blocked_reason = "server_owned_audience_preflight_required"
            elif not audience_live_ready:
                audience_blocked_reason = f"{test_variable}_randomization_preflight_required"
        plan_versions = {
            "creative_direction": "NEW_ACCOUNT_BATCH_V1",
            "audience_strategy": "NEW_ACCOUNT_AUDIENCE_BATCH_V1",
            "copy_variant": "NEW_ACCOUNT_COPY_BATCH_V1",
        }
        plan = {
            "plan_id": new_id("plan"),
            "plan_version": plan_versions[test_variable],
            "launch_id": normalized_launch_id, "experiment_id": baseline_experiment_id,
            "experiment_ids": [str(item["experiment_id"]) for item in experiments],
            "experiment_type": "COPY_ONLY" if test_variable == "copy_variant" else "",
            "unique_variable": "PRIMARY_TEXT" if test_variable == "copy_variant" else "",
            "action_type": "CREATE_PAUSED_AD", "target_account_id": account_id,
            "target_object_type": "LAUNCH", "target_object_id": normalized_launch_id,
            "campaign": {"name": campaign_name, "objective": "OUTCOME_APP_PROMOTION", "buying_type": "AUCTION", "special_ad_categories": [], "status": "PAUSED"},
            "cells": compiled_cells, "baseline_experiment_id": baseline_experiment_id,
            "test_variable": test_variable,
            "sdk_contract_version": META_SDK_CONTRACT_VERSION if test_variable == "copy_variant" else "",
            "copy_benchmark_versions": sorted({
                str(cell.get("copy_benchmark_version") or "")
                for cell in compiled_cells if str(cell.get("copy_benchmark_version") or "")
            }),
            "frozen_creative_id": frozen_creative_id if randomized_test else "",
            "study": ({
                "business_id": business_id,
                "name": f"{campaign_name}_{'CV' if test_variable == 'copy_variant' else 'AS'}",
                "type": "SPLIT_TEST",
                "start_time": str(audience_preflight.get("start_time") or ""),
                "end_time": str(audience_preflight.get("end_time") or ""),
            } if randomized_test else {}),
            "audience_preflight": audience_preflight if randomized_test else {},
            "invariants": {
                **({
                    "base_conditions": audience_contract(country, "BROAD")["base_conditions"],
                    "audience_strategies": [audience_strategy(key) for key in requested_strategy_keys],
                    "advantage_audience": "DISABLED", "gender_as_suggestion": False,
                    "age_as_suggestion": False, "frozen_creative_id": frozen_creative_id,
                    "single_variable": "audience_strategy", "randomization": "META_SPLIT_TEST_REQUIRED",
                } if test_variable == "audience_strategy" else ({
                    **audience_contract(country, "BROAD"),
                    "frozen_creative_id": frozen_creative_id,
                    "single_variable": "copy_variant", "randomization": "META_SPLIT_TEST_REQUIRED",
                    "copy_versions": [cell["copy_version_id"] for cell in compiled_cells],
                } if test_variable == "copy_variant" else audience_contract(country, "BROAD"))),
                "budget_mode": "ABO", "equal_daily_budget_usd": next(iter(budgets)),
                "bid_strategy": "COST_CAP", "cost_cap_usd": cost_cap_usd,
                "optimization_goal": "APP_INSTALLS", "placement": "ADVANTAGE_PLUS",
                "attribution": "1d_click_1d_view_1d_engaged_video_view",
            },
            "delivery_guardrails": expected_guardrails,
            "max_write_requests": ((4 + 2 * len(compiled_cells)) if test_variable == "audience_strategy" else (2 + 4 * len(compiled_cells) if test_variable == "copy_variant" else 1 + 4 * len(compiled_cells))),
            "execution_policy": {
                "live_creation_allowed": not randomized_test or audience_live_ready,
                "blocked_reason": audience_blocked_reason,
                "required_readback": ["study_id", "cell_ids", "adset_ids", "ads", "strict_targeting"],
            },
            "evaluation_window": dict(request.get("evaluation_window") or {"checkpoints": ["D1", "D3", "D5"]}),
            "market_profile": ({"country": "CO", "creative_currency": "COP", "reporting_timezone": "America/Bogota", "target_app": "Tugao", "creation_status": "PAUSED"} if country == "CO" else {}),
            "expires_at": expires_at,
        }
        if recovery_evidence:
            plan["recovery"] = {**recovery_evidence, "source_account_id": next(iter(account_ids)), "target_account_id": account_id, "strategy": "NEW_IMMUTABLE_PLAN"}
        if test_variable == "copy_variant":
            if recovery_evidence:
                raise GrowthValidationError("gle_primary_text_only_recovery_plan_not_allowed")
            if not expires_at:
                plan["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            plan = attach_compiler_receipt(plan)
            assert_phase1_live_permission(plan, conn=self.conn)
            expires_at = str(plan["expires_at"])
        action = ExecutionTaskService(self.conn).create_operation_action(
            decision_id=str(decision["decision_id"]), episode_id=str(decision["episode_id"] or ""),
            action_type="CREATE_PAUSED_AD", action_scope="EXPERIMENT",
            target_type="LAUNCH", target_id=normalized_launch_id,
            payload={"experiment_id": baseline_experiment_id, "experiment_ids": plan["experiment_ids"], "launch_id": normalized_launch_id, "plan": plan, "plan_request_hash": request_digest},
            created_by=actor, idempotency_key=f"batch-plan-action:{idempotency_key}",
        )
        approval = OperationApprovalService(self.conn).propose(
            action["operation_action_id"], plan, proposed_by=actor,
            idempotency_key=f"batch-plan-approval:{idempotency_key}", expires_at=expires_at,
        )
        control = {
            "baseline_experiment_id": baseline_experiment_id,
            "test_variable": test_variable, "invariants": plan["invariants"],
            "baseline_source": "operator_confirmed",
        }
        with self.conn:
            for experiment in experiments:
                experiment_id = str(experiment["experiment_id"])
                self.conn.execute(
                    "UPDATE ad_experiment SET control_definition_json=?,updated_at=? WHERE experiment_id=?",
                    (canonical_json({**control, "role": "BASELINE" if experiment_id == baseline_experiment_id else "CHALLENGER"}), utc_now(), experiment_id),
                )
                current = self.get(experiment_id)
                if "WAITING_CREATE_APPROVAL" in EXPERIMENT_TRANSITIONS.get(str(current["state"]), set()):
                    self.transition(experiment_id, "WAITING_CREATE_APPROVAL", actor=actor, reason="LAUNCH_BATCH_CREATE", event_type="BATCH_PLAN_PROPOSED", evidence={"plan_id": action["operation_action_id"], "launch_id": normalized_launch_id})
            result = {"plan_id": action["operation_action_id"], "operation_action": action, "approval": approval, "plan": plan, "plan_hash": payload_hash(plan)}
            self.conn.execute(
                """INSERT INTO growth_idempotency_record
                (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
                VALUES ('new_account_batch.plan_preview',?,?,201,?,?)""",
                (idempotency_key, request_digest, canonical_json(result), utc_now()),
            )
        return result

    def _approved_launch_image(self, launch_id: str, experiment_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            """
            SELECT i.* FROM creative_pro_work_queue q
            JOIN creative_generated_images i
              ON i.request_id=json_extract(q.generation_plan_json,'$.generation_request_id')
            JOIN creative_review_records r ON r.image_id=i.image_id
            WHERE json_extract(q.material_refs_json,'$.launch_id')=?
              AND json_extract(q.material_refs_json,'$.growth_experiment_id')=?
              AND lower(q.status)='completed' AND upper(r.review_status)='APPROVED'
              AND lower(i.review_status)='approved'
            ORDER BY r.created_at DESC,i.created_at DESC LIMIT 1
            """,
            (launch_id, experiment_id),
        ).fetchone()
        if not row:
            raise GrowthValidationError("approved_creative_image_required_for_every_cell")
        image_path = Path(str(row["image_ref"] or "")).expanduser().resolve()
        if not image_path.is_file():
            raise GrowthValidationError("approved_creative_file_missing")
        return row

    def _approved_image_by_id(self, image_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            """
            SELECT i.* FROM creative_generated_images i
            WHERE i.image_id=? AND lower(i.review_status) IN ('approved','used_in_ad')
              AND EXISTS (
                  SELECT 1 FROM creative_review_records r
                  WHERE r.image_id=i.image_id AND upper(r.review_status)='APPROVED'
              )
            LIMIT 1
            """,
            (str(image_id or "").strip(),),
        ).fetchone()
        if not row:
            raise GrowthValidationError("approved_frozen_creative_required")
        image_path = Path(str(row["image_ref"] or "")).expanduser().resolve()
        if not image_path.is_file():
            raise GrowthValidationError("approved_creative_file_missing")
        return row

    def _resolve_create_plan_request(
        self, experiment: Dict[str, Any], request: Dict[str, Any], action_type: str,
    ) -> Dict[str, Any]:
        resolved = copy.deepcopy(request)
        if action_type != "CREATE_PAUSED_AD":
            return resolved
        if str(experiment.get("target_app") or "").strip().lower() != "tugao":
            raise GrowthValidationError("target_app_must_be_tugao")
        steps = dict(resolved.get("steps") or {})
        image_step = dict(steps.get("IMAGE_UPLOAD") or {})
        verified_reuse_hash = str(image_step.get("reuse_image_hash") or "").strip()
        verified_source = str(dict(resolved.get("preflight_snapshot_json") or {}).get("source") or "") == "meta_graph_read_only"
        image_id = str(image_step.get("image_id") or "").strip()
        current_creative = self.latest_approved_creative(str(experiment["experiment_id"]))
        if verified_reuse_hash and verified_source:
            image_step = {"reuse_image_hash": verified_reuse_hash, "source": "verified_meta_source_creative"}
            resolved["asset_sha256"] = ""
        elif self._creative_linkage_tables_available():
            if not current_creative:
                raise GrowthValidationError("approved_creative_image_required_for_current_experiment")
            image_id = str(current_creative["image_id"])
            image_step["image_id"] = image_id
            resolved["asset_sha256"] = str(current_creative.get("image_hash") or "")
        elif not image_id:
            raise GrowthValidationError("approved_creative_image_required")
        image_path = Path()
        if not verified_reuse_hash:
            review = self.conn.execute(
                """
                SELECT review_status FROM creative_review_records
                WHERE image_id=? ORDER BY created_at DESC LIMIT 1
                """,
                (image_id,),
            ).fetchone()
            if not review or str(review["review_status"] or "").upper() != "APPROVED":
                raise GrowthValidationError("approved_creative_image_required")
            image = self.conn.execute(
                "SELECT image_ref FROM creative_generated_images WHERE image_id=?",
                (image_id,),
            ).fetchone()
            image_path = Path(str(image["image_ref"] or "")).expanduser().resolve() if image else Path()
            if not image or not image_path.is_file():
                raise GrowthValidationError("approved_creative_file_missing")
            image_step["image_path"] = str(image_path)
        steps["IMAGE_UPLOAD"] = image_step

        after = dict(resolved.get("after_json") or {})
        campaign = dict(after.get("campaign") or {})
        adset = dict(after.get("adset") or {})
        ad = dict(after.get("ad") or {})
        creative = dict(after.get("creative") or {})
        reuse_campaign_id = str(after.get("reuse_campaign_id") or "").strip()
        initial_status = str(after.get("initial_status") or after.get("status") or "PAUSED").upper()
        if initial_status not in {"PAUSED", "ACTIVE"}:
            raise GrowthValidationError("invalid_create_initial_status")
        if image_id:
            creative["image_id"] = image_id
        if verified_reuse_hash:
            creative["source_image_hash"] = verified_reuse_hash
        after["creative"] = creative
        resolved["after_json"] = after
        creative_step = dict(steps.get("CREATIVE_CREATE") or {})
        if creative_step:
            creative_step["image_id"] = image_id
            steps["CREATIVE_CREATE"] = creative_step
        resolved["steps"] = steps
        hypothesis = dict(experiment.get("hypothesis_json") or {})
        cost_cap_usd = float(hypothesis.get("cpi_target") or adset.get("cost_cap_usd") or 0)
        bid_amount = _cost_cap_bid_amount(cost_cap_usd)
        audience = dict(hypothesis.get("audience") or {})
        country = str(experiment.get("country") or audience.get("country") or "").upper()
        targeting = dict(adset.get("targeting") or {}) if verified_reuse_hash and verified_source else strict_meta_targeting(country, "BROAD")
        assert_strict_targeting(targeting, country)

        application_id = str(os.getenv("GROWTH_META_TUGAO_APPLICATION_ID") or "1684703062404662").strip()
        store_url = str(
            os.getenv("GROWTH_META_TUGAO_STORE_URL")
            or "http://play.google.com/store/apps/details?id=com.timetrade.duitan"
        ).strip()
        page_id = str(creative.get("page_id") or hypothesis.get("page_id") or "").strip()
        if not page_id:
            raise GrowthValidationError("meta_page_id_required")
        budget_mode = str(after.get("budget_mode") or "ABO").strip().upper()
        if budget_mode not in {"ABO", "CBO"}:
            raise GrowthValidationError("rebuild_budget_mode_invalid")
        if budget_mode == "CBO":
            if not reuse_campaign_id:
                raise GrowthValidationError("cbo_rebuild_requires_reused_campaign")
            daily_budget_usd = float(campaign.get("daily_budget_usd") or 0)
            if daily_budget_usd < 5 or daily_budget_usd > 100:
                raise GrowthValidationError("campaign_daily_budget_out_of_range")
        else:
            daily_budget_usd = float(adset.get("daily_budget_usd") or adset.get("daily_budget") or 0)
            if daily_budget_usd < 5 or daily_budget_usd > 100:
                raise GrowthValidationError("adset_daily_budget_out_of_range")

        if reuse_campaign_id:
            steps.pop("CAMPAIGN_CREATE", None)
        else:
            steps["CAMPAIGN_CREATE"] = {
                "name": str(campaign.get("name") or "").strip(),
                "objective": "OUTCOME_APP_PROMOTION",
                "buying_type": "AUCTION",
                "special_ad_categories": [],
                "status": "PAUSED",
            }
        steps["ADSET_CREATE"] = {
            "name": str(adset.get("name") or "").strip(),
            "optimization_goal": "APP_INSTALLS",
            "billing_event": "IMPRESSIONS",
            "bid_strategy": "COST_CAP",
            "bid_amount": bid_amount,
            "targeting": targeting,
            "promoted_object": {
                "application_id": application_id,
                "object_store_url": store_url,
            },
            "attribution_spec": [
                {"event_type": "CLICK_THROUGH", "window_days": 1},
                {"event_type": "VIEW_THROUGH", "window_days": 1},
                {"event_type": "ENGAGED_VIDEO_VIEW", "window_days": 1},
            ],
            "status": initial_status,
        }
        if budget_mode == "ABO":
            steps["ADSET_CREATE"]["daily_budget"] = int(round(daily_budget_usd * 100))
        regional_identities = dict(adset.get("regional_regulation_identities") or {})
        if regional_identities:
            steps["ADSET_CREATE"]["regional_regulation_identities"] = regional_identities
        link_data = {
            "link": store_url,
            "message": str(ad.get("primary_text") or "").strip(),
            "name": str(ad.get("headline") or "").strip(),
            "description": str(ad.get("description") or "").strip(),
            "call_to_action": {
                "type": str(ad.get("call_to_action") or "INSTALL_MOBILE_APP"),
                "value": {"link": store_url},
            },
        }
        steps["CREATIVE_CREATE"] = {
            "name": str(dict(steps.get("CREATIVE_CREATE") or {}).get("name") or f"{ad.get('name')}_CR"),
            "object_story_spec": {"page_id": page_id, "link_data": link_data},
        }
        if image_id:
            steps["CREATIVE_CREATE"]["image_id"] = image_id
        steps["AD_CREATE"] = {"name": str(ad.get("name") or "").strip(), "status": initial_status}
        named_steps = ("ADSET_CREATE", "CREATIVE_CREATE", "AD_CREATE") if reuse_campaign_id else ("CAMPAIGN_CREATE", "ADSET_CREATE", "CREATIVE_CREATE", "AD_CREATE")
        if any(not str(dict(steps.get(name) or {}).get("name") or "").strip() for name in named_steps):
            raise GrowthValidationError("meta_object_names_required")
        resolved["steps"] = steps
        resolved["asset_sha256"] = "" if verified_reuse_hash else hashlib.sha256(image_path.read_bytes()).hexdigest()
        resolved["max_write_requests"] = 4 if reuse_campaign_id else 5
        return resolved

    def _creative_linkage_tables_available(self) -> bool:
        rows = self.conn.execute(
            """SELECT name FROM sqlite_master WHERE type='table' AND name IN
            ('creative_pro_work_queue','creative_generated_images','creative_review_records')"""
        ).fetchall()
        return len(rows) == 3

    @staticmethod
    def _compile_plan(experiment: Dict[str, Any], request: Dict[str, Any], action_type: str, target_type: str, target_id: str) -> Dict[str, Any]:
        before = dict(request.get("before_json") or {})
        after = dict(request.get("after_json") or {})
        steps = dict(request.get("steps") or {})
        if action_type in {"INCREASE_BUDGET", "DECREASE_BUDGET"}:
            before_budget = before.get("budget")
            after_budget = after.get("budget")
            if before_budget in (None, "") or after_budget in (None, ""):
                raise GrowthValidationError("budget_before_and_after_required")
            before_value, after_value = float(before_budget), float(after_budget)
            if before_value <= 0 or after_value <= 0:
                raise GrowthValidationError("budget_must_be_positive")
            if action_type == "INCREASE_BUDGET" and after_value <= before_value:
                raise GrowthValidationError("budget_direction_mismatch")
            if action_type == "DECREASE_BUDGET" and after_value >= before_value:
                raise GrowthValidationError("budget_direction_mismatch")
        if action_type == "PAUSE_AD":
            target_type = "AD"
            target_id = str(experiment.get("source_ad_id") or "").strip()
            if not target_id:
                raise GrowthValidationError("source_ad_id_required_for_pause")
            before = {"status": "ACTIVE"}
            after = {"status": "PAUSED"}
            steps = {
                "STATUS_UPDATE": {
                    "target_id": target_id,
                    "object_key": "ad_id",
                    "before_status": "ACTIVE",
                    "status": "PAUSED",
                },
            }
        elif action_type == "SET_COST_CAP":
            target_type = "ADSET"
            target_id = str(experiment.get("source_adset_id") or "").strip()
            if not target_id:
                raise GrowthValidationError("source_adset_id_required_for_cost_cap")
            hypothesis = dict(experiment.get("hypothesis_json") or {})
            bid_amount = _cost_cap_bid_amount(hypothesis.get("cpi_target"))
            before = {
                "bid_strategy": str(before.get("bid_strategy") or "LOWEST_COST_WITHOUT_CAP").upper(),
                "bid_amount": before.get("bid_amount"),
            }
            after = {"bid_strategy": "COST_CAP", "bid_amount": bid_amount}
            steps = {
                "BID_STRATEGY_UPDATE": {
                    "target_id": target_id,
                    "object_key": "adset_id",
                    "bid_strategy": "COST_CAP",
                    "bid_amount": bid_amount,
                },
            }
        elif action_type == "PAUSE_ADSET":
            after["status"] = "PAUSED"
        if action_type == "REACTIVATE_AD":
            delivery_paths = [dict(item or {}) for item in list(request.get("delivery_paths") or [])]
            if delivery_paths:
                if not 2 <= len(delivery_paths) <= 4:
                    raise GrowthValidationError("launch_delivery_path_count_invalid")
                campaign_ids = {str(item.get("campaign_id") or "").strip() for item in delivery_paths}
                experiment_ids = [str(item.get("experiment_id") or "").strip() for item in delivery_paths]
                if len(campaign_ids) != 1 or "" in campaign_ids or any(
                    not str(item.get("adset_id") or "").strip() or not str(item.get("ad_id") or "").strip()
                    for item in delivery_paths
                ) or any(not item for item in experiment_ids) or len(set(experiment_ids)) != len(experiment_ids):
                    raise GrowthValidationError("launch_delivery_path_object_ids_required")
                campaign_id = next(iter(campaign_ids))
                campaign_statuses = {
                    str(item.get("campaign_status") or "").strip().upper()
                    for item in delivery_paths
                }
                if len(campaign_statuses) != 1 or not campaign_statuses.issubset({"ACTIVE", "PAUSED"}):
                    raise GrowthValidationError("launch_campaign_status_readback_required")
                target_type = "LAUNCH"
                target_id = str(request.get("launch_id") or "").strip()
                if not target_id:
                    raise GrowthValidationError("launch_id_required_for_delivery")
                before = {"status": "PAUSED"}
                after = {"status": "ACTIVE"}
                steps = {
                    "CAMPAIGN_STATUS_UPDATE": {
                        "target_id": campaign_id, "object_key": "campaign_id",
                        "before_status": next(iter(campaign_statuses)), "status": "ACTIVE",
                    },
                }
                request["compiled_delivery_cells"] = [
                    {
                        "cell_key": f"C{index}", "experiment_id": item["experiment_id"],
                        "steps": {
                            "ADSET_STATUS_UPDATE": {
                                "target_id": str(item["adset_id"]), "object_key": f"c{index}_adset_id",
                                "before_status": str(item.get("adset_status") or "").strip().upper(), "status": "ACTIVE",
                            },
                            "AD_STATUS_UPDATE": {
                                "target_id": str(item["ad_id"]), "object_key": f"c{index}_ad_id",
                                "before_status": str(item.get("ad_status") or "").strip().upper(), "status": "ACTIVE",
                            },
                        },
                    }
                    for index, item in enumerate(delivery_paths, start=1)
                ]
                request["compiled_experiment_ids"] = experiment_ids
                if any(
                    str(step.get("before_status") or "") not in {"ACTIVE", "PAUSED"}
                    for cell in request["compiled_delivery_cells"]
                    for step in dict(cell.get("steps") or {}).values()
                ):
                    raise GrowthValidationError("launch_delivery_status_readback_required")
            else:
                object_ids = {
                    "campaign_id": str(experiment.get("source_campaign_id") or "").strip(),
                    "adset_id": str(experiment.get("source_adset_id") or "").strip(),
                    "ad_id": str(experiment.get("source_ad_id") or "").strip(),
                }
                missing = [key for key, value in object_ids.items() if not value]
                if missing:
                    raise GrowthValidationError("delivery_path_object_ids_required")
                target_type = "DELIVERY_PATH"
                target_id = object_ids["ad_id"]
                before = {"status": "PAUSED", "object_statuses": {key: "PAUSED" for key in object_ids}}
                after = {"status": "ACTIVE", "object_statuses": {key: "ACTIVE" for key in object_ids}}
                steps = {
                    "CAMPAIGN_STATUS_UPDATE": {
                        "target_id": object_ids["campaign_id"], "object_key": "campaign_id",
                        "before_status": "PAUSED", "status": "ACTIVE",
                    },
                    "ADSET_STATUS_UPDATE": {
                        "target_id": object_ids["adset_id"], "object_key": "adset_id",
                        "before_status": "PAUSED", "status": "ACTIVE",
                    },
                    "AD_STATUS_UPDATE": {
                        "target_id": object_ids["ad_id"], "object_key": "ad_id",
                        "before_status": "PAUSED", "status": "ACTIVE",
                    },
                }
        required_create = {"ADSET_CREATE", "IMAGE_UPLOAD", "CREATIVE_CREATE", "AD_CREATE"}
        if not str(after.get("reuse_campaign_id") or "").strip():
            required_create.add("CAMPAIGN_CREATE")
        if action_type == "CREATE_PAUSED_AD" and not required_create.issubset(steps):
            raise GrowthValidationError("create_paused_ad_steps_required")
        if action_type == "REPLACE_CREATIVE" and not dict(after.get("creative") or request.get("creative") or {}):
            raise GrowthValidationError("replacement_creative_required")
        compiled_delivery_cells = list(request.get("compiled_delivery_cells") or [])
        max_writes = (
            (1 + 2 * len(compiled_delivery_cells)) if action_type == "REACTIVATE_AD" and compiled_delivery_cells
            else 3 if action_type == "REACTIVATE_AD"
            else int(request.get("max_write_requests") or (5 if action_type == "CREATE_PAUSED_AD" else (2 if action_type == "REPLACE_CREATIVE" else 1)))
        )
        if max_writes < 1 or max_writes > 17:
            raise GrowthValidationError("invalid_max_write_requests")
        expires_at = ""
        plan = {
            "plan_id": new_id("plan"), "experiment_id": experiment["experiment_id"],
            "recommendation_id": str(request.get("recommendation_id") or experiment["source_recommendation_id"]),
            "action_type": action_type, "target_account_id": str(request.get("target_account_id") or experiment["account_id"]).removeprefix("act_"),
            "target_object_type": target_type, "target_object_id": target_id,
            "before_json": before, "after_json": after, "steps": steps,
            "asset_sha256": str(request.get("asset_sha256") or ""),
            "copy_version_id": str(request.get("copy_version_id") or ""),
            "max_write_requests": max_writes,
            "preflight_snapshot_json": dict(request.get("preflight_snapshot_json") or before),
            "reason": str(request.get("reason") or ""),
            "evidence_window": dict(request.get("evidence_window") or {}),
            "expected_effect": dict(request.get("expected_effect") or {}),
            "evaluation_window": dict(request.get("evaluation_window") or {"checkpoints": ["D1", "D3", "D5"]}),
            "expires_at": expires_at,
        }
        if action_type == "CREATE_PAUSED_AD":
            reuse_campaign_id = str(after.get("reuse_campaign_id") or "").strip()
            initial_status = str(after.get("initial_status") or after.get("status") or "PAUSED").upper()
            if initial_status not in {"PAUSED", "ACTIVE"}:
                raise GrowthValidationError("invalid_create_initial_status")
            if reuse_campaign_id:
                plan["reuse_campaign_id"] = reuse_campaign_id
            plan["initial_status"] = initial_status
        if compiled_delivery_cells:
            plan.update({
                "plan_version": "NEW_ACCOUNT_DELIVERY_BATCH_V1",
                "launch_id": str(request.get("launch_id") or ""),
                "experiment_ids": list(request.get("compiled_experiment_ids") or []),
                "cells": compiled_delivery_cells,
            })
        return plan

    def _event(self, experiment_id: str, from_state: str, to_state: str, event_type: str, actor: str, reason: str, evidence: Dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO ad_experiment_events
            (event_id,experiment_id,from_state,to_state,event_type,actor,reason,evidence_json,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (new_id("adevent"), experiment_id, from_state, to_state, event_type, actor, reason, canonical_json(evidence), utc_now()),
        )

    @staticmethod
    def _serialize(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        for key, value in list(result.items()):
            if key.endswith("_json"):
                result[key] = decode_json(value, [] if str(value or "").lstrip().startswith("[") else {})
        hypothesis = dict(result.get("hypothesis_json") or {})
        if hypothesis.get("mode") == "passive_observation":
            result["experiment_name"] = str(hypothesis.get("display_name") or "广告表现观察")
        return result
