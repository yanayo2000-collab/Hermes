from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from app.growth.common import decode_json
from app.growth.context_service import ContextService
from app.growth.episode_service import EPISODE_TRANSITIONS, EpisodeService
from app.growth.errors import GrowthNotFound, GrowthValidationError
from app.growth.similar_episode_service import SimilarEpisodeService
from app.growth.schema import ensure_growth_schema


KNOWLEDGE_STATUSES = {"RAW", "REVIEWED", "ACTIVE", "ARCHIVED"}


class GrowthReadService:
    """Read models for the Growth views. No business state is mutated here."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_growth_schema(conn)

    def list_episodes(self, *, status: str = "", limit: int = 50) -> Dict[str, Any]:
        normalized_status = str(status or "").strip().upper()
        if normalized_status and normalized_status not in EPISODE_TRANSITIONS:
            raise GrowthValidationError("invalid_episode_status")
        params: List[Any] = []
        where = ""
        if normalized_status:
            where = "WHERE e.status=?"
            params.append(normalized_status)
        params.append(max(1, min(int(limit or 50), 200)))
        rows = self.conn.execute(
            f"""
            SELECT e.*, d.recommendation_id, d.selected_action, d.decision_reason_json,
                   d.confidence, d.status AS decision_status, d.target_type, d.target_id,
                   c.app_id, c.country, c.platform, c.creative_type, c.creative_angle
            FROM growth_decision_episode e
            JOIN growth_decision d ON d.decision_id=e.decision_id
            JOIN growth_context_snapshot c ON c.context_snapshot_id=e.context_snapshot_id
            {where}
            ORDER BY e.updated_at DESC, e.episode_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        items = [self._episode_summary(row) for row in rows]
        return {"items": items, "count": len(items), "status": normalized_status}

    def get_episode_detail(self, episode_id: str) -> Dict[str, Any]:
        episode = EpisodeService(self.conn).get_episode(episode_id)
        decision = self._decision(episode["decision_id"])
        context = ContextService(self.conn).get_snapshot(episode["context_snapshot_id"])
        actions = self._actions(decision["decision_id"], episode_id)
        knowledge = self._knowledge_for_episode(episode_id)
        for item in knowledge:
            item["read_only"] = episode.get("data_origin") == "LEGACY" or item["status"] == "ARCHIVED"
        task_groups = [self._tasks_for_action(action["operation_action_id"]) for action in actions]
        tasks = [task for group in task_groups for task in group]
        similar = SimilarEpisodeService(self.conn).find_similar(
            episode["context_snapshot_id"], limit=5,
        )
        transitions = self._transitions({
            "DECISION": decision["decision_id"],
            "EPISODE": episode_id,
            "OPERATION_ACTION": [item["operation_action_id"] for item in actions],
            "EXECUTION_TASK": [item["execution_task_id"] for item in tasks],
            "KNOWLEDGE": [item["knowledge_id"] for item in knowledge],
        })
        read_only = episode.get("data_origin") == "LEGACY" or episode["status"] == "COMPLETED"
        return {
            "episode": episode,
            "decision": decision,
            "context": context,
            "actions": actions,
            "execution_tasks": tasks,
            "outcome": episode["outcome_json"],
            "lesson": episode["lesson_json"],
            "knowledge": knowledge,
            "similar_episodes": similar,
            "transitions": transitions,
            "lineage": self._lineage(context, decision, episode, actions, knowledge),
            "read_only": read_only,
            "read_only_reason": (
                "LEGACY_DATA_READ_ONLY" if episode.get("data_origin") == "LEGACY"
                else ("COMPLETED_EPISODE_READ_ONLY" if episode["status"] == "COMPLETED" else "")
            ),
            "allowed_next_statuses": sorted(EPISODE_TRANSITIONS.get(episode["status"], set())),
        }

    def get_experiment_detail(self, experiment_id: str) -> Dict[str, Any]:
        normalized_id = str(experiment_id or "").strip()
        if not normalized_id:
            raise GrowthValidationError("experiment_id_is_required")
        episode_row = self.conn.execute(
            """
            SELECT e.episode_id
            FROM growth_decision_episode e
            JOIN growth_decision d ON d.decision_id=e.decision_id
            WHERE e.experiment_id=? OR (d.target_type='EXPERIMENT' AND d.target_id=?)
            ORDER BY e.updated_at DESC LIMIT 1
            """,
            (normalized_id, normalized_id),
        ).fetchone()
        context_rows = self.conn.execute(
            """
            SELECT c.*, x.relation_type
            FROM experiment_context_snapshots x
            JOIN growth_context_snapshot c ON c.context_snapshot_id=x.context_snapshot_id
            WHERE x.experiment_id=? ORDER BY x.created_at
            """,
            (normalized_id,),
        ).fetchall()
        experiment = self._optional_row("creative_experiment_suggestions", "experiment_id", normalized_id)
        if not episode_row and not context_rows and not experiment:
            raise GrowthNotFound("experiment_not_found")
        detail = self.get_episode_detail(episode_row["episode_id"]) if episode_row else None
        contexts = [self._decode_row(dict(row)) for row in context_rows]
        if detail and not contexts:
            contexts = [detail["context"]]
        return {
            "experiment_id": normalized_id,
            "experiment": experiment,
            "contexts": contexts,
            "episode_detail": detail,
            "lineage": detail["lineage"] if detail else self._lineage(
                contexts[0] if contexts else {}, {}, {}, [], [],
            ),
            "read_only": bool(detail and detail["read_only"]),
        }

    def list_knowledge(self, *, status: str = "", limit: int = 50) -> Dict[str, Any]:
        normalized_status = str(status or "").strip().upper()
        if normalized_status and normalized_status not in KNOWLEDGE_STATUSES:
            raise GrowthValidationError("invalid_knowledge_status")
        params: List[Any] = []
        where = ""
        if normalized_status:
            where = "WHERE k.status=?"
            params.append(normalized_status)
        params.append(max(1, min(int(limit or 50), 200)))
        rows = self.conn.execute(
            f"""
            SELECT k.*, e.status AS episode_status, e.data_origin AS episode_data_origin,
                   e.outcome_json, e.lesson_json,
                   e.context_snapshot_id, d.recommendation_id, d.selected_action,
                   c.app_id, c.country, c.creative_type, c.creative_angle
            FROM growth_strategy_knowledge k
            JOIN growth_decision_episode e ON e.episode_id=k.episode_id
            JOIN growth_decision d ON d.decision_id=e.decision_id
            JOIN growth_context_snapshot c ON c.context_snapshot_id=e.context_snapshot_id
            {where}
            ORDER BY k.updated_at DESC, k.knowledge_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        items = []
        for row in rows:
            item = self._decode_row(dict(row))
            item["read_only"] = item["status"] == "ARCHIVED" or item["episode_data_origin"] == "LEGACY"
            items.append(item)
        return {"items": items, "count": len(items), "status": normalized_status}

    def get_knowledge_detail(self, knowledge_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM growth_strategy_knowledge WHERE knowledge_id=?",
            (knowledge_id,),
        ).fetchone()
        if not row:
            raise GrowthNotFound("knowledge_not_found")
        knowledge = self._decode_row(dict(row))
        episode_detail = self.get_episode_detail(knowledge["episode_id"])
        return {
            "knowledge": knowledge,
            "episode_detail": episode_detail,
            "similar_episodes": episode_detail["similar_episodes"],
            "evidence": {
                "context": episode_detail["context"],
                "decision": episode_detail["decision"],
                "outcome": episode_detail["outcome"],
                "lesson": episode_detail["lesson"],
            },
            "read_only": (
                knowledge["status"] == "ARCHIVED"
                or episode_detail["read_only_reason"] == "LEGACY_DATA_READ_ONLY"
            ),
        }

    def _decision(self, decision_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM growth_decision WHERE decision_id=?", (decision_id,),
        ).fetchone()
        if not row:
            raise GrowthNotFound("decision_not_found")
        return self._decode_row(dict(row))

    def _actions(self, decision_id: str, episode_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM growth_operation_action
            WHERE decision_id=? OR episode_id=? ORDER BY created_at, operation_action_id
            """,
            (decision_id, episode_id),
        ).fetchall()
        actions = []
        for row in rows:
            action = self._decode_row(dict(row))
            approval = self.conn.execute(
                """
                SELECT * FROM growth_operation_approval
                WHERE operation_action_id=?
                ORDER BY created_at DESC, approval_id DESC
                LIMIT 1
                """,
                (action["operation_action_id"],),
            ).fetchone()
            action["approval"] = self._decode_row(dict(approval)) if approval else {}
            actions.append(action)
        return actions

    def _knowledge_for_episode(self, episode_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM growth_strategy_knowledge WHERE episode_id=? ORDER BY created_at",
            (episode_id,),
        ).fetchall()
        return [self._decode_row(dict(row)) for row in rows]

    def _tasks_for_action(self, operation_action_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM meta_execution_task WHERE operation_action_id=? ORDER BY created_at",
            (operation_action_id,),
        ).fetchall()
        tasks = []
        for row in rows:
            task = self._decode_row(dict(row))
            receipts = self.conn.execute(
                "SELECT * FROM meta_execution_task_receipt WHERE execution_task_id=? ORDER BY created_at",
                (task["execution_task_id"],),
            ).fetchall()
            task["receipts"] = [self._decode_row(dict(receipt)) for receipt in receipts]
            tasks.append(task)
        return tasks

    def _transitions(self, entities: Dict[str, Any]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for entity_type, raw_ids in entities.items():
            ids = raw_ids if isinstance(raw_ids, list) else [raw_ids]
            ids = [str(item) for item in ids if item]
            if not ids:
                continue
            placeholders = ",".join("?" for _ in ids)
            rows = self.conn.execute(
                f"""
                SELECT * FROM growth_state_transition
                WHERE entity_type=? AND entity_id IN ({placeholders})
                ORDER BY created_at, transition_id
                """,
                [entity_type, *ids],
            ).fetchall()
            items.extend(dict(row) for row in rows)
        items.sort(key=lambda item: (item["created_at"], item["transition_id"]))
        return items

    def _optional_row(self, table: str, key: str, value: str) -> Dict[str, Any]:
        exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
        ).fetchone()
        if not exists:
            return {}
        row = self.conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
        return self._decode_row(dict(row)) if row else {}

    @staticmethod
    def _decode_row(item: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in list(item.items()):
            if key.endswith("_json"):
                default = [] if str(value or "").lstrip().startswith("[") else {}
                item[key] = decode_json(value, default)
        return item

    @classmethod
    def _episode_summary(cls, row: sqlite3.Row) -> Dict[str, Any]:
        item = cls._decode_row(dict(row))
        item["read_only"] = item.get("data_origin") == "LEGACY" or item["status"] == "COMPLETED"
        item["allowed_next_statuses"] = sorted(EPISODE_TRANSITIONS.get(item["status"], set()))
        return item

    @staticmethod
    def _lineage(
        context: Dict[str, Any], decision: Dict[str, Any], episode: Dict[str, Any],
        actions: List[Dict[str, Any]], knowledge: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        outcome = episode.get("outcome_json") or {}
        action_status = actions[-1]["status"] if actions else (
            "RECORDED" if episode.get("action_json") else "PENDING"
        )
        knowledge_status = knowledge[-1]["status"] if knowledge else "PENDING"
        return [
            {"stage": "CONTEXT", "status": "CAPTURED" if context else "PENDING", "entity_id": context.get("context_snapshot_id", "")},
            {"stage": "DECISION", "status": decision.get("status", "PENDING"), "entity_id": decision.get("decision_id", "")},
            {"stage": "ACTION", "status": action_status, "entity_id": actions[-1]["operation_action_id"] if actions else ""},
            {"stage": "OUTCOME", "status": "CAPTURED" if outcome else "PENDING", "entity_id": episode.get("episode_id", "")},
            {"stage": "KNOWLEDGE", "status": knowledge_status, "entity_id": knowledge[-1]["knowledge_id"] if knowledge else ""},
        ]
