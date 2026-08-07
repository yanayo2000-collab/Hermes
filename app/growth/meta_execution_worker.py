from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable, Dict, Protocol

from app.growth.execution_service import ExecutionTaskService
from app.growth.common import payload_hash
from app.growth.errors import GrowthValidationError


MAX_READBACK_RECONCILIATION_ATTEMPTS = 8


META_EXECUTION_STEPS = (
    "CAMPAIGN_CREATE",
    "ADSET_CREATE",
    "IMAGE_UPLOAD",
    "CREATIVE_CREATE",
    "AD_CREATE",
)

ACTION_EXECUTION_STEPS = {
    "CREATE_EXPERIMENT": META_EXECUTION_STEPS,
    "CREATE_PAUSED_AD": META_EXECUTION_STEPS,
    "REPLACE_CREATIVE": ("CREATIVE_CREATE", "AD_CREATIVE_UPDATE"),
    "INCREASE_BUDGET": ("BUDGET_UPDATE",),
    "SCALE_UP": ("BUDGET_UPDATE",),
    "DECREASE_BUDGET": ("BUDGET_UPDATE",),
    "REDUCE_BUDGET": ("BUDGET_UPDATE",),
    "PAUSE": ("STATUS_UPDATE",),
    "PAUSE_AD": ("STATUS_UPDATE",),
    "PAUSE_ADSET": ("STATUS_UPDATE",),
    "REACTIVATE_AD": ("STATUS_UPDATE",),
    "SET_COST_CAP": ("BID_STRATEGY_UPDATE",),
}


def execution_steps_for(action_type: str, payload: Dict[str, Any]) -> tuple[str, ...]:
    plan = dict(payload.get("plan") or {})
    cells = list(plan.get("cells") or [])
    if str(action_type or "").upper() == "REPLACE_CREATIVE":
        planned = dict(plan.get("steps") or {})
        if all(step in planned for step in ("IMAGE_UPLOAD", "CREATIVE_CREATE", "AD_CREATIVE_UPDATE")):
            return ("IMAGE_UPLOAD", "CREATIVE_CREATE", "AD_CREATIVE_UPDATE")
    if str(action_type or "").upper() == "REACTIVATE_AD":
        if cells and dict(plan.get("steps") or {}).get("CAMPAIGN_STATUS_UPDATE"):
            steps = ["CAMPAIGN_STATUS_UPDATE"]
            for index, raw_cell in enumerate(cells, start=1):
                cell = dict(raw_cell or {})
                cell_key = str(cell.get("cell_key") or f"C{index}").strip().upper()
                cell_steps = dict(cell.get("steps") or {})
                if all(name in cell_steps for name in ("ADSET_STATUS_UPDATE", "AD_STATUS_UPDATE")):
                    steps.extend((f"{cell_key}_ADSET_STATUS_UPDATE", f"{cell_key}_AD_STATUS_UPDATE"))
                else:
                    return ()
            return tuple(steps)
        delivery_steps = (
            "CAMPAIGN_STATUS_UPDATE",
            "ADSET_STATUS_UPDATE",
            "AD_STATUS_UPDATE",
        )
        planned = dict(plan.get("steps") or {})
        if all(step in planned for step in delivery_steps):
            return delivery_steps
    if str(action_type or "").upper() == "CREATE_PAUSED_AD" and cells:
        if str(plan.get("test_variable") or "").lower() == "audience_strategy":
            # Audience experiments freeze one creative. Creating one creative per
            # cell would silently introduce a second variable into the test.
            steps = ["CAMPAIGN_CREATE", "C1_IMAGE_UPLOAD", "C1_CREATIVE_CREATE"]
            for index, cell in enumerate(cells, start=1):
                cell_key = str(dict(cell).get("cell_key") or f"C{index}").strip().upper()
                steps.extend((f"{cell_key}_ADSET_CREATE", f"{cell_key}_AD_CREATE"))
            steps.append("STUDY_CREATE")
            return tuple(steps)
        if str(plan.get("test_variable") or "").lower() == "copy_variant":
            steps = ["CAMPAIGN_CREATE"]
            for index, cell in enumerate(cells, start=1):
                cell_key = str(dict(cell).get("cell_key") or f"C{index}").strip().upper()
                steps.extend(f"{cell_key}_{step}" for step in ("IMAGE_UPLOAD", "CREATIVE_CREATE", "ADSET_CREATE", "AD_CREATE"))
            steps.append("STUDY_CREATE")
            return tuple(steps)
        steps = ["CAMPAIGN_CREATE"]
        for index, cell in enumerate(cells, start=1):
            cell_key = str(dict(cell).get("cell_key") or f"C{index}").strip().upper()
            steps.extend(
                f"{cell_key}_{step}"
                for step in ("IMAGE_UPLOAD", "CREATIVE_CREATE", "ADSET_CREATE", "AD_CREATE")
            )
        return tuple(steps)
    return tuple(ACTION_EXECUTION_STEPS.get(str(action_type or "").upper()) or ())


