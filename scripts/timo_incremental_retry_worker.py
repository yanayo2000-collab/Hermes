#!/usr/bin/env python3
"""Run due Timo incremental-sync retries without a queue or message broker."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault('MCN_PROCESS_ROLE', 'timo-incremental-retry')
os.environ.setdefault('MCN_DISABLE_GLOBAL_APP_BOOTSTRAP', '1')

from app.batch_runtime import assert_managed_batch_runtime  # noqa: E402
from app.batch_terminal import source_quality_collection_exit_code  # noqa: E402
from app.main import Database, Service  # noqa: E402
from app.sqlite_job_lock import (  # noqa: E402
    JobLockBusy,
    acquire_sqlite_job_lock,
    print_job_lock_skip,
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run due Timo incremental sync retries.')
    parser.add_argument('--db-path', default=str(ROOT_DIR / 'data' / 'automation.db'))
    parser.add_argument(
        '--status-path',
        default=str(ROOT_DIR / 'data' / 'timo_incremental_retry_status.json'),
    )
    parser.add_argument('--max-dates', type=int, default=1)
    parser.add_argument('--fail-on-lock-busy', action='store_true')
    return parser.parse_args()


def due_retry_dates(service: Service, *, max_dates: int = 1) -> List[str]:
    with service.db.connect() as conn:
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT guild_executor_key, stat_date_bj, MAX(start_time) AS latest_start_time
                FROM timo_sync_run_log
                GROUP BY guild_executor_key, stat_date_bj
            )
            SELECT DISTINCT runs.stat_date_bj
            FROM timo_sync_run_log AS runs
            JOIN latest
              ON latest.guild_executor_key=runs.guild_executor_key
             AND latest.stat_date_bj=runs.stat_date_bj
             AND latest.latest_start_time=runs.start_time
            WHERE runs.status IN ('failed', 'quality_failed')
              AND runs.retry_attempt>=1
              AND COALESCE(runs.next_retry_at, '')<>''
              AND runs.next_retry_at<=?
            ORDER BY runs.next_retry_at ASC, runs.stat_date_bj ASC
            LIMIT ?
            """,
            (datetime.now(timezone.utc).isoformat(), max(1, min(10, int(max_dates or 1)))),
        ).fetchall()
    return [str(row['stat_date_bj']) for row in rows]


def _run(args: argparse.Namespace) -> Dict[str, Any]:
    service = Service(Database(args.db_path))
    dates = due_retry_dates(service, max_dates=args.max_dates)
    results: List[Dict[str, Any]] = []
    for stat_date_bj in dates:
        result = service.materialize_timo_external_feed_snapshot(
            data_date_bj=stat_date_bj,
            include_today=True,
            user={'role': 'super_admin', 'username': 'timo-incremental-retry'},
        )
        results.append(result)
    return {
        'ok': all(bool(result.get('ok')) for result in results),
        'status': 'idle' if not dates else ('success' if all(bool(result.get('ok')) for result in results) else 'partial'),
        'due_dates': dates,
        'results': results,
    }


def _write_status(path: str, result: Dict[str, Any]) -> None:
    status_path = Path(path)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = status_path.with_suffix(status_path.suffix + '.tmp')
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
        encoding='utf-8',
    )
    temporary.replace(status_path)


def _result_exit_code(result: Dict[str, Any]) -> int:
    return source_quality_collection_exit_code(result.get('results') or [])


def main() -> int:
    args = _args()
    assert_managed_batch_runtime('timo_incremental_retry', required_slice='mcn-batch.slice')
    try:
        lock = acquire_sqlite_job_lock('sqlite-etl')
    except JobLockBusy as exc:
        print_job_lock_skip(exc)
        return 75 if args.fail_on_lock_busy else 0
    with lock:
        result = _run(args)
    _write_status(args.status_path, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return _result_exit_code(result)


if __name__ == '__main__':
    raise SystemExit(main())
