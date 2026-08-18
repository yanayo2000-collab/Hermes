#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault('MCN_PROCESS_ROLE', 'timo-revenue-publication-repair')
os.environ.setdefault('MCN_DISABLE_GLOBAL_APP_BOOTSTRAP', '1')

from app.sqlite_write_window import connect_short_write_sqlite  # noqa: E402
from app.timo_incremental_materialization import (  # noqa: E402
    TimoDbSyncLease,
    materialize_timo_revenue_snapshot,
    timo_external_feed_status,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Atomically publish a complete authoritative Timo revenue scope snapshot.',
    )
    parser.add_argument('--db-path', default=str(ROOT / 'data' / 'automation.db'))
    parser.add_argument('--snapshot-json', required=True)
    parser.add_argument('--expected-snapshot-sha256', required=True)
    parser.add_argument('--sync-id', default='')
    return parser.parse_args()


def load_snapshot(path: Path, expected_sha256: str) -> Dict[str, Any]:
    raw = path.read_bytes()
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != str(expected_sha256 or '').strip().lower():
        raise ValueError(f'snapshot_sha256_mismatch:{actual_sha256}')
    payload = json.loads(raw)
    if payload.get('schema_version') != 'timo_official_revenue_snapshot_v1':
        raise ValueError('unsupported_snapshot_schema')
    rows = list(payload.get('rows') or [])
    expected_rows = int(payload.get('effective_row_count') or 0)
    expected_total = float(payload.get('total_income') or 0)
    actual_total = sum(float(row.get('total_income') or 0) for row in rows)
    ids = [str(row.get('timo_id') or '').strip() for row in rows]
    if not rows or len(rows) != expected_rows or len(set(ids)) != len(ids) or any(not item for item in ids):
        raise ValueError('snapshot_row_set_mismatch')
    if abs(actual_total - expected_total) > 0.000001:
        raise ValueError('snapshot_total_mismatch')
    if any(str(row.get('guild_id') or '') != str(payload.get('guild_id') or '') for row in rows):
        raise ValueError('snapshot_guild_mismatch')
    payload['_snapshot_sha256'] = actual_sha256
    return payload


def main() -> int:
    args = parse_args()
    snapshot = load_snapshot(Path(args.snapshot_json), args.expected_snapshot_sha256)
    db_path = str(Path(args.db_path).expanduser().resolve())
    sync_id = str(args.sync_id or '').strip() or f'timo_official_repair_{uuid.uuid4().hex}'
    scope_key = str(snapshot['guild_executor_key'])
    business_date = str(snapshot['business_date_bj'])

    def connect() -> sqlite3.Connection:
        conn = connect_short_write_sqlite(
            db_path,
            lock_name='sqlite-etl',
            source='timo-revenue-publication-repair',
            busy_timeout_ms_override=10000,
            write_window_timeout_seconds=120.0,
            write_lock_timeout_seconds=10.0,
        )
        conn.row_factory = sqlite3.Row
        return conn

    lease = TimoDbSyncLease(
        connect,
        lock_key=f'timo_sync:{scope_key}:{business_date}',
        owner_sync_id=sync_id,
        ttl_seconds=600,
        auto_renew=True,
    ).acquire()
    try:
        result = materialize_timo_revenue_snapshot(
            connect,
            sync_id=sync_id,
            parent_run_id='official-authoritative-publication-repair',
            guild_executor_key=scope_key,
            guild_name=str(snapshot['guild_name']),
            country=str(snapshot['country']),
            stat_date_bj=business_date,
            provisional=False,
            revenue_rows=snapshot['rows'],
            snapshot_at=str(snapshot['source_exported_at']),
            idempotency_key=sync_id,
            source_provenance={
                'source_kind': str(snapshot['source_kind']),
                'source_business_date_bj': business_date,
                'normalized_stat_date_bj': business_date,
                'fetched_at': str(snapshot['source_exported_at']),
                'raw_response_sha256': str(snapshot['source_workbook_sha256']),
                'authoritative_snapshot_sha256': str(snapshot['_snapshot_sha256']),
                'raw_row_count': int(snapshot['raw_row_count']),
                'effective_row_count': int(snapshot['effective_row_count']),
            },
        )
    finally:
        lease.release()

    conn = connect()
    try:
        manifest = timo_external_feed_status(
            conn,
            stat_date_bj=business_date,
            country=str(snapshot['country']),
            guild_name=str(snapshot['guild_name']),
        )
    finally:
        conn.close()
    print(json.dumps({
        'ok': True,
        'sync_id': sync_id,
        'snapshot_sha256': snapshot['_snapshot_sha256'],
        'result': result,
        'manifest': manifest,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
