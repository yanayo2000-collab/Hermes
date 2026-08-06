from __future__ import annotations

import argparse
import sqlite3

from app.growth.schema import GROWTH_SCHEMA_DOWN_SQL, ensure_growth_schema


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply or roll back Growth Loop v2 schema")
    parser.add_argument("direction", choices=("up", "down"))
    parser.add_argument("database_path")
    args = parser.parse_args()
    with sqlite3.connect(args.database_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        if args.direction == "up":
            ensure_growth_schema(conn)
        else:
            conn.executescript(GROWTH_SCHEMA_DOWN_SQL)
        conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
