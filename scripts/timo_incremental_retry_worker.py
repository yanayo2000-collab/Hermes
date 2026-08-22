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
from scripts.notify_timo_materialization import current_event_for_date  # noqa: E402


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run due Timo incremental sync retries.')
    parser.add_argument('--db-path', default=str(ROOT_DIR / 'data' / 'automation.db'))
    parser.add_argument(
        '--status-path',
        default=str(ROOT_DIR / 'data' / 'timo_incremental_retry_status.json'),
    )
    parser.add_argument('--max-dates', type=int, default=1)
    parser.add_argument(
        '--notification-ack-path',
        default=str(ROOT_DIR / 'data' / 'timo_materialization_notification_ack.json'),
    )
    parser.add_argument('--fail-on-lock-busy', action='store_true')
    return parser.parse_args()


def _acknowledged_event_id(path: str | Path | None) -> str:
    if not path:
        return ''
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return ''
    return str(payload.get('event_id') or '') if isinstance(payload, dict) else ''


def due_retry_dates(
    service: Service,
    *,
    max_dates: int = 1,
    notification_ack_path: str | Path | None = None,
) -> List[str]:
    limit = max(1, min(10, int(max_dates or 1)))
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
            (datetime.now(timezone.utc).isoformat(), limit),
        ).fetchall()
        dates = [str(row['stat_date_bj']) for row in rows]
        if notification_ack_path and len(dates) < limit:
            latest = conn.execute(
                'SELECT MAX(stat_date_bj) AS stat_date_bj FROM timo_sync_watermark',
            ).fetchone()
            candidate = str(latest['stat_date_bj'] or '') if latest is not None else ''
            if candidate and candidate not in dates:
                try:
                    event = current_event_for_date(conn, candidate)
                except ValueError:
                    event = {}
                if (
                    event
                    and str(event.get('eventId') or '')
                    != _acknowledged_event_id(notification_ack_path)
                ):
                    dates.append(candidate)
    return dates[:limit]


def _run(args: argparse.Namespace) -> Dict[str, Any]:
    service = Service(Database(args.db_path))
    dates = due_retry_dates(
        service,
        max_dates=args.max_dates,
        notification_ack_path=args.notification_ack_path,
    )
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
