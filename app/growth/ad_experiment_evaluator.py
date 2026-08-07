from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

from app.growth.ad_experiment_service import AdExperimentService, EXPERIMENT_TRANSITIONS
from app.growth.common import canonical_json, decode_json, new_id, payload_hash, utc_now
from app.growth.episode_service import EpisodeService
from app.growth.errors import GrowthNotFound, GrowthStateConflict, GrowthValidationError
from app.growth.knowledge_service import KnowledgeService
from app.growth.schema import ensure_growth_schema


CHECKPOINTS = {"D1", "D3", "D7"}
FINAL_TO_EXPERIMENT_STATE = {
    "EFFECTIVE": "EFFECTIVE",
    "INEFFECTIVE": "INEFFECTIVE",
    "NEUTRAL": "INCONCLUSIVE",
    "INSUFFICIENT_SAMPLE": "INCONCLUSIVE",
    "DATA_INCOMPLETE": "DATA_INCOMPLETE",
    "NOT_ATTRIBUTABLE": "INCONCLUSIVE",
    "MIXED_CHANGE": "MIXED_CHANGE",
    "NOT_EXECUTED": "INCONCLUSIVE",
    "PENDING": "INCONCLUSIVE",
}


class AdExperimentEvaluator:
    """Persists D1/D3/D7 evidence and closes the linked Growth Episode at D7."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_growth_schema(conn)

    @staticmethod
    def _with_core_metrics(metrics: Dict[str, Any], *, has_install_source: bool) -> Dict[str, Any]:
        result = dict(metrics)
        installs = float(result.get("installs") or 0) if has_install_source else None
        spend = float(result.get("spend") or 0)
        joins = float(result.get("real_bind_count", result.get("conversions", 0)) or 0)
        result["installs"] = installs
        result["cpi"] = spend / installs if installs and spend > 0 else None
        result["real_bind_count"] = joins
        result["real_bind_cpa"] = spend / joins if joins else None
        return result

    def record_checkpoint(
        self, experiment_id: str, payload: Dict[str, Any], *, actor: str, idempotency_key: str,
    ) -> Dict[str, Any]:
        experiment = AdExperimentService(self.conn).get(experiment_id)
        checkpoint = str(payload.get("checkpoint") or "").strip().upper()
        if checkpoint not in CHECKPOINTS:
            raise GrowthValidationError("invalid_evaluation_checkpoint")
        digest = payload_hash({"experiment_id": experiment_id, **payload})
        route = f"ad_experiment.evaluate:{checkpoint}"
        existing = self.conn.execute(
            "SELECT request_hash,response_json FROM growth_idempotency_record WHERE route_key=? AND idempotency_key=?",
            (route, idempotency_key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != digest:
                raise GrowthStateConflict("idempotency_key_payload_conflict")
            return decode_json(existing["response_json"], {})
        duplicate = self.conn.execute(
            "SELECT evaluation_id FROM ad_experiment_evaluation WHERE experiment_id=? AND checkpoint=?",
            (experiment_id, checkpoint),
        ).fetchone()
        if duplicate:
            raise GrowthStateConflict("evaluation_checkpoint_already_recorded")
        episode_id = str(payload.get("episode_id") or self._episode_id(experiment_id) or "")
        baseline = dict(payload.get("baseline_metrics") or {})
        post = dict(payload.get("post_metrics") or {})
        scorecard_snapshot = dict(dict(experiment.get("hypothesis_json") or {}).get("latest_observation") or {})
        if scorecard_snapshot:
            post.setdefault("v4_scorecard_snapshot", scorecard_snapshot)
        status = self._evaluate(experiment, payload, baseline, post)
        now = utc_now()
        evaluation_id = new_id("adeval")
        result = {
            "evaluation_id": evaluation_id, "experiment_id": experiment_id,
            "episode_id": episode_id, "checkpoint": checkpoint,
            "baseline_window_json": dict(payload.get("baseline_window") or baseline.get("window") or {}),
            "post_window_json": dict(payload.get("post_window") or post.get("window") or {}),
            "baseline_metrics_json": baseline, "post_metrics_json": post,
            "data_quality_status": str(payload.get("data_quality_status") or "PASS").upper(),
            "dedupe_version": str(payload.get("dedupe_version") or ""),
            "attribution_version": str(payload.get("attribution_version") or ""),
            "evaluation_status": status, "evaluated_at": now,
            "scorecard_snapshot": scorecard_snapshot,
        }
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO ad_experiment_evaluation
                (evaluation_id,experiment_id,episode_id,checkpoint,baseline_window_json,post_window_json,
                 baseline_metrics_json,post_metrics_json,data_quality_status,dedupe_version,
                 attribution_version,evaluation_status,evaluated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    evaluation_id, experiment_id, episode_id, checkpoint,
                    canonical_json(result["baseline_window_json"]), canonical_json(result["post_window_json"]),
                    canonical_json(baseline), canonical_json(post), result["data_quality_status"],
                    result["dedupe_version"], result["attribution_version"], status, now,
                ),
            )
            self.conn.execute(
                """INSERT INTO growth_idempotency_record
                (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
                VALUES (?,?,?,201,?,?)""",
                (route, idempotency_key, digest, canonical_json(result), now),
            )
        self._advance_experiment(experiment_id, checkpoint, status, actor, evaluation_id)
        if checkpoint == "D7" and episode_id:
            self._close_episode(episode_id, experiment_id, status, actor)
        result["experiment"] = AdExperimentService(self.conn).get(experiment_id)
        result["checkpoints"] = self.list(experiment_id)["items"]
        return result

    def list(self, experiment_id: str) -> Dict[str, Any]:
        AdExperimentService(self.conn).get(experiment_id)
        rows = self.conn.execute(
            "SELECT * FROM ad_experiment_evaluation WHERE experiment_id=? ORDER BY CASE checkpoint WHEN 'D1' THEN 1 WHEN 'D3' THEN 3 ELSE 7 END",
            (experiment_id,),
        ).fetchall()
        items = [self._serialize(row) for row in rows]
        return {"experiment_id": experiment_id, "items": items, "count": len(items), "required_checkpoints": ["D1", "D3", "D7"]}

    def evaluate_due(self, *, as_of_date: str = "", account_id: str = "") -> Dict[str, Any]:
        """Materialize due checkpoints from the dashboard's existing daily facts."""
        fact_table = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_creative_performance_daily'",
        ).fetchone()
        if not fact_table:
            return {"evaluated": [], "count": 0, "reason": "performance_table_not_ready"}
        try:
            as_of = date.fromisoformat(as_of_date) if as_of_date else datetime.now(timezone.utc).date()
        except ValueError as exc:
            raise GrowthValidationError("invalid_as_of_date") from exc
        experiments = self.conn.execute(
            """SELECT * FROM ad_experiment
            WHERE source_ad_id<>'' AND state IN ('RUNNING','MATURING','EVALUATING_ADJUSTMENT')
            ORDER BY updated_at,experiment_id"""
        ).fetchall()
        created: List[Dict[str, Any]] = []
        reconciled: List[Dict[str, Any]] = []
        normalized_account_id = str(account_id or "").strip().removeprefix("act_")
        for row in experiments:
            experiment = AdExperimentService._serialize(row)
            if normalized_account_id and str(experiment.get("account_id") or "").removeprefix("act_") != normalized_account_id:
                continue
            passive_observation = dict(experiment.get("hypothesis_json") or {}).get("mode") == "passive_observation"
            control = dict(experiment.get("control_definition_json") or {})
            if str(experiment.get("source_report_id") or "") and str(control.get("test_variable") or "") in {
                "creative_direction", "audience_strategy", "copy_variant",
            }:
                # New launch experiments have no same-Ad pre-period.  Their
                # declared variable is evaluated by the matching launch-level
                # evaluator over one shared post window.
                continue
            boundary = self._evaluation_boundary(experiment["experiment_id"], experiment["created_at"])
            for checkpoint, days in (("D1", 1), ("D3", 3), ("D7", 7)):
                exists = self.conn.execute(
                    "SELECT * FROM ad_experiment_evaluation WHERE experiment_id=? AND checkpoint=?",
                    (experiment["experiment_id"], checkpoint),
                ).fetchone()
                if as_of < boundary + timedelta(days=days):
                    continue
                if passive_observation:
                    payload = self._passive_observation_payload(experiment, checkpoint, boundary, days)
                    if not payload:
                        continue
                    if exists:
                        item = self._reconcile_passive_checkpoint(experiment, exists, payload)
                        if item:
                            reconciled.append(item)
                        continue
                    created.append(self.record_checkpoint(
                        experiment["experiment_id"], payload,
                        actor="growth-experiment-evaluator",
                        idempotency_key=f"scheduled-passive:{experiment['experiment_id']}:{checkpoint}:{boundary.isoformat()}",
                    ))
                    continue
                if exists:
                    continue
                baseline = self._aggregate_daily(experiment["source_ad_id"], boundary - timedelta(days=3), boundary - timedelta(days=1))
                post = self._aggregate_daily(experiment["source_ad_id"], boundary, boundary + timedelta(days=days - 1))
                if int(post.get("day_count") or 0) < days:
                    continue
                quality = "PASS" if baseline.get("quality_pass") and post.get("quality_pass") else "DATA_INCOMPLETE"
                created.append(self.record_checkpoint(
                    experiment["experiment_id"],
                    {
                        "checkpoint": checkpoint, "episode_id": self._episode_id(experiment["experiment_id"]),
                        "action_type": experiment["experiment_type"], "execution_status": "CLEAN_EXECUTED",
                        "baseline_window": {"start": (boundary - timedelta(days=3)).isoformat(), "end": (boundary - timedelta(days=1)).isoformat()},
                        "post_window": {"start": boundary.isoformat(), "end": (boundary + timedelta(days=days - 1)).isoformat()},
                        "baseline_metrics": baseline, "post_metrics": post,
                        "data_quality_status": quality, "dedupe_version": "ad_dashboard_daily_v1",
                        "attribution_version": "dashboard_existing_metrics_v1",
                    }, actor="growth-experiment-evaluator",
                    idempotency_key=f"scheduled:{experiment['experiment_id']}:{checkpoint}:{boundary.isoformat()}",
                ))
        return {
            "evaluated": created, "reconciled": reconciled,
            "count": len(created), "reconciled_count": len(reconciled),
            "as_of_date": as_of.isoformat(),
        }

    @staticmethod
    def _passive_observation_payload(
        experiment: Dict[str, Any], checkpoint: str, boundary: date, days: int,
    ) -> Dict[str, Any]:
        latest = dict(dict(experiment.get("hypothesis_json") or {}).get("latest_observation") or {})
        core = dict(latest.get("metrics") or {})
        technical = dict(latest.get("technical_metrics") or {})
        required = ("installs", "ctr", "real_bind_count")
        if not latest.get("report_date") or any(key not in core or core.get(key) is None for key in required):
            return {}
        if "spend" not in technical or technical.get("spend") is None:
            return {}
        post = {
            "installs": core.get("installs"), "cpi": core.get("cpi"),
            "ctr": core.get("ctr"), "real_bind_count": core.get("real_bind_count"),
            "conversions": core.get("real_bind_count"),
            "real_bind_cpa": core.get("real_bind_cpa"), "cpa": core.get("real_bind_cpa"),
            "spend": technical.get("spend"), "impressions": technical.get("impressions"),
            "clicks": technical.get("clicks"), "quality_pass": True,
            "source_report_id": latest.get("report_id"), "source_report_date": latest.get("report_date"),
        }
        return {
            "checkpoint": checkpoint, "action_type": "PASSIVE_OBSERVATION",
            "execution_status": "CLEAN_EXECUTED", "baseline_metrics": {},
            "post_metrics": post, "data_quality_status": "PASS",
            "post_window": {
                "start": boundary.isoformat(),
                "end": (boundary + timedelta(days=days - 1)).isoformat(),
            },
            "dedupe_version": "ad_observation_location_v1",
            "attribution_version": "tugao_af_joined_report_v1",
        }

    def _reconcile_passive_checkpoint(
        self, experiment: Dict[str, Any], existing: sqlite3.Row, payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        if str(existing["evaluation_status"] or "") != "DATA_INCOMPLETE":
            return {}
        post = dict(payload.get("post_metrics") or {})
        checkpoint = str(existing["checkpoint"] or "")
        status = self._evaluate(experiment, payload, {}, post)
        now = utc_now()
        before = self._serialize(existing)
        with self.conn:
            self.conn.execute(
                """UPDATE ad_experiment_evaluation
                   SET baseline_window_json='{}',baseline_metrics_json='{}',post_window_json=?,
                       post_metrics_json=?,data_quality_status='PASS',dedupe_version=?,
                       attribution_version=?,evaluation_status=?,evaluated_at=?
                   WHERE evaluation_id=?""",
                (
                    canonical_json(payload.get("post_window") or {}), canonical_json(post),
                    str(payload.get("dedupe_version") or ""), str(payload.get("attribution_version") or ""),
                    status, now, str(existing["evaluation_id"]),
                ),
            )
            self.conn.execute(
                """UPDATE growth_next_action SET status='DISMISSED',updated_at=?
                   WHERE source_type='EXPERIMENT' AND source_id=? AND action_type='CHECK_DATA'
                     AND status IN ('READY','BLOCKED','APPROVAL_REQUIRED')""",
                (now, str(existing["evaluation_id"])),
            )
            AdExperimentService(self.conn)._event(
                experiment["experiment_id"], str(experiment["state"]), str(experiment["state"]),
                "PASSIVE_EVALUATION_ATTRIBUTION_RECONCILED", "growth-experiment-evaluator",
                f"{checkpoint}:joined_attribution_restored",
                {"evaluation_id": str(existing["evaluation_id"]), "before": before,
                 "after_status": status, "post_metrics": post, "meta_writes_performed": False},
            )
        return self._serialize(self.conn.execute(
            "SELECT * FROM ad_experiment_evaluation WHERE evaluation_id=?",
            (str(existing["evaluation_id"]),),
        ).fetchone())

    def _aggregate_daily(self, ad_id: str, start: date, end: date) -> Dict[str, Any]:
        columns = {
            str(row[1]) for row in self.conn.execute(
                "PRAGMA table_info(ad_creative_performance_daily)",
            ).fetchall()
        }
        install_column = ",installs" if "installs" in columns else ""
        rows = self.conn.execute(
            f"""SELECT report_date_london,spend,impressions,clicks{install_column},tugao_real_bind_count,
                      real_bind_cpa,data_quality_status
            FROM ad_creative_performance_daily
            WHERE ad_id=? AND report_date_london BETWEEN ? AND ?
            ORDER BY report_date_london""",
            (ad_id, start.isoformat(), end.isoformat()),
        ).fetchall()
        if not rows:
            return self._aggregate_report_objects(ad_id, start, end)
        spend = sum(float(row["spend"] or 0) for row in rows)
        impressions = sum(float(row["impressions"] or 0) for row in rows)
        clicks = sum(float(row["clicks"] or 0) for row in rows)
        installs = sum(float(row["installs"] or 0) for row in rows) if "installs" in columns else 0
        conversions = sum(float(row["tugao_real_bind_count"] or 0) for row in rows)
        return self._with_core_metrics({
            "spend": spend, "impressions": impressions, "clicks": clicks,
            "ctr": clicks / impressions if impressions else 0.0,
            "installs": installs, "conversions": conversions, "real_bind_count": conversions,
            "cpa": spend / conversions if conversions else None,
            "real_bind_cpa": spend / conversions if conversions else None,
            "day_count": len({str(row["report_date_london"]) for row in rows}),
            "quality_pass": bool(rows) and all(str(row["data_quality_status"] or "").upper() == "PASS" for row in rows),
        }, has_install_source="installs" in columns)

    def _aggregate_report_objects(self, object_id: str, start: date, end: date) -> Dict[str, Any]:
        """Fallback for dashboard objects whose stable ID is not a Meta ad_id."""
        report_rows = self.conn.execute(
            """
            SELECT report_date,payload_json FROM ad_daily_report
            WHERE data_mode='real' AND report_date BETWEEN ? AND ? AND report_date NOT LIKE '%__last%'
            ORDER BY report_date,generated_at_utc DESC
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        selected: Dict[str, Dict[str, Any]] = {}
        for report_row in report_rows:
            report_date = str(report_row["report_date"] or "")
            if report_date in selected:
                continue
            payload = decode_json(report_row["payload_json"], {})
            item = next(
                (
                    dict(candidate)
                    for candidate in payload.get("ad_objects") or []
                    if str((candidate or {}).get("object_id") or "") == object_id
                ),
                {},
            )
            if item:
                selected[report_date] = item
        rows = list(selected.values())
        spend = sum(float(row.get("spend") or 0) for row in rows)
        impressions = sum(float(row.get("impressions") or 0) for row in rows)
        clicks = sum(float(row.get("clicks") or 0) for row in rows)
        conversions = sum(float(row.get("real_bind_count") or 0) for row in rows)
        installs = sum(float(row.get("installs") or 0) for row in rows)
        registrations = sum(float(row.get("registrations") or 0) for row in rows)
        applies = sum(float(row.get("auto_apply_user_count") or row.get("im_entries") or 0) for row in rows)
        valid_im = sum(float(row.get("user_engaged_im_users") or 0) for row in rows)
        return self._with_core_metrics({
            "spend": spend,
            "impressions": impressions,
            "clicks": clicks,
            "ctr": clicks / impressions if impressions else 0.0,
            "conversions": conversions,
            "real_bind_count": conversions,
            "cpa": spend / conversions if conversions else None,
            "real_bind_cpa": spend / conversions if conversions else None,
            "installs": installs,
            "technical_metrics": {
                "registrations": registrations,
                "auto_apply_user_count": applies,
                "user_engaged_im_users": valid_im,
            },
            "day_count": len(rows),
            "quality_pass": bool(rows) and all(
                str(dict(row.get("data_quality") or {}).get("status") or "").lower() == "ok"
                for row in rows
            ),
        }, has_install_source=True)

    def _evaluation_boundary(self, experiment_id: str, fallback: str) -> date:
        rows = self.conn.execute(
            """SELECT to_state,event_type,evidence_json,created_at FROM ad_experiment_events
            WHERE experiment_id=? ORDER BY created_at,event_id""",
            (experiment_id,),
        ).fetchall()
        value = ""
        fallback_running = ""
        for row in rows:
            if str(row["to_state"] or "").upper() != "RUNNING":
                continue
            fallback_running = fallback_running or str(row["created_at"] or "")
            evidence = canonical_json(decode_json(row["evidence_json"], {})).upper()
            event_type = str(row["event_type"] or "").upper()
            if any(marker in evidence for marker in ('"SUCCESS"', '"VERIFIED"', '"ACTIVE"')) or any(
                marker in event_type for marker in ("RECONCILED", "ACTIVATED", "ACTIVATION_VERIFIED")
            ):
                value = str(row["created_at"] or "")
                break
        value = str(value or fallback_running or fallback or "")
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return datetime.now(timezone.utc).date()

    @staticmethod
    def _evaluate(experiment: Dict[str, Any], payload: Dict[str, Any], baseline: Dict[str, Any], post: Dict[str, Any]) -> str:
        quality = str(payload.get("data_quality_status") or "PASS").upper()
        execution = str(payload.get("execution_status") or "CLEAN_EXECUTED").upper()
        if execution == "MIXED_CHANGED" or payload.get("mixed_change") is True:
            return "MIXED_CHANGE"
        if execution == "NOT_EXECUTED":
            return "NOT_EXECUTED"
        if execution != "CLEAN_EXECUTED":
            return "NOT_ATTRIBUTABLE"
        if quality != "PASS":
            return "DATA_INCOMPLETE"
        if dict(experiment.get("hypothesis_json") or {}).get("mode") == "passive_observation":
            checkpoint = str(payload.get("checkpoint") or "").upper()
            if checkpoint in {"D1", "D3"}:
                return "PENDING"
            minimum = int(dict(experiment.get("maturity_rule_json") or {}).get("minimum_conversions") or 10)
            joins = float(post.get("real_bind_count") or post.get("conversions") or 0)
            return "NEUTRAL" if joins >= minimum else "INSUFFICIENT_SAMPLE"
        minimum = int(dict(experiment.get("maturity_rule_json") or {}).get("minimum_conversions") or 10)
        before_conversions = float(baseline.get("conversions") or baseline.get("real_bind_count") or 0)
        after_conversions = float(post.get("conversions") or post.get("real_bind_count") or 0)
        if min(before_conversions, after_conversions) < minimum:
            return "INSUFFICIENT_SAMPLE"
        before_cpa = baseline.get("cpa", baseline.get("real_bind_cpa"))
        after_cpa = post.get("cpa", post.get("real_bind_cpa"))
        before_spend = float(baseline.get("spend") or 0)
        after_spend = float(post.get("spend") or 0)
        action = str(payload.get("action_type") or experiment.get("experiment_type") or "").upper()
        if action in {"PAUSE_AD", "PAUSE_ADSET", "PAUSE_TEST"}:
            return "EFFECTIVE" if after_spend <= before_spend * 0.1 else "INEFFECTIVE"
        if before_cpa in (None, "") or after_cpa in (None, ""):
            return "NEUTRAL"
        before_cpa_value, after_cpa_value = float(before_cpa), float(after_cpa)
        target_cpa = payload.get("target_cpa")
        if action in {"INCREASE_BUDGET", "BUDGET_SCALE_UP"}:
            if after_conversions >= before_conversions * 1.1 and (target_cpa in (None, "") or after_cpa_value <= float(target_cpa)):
                return "EFFECTIVE"
            return "INEFFECTIVE" if after_cpa_value > before_cpa_value * 1.2 else "NEUTRAL"
        if action in {"DECREASE_BUDGET", "BUDGET_REDUCTION"}:
            if after_spend < before_spend and after_cpa_value <= before_cpa_value * 1.05:
                return "EFFECTIVE"
            return "INEFFECTIVE" if after_cpa_value > before_cpa_value * 1.2 else "NEUTRAL"
        if after_cpa_value <= before_cpa_value * 0.9:
            return "EFFECTIVE"
        if after_cpa_value > before_cpa_value * 1.2:
            return "INEFFECTIVE"
        return "NEUTRAL"

    def _advance_experiment(self, experiment_id: str, checkpoint: str, status: str, actor: str, evaluation_id: str) -> None:
        service = AdExperimentService(self.conn)
        experiment = service.get(experiment_id)
        passive_observation = dict(experiment.get("hypothesis_json") or {}).get("mode") == "passive_observation"
        if passive_observation and checkpoint == "D7" and status == "INSUFFICIENT_SAMPLE":
            # A read-only observation is not a user decision merely because its
            # first seven-day window is still small. Later reports keep the
            # visible snapshot current while the task remains system-owned.
            return
        if checkpoint == "D7" and experiment["state"] == "MATURING":
            service.transition(
                experiment_id, "EVALUATING_ADJUSTMENT", actor=actor,
                reason="D7:sample_mature", event_type="EVALUATION_WINDOW_MATURED",
                evidence={"evaluation_id": evaluation_id, "checkpoint": checkpoint},
            )
            experiment = service.get(experiment_id)
        target = "MATURING" if checkpoint in {"D1", "D3"} else FINAL_TO_EXPERIMENT_STATE[status]
        if target in EXPERIMENT_TRANSITIONS.get(experiment["state"], set()):
            service.transition(
                experiment_id, target, actor=actor, reason=f"{checkpoint}:{status}",
                event_type="PERFORMANCE_EVALUATED",
                evidence={"evaluation_id": evaluation_id, "checkpoint": checkpoint, "status": status, "causal_claim": False},
            )

    def _close_episode(self, episode_id: str, experiment_id: str, status: str, actor: str) -> None:
        episodes = EpisodeService(self.conn)
        episode = episodes.get_episode(episode_id)
        while episode["status"] in {"CREATED", "ACTION_EXECUTING"}:
            next_status = "ACTION_EXECUTING" if episode["status"] == "CREATED" else "WAITING_OUTCOME"
            episode = episodes.transition(episode_id, next_status, actor="system", reason="ad_experiment_execution_observed")
        evaluations = self.list(experiment_id)["items"]
        outcome = {
            "experiment_id": experiment_id, "final_status": status,
            "checkpoints": evaluations, "causal_claim": False,
        }
        lesson = {
            "summary": f"D7 evaluation: {status}",
            "next_action": {
                "EFFECTIVE": "SCALE_OR_EXTEND", "INEFFECTIVE": "ROLLBACK_OR_REPAIR",
                "NEUTRAL": "OBSERVE_OR_CLOSE", "INSUFFICIENT_SAMPLE": "WAIT_FOR_SAMPLE",
                "DATA_INCOMPLETE": "FREEZE_AND_REPAIR_DATA", "MIXED_CHANGE": "REBUILD_CLEAN_EXPERIMENT",
            }.get(status, "MANUAL_REVIEW"),
            "causal_claim": False,
        }
        episode = episodes.get_episode(episode_id)
        if episode["status"] == "WAITING_OUTCOME":
            episode = episodes.transition(episode_id, "OUTCOME_READY", outcome=outcome, actor=actor, reason="d7_evaluation_ready")
        if episode["status"] == "OUTCOME_READY":
            episode = episodes.transition(episode_id, "LESSON_REVIEW", lesson=lesson, actor=actor, reason="d7_lesson_generated")
        if episode["status"] == "LESSON_REVIEW":
            episodes.transition(episode_id, "COMPLETED", actor=actor, reason="d7_closed_loop_completed")
        pattern_type = "SUCCESS_PATTERN" if status == "EFFECTIVE" else ("FAILURE_PATTERN" if status == "INEFFECTIVE" else "WARNING_PATTERN")
        KnowledgeService(self.conn).create_candidate(
            episode_id, pattern_type,
            {"experiment_id": experiment_id, "evaluation_status": status, "lesson": lesson, "causal_claim": False},
            idempotency_key=f"d7-knowledge:{experiment_id}",
        )

    def _episode_id(self, experiment_id: str) -> str:
        row = self.conn.execute(
            "SELECT episode_id FROM growth_decision_episode WHERE experiment_id=? ORDER BY created_at DESC LIMIT 1",
            (experiment_id,),
        ).fetchone()
        return str(row["episode_id"] or "") if row else ""

    @staticmethod
    def _serialize(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        for key, value in list(result.items()):
            if key.endswith("_json"):
                result[key] = decode_json(value, {})
        return result
