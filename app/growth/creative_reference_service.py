from __future__ import annotations

import sqlite3
import unicodedata
from typing import Any, Dict, Iterable, Optional, Set

from app.growth.common import canonical_json, payload_hash, utc_now


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone())


def _columns(conn: sqlite3.Connection, name: str) -> Set[str]:
    if not _table_exists(conn, name):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({name})").fetchall()}


def _value(row: Optional[sqlite3.Row], *names: str) -> str:
    if row is None:
        return ""
    keys = set(row.keys())
    for name in names:
        if name in keys and row[name] not in (None, ""):
            return str(row[name])
    return ""


def _infer_direction_from_copy(asset: sqlite3.Row) -> Dict[str, Any]:
    """Infer a fixed direction from copy semantics, never from an ad name."""
    raw = " ".join(
        _value(asset, field)
        for field in ("body_text", "title_text", "description_text")
    ).lower()
    text = "".join(
        char for char in unicodedata.normalize("NFKD", raw)
        if not unicodedata.combining(char)
    )
    lexicon = {
        "points_reward": ("ponto", "pontos", "recompensa", "recompensas", "acumula", "reward", "points"),
        "easy_start": ("facil", "simples", "comecar", "flexivel", "easy", "simple", "start"),
        "guided_trust": ("consultor", "orientacao", "ajuda", "conversa", "whatsapp", "guide", "support"),
        "safe_compliance": ("seguro", "seguranca", "transparente", "privacy", "secure", "safe"),
    }
    scores = {
        key: sum(1 for token in tokens if token in text)
        for key, tokens in lexicon.items()
    }
    ranking = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if not ranking or ranking[0][1] < 2 or (len(ranking) > 1 and ranking[0][1] == ranking[1][1]):
        return {"key": "", "source": "unmapped", "authoritative": False, "confidence": 0.0}
    key, score = ranking[0]
    return {
        "key": key,
        "source": "inferred_copy_semantics_v1",
        "authoritative": False,
        "confidence": min(0.95, 0.7 + score * 0.05),
        "evidence": "body_title_description_semantic_match",
    }


def ensure_creative_reference_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_creative_reference_knowledge (
            reference_id TEXT PRIMARY KEY,
            ad_id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL,
            campaign_id TEXT NOT NULL DEFAULT '',
            adset_id TEXT NOT NULL DEFAULT '',
            creative_id TEXT NOT NULL DEFAULT '',
            direction_key TEXT NOT NULL DEFAULT '',
            direction_source TEXT NOT NULL DEFAULT 'unmapped',
            source_origin TEXT NOT NULL,
            access_status TEXT NOT NULL,
            original_prompt_available INTEGER NOT NULL DEFAULT 0,
            snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ad_creative_reference_access "
        "ON ad_creative_reference_knowledge(access_status, account_id, updated_at)"
    )


def _latest_adoption(conn: sqlite3.Connection, ad_id: str, experiment_id: str = "") -> Optional[sqlite3.Row]:
    columns = _columns(conn, "creative_adoption_records")
    if not columns:
        return None
    clauses = []
    params = []
    if experiment_id and "experiment_id" in columns:
        clauses.append("experiment_id=?")
        params.append(experiment_id)
    ad_columns = [name for name in ("adopted_ad_id", "ad_id") if name in columns]
    if ad_id and ad_columns:
        clauses.append("(" + " OR ".join(f"{name}=?" for name in ad_columns) + ")")
        params.extend([ad_id] * len(ad_columns))
    if not clauses:
        return None
    order = next((name for name in ("confirmed_at", "matched_at", "adopted_at") if name in columns), "rowid")
    return conn.execute(
        f"SELECT * FROM creative_adoption_records WHERE {' OR '.join(clauses)} ORDER BY {order} DESC LIMIT 1",
        params,
    ).fetchone()


def _generated_lineage(conn: sqlite3.Connection, adoption: Optional[sqlite3.Row]) -> Dict[str, Any]:
    if adoption is None or not _table_exists(conn, "creative_generated_images"):
        return {}
    image_id = _value(adoption, "generated_image_id", "image_id")
    if not image_id:
        return {}
    image = conn.execute(
        "SELECT * FROM creative_generated_images WHERE image_id=?", (image_id,),
    ).fetchone()
    if image is None:
        return {}
    task = None
    task_columns = _columns(conn, "creative_generation_tasks")
    if task_columns:
        image_task_id = _value(image, "task_id")
        request_id = _value(image, "request_id")
        if image_task_id and "task_id" in task_columns:
            task = conn.execute(
                "SELECT * FROM creative_generation_tasks WHERE task_id=? LIMIT 1", (image_task_id,),
            ).fetchone()
        if task is None and request_id and "generation_request_id" in task_columns:
            task = conn.execute(
                "SELECT * FROM creative_generation_tasks WHERE generation_request_id=? ORDER BY created_at DESC LIMIT 1",
                (request_id,),
            ).fetchone()
    return {
        "image_id": image_id,
        "image_hash": _value(image, "image_hash", "final_delivery_hash"),
        "generation_request_id": _value(adoption, "generation_request_id") or _value(image, "request_id"),
        "generation_task_id": _value(task, "task_id"),
        "prompt_version": _value(task, "currency_threshold_version"),
        "prompt_hash": _value(image, "prompt_hash"),
        "final_prompt": _value(task, "final_prompt", "prompt"),
        "prompt_package_json": _value(task, "prompt_package_json"),
        "binding_status": _value(adoption, "binding_status", "status"),
    }


