from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.growth.common import canonical_json, decode_json, new_id, payload_hash, utc_now
from app.growth.errors import GrowthNotFound, GrowthStateConflict, GrowthValidationError
from app.growth.schema import ensure_growth_schema


EXECUTION_TRANSITIONS = {
    "QUEUED": {"RUNNING", "MANUAL_REVIEW"},
    "RUNNING": {"VERIFYING", "MANUAL_REVIEW"},
    "VERIFYING": {"QUEUED", "SUCCESS", "MANUAL_REVIEW"},
    "SUCCESS": set(),
    "MANUAL_REVIEW": {"VERIFYING"},
}

OPERATION_ACTION_SCOPES = {"EXPERIMENT", "BUSINESS_PROTECTION"}


class ExecutionTaskService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_growth_schema(conn)

    def create_operation_action(
        self,
        *,
        decision_id: str,
        action_type: str,
        target_type: str,
        target_id: str,
        payload: Dict[str, Any],
        episode_id: str = "",
        action_scope: str = "BUSINESS_PROTECTION",
        created_by: str = "",
        idempotency_key: str = "",
    ) -> Dict[str, Any]:
        normalized_scope = str(action_scope or "").strip().upper()
        if normalized_scope not in OPERATION_ACTION_SCOPES:
            raise GrowthValidationError("invalid_action_scope")
        if not str(action_type or "").strip():
            raise GrowthValidationError("action_type_is_required")
        if not str(target_type or "").strip() or not str(target_id or "").strip():
            raise GrowthValidationError("action_target_is_required")
        decision = self.conn.execute(
            "SELECT decision_id FROM growth_decision WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        if not decision:
            raise GrowthNotFound("decision_not_found")
        if episode_id:
            episode = self.conn.execute(
                "SELECT decision_id FROM growth_decision_episode WHERE episode_id=?",
                (episode_id,),
            ).fetchone()
            if not episode:
                raise GrowthNotFound("episode_not_found")
            if str(episode["decision_id"]) != str(decision_id):
                raise GrowthStateConflict("episode_decision_mismatch")
        digest = payload_hash({
            "decision_id": decision_id, "episode_id": episode_id,
            "action_type": action_type, "action_scope": normalized_scope,
            "target_type": target_type, "target_id": target_id, "payload": payload,
        })
        if idempotency_key:
            existing = self.conn.execute(
                """
                SELECT request_hash, response_json FROM growth_idempotency_record
                WHERE route_key='operation_action.create' AND idempotency_key=?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != digest:
                    raise GrowthStateConflict("idempotency_key_payload_conflict")
                return decode_json(existing["response_json"], {})
        now = utc_now()
        action_id = new_id("operation")
        try:
            if idempotency_key:
                self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                """
                INSERT INTO growth_operation_action
                (operation_action_id, decision_id, episode_id, action_type, action_scope,
                 target_type, target_id, payload_json, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CREATED', ?, ?, ?)
                """,
                (
                    action_id, decision_id, episode_id or None, action_type, normalized_scope,
                    target_type, target_id, canonical_json(payload), created_by, now, now,
                ),
            )
            result = self.get_operation_action(action_id)
            if idempotency_key:
                self.conn.execute(
                    """
                    INSERT INTO growth_idempotency_record
                    (route_key, idempotency_key, request_hash, response_status, response_json, created_at)
                    VALUES ('operation_action.create', ?, ?, 201, ?, ?)
                    """,
                    (idempotency_key, digest, canonical_json(result), now),
                )
            self.conn.commit()
            return result
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            if idempotency_key:
                existing = self.conn.execute(
                    """
                    SELECT request_hash, response_json FROM growth_idempotency_record
                    WHERE route_key='operation_action.create' AND idempotency_key=?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if existing and existing["request_hash"] == digest:
                    return decode_json(existing["response_json"], {})
            raise GrowthStateConflict("operation_action_constraint_conflict") from exc
        except Exception:
            self.conn.rollback()
            raise

    def enqueue_task(
        self,
        operation_action_id: str,
        *,
        idempotency_key: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not str(idempotency_key or "").strip():
            raise GrowthValidationError("idempotency_key_is_required")
        payload = dict(payload or {})
        live_approval = None
        if str(payload.get("execution_mode") or "").strip().lower() == "live":
            from app.growth.approval_service import OperationApprovalService

            action = self.get_operation_action(operation_action_id)
            plan = dict(payload.get("plan") or {})
            approval_id = str(payload.get("approval_id") or "").strip()
            live_approval = OperationApprovalService(self.conn).get(approval_id)
            if live_approval["operation_action_id"] != operation_action_id:
                raise GrowthStateConflict("approval_action_mismatch")
            if live_approval["plan_hash"] != payload_hash(plan):
                raise GrowthStateConflict("approved_plan_changed")
            payload["approval"] = {
                "approval_id": live_approval["approval_id"],
                "status": live_approval["status"],
                "approved_by": live_approval["approved_by"],
                "approved_at": live_approval["approved_at"],
                "expires_at": live_approval.get("expires_at", ""),
                "consumed_at": live_approval.get("consumed_at", ""),
            }
            payload["action_type"] = action["action_type"]
        digest = payload_hash({
            "operation_action_id": operation_action_id,
            "payload": payload,
        })
        task_id = new_id("meta_task")
        now = utc_now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute(
                "SELECT * FROM meta_execution_task WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != digest:
                    legacy_digest = payload_hash(payload)
                    if (
                        existing["operation_action_id"] != operation_action_id
                        or existing["request_hash"] != legacy_digest
                    ):
                        raise GrowthStateConflict("idempotency_key_payload_conflict")
                    self.conn.execute(
                        "UPDATE meta_execution_task SET request_hash=?, updated_at=? WHERE execution_task_id=?",
                        (digest, now, existing["execution_task_id"]),
                    )
                    existing = self.conn.execute(
                        "SELECT * FROM meta_execution_task WHERE execution_task_id=?",
                        (existing["execution_task_id"],),
                    ).fetchone()
                result = self._serialize_task(existing)
                self.conn.commit()
                return result
            if live_approval is not None:
                payload["approval"] = OperationApprovalService(self.conn).approved_payload(
                    str(live_approval["approval_id"]), operation_action_id,
                    dict(payload.get("plan") or {}), consume=True,
                )
                digest = payload_hash({
                    "operation_action_id": operation_action_id,
                    "payload": payload,
                })
            self.get_operation_action(operation_action_id)
            cursor = self.conn.execute(
                """
                UPDATE growth_operation_action SET status='QUEUED', updated_at=?
                WHERE operation_action_id=? AND status='CREATED'
                """,
                (now, operation_action_id),
            )
            if cursor.rowcount != 1:
                raise GrowthStateConflict("operation_action_not_queueable")
            self.conn.execute(
                """
                INSERT INTO meta_execution_task
                (execution_task_id, operation_action_id, idempotency_key, request_hash,
                 status, payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'QUEUED', ?, ?, ?)
                """,
                (task_id, operation_action_id, idempotency_key, digest, canonical_json(payload), now, now),
            )
            self._record_action_transition(
                operation_action_id, "CREATED", "QUEUED", "",
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise GrowthStateConflict("operation_action_not_queueable") from exc
        except Exception:
            self.conn.rollback()
            raise
        return self.get_task(task_id)

    def claim_next(
        self, worker_id: str, *, execution_mode: str = "",
    ) -> Optional[Dict[str, Any]]:
        if not str(worker_id or "").strip():
            raise GrowthValidationError("worker_id_is_required")
        now = utc_now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            mode = str(execution_mode or "").strip().lower()
            mode_clause = ""
            params = []
            if mode == "live":
                mode_clause = "AND COALESCE(json_extract(payload_json, '$.execution_mode'), '')='live'"
            elif mode in {"dry_run", "fake"}:
                mode_clause = "AND COALESCE(json_extract(payload_json, '$.execution_mode'), '')<>'live'"
            row = self.conn.execute(
                f"""
                SELECT execution_task_id, operation_action_id FROM meta_execution_task
                WHERE status='QUEUED'
                {mode_clause}
                ORDER BY created_at, execution_task_id
                LIMIT 1
                """,
                params,
            ).fetchone()
            if not row:
                self.conn.commit()
                return None
            task_id = row["execution_task_id"]
            operation_action_id = row["operation_action_id"]
            cursor = self.conn.execute(
                """
                UPDATE meta_execution_task
                SET status='RUNNING', locked_by=?, locked_at=?, heartbeat_at=?, updated_at=?
                WHERE execution_task_id=? AND status='QUEUED'
                """,
                (worker_id, now, now, now, task_id),
            )
            if cursor.rowcount != 1:
                self.conn.rollback()
                return None
            action_cursor = self.conn.execute(
                """
                UPDATE growth_operation_action SET status='EXECUTING', updated_at=?
                WHERE operation_action_id=? AND status='QUEUED'
                """,
                (now, operation_action_id),
            )
            if action_cursor.rowcount != 1:
                raise GrowthStateConflict("operation_action_not_executable")
            self._record_transition(task_id, "QUEUED", "RUNNING", worker_id)
            self._record_action_transition(
                operation_action_id, "QUEUED", "EXECUTING", worker_id,
            )
            self._sync_experiment_for_action(operation_action_id, phase="STARTED", actor=worker_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_task(task_id)

    def expire_queued_live_tasks(self, *, actor: str = "system-expiry") -> int:
        # Operator plans are immutable and guarded by approval consumption plus
        # before-value drift checks. They do not expire with wall-clock time.
        return 0

    def heartbeat(self, task_id: str, worker_id: str) -> Dict[str, Any]:
        now = utc_now()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE meta_execution_task SET heartbeat_at=?, updated_at=?
                WHERE execution_task_id=? AND locked_by=? AND status IN ('RUNNING', 'VERIFYING')
                """,
                (now, now, task_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise GrowthStateConflict("execution_task_lock_lost")
        return self.get_task(task_id)

    def defer_final_readback_reconciliation(
        self,
        task_id: str,
        *,
        worker_id: str,
        current_step: str,
        meta_object_ids: Dict[str, Any],
        error_message: str,
    ) -> Dict[str, Any]:
        """Release a VERIFYING task for one GET-only reconciliation attempt."""
        now = utc_now()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE meta_execution_task
                SET current_step=?, meta_object_ids_json=?, locked_by='', locked_at='', heartbeat_at='',
                    error_code='heartbeat_expired', error_message=?, updated_at=?
                WHERE execution_task_id=? AND status='VERIFYING' AND locked_by=?
                """,
                (
                    current_step or "VERIFY", canonical_json(meta_object_ids),
                    error_message or "final_get_readback_retry_required", now,
                    task_id, worker_id,
                ),
            )
            if cursor.rowcount != 1:
                raise GrowthStateConflict("execution_task_lock_lost")
        return self.get_task(task_id)

    def defer_step_readback_reconciliation(
        self, task_id: str, *, worker_id: str, current_step: str,
        meta_object_ids: Dict[str, Any], error_message: str,
    ) -> Dict[str, Any]:
        """Pause after a successful Meta write and retry only its GET readback."""
        task = self.get_task(task_id)
        if task["status"] not in {"RUNNING", "VERIFYING"}:
            raise GrowthStateConflict("execution_task_not_reconcilable")
        if task["locked_by"] != worker_id:
            raise GrowthStateConflict("execution_task_lock_lost")
        now = utc_now()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE meta_execution_task
                SET status='VERIFYING', current_step=?, meta_object_ids_json=?,
                    locked_by='', locked_at='', heartbeat_at='',
                    error_code='step_readback_required', error_message=?, updated_at=?
                WHERE execution_task_id=? AND status=? AND locked_by=?
                """,
                (current_step, canonical_json(meta_object_ids),
                 error_message or "step_get_readback_retry_required", now,
                 task_id, task["status"], worker_id),
            )
            if cursor.rowcount != 1:
                raise GrowthStateConflict("execution_task_lock_lost")
            if task["status"] != "VERIFYING":
                self._record_transition(task_id, task["status"], "VERIFYING", worker_id)
        return self.get_task(task_id)

    def defer_reconciliation_retry(
        self, task_id: str, *, worker_id: str, current_step: str,
        meta_object_ids: Dict[str, Any], error_message: str,
    ) -> Dict[str, Any]:
        now = utc_now()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE meta_execution_task
                SET current_step=?, meta_object_ids_json=?, locked_by='', locked_at='', heartbeat_at='',
                    error_code='step_readback_required', error_message=?, updated_at=?
                WHERE execution_task_id=? AND status='VERIFYING' AND locked_by=?
                """,
                (current_step, canonical_json(meta_object_ids),
                 error_message or "step_get_readback_retry_required", now, task_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise GrowthStateConflict("execution_task_lock_lost")
        return self.get_task(task_id)

    def resume_after_reconciled_step(
        self, task_id: str, *, worker_id: str, current_step: str,
        meta_object_ids: Dict[str, Any],
    ) -> Dict[str, Any]:
        task = self.get_task(task_id)
        if task["status"] != "VERIFYING" or task["locked_by"] != worker_id:
            raise GrowthStateConflict("execution_task_lock_lost")
        payload = dict(task["payload_json"] or {})
        plan = dict(payload.get("plan") or {})
        from app.growth.meta_execution_worker import execution_steps_for
        steps = execution_steps_for(str(payload.get("action_type") or ""), payload)
        normalized_step = str(current_step or "").upper()
        if normalized_step not in steps:
            raise GrowthStateConflict("reconciled_step_not_in_plan")
        completed_steps = list(steps[:steps.index(normalized_step) + 1])
        payload["continuation"] = {
            "source_execution_task_id": task_id,
            "plan_hash": payload_hash(plan),
            "completed_steps": completed_steps,
            "verified_steps": completed_steps,
            "meta_object_ids": dict(meta_object_ids),
            "verification_only": False,
            "reconciled_step": normalized_step,
        }
        action = self.get_operation_action(task["operation_action_id"])
        if action["status"] != "EXECUTING":
            raise GrowthStateConflict("operation_action_not_executing")
        now = utc_now()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE meta_execution_task
                SET status='QUEUED', payload_json=?, meta_object_ids_json=?, current_step=?,
                    locked_by='', locked_at='', heartbeat_at='', error_code='', error_message='',
                    updated_at=?, finished_at=''
                WHERE execution_task_id=? AND status='VERIFYING' AND locked_by=?
                """,
                (canonical_json(payload), canonical_json(meta_object_ids), normalized_step,
                 now, task_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise GrowthStateConflict("execution_task_lock_lost")
            action_cursor = self.conn.execute(
                """UPDATE growth_operation_action SET status='QUEUED', updated_at=?
                   WHERE operation_action_id=? AND status='EXECUTING'""",
                (now, task["operation_action_id"]),
            )
            if action_cursor.rowcount != 1:
                raise GrowthStateConflict("operation_action_changed_concurrently")
            self._record_transition(task_id, "VERIFYING", "QUEUED", worker_id)
            self._record_action_transition(task["operation_action_id"], "EXECUTING", "QUEUED", worker_id)
        return self.get_task(task_id)

    def recover_rate_limited_activation_tasks(self, *, actor: str) -> int:
        rows = self.conn.execute(
            """
            SELECT t.*, a.action_type, a.status AS action_status
            FROM meta_execution_task t
            JOIN growth_operation_action a ON a.operation_action_id=t.operation_action_id
            WHERE t.status='MANUAL_REVIEW' AND t.error_code='meta_result_uncertain'
              AND a.action_type='REACTIVATE_AD' AND a.status='MANUAL_REVIEW'
            ORDER BY t.updated_at, t.execution_task_id
            """
        ).fetchall()
        recovered = 0
        for raw in rows:
            task = self._serialize_task(raw)
            step = str(task.get("current_step") or "").upper()
            if not (step in {"CAMPAIGN_STATUS_UPDATE", "ADSET_STATUS_UPDATE", "AD_STATUS_UPDATE"}
                    or step.endswith("_ADSET_STATUS_UPDATE") or step.endswith("_AD_STATUS_UPDATE")):
                continue
            receipt = self.conn.execute(
                """SELECT step_status,step_result_json,verification_result_json
                   FROM meta_execution_task_receipt
                   WHERE execution_task_id=? AND step_name=?
                   ORDER BY created_at DESC,receipt_id DESC LIMIT 1""",
                (task["execution_task_id"], step),
            ).fetchone()
            if not receipt:
                continue
            result = decode_json(receipt["step_result_json"], {})
            verification = decode_json(receipt["verification_result_json"], {})
            if not (str(receipt["step_status"] or "").upper() == "UNKNOWN"
                    and str(result.get("status") or "").upper() == "SUCCESS"
                    and str(verification.get("status") or "").upper() == "UNKNOWN"
                    and str(verification.get("error") or "") == "adapter_verify_exception"
                    and str(verification.get("exception_type") or "") == "MetaRateLimitBlocked"):
                continue
            now = utc_now()
            with self.conn:
                task_cursor = self.conn.execute(
                    """UPDATE meta_execution_task
                       SET status='VERIFYING', locked_by='', locked_at='', heartbeat_at='',
                           error_code='step_readback_required',
                           error_message='rate_limit_readback_reconciliation_pending',
                           updated_at=?, finished_at=''
                       WHERE execution_task_id=? AND status='MANUAL_REVIEW'""",
                    (now, task["execution_task_id"]),
                )
                action_cursor = self.conn.execute(
                    """UPDATE growth_operation_action SET status='EXECUTING', updated_at=?
                       WHERE operation_action_id=? AND status='MANUAL_REVIEW'""",
                    (now, task["operation_action_id"]),
                )
                if task_cursor.rowcount != 1 or action_cursor.rowcount != 1:
                    raise GrowthStateConflict("rate_limit_recovery_changed_concurrently")
                self._record_transition(task["execution_task_id"], "MANUAL_REVIEW", "VERIFYING", actor)
                self._record_action_transition(task["operation_action_id"], "MANUAL_REVIEW", "EXECUTING", actor)
                self._sync_experiment_for_action(task["operation_action_id"], phase="STARTED", actor=actor)
            recovered += 1
        return recovered

    def claim_reconciliation(
        self, worker_id: str, *, stale_after_seconds: int = 90, execution_mode: str = "",
    ) -> Optional[Dict[str, Any]]:
        if not str(worker_id or "").strip():
            raise GrowthValidationError("worker_id_is_required")
        now = utc_now()
        cutoff = (datetime.now(timezone.utc) - timedelta(
            seconds=max(30, stale_after_seconds),
        )).isoformat()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            mode = str(execution_mode or "").strip().lower()
            mode_clause = ""
            if mode == "live":
                mode_clause = "AND COALESCE(json_extract(payload_json, '$.execution_mode'), '')='live'"
            elif mode in {"dry_run", "fake"}:
                mode_clause = "AND COALESCE(json_extract(payload_json, '$.execution_mode'), '')<>'live'"
            row = self.conn.execute(
                f"""
                SELECT execution_task_id FROM meta_execution_task
                WHERE status='VERIFYING'
                  AND (error_code IN ('heartbeat_expired','step_readback_required') OR (heartbeat_at<>'' AND heartbeat_at<?))
                  {mode_clause}
                ORDER BY updated_at, execution_task_id
                LIMIT 1
                """,
                (cutoff,),
            ).fetchone()
            if not row:
                self.conn.commit()
                return None
            task_id = row["execution_task_id"]
            cursor = self.conn.execute(
                """
                UPDATE meta_execution_task
                SET locked_by=?, locked_at=?, heartbeat_at=?, updated_at=?,
                    error_code='reconciliation_claimed', error_message='get_verification_in_progress'
                WHERE execution_task_id=? AND status='VERIFYING'
                """,
                (worker_id, now, now, now, task_id),
            )
            if cursor.rowcount != 1:
                raise GrowthStateConflict("execution_task_lock_lost")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_task(task_id)

    def transition(
        self,
        task_id: str,
        to_status: str,
        *,
        worker_id: str,
        current_step: str = "",
        meta_object_ids: Optional[Dict[str, Any]] = None,
        error_code: str = "",
        error_message: str = "",
    ) -> Dict[str, Any]:
        task = self.get_task(task_id)
        target = str(to_status or "").strip().upper()
        if target not in EXECUTION_TRANSITIONS.get(task["status"], set()):
            raise GrowthStateConflict(f"illegal_execution_transition:{task['status']}:{target}")
        if task["locked_by"] and task["locked_by"] != worker_id:
            raise GrowthStateConflict("execution_task_lock_lost")
        now = utc_now()
        finished_at = now if target in {"SUCCESS", "MANUAL_REVIEW"} else ""
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE meta_execution_task
                SET status=?, current_step=?, meta_object_ids_json=?, error_code=?, error_message=?,
                    heartbeat_at=?, updated_at=?, finished_at=?
                WHERE execution_task_id=? AND status=?
                """,
                (
                    target, current_step or task["current_step"],
                    canonical_json(meta_object_ids if meta_object_ids is not None else task["meta_object_ids_json"]),
                    error_code, error_message, now, now, finished_at, task_id, task["status"],
                ),
            )
            if cursor.rowcount != 1:
                raise GrowthStateConflict("execution_task_changed_concurrently")
            action_status = "VERIFIED" if target == "SUCCESS" else ("MANUAL_REVIEW" if target == "MANUAL_REVIEW" else "EXECUTING")
            action = self.get_operation_action(task["operation_action_id"])
            if action_status != action["status"]:
                action_cursor = self.conn.execute(
                    """
                    UPDATE growth_operation_action SET status=?, updated_at=?
                    WHERE operation_action_id=? AND status=?
                    """,
                    (action_status, now, task["operation_action_id"], action["status"]),
                )
                if action_cursor.rowcount != 1:
                    raise GrowthStateConflict("operation_action_changed_concurrently")
                self._record_action_transition(
                    task["operation_action_id"], action["status"], action_status, worker_id,
                )
            self._record_transition(task_id, task["status"], target, worker_id)
            if target in {"SUCCESS", "MANUAL_REVIEW"}:
                if meta_object_ids:
                    self._bind_experiment_meta_objects(
                        task["operation_action_id"], meta_object_ids or {},
                        require_complete=target == "SUCCESS",
                    )
                self._sync_experiment_for_action(
                    task["operation_action_id"], phase=target, actor=worker_id,
                )
        return self.get_task(task_id)

    def record_receipt(
        self,
        task_id: str,
        *,
        step_name: str,
        step_status: str,
        step_result: Dict[str, Any],
        meta_object_ids: Dict[str, Any],
        verification_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self.get_task(task_id)
        receipt_id = new_id("meta_receipt")
        created_at = utc_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO meta_execution_task_receipt
                (receipt_id, execution_task_id, step_name, step_status, step_result_json,
                 meta_object_ids_json, verification_result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id, task_id, step_name, step_status, canonical_json(step_result),
                    canonical_json(meta_object_ids), canonical_json(verification_result or {}), created_at,
                ),
            )
        return {
            "receipt_id": receipt_id,
            "execution_task_id": task_id,
            "step_name": step_name,
            "step_status": step_status,
            "created_at": created_at,
        }

    def complete_local_action(self, operation_action_id: str, *, actor: str) -> Dict[str, Any]:
        action = self.get_operation_action(operation_action_id)
        if action["action_type"] not in {"OBSERVE", "CHECK_DATA"}:
            raise GrowthStateConflict("local_action_not_low_risk")
        if action["status"] != "CREATED":
            raise GrowthStateConflict("operation_action_not_locally_completable")
        now = utc_now()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE growth_operation_action SET status='VERIFIED', updated_at=?
                WHERE operation_action_id=? AND status='CREATED'
                """,
                (now, operation_action_id),
            )
            if cursor.rowcount != 1:
                raise GrowthStateConflict("operation_action_changed_concurrently")
            self._record_action_transition(operation_action_id, "CREATED", "VERIFIED", actor)
        return self.get_operation_action(operation_action_id)

    def move_expired_to_reconciliation(self, *, stale_after_seconds: int = 90) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(30, stale_after_seconds))).isoformat()
        rows = self.conn.execute(
            """
            SELECT execution_task_id, locked_by FROM meta_execution_task
            WHERE status='RUNNING' AND heartbeat_at<>'' AND heartbeat_at<?
            """,
            (cutoff,),
        ).fetchall()
        moved = 0
        for row in rows:
            try:
                self.transition(
                    row["execution_task_id"], "VERIFYING", worker_id=row["locked_by"],
                    error_code="heartbeat_expired", error_message="reconciliation_required",
                )
                moved += 1
            except GrowthStateConflict:
                continue
        return moved

    def get_operation_action(self, operation_action_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM growth_operation_action WHERE operation_action_id=?",
            (operation_action_id,),
        ).fetchone()
        if not row:
            raise GrowthNotFound("operation_action_not_found")
        result = dict(row)
        result["payload_json"] = decode_json(result["payload_json"], {})
        return result

    def get_task(self, task_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM meta_execution_task WHERE execution_task_id=?",
            (task_id,),
        ).fetchone()
        if not row:
            raise GrowthNotFound("execution_task_not_found")
        return self._serialize_task(row)

    def _sync_experiment_for_action(self, operation_action_id: str, *, phase: str, actor: str) -> None:
        action = self.get_operation_action(operation_action_id)
        action_payload = dict(action.get("payload_json") or {})
        experiment_ids = [str(item).strip() for item in list(action_payload.get("experiment_ids") or []) if str(item).strip()]
        if not experiment_ids:
            experiment_id = str(action_payload.get("experiment_id") or "").strip()
            experiment_ids = [experiment_id] if experiment_id else []
        if not experiment_ids:
            return
        from app.growth.ad_experiment_service import AdExperimentService, EXPERIMENT_TRANSITIONS

        experiments = AdExperimentService(self.conn)
        action_type = str(action.get("action_type") or "").upper()
        if phase == "STARTED":
            next_state = "CREATING_PAUSED_OBJECTS" if action_type == "CREATE_PAUSED_AD" else "ADJUSTING"
        elif phase == "MANUAL_REVIEW":
            next_state = "CREATION_PARTIAL_FAILURE" if action_type == "CREATE_PAUSED_AD" else "DATA_INCOMPLETE"
        elif action_type == "CREATE_PAUSED_AD":
            next_state = "META_REVIEW_PENDING"
        elif action_type in {"PAUSE_AD", "PAUSE_ADSET"}:
            next_state = "PAUSED"
        elif action_type == "REACTIVATE_AD":
            next_state = "RUNNING"
        elif action_type == "REPLACE_CREATIVE":
            next_state = "META_REVIEW_PENDING"
        else:
            next_state = "EVALUATING_ADJUSTMENT"
        for experiment_id in experiment_ids:
            experiment = experiments.get(experiment_id)
            if next_state in EXPERIMENT_TRANSITIONS.get(experiment["state"], set()):
                experiments.transition(
                    experiment_id, next_state, actor=actor,
                    reason=f"operation_action:{operation_action_id}:{phase.lower()}",
                    event_type="EXECUTION_STATUS_CHANGED",
                    evidence={"operation_action_id": operation_action_id, "action_type": action_type, "phase": phase},
                )
            if action_type == "REPLACE_CREATIVE" and phase == "SUCCESS":
                self.conn.execute(
                    """
                    UPDATE ad_meta_review_state
                    SET remediation_status='SUBMITTED',last_checked_at='',updated_at=?
                    WHERE experiment_id=?
                    """,
                    (utc_now(), experiment_id),
                )

    def _bind_experiment_meta_objects(
        self, operation_action_id: str, object_ids: Dict[str, Any], *, require_complete: bool = False,
    ) -> None:
        action = self.get_operation_action(operation_action_id)
        action_payload = dict(action.get("payload_json") or {})
        plan = dict(action_payload.get("plan") or {})
        target_account_id = str(plan.get("target_account_id") or "").removeprefix("act_")
        cells = list(plan.get("cells") or [])
        bindings = []
        if cells:
            for index, raw_cell in enumerate(cells, start=1):
                cell = dict(raw_cell or {})
                prefix = f"{str(cell.get('cell_key') or f'C{index}').lower()}_"
                experiment_id = str(cell.get("experiment_id") or "")
                steps = dict(cell.get("steps") or {})
                image_step = dict(steps.get("IMAGE_UPLOAD") or {})
                image_id = str(cell.get("frozen_creative_id") or image_step.get("image_id") or "").strip()
                image_path = str(image_step.get("image_path") or "").strip()
                if not image_id and image_path:
                    row = self.conn.execute(
                        "SELECT image_id FROM creative_generated_images WHERE file_path=? OR image_ref=? LIMIT 1",
                        (image_path, image_path),
                    ).fetchone()
                    image_id = str(row["image_id"] or "").strip() if row else ""
                    if not image_id and Path(image_path).stem.startswith("pro_img_"):
                        image_id = Path(image_path).stem
                if not image_id and experiment_id:
                    from app.growth.ad_experiment_service import AdExperimentService

                    approved = AdExperimentService(self.conn).latest_approved_creative(experiment_id)
                    image_id = str(approved.get("image_id") or "").strip()
                bindings.append((experiment_id, {
                    "source_campaign_id": str(object_ids.get("campaign_id") or ""),
                    "source_adset_id": str(object_ids.get(f"{prefix}adset_id") or ""),
                    "source_creative_id": str(
                        object_ids.get(f"{prefix}creative_id")
                        or object_ids.get("c1_creative_id")
                        or ""
                    ),
                    "source_ad_id": str(object_ids.get(f"{prefix}ad_id") or ""),
                }, image_id))
        else:
            bindings.append((str(action_payload.get("experiment_id") or ""), {
                "source_campaign_id": str(object_ids.get("campaign_id") or ""),
                "source_adset_id": str(object_ids.get("adset_id") or ""),
                "source_creative_id": str(object_ids.get("creative_id") or ""),
                "source_ad_id": str(object_ids.get("ad_id") or object_ids.get("target_id") or ""),
            }, ""))
        if require_complete and cells and str(action.get("action_type") or "").upper() == "CREATE_PAUSED_AD":
            for index, (experiment_id, values, image_id) in enumerate(bindings, start=1):
                required = {"experiment_id": experiment_id, "image_id": image_id, **values}
                missing = [key for key, value in required.items() if not str(value or "").strip()]
                if missing:
                    raise GrowthStateConflict(
                        f"verified_meta_binding_incomplete:C{index}:{','.join(missing)}"
                    )
        for experiment_id, values, image_id in bindings:
            if not experiment_id:
                continue
            update_values = {"account_id": target_account_id, **values}
            assignments = [f"{column}=CASE WHEN ?<>'' THEN ? ELSE {column} END" for column in update_values]
            params = []
            for value in update_values.values():
                params.extend([value, value])
            params.extend([utc_now(), experiment_id])
            self.conn.execute(
                f"UPDATE ad_experiment SET {','.join(assignments)},updated_at=? WHERE experiment_id=?",
                params,
            )
            ad_id = str(values.get("source_ad_id") or "").strip()
            if ad_id:
                from app.creative_image_generation import mark_generated_image_adopted
                from app.growth.ad_experiment_service import AdExperimentService

                experiment = AdExperimentService(self.conn).get(experiment_id)
                if image_id:
                    mark_generated_image_adopted(
                        self.conn,
                        image_id=image_id,
                        ad_id=ad_id,
                        creative_id=str(values.get("source_creative_id") or ""),
                        adset_id=str(values.get("source_adset_id") or ""),
                        campaign_id=str(values.get("source_campaign_id") or ""),
                        adopted_by="meta_execution_worker",
                        experiment_id=experiment_id,
                        experiment_code=str(experiment.get("experiment_code") or ""),
                        adoption_type="verified_meta_creation",
                        binding_method="META_EXECUTION_RECEIPT_MATCH",
                        binding_confidence="HIGH",
                        binding_status="confirmed",
                        evidence={
                            "operation_action_id": operation_action_id,
                            "source": "verified_meta_creation_receipt",
                        },
                        notes="bound from verified Meta creation result",
                        commit=False,
                    )
            if cells and str(plan.get("test_variable") or "").lower() in {"audience_strategy", "copy_variant"}:
                row = self.conn.execute(
                    "SELECT control_definition_json FROM ad_experiment WHERE experiment_id=?",
                    (experiment_id,),
                ).fetchone()
                control = decode_json(row["control_definition_json"], {}) if row else {}
                cell = next((dict(item) for item in cells if str(dict(item).get("experiment_id") or "") == experiment_id), {})
                key = str(cell.get("cell_key") or "").lower()
                control["meta_randomization"] = {
                    "study_id": str(object_ids.get("study_id") or ""),
                    "study_cell_id": str(object_ids.get(f"{key}_study_cell_id") or ""),
                    "allocation_percent": int(cell.get("allocation_percent") or 50),
                    "readback_verified": bool(
                        object_ids.get("study_id") and object_ids.get(f"{key}_study_cell_id")
                    ),
                }
                self.conn.execute(
                    "UPDATE ad_experiment SET control_definition_json=?,updated_at=? WHERE experiment_id=?",
                    (canonical_json(control), utc_now(), experiment_id),
                )
        if cells:
            now = utc_now()
            self.conn.execute(
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
            for index, raw_cell in enumerate(cells, start=1):
                cell = dict(raw_cell or {})
                prefix = f"{str(cell.get('cell_key') or f'C{index}').lower()}_"
                ad_id = str(object_ids.get(f"{prefix}ad_id") or "").strip()
                raw_direction = cell.get("creative_direction")
                if isinstance(raw_direction, dict):
                    direction_key = str(
                        raw_direction.get("key") or raw_direction.get("direction_id") or ""
                    ).strip()
                else:
                    direction_key = str(raw_direction or "").strip()
                if not ad_id or not direction_key:
                    continue
                self.conn.execute(
                    """
                    INSERT INTO ad_creative_direction_mapping
                    (ad_id,direction_key,experiment_id,launch_id,source,created_at,updated_at)
                    VALUES (?,?,?,?, 'new_account_batch_plan',?,?)
                    ON CONFLICT(ad_id) DO UPDATE SET
                        direction_key=excluded.direction_key,
                        experiment_id=excluded.experiment_id,
                        launch_id=excluded.launch_id,
                        source=excluded.source,
                        updated_at=excluded.updated_at
                    """,
                    (
                        ad_id, direction_key, str(cell.get("experiment_id") or ""),
                        str(plan.get("launch_id") or ""), now, now,
                    ),
                )

    @staticmethod
    def _serialize_task(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["payload_json"] = decode_json(result["payload_json"], {})
        result["meta_object_ids_json"] = decode_json(result["meta_object_ids_json"], {})
        return result

    def _record_transition(self, task_id: str, from_status: str, to_status: str, actor: str) -> None:
        self.conn.execute(
            """
            INSERT INTO growth_state_transition
            (transition_id, entity_type, entity_id, from_status, to_status, actor, created_at)
            VALUES (?, 'EXECUTION_TASK', ?, ?, ?, ?, ?)
            """,
            (new_id("transition"), task_id, from_status, to_status, actor, utc_now()),
        )

    def _record_action_transition(
        self, action_id: str, from_status: str, to_status: str, actor: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO growth_state_transition
            (transition_id, entity_type, entity_id, from_status, to_status, actor, created_at)
            VALUES (?, 'OPERATION_ACTION', ?, ?, ?, ?, ?)
            """,
            (new_id("transition"), action_id, from_status, to_status, actor, utc_now()),
        )
