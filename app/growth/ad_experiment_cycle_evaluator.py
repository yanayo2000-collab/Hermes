from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from app.growth.ad_experiment_cycle_service import REPORTING_TIMEZONE
from app.growth.ad_experiment_service import AdExperimentService
from app.growth.common import canonical_json, decode_json, new_id, payload_hash, utc_now
from app.growth.errors import GrowthError, GrowthStateConflict, GrowthValidationError
from app.growth.schema import ensure_growth_schema


CHECKPOINT_DAYS = {"D1": 1, "D3": 3, "D7": 7}


class AdExperimentCycleEvaluator:
    """Turn a verified action cycle into evidence and the next immutable plan.

    The evaluator reads local daily facts only.  It can propose an immutable
    operation plan, but it never approves it, creates an execution task or
    writes Meta.  Its observations are operational evidence, not a causal or
    Gate receipt.
    """

    def __init__(self, conn: sqlite3.Connection, *, ensure_schema: bool = True) -> None:
        self.conn = conn
        if ensure_schema:
            ensure_growth_schema(conn)

    def evaluate_due(self, *, as_of_date: str = "", limit: int = 100) -> Dict[str, Any]:
        if not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_creative_performance_daily'",
        ).fetchone():
            return {
                "evaluated": [], "deferred": [], "count": 0,
                "reason": "performance_table_not_ready", "meta_writes_performed": False,
            }
        try:
            as_of = (
                date.fromisoformat(as_of_date)
                if as_of_date
                else datetime.now(ZoneInfo(REPORTING_TIMEZONE)).date()
            )
        except ValueError as exc:
            raise GrowthValidationError("invalid_cycle_as_of_date") from exc
        bounded_limit = max(1, min(int(limit or 100), 500))
        rows = self.conn.execute(
            """SELECT * FROM ad_experiment_cycle
               WHERE state IN ('WAITING_EVIDENCE','EVALUATING')
               ORDER BY first_complete_date,created_at,cycle_id LIMIT ?""",
            (bounded_limit,),
        ).fetchall()
        evaluated: List[Dict[str, Any]] = []
        deferred: List[Dict[str, str]] = []
        for row in rows:
            cycle = self._cycle(row)
            boundary = date.fromisoformat(str(cycle["first_complete_date"]))
            for checkpoint, required_days in CHECKPOINT_DAYS.items():
                due_date = boundary + timedelta(days=required_days - 1)
                if as_of < due_date:
                    break
                existing = self.conn.execute(
                    "SELECT * FROM ad_experiment_cycle_evaluation WHERE cycle_id=? AND checkpoint=?",
                    (cycle["cycle_id"], checkpoint),
                ).fetchone()
                if existing:
                    item = self._evaluation(existing)
                    try:
                        self._ensure_next_plan(cycle, item)
                    except (GrowthStateConflict, GrowthValidationError) as exc:
                        deferred.append({
                            "cycle_id": cycle["cycle_id"], "checkpoint": checkpoint,
                            "reason": str(exc),
                        })
                        break
                    if item["evaluation_status"] == "ACTION_RECOMMENDED":
                        break
                    continue
                try:
                    item = self._record_checkpoint(
                        cycle, checkpoint=checkpoint, boundary=boundary,
                        window_end=due_date, required_days=required_days,
                    )
                except (GrowthStateConflict, GrowthValidationError) as exc:
                    deferred.append({
                        "cycle_id": cycle["cycle_id"], "checkpoint": checkpoint,
                        "reason": str(exc),
                    })
                    break
                evaluated.append(item)
                try:
                    plan = self._ensure_next_plan(cycle, item)
                except (GrowthStateConflict, GrowthValidationError) as exc:
                    deferred.append({
                        "cycle_id": cycle["cycle_id"], "checkpoint": checkpoint,
                        "reason": str(exc),
                    })
                    break
                if plan["action_type"] == "PAUSE_AD":
                    break
        return {
            "evaluated": evaluated,
            "deferred": deferred,
            "count": len(evaluated),
            "as_of_date": as_of.isoformat(),
            "causal_claim": False,
            "meta_writes_performed": False,
        }

    def detail(self, cycle_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM ad_experiment_cycle WHERE cycle_id=?",
            (str(cycle_id or "").strip(),),
        ).fetchone()
        if not row:
            raise GrowthValidationError("ad_experiment_cycle_not_found")
        evaluations = self.conn.execute(
            """SELECT * FROM ad_experiment_cycle_evaluation WHERE cycle_id=?
               ORDER BY CASE checkpoint WHEN 'D1' THEN 1 WHEN 'D3' THEN 3 ELSE 7 END""",
            (str(cycle_id),),
        ).fetchall()
        plans = self.conn.execute(
            """SELECT * FROM ad_experiment_cycle_next_plan WHERE cycle_id=?
               ORDER BY created_at,cycle_plan_id""",
            (str(cycle_id),),
        ).fetchall()
        cycle = self._cycle(row)
        return {
            "cycle": cycle,
            "evaluations": [self._evaluation(item) for item in evaluations],
            "plans": [self._plan(item) for item in plans],
            "immediate_assessment": self._immediate_assessment(cycle),
            "causal_claim": False,
            "meta_writes_performed": False,
        }

    def _immediate_assessment(self, cycle: Dict[str, Any]) -> Dict[str, Any]:
        """Give an immediate operating answer without faking a post-action checkpoint.

        This projection uses only complete local delivery days strictly before the
        action's local calendar date.  It never inserts an evaluation or a plan;
        D1/D3/D7 remain the only post-action checkpoints.
        """
        base = {
            "schema_version": "gle-cycle-immediate-operating-assessment-v1",
            "cycle_id": str(cycle["cycle_id"]),
            "evidence_timing": "PRE_ACTION_SETTLED_HISTORY",
            "history_is_post_action": False,
            "metric_source": "ad_creative_performance_daily",
            "metric_date_field": "report_date_london",
            "metric_content_authority": "LOCAL_OPERATIONAL_FACTS_NOT_GATE_AUTHORITY",
            "creates_cycle_evaluation": False,
            "creates_next_plan": False,
            "causal_claim": False,
            "meta_write_allowed": False,
        }
        if not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_creative_performance_daily'",
        ).fetchone():
            return self._assessment_result(
                base,
                status="DATA_NOT_READY",
                recommended_action="WAIT_FOR_DATA",
                summary="历史投放事实表尚未就绪，不能提前判断",
                reason="performance_table_not_ready",
            )
        columns = {
            str(row[1]) for row in self.conn.execute(
                "PRAGMA table_info(ad_creative_performance_daily)",
            ).fetchall()
        }
        if not {"report_date_london", "ad_id"}.issubset(columns):
            return self._assessment_result(
                base,
                status="BLOCKED",
                recommended_action="REVIEW_EVIDENCE",
                summary="历史投放事实表缺少精确日期或广告 ID，已停止提前判断",
                reason="cycle_metric_source_schema_invalid",
            )
        try:
            _, _, remaining = self._validated_remaining_cells(cycle)
            opened_at = datetime.fromisoformat(str(cycle["window_opened_at"]))
            if opened_at.tzinfo is None:
                raise ValueError("cycle_window_opened_at_timezone_required")
            opened_local_date = opened_at.astimezone(
                ZoneInfo(REPORTING_TIMEZONE),
            ).date()
            first_complete_date = date.fromisoformat(str(cycle["first_complete_date"]))
        except (GrowthStateConflict, GrowthValidationError, ValueError) as exc:
            return self._assessment_result(
                base,
                status="BLOCKED",
                recommended_action="REVIEW_EVIDENCE",
                summary="周期身份或时间边界无法安全重验，已停止提前判断",
                reason=str(exc),
            )
        cutoff = min(
            opened_local_date - timedelta(days=1),
            first_complete_date - timedelta(days=1),
        )
        common_dates = self._common_fact_dates(remaining, cutoff=cutoff, limit=7)
        if not common_dates:
            return self._assessment_result(
                base,
                status="DATA_NOT_READY",
                recommended_action="WAIT_FOR_DATA",
                summary="暂停前没有可供全部剩余广告共同比较的完整日期",
                reason="pre_action_common_window_not_ready",
                source_cutoff=cutoff.isoformat(),
            )

        metrics: Dict[str, Dict[str, Any]] = {}
        candidates: List[Dict[str, Any]] = []
        for item in remaining:
            experiment_id = str(item["experiment_id"])
            aggregate = self._aggregate_daily(
                str(item["ad_id"]), common_dates[0], common_dates[-1],
            )
            if (
                list(aggregate.get("source_dates") or [])
                != [value.isoformat() for value in common_dates]
                or not bool(aggregate.get("quality_pass"))
            ):
                return self._assessment_result(
                    base,
                    status="DATA_NOT_READY",
                    recommended_action="WAIT_FOR_DATA",
                    summary="暂停前历史日期或数据质量未形成共同可比窗口",
                    reason=f"pre_action_metric_window_incomplete:{experiment_id}",
                    source_cutoff=cutoff.isoformat(),
                )
            core = self._core_metrics(aggregate)
            core["observed_dates"] = [value.isoformat() for value in common_dates]
            metrics[experiment_id] = core
            rules = dict(item.get("delivery_guardrails") or {})
            if payload_hash(rules) != str(item.get("delivery_guardrails_hash") or ""):
                return self._assessment_result(
                    base,
                    status="BLOCKED",
                    recommended_action="REVIEW_EVIDENCE",
                    summary="冻结的投放护栏校验失败，已停止提前判断",
                    reason="cycle_delivery_guardrails_hash_invalid",
                    source_cutoff=cutoff.isoformat(),
                )
            breaches = self._exact_ctr_breaches(core, rules)
            if breaches:
                candidates.append({
                    "action_type": "PAUSE_AD",
                    "experiment_id": experiment_id,
                    "ad_id": str(item["ad_id"]),
                    "breaches": breaches,
                    "delivery_guardrails_hash": str(item["delivery_guardrails_hash"]),
                })
        candidates.sort(key=lambda item: (item["experiment_id"], item["ad_id"]))
        window = {
            "start": common_dates[0].isoformat(),
            "end": common_dates[-1].isoformat(),
            "observed_day_count": len(common_dates),
        }
        if candidates:
            return self._assessment_result(
                base,
                status="INTERVENTION_REVIEW_SUPPORTED",
                recommended_action="REVIEW_PAUSE_CANDIDATE",
                summary=(
                    f"历史数据发现 {len(candidates)} 条广告达到暂停护栏；"
                    "需先生成不可变方案并确认，当前没有执行 Meta 操作"
                ),
                source_cutoff=cutoff.isoformat(),
                source_window=window,
                metrics_by_experiment=metrics,
                action_candidates=candidates,
                operator_review_required=True,
            )
        return self._assessment_result(
            base,
            status="NO_INTERVENTION_SUPPORTED",
            recommended_action="OBSERVE",
            summary=(
                f"已读取暂停前 {len(common_dates)} 个共同完整日；"
                "剩余广告均未达到暂停护栏，当前继续观察、无需确认"
            ),
            source_cutoff=cutoff.isoformat(),
            source_window=window,
            metrics_by_experiment=metrics,
            action_candidates=[],
            operator_review_required=False,
        )

    @staticmethod
    def _assessment_result(
        base: Dict[str, Any], *, status: str, recommended_action: str,
        summary: str, **extra: Any,
    ) -> Dict[str, Any]:
        result = {
            **base,
            "status": status,
            "recommended_action": recommended_action,
            "summary": summary,
            "source_window": {},
            "metrics_by_experiment": {},
            "action_candidates": [],
            "operator_review_required": False,
            "reason": "",
            **extra,
        }
        result["assessment_hash"] = payload_hash(result)
        return result

    def _common_fact_dates(
        self, cells: List[Dict[str, Any]], *, cutoff: date, limit: int,
    ) -> List[date]:
        common: set[date] | None = None
        for item in cells:
            rows = self.conn.execute(
                """SELECT DISTINCT report_date_london
                   FROM ad_creative_performance_daily
                   WHERE ad_id=? AND report_date_london<=?
                   ORDER BY report_date_london DESC LIMIT 32""",
                (str(item["ad_id"]), cutoff.isoformat()),
            ).fetchall()
            current: set[date] = set()
            for row in rows:
                try:
                    value = date.fromisoformat(str(row["report_date_london"] or ""))
                except ValueError:
                    continue
                if value <= cutoff:
                    current.add(value)
            common = current if common is None else common & current
            if not common:
                return []
        return sorted(common or set())[-max(1, min(int(limit or 7), 7)):]

    def _record_checkpoint(
        self, cycle: Dict[str, Any], *, checkpoint: str, boundary: date,
        window_end: date, required_days: int,
    ) -> Dict[str, Any]:
        subject, _, remaining = self._validated_remaining_cells(cycle)
        target_ad_id = str(subject.get("target_ad_id") or "")

        metrics: Dict[str, Dict[str, Any]] = {}
        candidates: List[Dict[str, Any]] = []
        for item in remaining:
            experiment_id = str(item["experiment_id"])
            ad_id = str(item["ad_id"])
            aggregate = self._aggregate_daily(ad_id, boundary, window_end)
            if int(aggregate.get("day_count") or 0) != required_days:
                raise GrowthValidationError(
                    f"cycle_shared_window_incomplete:{experiment_id}:{checkpoint}"
                )
            if not bool(aggregate.get("quality_pass")):
                raise GrowthValidationError(
                    f"cycle_metric_quality_not_pass:{experiment_id}:{checkpoint}"
                )
            core = self._core_metrics(aggregate)
            metrics[experiment_id] = core
            rules = dict(item.get("delivery_guardrails") or {})
            if payload_hash(rules) != str(item.get("delivery_guardrails_hash") or ""):
                raise GrowthStateConflict("cycle_delivery_guardrails_hash_invalid")
            # This cycle intentionally admits only exact Meta delivery fields.
            # Evaluate CTR locally so future additions to the broader operating
            # guardrail engine cannot silently become executable here.
            breaches = self._exact_ctr_breaches(core, rules)
            if breaches:
                candidates.append({
                    "action_type": "PAUSE_AD",
                    "experiment_id": experiment_id,
                    "ad_id": ad_id,
                    "breaches": breaches,
                    "delivery_guardrails_hash": str(item["delivery_guardrails_hash"]),
                })
        candidates.sort(key=lambda item: (item["experiment_id"], item["ad_id"]))
        status = (
            "ACTION_RECOMMENDED" if candidates
            else "CYCLE_COMPLETE_NO_CHANGE" if checkpoint == "D7"
            else "OBSERVE"
        )
        evidence = {
            "schema_version": "gle-ad-experiment-cycle-evaluation-v1",
            "cycle_id": cycle["cycle_id"],
            "cycle_evidence_root_hash": cycle["evidence_root_hash"],
            "evaluation_subject_hash": cycle["evaluation_subject_hash"],
            "checkpoint": checkpoint,
            "window": {"start": boundary.isoformat(), "end": window_end.isoformat()},
            "metric_source": "ad_creative_performance_daily",
            "metric_date_field": "report_date_london",
            "metric_content_authority": "LOCAL_OPERATIONAL_FACTS_NOT_GATE_AUTHORITY",
            "decision_metric_fields": ["spend", "impressions", "clicks", "ctr"],
            "unavailable_decision_metric_fields": ["installs", "cpi", "real_bind_count", "real_bind_cpa"],
            "install_attribution_status": "NOT_ADMITTED_FOR_ACTION_DECISION",
            "target_paused_ad_excluded": target_ad_id,
            "evaluated_remaining_experiment_ids": sorted(metrics),
            "causal_claim": False,
            "meta_write_allowed": False,
        }
        evidence_hash = payload_hash({
            "evidence": evidence,
            "metrics_by_experiment": metrics,
            "action_candidates": candidates,
            "evaluation_status": status,
        })
        evaluation_id = new_id("adcycleeval")
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """INSERT INTO ad_experiment_cycle_evaluation
                   (cycle_evaluation_id,cycle_id,checkpoint,window_json,
                    metrics_by_experiment_json,action_candidates_json,evaluation_status,
                    data_quality_status,evidence_json,evidence_hash,causal_claim,
                    meta_write_allowed,evaluated_at)
                   VALUES (?,?,?,?,?,?,?,'PASS',?,?,0,0,?)""",
                (
                    evaluation_id, cycle["cycle_id"], checkpoint,
                    canonical_json(evidence["window"]), canonical_json(metrics),
                    canonical_json(candidates), status, canonical_json(evidence),
                    evidence_hash, now,
                ),
            )
            self.conn.execute(
                """UPDATE ad_experiment_cycle SET state='EVALUATING',latest_checkpoint=?,
                   latest_evaluation_status=?,updated_at=? WHERE cycle_id=?""",
                (checkpoint, status, now, cycle["cycle_id"]),
            )
        return self._evaluation(self.conn.execute(
            "SELECT * FROM ad_experiment_cycle_evaluation WHERE cycle_evaluation_id=?",
            (evaluation_id,),
        ).fetchone())

    def _validated_remaining_cells(
        self, cycle: Dict[str, Any],
    ) -> tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        subject = dict(cycle["evaluation_subject"] or {})
        if payload_hash(subject) != str(cycle["evaluation_subject_hash"]):
            raise GrowthStateConflict("cycle_evaluation_subject_hash_invalid")
        if (
            subject.get("schema_version") != "gle-evaluation-cycle-subject-v1"
            or subject.get("metric_source") != "ad_creative_performance_daily"
            or subject.get("metric_date_field") != "report_date_london"
            or subject.get("causal_claim") is not False
            or subject.get("meta_write_allowed") is not False
        ):
            raise GrowthStateConflict("cycle_evaluation_subject_invalid")
        target_experiment_id = str(subject.get("target_experiment_id") or "")
        target_ad_id = str(subject.get("target_ad_id") or "")
        raw_cells = subject.get("cells")
        if not isinstance(raw_cells, list):
            raise GrowthStateConflict("cycle_evaluation_cells_invalid")
        cells = [dict(item or {}) for item in raw_cells]
        if not cells or len(cells) > 4:
            raise GrowthStateConflict("cycle_evaluation_cells_invalid")
        experiment_ids = [str(item.get("experiment_id") or "") for item in cells]
        ad_ids = [str(item.get("ad_id") or "") for item in cells]
        if (
            any(not item for item in experiment_ids + ad_ids)
            or len(set(experiment_ids)) != len(experiment_ids)
            or len(set(ad_ids)) != len(ad_ids)
            or target_experiment_id not in experiment_ids
            or target_ad_id not in ad_ids
        ):
            raise GrowthStateConflict("cycle_evaluation_cell_identity_invalid")
        self._validate_current_bindings(subject, cells)
        remaining = [
            item for item in cells
            if str(item.get("experiment_id") or "") != target_experiment_id
            and str(item.get("ad_id") or "") != target_ad_id
        ]
        if not remaining:
            raise GrowthValidationError("cycle_has_no_remaining_ad_for_evaluation")
        return subject, cells, remaining

    @staticmethod
    def _exact_ctr_breaches(
        metrics: Dict[str, Any], rules: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        ctr_rule = dict(rules.get("ctr_floor") or {})
        ctr = metrics.get("ctr")
        impressions = float(metrics.get("impressions") or 0)
        if (
            not ctr_rule
            or ctr is None
            or impressions < float(ctr_rule.get("minimum_impressions") or 0)
            or float(ctr) >= float(ctr_rule.get("minimum_ctr") or 0)
        ):
            return []
        return [{
            "rule": "ctr_floor",
            "summary": (
                f"展示已达 {int(impressions)}，CTR {float(ctr):.2%} 低于 "
                f"{float(ctr_rule.get('minimum_ctr') or 0):.2%}"
            ),
        }]

    def _ensure_next_plan(
        self, cycle: Dict[str, Any], evaluation: Dict[str, Any],
    ) -> Dict[str, Any]:
        existing = self.conn.execute(
            "SELECT * FROM ad_experiment_cycle_next_plan WHERE cycle_evaluation_id=?",
            (evaluation["cycle_evaluation_id"],),
        ).fetchone()
        if existing:
            result = self._plan(existing)
            self._finalize_source_state(cycle, result)
            return result
        candidates = list(evaluation["action_candidates"] or [])
        checkpoint = str(evaluation["checkpoint"])
        now = utc_now()
        target_experiment_id = ""
        target_id = ""
        operation_action_id = ""
        requires_confirmation = False
        status = "READY"
        if candidates:
            candidate = dict(candidates[0])
            target_experiment_id = str(candidate["experiment_id"])
            target_id = str(candidate["ad_id"])
            source_action = self.conn.execute(
                "SELECT decision_id FROM growth_operation_action WHERE operation_action_id=?",
                (str(cycle["source_operation_action_id"]),),
            ).fetchone()
            if not source_action or not str(source_action["decision_id"] or ""):
                raise GrowthStateConflict("cycle_source_decision_missing")
            request = {
                "decision_id": str(source_action["decision_id"]),
                "action_type": "PAUSE_AD",
                "target_object_type": "AD",
                "target_object_id": target_id,
                "reason": (
                    f"闭环 {cycle['cycle_id']} {checkpoint} 止损："
                    + "；".join(str(item["summary"]) for item in candidate["breaches"])
                ),
                "evidence_window": {
                    "cycle_id": cycle["cycle_id"],
                    "cycle_evaluation_id": evaluation["cycle_evaluation_id"],
                    "checkpoint": checkpoint,
                    **dict(evaluation["window"] or {}),
                },
                "expected_effect": {
                    "primary": "stop_further_inefficient_spend",
                    "causal_claim": False,
                },
                "evaluation_window": {"checkpoints": ["D1", "D3", "D7"]},
            }
            try:
                preview = AdExperimentService(self.conn).preview_plan(
                    target_experiment_id, request,
                    actor="growth-cycle-evaluator",
                    idempotency_key=(
                        f"cycle:{cycle['cycle_id']}:{checkpoint}:pause:{target_experiment_id}"
                    ),
                )
                plan = dict(preview["plan"])
                operation_action_id = str(preview["plan_id"])
                requires_confirmation = True
                status = "AWAITING_CONFIRMATION"
            except GrowthError as exc:
                plan = {
                    "schema_version": "gle-cycle-next-plan-v1",
                    "cycle_id": cycle["cycle_id"],
                    "cycle_evaluation_id": evaluation["cycle_evaluation_id"],
                    "action_type": "PAUSE_AD",
                    "target_experiment_id": target_experiment_id,
                    "target_id": target_id,
                    "status": "BLOCKED",
                    "block_reason": str(exc),
                    "causal_claim": False,
                    "meta_write_allowed": False,
                }
                status = "BLOCKED"
        else:
            next_checkpoint = {"D1": "D3", "D3": "D7", "D7": ""}[checkpoint]
            plan = {
                "schema_version": "gle-cycle-next-plan-v1",
                "cycle_id": cycle["cycle_id"],
                "cycle_evaluation_id": evaluation["cycle_evaluation_id"],
                "action_type": "OBSERVE",
                "next_checkpoint": next_checkpoint,
                "summary": (
                    f"继续观察至 {next_checkpoint}，暂不修改广告"
                    if next_checkpoint else "本轮完成，当前没有足够证据要求修改广告"
                ),
                "causal_claim": False,
                "requires_confirmation": False,
                "meta_write_allowed": False,
            }
        plan_hash = payload_hash(plan)
        plan_id = new_id("adcycleplan")
        terminal = status in {"AWAITING_CONFIRMATION", "BLOCKED"} or checkpoint == "D7"
        cycle_state = (
            "NEXT_PLAN_READY" if status == "AWAITING_CONFIRMATION"
            else "BLOCKED" if status == "BLOCKED"
            else "EVALUATED" if checkpoint == "D7"
            else "EVALUATING"
        )
        with self.conn:
            self.conn.execute(
                """UPDATE ad_experiment_cycle_next_plan SET status='SUPERSEDED',updated_at=?
                   WHERE cycle_id=? AND status='READY'""",
                (now, cycle["cycle_id"]),
            )
            self.conn.execute(
                """INSERT INTO ad_experiment_cycle_next_plan
                   (cycle_plan_id,cycle_id,cycle_evaluation_id,checkpoint,action_type,
                    target_experiment_id,target_id,operation_action_id,plan_json,plan_hash,
                    status,requires_confirmation,causal_claim,meta_write_allowed,
                    created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?)""",
                (
                    plan_id, cycle["cycle_id"], evaluation["cycle_evaluation_id"], checkpoint,
                    "PAUSE_AD" if candidates else "OBSERVE", target_experiment_id, target_id,
                    operation_action_id, canonical_json(plan), plan_hash, status,
                    1 if requires_confirmation else 0, now, now,
                ),
            )
            self.conn.execute(
                "UPDATE ad_experiment_cycle SET state=?,updated_at=? WHERE cycle_id=?",
                (cycle_state, now, cycle["cycle_id"]),
            )
            experiment_service = AdExperimentService(self.conn)
            source_experiment = experiment_service.get(str(cycle["experiment_id"]))
            event_type = "CYCLE_NEXT_PLAN_READY" if terminal else "CYCLE_OBSERVATION_CONTINUES"
            experiment_service._event(
                str(cycle["experiment_id"]), source_experiment["state"],
                source_experiment["state"], event_type, "growth-cycle-evaluator",
                f"{checkpoint}:{status}",
                {
                    "cycle_id": cycle["cycle_id"],
                    "cycle_evaluation_id": evaluation["cycle_evaluation_id"],
                    "cycle_plan_id": plan_id,
                    "operation_action_id": operation_action_id,
                    "requires_confirmation": requires_confirmation,
                    "causal_claim": False,
                    "meta_writes_performed": False,
                },
            )
        result = self._plan(self.conn.execute(
            "SELECT * FROM ad_experiment_cycle_next_plan WHERE cycle_plan_id=?",
            (plan_id,),
        ).fetchone())
        self._finalize_source_state(cycle, result)
        return result

    def _finalize_source_state(
        self, cycle: Dict[str, Any], plan: Dict[str, Any],
    ) -> None:
        terminal = (
            str(plan.get("status") or "") in {"AWAITING_CONFIRMATION", "BLOCKED"}
            or str(plan.get("checkpoint") or "") == "D7"
        )
        if not terminal:
            return
        service = AdExperimentService(self.conn)
        experiment = service.get(str(cycle["experiment_id"]))
        if experiment["state"] == "PAUSED":
            return
        if experiment["state"] != "EVALUATING_ADJUSTMENT":
            raise GrowthStateConflict(
                f"cycle_terminal_experiment_state_invalid:{experiment['state']}"
            )
        service.transition(
            str(cycle["experiment_id"]), "PAUSED", actor="growth-cycle-evaluator",
            reason=f"cycle_terminal_plan:{plan['cycle_plan_id']}",
            event_type="ADJUSTMENT_EVALUATION_COMPLETED",
            evidence={
                "cycle_id": cycle["cycle_id"],
                "cycle_plan_id": plan["cycle_plan_id"],
                "checkpoint": plan["checkpoint"],
                "plan_status": plan["status"],
                "causal_claim": False,
                "meta_writes_performed": False,
            },
        )

    def _validate_current_bindings(
        self, subject: Dict[str, Any], cells: List[Dict[str, Any]],
    ) -> None:
        experiment_ids = [str(item["experiment_id"]) for item in cells]
        placeholders = ",".join("?" for _ in experiment_ids)
        rows = self.conn.execute(
            f"""SELECT experiment_id,account_id,source_report_id,source_ad_id
                FROM ad_experiment WHERE experiment_id IN ({placeholders})
                ORDER BY experiment_id""",
            tuple(experiment_ids),
        ).fetchall()
        if len(rows) != len(experiment_ids):
            raise GrowthStateConflict("cycle_evaluation_experiment_binding_missing")
        expected = {
            str(item["experiment_id"]): str(item["ad_id"])
            for item in cells
        }
        account_id = str(subject.get("account_id") or "")
        launch_id = str(subject.get("launch_id") or "")
        for row in rows:
            if (
                str(row["source_ad_id"] or "") != expected[str(row["experiment_id"])]
                or str(row["account_id"] or "").removeprefix("act_") != account_id
                or (launch_id and str(row["source_report_id"] or "") != launch_id)
            ):
                raise GrowthStateConflict("cycle_evaluation_experiment_binding_drift")

    def _aggregate_daily(self, ad_id: str, start: date, end: date) -> Dict[str, Any]:
        columns = {
            str(row[1]) for row in self.conn.execute(
                "PRAGMA table_info(ad_creative_performance_daily)",
            ).fetchall()
        }
        required = {
            "report_date_london", "asset_id", "ad_id", "spend", "impressions",
            "clicks", "data_quality_status",
        }
        if not required.issubset(columns):
            raise GrowthStateConflict("cycle_metric_source_schema_invalid")
        rows = self.conn.execute(
            """SELECT report_date_london,asset_id,spend,impressions,clicks,
                       data_quality_status
                FROM ad_creative_performance_daily
                WHERE ad_id=? AND report_date_london BETWEEN ? AND ?
                ORDER BY report_date_london,asset_id""",
            (ad_id, start.isoformat(), end.isoformat()),
        ).fetchall()
        if not rows:
            return {"day_count": 0, "quality_pass": False}
        allowed_quality = {
            "PASS", "MEDIA_ONLY_AD_ID", "AD_ID_WITH_DOWNSTREAM_TEXT_MATCH",
        }
        by_date: Dict[str, Dict[str, Any]] = {}
        quality_pass = True
        quality_statuses: set[str] = set()
        for row in rows:
            report_date = str(row["report_date_london"] or "")
            current = {
                "spend": float(row["spend"] or 0),
                "impressions": float(row["impressions"] or 0),
                "clicks": float(row["clicks"] or 0),
            }
            if report_date in by_date and by_date[report_date] != current:
                quality_pass = False
            else:
                by_date[report_date] = current
            quality_status = str(row["data_quality_status"] or "").upper()
            quality_statuses.add(quality_status)
            if quality_status not in allowed_quality:
                quality_pass = False
        canonical_days = [by_date[key] for key in sorted(by_date)]
        spend = sum(item["spend"] for item in canonical_days)
        impressions = sum(item["impressions"] for item in canonical_days)
        clicks = sum(item["clicks"] for item in canonical_days)
        return {
            "spend": spend,
            "impressions": impressions,
            "clicks": clicks,
            "installs": None,
            "real_bind_count": None,
            "day_count": len(canonical_days),
            "source_row_count": len(rows),
            "duplicate_projection_rows_collapsed": len(rows) - len(canonical_days),
            "source_quality_statuses": sorted(quality_statuses),
            "source_dates": sorted(by_date),
            "source_fact_hash": payload_hash({
                "metric_source": "ad_creative_performance_daily",
                "ad_id": ad_id,
                "window": {"start": start.isoformat(), "end": end.isoformat()},
                "canonical_daily_delivery_facts": [
                    {"report_date": key, **by_date[key]} for key in sorted(by_date)
                ],
                "source_quality_statuses": sorted(quality_statuses),
                "source_row_count": len(rows),
                "duplicate_projection_rows_collapsed": len(rows) - len(canonical_days),
            }),
            "quality_pass": quality_pass,
        }

    @staticmethod
    def _core_metrics(aggregate: Dict[str, Any]) -> Dict[str, Any]:
        spend = float(aggregate.get("spend") or 0)
        impressions = float(aggregate.get("impressions") or 0)
        clicks = float(aggregate.get("clicks") or 0)
        return {
            "spend": spend,
            "impressions": impressions,
            "clicks": clicks,
            "installs": None,
            "real_bind_count": None,
            "ctr": clicks / impressions if impressions else 0.0,
            "cpi": None,
            "real_bind_cpa": None,
            "source_row_count": int(aggregate.get("source_row_count") or 0),
            "duplicate_projection_rows_collapsed": int(
                aggregate.get("duplicate_projection_rows_collapsed") or 0
            ),
            "source_quality_statuses": list(aggregate.get("source_quality_statuses") or []),
            "source_fact_hash": str(aggregate.get("source_fact_hash") or ""),
        }

    @staticmethod
    def _cycle(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["evaluation_subject"] = decode_json(result.pop("evaluation_subject_json"), {})
        result["evaluation_checkpoints"] = decode_json(
            result.pop("evaluation_checkpoints_json"), [],
        )
        result["causal_claim"] = bool(result["causal_claim"])
        result["meta_write_allowed"] = bool(result["meta_write_allowed"])
        return result

    @staticmethod
    def _evaluation(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["window"] = decode_json(result.pop("window_json"), {})
        result["metrics_by_experiment"] = decode_json(
            result.pop("metrics_by_experiment_json"), {},
        )
        result["action_candidates"] = decode_json(result.pop("action_candidates_json"), [])
        result["evidence"] = decode_json(result.pop("evidence_json"), {})
        result["causal_claim"] = bool(result["causal_claim"])
        result["meta_write_allowed"] = bool(result["meta_write_allowed"])
        return result

    @staticmethod
    def _plan(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["plan"] = decode_json(result.pop("plan_json"), {})
        result["requires_confirmation"] = bool(result["requires_confirmation"])
        result["causal_claim"] = bool(result["causal_claim"])
        result["meta_write_allowed"] = bool(result["meta_write_allowed"])
        result["meta_writes_performed"] = False
        return result
