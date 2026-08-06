from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any, Dict, List, Tuple

from app.growth.common import canonical_json, decode_json, new_id, payload_hash, utc_now
from app.growth.errors import GrowthStateConflict
from app.growth.knowledge_service import KnowledgeService
from app.growth.schema import ensure_growth_schema


PATTERN_CONTEXT_FIELDS = (
    "app_id", "country", "platform", "funnel_stage", "business_goal",
    "creative_type", "creative_angle", "placement", "bid_strategy",
)


def outcome_succeeded(outcome: Dict[str, Any]) -> bool:
    if isinstance(outcome.get("success"), bool):
        return bool(outcome["success"])
    status = str(outcome.get("status") or outcome.get("result") or "").strip().upper()
    if status in {"SUCCESS", "WON", "POSITIVE", "TARGET_HIT"}:
        return True
    if status in {"FAILED", "LOST", "NEGATIVE", "TARGET_MISSED"}:
        return False
    if outcome.get("target_hit") is not None:
        return bool(outcome.get("target_hit"))
    positive = float(outcome.get("ctr_delta") or outcome.get("conversion_delta") or 0)
    negative_cost = float(outcome.get("cpa_delta") or 0)
    return positive > 0 or negative_cost < 0


class PatternMiningService:
    """Create reviewable candidates from repeated completed Episodes.

    Mining never activates knowledge. A human still performs RAW -> REVIEWED ->
    ACTIVE through KnowledgeService.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_growth_schema(conn)

    def mine(
        self, *, minimum_support: int = 2, idempotency_key: str = "",
    ) -> List[Dict[str, Any]]:
        support_floor = max(2, int(minimum_support or 2))
        digest = payload_hash({"minimum_support": support_floor})
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute(
                """
                SELECT request_hash, response_json FROM growth_idempotency_record
                WHERE route_key='pattern.mine' AND idempotency_key=?
                """,
                (idempotency_key,),
            ).fetchone() if idempotency_key else None
            if existing:
                if existing["request_hash"] != digest:
                    raise GrowthStateConflict("idempotency_key_payload_conflict")
                self.conn.commit()
                return decode_json(existing["response_json"], [])
            rows = self.conn.execute(
                """
                SELECT e.episode_id, e.outcome_json, e.completed_at,
                       d.selected_action, c.*
                FROM growth_decision_episode e
                JOIN growth_decision d ON d.decision_id=e.decision_id
                JOIN growth_context_snapshot c ON c.context_snapshot_id=e.context_snapshot_id
                WHERE e.status='COMPLETED' AND e.data_origin='NATIVE_V2'
                ORDER BY e.completed_at, e.episode_id
                """,
            ).fetchall()
            groups: Dict[Tuple[Any, ...], List[sqlite3.Row]] = defaultdict(list)
            for row in rows:
                key = tuple(row[field] for field in PATTERN_CONTEXT_FIELDS) + (row["selected_action"],)
                groups[key].append(row)

            created: List[Dict[str, Any]] = []
            knowledge = KnowledgeService(self.conn)
            for key, episodes in groups.items():
                if len(episodes) < support_floor:
                    continue
                successes = sum(outcome_succeeded(decode_json(row["outcome_json"], {})) for row in episodes)
                success_rate = successes / len(episodes)
                pattern_type = (
                    "SUCCESS_PATTERN" if success_rate >= 0.6
                    else "FAILURE_PATTERN" if success_rate <= 0.4
                    else "WARNING_PATTERN"
                )
                context = dict(zip(PATTERN_CONTEXT_FIELDS, key[:-1]))
                pattern = {
                    "version": "growth-pattern-v1",
                    "context": context,
                    "action_type": key[-1],
                    "support_count": len(episodes),
                    "success_count": successes,
                    "success_rate": round(success_rate, 4),
                    "evidence_episode_ids": [row["episode_id"] for row in episodes],
                }
                encoded = canonical_json(pattern)
                duplicate = self.conn.execute(
                    """
                    SELECT knowledge_id FROM growth_strategy_knowledge
                    WHERE pattern_type=? AND pattern_json=?
                    LIMIT 1
                    """,
                    (pattern_type, encoded),
                ).fetchone()
                if duplicate:
                    continue
                now = utc_now()
                knowledge_id = new_id("knowledge")
                self.conn.execute(
                    """
                    INSERT INTO growth_strategy_knowledge
                    (knowledge_id, episode_id, pattern_type, pattern_json, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'RAW', ?, ?)
                    """,
                    (knowledge_id, episodes[-1]["episode_id"], pattern_type, encoded, now, now),
                )
                created.append(knowledge.get_knowledge(knowledge_id))
            if idempotency_key:
                self.conn.execute(
                    """
                    INSERT INTO growth_idempotency_record
                    (route_key, idempotency_key, request_hash, response_status, response_json, created_at)
                    VALUES ('pattern.mine', ?, ?, 201, ?, ?)
                    """,
                    (idempotency_key, digest, canonical_json(created), utc_now()),
                )
            self.conn.commit()
            return created
        except Exception:
            self.conn.rollback()
            raise
