from __future__ import annotations

import sqlite3
from typing import Any, Dict

from app.growth.common import canonical_json, decode_json, payload_hash, utc_now
from app.growth.errors import GrowthNotFound, GrowthValidationError


class RebuildSourceAdCleanupService:
    """Delete exactly one source Ad only after its replacement is verified."""

    def __init__(
        self, conn: sqlite3.Connection, *, session: Any,
        access_token: str, graph_root: str,
    ) -> None:
        self.conn = conn
        self.session = session
        self.access_token = str(access_token or "").strip()
        self.graph_root = str(graph_root or "").strip().rstrip("/")

    def execute(
        self, experiment_id: str, *, plan_id: str, idempotency_key: str,
        request_id: str,
    ) -> Dict[str, Any]:
        request_payload = {"experiment_id": experiment_id, "plan_id": plan_id}
        digest = payload_hash(request_payload)
        existing = self.conn.execute(
            "SELECT request_hash,response_json FROM growth_idempotency_record WHERE route_key=? AND idempotency_key=?",
            ("ad_experiment.rebuild_source_ad_delete", idempotency_key),
        ).fetchone()
        if existing:
            if str(existing["request_hash"] or "") != digest:
                reconciled = self._reconcile_completed_delete(
                    experiment_id, plan_id=plan_id,
                    completed=decode_json(existing["response_json"], {}),
                    request_id=request_id,
                )
                if reconciled:
                    return reconciled
                raise GrowthValidationError("idempotency_key_payload_conflict")
            return decode_json(existing["response_json"], {})

        completed = self._latest_completed_delete(experiment_id)
        if completed:
            reconciled = self._reconcile_completed_delete(
                experiment_id, plan_id=plan_id, completed=completed,
                request_id=request_id,
            )
            if reconciled:
                with self.conn:
                    self.conn.execute(
                        """INSERT INTO growth_idempotency_record
                        (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
                        VALUES (?,?,?,?,?,?)""",
                        (
                            "ad_experiment.rebuild_source_ad_delete", idempotency_key,
                            digest, 200, canonical_json(reconciled), utc_now(),
                        ),
                    )
                return reconciled

        result = self._delete_once(experiment_id, plan_id=plan_id, request_id=request_id)
        with self.conn:
            self.conn.execute(
                """INSERT INTO growth_idempotency_record
                (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
                VALUES (?,?,?,?,?,?)""",
                (
                    "ad_experiment.rebuild_source_ad_delete", idempotency_key,
                    digest, 200, canonical_json(result), utc_now(),
                ),
            )
        return result

    def _latest_completed_delete(self, experiment_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            """SELECT response_json FROM growth_idempotency_record
               WHERE route_key='ad_experiment.rebuild_source_ad_delete'
                 AND response_status=200
                 AND json_extract(response_json,'$.experiment_id')=?
                 AND json_extract(response_json,'$.status')='SUCCESS'
                 AND json_extract(response_json,'$.source_ad_deleted')=1
               ORDER BY created_at DESC LIMIT 1""",
            (experiment_id,),
        ).fetchone()
        return decode_json(row["response_json"], {}) if row else {}

    def _reconcile_completed_delete(
        self, experiment_id: str, *, plan_id: str,
        completed: Dict[str, Any], request_id: str,
    ) -> Dict[str, Any]:
        """Reuse a verified source-delete fact across equivalent repair successors.

        Page repair can be requested by both the durable worker and a still-open
        browser.  A later equivalent successor must not try to delete the already
        removed source Ad again, and a caller idempotency key must not turn that
        completed fact into a false MANUAL_REVIEW result.
        """
        if not (
            str(completed.get("experiment_id") or "") == experiment_id
            and str(completed.get("status") or "").upper() == "SUCCESS"
            and completed.get("source_ad_deleted") is True
        ):
            return {}
        current = self._verified_plan_context(experiment_id, plan_id)
        completed_plan_id = str(completed.get("plan_id") or "")
        if not completed_plan_id:
            return {}
        previous = self._verified_plan_context(experiment_id, completed_plan_id)
        if (
            current["source_ad_id"] != str(completed.get("source_ad_id") or "")
            or previous["source_ad_id"] != current["source_ad_id"]
            or previous["repair_root_plan_id"] != current["repair_root_plan_id"]
        ):
            return {}
        result = dict(completed)
        result.update({
            "request_id": request_id,
            "requested_plan_id": plan_id,
            "reconciled_delete_fact": True,
            "automatic_retry": False,
        })
        current_new_ad_id = str(current.get("new_ad_id") or "")
        canonical_new_ad_id = str(completed.get("new_ad_id") or "")
        if current_new_ad_id and current_new_ad_id != canonical_new_ad_id:
            result["duplicate_replacement_ad_id"] = current_new_ad_id
        return result

    def _verified_plan_context(self, experiment_id: str, plan_id: str) -> Dict[str, str]:
        action = self.conn.execute(
            "SELECT * FROM growth_operation_action WHERE operation_action_id=?", (plan_id,),
        ).fetchone()
        if not action:
            raise GrowthNotFound("operation_action_not_found")
        action_row = dict(action)
        action_payload = decode_json(action_row.get("payload_json"), {})
        plan = dict(action_payload.get("plan") or {})
        if str(action_payload.get("experiment_id") or plan.get("experiment_id") or "") != experiment_id:
            raise GrowthValidationError("rebuild_delete_plan_experiment_mismatch")
        if str(action_row.get("action_type") or "") != "CREATE_PAUSED_AD" or str(action_row.get("status") or "") != "VERIFIED":
            raise GrowthValidationError("rebuild_delete_requires_verified_plan")
        task = self.conn.execute(
            "SELECT * FROM meta_execution_task WHERE operation_action_id=? ORDER BY created_at DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        if not task or str(task["status"] or "") != "SUCCESS":
            raise GrowthValidationError("rebuild_delete_requires_successful_creation")
        before = dict(plan.get("before_json") or {})
        after = dict(plan.get("after_json") or {})
        old_ids = dict(before.get("source_ids") or {})
        object_ids = decode_json(task["meta_object_ids_json"], {})
        return {
            "source_ad_id": str(after.get("source_ad_id_to_delete") or old_ids.get("ad_id") or "").strip(),
            "new_ad_id": str(dict(object_ids or {}).get("ad_id") or "").strip(),
            "repair_root_plan_id": str(action_payload.get("repair_of_operation_action_id") or plan_id),
        }

    def _delete_once(self, experiment_id: str, *, plan_id: str, request_id: str) -> Dict[str, Any]:
        if not self.conn.execute(
            "SELECT 1 FROM ad_experiment WHERE experiment_id=?", (experiment_id,),
        ).fetchone():
            raise GrowthNotFound("ad_experiment_not_found")
        action = self.conn.execute(
            "SELECT * FROM growth_operation_action WHERE operation_action_id=?", (plan_id,),
        ).fetchone()
        if not action:
            raise GrowthNotFound("operation_action_not_found")
        action_row = dict(action)
        action_payload = decode_json(action_row.get("payload_json"), {})
        plan = dict(action_payload.get("plan") or {})
        if str(action_payload.get("experiment_id") or plan.get("experiment_id") or "") != experiment_id:
            raise GrowthValidationError("rebuild_delete_plan_experiment_mismatch")
        if str(action_row.get("action_type") or "") != "CREATE_PAUSED_AD" or str(action_row.get("status") or "") != "VERIFIED":
            raise GrowthValidationError("rebuild_delete_requires_verified_plan")
        task = self.conn.execute(
            "SELECT * FROM meta_execution_task WHERE operation_action_id=? ORDER BY created_at DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        if not task or str(task["status"] or "") != "SUCCESS":
            raise GrowthValidationError("rebuild_delete_requires_successful_creation")
        object_ids = decode_json(task["meta_object_ids_json"], {})
        new_ad_id = str(dict(object_ids or {}).get("ad_id") or "").strip()
        before = dict(plan.get("before_json") or {})
        after = dict(plan.get("after_json") or {})
        old_ids = dict(before.get("source_ids") or {})
        old_ad_id = str(after.get("source_ad_id_to_delete") or old_ids.get("ad_id") or "").strip()
        campaign_id = str(plan.get("reuse_campaign_id") or after.get("reuse_campaign_id") or old_ids.get("campaign_id") or "").strip()
        old_adset_id = str(old_ids.get("adset_id") or "").strip()
        if not new_ad_id or not old_ad_id or new_ad_id == old_ad_id:
            raise GrowthValidationError("rebuild_delete_ad_identity_invalid")

        new_ad = self._read_ad(new_ad_id)
        old_ad = self._read_ad(old_ad_id)
        if str(new_ad.get("campaign_id") or "") != campaign_id:
            raise GrowthValidationError("rebuild_new_ad_campaign_mismatch")
        if str(old_ad.get("campaign_id") or "") != campaign_id or str(old_ad.get("adset_id") or "") != old_adset_id:
            raise GrowthValidationError("rebuild_source_ad_hierarchy_mismatch")

        delete_acknowledged = False
        delete_error = ""
        try:
            response = self.session.delete(
                f"{self.graph_root}/{old_ad_id}",
                data={"access_token": self.access_token}, timeout=25,
            )
            delete_acknowledged = self._body(response).get("success") is True
            if not delete_acknowledged:
                delete_error = "meta_delete_not_acknowledged"
        except Exception as exc:
            delete_error = type(exc).__name__

        try:
            response = self.session.get(
                f"{self.graph_root}/{old_ad_id}",
                params={"access_token": self.access_token, "fields": "id,status,effective_status"},
                timeout=25,
            )
            verified_deleted = self._is_missing(response, self._body(response))
        except Exception:
            verified_deleted = False
        result = {
            "experiment_id": experiment_id, "plan_id": plan_id,
            "execution_task_id": str(task["execution_task_id"] or ""),
            "new_ad_id": new_ad_id, "source_ad_id": old_ad_id,
            "source_ad_delete_acknowledged": delete_acknowledged,
            "source_ad_deleted": verified_deleted,
            "status": "SUCCESS" if verified_deleted else "MANUAL_REVIEW",
            "error": "" if verified_deleted else (delete_error or "meta_delete_readback_uncertain"),
            "automatic_retry": False, "request_id": request_id,
        }
        with self.conn:
            self.conn.execute(
                "UPDATE ad_experiment SET updated_at=? WHERE experiment_id=?",
                (utc_now(), experiment_id),
            )
        return result

    def _read_ad(self, ad_id: str) -> Dict[str, Any]:
        response = self.session.get(
            f"{self.graph_root}/{ad_id}",
            params={
                "access_token": self.access_token,
                "fields": "id,name,status,effective_status,campaign_id,adset_id,creative{id}",
            },
            timeout=25,
        )
        body = self._body(response)
        if str(body.get("id") or "") != ad_id:
            raise GrowthValidationError("rebuild_ad_readback_failed")
        return body

    @staticmethod
    def _body(response: Any) -> Dict[str, Any]:
        body = response.json() if hasattr(response, "json") else {}
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _is_missing(response: Any, body: Dict[str, Any]) -> bool:
        if int(getattr(response, "status_code", 0) or 0) in {404, 410}:
            return True
        if "DELETED" in {
            str(body.get("status") or "").upper(),
            str(body.get("effective_status") or "").upper(),
        }:
            return True
        error = dict(body.get("error") or {})
        message = str(error.get("message") or "").lower()
        return str(error.get("error_subcode") or "") == "33" or any(
            marker in message for marker in (
                "unsupported get request", "object does not exist", "cannot be loaded",
            )
        )