def resolve_creative_reference(
    conn: sqlite3.Connection,
    *,
    ad_id: str,
    accessible_account_ids: Iterable[str],
    experiment_id: str = "",
) -> Dict[str, Any]:
    """Resolve exact lineage; active mining requires a live-access allowlist."""
    allowed = {str(item).removeprefix("act_").strip() for item in accessible_account_ids if str(item).strip()}
    if not _table_exists(conn, "ad_creative_asset"):
        return {"status": "NOT_FOUND", "reason": "creative_asset_table_missing", "ad_id": ad_id}
    asset = conn.execute(
        "SELECT * FROM ad_creative_asset WHERE ad_id=? ORDER BY updated_at DESC, last_seen_at DESC LIMIT 1",
        (str(ad_id).strip(),),
    ).fetchone()
    if asset is None:
        return {"status": "NOT_FOUND", "reason": "creative_asset_not_found", "ad_id": ad_id}
    account_id = _value(asset, "account_id").removeprefix("act_")
    if not account_id or account_id not in allowed:
        return {
            "status": "EXCLUDED_ACCESS_LOST",
            "reason": "account_not_in_live_access_allowlist",
            "ad_id": str(ad_id),
            "account_id": account_id,
        }

    direction_key = ""
    direction_source = "unmapped"
    if _table_exists(conn, "ad_creative_direction_mapping"):
        mapping = conn.execute(
            "SELECT * FROM ad_creative_direction_mapping WHERE ad_id=?", (str(ad_id),),
        ).fetchone()
        if mapping:
            direction_key = _value(mapping, "direction_key")
            direction_source = _value(mapping, "source") or "persisted_mapping"

    inferred_direction = _infer_direction_from_copy(asset) if not direction_key else {}
    if inferred_direction.get("key"):
        direction_key = str(inferred_direction["key"])
        direction_source = str(inferred_direction["source"])

    adoption = _latest_adoption(conn, str(ad_id), experiment_id)
    generated = _generated_lineage(conn, adoption)
    source_origin = "generated_and_adopted" if generated else "external_meta_reference"
    original_prompt_available = bool(generated.get("final_prompt"))
    snapshot = {
        "status": "ACTIVE_REFERENCE",
        "source_origin": source_origin,
        "actual_meta_ids": {
            "account_id": account_id,
            "campaign_id": _value(asset, "campaign_id"),
            "adset_id": _value(asset, "adset_id"),
            "ad_id": _value(asset, "ad_id"),
            "creative_id": _value(asset, "creative_id"),
        },
        "asset": {
            "asset_id": _value(asset, "asset_id"),
            "asset_type": _value(asset, "asset_type"),
            "image_hash": _value(asset, "image_hash"),
            "source_image_hash": _value(asset, "source_image_hash", "image_hash"),
            "local_media_ref": _value(asset, "source_image_local_ref", "local_media_ref"),
        },
        "copy": {
            "body": _value(asset, "body_text"),
            "title": _value(asset, "title_text"),
            "description": _value(asset, "description_text"),
            "cta": _value(asset, "cta_type"),
        },
        "direction": inferred_direction or {
            "key": direction_key,
            "source": direction_source,
            "authoritative": bool(direction_key),
            "confidence": 1.0 if direction_key else 0.0,
        },
        "prompt_lineage": generated,
        "original_prompt_available": original_prompt_available,
        "access_evidence": "caller_supplied_live_access_allowlist",
    }
    snapshot["reference_id"] = "cref_" + payload_hash(snapshot)[:24]
    return snapshot


def persist_creative_reference(conn: sqlite3.Connection, reference: Dict[str, Any]) -> Dict[str, Any]:
    if reference.get("status") != "ACTIVE_REFERENCE":
        return {"persisted": False, **reference}
    ensure_creative_reference_table(conn)
    ids = dict(reference.get("actual_meta_ids") or {})
    direction = dict(reference.get("direction") or {})
    now = utc_now()
    with conn:
        conn.execute(
            """INSERT INTO ad_creative_reference_knowledge
            (reference_id,ad_id,account_id,campaign_id,adset_id,creative_id,
             direction_key,direction_source,source_origin,access_status,
             original_prompt_available,snapshot_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,'ACCESSIBLE',?,?,?,?)
            ON CONFLICT(ad_id) DO UPDATE SET
                reference_id=excluded.reference_id, account_id=excluded.account_id,
                campaign_id=excluded.campaign_id, adset_id=excluded.adset_id,
                creative_id=excluded.creative_id, direction_key=excluded.direction_key,
                direction_source=excluded.direction_source, source_origin=excluded.source_origin,
                access_status='ACCESSIBLE', original_prompt_available=excluded.original_prompt_available,
                snapshot_json=excluded.snapshot_json, updated_at=excluded.updated_at""",
            (
                reference["reference_id"], ids.get("ad_id", ""), ids.get("account_id", ""),
                ids.get("campaign_id", ""), ids.get("adset_id", ""), ids.get("creative_id", ""),
                direction.get("key", ""), direction.get("source", "unmapped"), reference.get("source_origin", ""),
                1 if reference.get("original_prompt_available") else 0,
                canonical_json(reference), now, now,
            ),
        )
    return {"persisted": True, **reference}
