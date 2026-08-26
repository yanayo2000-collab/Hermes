#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


def audit(db_path: Path) -> dict[str, object]:
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    errors = []
    accepted_rows = 0
    accepted_total = 0.0
    watermarks = conn.execute(
        "SELECT guild_executor_key,stat_date_bj,checksum,last_success_sync_id,"
        "row_count,total_income,revision_version FROM timo_sync_watermark "
        "WHERE lower(COALESCE(data_status,''))='complete' "
        "ORDER BY stat_date_bj,guild_executor_key"
    ).fetchall()
    for watermark in watermarks:
        rows = conn.execute(
            "SELECT timo_id,total_income,revision_version,last_sync_id,row_hash "
            "FROM timo_external_revenue_daily "
            "WHERE guild_executor_key=? AND stat_date_bj=? AND provisional=0 "
            "ORDER BY timo_id,row_hash",
            (watermark['guild_executor_key'], watermark['stat_date_bj']),
        ).fetchall()
        digest = hashlib.sha256()
        for row in rows:
            digest.update(str(row['timo_id']).encode('utf-8'))
            digest.update(b'\x1f')
            digest.update(str(row['row_hash']).encode('ascii'))
            digest.update(b'\n')
        actual_total = sum(float(row['total_income'] or 0) for row in rows)
        mismatches = []
        if len(rows) != int(watermark['row_count'] or 0):
            mismatches.append('row_count')
        if abs(actual_total - float(watermark['total_income'] or 0)) > 0.000001:
            mismatches.append('total_income')
        if rows and {int(row['revision_version'] or 0) for row in rows} != {
            int(watermark['revision_version'] or 0)
        }:
            mismatches.append('revision')
        if rows and {str(row['last_sync_id'] or '') for row in rows} != {
            str(watermark['last_success_sync_id'] or '')
        }:
            mismatches.append('sync_id')
        if any(len(str(row['row_hash'] or '')) != 64 for row in rows):
            mismatches.append('row_hash')
        if digest.hexdigest() != str(watermark['checksum'] or ''):
            mismatches.append('checksum')
        if mismatches:
            errors.append({
                'guild_executor_key': watermark['guild_executor_key'],
                'stat_date_bj': watermark['stat_date_bj'],
                'mismatches': mismatches,
            })
        else:
            accepted_rows += len(rows)
            accepted_total += actual_total
    conn.close()
    return {
        'complete_scopes': len(watermarks),
        'accepted_rows': accepted_rows,
        'accepted_total': round(accepted_total, 6),
        'integrity_error_count': len(errors),
        'errors': errors[:20],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.db)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result['integrity_error_count'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
