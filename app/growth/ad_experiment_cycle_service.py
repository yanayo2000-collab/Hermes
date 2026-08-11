from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from app.growth.ad_experiment_service import AdExperimentService
from app.growth.common import canonical_json, decode_json, new_id, payload_hash, utc_now
from app.growth.delivery_guardrails import new_account_delivery_guardrails
from app.growth.errors import GrowthNotFound, GrowthStateConflict, GrowthValidationError
from app.growth.schema import ensure_growth_schema


REPORTING_TIMEZONE = "Asia/Shanghai"
EVALUATION_CHECKPOINTS = ["D1", "D3", "D7"]
SUPPORTED_CYCLE_ACTIONS = {"PAUSE_AD"}


class AdExperimentCycleService:
    """Opens one evidence-bound evaluation cycle after a verified Meta action.

    The cycle is local orchestration truth.  It never calls Meta, never creates an
    execution task and never makes a causal claim.
    """

    def __init__(self, conn: sqlite3.Connection, *, ensure_schema: bool = True) -> None:
        self.conn = conn
        if ensure_schema:
            ensure_growth_schema(conn)

    def reconcile_verified_action(
        self, operation_action_id: str, *, actor: str,
    ) -> Dict[str, Any]:
        normalized_action_id = str(operation_action_id or "").strip()
        normalized_actor = str(actor or "").strip()
        if not normalized_action_id:
            raise GrowthValidationError("operation_action_id_is_required")
        if not normalized_actor:
            raise GrowthValidationError("cycle_actor_is_required")

        source = self._verified_source(normalized_action_id)
        existing = self.conn.execute(
            "SELECT * FROM ad_experiment_cycle WHERE source_operation_action_id=?",
            (normalized_action_id,),
        ).fetchone()
        if existing:
            result = self._serialize(existing)
            if (
                result["source_plan_hash"] != source["plan_hash"]
                or result["source_receipt_hash"] != source["receipt_hash"]
                or result["evidence_root_hash"] != source["evidence_root_hash"]
                or result["source_execution_task_id"] != source["execution_task_id"]
                or result["evaluation_subject_hash"] != source["evaluation_subject_hash"]
            ):
                raise GrowthStateConflict("closed_loop_cycle_source_drift")
            return result

        cycle_id = new_id("adcycle")
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO ad_experiment_cycle
                (cycle_id,experiment_id,source_operation_action_id,source_execution_task_id,
                 source_receipt_id,source_plan_hash,source_receipt_hash,evidence_root_hash,
                 action_type,target_type,target_id,evaluation_subject_json,
                 evaluation_subject_hash,evaluation_checkpoints_json,
                 window_opened_at,first_complete_date,reporting_timezone,state,
                 latest_checkpoint,latest_evaluation_status,causal_claim,meta_write_allowed,
                 created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'WAITING_EVIDENCE','','',0,0,?,?)
                """,
                (
                    cycle_id, source["experiment_id"], normalized_action_id,
                    source["execution_task_id"], source["receipt_id"], source["plan_hash"],
                    source["receipt_hash"], source["evidence_root_hash"], source["action_type"],
                    source["target_type"], source["target_id"],
                    canonical_json(source["evaluation_subject"]),
                    source["evaluation_subject_hash"],
                    canonical_json(EVALUATION_CHECKPOINTS), source["window_opened_at"],
                    source["first_complete_date"], REPORTING_TIMEZONE, now, now,
                ),
            )
            experiments = AdExperimentService(self.conn)
            experiment = experiments.get(source["experiment_id"])
            if experiment["state"] in {"ADJUSTING", "PAUSED"}:
                experiment = experiments.transition(
                    source["experiment_id"], "EVALUATING_ADJUSTMENT", actor=normalized_actor,
                    reason=f"cycle_opened:{cycle_id}", event_type="ADJUSTMENT_EVALUATION_STARTED",
                    evidence={
                        "cycle_id": cycle_id,
                        "operation_action_id": normalized_action_id,
                        "execution_task_id": source["execution_task_id"],
                        "meta_writes_performed": False,
                    },
                )
            elif experiment["state"] != "EVALUATING_ADJUSTMENT":
                raise GrowthStateConflict(
                    f"closed_loop_cycle_experiment_state_invalid:{experiment['state']}"
                )
            experiments._event(
                source["experiment_id"], experiment["state"], experiment["state"],
                "EVALUATION_WINDOW_OPENED", normalized_actor,
                f"verified_action:{normalized_action_id}",
                {
                    "cycle_id": cycle_id,
                    "operation_action_id": normalized_action_id,
                    "execution_task_id": source["execution_task_id"],
                    "source_receipt_id": source["receipt_id"],
                    "source_receipt_hash": source["receipt_hash"],
                    "window_opened_at": source["window_opened_at"],
                    "first_complete_date": source["first_complete_date"],
                    "reporting_timezone": REPORTING_TIMEZONE,
                    "checkpoints": EVALUATION_CHECKPOINTS,
                    "evaluation_subject_hash": source["evaluation_subject_hash"],
                    "causal_claim": False,
                    "meta_writes_performed": False,
                },
            )
        return self.get(cycle_id)

    def get(self, cycle_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM ad_experiment_cycle WHERE cycle_id=?", (str(cycle_id or "").strip(),),
        ).fetchone()
        if not row:
            raise GrowthNotFound("ad_experiment_cycle_not_found")
        return self._serialize(row)

    def list_for_experiment(self, experiment_id: str) -> Dict[str, Any]:
        AdExperimentService(self.conn).get(str(experiment_id or "").strip())
        rows = self.conn.execute(
            "SELECT * FROM ad_experiment_cycle WHERE experiment_id=? ORDER BY created_at,cycle_id",
            (str(experiment_id or "").strip(),),
        ).fetchall()
        items = [self._serialize(row) for row in rows]
        return {
            "experiment_id": str(experiment_id or "").strip(),
            "items": items,
            "count": len(items),
            "causal_claim": False,
            "meta_writes_performed": False,
        }

    def reconcile_pending(self, *, actor: str, limit: int = 100) -> Dict[str, Any]:
        normalized_actor = str(actor or "").strip()
        if not normalized_actor:
            raise GrowthValidationError("cycle_actor_is_required")
        bounded_limit = max(1, min(int(limit or 100), 500))
        rows = self.conn.execute(
            """
            SELECT a.operation_action_id
            FROM growth_operation_action a
            JOIN meta_execution_task t ON t.operation_action_id=a.operation_action_id
            LEFT JOIN ad_experiment_cycle c
              ON c.source_operation_action_id=a.operation_action_id
            WHERE a.action_type='PAUSE_AD' AND a.status='VERIFIED'
              AND t.status='SUCCESS' AND c.cycle_id IS NULL
            ORDER BY t.finished_at,a.operation_action_id
            LIMIT ?
            """,
            (bounded_limit,),
        ).fetchall()
        opened: List[Dict[str, Any]] = []
        rejected: List[Dict[str, str]] = []
        for row in rows:
            operation_action_id = str(row["operation_action_id"])
            try:
                opened.append(self.reconcile_verified_action(
                    operation_action_id, actor=normalized_actor,
                ))
            except (GrowthNotFound, GrowthStateConflict, GrowthValidationError) as exc:
                rejected.append({
                    "operation_action_id": operation_action_id,
                    "reason": str(exc),
                })
        return {
            "opened": opened,
            "opened_count": len(opened),
            "rejected": rejected,
            "rejected_count": len(rejected),
            "meta_writes_performed": False,
        }

    def _verified_source(self, operation_action_id: str) -> Dict[str, Any]:
        action = self.conn.execute(
            "SELECT * FROM growth_operation_action WHERE operation_action_id=?",
            (operation_action_id,),
        ).fetchone()
        if not action:
            raise GrowthNotFound("operation_action_not_found")
        action = dict(action)
        action_type = str(action.get("action_type") or "").upper()
        if action_type not in SUPPORTED_CYCLE_ACTIONS:
            raise GrowthValidationError("closed_loop_cycle_action_not_supported")
        if str(action.get("action_scope") or "") != "EXPERIMENT":
            raise GrowthStateConflict("closed_loop_cycle_action_scope_invalid")
        if str(action.get("status") or "") != "VERIFIED":
            raise GrowthStateConflict("closed_loop_cycle_action_not_verified")

        action_payload = dict(decode_json(action.get("payload_json"), {}))
        experiment_id = str(action_payload.get("experiment_id") or "").strip()
        raw_experiment_ids = action_payload.get("experiment_ids") or []
        if not isinstance(raw_experiment_ids, list):
            raise GrowthStateConflict("closed_loop_cycle_experiment_binding_invalid")
        experiment_ids = [
            str(item or "").strip() for item in raw_experiment_ids
            if str(item or "").strip()
        ]
        if not experiment_id or (experiment_ids and experiment_ids != [experiment_id]):
            raise GrowthStateConflict("closed_loop_cycle_experiment_binding_invalid")
        experiment = AdExperimentService(self.conn).get(experiment_id)
        target_id = str(action.get("target_id") or "").strip()
        target_type = str(action.get("target_type") or "").strip().upper()
        if (
            target_type != "AD"
            or target_id != str(experiment.get("source_ad_id") or "").strip()
        ):
            raise GrowthStateConflict("closed_loop_cycle_target_binding_invalid")

        approval = self.conn.execute(
            "SELECT * FROM growth_operation_approval WHERE operation_action_id=?",
            (operation_action_id,),
        ).fetchone()
        if not approval:
            raise GrowthStateConflict("closed_loop_cycle_approval_missing")
        approval = dict(approval)
        plan = dict(decode_json(approval.get("plan_json"), {}))
        plan_hash = payload_hash(plan)
        if (
            str(approval.get("status") or "") != "APPROVED"
            or not str(approval.get("approved_at") or "").strip()
            or not str(approval.get("consumed_at") or "").strip()
            or plan_hash != str(approval.get("plan_hash") or "")
            or canonical_json(dict(action_payload.get("plan") or {})) != canonical_json(plan)
        ):
            raise GrowthStateConflict("closed_loop_cycle_plan_not_consumed")
        if (
            str(plan.get("experiment_id") or "") != experiment_id
            or str(plan.get("action_type") or "").upper() != action_type
            or str(plan.get("target_object_type") or "").upper() != target_type
            or str(plan.get("target_object_id") or "") != target_id
            or list(dict(plan.get("evaluation_window") or {}).get("checkpoints") or [])
            != EVALUATION_CHECKPOINTS
        ):
            raise GrowthStateConflict("closed_loop_cycle_plan_binding_invalid")
        if (
            str(dict(plan.get("before_json") or {}).get("status") or "").upper() != "ACTIVE"
            or str(dict(plan.get("after_json") or {}).get("status") or "").upper() != "PAUSED"
        ):
            raise GrowthStateConflict("closed_loop_cycle_pause_contract_invalid")
        status_step = dict(dict(plan.get("steps") or {}).get("STATUS_UPDATE") or {})
        if (
            str(status_step.get("target_id") or "") != target_id
            or str(status_step.get("status") or "").upper() != "PAUSED"
        ):
            raise GrowthStateConflict("closed_loop_cycle_pause_step_invalid")

        task_rows = self.conn.execute(
            "SELECT * FROM meta_execution_task WHERE operation_action_id=?",
            (operation_action_id,),
        ).fetchall()
        if len(task_rows) != 1:
            raise GrowthStateConflict("closed_loop_cycle_execution_task_invalid")
        task = dict(task_rows[0])
        if str(task.get("status") or "") != "SUCCESS" or not str(task.get("finished_at") or ""):
            raise GrowthStateConflict("closed_loop_cycle_execution_not_verified")
        task_ids = dict(decode_json(task.get("meta_object_ids_json"), {}))
        if str(task_ids.get("ad_id") or "") != target_id:
            raise GrowthStateConflict("closed_loop_cycle_task_target_mismatch")

        raw_receipts = self.conn.execute(
            """SELECT * FROM meta_execution_task_receipt
               WHERE execution_task_id=? ORDER BY created_at,receipt_id""",
            (str(task["execution_task_id"]),),
        ).fetchall()
        receipts = [self._receipt(row) for row in raw_receipts]
        by_step: Dict[str, List[Dict[str, Any]]] = {}
        for receipt in receipts:
            by_step.setdefault(str(receipt["step_name"] or "").upper(), []).append(receipt)
        if any(len(by_step.get(step) or []) != 1 for step in ("STATUS_UPDATE", "VERIFY", "RECEIPT")):
            raise GrowthStateConflict("closed_loop_cycle_receipt_chain_invalid")
        status_receipt = by_step["STATUS_UPDATE"][0]
        verify_receipt = by_step["VERIFY"][0]
        final_receipt = by_step["RECEIPT"][0]
        if (
            str(status_receipt["step_status"]).upper() != "SUCCESS"
            or str(verify_receipt["step_status"]).upper() != "VERIFIED"
            or str(final_receipt["step_status"]).upper() != "SUCCESS"
        ):
            raise GrowthStateConflict("closed_loop_cycle_receipt_status_invalid")
        for receipt in (verify_receipt, final_receipt):
            verification = dict(receipt["verification_result_json"] or {})
            if (
                str(verification.get("status") or "").upper() != "SUCCESS"
                or str(dict(verification.get("meta_object_ids") or {}).get("ad_id") or "") != target_id
                or str(dict(verification.get("object_statuses") or {}).get("ad_id") or "").upper()
                != "PAUSED"
            ):
                raise GrowthStateConflict("closed_loop_cycle_readback_invalid")
        if any(str(dict(receipt["meta_object_ids_json"] or {}).get("ad_id") or "") != target_id for receipt in receipts):
            raise GrowthStateConflict("closed_loop_cycle_receipt_target_mismatch")

        approved_at = self._utc(str(approval["approved_at"]), "closed_loop_cycle_approval_time_invalid")
        consumed_at = self._utc(str(approval["consumed_at"]), "closed_loop_cycle_approval_time_invalid")
        finished_at = self._utc(str(task["finished_at"]), "closed_loop_cycle_finished_time_invalid")
        receipt_times = [
            self._utc(str(receipt["created_at"]), "closed_loop_cycle_receipt_time_invalid")
            for receipt in receipts
        ]
        if not (approved_at <= consumed_at <= finished_at) or any(value > finished_at for value in receipt_times):
            raise GrowthStateConflict("closed_loop_cycle_time_order_invalid")

        opened_at = finished_at.astimezone(timezone.utc).isoformat()
        first_complete_date = (
            finished_at.astimezone(ZoneInfo(REPORTING_TIMEZONE)).date() + timedelta(days=1)
        ).isoformat()
        receipt_hash = payload_hash(receipts)
        evaluation_subject = self._evaluation_subject(experiment, target_id)
        evaluation_subject_hash = payload_hash(evaluation_subject)
        evidence_root_hash = payload_hash({
            "schema_version": "gle-ad-experiment-cycle-v1",
            "experiment_id": experiment_id,
            "operation_action_id": operation_action_id,
            "execution_task_id": str(task["execution_task_id"]),
            "source_plan_hash": plan_hash,
            "source_receipt_hash": receipt_hash,
            "action_type": action_type,
            "target_type": target_type,
            "target_id": target_id,
            "evaluation_subject_hash": evaluation_subject_hash,
            "window_opened_at": opened_at,
            "first_complete_date": first_complete_date,
            "reporting_timezone": REPORTING_TIMEZONE,
            "evaluation_checkpoints": EVALUATION_CHECKPOINTS,
            "causal_claim": False,
            "meta_write_allowed": False,
        })
        return {
            "experiment_id": experiment_id,
            "execution_task_id": str(task["execution_task_id"]),
            "receipt_id": str(final_receipt["receipt_id"]),
            "plan_hash": plan_hash,
            "receipt_hash": receipt_hash,
            "evidence_root_hash": evidence_root_hash,
            "action_type": action_type,
            "target_type": target_type,
            "target_id": target_id,
            "evaluation_subject": evaluation_subject,
            "evaluation_subject_hash": evaluation_subject_hash,
            "window_opened_at": opened_at,
            "first_complete_date": first_complete_date,
        }

    def _evaluation_subject(
        self, experiment: Dict[str, Any], target_ad_id: str,
    ) -> Dict[str, Any]:
        experiment_id = str(experiment.get("experiment_id") or "").strip()
        launch_id = str(experiment.get("source_report_id") or "").strip()
        account_id = str(experiment.get("account_id") or "").removeprefix("act_")
        rows = []
        if launch_id:
            rows = self.conn.execute(
                """SELECT * FROM ad_experiment
                   WHERE source_report_id=? ORDER BY experiment_id""",
                (launch_id,),
            ).fetchall()
        experiments = [
            AdExperimentService._serialize(row) for row in rows
        ] if rows else [experiment]
        valid_group = 2 <= len(experiments) <= 4
        cells: List[Dict[str, Any]] = []
        seen_experiments: set[str] = set()
        seen_ads: set[str] = set()
        for item in experiments:
            item_experiment_id = str(item.get("experiment_id") or "").strip()
            ad_id = str(item.get("source_ad_id") or "").strip()
            item_account_id = str(item.get("account_id") or "").removeprefix("act_")
            if (
                not item_experiment_id or not ad_id or item_account_id != account_id
                or item_experiment_id in seen_experiments or ad_id in seen_ads
            ):
                valid_group = False
                break
            seen_experiments.add(item_experiment_id)
            seen_ads.add(ad_id)
            stop_rules = dict(
                dict(item.get("stop_rule_json") or {}).get("delivery_guardrails") or {}
            )
            stop_rules_source = "EXPERIMENT_FROZEN_DELIVERY_GUARDRAILS"
            if not stop_rules and launch_id:
                stop_rules = new_account_delivery_guardrails()
                stop_rules_source = "LEGACY_NEW_ACCOUNT_POLICY_COMPAT_MX_COLD_START_STOP_V1"
            cells.append({
                "experiment_id": item_experiment_id,
                "ad_id": ad_id,
                "role": str(dict(item.get("control_definition_json") or {}).get("role") or "").upper(),
                "delivery_guardrails": stop_rules,
                "delivery_guardrails_hash": payload_hash(stop_rules),
                "delivery_guardrails_source": stop_rules_source,
            })
        if (
            not valid_group or experiment_id not in seen_experiments
            or target_ad_id not in seen_ads
        ):
            stop_rules = dict(
                dict(experiment.get("stop_rule_json") or {}).get("delivery_guardrails") or {}
            )
            stop_rules_source = "EXPERIMENT_FROZEN_DELIVERY_GUARDRAILS"
            if not stop_rules and str(experiment.get("source_report_id") or "").strip():
                stop_rules = new_account_delivery_guardrails()
                stop_rules_source = "LEGACY_NEW_ACCOUNT_POLICY_COMPAT_MX_COLD_START_STOP_V1"
            cells = [{
                "experiment_id": experiment_id,
                "ad_id": target_ad_id,
                "role": str(dict(experiment.get("control_definition_json") or {}).get("role") or "").upper(),
                "delivery_guardrails": stop_rules,
                "delivery_guardrails_hash": payload_hash(stop_rules),
                "delivery_guardrails_source": stop_rules_source,
            }]
            launch_id = ""
        return {
            "schema_version": "gle-evaluation-cycle-subject-v1",
            "mode": "SAME_LAUNCH_REMAINING_ADS" if len(cells) >= 2 else "SINGLE_TARGET_AFTER_PAUSE",
            "launch_id": launch_id,
            "account_id": account_id,
            "target_experiment_id": experiment_id,
            "target_ad_id": target_ad_id,
            "cells": cells,
            "metric_source": "ad_creative_performance_daily",
            "metric_date_field": "report_date_london",
            "causal_claim": False,
            "meta_write_allowed": False,
        }

    @staticmethod
    def _utc(value: str, code: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError as exc:
            raise GrowthStateConflict(code) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise GrowthStateConflict(code)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _receipt(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        for key in ("step_result_json", "meta_object_ids_json", "verification_result_json"):
            result[key] = decode_json(result.get(key), {})
        return result

    @staticmethod
    def _serialize(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["evaluation_subject"] = decode_json(
            result.pop("evaluation_subject_json"), {},
        )
        result["evaluation_checkpoints"] = decode_json(
            result.pop("evaluation_checkpoints_json"), [],
        )
        result["causal_claim"] = bool(result["causal_claim"])
        result["meta_write_allowed"] = bool(result["meta_write_allowed"])
        return result
