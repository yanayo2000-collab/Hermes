from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

from app.growth.ad_experiment_evaluator import AdExperimentEvaluator
from app.growth.ad_experiment_service import AdExperimentService
from app.growth.common import canonical_json, decode_json, new_id, payload_hash, utc_now
from app.growth.creative_reference_service import resolve_creative_reference
from app.growth.errors import GrowthStateConflict, GrowthValidationError
from app.growth.schema import ensure_growth_schema


CHECKPOINT_DAYS = {"D1": 1, "D3": 3, "D7": 7}


class CreativeGroupEvaluator:
    """Evaluate one new-creative launch horizontally across its experiment cells.

    A newly created Ad has no meaningful pre-period.  This evaluator therefore
    compares every cell in the same launch over one shared, complete post window.
    It never treats the absence of a pre-period as missing data.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_growth_schema(conn)

    def record_checkpoint(
        self, launch_id: str, payload: Dict[str, Any], *, actor: str, idempotency_key: str,
    ) -> Dict[str, Any]:
        checkpoint = str(payload.get("checkpoint") or "").upper()
        if checkpoint not in CHECKPOINT_DAYS:
            raise GrowthValidationError("invalid_evaluation_checkpoint")
        experiments = self._group(launch_id)
        metrics = dict(payload.get("metrics_by_experiment") or {})
        expected_ids = {str(item["experiment_id"]) for item in experiments}
        if set(metrics) != expected_ids:
            raise GrowthValidationError("creative_group_metrics_incomplete")
        digest = payload_hash({"launch_id": launch_id, **payload})
        route = "creative_group.evaluate"
        existing = self.conn.execute(
            "SELECT request_hash,response_json FROM growth_idempotency_record WHERE route_key=? AND idempotency_key=?",
            (route, idempotency_key),
        ).fetchone()
        if existing:
            if str(existing["request_hash"]) != digest:
                raise GrowthStateConflict("idempotency_key_payload_conflict")
            return decode_json(existing["response_json"], {})
        if self.conn.execute(
            "SELECT 1 FROM ad_creative_group_evaluation WHERE launch_id=? AND checkpoint=?",
            (launch_id, checkpoint),
        ).fetchone():
            raise GrowthStateConflict("creative_group_checkpoint_already_recorded")

        quality = str(payload.get("data_quality_status") or "PASS").upper()
        actual_days = int(payload.get("actual_days") or CHECKPOINT_DAYS[checkpoint])
        winner_id, decision_status, reason, ranking = self._decision(
            checkpoint, metrics, quality, actual_days=actual_days,
        )
        if checkpoint == "D7" and decision_status == "DEFER":
            raise GrowthValidationError("creative_group_d7_not_mature")

        evaluation_id = new_id("cregroup")
        now = utc_now()
        confidence_tier = self._confidence_tier(metrics) if winner_id else "NONE"
        evidence = {
            "single_variable": "creative_direction",
            "comparison_method": "same_launch_shared_post_window",
            "causal_claim": False,
            "reason": reason,
            "confidence_tier": confidence_tier,
            "strong_action_sample_eligible": confidence_tier == "HIGH",
            "requires_operator_approval": True,
            "meta_writes_performed": False,
            "actor": actor,
        }
        result = {
            "group_evaluation_id": evaluation_id,
            "launch_id": launch_id,
            "checkpoint": checkpoint,
            "window": dict(payload.get("window") or {}),
            "metrics_by_experiment": metrics,
            "ranking": ranking,
            "winner_experiment_id": winner_id,
            "decision_status": decision_status,
            "confidence_tier": confidence_tier,
            "actual_days": actual_days,
            "data_quality_status": quality,
            "evidence": evidence,
            "next_generation": {},
        }
        with self.conn:
            self.conn.execute(
                """INSERT INTO ad_creative_group_evaluation
                (group_evaluation_id,launch_id,checkpoint,window_json,
                 metrics_by_experiment_json,ranking_json,winner_experiment_id,
                 decision_status,actual_days,data_quality_status,evidence_json,evaluated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    evaluation_id, launch_id, checkpoint, canonical_json(result["window"]),
                    canonical_json(metrics), canonical_json(ranking), winner_id,
                    decision_status, actual_days, quality, canonical_json(evidence), now,
                ),
            )
            if checkpoint == "D7" and decision_status == "WINNER":
                result["next_generation"] = self._propose_next_generation(
                    launch_id, evaluation_id, winner_id, experiments, metrics, evidence, now,
                )
            self._advance_group(experiments, checkpoint, decision_status, actor, evaluation_id, winner_id, now)
            self.conn.execute(
                """INSERT INTO growth_idempotency_record
                (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
                VALUES (?,?,?,201,?,?)""",
                (route, idempotency_key, digest, canonical_json(result), now),
            )
        return result

    def list(self, launch_id: str) -> Dict[str, Any]:
        rows = self.conn.execute(
            """SELECT * FROM ad_creative_group_evaluation WHERE launch_id=?
            ORDER BY CASE checkpoint WHEN 'D1' THEN 1 WHEN 'D3' THEN 3 ELSE 7 END""",
            (launch_id,),
        ).fetchall()
        generations = self.conn.execute(
            "SELECT * FROM ad_creative_generation WHERE launch_id=? ORDER BY created_at",
            (launch_id,),
        ).fetchall()
        return {
            "launch_id": launch_id,
            "checkpoints": [self._serialize(row) for row in rows],
            "generations": [self._serialize(row) for row in generations],
        }

    def evaluate_due(self, *, as_of_date: str = "") -> Dict[str, Any]:
        if not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_creative_performance_daily'",
        ).fetchone():
            return {"evaluated": [], "deferred": [], "count": 0, "reason": "performance_table_not_ready"}
        try:
            as_of = date.fromisoformat(as_of_date) if as_of_date else datetime.now(timezone.utc).date()
        except ValueError as exc:
            raise GrowthValidationError("invalid_as_of_date") from exc
        launch_rows = self.conn.execute(
            """SELECT DISTINCT source_report_id FROM ad_experiment
            WHERE source_report_id<>'' AND source_ad_id<>''
              AND state IN ('RUNNING','MATURING','EVALUATING_ADJUSTMENT')
            ORDER BY source_report_id""",
        ).fetchall()
        metrics_reader = AdExperimentEvaluator(self.conn)
        created: List[Dict[str, Any]] = []
        deferred: List[Dict[str, Any]] = []
        for launch_row in launch_rows:
            launch_id = str(launch_row["source_report_id"] or "")
            try:
                experiments = self._group(launch_id)
            except GrowthValidationError:
                continue
            boundary = self._first_complete_day(experiments)
            for checkpoint, required_days in CHECKPOINT_DAYS.items():
                if self.conn.execute(
                    "SELECT 1 FROM ad_creative_group_evaluation WHERE launch_id=? AND checkpoint=?",
                    (launch_id, checkpoint),
                ).fetchone():
                    continue
                elapsed_days = (as_of - boundary).days
                if elapsed_days < required_days:
                    continue
                actual_days = required_days
                if checkpoint == "D7":
                    actual_days = min(14, max(7, elapsed_days))
                window_end = boundary + timedelta(days=actual_days - 1)
                metrics: Dict[str, Dict[str, Any]] = {}
                quality_pass = True
                for experiment in experiments:
                    aggregate = metrics_reader._aggregate_daily(
                        str(experiment["source_ad_id"]), boundary, window_end,
                    )
                    if int(aggregate.get("day_count") or 0) < actual_days:
                        metrics = {}
                        break
                    quality_pass = quality_pass and bool(aggregate.get("quality_pass"))
                    metrics[str(experiment["experiment_id"])] = self._core_metrics(aggregate)
                if len(metrics) != len(experiments):
                    deferred.append({
                        "launch_id": launch_id, "checkpoint": checkpoint,
                        "reason": "shared_window_incomplete", "actual_days": actual_days,
                    })
                    continue
                quality = "PASS" if quality_pass else "DATA_INCOMPLETE"
                _, status, reason, _ = self._decision(
                    checkpoint, metrics, quality, actual_days=actual_days,
                )
                if checkpoint == "D7" and status == "DEFER":
                    deferred.append({
                        "launch_id": launch_id, "checkpoint": checkpoint,
                        "reason": reason, "actual_days": actual_days,
                    })
                    continue
                created.append(self.record_checkpoint(
                    launch_id,
                    {
                        "checkpoint": checkpoint,
                        "window": {"start": boundary.isoformat(), "end": window_end.isoformat()},
                        "metrics_by_experiment": metrics,
                        "data_quality_status": quality,
                        "actual_days": actual_days,
                    },
                    actor="growth-experiment-evaluator",
                    idempotency_key=f"scheduled:{launch_id}:{checkpoint}:{boundary.isoformat()}:{actual_days}",
                ))
        return {
            "evaluated": created, "deferred": deferred, "count": len(created),
            "as_of_date": as_of.isoformat(),
        }

    def _group(self, launch_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM ad_experiment WHERE source_report_id=? ORDER BY created_at,experiment_code",
            (str(launch_id or "").strip(),),
        ).fetchall()
        if not 2 <= len(rows) <= 4:
            raise GrowthValidationError("creative_experiment_group_must_have_2_to_4_cells")
        result: List[Dict[str, Any]] = []
        roles: List[str] = []
        directions: set[str] = set()
        ad_ids: set[str] = set()
        accounts: set[str] = set()
        countries: set[str] = set()
        campaigns: set[str] = set()
        invariant_values: Dict[str, set[str]] = {
            "audience_strategy": set(), "daily_budget_usd": set(), "attribution_spec": set(),
        }
        for row in rows:
            experiment = AdExperimentService._serialize(row)
            control = dict(experiment.get("control_definition_json") or {})
            hypothesis = dict(experiment.get("hypothesis_json") or {})
            if str(control.get("test_variable") or "") != "creative_direction":
                raise GrowthValidationError("launch_is_not_creative_direction_experiment")
            role = str(control.get("role") or "").upper()
            roles.append(role)
            direction = dict(hypothesis.get("creative_direction") or {})
            direction_key = str(direction.get("key") or direction.get("direction_id") or direction.get("code") or "").strip()
            if not direction_key or direction_key in directions:
                raise GrowthValidationError("creative_direction_identity_invalid")
            directions.add(direction_key)
            ad_id = str(experiment.get("source_ad_id") or "").strip()
            if not ad_id or ad_id in ad_ids:
                raise GrowthValidationError("creative_group_source_ad_identity_invalid")
            ad_ids.add(ad_id)
            accounts.add(str(experiment.get("account_id") or ""))
            countries.add(str(experiment.get("country") or "").upper())
            campaigns.add(str(experiment.get("source_campaign_id") or ""))
            for key in invariant_values:
                value = control.get(key, hypothesis.get(key))
                if value not in (None, "", [], {}):
                    invariant_values[key].add(canonical_json(value))
            result.append({
                **experiment, "control": control, "hypothesis": hypothesis,
                "direction_key": direction_key,
            })
        if roles.count("BASELINE") != 1 or any(role not in {"BASELINE", "CHALLENGER"} for role in roles):
            raise GrowthValidationError("creative_group_requires_one_baseline")
        if len(accounts) != 1 or len(countries) != 1 or len(campaigns) != 1 or "" in campaigns:
            raise GrowthValidationError("creative_group_account_country_campaign_mismatch")
        if any(len(values) > 1 for values in invariant_values.values()):
            raise GrowthValidationError("creative_group_non_creative_invariant_mismatch")
        return result

    def _first_complete_day(self, experiments: List[Dict[str, Any]]) -> date:
        activation_dates = [
            self._verified_activation_date(str(item["experiment_id"]), str(item["created_at"]))
            for item in experiments
        ]
        ad_ids = [str(item.get("source_ad_id") or "") for item in experiments if item.get("source_ad_id")]
        revision_table = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_creative_revision_window'",
        ).fetchone()
        if revision_table and ad_ids:
            placeholders = ",".join("?" for _ in ad_ids)
            revision_rows = self.conn.execute(
                f"""
                SELECT effective_from FROM ad_creative_revision_window
                WHERE status='CURRENT' AND ad_id IN ({placeholders})
                """,
                tuple(ad_ids),
            ).fetchall()
            activation_dates.extend(
                self._as_date(str(row["effective_from"] or "")) for row in revision_rows
            )
        return max(activation_dates) + timedelta(days=1)

    def _verified_activation_date(self, experiment_id: str, fallback: str) -> date:
        rows = self.conn.execute(
            """SELECT to_state,event_type,evidence_json,created_at FROM ad_experiment_events
            WHERE experiment_id=? ORDER BY created_at,event_id""",
            (experiment_id,),
        ).fetchall()
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
                return self._as_date(str(row["created_at"] or fallback))
        return self._as_date(fallback_running or fallback)

    @staticmethod
    def _as_date(value: str) -> date:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except ValueError:
            return datetime.now(timezone.utc).date()

    @staticmethod
    def _core_metrics(aggregate: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "spend": float(aggregate.get("spend") or 0),
            "impressions": float(aggregate.get("impressions") or 0),
            "clicks": float(aggregate.get("clicks") or 0),
            "installs": int(aggregate.get("installs") or 0),
            "real_bind_count": int(aggregate.get("real_bind_count") or 0),
            "cpi": aggregate.get("cpi"),
            "ctr": aggregate.get("ctr"),
            "real_bind_cpa": aggregate.get("real_bind_cpa"),
        }

    @classmethod
    def _decision(
        cls, checkpoint: str, metrics: Dict[str, Dict[str, Any]], quality: str, *, actual_days: int,
    ) -> tuple[str, str, str, List[Dict[str, Any]]]:
        ranking = cls._ranking(metrics)
        if quality != "PASS":
            status = "DATA_INCOMPLETE" if checkpoint != "D7" or actual_days >= 14 else "DEFER"
            return "", status, "data_quality_not_pass", ranking
        if checkpoint == "D1":
            return "", "OBSERVE", "d1_observation_only", ranking
        if checkpoint == "D3":
            installs = [int(item.get("installs") or 0) for item in metrics.values()]
            early_sample = all(value >= 20 for value in installs) or (sum(installs) >= 60 and all(value >= 10 for value in installs))
            if early_sample:
                lead = cls._relative_lead(ranking[0], ranking[1])
                if lead >= 0.10:
                    return str(ranking[0]["experiment_id"]), "PROVISIONAL", "d3_directional_leader", ranking
                return "", "TIE", "d3_difference_not_material", ranking
            return "", "INCONCLUSIVE", "d3_sample_not_mature", ranking
        mature = all(
            int(item.get("installs") or 0) >= 50
            and int(item.get("real_bind_count") or 0) >= 3
            and item.get("cpi") not in (None, "")
            and item.get("ctr") not in (None, "")
            and item.get("real_bind_cpa") not in (None, "")
            for item in metrics.values()
        )
        if not mature:
            status = "INCONCLUSIVE" if actual_days >= 14 else "DEFER"
            return "", status, "group_sample_not_mature", ranking
        first, second = ranking[0], ranking[1]
        cpa_lead = (float(second["real_bind_cpa"]) - float(first["real_bind_cpa"])) / float(second["real_bind_cpa"])
        cpi_ok = float(first["cpi"]) <= float(second["cpi"]) * 1.2
        ctr_ok = float(first["ctr"]) >= float(second["ctr"]) * 0.8
        if cpa_lead >= 0.15 and cpi_ok and ctr_ok:
            return str(first["experiment_id"]), "WINNER", "cpa_lead_with_guardrails", ranking
        return "", "TIE", "no_materially_better_cell", ranking

    @staticmethod
    def _relative_lead(first: Dict[str, Any], second: Dict[str, Any]) -> float:
        if int(first.get("real_bind_count") or 0) >= 3 and int(second.get("real_bind_count") or 0) >= 3:
            first_value = first.get("real_bind_cpa")
            second_value = second.get("real_bind_cpa")
        else:
            first_value = first.get("cpi")
            second_value = second.get("cpi")
        if first_value in (None, "") or second_value in (None, "") or float(second_value) <= 0:
            return 0.0
        return max(0.0, (float(second_value) - float(first_value)) / float(second_value))

    @staticmethod
    def _confidence_tier(metrics: Dict[str, Dict[str, Any]]) -> str:
        if all(
            int(item.get("installs") or 0) >= 100 and int(item.get("real_bind_count") or 0) >= 10
            for item in metrics.values()
        ):
            return "HIGH"
        if all(int(item.get("real_bind_count") or 0) >= 5 for item in metrics.values()):
            return "TRUSTED"
        return "DIRECTIONAL"

    @staticmethod
    def _ranking(metrics: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for experiment_id, raw in metrics.items():
            item = dict(raw or {})
            spend = float(item.get("spend") or 0)
            installs = int(item.get("installs") or 0)
            joins = int(item.get("real_bind_count") or 0)
            normalized.append({
                "experiment_id": experiment_id,
                "spend": spend,
                "installs": installs,
                "real_bind_count": joins,
                "cpi": item.get("cpi") if item.get("cpi") not in (None, "") else (spend / installs if installs else None),
                "ctr": item.get("ctr"),
                "real_bind_cpa": item.get("real_bind_cpa") if item.get("real_bind_cpa") not in (None, "") else (spend / joins if joins else None),
            })
        return sorted(
            normalized,
            key=lambda item: (
                item["real_bind_cpa"] is None,
                float(item["real_bind_cpa"] or 10**12),
                -int(item["real_bind_count"]),
                item["cpi"] is None,
                float(item["cpi"] or 10**12),
                -(float(item["ctr"] or 0)),
            ),
        )

    def _propose_next_generation(
        self, launch_id: str, evaluation_id: str, winner_id: str,
        experiments: List[Dict[str, Any]], metrics: Dict[str, Dict[str, Any]],
        evidence: Dict[str, Any], now: str,
    ) -> Dict[str, Any]:
        winner = next(item for item in experiments if str(item["experiment_id"]) == winner_id)
        hypothesis = dict(winner["hypothesis"])
        direction = dict(hypothesis.get("creative_direction") or {})
        winning_key = str(direction.get("key") or direction.get("direction_id") or winner["direction_key"])
        account_id = str(winner.get("account_id") or "").removeprefix("act_")
        source_ad_id = str(winner.get("source_ad_id") or "")
        reference_asset = resolve_creative_reference(
            self.conn,
            ad_id=source_ad_id,
            accessible_account_ids={account_id},
            experiment_id=winner_id,
        )
        prompt_lineage = {
            "parent_launch_id": launch_id,
            "parent_experiment_id": winner_id,
            "winning_direction_key": winning_key,
            "winning_creative_id": str(winner.get("source_creative_id") or ""),
            "prompt_version": str(hypothesis.get("prompt_version") or ""),
            "prompt_hash": str(hypothesis.get("prompt_hash") or ""),
            "metrics": dict(metrics.get(winner_id) or {}),
            "reference_asset": reference_asset,
        }
        proposals = [
            {"role": "BASELINE", "direction_key": winning_key, "mutation": "NONE", "single_variable": "creative_prompt"},
            {"role": "CHALLENGER", "direction_key": winning_key, "mutation": "HOOK", "single_variable": "creative_prompt"},
            {"role": "CHALLENGER", "direction_key": "UNTESTED_FIXED_DIRECTION", "mutation": "DIRECTION", "single_variable": "creative_direction"},
        ]
        generation_id = new_id("cregen")
        generation_evidence = {
            **evidence,
            "source_group_evaluation_id": evaluation_id,
            "requires_operator_approval": True,
            "meta_writes_performed": False,
        }
        self.conn.execute(
            """INSERT INTO ad_creative_generation
            (generation_id,launch_id,parent_generation_id,source_group_evaluation_id,
             winning_experiment_id,winning_direction_key,prompt_lineage_json,
             variant_proposals_json,evidence_json,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,'PROPOSED',?,?)""",
            (
                generation_id, launch_id, "", evaluation_id, winner_id, winning_key,
                canonical_json(prompt_lineage), canonical_json(proposals),
                canonical_json(generation_evidence), now, now,
            ),
        )
        return {
            "generation_id": generation_id,
            "status": "PROPOSED",
            "prompt_lineage": prompt_lineage,
            "variant_proposals": proposals,
            "requires_operator_approval": True,
            "meta_writes_performed": False,
        }

    def _advance_group(
        self, experiments: List[Dict[str, Any]], checkpoint: str, decision_status: str,
        actor: str, evaluation_id: str, winner_id: str, now: str,
    ) -> None:
        target = "MATURING" if checkpoint in {"D1", "D3"} else "RECOMMENDATION_READY"
        for experiment in experiments:
            current = str(experiment["state"])
            if current == target:
                continue
            allowed = {
                "RUNNING": {"MATURING", "RECOMMENDATION_READY"},
                "MATURING": {"RECOMMENDATION_READY"},
                "EVALUATING_ADJUSTMENT": {"RECOMMENDATION_READY"},
            }
            if target not in allowed.get(current, set()):
                raise GrowthStateConflict(f"creative_group_state_transition_invalid:{current}:{target}")
            experiment_id = str(experiment["experiment_id"])
            reason = f"{checkpoint}:{decision_status}"
            cursor = self.conn.execute(
                "UPDATE ad_experiment SET state=?,state_reason=?,updated_at=? WHERE experiment_id=? AND state=?",
                (target, reason, now, experiment_id, current),
            )
            if cursor.rowcount != 1:
                raise GrowthStateConflict("ad_experiment_changed_concurrently")
            event_id = new_id("adevent")
            event_evidence = canonical_json({
                "group_evaluation_id": evaluation_id,
                "checkpoint": checkpoint,
                "decision_status": decision_status,
                "winner_experiment_id": winner_id,
                "causal_claim": False,
            })
            self.conn.execute(
                """INSERT INTO ad_experiment_events
                (event_id,experiment_id,from_state,to_state,event_type,actor,reason,evidence_json,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (event_id, experiment_id, current, target, "CREATIVE_GROUP_EVALUATED", actor, reason, event_evidence, now),
            )
            self.conn.execute(
                """INSERT INTO growth_state_transition
                (transition_id,entity_type,entity_id,from_status,to_status,reason,actor,created_at)
                VALUES (?,'AD_EXPERIMENT',?,?,?,?,?,?)""",
                (new_id("transition"), experiment_id, current, target, reason, actor, now),
            )

    @staticmethod
    def _serialize(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        for key in list(result):
            if key.endswith("_json"):
                result[key] = decode_json(result[key], [] if "ranking" in key or "proposals" in key else {})
        return result
