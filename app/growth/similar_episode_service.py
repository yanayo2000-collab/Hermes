from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from app.growth.common import decode_json
from app.growth.context_service import ContextService
from app.growth.schema import ensure_growth_schema


SIMILARITY_FIELDS = (
    "app_id", "country", "platform", "funnel_stage", "business_goal",
    "creative_type", "creative_angle", "placement", "bid_strategy",
)


class SimilarEpisodeService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_growth_schema(conn)

    def find_similar(self, context_snapshot_id: str, *, limit: int = 5) -> List[Dict[str, Any]]:
        target = ContextService(self.conn).get_snapshot(context_snapshot_id)
        return self.find_similar_context(
            target, limit=limit, exclude_context_snapshot_id=context_snapshot_id,
        )

    def find_similar_context(
        self, target: Dict[str, Any], *, limit: int = 5,
        exclude_context_snapshot_id: str = "",
    ) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT e.*, c.*
            FROM growth_decision_episode e
            JOIN growth_context_snapshot c ON c.context_snapshot_id=e.context_snapshot_id
            WHERE e.status='COMPLETED' AND (?='' OR e.context_snapshot_id<>?)
            ORDER BY e.completed_at DESC
            LIMIT 200
            """,
            (exclude_context_snapshot_id, exclude_context_snapshot_id),
        ).fetchall()
        ranked: List[Dict[str, Any]] = []
        for row in rows:
            candidate = dict(row)
            matched = [field for field in SIMILARITY_FIELDS if target.get(field) and target.get(field) == candidate.get(field)]
            score = len(matched) / len(SIMILARITY_FIELDS)
            target_market = target.get("market_context_json") or {}
            candidate_market = decode_json(candidate.get("market_context_json"), {})
            if target_market.get("object_level") and target_market.get("object_level") == candidate_market.get("object_level"):
                score += 0.1
                matched.append("object_level")
            ranked.append({
                "episode_id": candidate["episode_id"],
                "decision_id": candidate["decision_id"],
                "score": round(min(score, 1.0), 4),
                "matched_fields": matched,
                "outcome": decode_json(candidate["outcome_json"], {}),
                "lesson": decode_json(candidate["lesson_json"], {}),
                "completed_at": candidate["completed_at"],
            })
        ranked.sort(key=lambda item: (item["score"], item["completed_at"]), reverse=True)
        return ranked[: max(1, min(int(limit or 5), 20))]
