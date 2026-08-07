from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

from app.growth.common import canonical_json, decode_json, new_id, utc_now
from app.growth.errors import GrowthLegacyReadOnly, GrowthNotFound, GrowthStateConflict, GrowthValidationError
from app.growth.schema import ensure_growth_schema


EPISODE_TRANSITIONS = {
    "CREATED": {"ACTION_EXECUTING"},
    "ACTION_EXECUTING": {"WAITING_OUTCOME"},
    "WAITING_OUTCOME": {"OUTCOME_READY"},
    "OUTCOME_READY": {"LESSON_REVIEW"},
    "LESSON_REVIEW": {"COMPLETED"},
    "COMPLETED": set(),
}


class EpisodeService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_growth_schema(conn)

    def get_episode(self, episode_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM growth_decision_episode WHERE episode_id=?",
            (episode_id,),
        ).fetchone()
        if not row:
            raise GrowthNotFound("episode_not_found")
        result = dict(row)
        for field in ("observation_json", "hypothesis_json", "action_json", "outcome_json", "lesson_json"):
            result[field] = decode_json(result[field], {})
        return result

    def transition(
        self,
        episode_id: str,
        to_status: str,
        *,
        outcome: Optional[Dict[str, Any]] = None,
        lesson: Optional[Dict[str, Any]] = None,
        action: Optional[Dict[str, Any]] = None,
        actor: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        episode = self.get_episode(episode_id)
        if episode.get("data_origin") == "LEGACY":
            raise GrowthLegacyReadOnly("LEGACY_DATA_READ_ONLY")
        current = episode["status"]
        target = str(to_status or "").strip().upper()
        if target not in EPISODE_TRANSITIONS.get(current, set()):
            raise GrowthStateConflict(f"illegal_episode_transition:{current}:{target}")
        next_outcome = outcome if outcome is not None else episode["outcome_json"]
        next_lesson = lesson if lesson is not None else episode["lesson_json"]
        next_action = action if action is not None else episode["action_json"]
        if target == "OUTCOME_READY" and not next_outcome:
            raise GrowthValidationError("outcome_is_required")
        if target in {"LESSON_REVIEW", "COMPLETED"} and not next_lesson:
            raise GrowthValidationError("lesson_is_required")
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                UPDATE growth_decision_episode
                SET status=?, outcome_json=?, lesson_json=?, action_json=?, updated_at=?, completed_at=?
                WHERE episode_id=? AND status=?
                """,
                (
                    target, canonical_json(next_outcome), canonical_json(next_lesson), canonical_json(next_action),
                    now, now if target == "COMPLETED" else "", episode_id, current,
                ),
            )
            if self.conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise GrowthStateConflict("episode_changed_concurrently")
            self.conn.execute(
                """
                INSERT INTO growth_state_transition
                (transition_id, entity_type, entity_id, from_status, to_status, reason, actor, created_at)
                VALUES (?, 'EPISODE', ?, ?, ?, ?, ?, ?)
                """,
                (new_id("transition"), episode_id, current, target, reason, actor, now),
            )
        return self.get_episode(episode_id)
