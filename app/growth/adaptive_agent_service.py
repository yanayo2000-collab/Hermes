from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from app.growth.common import canonical_json, decode_json, new_id, payload_hash, utc_now
from app.growth.context_service import ContextService
from app.growth.errors import GrowthNotFound, GrowthStateConflict, GrowthValidationError
from app.growth.execution_service import ExecutionTaskService
from app.growth.pattern_mining_service import PATTERN_CONTEXT_FIELDS, outcome_succeeded
from app.growth.schema import ensure_growth_schema


LOW_RISK_AUTOMATIC_ACTIONS = frozenset({"OBSERVE", "CHECK_DATA"})


class AdaptiveGrowthAgentService:
    """Evidence-backed recommendation and simulation; no causal claim is made."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_growth_schema(conn)

    def recommend(
        self, context_snapshot_id: str, *, created_by: str = "growth-agent",
        idempotency_key: str = "",
    ) -> Dict[str, Any]:
        context = ContextService(self.conn).get_snapshot(context_snapshot_id)
        replay = self._idempotent_response(
            "strategy_recommendation.create", idempotency_key,
            payload_hash({"context_snapshot_id": context_snapshot_id}),
        )
        if replay is not None:
            return replay
        candidates: List[Dict[str, Any]] = []
        for row in self.conn.execute(
            "SELECT * FROM growth_strategy_knowledge WHERE status='ACTIVE' ORDER BY activated_at DESC"
        ).fetchall():
            pattern = decode_json(row["pattern_json"], {})
            pattern_context = dict(pattern.get("context") or {})
            matched = sum(
                1 for field in PATTERN_CONTEXT_FIELDS
                if pattern_context.get(field) and pattern_context.get(field) == context.get(field)
            )
            if matched == 0:
                continue
            support = int(pattern.get("support_count") or 1)
            rate = float(pattern.get("success_rate") or 0)
            confidence = min(1.0, (matched / len(PATTERN_CONTEXT_FIELDS)) * 0.7 + min(support, 10) / 10 * 0.2 + rate * 0.1)
            candidates.append({
                "knowledge_id": row["knowledge_id"],
                "action_type": str(pattern.get("action_type") or "OBSERVE").upper(),
                "confidence": confidence,
                "matched_fields": matched,
                "support_count": support,
                "success_rate": rate,
            })
        if not candidates:
            raise GrowthStateConflict("active_knowledge_not_found")
        candidates.sort(key=lambda item: (item["confidence"], item["support_count"]), reverse=True)
        best = candidates[0]
        recommendation_id = new_id("strategy")
        now = utc_now()
        rationale = {
            "matched_fields": best["matched_fields"],
            "support_count": best["support_count"],
            "historical_success_rate": best["success_rate"],
            "causal_claim": False,
        }
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO growth_strategy_recommendation
                (strategy_recommendation_id, context_snapshot_id, action_type, rationale_json,
                 source_knowledge_ids_json, confidence, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'PROPOSED', ?, ?, ?)
                """,
                (
                    recommendation_id, context_snapshot_id, best["action_type"], canonical_json(rationale),
                    canonical_json([best["knowledge_id"]]), round(best["confidence"], 4), created_by, now, now,
                ),
            )
            result = self.get_recommendation(recommendation_id)
            self._store_idempotent_response(
                "strategy_recommendation.create", idempotency_key,
                payload_hash({"context_snapshot_id": context_snapshot_id}), result,
            )
        return result

    def simulate(
        self, context_snapshot_id: str, proposed_action: str, *, idempotency_key: str = "",
    ) -> Dict[str, Any]:
        ContextService(self.conn).get_snapshot(context_snapshot_id)
        action = str(proposed_action or "").strip().upper()
        if not action:
            raise GrowthValidationError("proposed_action_is_required")
        digest = payload_hash({
            "context_snapshot_id": context_snapshot_id, "proposed_action": action,
        })
        replay = self._idempotent_response("simulation.create", idempotency_key, digest)
        if replay is not None:
            return replay
        rows = self.conn.execute(
            """
            SELECT e.episode_id, e.outcome_json
            FROM growth_decision_episode e
            JOIN growth_decision d ON d.decision_id=e.decision_id
            WHERE e.status='COMPLETED' AND d.selected_action=?
            ORDER BY e.completed_at DESC LIMIT 100
            """,
            (action,),
        ).fetchall()
        successes = sum(outcome_succeeded(decode_json(row["outcome_json"], {})) for row in rows)
        sample_count = len(rows)
        expected = successes / sample_count if sample_count else 0.0
        risk = (
            "INSUFFICIENT_DATA" if sample_count < 3
            else "LOW" if expected >= 0.7
            else "MEDIUM" if expected >= 0.4
            else "HIGH"
        )
        simulation_id = new_id("simulation")
        created_at = utc_now()
        assumptions = {
            "method": "historical_episode_frequency",
            "causal_claim": False,
            "context_snapshot_id": context_snapshot_id,
        }
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO growth_simulation
                (simulation_id, context_snapshot_id, proposed_action, sample_count,
                 expected_success_rate, risk_level, evidence_episode_ids_json,
                 assumptions_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    simulation_id, context_snapshot_id, action, sample_count, round(expected, 4), risk,
                    canonical_json([row["episode_id"] for row in rows]), canonical_json(assumptions), created_at,
                ),
            )
            result = self.get_simulation(simulation_id)
            self._store_idempotent_response("simulation.create", idempotency_key, digest, result)
        return result

    def transition_recommendation(
        self, strategy_recommendation_id: str, to_status: str, *, actor: str,
    ) -> Dict[str, Any]:
        current = self.get_recommendation(strategy_recommendation_id)
        target = str(to_status or "").strip().upper()
        allowed = {"PROPOSED": {"APPROVED", "REJECTED"}, "APPROVED": {"EXECUTED"}}
        if target not in allowed.get(current["status"], set()):
            raise GrowthStateConflict("illegal_strategy_recommendation_transition")
        now = utc_now()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE growth_strategy_recommendation SET status=?, updated_at=?
                WHERE strategy_recommendation_id=? AND status=?
                """,
                (target, now, strategy_recommendation_id, current["status"]),
            )
            if cursor.rowcount != 1:
                raise GrowthStateConflict("strategy_recommendation_changed_concurrently")
            self.conn.execute(
                """
                INSERT INTO growth_state_transition
                (transition_id, entity_type, entity_id, from_status, to_status, actor, created_at)
                VALUES (?, 'STRATEGY_RECOMMENDATION', ?, ?, ?, ?, ?)
                """,
                (new_id("transition"), strategy_recommendation_id, current["status"], target, actor, now),
            )
        return self.get_recommendation(strategy_recommendation_id)

    def execute_low_risk(
        self, strategy_recommendation_id: str, *, decision_id: str, actor: str,
        idempotency_key: str = "",
    ) -> Dict[str, Any]:
        digest = payload_hash({
            "strategy_recommendation_id": strategy_recommendation_id,
            "decision_id": decision_id,
        })
        replay = self._idempotent_response("strategy.execute", idempotency_key, digest)
        if replay is not None:
            return replay
        recommendation = self.get_recommendation(strategy_recommendation_id)
        if recommendation["status"] not in {"APPROVED", "EXECUTED"}:
            raise GrowthStateConflict("strategy_recommendation_not_approved")
        if recommendation["action_type"] not in LOW_RISK_AUTOMATIC_ACTIONS:
            raise GrowthStateConflict("automatic_action_not_low_risk")
        decision = self.conn.execute(
            "SELECT context_snapshot_id FROM growth_decision WHERE decision_id=?", (decision_id,),
        ).fetchone()
        if not decision:
            raise GrowthNotFound("decision_not_found")
        if decision["context_snapshot_id"] != recommendation["context_snapshot_id"]:
            raise GrowthStateConflict("strategy_decision_context_mismatch")
        tasks = ExecutionTaskService(self.conn)
        action = tasks.create_operation_action(
            decision_id=decision_id, action_type=recommendation["action_type"],
            target_type="CONTEXT", target_id=recommendation["context_snapshot_id"],
            payload={"strategy_recommendation_id": strategy_recommendation_id}, created_by=actor,
            idempotency_key=f"strategy-execute:{idempotency_key}" if idempotency_key else "",
        )
        if action["status"] == "CREATED":
            action = tasks.complete_local_action(action["operation_action_id"], actor=actor)
        if recommendation["status"] == "APPROVED":
            self.transition_recommendation(strategy_recommendation_id, "EXECUTED", actor=actor)
        self._store_idempotent_response("strategy.execute", idempotency_key, digest, action)
        return action

    def get_recommendation(self, strategy_recommendation_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM growth_strategy_recommendation WHERE strategy_recommendation_id=?",
            (strategy_recommendation_id,),
        ).fetchone()
        if not row:
            raise GrowthNotFound("strategy_recommendation_not_found")
        result = dict(row)
        result["rationale_json"] = decode_json(result["rationale_json"], {})
        result["source_knowledge_ids_json"] = decode_json(result["source_knowledge_ids_json"], [])
        return result

    def get_simulation(self, simulation_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM growth_simulation WHERE simulation_id=?", (simulation_id,),
        ).fetchone()
        if not row:
            raise GrowthNotFound("simulation_not_found")
        result = dict(row)
        result["evidence_episode_ids_json"] = decode_json(result["evidence_episode_ids_json"], [])
        result["assumptions_json"] = decode_json(result["assumptions_json"], {})
        return result

    def _idempotent_response(
        self, route_key: str, idempotency_key: str, digest: str,
    ) -> Optional[Dict[str, Any]]:
        if not idempotency_key:
            return None
        row = self.conn.execute(
            """
            SELECT request_hash, response_json FROM growth_idempotency_record
            WHERE route_key=? AND idempotency_key=?
            """,
            (route_key, idempotency_key),
        ).fetchone()
        if not row:
            return None
        if row["request_hash"] != digest:
            raise GrowthStateConflict("idempotency_key_payload_conflict")
        return decode_json(row["response_json"], {})

    def _store_idempotent_response(
        self, route_key: str, idempotency_key: str, digest: str, result: Dict[str, Any],
    ) -> None:
        if not idempotency_key:
            return
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO growth_idempotency_record
                (route_key, idempotency_key, request_hash, response_status, response_json, created_at)
                VALUES (?, ?, ?, 201, ?, ?)
                """,
                (route_key, idempotency_key, digest, canonical_json(result), utc_now()),
            )
