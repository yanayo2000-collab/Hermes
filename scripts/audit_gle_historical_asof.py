#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.growth.historical_asof_audit import (
    HistoricalAsOfAuditError,
    build_audit,
    make_request,
    open_readonly_snapshot,
    write_audit_bundle,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the offline GLE G1-02A immutable as-of audit bundle")
    parser.add_argument("--database", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--audit-id", required=True)
    parser.add_argument("--data-cutoff-at", required=True)
    parser.add_argument("--captured-at", required=True)
    parser.add_argument("--source-logical-id", required=True)
    args = parser.parse_args(argv)
    conn = None
    try:
        request = make_request(
            audit_id=args.audit_id, data_cutoff_at=args.data_cutoff_at,
            captured_at=args.captured_at, source_logical_id=args.source_logical_id,
        )
        conn = open_readonly_snapshot(args.database)
        bundle = build_audit(conn, request, source_path=args.database)
        manifest = write_audit_bundle(bundle, args.output_dir)
    except HistoricalAsOfAuditError as exc:
        print(str(exc).split(":", 1)[0], file=sys.stderr)
        return 64
    finally:
        if conn is not None:
            conn.close()
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
