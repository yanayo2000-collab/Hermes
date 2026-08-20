from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.growth.ad_experiment_service import AdExperimentService
from app.growth.common import canonical_json, decode_json, utc_now


NON_RETRYABLE_PREPARE_REASONS = {
    "rebuild_source_daily_budget_out_of_range",
    "active_rebuild_requires_active_source_campaign",
    "rebuild_source_account_mismatch",
    "rebuild_source_creative_mismatch",
    "rebuild_source_not_tugao",
}


class ApprovedRebuildAutopilot:
    """Continue an authorized rebuild after its replacement image is approved.

    The durable experiment, operation action, execution task, receipts, and
    cleanup idempotency record are the state machine. Browser storage is never
    used as execution authority.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        session: Any,
        base_url: str,
        internal_token: str,
        legacy_batch_prefixes: Iterable[str] = (),
        timeout_seconds: float = 25.0,
    ) -> None:
        self.conn = conn
        self.session = session
        self.base_url = str(base_url or "http://127.0.0.1:8011").rstrip("/")
        self.internal_token = str(internal_token or "").strip()
        self.legacy_batch_prefixes = frozenset(
            str(prefix or "").strip().rstrip(":")
            for prefix in legacy_batch_prefixes
            if str(prefix or "").strip()
        )
        self.timeout_seconds = max(1.0, float(timeout_seconds or 25.0))
        self.experiments = AdExperimentService(conn)

    def advance(self, *, limit: int = 20, allow_live: bool = True) -> Dict[str, Any]:
        if not allow_live or not self.internal_token:
            return {"processed": 0, "results": [], "deferred": "live_channel_closed"}
        authorization_sql = "json_extract(q.material_refs_json,'$.auto_rebuild_on_approval')=1"
        authorization_params: List[Any] = []
        if self.legacy_batch_prefixes:
            legacy_clauses = []
            for prefix in sorted(self.legacy_batch_prefixes):
                legacy_clauses.append(
                    """EXISTS (
                        SELECT 1
                        FROM growth_decision d
                        JOIN growth_idempotency_record intent
                          ON intent.route_key='decision.create'
                         AND json_extract(intent.response_json,'$.decision_id')=d.decision_id
                        WHERE d.target_type='EXPERIMENT'
                          AND d.target_id=e.experiment_id
                          AND intent.idempotency_key LIKE ?
                    )"""
                )
                authorization_params.append(f"{prefix}:%")
            authorization_sql = f"({authorization_sql} OR {' OR '.join(legacy_clauses)})"
        rows = self.conn.execute(
            f"""
            SELECT DISTINCT e.experiment_id
            FROM ad_experiment e
            JOIN creative_pro_work_queue q
              ON json_extract(q.material_refs_json,'$.growth_experiment_id')=e.experiment_id
            WHERE lower(q.status)!='deleted'
              AND coalesce(json_extract(e.hypothesis_json,'$.rebuild_auto_blocked_reason'),'')=''
              AND {authorization_sql}
              AND EXISTS (
                  SELECT 1
                  FROM creative_generated_images i
                  JOIN creative_review_records r ON r.image_id=i.image_id
                  WHERE upper(r.review_status)='APPROVED'
                    AND (
                        i.request_id=json_extract(q.generation_plan_json,'$.generation_request_id')
                        OR json_extract(i.metadata_json,'$.job_id')=q.job_id
                        OR json_extract(i.metadata_json,'$.creative_pro_job_id')=q.job_id
                    )
              )
              AND NOT EXISTS (
                  SELECT 1 FROM growth_idempotency_record done
                  WHERE done.route_key='ad_experiment.rebuild_source_ad_delete'
                    AND json_extract(done.response_json,'$.experiment_id')=e.experiment_id
                    AND json_extract(done.response_json,'$.status')='SUCCESS'
              )
            ORDER BY e.updated_at,e.experiment_id
            LIMIT ?
            """,
            (*authorization_params, max(1, min(int(limit or 20), 100))),
        ).fetchall()
        results: List[Dict[str, Any]] = []
        for row in rows:
            experiment_id = str(row["experiment_id"] or "")
            try:
                result = self._advance_one(experiment_id)
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower():
                    raise
                result = {
                    "experiment_id": experiment_id,
                    "status": "DEFERRED",
                    "reason": f"OperationalError:{str(exc)[:180]}",
                }
            except Exception as exc:  # fail one item without stopping the worker lane
                result = {
                    "experiment_id": experiment_id,
                    "status": "DEFERRED",
                    "reason": f"{type(exc).__name__}:{str(exc)[:180]}",
                }
            results.append(result)
        return {"processed": len(results), "results": results}

    def _advance_one(self, experiment_id: str) -> Dict[str, Any]:
        experiment = self.experiments.get(experiment_id)
        approved = self.experiments.latest_approved_creative(experiment_id)
        image_id = str(approved.get("image_id") or "")
        if not image_id:
            return {"experiment_id": experiment_id, "status": "WAITING_CREATIVE_APPROVAL"}

        initial_status, intent_source = self._authorized_initial_status(experiment_id, experiment)
        if initial_status not in {"PAUSED", "ACTIVE"}:
            return {
                "experiment_id": experiment_id,
                "status": "NEEDS_AUTHORIZATION",
                "reason": "durable_rebuild_initial_status_missing",
            }
        self._persist_intent(experiment_id, experiment, initial_status, intent_source)

        action = self._latest_create_action(experiment_id)
        if not action:
            try:
                prepared = self._post(
                    f"/api/ops/ad-data-dashboard/experiments/{experiment_id}/rebuild-plan/prepare",
                    {
                        "creation_scope": "REUSE_CAMPAIGN_NEW_ADSET",
                        "initial_status": initial_status,
                        "approved_image_id": image_id,
                    },
                    key=f"approved-rebuild:{experiment_id}:{image_id}:{initial_status}:prepare",
                )
            except RuntimeError as exc:
                reason = self._non_retryable_prepare_reason(str(exc))
                if not reason:
                    raise
                self._persist_block(experiment_id, reason)
                return {
                    "experiment_id": experiment_id,
                    "status": "NEEDS_PARAMETER_CONFIRMATION",
                    "reason": reason,
                }
            action = self._action(str(prepared.get("plan_id") or prepared.get("operation_action_id") or ""))
        if not action:
            raise RuntimeError("rebuild_plan_not_persisted")

        plan_id = str(action["operation_action_id"] or "")
        action_status = str(action["status"] or "").upper()
        if action_status in {"CANCELLED", "FAILED", "INVALIDATED"}:
            return {
                "experiment_id": experiment_id,
                "status": "MANUAL_REVIEW",
                "plan_id": plan_id,
                "reason": f"rebuild_plan_{action_status.lower()}",
            }
        approval = self.conn.execute(
            "SELECT * FROM growth_operation_approval WHERE operation_action_id=?",
            (plan_id,),
        ).fetchone()
        approval_status = str((approval["status"] if approval else "") or "").upper()
        if approval_status in {"", "PROPOSED", "PENDING"}:
            self._post(
                f"/api/ops/ad-data-dashboard/meta-plans/{plan_id}/approve",
                {"confirmation": "APPROVE_EXACT_PLAN"},
                key=f"approved-rebuild:{experiment_id}:{plan_id}:approve",
            )

        task = self._latest_task(plan_id)
        if not task:
            self._post(
                f"/api/ops/ad-data-dashboard/meta-plans/{plan_id}/execute",
                {"execution_mode": "dry_run"},
                key=f"approved-rebuild:{experiment_id}:{plan_id}:dry-run",
            )
            self._post(
                f"/api/ops/ad-data-dashboard/meta-plans/{plan_id}/execute",
                {"execution_mode": "live", "confirmation": "CREATE_PAUSED_OBJECTS"},
                key=f"approved-rebuild:{experiment_id}:{plan_id}:live",
            )
            task = self._latest_task(plan_id)
        if not task:
            raise RuntimeError("rebuild_execution_task_not_persisted")

        task_status = str(task["status"] or "").upper()
        if task_status in {"QUEUED", "RUNNING", "VERIFYING"}:
            return {
                "experiment_id": experiment_id,
                "status": "EXECUTION_QUEUED",
                "plan_id": plan_id,
                "execution_task_id": str(task["execution_task_id"] or ""),
            }
        if task_status == "MANUAL_REVIEW":
            repair = self._repair_page_if_safe(experiment, action, task)
            if repair:
                return {"experiment_id": experiment_id, "status": "PAGE_REPAIR_QUEUED", **repair}
            return {
                "experiment_id": experiment_id,
                "status": "MANUAL_REVIEW",
                "plan_id": plan_id,
                "reason": str(task["error_message"] or "")[:180],
            }
        if task_status != "SUCCESS" or str(action["status"] or "").upper() != "VERIFIED":
            return {
                "experiment_id": experiment_id,
                "status": "WAITING_VERIFIED_READBACK",
                "plan_id": plan_id,
                "execution_task_status": task_status,
            }

        deleted = self._post(
            f"/api/ops/ad-data-dashboard/experiments/{experiment_id}/rebuild-source-ad/delete",
            {"plan_id": plan_id, "confirmation": "DELETE_SOURCE_AD_AFTER_VERIFIED_REBUILD"},
            key=f"approved-rebuild:{experiment_id}:{plan_id}:delete-source",
        )
        if str(deleted.get("status") or "").upper() != "SUCCESS" or deleted.get("source_ad_deleted") is not True:
            return {
                "experiment_id": experiment_id,
                "status": "MANUAL_REVIEW",
                "plan_id": plan_id,
                "reason": str(deleted.get("error") or "source_ad_delete_unverified")[:180],
            }
        return {
            "experiment_id": experiment_id,
            "status": "SUCCESS",
            "plan_id": plan_id,
            "new_ad_id": str(deleted.get("new_ad_id") or ""),
            "source_ad_id": str(deleted.get("source_ad_id") or ""),
        }

    def _authorized_initial_status(
        self, experiment_id: str, experiment: Dict[str, Any],
    ) -> Tuple[str, str]:
        hypothesis = dict(experiment.get("hypothesis_json") or {})
        explicit = str(hypothesis.get("rebuild_initial_status") or "").upper()
        if explicit in {"PAUSED", "ACTIVE"}:
            return explicit, "experiment_hypothesis"

        job = self.conn.execute(
            """
            SELECT material_refs_json FROM creative_pro_work_queue
            WHERE json_extract(material_refs_json,'$.growth_experiment_id')=?
              AND lower(status)!='deleted'
            ORDER BY created_at DESC LIMIT 1
            """,
            (experiment_id,),
        ).fetchone()
        refs = decode_json(job["material_refs_json"], {}) if job else {}
        ref_status = str(refs.get("rebuild_initial_status") or "").upper()
        if (
            refs.get("auto_rebuild_on_approval") is True
            and str(refs.get("rebuild_authorized_at") or "").strip()
            and ref_status in {"PAUSED", "ACTIVE"}
        ):
            return ref_status, "creative_job_authorization"

        batch_prefix = self._decision_batch_prefix(experiment_id)
        if not batch_prefix or batch_prefix not in self.legacy_batch_prefixes:
            return "", ""
        statuses = {
            str(row["initial_status"] or "").upper()
            for row in self.conn.execute(
                """
                SELECT json_extract(e.hypothesis_json,'$.rebuild_initial_status') AS initial_status
                FROM growth_idempotency_record r
                JOIN growth_decision d
                  ON d.decision_id=json_extract(r.response_json,'$.decision_id')
                JOIN ad_experiment e
                  ON e.experiment_id=d.target_id AND d.target_type='EXPERIMENT'
                WHERE r.route_key='decision.create'
                  AND r.idempotency_key LIKE ?
                """,
                (f"{batch_prefix}:%",),
            ).fetchall()
            if str(row["initial_status"] or "").upper() in {"PAUSED", "ACTIVE"}
        }
        if len(statuses) == 1:
            return next(iter(statuses)), "legacy_batch_sibling_status"
        return "", ""

    def _decision_batch_prefix(self, experiment_id: str) -> str:
        decision = self.conn.execute(
            """
            SELECT decision_id FROM growth_decision
            WHERE target_type='EXPERIMENT' AND target_id=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (experiment_id,),
        ).fetchone()
        if not decision:
            return ""
        record = self.conn.execute(
            """
            SELECT idempotency_key FROM growth_idempotency_record
            WHERE route_key='decision.create'
              AND json_extract(response_json,'$.decision_id')=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (str(decision["decision_id"] or ""),),
        ).fetchone()
        key = str((record["idempotency_key"] if record else "") or "")
        if not key.startswith("gle-bulk-rebuild:") or key.count(":") < 3:
            return ""
        return key.rsplit(":", 2)[0]

    def _persist_intent(
        self, experiment_id: str, experiment: Dict[str, Any], initial_status: str, source: str,
    ) -> None:
        hypothesis = dict(experiment.get("hypothesis_json") or {})
        if (
            str(hypothesis.get("rebuild_initial_status") or "").upper() == initial_status
            and hypothesis.get("auto_rebuild_on_approval") is True
        ):
            return
        hypothesis.update({
            "rebuild_initial_status": initial_status,
            "auto_rebuild_on_approval": True,
            "rebuild_auto_intent_source": source,
        })
        with self.conn:
            self.conn.execute(
                "UPDATE ad_experiment SET hypothesis_json=?,updated_at=? WHERE experiment_id=?",
                (canonical_json(hypothesis), utc_now(), experiment_id),
            )

    @staticmethod
    def _non_retryable_prepare_reason(error: str) -> str:
        text = str(error or "")
        return next((reason for reason in sorted(NON_RETRYABLE_PREPARE_REASONS) if reason in text), "")

    def _persist_block(self, experiment_id: str, reason: str) -> None:
        row = self.conn.execute(
            "SELECT hypothesis_json FROM ad_experiment WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        hypothesis = decode_json(row["hypothesis_json"], {}) if row else {}
        if str(hypothesis.get("rebuild_auto_blocked_reason") or "") == reason:
            return
        hypothesis.update({
            "rebuild_auto_blocked_reason": reason,
            "rebuild_auto_blocked_at": utc_now(),
            "rebuild_auto_next_step": "CONFIRM_REBUILD_PARAMETERS",
        })
        with self.conn:
            self.conn.execute(
                "UPDATE ad_experiment SET hypothesis_json=?,updated_at=? WHERE experiment_id=?",
                (canonical_json(hypothesis), utc_now(), experiment_id),
            )

    def _latest_create_action(self, experiment_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM growth_operation_action
            WHERE action_type='CREATE_PAUSED_AD'
              AND json_extract(payload_json,'$.experiment_id')=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (experiment_id,),
        ).fetchone()

    def _action(self, plan_id: str) -> Optional[sqlite3.Row]:
        if not plan_id:
            return None
        return self.conn.execute(
            "SELECT * FROM growth_operation_action WHERE operation_action_id=?",
            (plan_id,),
        ).fetchone()

    def _latest_task(self, plan_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM meta_execution_task WHERE operation_action_id=? ORDER BY created_at DESC LIMIT 1",
            (plan_id,),
        ).fetchone()

    def _repair_page_if_safe(
        self, experiment: Dict[str, Any], action: sqlite3.Row, task: sqlite3.Row,
    ) -> Dict[str, str]:
        error = str(task["error_message"] or "")
        lowered = error.lower()
        if not any(marker in lowered for marker in (
            "3858749", "1815645", "page creative permission",
            "does not have ads permissions", "doesn't have ads permissions",
        )):
            return {}
        plan_id = str(action["operation_action_id"] or "")
        action_payload = decode_json(action["payload_json"], {})
        plan = dict(action_payload.get("plan") or {})
        account_id = str(plan.get("target_account_id") or experiment.get("account_id") or "").removeprefix("act_")
        country = str(experiment.get("country") or "").upper()
        hypothesis = dict(experiment.get("hypothesis_json") or {})
        page_match = re.search(r'\\?"page_id\\?"\s*:\s*\\?"(\d+)\\?"', error)
        rejected_page_id = str((page_match.group(1) if page_match else "") or hypothesis.get("page_id") or "")
        eligibility = self._post(
            "/api/ops/ad-data-dashboard/meta-accounts/page-eligibility",
            {"account_id": account_id, "country": country, "force": False},
            key=f"approved-rebuild:{plan_id}:page-eligibility",
        )
        verified = [
            page for page in list(eligibility.get("pages") or [])
            if page and page.get("eligible") is True and page.get("permission_verified") is True
            and str(page.get("page_id") or "") != rejected_page_id
        ]
        default_page_id = str(eligibility.get("default_page_id") or "")
        selected = next((page for page in verified if str(page.get("page_id") or "") == default_page_id), None)
        if selected is None and len(verified) == 1:
            selected = verified[0]
        if selected is None:
            return {}
        repaired = self._post(
            f"/api/ops/ad-data-dashboard/meta-plans/{plan_id}/repair-page-plan",
            {"target_page_id": str(selected.get("page_id") or ""), "confirmation": "APPROVE_REPAIR_PLAN"},
            key=f"approved-rebuild:{plan_id}:repair-page",
        )
        return {
            "plan_id": str(repaired.get("repair_plan_id") or plan_id),
            "execution_task_id": str(repaired.get("execution_task_id") or ""),
            "page_id": str(selected.get("page_id") or ""),
        }

    def _post(self, path: str, body: Dict[str, Any], *, key: str) -> Dict[str, Any]:
        request_id = f"{key}:request"
        response = self.session.post(
            f"{self.base_url}{path}",
            json=body,
            headers={
                "x-ops-internal-token": self.internal_token,
                "Idempotency-Key": key,
                "X-Request-ID": request_id,
            },
            timeout=self.timeout_seconds,
        )
        payload = response.json() if hasattr(response, "json") else {}
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else ""
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("code") or ""
            raise RuntimeError(f"internal_api_{response.status_code}:{str(detail or 'request_failed')[:160]}")
        if not isinstance(payload, dict):
            raise RuntimeError("internal_api_response_invalid")
        return payload
