#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3

from app.growth.creative_naming import backfill_launch_creative_names


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with sqlite3.connect(args.db, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        report = backfill_launch_creative_names(conn, apply=args.apply)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
