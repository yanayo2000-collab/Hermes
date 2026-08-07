from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Sequence


RETENTION_DAYS = 7


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone())


def _placeholders(values: Sequence[str]) -> str:
    return ",".join("?" for _ in values)


def _rows_for_ids(
    conn: sqlite3.Connection, table: str, column: str, values: Sequence[str], columns: str = "*",
) -> List[sqlite3.Row]:
    if not values or not _table_exists(conn, table):
        return []
    return list(conn.execute(
        f"SELECT {columns} FROM {table} WHERE {column} IN ({_placeholders(values)})", values,
    ).fetchall())


def ensure_new_account_launch_retention_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ad_new_account_launch_archive (
            launch_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'ARCHIVED',
            archived_at TEXT NOT NULL DEFAULT '',
            archived_by TEXT NOT NULL DEFAULT '',
            restored_at TEXT NOT NULL DEFAULT '',
            restored_by TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ad_new_account_launch_purge_audit (
            purge_id INTEGER PRIMARY KEY AUTOINCREMENT,
            launch_fingerprint TEXT NOT NULL,
            purged_at TEXT NOT NULL,
            purged_by TEXT NOT NULL DEFAULT '',
            purge_reason TEXT NOT NULL DEFAULT '',
            deleted_counts_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS ad_new_account_launch_meta_delete_audit (
            delete_id TEXT PRIMARY KEY,
            launch_fingerprint TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('STARTED','SUCCESS','MANUAL_REVIEW')),
            requested_by TEXT NOT NULL DEFAULT '',
            object_ids_json TEXT NOT NULL DEFAULT '[]',
            results_json TEXT NOT NULL DEFAULT '[]',
            response_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_new_account_launch_archive_due
            ON ad_new_account_launch_archive(status, archived_at);
        """
    )


def launch_retention_status(
    conn: sqlite3.Connection,
    launch_id: str,
    *,
    retention_days: int = RETENTION_DAYS,
    now: datetime | None = None,
) -> Dict[str, Any]:
    ensure_new_account_launch_retention_tables(conn)
    archive = conn.execute(
        "SELECT * FROM ad_new_account_launch_archive WHERE launch_id=?", (launch_id,),
    ).fetchone()
    archived = bool(archive and str(archive["status"] or "").upper() == "ARCHIVED")
    archived_at = _parse_utc(str(archive["archived_at"] or "")) if archive else None
    purge_at = archived_at + timedelta(days=max(1, int(retention_days))) if archived_at else None

    experiment_rows = list(conn.execute(
        """SELECT experiment_id,source_recommendation_id,source_campaign_id,source_adset_id,
                  source_ad_id,source_creative_id
           FROM ad_experiment WHERE source_report_id=?""",
        (launch_id,),
    ).fetchall())
    experiment_ids = [str(row["experiment_id"]) for row in experiment_rows]
    recommendation_ids = [str(row["source_recommendation_id"] or "") for row in experiment_rows]
    recommendation_ids = [value for value in recommendation_ids if value]
    decision_rows = _rows_for_ids(
        conn, "growth_decision", "recommendation_id", recommendation_ids, "decision_id,target_id",
    )
    decision_ids = [str(row["decision_id"]) for row in decision_rows]

    operation_count = 0
    if _table_exists(conn, "growth_operation_action"):
        clauses = ["json_extract(payload_json,'$.launch_id')=?"]
        params: List[str] = [launch_id]
        if decision_ids:
            clauses.append(f"decision_id IN ({_placeholders(decision_ids)})")
            params.extend(decision_ids)
        operation_count = int(conn.execute(
            f"SELECT COUNT(*) FROM growth_operation_action WHERE {' OR '.join(clauses)}", params,
        ).fetchone()[0])

    external_ids = any(
        str(row[column] or "").strip()
        for row in experiment_rows
        for column in ("source_campaign_id", "source_adset_id", "source_ad_id", "source_creative_id")
    )
    adoption_count = 0
    if experiment_ids and _table_exists(conn, "creative_adoption_records"):
        adoption_count = int(conn.execute(
            f"SELECT COUNT(*) FROM creative_adoption_records WHERE experiment_id IN ({_placeholders(experiment_ids)})",
            experiment_ids,
        ).fetchone()[0])

    protected_audit = bool(operation_count or external_ids or adoption_count)
    blocked_reason = ""
    if not archived:
        blocked_reason = "launch_must_be_archived_first"
    elif not experiment_ids:
        blocked_reason = "launch_not_found"

    effective_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "archived": archived,
        "archived_at": str(archive["archived_at"] or "") if archive else "",
        "purge_after": purge_at.isoformat() if purge_at else "",
        "purge_due": bool(purge_at and purge_at <= effective_now),
        "retention_days": max(1, int(retention_days)),
        "can_permanently_delete": not blocked_reason,
        "permanent_delete_blocked_reason": blocked_reason,
        "permanent_delete_mode": "REMOVE_ORDER_KEEP_AUDIT" if protected_audit else "FULL_PURGE",
        "protected_audit_present": protected_audit,
    }


def _delete_for_ids(
    conn: sqlite3.Connection,
    counts: Dict[str, int],
    table: str,
    column: str,
    values: Sequence[str],
) -> None:
    if not values or not _table_exists(conn, table):
        return
    cursor = conn.execute(
        f"DELETE FROM {table} WHERE {column} IN ({_placeholders(values)})", values,
    )
    counts[table] = counts.get(table, 0) + max(0, int(cursor.rowcount))


def purge_new_account_launch(
    conn: sqlite3.Connection,
    launch_id: str,
    *,
    actor: str,
    reason: str,
    now: datetime | None = None,
) -> Dict[str, Any]:
    status = launch_retention_status(conn, launch_id, now=now)
    if not status["can_permanently_delete"]:
        raise ValueError(str(status["permanent_delete_blocked_reason"] or "launch_not_purgeable"))

    if status["protected_audit_present"]:
        counts = {"ad_new_account_launch_archive": 1}
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """UPDATE ad_new_account_launch_archive
                   SET status='PURGED',updated_at=?
                   WHERE launch_id=? AND status='ARCHIVED'""",
                ((now or datetime.now(timezone.utc)).isoformat(), launch_id),
            )
            if int(cursor.rowcount) != 1:
                raise ValueError("launch_not_purgeable")
            fingerprint = hashlib.sha256(launch_id.encode("utf-8")).hexdigest()[:20]
            conn.execute(
                """INSERT INTO ad_new_account_launch_purge_audit
                   (launch_fingerprint,purged_at,purged_by,purge_reason,deleted_counts_json)
                   VALUES (?,?,?,?,?)""",
                (
                    fingerprint,
                    (now or datetime.now(timezone.utc)).isoformat(),
                    actor,
                    reason,
                    json.dumps(counts, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {
            "launch_id": launch_id,
            "status": "DELETED",
            "deleted_counts": counts,
            "audit_retained": True,
        }

    experiment_rows = list(conn.execute(
        "SELECT experiment_id,experiment_code,source_recommendation_id FROM ad_experiment WHERE source_report_id=?",
        (launch_id,),
    ).fetchall())
    experiment_ids = [str(row["experiment_id"]) for row in experiment_rows]
    experiment_codes = [str(row["experiment_code"]) for row in experiment_rows]
    recommendation_ids = [str(row["source_recommendation_id"] or "") for row in experiment_rows]
    recommendation_ids = [value for value in recommendation_ids if value]
    decision_rows = _rows_for_ids(
        conn, "growth_decision", "recommendation_id", recommendation_ids, "decision_id",
    )
    decision_ids = [str(row["decision_id"]) for row in decision_rows]
    episode_rows = _rows_for_ids(conn, "growth_decision_episode", "decision_id", decision_ids, "episode_id")
    episode_ids = [str(row["episode_id"]) for row in episode_rows]

    job_rows: List[sqlite3.Row] = []
    if _table_exists(conn, "creative_pro_work_queue"):
        job_rows = list(conn.execute(
            "SELECT job_id,generation_plan_json FROM creative_pro_work_queue WHERE json_extract(material_refs_json,'$.launch_id')=?",
            (launch_id,),
        ).fetchall())
    job_ids = [str(row["job_id"]) for row in job_rows]
    task_rows = _rows_for_ids(
        conn, "creative_generation_tasks", "job_id", job_ids, "task_id,generation_request_id",
    )
    request_ids = [str(row["generation_request_id"] or "") for row in task_rows]
    for row in job_rows:
        try:
            plan = json.loads(str(row["generation_plan_json"] or "{}"))
        except (TypeError, ValueError):
            plan = {}
        request_ids.append(str(plan.get("generation_request_id") or ""))
    request_ids = sorted({value for value in request_ids if value})
    image_rows = _rows_for_ids(
        conn, "creative_generated_images", "request_id", request_ids, "image_id",
    )
    image_ids = [str(row["image_id"]) for row in image_rows]

    counts: Dict[str, int] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        _delete_for_ids(conn, counts, "creative_generated_image_links", "image_id", image_ids)
        _delete_for_ids(conn, counts, "creative_review_records", "image_id", image_ids)
        _delete_for_ids(conn, counts, "creative_generation_review_results", "generated_image_id", image_ids)
        _delete_for_ids(conn, counts, "creative_generated_images", "image_id", image_ids)
        _delete_for_ids(conn, counts, "creative_generation_requests", "request_id", request_ids)
        _delete_for_ids(conn, counts, "creative_generation_tasks", "job_id", job_ids)
        _delete_for_ids(conn, counts, "creative_experiment_suggestions", "experiment_id", experiment_ids)
        _delete_for_ids(conn, counts, "creative_generation_review_results", "experiment_id", experiment_ids)
        _delete_for_ids(conn, counts, "creative_pro_work_queue", "job_id", job_ids)

        _delete_for_ids(conn, counts, "growth_strategy_knowledge", "episode_id", episode_ids)
        _delete_for_ids(conn, counts, "growth_decision_episode", "episode_id", episode_ids)
        _delete_for_ids(conn, counts, "growth_state_transition", "entity_id", episode_ids + decision_ids + experiment_ids)
        _delete_for_ids(conn, counts, "growth_decision", "decision_id", decision_ids)
        _delete_for_ids(conn, counts, "ad_experiment_evaluation", "experiment_id", experiment_ids)
        _delete_for_ids(conn, counts, "ad_experiment_events", "experiment_id", experiment_ids)
        _delete_for_ids(conn, counts, "experiment_context_snapshots", "experiment_id", experiment_ids)
        _delete_for_ids(conn, counts, "ad_experiment", "experiment_id", experiment_ids)
        _delete_for_ids(conn, counts, "ad_recommendation", "recommendation_id", recommendation_ids)

        if _table_exists(conn, "growth_idempotency_record"):
            identity_tokens = [launch_id, *experiment_ids, *decision_ids, *episode_ids]
            clauses = " OR ".join("instr(response_json,?) > 0" for _ in identity_tokens)
            cursor = conn.execute(
                f"DELETE FROM growth_idempotency_record WHERE {clauses}", identity_tokens,
            )
            counts["growth_idempotency_record"] = max(0, int(cursor.rowcount))

        cursor = conn.execute("DELETE FROM ad_new_account_launch_archive WHERE launch_id=?", (launch_id,))
        counts["ad_new_account_launch_archive"] = max(0, int(cursor.rowcount))
        fingerprint = hashlib.sha256(launch_id.encode("utf-8")).hexdigest()[:20]
        conn.execute(
            """INSERT INTO ad_new_account_launch_purge_audit
               (launch_fingerprint,purged_at,purged_by,purge_reason,deleted_counts_json)
               VALUES (?,?,?,?,?)""",
            (fingerprint, (now or datetime.now(timezone.utc)).isoformat(), actor, reason,
             json.dumps(counts, ensure_ascii=False, sort_keys=True)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "launch_id": launch_id,
        "status": "DELETED",
        "deleted_counts": counts,
        "audit_retained": False,
    }


def purge_due_archived_launches(
    conn: sqlite3.Connection,
    *,
    retention_days: int = RETENTION_DAYS,
    limit: int = 50,
    dry_run: bool = False,
    actor: str = "system:growth-retention",
    now: datetime | None = None,
) -> Dict[str, Any]:
    ensure_new_account_launch_retention_tables(conn)
    effective_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows = conn.execute(
        """SELECT launch_id,archived_at FROM ad_new_account_launch_archive
           WHERE status='ARCHIVED' ORDER BY archived_at,launch_id LIMIT ?""",
        (max(1, min(int(limit), 200)),),
    ).fetchall()
    due: List[str] = []
    skipped: List[Dict[str, str]] = []
    purged: List[str] = []
    for row in rows:
        archived_at = _parse_utc(str(row["archived_at"] or ""))
        if not archived_at or archived_at + timedelta(days=max(1, int(retention_days))) > effective_now:
            continue
        launch_id = str(row["launch_id"])
        status = launch_retention_status(
            conn, launch_id, retention_days=retention_days, now=effective_now,
        )
        if not status["can_permanently_delete"]:
            skipped.append({"launch_id": launch_id, "reason": status["permanent_delete_blocked_reason"]})
            continue
        due.append(launch_id)
        if not dry_run:
            purge_new_account_launch(
                conn, launch_id, actor=actor, reason=f"retention_{retention_days}_days", now=effective_now,
            )
            purged.append(launch_id)
    return {
        "retention_days": max(1, int(retention_days)),
        "dry_run": bool(dry_run),
        "due_count": len(due),
        "purged_count": len(purged),
        "due_launch_ids": due,
        "purged_launch_ids": purged,
        "skipped": skipped,
    }
