#!/usr/bin/env python3
"""Run due Timo incremental-sync retries without a queue or message broker."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

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


PUBLICATION_STABILITY_SECONDS = 2700
PUBLICATION_REOBSERVE_SECONDS = max(
    60,
    int(os.getenv('TIMO_PUBLICATION_REOBSERVE_SECONDS') or 300),
)


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
    parser.add_argument('--check-due-only', action='store_true')
    return parser.parse_args()


def _ack_payload(path: str | Path | None) -> Dict[str, Any]:
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _acknowledged_scope_lineage(path: str | Path | None, stat_date_bj: str) -> Dict[str, Any]:
    payload = _ack_payload(path)
    acknowledgements = payload.get('acknowledgements')
    if isinstance(acknowledgements, dict):
        item = acknowledgements.get(stat_date_bj)
        lineage = item.get('scope_lineage') if isinstance(item, dict) else None
        return lineage if isinstance(lineage, dict) else {}
    if str(payload.get('business_date') or '') != stat_date_bj:
        return {}
    lineage = payload.get('scope_lineage')
    return lineage if isinstance(lineage, dict) else {}


def _has_unacknowledged_lineage(
    current_lineage: Dict[str, Any],
    acknowledged_lineage: Dict[str, Any],
) -> bool:
    def content_identity(value: Any) -> tuple[str, int]:
        item = value if isinstance(value, dict) else {}
        return (
            str(item.get('checksum') or ''),
            int(item.get('revision') or 0),
        )

    return any(
        content_identity(acknowledged_lineage.get(guild_name)) != content_identity(lineage)
        for guild_name, lineage in current_lineage.items()
    )


def _tracked_publication_dates(
    conn: Any,
    notification_ack_path: str | Path | None,
) -> List[str]:
    current_bj = datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
    lag_days = 1 if current_bj.hour >= 16 else 2
    latest_eligible = (current_bj.date() - timedelta(days=lag_days)).isoformat()
    latest = conn.execute(
        """
        SELECT MAX(stat_date_bj) AS stat_date_bj
        FROM timo_sync_watermark
        WHERE stat_date_bj<=?
        """,
        (latest_eligible,),
    ).fetchone()
    dates = {str(latest['stat_date_bj'] or '')} if latest is not None else set()
    payload = _ack_payload(notification_ack_path)
    acknowledgements = payload.get('acknowledgements')
    if isinstance(acknowledgements, dict):
        dates.update(str(value) for value in acknowledgements if str(value) <= latest_eligible)
    elif str(payload.get('business_date') or '') <= latest_eligible:
        dates.add(str(payload.get('business_date') or ''))
    return sorted((value for value in dates if value), reverse=True)[:7]


def _publication_lineage(conn: Any, stat_date_bj: str) -> Dict[str, Any]:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=PUBLICATION_STABILITY_SECONDS)
    ).isoformat()
    lineage: Dict[str, Any] = {}
    watermarks = conn.execute(
        """
        SELECT guild_executor_key, guild_name, checksum, revision_version,
               last_success_sync_id, last_success_time
        FROM timo_sync_watermark
        WHERE stat_date_bj=? AND data_status='complete'
        """,
        (stat_date_bj,),
    ).fetchall()
    for watermark in watermarks:
        latest = conn.execute(
            """
            SELECT status
            FROM timo_sync_run_log
            WHERE guild_executor_key=? AND stat_date_bj=?
            ORDER BY start_time DESC
            LIMIT 1
            """,
            (str(watermark['guild_executor_key']), stat_date_bj),
        ).fetchone()
        observations = conn.execute(
            """
            SELECT COUNT(*) AS observation_count
            FROM timo_sync_run_log
            WHERE guild_executor_key=? AND stat_date_bj=? AND data_status='complete'
              AND status IN ('success','no_op') AND checksum=?
            """,
            (
                str(watermark['guild_executor_key']),
                stat_date_bj,
                str(watermark['checksum'] or ''),
            ),
        ).fetchone()
        if (
            latest is None
            or str(latest['status'] or '') not in {'success', 'no_op'}
            or int(observations['observation_count'] or 0) < 2
            or str(watermark['last_success_time'] or '') > cutoff
        ):
            continue
        lineage[str(watermark['guild_name'] or '')] = {
            'checksum': str(watermark['checksum'] or ''),
            'revision': int(watermark['revision_version'] or 0),
            'source_generation': str(watermark['last_success_sync_id'] or ''),
        }
    return lineage


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _publication_reobservation_due(
    conn: Any,
    stat_date_bj: str,
    notification_ack_path: str | Path | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Actively close a complete scope's missing second observation.

    The 45-minute stability gate remains authoritative. This only stops a first
    complete observation from waiting for the sparse revision timer to happen
    to revisit the same business date.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    acknowledged = _acknowledged_scope_lineage(notification_ack_path, stat_date_bj)
    watermarks = conn.execute(
        """
        SELECT guild_executor_key, guild_name, checksum, revision_version,
               last_success_sync_id
        FROM timo_sync_watermark
        WHERE stat_date_bj=? AND data_status='complete'
        """,
        (stat_date_bj,),
    ).fetchall()
    for watermark in watermarks:
        guild_name = str(watermark['guild_name'] or '')
        current_lineage = {
            'checksum': str(watermark['checksum'] or ''),
            'revision': int(watermark['revision_version'] or 0),
            'source_generation': str(watermark['last_success_sync_id'] or ''),
        }
        if current_lineage == acknowledged.get(guild_name):
            continue
        latest = conn.execute(
            """
            SELECT status, start_time, end_time
            FROM timo_sync_run_log
            WHERE guild_executor_key=? AND stat_date_bj=?
            ORDER BY start_time DESC
            LIMIT 1
            """,
            (str(watermark['guild_executor_key']), stat_date_bj),
        ).fetchone()
        if latest is None or str(latest['status'] or '') not in {'success', 'no_op'}:
            continue
        observations = conn.execute(
            """
            SELECT COUNT(*) AS observation_count
            FROM timo_sync_run_log
            WHERE guild_executor_key=? AND stat_date_bj=? AND data_status='complete'
              AND status IN ('success','no_op') AND checksum=?
            """,
            (
                str(watermark['guild_executor_key']),
                stat_date_bj,
                str(watermark['checksum'] or ''),
            ),
        ).fetchone()
        if int(observations['observation_count'] or 0) >= 2:
            continue
        observed_at = _parse_utc(latest['end_time'] or latest['start_time'])
        if observed_at and (current - observed_at).total_seconds() >= PUBLICATION_REOBSERVE_SECONDS:
            return True
    return False


def due_retry_dates(
    service: Service,
    *,
    max_dates: int = 1,
    notification_ack_path: str | Path | None = None,
) -> List[str]:
    limit = max(1, min(10, int(max_dates or 1)))
    with service.db.connect() as conn:
        dates: List[str] = []
        if notification_ack_path:
            for candidate in _tracked_publication_dates(conn, notification_ack_path):
                current_lineage = _publication_lineage(conn, candidate)
                if (
                    current_lineage
                    and _has_unacknowledged_lineage(
                        current_lineage,
                        _acknowledged_scope_lineage(notification_ack_path, candidate),
                    )
                ) or _publication_reobservation_due(
                    conn,
                    candidate,
                    notification_ack_path,
                ):
                    dates.append(candidate)
                    if len(dates) >= limit:
                        return dates
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
            (datetime.now(timezone.utc).isoformat(), limit - len(dates)),
        ).fetchall()
        dates.extend(
            str(row['stat_date_bj'])
            for row in rows
            if str(row['stat_date_bj']) not in dates
        )
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


def _check_due(args: argparse.Namespace) -> Dict[str, Any]:
    service = Service(Database(args.db_path))
    dates = due_retry_dates(
        service,
        max_dates=args.max_dates,
        notification_ack_path=args.notification_ack_path,
    )
    return {
        'ok': True,
        'status': 'due' if dates else 'idle',
        'due_dates': dates,
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
    if args.check_due_only:
        result = _check_due(args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
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
