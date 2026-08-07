from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict

from app.growth.common import canonical_json, decode_json, new_id, payload_hash, utc_now
from app.growth.errors import GrowthNotFound, GrowthStateConflict, GrowthValidationError
from app.growth.primary_text_only_compiler import (
    assert_phase1_live_permission,
    is_primary_text_only_plan,
    require_human_approver,
    require_unexpired_approval,
    verify_action_binding,
    verify_compiler_receipt,
)
from app.growth.schema import ensure_growth_schema


class OperationApprovalService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_growth_schema(conn)

    def propose(
        self, operation_action_id: str, plan: Dict[str, Any], *,
        proposed_by: str, idempotency_key: str, expires_at: str = "",
    ) -> Dict[str, Any]:
        if not str(idempotency_key or "").strip():
            raise GrowthValidationError("idempotency_key_is_required")
        action = self.conn.execute(
            """SELECT status,payload_json,action_type,action_scope,target_type,target_id
            FROM growth_operation_action WHERE operation_action_id=?""",
            (operation_action_id,),
        ).fetchone()
        if not action:
            raise GrowthNotFound("operation_action_not_found")
        if action["status"] != "CREATED":
            raise GrowthStateConflict("operation_action_not_approvable")
        if not isinstance(plan, dict) or not plan:
            raise GrowthValidationError("meta_execution_plan_required")
        if is_primary_text_only_plan(plan):
            action_plan = dict(decode_json(action["payload_json"], {}).get("plan") or {})
            if payload_hash(action_plan) != payload_hash(plan):
                raise GrowthStateConflict("gle_primary_text_only_action_plan_mismatch")
            verify_compiler_receipt(plan)
            verify_action_binding(
                plan, action_type=action["action_type"], action_scope=action["action_scope"],
                target_type=action["target_type"], target_id=action["target_id"],
            )
            require_unexpired_approval(
                expires_at, plan_expires_at=plan.get("expires_at"),
            )
            assert_phase1_live_permission(plan, conn=self.conn)
        request_digest = payload_hash({
            "operation_action_id": operation_action_id, "plan": plan,
        })
        existing = self.conn.execute(
            "SELECT * FROM growth_operation_approval WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_digest:
                raise GrowthStateConflict("idempotency_key_payload_conflict")
            return self._serialize(existing)
        approval_id = new_id("approval")
        now = utc_now()
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO growth_operation_approval
                    (approval_id, operation_action_id, plan_hash, plan_json, status,
                     proposed_by, expires_at, idempotency_key, request_hash, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'PROPOSED', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approval_id, operation_action_id, payload_hash(plan), canonical_json(plan),
                        proposed_by, str(expires_at or ""), idempotency_key, request_digest, now, now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise GrowthStateConflict("operation_approval_constraint_conflict") from exc
        return self.get(approval_id)

    def transition(
        self, approval_id: str, status: str, *, actor: str,
        single_operator_confirmation: str = "",
    ) -> Dict[str, Any]:
        current = self.get(approval_id)
        target = str(status or "").strip().upper()
        if current["status"] != "PROPOSED" or target not in {"APPROVED", "REJECTED"}:
            raise GrowthStateConflict("illegal_operation_approval_transition")
        same_operator = str(current.get("proposed_by") or "") == str(actor or "")
        if target == "APPROVED" and is_primary_text_only_plan(current.get("plan_json")):
            action = self.conn.execute(
                """SELECT payload_json,action_type,action_scope,target_type,target_id
                FROM growth_operation_action WHERE operation_action_id=?""",
                (current["operation_action_id"],),
            ).fetchone()
            action_plan = dict(decode_json(action["payload_json"], {}).get("plan") or {}) if action else {}
            if payload_hash(action_plan) != payload_hash(current["plan_json"]):
                raise GrowthStateConflict("gle_primary_text_only_action_plan_mismatch")
            verify_compiler_receipt(current["plan_json"])
            verify_action_binding(
                current["plan_json"], action_type=action["action_type"],
                action_scope=action["action_scope"], target_type=action["target_type"],
                target_id=action["target_id"],
            )
            require_unexpired_approval(
                current.get("expires_at"),
                plan_expires_at=current["plan_json"].get("expires_at"),
            )
            assert_phase1_live_permission(current["plan_json"], conn=self.conn)
            require_human_approver(actor)
        if target == "APPROVED" and same_operator:
            confirmation = str(single_operator_confirmation or "").strip().upper()
            if confirmation != "APPROVE_EXACT_PLAN":
                raise GrowthStateConflict("single_operator_second_confirmation_required")
        now = utc_now()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE growth_operation_approval
                SET status=?, approved_by=?, approved_at=?, updated_at=?
                WHERE approval_id=? AND status='PROPOSED'
                """,
                (target, actor, now, now, approval_id),
            )
            if cursor.rowcount != 1:
                raise GrowthStateConflict("operation_approval_changed_concurrently")
            self.conn.execute(
                """
                INSERT INTO growth_state_transition
                (transition_id, entity_type, entity_id, from_status, to_status, actor, created_at)
                VALUES (?, 'OPERATION_APPROVAL', ?, 'PROPOSED', ?, ?, ?)
                """,
                (new_id("transition"), approval_id, target, actor, now),
            )
        return self.get(approval_id)

    def approved_payload(
        self, approval_id: str, operation_action_id: str, plan: Dict[str, Any], *, consume: bool = False,
    ) -> Dict[str, Any]:
        approval = self.get(approval_id)
        if approval["operation_action_id"] != operation_action_id:
            raise GrowthStateConflict("approval_action_mismatch")
        if approval["status"] != "APPROVED":
            raise GrowthStateConflict("operation_approval_required")
        if str(approval.get("consumed_at") or "").strip():
            raise GrowthStateConflict("operation_approval_already_consumed")
        if approval["plan_hash"] != payload_hash(plan):
            raise GrowthStateConflict("approved_plan_changed")
        if is_primary_text_only_plan(plan):
            verify_compiler_receipt(plan)
            require_unexpired_approval(
                approval.get("expires_at"), plan_expires_at=plan.get("expires_at"),
            )
            require_human_approver(str(approval.get("approved_by") or ""))
        consumed_at = ""
        if consume:
            consumed_at = utc_now()
            cursor = self.conn.execute(
                """
                UPDATE growth_operation_approval SET consumed_at=?,updated_at=?
                WHERE approval_id=? AND status='APPROVED' AND consumed_at=''
                """,
                (consumed_at, consumed_at, approval_id),
            )
            if cursor.rowcount != 1:
                raise GrowthStateConflict("operation_approval_already_consumed")
        return {
            "approval_id": approval["approval_id"],
            "status": approval["status"],
            "approved_by": approval["approved_by"],
            "approved_at": approval["approved_at"],
            "expires_at": approval["expires_at"],
            "consumed_at": consumed_at,
        }

    def get(self, approval_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM growth_operation_approval WHERE approval_id=?", (approval_id,),
        ).fetchone()
        if not row:
            raise GrowthNotFound("operation_approval_not_found")
        return self._serialize(row)

    @staticmethod
    def _is_expired(value: Any) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc)

    @staticmethod
    def _serialize(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["plan_json"] = decode_json(result["plan_json"], {})
        return result
