from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict

from app.growth.ad_experiment_evaluator import AdExperimentEvaluator
from app.growth.audience_strategy import audience_strategy
from app.growth.common import canonical_json, decode_json, new_id, payload_hash, utc_now
from app.growth.errors import GrowthStateConflict, GrowthValidationError
from app.growth.schema import ensure_growth_schema


class AudienceExperimentEvaluator:
    """Evaluate one randomized audience pair without changing it mid-window."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_growth_schema(conn)

    def record_checkpoint(
        self, launch_id: str, payload: Dict[str, Any], *, actor: str, idempotency_key: str,
    ) -> Dict[str, Any]:
        checkpoint = str(payload.get("checkpoint") or "").upper()
        if checkpoint not in {"D1", "D3", "D7"}:
            raise GrowthValidationError("invalid_evaluation_checkpoint")
        experiments = self._pair(launch_id)
        by_role = {str(dict(item["control"]).get("role") or "").upper(): item for item in experiments}
        if set(by_role) != {"BASELINE", "CHALLENGER"}:
            raise GrowthValidationError("audience_experiment_roles_invalid")
        metrics = dict(payload.get("metrics_by_experiment") or {})
        expected_ids = {str(item["experiment_id"]) for item in experiments}
        if set(metrics) != expected_ids:
            raise GrowthValidationError("audience_pair_metrics_incomplete")
        digest = payload_hash({"launch_id": launch_id, **payload})
        route = "audience_pair.evaluate"
        existing = self.conn.execute(
            "SELECT request_hash,response_json FROM growth_idempotency_record WHERE route_key=? AND idempotency_key=?",
            (route, idempotency_key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != digest:
                raise GrowthStateConflict("idempotency_key_payload_conflict")
            return decode_json(existing["response_json"], {})
        if self.conn.execute(
            "SELECT 1 FROM ad_audience_pair_evaluation WHERE launch_id=? AND checkpoint=?",
            (launch_id, checkpoint),
        ).fetchone():
            raise GrowthStateConflict("audience_pair_checkpoint_already_recorded")

        quality = str(payload.get("data_quality_status") or "PASS").upper()
        winner_id, status, reason = self._winner(checkpoint, metrics, quality)
        baseline = by_role["BASELINE"]
        challenger = by_role["CHALLENGER"]
        evaluation_id = new_id("audpair")
        now = utc_now()
        evidence = {
            "single_variable": "audience_strategy",
            "frozen_creative_id": str(dict(baseline["hypothesis"]).get("frozen_creative", {}).get("image_id") or ""),
            "randomization": dict(baseline["control"]).get("meta_randomization") or {},
            "reason": reason,
            "actor": actor,
        }
        result = {
            "pair_evaluation_id": evaluation_id,
            "launch_id": launch_id,
            "checkpoint": checkpoint,
            "baseline_experiment_id": baseline["experiment_id"],
            "challenger_experiment_id": challenger["experiment_id"],
            "metrics_by_experiment": metrics,
            "winner_experiment_id": winner_id,
            "decision_status": status,
            "evidence": evidence,
            "next_generation": {},
        }
        with self.conn:
            self.conn.execute(
                """INSERT INTO ad_audience_pair_evaluation
                (pair_evaluation_id,launch_id,checkpoint,baseline_experiment_id,
                 challenger_experiment_id,metrics_json,winner_experiment_id,
                 decision_status,evidence_json,evaluated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (evaluation_id, launch_id, checkpoint, baseline["experiment_id"],
                 challenger["experiment_id"], canonical_json(metrics), winner_id,
                 status, canonical_json(evidence), now),
            )
            if checkpoint == "D7" and status == "WINNER":
                result["next_generation"] = self._propose_next_generation(
                    launch_id, evaluation_id, winner_id, experiments, evidence, now,
                )
            self.conn.execute(
                """INSERT INTO growth_idempotency_record
                (route_key,idempotency_key,request_hash,response_status,response_json,created_at)
                VALUES (?,?,?,201,?,?)""",
                (route, idempotency_key, digest, canonical_json(result), now),
            )
        return result

    def list(self, launch_id: str) -> Dict[str, Any]:
        rows = self.conn.execute(
            "SELECT * FROM ad_audience_pair_evaluation WHERE launch_id=? ORDER BY CASE checkpoint WHEN 'D1' THEN 1 WHEN 'D3' THEN 3 ELSE 7 END",
            (launch_id,),
        ).fetchall()
        generations = self.conn.execute(
            "SELECT * FROM ad_audience_generation WHERE launch_id=? ORDER BY created_at",
            (launch_id,),
        ).fetchall()
        return {
            "launch_id": launch_id,
            "checkpoints": [self._serialize(row) for row in rows],
            "generations": [self._serialize(row) for row in generations],
        }

    def evaluate_due(self, *, as_of_date: str = "") -> Dict[str, Any]:
        """Create paired D1/D3/D7 evaluations from persisted ad-level facts."""
        if not self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_creative_performance_daily'",
        ).fetchone():
            return {"evaluated": [], "count": 0, "reason": "performance_table_not_ready"}
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
        created = []
        for launch_row in launch_rows:
            launch_id = str(launch_row["source_report_id"] or "")
            try:
                experiments = self._pair(launch_id)
            except GrowthValidationError:
                continue
            boundary = max(
                metrics_reader._evaluation_boundary(item["experiment_id"], item["created_at"])
                for item in experiments
            )
            for checkpoint, days in (("D1", 1), ("D3", 3), ("D7", 7)):
                if as_of < boundary + timedelta(days=days) or self.conn.execute(
                    "SELECT 1 FROM ad_audience_pair_evaluation WHERE launch_id=? AND checkpoint=?",
                    (launch_id, checkpoint),
                ).fetchone():
                    continue
                metrics: Dict[str, Dict[str, Any]] = {}
                quality_pass = True
                for experiment in experiments:
                    aggregate = metrics_reader._aggregate_daily(
                        experiment["source_ad_id"], boundary, boundary + timedelta(days=days - 1),
                    )
                    if int(aggregate.get("day_count") or 0) < days:
                        metrics = {}
                        break
                    quality_pass = quality_pass and bool(aggregate.get("quality_pass"))
                    metrics[experiment["experiment_id"]] = {
                        "spend": float(aggregate.get("spend") or 0),
                        "installs": int(aggregate.get("installs") or 0),
                        "real_bind_count": int(aggregate.get("real_bind_count") or 0),
                    }
                if len(metrics) != 2:
                    continue
                created.append(self.record_checkpoint(
                    launch_id,
                    {
                        "checkpoint": checkpoint,
                        "metrics_by_experiment": metrics,
                        "data_quality_status": "PASS" if quality_pass else "DATA_INCOMPLETE",
                    },
                    actor="growth-experiment-evaluator",
                    idempotency_key=f"scheduled:{launch_id}:{checkpoint}:{boundary.isoformat()}",
                ))
        return {"evaluated": created, "count": len(created), "as_of_date": as_of.isoformat()}

    def _pair(self, launch_id: str) -> list[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM ad_experiment WHERE source_report_id=? ORDER BY created_at,experiment_code",
            (str(launch_id or "").strip(),),
        ).fetchall()
        if len(rows) != 2:
            raise GrowthValidationError("audience_experiment_pair_required")
        result = []
        frozen_ids = set()
        study_ids = set()
        for row in rows:
            hypothesis = decode_json(row["hypothesis_json"], {})
            control = decode_json(row["control_definition_json"], {})
            if str(control.get("test_variable") or "") != "audience_strategy":
                raise GrowthValidationError("launch_is_not_audience_experiment")
            frozen_ids.add(str(dict(hypothesis.get("frozen_creative") or {}).get("image_id") or ""))
            randomization = dict(control.get("meta_randomization") or {})
            study_ids.add(str(randomization.get("study_id") or ""))
            if not randomization.get("readback_verified") or not str(randomization.get("study_cell_id") or ""):
                raise GrowthValidationError("audience_randomization_readback_required")
            result.append({
                "experiment_id": row["experiment_id"],
                "source_ad_id": str(row["source_ad_id"] or ""),
                "created_at": str(row["created_at"] or ""),
                "hypothesis": hypothesis,
                "control": control,
            })
        if len(frozen_ids) != 1 or "" in frozen_ids:
            raise GrowthValidationError("audience_experiment_frozen_creative_mismatch")
        if len(study_ids) != 1 or "" in study_ids:
            raise GrowthValidationError("audience_experiment_study_mismatch")
        return result

    @staticmethod
    def _winner(checkpoint: str, metrics: Dict[str, Any], quality: str) -> tuple[str, str, str]:
        if quality != "PASS":
            return "", "DATA_INCOMPLETE", "data_quality_not_pass"
        if checkpoint == "D1":
            return "", "OBSERVE", "d1_observation_only"
        normalized = []
        for experiment_id, raw in metrics.items():
            item = dict(raw or {})
            spend = float(item.get("spend") or 0)
            installs = int(item.get("installs") or 0)
            joins = int(item.get("real_bind_count") or 0)
            normalized.append((experiment_id, spend, installs, joins))
        if all(item[3] >= 10 for item in normalized):
            winner = min(normalized, key=lambda item: item[1] / item[3])
            return winner[0], "WINNER" if checkpoint == "D7" else "PROVISIONAL", "lowest_real_join_cpa"
        if all(item[2] >= 30 for item in normalized):
            winner = min(normalized, key=lambda item: item[1] / item[2])
            return winner[0], "WINNER" if checkpoint == "D7" else "PROVISIONAL", "lowest_cpi_before_join_maturity"
        return "", "INCONCLUSIVE", "pair_sample_not_mature"

    def _propose_next_generation(
        self, launch_id: str, evaluation_id: str, winner_id: str,
        experiments: list[Dict[str, Any]], evidence: Dict[str, Any], now: str,
    ) -> Dict[str, Any]:
        winner = next(item for item in experiments if item["experiment_id"] == winner_id)
        winning_key = str(dict(winner["hypothesis"].get("audience_strategy") or {}).get("strategy_key") or "BROAD")
        tested = {
            str(dict(item["hypothesis"].get("audience_strategy") or {}).get("strategy_key") or "BROAD")
            for item in experiments
        }
        candidates = [key for key in ("DIGITAL_SELLER", "FAMILY_HOME", "SIDE_HUSTLE") if key not in tested]
        mutations = [{
            "operation": "PROPOSE_PAIR",
            "baseline_strategy": winning_key,
            "challenger_strategy": candidates[0] if candidates else winning_key,
            "rule": "freeze_winning_creative_and_copy; change_audience_only",
            "requires_meta_id_revalidation": True,
            "requires_operator_approval": True,
        }]
        generation_id = new_id("audgen")
        self.conn.execute(
            """INSERT INTO ad_audience_generation
            (generation_id,launch_id,parent_generation_id,source_pair_evaluation_id,
             winning_strategy_key,candidate_strategy_keys_json,keyword_mutations_json,
             evidence_json,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?, 'PROPOSED',?,?)""",
            (generation_id, launch_id, "", evaluation_id, winning_key,
             canonical_json(candidates), canonical_json(mutations), canonical_json(evidence), now, now),
        )
        return {
            "generation_id": generation_id,
            "status": "PROPOSED",
            "winning_strategy": audience_strategy(winning_key),
            "candidate_strategy_keys": candidates,
            "keyword_mutations": mutations,
        }

    @staticmethod
    def _serialize(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        for key in list(result):
            if key.endswith("_json"):
                result[key] = decode_json(result[key], [] if "keys" in key or "mutations" in key else {})
        return result