def is_delivery_status_step(step: str) -> bool:
    normalized = str(step or "").strip().upper()
    return normalized in {"CAMPAIGN_STATUS_UPDATE", "ADSET_STATUS_UPDATE", "AD_STATUS_UPDATE"} or any(
        normalized.endswith(f"_{suffix}")
        for suffix in ("ADSET_STATUS_UPDATE", "AD_STATUS_UPDATE")
    )


class MetaExecutionAdapter(Protocol):
    def execute_step(self, step: str, payload: Dict[str, Any], object_ids: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def verify_step(self, step: str, payload: Dict[str, Any], object_ids: Dict[str, Any]) -> Dict[str, Any]:
        ...


class MetaExecutionWorker:
    """Run one claimed task. It never retries a Meta write operation."""

    def __init__(
        self,
        tasks: ExecutionTaskService,
        adapter: MetaExecutionAdapter,
        *,
        worker_id: str,
        execution_mode: str = "dry_run",
        heartbeat_interval_seconds: float = 30.0,
    ) -> None:
        normalized_mode = str(execution_mode or "").strip().lower()
        if normalized_mode not in {"fake", "dry_run", "live"}:
            raise ValueError("real_meta_writes_disabled")
        if normalized_mode == "live" and not bool(getattr(adapter, "live_writes_enabled", False)):
            raise ValueError("real_meta_writes_disabled")
        self.tasks = tasks
        self.adapter = adapter
        self.worker_id = worker_id
        self.execution_mode = normalized_mode
        self.heartbeat_interval_seconds = max(0.01, float(heartbeat_interval_seconds or 30.0))

    def _call_with_heartbeat(self, task_id: str, callback: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        """Keep the lease fresh while one network call is in flight; never replay it."""
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="growth-meta-step") as executor:
            future: Future[Dict[str, Any]] = executor.submit(callback)
            while True:
                try:
                    return future.result(timeout=self.heartbeat_interval_seconds)
                except FutureTimeout:
                    self.tasks.heartbeat(task_id, self.worker_id)

    def _execute_step(
        self, task_id: str, step: str, payload: Dict[str, Any], object_ids: Dict[str, Any],
    ) -> Dict[str, Any]:
        try:
            return dict(self._call_with_heartbeat(
                task_id, lambda: self.adapter.execute_step(step, payload, object_ids),
            ) or {})
        except Exception as exc:
            result = {
                "status": "UNKNOWN",
                "error": "adapter_execute_exception",
                "exception_type": type(exc).__name__,
            }
            if isinstance(exc, GrowthValidationError):
                result["error_detail"] = str(exc)
            return result

    def _verify_step(
        self, task_id: str, step: str, payload: Dict[str, Any], object_ids: Dict[str, Any],
    ) -> Dict[str, Any]:
        try:
            return dict(self._call_with_heartbeat(
                task_id, lambda: self.adapter.verify_step(step, payload, object_ids),
            ) or {})
        except Exception as exc:
            result = {
                "status": "UNKNOWN",
                "error": "adapter_verify_exception",
                "exception_type": type(exc).__name__,
            }
            if isinstance(exc, GrowthValidationError):
                result["error_detail"] = str(exc)
            return result

    def run_once(self) -> Dict[str, Any]:
        self.tasks.expire_queued_live_tasks(actor=self.worker_id)
        task = self.tasks.claim_next(self.worker_id, execution_mode=self.execution_mode)
        if task is None:
            return {"status": "IDLE"}
        task_id = task["execution_task_id"]
        payload = dict(task["payload_json"])
        object_ids = dict(task["meta_object_ids_json"])
        continuation = dict(payload.get("continuation") or {})
        completed_steps: set[str] = set()
        reused_steps: set[str] = set()
        verified_steps: set[str] = set()
        if continuation:
            plan = dict(payload.get("plan") or {})
            continuation_action_type = str(payload.get("action_type") or "").strip().upper()
            continuation_steps = tuple(str(item or "").upper() for item in continuation.get("completed_steps") or ())
            continuation_reused_steps = tuple(
                str(item or "").upper() for item in continuation.get("reused_steps") or ()
            )
            verified_steps = {
                str(item or "").upper() for item in continuation.get("verified_steps") or ()
            }
            planned_steps = execution_steps_for(str(payload.get("action_type") or ""), payload)
            verification_only = continuation.get("verification_only") is True
            continuation_ids = dict(continuation.get("meta_object_ids") or {})
            continuation_identity_valid = bool(
                str(continuation_ids.get("campaign_id") or "").strip()
            )
            if continuation_action_type == "REPLACE_CREATIVE":
                continuation_identity_valid = bool(
                    str(continuation_ids.get("image_hash") or "").strip()
                    and str(continuation_ids.get("creative_id") or "").strip()
                )
            reused_steps_valid = all(
                step in planned_steps
                and step.endswith("_ADSET_CREATE")
                and str(continuation_ids.get(f"{step.removesuffix('_ADSET_CREATE').lower()}_adset_id") or "").strip()
                for step in continuation_reused_steps
            )
            if (
                str(continuation.get("plan_hash") or "") != payload_hash(plan)
                or not continuation_steps
                or continuation_steps != planned_steps[:len(continuation_steps)]
                or len(continuation_steps) > len(planned_steps)
                or (len(continuation_steps) == len(planned_steps) and not verification_only)
                or not continuation_identity_valid
                or len(set(continuation_reused_steps)) != len(continuation_reused_steps)
                or bool(set(continuation_steps).intersection(continuation_reused_steps))
                or not reused_steps_valid
                or not verified_steps.issubset(set(continuation_steps))
            ):
                return self.tasks.transition(
                    task_id, "MANUAL_REVIEW", worker_id=self.worker_id,
                    current_step="PLAN", meta_object_ids=object_ids,
                    error_code="continuation_evidence_invalid",
                    error_message="same_plan_continuation_evidence_invalid",
                )
            object_ids.update(continuation_ids)
            completed_steps = set(continuation_steps)
            reused_steps = set(continuation_reused_steps)
        action = self.tasks.get_operation_action(task["operation_action_id"])
        action_type = str(action.get("action_type") or payload.get("action_type") or "").strip().upper()
        steps = execution_steps_for(action_type, payload)
        if not steps:
            return self.tasks.transition(
                task_id, "MANUAL_REVIEW", worker_id=self.worker_id,
                current_step="PLAN", meta_object_ids=object_ids,
                error_code="unsupported_meta_action", error_message=action_type,
            )
        payload.setdefault("action_type", action_type)
        payload.setdefault("target_type", action.get("target_type"))
        payload.setdefault("target_id", action.get("target_id"))
        for step in steps:
            self.tasks.heartbeat(task_id, self.worker_id)
            if step in completed_steps or step in reused_steps:
                if step in verified_steps:
                    continue
                verification = self._verify_step(task_id, step, payload, object_ids)
                verified = str(verification.get("status") or "UNKNOWN").upper()
                object_ids.update(dict(verification.get("meta_object_ids") or {}))
                self.tasks.record_receipt(
                    task_id, step_name=step,
                    step_status="VERIFIED" if verified == "SUCCESS" else "UNKNOWN",
                    step_result={
                        "source": (
                            "existing_order_page_repair_reuse"
                            if step in reused_steps
                            else "same_plan_continuation_get_readback"
                        ),
                    },
                    meta_object_ids=object_ids, verification_result=verification,
                )
                if verified == "SUCCESS":
                    continue
                if str(verification.get("error") or "") == "adapter_verify_exception":
                    return self.tasks.defer_step_readback_reconciliation(
                        task_id, worker_id=self.worker_id, current_step=step,
                        meta_object_ids=object_ids,
                        error_message="step_get_readback_retry_required",
                    )
                return self.tasks.transition(
                    task_id, "MANUAL_REVIEW", worker_id=self.worker_id, current_step=step,
                    meta_object_ids=object_ids, error_code="continuation_verification_uncertain",
                    error_message=str(verification.get("error") or verified),
                )
            result = self._execute_step(task_id, step, payload, object_ids)
            status = str(result.get("status") or "UNKNOWN").upper()
            object_ids.update(dict(result.get("meta_object_ids") or {}))
            if status == "SUCCESS" and is_delivery_status_step(step):
                verification = self._verify_step(task_id, step, payload, object_ids)
                verified = str(verification.get("status") or "UNKNOWN").upper()
                object_ids.update(dict(verification.get("meta_object_ids") or {}))
                self.tasks.record_receipt(
                    task_id, step_name=step,
                    step_status="VERIFIED" if verified == "SUCCESS" else "UNKNOWN",
                    step_result=result, meta_object_ids=object_ids,
                    verification_result=verification,
                )
                if verified == "SUCCESS":
                    continue
                if str(verification.get("error") or "") == "adapter_verify_exception":
                    return self.tasks.defer_step_readback_reconciliation(
                        task_id, worker_id=self.worker_id, current_step=step,
                        meta_object_ids=object_ids,
                        error_message="step_get_readback_retry_required",
                    )
                return self.tasks.transition(
                    task_id, "MANUAL_REVIEW", worker_id=self.worker_id, current_step=step,
                    meta_object_ids=object_ids, error_code="meta_result_uncertain",
                    error_message=str(verification.get("error") or verified),
                )
            if status == "SUCCESS":
                self.tasks.record_receipt(
                    task_id, step_name=step, step_status="SUCCESS", step_result=result,
                    meta_object_ids=object_ids,
                )
                continue
            verification = self._verify_step(task_id, step, payload, object_ids)
            verified = str(verification.get("status") or "UNKNOWN").upper()
            self.tasks.record_receipt(
                task_id, step_name=step, step_status="VERIFIED" if verified == "SUCCESS" else "UNKNOWN",
                step_result=result, meta_object_ids=object_ids, verification_result=verification,
            )
            if verified == "SUCCESS":
                object_ids.update(dict(verification.get("meta_object_ids") or {}))
                continue
            return self.tasks.transition(
                task_id, "MANUAL_REVIEW", worker_id=self.worker_id, current_step=step,
                meta_object_ids=object_ids, error_code="meta_result_uncertain",
                error_message=str(result.get("error_detail") or result.get("error") or status),
            )
        verifying = self.tasks.transition(
            task_id, "VERIFYING", worker_id=self.worker_id, current_step="VERIFY",
            meta_object_ids=object_ids,
        )
        verification = self._verify_step(task_id, "VERIFY", payload, object_ids)
        verified = str(verification.get("status") or "UNKNOWN").upper()
        self.tasks.record_receipt(
            task_id, step_name="VERIFY", step_status="VERIFIED" if verified == "SUCCESS" else "UNKNOWN",
            step_result={}, meta_object_ids=object_ids, verification_result=verification,
        )
        if verified != "SUCCESS":
            if str(verification.get("error") or "") == "adapter_verify_exception":
                return self.tasks.defer_final_readback_reconciliation(
                    task_id,
                    worker_id=self.worker_id,
                    current_step="VERIFY",
                    meta_object_ids=object_ids,
                    error_message="final_get_readback_retry_required",
                )
            return self.tasks.transition(
                task_id, "MANUAL_REVIEW", worker_id=self.worker_id, current_step="VERIFY",
                meta_object_ids=object_ids, error_code="final_verification_uncertain",
                error_message=str(verification.get("error") or verified),
            )
        object_ids.update(dict(verification.get("meta_object_ids") or {}))
        self.tasks.record_receipt(
            task_id, step_name="RECEIPT", step_status="SUCCESS",
            step_result={"final_status": "SUCCESS"}, meta_object_ids=object_ids,
            verification_result=verification,
        )
        return self.tasks.transition(
            task_id, "SUCCESS", worker_id=self.worker_id, current_step="RECEIPT",
            meta_object_ids=object_ids,
        )

    def reconcile_once(self) -> Dict[str, Any]:
        """GET-verify one uncertain task. A write operation is never repeated here."""
        task = self.tasks.claim_reconciliation(
            self.worker_id, execution_mode=self.execution_mode,
        )
        if task is None:
            return {"status": "IDLE"}
        task_id = task["execution_task_id"]
        payload = dict(task["payload_json"])
        object_ids = dict(task["meta_object_ids_json"])
        current_step = str(task.get("current_step") or "VERIFY").upper()
        step = current_step if is_delivery_status_step(current_step) else "VERIFY"
        receipt_step = f"RECONCILE_{step}" if is_delivery_status_step(step) else "RECONCILE"
        verification = self._verify_step(task_id, step, payload, object_ids)
        verified = str(verification.get("status") or "UNKNOWN").upper()
        object_ids.update(dict(verification.get("meta_object_ids") or {}))
        self.tasks.record_receipt(
            task_id, step_name=receipt_step, step_status="VERIFIED" if verified == "SUCCESS" else "UNKNOWN",
            step_result={}, meta_object_ids=object_ids, verification_result=verification,
        )
        if verified != "SUCCESS":
            attempts = self.tasks.conn.execute(
                """
                SELECT COUNT(*) FROM meta_execution_task_receipt
                WHERE execution_task_id=? AND step_name=? AND step_status='UNKNOWN'
                """,
                (task_id, receipt_step),
            ).fetchone()[0]
            if (
                str(verification.get("error") or "") == "adapter_verify_exception"
                and int(attempts or 0) < MAX_READBACK_RECONCILIATION_ATTEMPTS
            ):
                return self.tasks.defer_reconciliation_retry(
                    task_id, worker_id=self.worker_id, current_step=step,
                    meta_object_ids=object_ids,
                    error_message="rate_limit_readback_reconciliation_pending",
                )
            return self.tasks.transition(
                task_id, "MANUAL_REVIEW", worker_id=self.worker_id, current_step="RECONCILE",
                meta_object_ids=object_ids, error_code="reconciliation_uncertain",
                error_message=str(verification.get("error") or verified),
            )
        if is_delivery_status_step(step):
            return self.tasks.resume_after_reconciled_step(
                task_id, worker_id=self.worker_id, current_step=step,
                meta_object_ids=object_ids,
            )
        self.tasks.record_receipt(
            task_id, step_name="RECEIPT", step_status="SUCCESS",
            step_result={"final_status": "SUCCESS", "source": "reconciliation"},
            meta_object_ids=object_ids, verification_result=verification,
        )
        return self.tasks.transition(
            task_id, "SUCCESS", worker_id=self.worker_id, current_step="RECEIPT",
            meta_object_ids=object_ids,
        )
