from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from app.growth.common import canonical_json, decode_json, new_id, payload_hash, utc_now
from app.growth.context_service import ContextService
from app.growth.errors import GrowthLegacyReadOnly, GrowthNotFound, GrowthStateConflict, GrowthValidationError
from app.growth.schema import ensure_growth_schema
from app.growth.similar_episode_service import SimilarEpisodeService


ALLOWED_ACTIONS = {
    "CREATE_EXPERIMENT", "PAUSE", "SCALE_UP", "REDUCE_BUDGET", "OBSERVE", "CHECK_DATA",
    "CREATE_PAUSED_AD", "REPLACE_CREATIVE", "INCREASE_BUDGET", "DECREASE_BUDGET",
    "PAUSE_AD", "PAUSE_ADSET", "REACTIVATE_AD",
}


class DecisionService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_growth_schema(conn)
        self.contexts = ContextService(conn)

    def create_decision(
        self,
        *,
        recommendation_id: str,
        selected_action: str,
        decision_reason: Dict[str, Any],
        confidence: float,
        idempotency_key: str,
        rejected_actions: Optional[List[str]] = None,
        decided_by: str = "",
        context_snapshot_id: str = "",
    ) -> Dict[str, Any]:
        action = str(selected_action or "").strip().upper()
        if action not in ALLOWED_ACTIONS:
            raise GrowthValidationError("INVALID_DECISION_ACTION")
        if not isinstance(decision_reason, dict) or not str(decision_reason.get("type") or "").strip():
            raise GrowthValidationError("decision_reason_type_is_required")
        if not 0 <= float(confidence) <= 1:
            raise GrowthValidationError("confidence_out_of_range")
        if not str(idempotency_key or "").strip():
            raise GrowthValidationError("idempotency_key_is_required")
        request_payload = {
            "recommendation_id": recommendation_id,
            "selected_action": action,
            "rejected_actions": list(rejected_actions or []),
            "decision_reason": decision_reason,
            "confidence": float(confidence),
            "context_snapshot_id": context_snapshot_id,
        }
        request_digest = payload_hash(request_payload)
        replay = self._idempotent_response(idempotency_key, request_digest)
        if replay:
            return replay
        recommendation = self._get_recommendation(recommendation_id)
        if recommendation.get("data_origin") == "LEGACY":
            raise GrowthLegacyReadOnly("LEGACY_DATA_READ_ONLY")
        context_id = context_snapshot_id or self._snapshot_recommendation_context(recommendation)["context_snapshot_id"]
        now = utc_now()
        decision_id = new_id("decision")
        episode_id = new_id("episode")
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO growth_decision
                    (decision_id, recommendation_id, context_snapshot_id, selected_action,
                     rejected_actions_json, decision_reason_json, confidence, status,
                     idempotency_key, request_hash, decided_by, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'CREATED', ?, ?, ?, ?, ?)
                    """,
                    (
                        decision_id, recommendation_id, context_id, action,
                        canonical_json(list(rejected_actions or [])), canonical_json(decision_reason),
                        float(confidence), idempotency_key, request_digest, decided_by, now, now,
                    ),
                )
                self.conn.execute(
                    """
                    INSERT INTO growth_decision_episode
                    (episode_id, decision_id, context_snapshot_id, observation_json,
                     hypothesis_json, action_json, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'CREATED', ?, ?)
                    """,
                    (
                        episode_id, decision_id, context_id,
                        canonical_json(self._observation(recommendation)),
                        canonical_json(decision_reason),
                        canonical_json({"selected_action": action}), now, now,
                    ),
                )
                response = {"decision_id": decision_id, "status": "CREATED", "episode_id": episode_id}
                self.conn.execute(
                    """
                    INSERT INTO growth_idempotency_record
                    (route_key, idempotency_key, request_hash, response_status, response_json, created_at)
                    VALUES ('decision.create', ?, ?, 201, ?, ?)
                    """,
                    (idempotency_key, request_digest, canonical_json(response), now),
                )
        except sqlite3.IntegrityError as exc:
            replay = self._idempotent_response(idempotency_key, request_digest)
            if replay:
                return replay
            raise GrowthStateConflict("recommendation_already_decided") from exc
        return response

    def bind_target(
        self, decision_id: str, *, target_type: str, target_id: str, actor: str = "",
    ) -> Dict[str, Any]:
        row = self.conn.execute("SELECT * FROM growth_decision WHERE decision_id=?", (decision_id,)).fetchone()
        if not row:
            raise GrowthNotFound("decision_not_found")
        if row["status"] != "CREATED":
            raise GrowthStateConflict("decision_not_bindable")
        now = utc_now()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE growth_decision SET status='BOUND', target_type=?, target_id=?, updated_at=?
                WHERE decision_id=? AND status='CREATED'
                """,
                (target_type, target_id, now, decision_id),
            )
            if cursor.rowcount != 1:
                raise GrowthStateConflict("decision_changed_concurrently")
            if target_type == "EXPERIMENT":
                self.conn.execute(
                    "UPDATE growth_decision_episode SET experiment_id=?, updated_at=? WHERE decision_id=?",
                    (target_id, now, decision_id),
                )
                self.contexts.bind_experiment(target_id, row["context_snapshot_id"])
            self.conn.execute(
                """
                INSERT INTO growth_state_transition
                (transition_id, entity_type, entity_id, from_status, to_status, actor, created_at)
                VALUES (?, 'DECISION', ?, 'CREATED', 'BOUND', ?, ?)
                """,
                (new_id("transition"), decision_id, actor, now),
            )
        return {"decision_id": decision_id, "status": "BOUND", "target_type": target_type, "target_id": target_id}

    def preview_recommendation(self, recommendation_id: str) -> Dict[str, Any]:
        recommendation = self._get_recommendation(recommendation_id)
        if recommendation.get("data_origin") == "LEGACY":
            raise GrowthLegacyReadOnly("LEGACY_DATA_READ_ONLY")
        context = self._recommendation_context(recommendation)
        similar = SimilarEpisodeService(self.conn).find_similar_context(context, limit=5)
        existing = self.conn.execute(
            """
            SELECT d.decision_id, d.selected_action, d.status, d.target_type, d.target_id,
                   d.created_at, d.updated_at, e.episode_id, e.status AS episode_status
            FROM growth_decision d
            LEFT JOIN growth_decision_episode e ON e.decision_id=d.decision_id
            WHERE d.recommendation_id=?
            ORDER BY d.created_at DESC, e.created_at DESC
            LIMIT 1
            """,
            (recommendation_id,),
        ).fetchone()
        return {
            "recommendation_id": recommendation_id,
            "context_preview": context,
            "observation": self._observation(recommendation),
            "similar_episodes": similar,
            "existing_decision": dict(existing) if existing else None,
        }

    def _idempotent_response(self, key: str, digest: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT request_hash, response_json FROM growth_idempotency_record WHERE route_key='decision.create' AND idempotency_key=?",
            (key,),
        ).fetchone()
        if not row:
            return None
        if row["request_hash"] != digest:
            raise GrowthStateConflict("idempotency_key_payload_conflict")
        return decode_json(row["response_json"], {})

    def _get_recommendation(self, recommendation_id: str) -> Dict[str, Any]:
        try:
            columns = {
                row[1] for row in self.conn.execute("PRAGMA table_info(ad_recommendation)").fetchall()
            }
            optional_columns = []
            if "decision_context_json" in columns:
                optional_columns.append("decision_context_json")
            if "data_origin" in columns:
                optional_columns.append("data_origin")
            select_columns = ", ".join(["payload_json", *optional_columns])
            row = self.conn.execute(
                f"SELECT {select_columns} FROM ad_recommendation WHERE recommendation_id=?",
                (recommendation_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if not row:
            raise GrowthNotFound("recommendation_not_found")
        result = decode_json(row["payload_json"], {})
        if "decision_context_json" in row.keys():
            result["decision_context"] = decode_json(row["decision_context_json"], result.get("decision_context") or {})
        if "data_origin" in row.keys():
            result["data_origin"] = str(row["data_origin"] or "LEGACY")
        else:
            result["data_origin"] = "LEGACY"
        return result

    def _snapshot_recommendation_context(self, recommendation: Dict[str, Any]) -> Dict[str, Any]:
        return self.contexts.create_snapshot(self._recommendation_context(recommendation))

    @staticmethod
    def _recommendation_context(recommendation: Dict[str, Any]) -> Dict[str, Any]:
        evidence = dict(recommendation.get("evidence") or {})
        funnel = dict(evidence.get("funnel_metrics") or {})
        decision_context = dict(recommendation.get("decision_context") or {})
        context = {
            "app_id": funnel.get("target_app") or recommendation.get("project") or "unknown",
            "country": recommendation.get("country") or "",
            "platform": decision_context.get("platform") or "meta",
            "funnel_stage": recommendation.get("primary_layer") or "",
            "business_goal": decision_context.get("business_goal") or "acquisition",
            "creative_type": decision_context.get("creative_type") or "",
            "creative_angle": decision_context.get("creative_angle") or "",
            "placement": decision_context.get("placement") or "",
            "budget": decision_context.get("budget"),
            "target_cpa": evidence.get("country_cap"),
            "bid_strategy": decision_context.get("bid_strategy") or "",
            "market_context_json": {
                "object_id": recommendation.get("object_id"),
                "object_level": recommendation.get("object_level"),
                "data_window": evidence.get("data_window") or {},
                "metrics": funnel,
            },
        }
        return context

    @staticmethod
    def _observation(recommendation: Dict[str, Any]) -> Dict[str, Any]:
        evidence = dict(recommendation.get("evidence") or {})
        return {
            "diagnosis_type": recommendation.get("diagnosis_type"),
            "reason": recommendation.get("reason_zh"),
            "evidence_points": evidence.get("evidence_points") or [],
            "metrics": evidence.get("funnel_metrics") or {},
            "risk": (recommendation.get("action_gate") or {}).get("blocked_reasons") or [],
        }
