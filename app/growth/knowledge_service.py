from __future__ import annotations

import sqlite3
from typing import Any, Dict

from app.growth.common import canonical_json, decode_json, new_id, payload_hash, utc_now
from app.growth.errors import GrowthLegacyReadOnly, GrowthNotFound, GrowthStateConflict, GrowthValidationError
from app.growth.schema import ensure_growth_schema


KNOWLEDGE_TRANSITIONS = {
    "RAW": {"REVIEWED", "ARCHIVED"},
    "REVIEWED": {"ACTIVE", "ARCHIVED"},
    "ACTIVE": {"ARCHIVED"},
    "ARCHIVED": set(),
}


class KnowledgeService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_growth_schema(conn)

    def create_candidate(
        self, episode_id: str, pattern_type: str, pattern: Dict[str, Any], *, idempotency_key: str = "",
    ) -> Dict[str, Any]:
        episode = self.conn.execute(
            "SELECT status, data_origin FROM growth_decision_episode WHERE episode_id=?",
            (episode_id,),
        ).fetchone()
        if not episode:
            raise GrowthNotFound("episode_not_found")
        if episode["data_origin"] == "LEGACY":
            raise GrowthLegacyReadOnly("LEGACY_DATA_READ_ONLY")
        if episode["status"] != "COMPLETED":
            raise GrowthStateConflict("episode_not_completed")
        normalized_type = str(pattern_type or "").strip().upper()
        if normalized_type not in {"SUCCESS_PATTERN", "FAILURE_PATTERN", "WARNING_PATTERN"}:
            raise GrowthValidationError("invalid_pattern_type")
        digest = payload_hash({
            "episode_id": episode_id, "pattern_type": normalized_type, "pattern": pattern,
        })
        if idempotency_key:
            existing = self.conn.execute(
                """
                SELECT request_hash, response_json FROM growth_idempotency_record
                WHERE route_key='knowledge.create' AND idempotency_key=?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != digest:
                    raise GrowthStateConflict("idempotency_key_payload_conflict")
                return decode_json(existing["response_json"], {})
        now = utc_now()
        knowledge_id = new_id("knowledge")
        if idempotency_key:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                """
                INSERT INTO growth_strategy_knowledge
                (knowledge_id, episode_id, pattern_type, pattern_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'RAW', ?, ?)
                """,
                (knowledge_id, episode_id, normalized_type, canonical_json(pattern), now, now),
            )
            result = self.get_knowledge(knowledge_id)
            if idempotency_key:
                self.conn.execute(
                    """
                    INSERT INTO growth_idempotency_record
                    (route_key, idempotency_key, request_hash, response_status, response_json, created_at)
                    VALUES ('knowledge.create', ?, ?, 201, ?, ?)
                    """,
                    (idempotency_key, digest, canonical_json(result), now),
                )
            self.conn.commit()
            return result
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            if idempotency_key:
                existing = self.conn.execute(
                    """
                    SELECT request_hash, response_json FROM growth_idempotency_record
                    WHERE route_key='knowledge.create' AND idempotency_key=?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if existing and existing["request_hash"] == digest:
                    return decode_json(existing["response_json"], {})
            raise GrowthStateConflict("knowledge_constraint_conflict") from exc
        except Exception:
            self.conn.rollback()
            raise

    def transition(self, knowledge_id: str, to_status: str, *, reviewer: str = "") -> Dict[str, Any]:
        current = self.get_knowledge(knowledge_id)
        episode = self.conn.execute(
            "SELECT data_origin FROM growth_decision_episode WHERE episode_id=?",
            (current["episode_id"],),
        ).fetchone()
        if episode and episode["data_origin"] == "LEGACY":
            raise GrowthLegacyReadOnly("LEGACY_DATA_READ_ONLY")
        target = str(to_status or "").strip().upper()
        if target not in KNOWLEDGE_TRANSITIONS.get(current["status"], set()):
            raise GrowthStateConflict(f"illegal_knowledge_transition:{current['status']}:{target}")
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                UPDATE growth_strategy_knowledge
                SET status=?, reviewed_by=?, reviewed_at=?, activated_at=?, updated_at=?
                WHERE knowledge_id=? AND status=?
                """,
                (
                    target,
                    reviewer if target in {"REVIEWED", "ACTIVE"} else current["reviewed_by"],
                    now if target == "REVIEWED" else current["reviewed_at"],
                    now if target == "ACTIVE" else current["activated_at"],
                    now, knowledge_id, current["status"],
                ),
            )
            if self.conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise GrowthStateConflict("knowledge_changed_concurrently")
            self.conn.execute(
                """
                INSERT INTO growth_state_transition
                (transition_id, entity_type, entity_id, from_status, to_status, actor, created_at)
                VALUES (?, 'KNOWLEDGE', ?, ?, ?, ?, ?)
                """,
                (new_id("transition"), knowledge_id, current["status"], target, reviewer, now),
            )
        return self.get_knowledge(knowledge_id)

    def get_knowledge(self, knowledge_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM growth_strategy_knowledge WHERE knowledge_id=?",
            (knowledge_id,),
        ).fetchone()
        if not row:
            raise GrowthNotFound("knowledge_not_found")
        result = dict(row)
        result["pattern_json"] = decode_json(result["pattern_json"], {})
        return result
