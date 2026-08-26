from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_timo_legacy_watermarks.py"
SPEC = importlib.util.spec_from_file_location("reconcile_timo_legacy_watermarks", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
reconcile_legacy_watermarks = MODULE.reconcile_legacy_watermarks


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE timo_external_revenue_daily(
          guild_executor_key TEXT, guild_name TEXT, country TEXT, stat_date_bj TEXT,
          timo_id TEXT, total_income REAL, provisional INTEGER, revision_version INTEGER,
          last_sync_id TEXT, row_hash TEXT, snapshot_at TEXT
        );
        CREATE TABLE timo_sync_run_log(
          sync_id TEXT, guild_executor_key TEXT, stat_date_bj TEXT, status TEXT,
          data_status TEXT, row_count INTEGER, checksum TEXT, start_time TEXT, end_time TEXT
        );
        CREATE TABLE timo_external_sync_runs(
          run_id TEXT, snapshot_at TEXT, data_date_bj TEXT, status TEXT,
          guild_count INTEGER, error TEXT
        );
        CREATE TABLE timo_sync_watermark(
          guild_executor_key TEXT, guild_name TEXT, country TEXT, stat_date_bj TEXT,
          checksum TEXT, last_success_sync_id TEXT, last_success_time TEXT,
          row_count INTEGER, total_income REAL, data_status TEXT, revision_version INTEGER,
          source_snapshot_at TEXT, PRIMARY KEY(guild_executor_key,stat_date_bj)
        );
        """
    )
    rows = [("1", 10.5, "a" * 64), ("2", 4.25, "b" * 64)]
    for timo_id, income, row_hash in rows:
        conn.execute(
            "INSERT INTO timo_external_revenue_daily VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("mx", "Agency MX somente", "Mexico", "2026-07-09", timo_id, income, 0, 1,
             "legacy-1", row_hash, "2026-07-15T08:00:00+00:00"),
        )
    digest = hashlib.sha256()
    for timo_id, _income, row_hash in rows:
        digest.update(timo_id.encode()); digest.update(b"\x1f"); digest.update(row_hash.encode()); digest.update(b"\n")
    conn.execute(
        "INSERT INTO timo_sync_run_log VALUES(?,?,?,?,?,?,?,?,?)",
        ("legacy-1", "mx", "2026-07-09", "success", "complete", 2, digest.hexdigest(),
         "2026-07-23T09:00:00+00:00", "2026-07-23T09:00:01+00:00"),
    )
    conn.execute(
        "INSERT INTO timo_external_sync_runs VALUES(?,?,?,?,?,?)",
        ("official-1", "2026-07-15T08:00:00+00:00", "2026-07-09", "success", 3, ""),
    )
    conn.commit()
    return conn


def test_reconcile_is_dry_run_by_default_and_atomic_on_apply() -> None:
    conn = _db()
    result = reconcile_legacy_watermarks(conn, guild_key="mx", dates=["2026-07-09"])
    assert result[0]["row_count"] == 2
    assert conn.execute("SELECT COUNT(*) FROM timo_sync_watermark").fetchone()[0] == 0
    reconcile_legacy_watermarks(conn, guild_key="mx", dates=["2026-07-09"], apply=True)
    stored = conn.execute(
        "SELECT row_count,total_income,data_status FROM timo_sync_watermark"
    ).fetchone()
    assert tuple(stored) == (2, 14.75, "complete")


def test_reconcile_fails_closed_on_receipt_mismatch() -> None:
    conn = _db()
    conn.execute("UPDATE timo_sync_run_log SET row_count=1")
    conn.commit()
    with pytest.raises(ValueError, match="success_receipt_mismatch"):
        reconcile_legacy_watermarks(conn, guild_key="mx", dates=["2026-07-09"], apply=True)
    assert conn.execute("SELECT COUNT(*) FROM timo_sync_watermark").fetchone()[0] == 0


def test_reconcile_promotes_exact_existing_provisional_watermark() -> None:
    conn = _db()
    candidate = reconcile_legacy_watermarks(
        conn, guild_key="mx", dates=["2026-07-09"]
    )[0]
    candidate.pop("existing_provisional")
    candidate["data_status"] = "provisional"
    columns = ",".join(candidate)
    placeholders = ",".join(f":{column}" for column in candidate)
    conn.execute(
        f"INSERT INTO timo_sync_watermark({columns}) VALUES({placeholders})", candidate
    )
    conn.commit()
    result = reconcile_legacy_watermarks(
        conn, guild_key="mx", dates=["2026-07-09"], apply=True
    )
    assert result[0]["existing_provisional"] is True
    assert conn.execute("SELECT data_status FROM timo_sync_watermark").fetchone()[0] == "complete"


def test_reconcile_refuses_conflicting_existing_watermark() -> None:
    conn = _db()
    reconcile_legacy_watermarks(conn, guild_key="mx", dates=["2026-07-09"], apply=True)
    with pytest.raises(ValueError, match="existing_watermark_mismatch"):
        reconcile_legacy_watermarks(conn, guild_key="mx", dates=["2026-07-09"], apply=True)


def test_reconcile_legacy_provisional_receipt_requires_full_official_generation() -> None:
    conn = _db()
    conn.execute("UPDATE timo_sync_run_log SET data_status='provisional'")
    conn.execute("UPDATE timo_external_revenue_daily SET last_sync_id='timo_legacy_bootstrap_20260709_x'")
    conn.execute("UPDATE timo_sync_run_log SET sync_id='timo_legacy_bootstrap_20260709_x'")
    conn.execute("DELETE FROM timo_external_sync_runs")
    conn.commit()
    with pytest.raises(ValueError, match="legacy_official_generation_missing"):
        reconcile_legacy_watermarks(conn, guild_key="mx", dates=["2026-07-09"], apply=True)
