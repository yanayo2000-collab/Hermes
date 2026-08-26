#!/usr/bin/env python3
"""Rebuild missing Timo scope watermarks from immutable accepted legacy facts.

The command is dry-run by default.  It never changes facts and only inserts a
missing watermark when the fact lineage and its legacy success receipt agree
exactly on scope, row count, revision, sync id and checksum.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.sqlite_job_lock import JobLockBusy, acquire_sqlite_job_lock  # noqa: E402


def _checksum(rows: Iterable[sqlite3.Row]) -> str:
    import hashlib

    digest = hashlib.sha256()
    for row in rows:
        digest.update(str(row["timo_id"]).encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(str(row["row_hash"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _candidate(conn: sqlite3.Connection, guild_key: str, stat_date: str) -> dict[str, Any]:
    existing = conn.execute(
        "SELECT * FROM timo_sync_watermark WHERE guild_executor_key=? AND stat_date_bj=?",
        (guild_key, stat_date),
    ).fetchone()
    facts = conn.execute(
        """
        SELECT guild_name,country,timo_id,total_income,revision_version,last_sync_id,
               row_hash,snapshot_at
        FROM timo_external_revenue_daily
        WHERE guild_executor_key=? AND stat_date_bj=? AND provisional=0
        ORDER BY timo_id,row_hash
        """,
        (guild_key, stat_date),
    ).fetchall()
    if not facts:
        raise ValueError(f"accepted_facts_missing:{stat_date}")
    if any(len(str(row["row_hash"] or "")) != 64 for row in facts):
        raise ValueError(f"invalid_fact_row_hash:{stat_date}")
    guild_names = {str(row["guild_name"] or "") for row in facts}
    countries = {str(row["country"] or "") for row in facts}
    revisions = {int(row["revision_version"] or 0) for row in facts}
    sync_ids = {str(row["last_sync_id"] or "") for row in facts}
    if len(guild_names) != 1 or len(countries) != 1 or len(revisions) != 1 or len(sync_ids) != 1:
        raise ValueError(f"mixed_fact_lineage:{stat_date}")
    revision = next(iter(revisions))
    sync_id = next(iter(sync_ids))
    if revision <= 0 or not sync_id:
        raise ValueError(f"invalid_fact_lineage:{stat_date}")
    checksum = _checksum(facts)
    receipt = conn.execute(
        """
        SELECT status,data_status,row_count,checksum,start_time,end_time
        FROM timo_sync_run_log
        WHERE sync_id=? AND guild_executor_key=? AND stat_date_bj=?
        """,
        (sync_id, guild_key, stat_date),
    ).fetchone()
    if receipt is None:
        raise ValueError(f"success_receipt_missing:{stat_date}")
    if str(receipt["status"] or "") not in {"success", "no_op"}:
        raise ValueError(f"success_receipt_invalid:{stat_date}")
    source_snapshot_at = max(str(row["snapshot_at"] or "") for row in facts)
    receipt_complete = str(receipt["data_status"] or "") == "complete"
    if receipt_complete:
        if int(receipt["row_count"] or 0) != len(facts) or str(receipt["checksum"] or "") != checksum:
            raise ValueError(f"success_receipt_mismatch:{stat_date}")
    else:
        if not sync_id.startswith("timo_legacy_bootstrap_"):
            raise ValueError(f"success_receipt_not_complete:{stat_date}")
        if (
            existing is None
            or int(existing["row_count"] or 0) != int(receipt["row_count"] or 0)
            or str(existing["checksum"] or "") != str(receipt["checksum"] or "")
            or str(existing["last_success_sync_id"] or "") != sync_id
            or str(existing["data_status"] or "") != "provisional"
        ):
            raise ValueError(f"legacy_provisional_receipt_mismatch:{stat_date}")
        official_generation = conn.execute(
            """
            SELECT run_id FROM timo_external_sync_runs
            WHERE data_date_bj=? AND status='success' AND guild_count>=3
              AND snapshot_at=? AND error=''
            """,
            (stat_date, source_snapshot_at),
        ).fetchone()
        if official_generation is None:
            raise ValueError(f"legacy_official_generation_missing:{stat_date}")
    candidate = {
        "guild_executor_key": guild_key,
        "guild_name": next(iter(guild_names)),
        "country": next(iter(countries)),
        "stat_date_bj": stat_date,
        "checksum": checksum,
        "last_success_sync_id": sync_id,
        "last_success_time": str(receipt["end_time"] or receipt["start_time"]),
        "row_count": len(facts),
        "total_income": float(sum(float(row["total_income"] or 0) for row in facts)),
        "data_status": "complete",
        "revision_version": revision,
        "source_snapshot_at": source_snapshot_at,
    }
    if existing is not None:
        exact_existing = bool(
            str(existing["guild_name"] or "") == candidate["guild_name"]
            and str(existing["country"] or "") == candidate["country"]
            and str(existing["last_success_sync_id"] or "") == candidate["last_success_sync_id"]
            and int(existing["revision_version"] or 0) == candidate["revision_version"]
            and str(existing["data_status"] or "") == "provisional"
        )
        if not exact_existing:
            raise ValueError(f"existing_watermark_mismatch:{stat_date}")
        candidate["existing_provisional"] = True
        candidate["preimage_checksum"] = str(existing["checksum"] or "")
        candidate["preimage_row_count"] = int(existing["row_count"] or 0)
        candidate["preimage_revision_version"] = int(existing["revision_version"] or 0)
    else:
        candidate["existing_provisional"] = False
    return candidate


def reconcile_legacy_watermarks(
    conn: sqlite3.Connection,
    *,
    guild_key: str,
    dates: list[str],
    apply: bool = False,
) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    candidates = [_candidate(conn, guild_key, stat_date) for stat_date in dates]
    if apply:
        conn.execute("BEGIN IMMEDIATE")
        try:
            new_candidates = [row for row in candidates if not row["existing_provisional"]]
            provisional_candidates = [row for row in candidates if row["existing_provisional"]]
            conn.executemany(
                """
                INSERT INTO timo_sync_watermark(
                    guild_executor_key,guild_name,country,stat_date_bj,checksum,
                    last_success_sync_id,last_success_time,row_count,total_income,
                    data_status,revision_version,source_snapshot_at
                ) VALUES(
                    :guild_executor_key,:guild_name,:country,:stat_date_bj,:checksum,
                    :last_success_sync_id,:last_success_time,:row_count,:total_income,
                    :data_status,:revision_version,:source_snapshot_at
                )
                """,
                new_candidates,
            )
            conn.executemany(
                """
                UPDATE timo_sync_watermark
                SET data_status='complete', source_snapshot_at=:source_snapshot_at,
                    checksum=:checksum, row_count=:row_count, total_income=:total_income
                WHERE guild_executor_key=:guild_executor_key
                  AND stat_date_bj=:stat_date_bj
                  AND data_status='provisional'
                  AND checksum=:preimage_checksum
                  AND last_success_sync_id=:last_success_sync_id
                  AND row_count=:preimage_row_count
                  AND revision_version=:preimage_revision_version
                """,
                provisional_candidates,
            )
            for candidate in candidates:
                verified = conn.execute(
                    """
                    SELECT row_count,total_income,checksum,revision_version,last_success_sync_id
                    FROM timo_sync_watermark WHERE guild_executor_key=? AND stat_date_bj=?
                    """,
                    (candidate["guild_executor_key"], candidate["stat_date_bj"]),
                ).fetchone()
                if (
                    verified is None
                    or int(verified["row_count"]) != candidate["row_count"]
                    or abs(float(verified["total_income"]) - candidate["total_income"]) > 0.000001
                    or str(verified["checksum"]) != candidate["checksum"]
                    or int(verified["revision_version"]) != candidate["revision_version"]
                    or str(verified["last_success_sync_id"]) != candidate["last_success_sync_id"]
                ):
                    raise ValueError(f"post_insert_verification_failed:{candidate['stat_date_bj']}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--guild-key", required=True)
    parser.add_argument("--date", action="append", dest="dates", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    lock = None
    try:
        if args.apply:
            lock = acquire_sqlite_job_lock("sqlite-etl", timeout_seconds=0)
            lock.__enter__()
        conn = sqlite3.connect(args.db)
        try:
            result = reconcile_legacy_watermarks(
                conn, guild_key=args.guild_key, dates=list(args.dates), apply=args.apply
            )
        finally:
            conn.close()
    except JobLockBusy:
        print(json.dumps({"ok": False, "error": "sqlite_etl_busy"}, sort_keys=True))
        return 75
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)
    print(json.dumps({"ok": True, "applied": args.apply, "scopes": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
