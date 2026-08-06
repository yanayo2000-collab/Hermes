#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path

from app.streamer_app_fan import import_complete_app_fan_history


def main() -> int:
    parser = argparse.ArgumentParser(description='Import a governed complete Tugao App-fan ID snapshot.')
    parser.add_argument('--db-path', required=True)
    parser.add_argument('--app-name', choices=('linky', 'timo'), required=True)
    parser.add_argument('--input', required=True)
    parser.add_argument('--complete-through', required=True)
    parser.add_argument('--expected-count', type=int, required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()

    source = Path(args.input)
    content = source.read_bytes()
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if actual_sha256 != args.expected_sha256.lower():
        raise SystemExit('app_fan_history_sha256_mismatch')
    ids = [line.strip().lstrip('\ufeff') for line in content.decode('utf-8-sig').splitlines() if line.strip()]
    unique_ids = set(ids)
    if len(ids) != len(unique_ids) or len(unique_ids) != args.expected_count:
        raise SystemExit('app_fan_history_count_mismatch')
    if not args.apply:
        print({'ok': True, 'dry_run': True, 'app_name': args.app_name, 'id_count': len(unique_ids), 'sha256': actual_sha256})
        return 0
    conn = sqlite3.connect(args.db_path)
    try:
        result = import_complete_app_fan_history(
            conn,
            app_name=args.app_name,
            streamer_ids=unique_ids,
            snapshot_id=actual_sha256,
            complete_through=args.complete_through,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print({'ok': True, 'dry_run': False, **result})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
