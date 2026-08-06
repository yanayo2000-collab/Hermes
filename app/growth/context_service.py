from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

from app.growth.common import canonical_json, decode_json, new_id, payload_hash, row_dict, utc_now
from app.growth.errors import GrowthNotFound, GrowthValidationError
from app.growth.schema import ensure_growth_schema


CONTEXT_FIELDS = (
    "app_id", "country", "platform", "device", "funnel_stage", "business_goal",
    "creative_type", "creative_angle", "copy_style", "cta", "placement",
    "budget", "target_cpa", "bid_strategy",
)


class ContextService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        ensure_growth_schema(conn)

    def create_snapshot(
        self,
        context: Dict[str, Any],
        *,
        snapshot_kind: str = "INITIAL",
        parent_snapshot_id: Optional[str] = None,
        data_origin: str = "NATIVE_V2",
    ) -> Dict[str, Any]:
        payload = dict(context or {})
        if not str(payload.get("app_id") or "").strip():
            raise GrowthValidationError("app_id_is_required")
        normalized = {name: payload.get(name) for name in CONTEXT_FIELDS}
        normalized["audience_json"] = dict(payload.get("audience_json") or {})
        normalized["market_context_json"] = dict(payload.get("market_context_json") or {})
        normalized["snapshot_kind"] = snapshot_kind
        normalized["parent_snapshot_id"] = parent_snapshot_id or ""
        normalized["data_origin"] = data_origin
        digest = payload_hash(normalized)
        existing = self.conn.execute(
            "SELECT * FROM growth_context_snapshot WHERE snapshot_hash=?",
            (digest,),
        ).fetchone()
        if existing:
            return self._serialize(existing)
        if parent_snapshot_id:
            self.get_snapshot(parent_snapshot_id)
        snapshot_id = new_id("ctx")
        created_at = utc_now()
        self.conn.execute(
            """
            INSERT INTO growth_context_snapshot
            (context_snapshot_id, app_id, country, platform, device, audience_json,
             funnel_stage, business_goal, creative_type, creative_angle, copy_style,
             cta, placement, budget, target_cpa, bid_strategy, market_context_json,
             snapshot_kind, parent_snapshot_id, snapshot_hash, data_origin, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id, str(payload.get("app_id") or "").strip(),
                str(payload.get("country") or "").strip(), str(payload.get("platform") or "").strip(),
                str(payload.get("device") or "").strip(), canonical_json(normalized["audience_json"]),
                str(payload.get("funnel_stage") or "").strip(), str(payload.get("business_goal") or "").strip(),
                str(payload.get("creative_type") or "").strip(), str(payload.get("creative_angle") or "").strip(),
                str(payload.get("copy_style") or "").strip(), str(payload.get("cta") or "").strip(),
                str(payload.get("placement") or "").strip(), payload.get("budget"), payload.get("target_cpa"),
                str(payload.get("bid_strategy") or "").strip(), canonical_json(normalized["market_context_json"]),
                snapshot_kind, parent_snapshot_id, digest, data_origin, created_at,
            ),
        )
        return self.get_snapshot(snapshot_id)

    def create_adjustment_snapshot(self, parent_snapshot_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        parent = self.get_snapshot(parent_snapshot_id)
        context = {name: parent.get(name) for name in CONTEXT_FIELDS}
        context["audience_json"] = parent["audience_json"]
        context["market_context_json"] = parent["market_context_json"]
        context.update(dict(changes or {}))
        return self.create_snapshot(
            context,
            snapshot_kind="ADJUSTMENT",
            parent_snapshot_id=parent_snapshot_id,
            data_origin=parent["data_origin"],
        )

    def get_snapshot(self, context_snapshot_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM growth_context_snapshot WHERE context_snapshot_id=?",
            (context_snapshot_id,),
        ).fetchone()
        if not row:
            raise GrowthNotFound("GROWTH_CONTEXT_NOT_FOUND")
        return self._serialize(row)

    def bind_experiment(self, experiment_id: str, context_snapshot_id: str, *, relation_type: str = "INITIAL") -> None:
        if not str(experiment_id or "").strip():
            raise GrowthValidationError("experiment_id_is_required")
        self.get_snapshot(context_snapshot_id)
        self.conn.execute(
            """
            INSERT OR IGNORE INTO experiment_context_snapshots
            (experiment_id, context_snapshot_id, relation_type, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (experiment_id, context_snapshot_id, relation_type, utc_now()),
        )

    @staticmethod
    def _serialize(row: sqlite3.Row) -> Dict[str, Any]:
        result = row_dict(row)
        result["audience_json"] = decode_json(result.get("audience_json"), {})
        result["market_context_json"] = decode_json(result.get("market_context_json"), {})
        return result
