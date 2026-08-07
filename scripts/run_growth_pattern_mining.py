#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3

from app.growth.pattern_mining_service import PatternMiningService
from app.growth.schema import ensure_growth_schema
from app.main_shared import DEFAULT_DB_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description="Mine reviewable Growth knowledge candidates")
    parser.add_argument("--database-path", default=os.getenv("DB_PATH") or DEFAULT_DB_PATH)
    parser.add_argument("--minimum-support", type=int, default=2)
    args = parser.parse_args()
    conn = sqlite3.connect(args.database_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_growth_schema(conn)
    try:
        created = PatternMiningService(conn).mine(minimum_support=args.minimum_support)
        print(json.dumps({
            "ok": True, "created_count": len(created),
            "knowledge_ids": [item["knowledge_id"] for item in created],
        }, ensure_ascii=False, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
