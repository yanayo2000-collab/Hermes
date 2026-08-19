#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault('MCN_PROCESS_ROLE', 'streamer-etl')
os.environ.setdefault('MCN_DISABLE_GLOBAL_APP_BOOTSTRAP', '1')

from app.streamer_external_sync import sync_streamer_external_data  # noqa: E402
from app.linky_source_readiness import persisted_linky_scope_ready  # noqa: E402
from app.linky_phase_admission import linky_compute_admission_command  # noqa: E402
from app.streamer_data_foundation import record_ingestion_scope  # noqa: E402
from app.streamer_analytics import (  # noqa: E402
    LINKY_STREAMER_ANALYTICS_SUPPORT_TABLES,
    materialize_streamer_analytics_tables,
)
from app.sqlite_write_window import connect_short_write_sqlite  # noqa: E402
from app.batch_runtime import assert_managed_batch_runtime  # noqa: E402
from mcn_phase_resource_handoff import handoff_network_phase  # noqa: E402


DEFAULT_PROGRESS_DIR = Path(
    os.getenv('MCN_TASK_PROGRESS_DIR')
    or '/var/lib/mcn-ai-automation/task-progress'
)


def _linky_candidate_build_admitted() -> bool:
    completed = subprocess.run(
        linky_compute_admission_command(ROOT),
        cwd=str(ROOT),
        check=False,
    )
    return completed.returncode == 0


def _open_read_only_source(database: str | Path) -> sqlite3.Connection:
    path = Path(database).expanduser().resolve()
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


class ProgressRecorder:
    """Persist the latest stage so callers do not need SSH polling."""

    def __init__(self, path: Optional[Path], *, app: str, target_date: str) -> None:
        self.path = path
        self.payload: dict[str, Any] = {
            'schema_version': 1,
            'app': app,
            'target_date': target_date,
            'status': 'running',
            'phase': 'starting',
            'started_at': datetime.now(timezone.utc).isoformat(),
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        self._persist()

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f'.{self.path.name}.',
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(
                    self.payload,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def phase(self, phase: str) -> None:
        self.payload.update({
            'phase': phase,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        })
        self._persist()
        print(
            json.dumps({'event': 'streamer_analytics_phase', 'phase': phase}),
            file=sys.stderr,
            flush=True,
        )

    def finish(self, status: str, **detail: Any) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.payload.update({
            'status': status,
            'phase': 'complete' if status == 'succeeded' else 'failed',
            'updated_at': now,
            'finished_at': now,
            **detail,
        })
        self._persist()


def _executor_key_from_row(row: sqlite3.Row) -> str:
    for key in ('cms_guild_sid', 'cms_guild_id'):
        value = str(row[key] or '').strip()
        if value:
            return f'linky:{key}:{value}'
    return str(row['guild_name'] or '').strip()


def _complete_linky_source_run(
    conn: sqlite3.Connection,
    *,
    target_date: date,
) -> Optional[dict[str, Any]]:
    """Return immutable evidence only when all enabled Linky scopes succeeded."""
    executors = conn.execute(
        """
        SELECT guild_name, cms_guild_sid, cms_guild_id
        FROM guild_executors
        WHERE enabled=1 AND lower(app_name)='linky'
        ORDER BY guild_name
        """
    ).fetchall()
    expected_keys = {
        _executor_key_from_row(row)
        for row in executors
    }
    if not expected_keys:
        return None
    rows = conn.execute(
        """
        SELECT run_id, guild_count, profile_count, revenue_count, updated_at
        FROM streamer_external_sync_runs
        WHERE app_name='linky'
          AND run_scope IN ('full','composite')
          AND status='success'
          AND date_from<=?
          AND date_to>=?
        ORDER BY updated_at DESC
        """,
        (target_date.isoformat(), target_date.isoformat()),
    ).fetchall()
    for row in rows:
        run_id = str(row['run_id'] or '')
        scope_rows = conn.execute(
            """
            SELECT dataset, guild_executor_key, business_date, status
            FROM streamer_ingestion_run_scopes
            WHERE run_id=?
              AND app_name='linky'
              AND dataset IN ('anchor_directory','streamer_stat')
            """,
            (run_id,),
        ).fetchall()
        anchor_keys = {
            str(scope['guild_executor_key'] or '')
            for scope in scope_rows
            if scope['dataset'] == 'anchor_directory'
            and scope['status'] == 'success'
        }
        stat_keys = {
            str(scope['guild_executor_key'] or '')
            for scope in scope_rows
            if scope['dataset'] == 'streamer_stat'
            and scope['status'] == 'success'
            and str(scope['business_date'] or '') == target_date.isoformat()
        }
        reusable_keys = {
            key for key in expected_keys
            if persisted_linky_scope_ready(
                conn,
                executor_key=key,
                target_date=target_date,
            )
        }
        if (
            anchor_keys == expected_keys
            and stat_keys == expected_keys
            and reusable_keys == expected_keys
        ):
            return {
                'run_id': run_id,
                'guild_count': len(expected_keys),
                'profile_count': int(row['profile_count'] or 0),
                'revenue_count': int(row['revenue_count'] or 0),
                'updated_at': str(row['updated_at'] or ''),
            }
    return None


def _linky_scope_coverage(
    conn: sqlite3.Connection,
    *,
    target_date: date,
) -> dict[str, Any]:
    executors = conn.execute(
        """
        SELECT guild_name, cms_guild_sid, cms_guild_id
        FROM guild_executors
        WHERE enabled=1 AND lower(app_name)='linky'
        ORDER BY guild_name
        """
    ).fetchall()
    expected = {
        _executor_key_from_row(row): str(row['guild_name'] or '').strip()
        for row in executors
    }
    target = target_date.isoformat()
    scope_rows = conn.execute(
        """
        SELECT s.*, r.updated_at AS run_updated_at
        FROM streamer_ingestion_run_scopes AS s
        JOIN streamer_external_sync_runs AS r ON r.run_id=s.run_id
        WHERE s.app_name='linky'
          AND s.status='success'
          AND (
            (s.dataset='streamer_stat' AND s.business_date=?)
            OR s.dataset='anchor_directory'
          )
        ORDER BY r.updated_at DESC, s.updated_at DESC
        """,
        (target,),
    ).fetchall()
    latest: dict[tuple[str, str], sqlite3.Row] = {}
    for row in scope_rows:
        key = str(row['guild_executor_key'] or '')
        marker = (str(row['dataset'] or ''), key)
        if key in expected and marker not in latest:
            latest[marker] = row
    persisted_keys = {
        key for key in expected
        if persisted_linky_scope_ready(
            conn,
            executor_key=key,
            target_date=target_date,
        )
    }
    covered_keys = {
        key for key in expected
        if ('streamer_stat', key) in latest
        and ('anchor_directory', key) in latest
        and key in persisted_keys
    }
    return {
        'expected': expected,
        'covered_keys': covered_keys,
        'missing_guilds': [expected[key] for key in expected if key not in covered_keys],
        'latest_scopes': latest,
    }


def _record_linky_composite_run(
    conn: sqlite3.Connection,
    *,
    target_date: date,
    coverage: dict[str, Any],
) -> dict[str, Any]:
    expected = dict(coverage.get('expected') or {})
    covered_keys = set(coverage.get('covered_keys') or set())
    if not expected or covered_keys != set(expected):
        raise RuntimeError('linky_composite_coverage_incomplete')
    latest_scopes = dict(coverage.get('latest_scopes') or {})
    target = target_date.isoformat()
    component_run_ids = sorted({
        str(latest_scopes[(dataset, key)]['run_id'])
        for key in expected
        for dataset in ('anchor_directory', 'streamer_stat')
    })
    run_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    profile_count = sum(
        int(latest_scopes[('anchor_directory', key)]['saved_rows'] or 0)
        for key in expected
    )
    revenue_count = sum(
        int(latest_scopes[('streamer_stat', key)]['saved_rows'] or 0)
        for key in expected
    )
    conn.execute(
        """
        INSERT INTO streamer_external_sync_runs(
            run_id,app_name,date_from,date_to,status,run_scope,scope_key,
            guild_count,profile_count,revenue_count,error_code,error_message,
            created_at,updated_at
        ) VALUES(?, 'linky', ?, ?, 'success', 'composite', ?, ?, ?, ?, '', '', ?, ?)
        """,
        (
            run_id, target, target,
            json.dumps({'component_run_ids': component_run_ids}, separators=(',', ':')),
            len(expected), profile_count, revenue_count, now, now,
        ),
    )
    for key, guild_name in expected.items():
        for dataset in ('anchor_directory', 'streamer_stat'):
            source = latest_scopes[(dataset, key)]
            record_ingestion_scope(
                conn,
                run_id=run_id,
                app_name='linky',
                dataset=dataset,
                guild_executor_key=key,
                guild_name=guild_name,
                business_date=target if dataset == 'streamer_stat' else '',
                source_timezone=str(source['source_timezone'] or 'UTC'),
                trigger_type='composite_recovery',
                status='success',
                expected_rows=int(source['expected_rows'] or 0),
                scanned_rows=int(source['scanned_rows'] or 0),
                saved_rows=int(source['saved_rows'] or 0),
                official_income=source['official_income'],
                detail_income=source['detail_income'],
                reconciliation_delta=source['reconciliation_delta'],
                started_at=str(source['started_at'] or now),
                completed_at=now,
            )
    conn.commit()
    return {
        'run_id': run_id,
        'guild_count': len(expected),
        'profile_count': profile_count,
        'revenue_count': revenue_count,
        'updated_at': now,
        'component_run_ids': component_run_ids,
    }


def _resume_linky_missing_guilds(
    conn: sqlite3.Connection,
    *,
    target_date: date,
    trigger_type: str = 'scheduled_recovery',
) -> Optional[dict[str, Any]]:
    coverage = _linky_scope_coverage(conn, target_date=target_date)
    expected = dict(coverage.get('expected') or {})
    covered_keys = set(coverage.get('covered_keys') or set())
    if not expected or not covered_keys:
        return None
    attempted: list[str] = []
    failures: list[dict[str, Any]] = []
    for guild_name in list(coverage.get('missing_guilds') or []):
        attempted.append(guild_name)
        result = sync_streamer_external_data(
            conn,
            app_name='linky',
            start=target_date,
            end=target_date,
            guild_name=guild_name,
            trigger_type=trigger_type,
        )
        if not result.get('ok'):
            failures.append({
                'guild_name': guild_name,
                'error_code': str(result.get('error_code') or 'linky_guild_recovery_failed'),
            })
    refreshed = _linky_scope_coverage(conn, target_date=target_date)
    if set(refreshed.get('covered_keys') or set()) == set(expected):
        evidence = _record_linky_composite_run(
            conn,
            target_date=target_date,
            coverage=refreshed,
        )
        return {
            'ok': True,
            'app': 'linky',
            'status': 'success',
            'source_resumed': True,
            'retried_guilds': attempted,
            **evidence,
        }
    return {
        'ok': False,
        'app': 'linky',
        'status': 'partial',
        'source_resumed': True,
        'retried_guilds': attempted,
        'failed_guilds': failures,
        'missing_guilds': list(refreshed.get('missing_guilds') or []),
        'guild_count': len(refreshed.get('covered_keys') or set()),
        'profile_count': 0,
        'revenue_count': 0,
    }


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Materialize Linky/Sugo streamer daily revenue history.')
    parser.add_argument('--db-path', default=str(ROOT / 'data' / 'automation.db'))
    parser.add_argument('--app', choices=('all', 'linky', 'sugo', 'sogo'), default='all')
    parser.add_argument('--days', type=int, default=1)
    parser.add_argument('--date-from')
    parser.add_argument('--date-to')
    parser.add_argument('--guild', default='')
    parser.add_argument('--fail-on-lock-busy', action='store_true')
    parser.add_argument(
        '--force-source-refresh',
        action='store_true',
        help='Ignore a complete immutable source run and fetch Linky again.',
    )
    parser.add_argument('--progress-path')
    parser.add_argument(
        '--check-source-complete',
        action='store_true',
        help='Read-only completeness probe; do not fetch or materialize.',
    )
    return parser.parse_args()


def _run(args: argparse.Namespace) -> int:
    end = date.fromisoformat(args.date_to) if args.date_to else date.today() - timedelta(days=1)
    start = date.fromisoformat(args.date_from) if args.date_from else end - timedelta(days=max(1, args.days) - 1)
    if start > end:
        raise SystemExit('date_from must not be after date_to')
    apps = ('linky', 'sugo') if args.app == 'all' else (('sugo',) if args.app == 'sogo' else (args.app,))
    target_text = end.isoformat()
    progress_path = (
        Path(args.progress_path)
        if args.progress_path
        else DEFAULT_PROGRESS_DIR / f'{args.app}-{target_text}.json'
    )
    progress = ProgressRecorder(
        progress_path,
        app=args.app,
        target_date=target_text,
    )
    # Enable SQLite URI handling so Linky's incremental materializer can attach
    # the previous analytics store with ``mode=ro`` on this connection.
    conn = connect_short_write_sqlite(
        args.db_path,
        lock_name='sqlite-writer',
        source='streamer-external-source',
        write_window_timeout_seconds=120.0,
        uri=True,
    )
    conn.row_factory = sqlite3.Row
    try:
        results = []
        for app in apps:
            source_evidence = (
                _complete_linky_source_run(conn, target_date=end)
                if app == 'linky'
                and not args.guild
                and start == end
                and not args.force_source_refresh
                else None
            )
            if source_evidence:
                progress.phase('app.linky.source_reused')
                results.append({
                    'ok': True,
                    'app': 'linky',
                    'status': 'source_reused',
                    'source_reused': True,
                    **source_evidence,
                })
                continue
            resumed = (
                _resume_linky_missing_guilds(conn, target_date=end)
                if app == 'linky'
                and not args.guild
                and start == end
                and not args.force_source_refresh
                else None
            )
            if resumed is not None:
                progress.phase('app.linky.source_resume.done')
                results.append(resumed)
                continue
            progress.phase(f'app.{app}.source_sync.start')
            result = sync_streamer_external_data(
                conn, app_name=app, start=start, end=end,
                guild_name=args.guild if app == 'linky' else '',
                trigger_type='scheduled',
            )
            progress.phase(f'app.{app}.source_sync.done')
            results.append(result)
        successful_apps = tuple(result['app'] for result in results if result.get('ok'))
        if successful_apps:
            if successful_apps == ('linky',) and not _linky_candidate_build_admitted():
                progress.finish(
                    'deferred',
                    source_ready=True,
                    reason='candidate_build_resource_deferred',
                )
                return 75
            phase_unit = {
                'linky': 'mcn-linky-external-feed.service',
                'sugo': 'mcn-sugo-external-feed.service',
                'sogo': 'mcn-sugo-external-feed.service',
            }.get(args.app)
            if phase_unit:
                handoff_network_phase(phase_unit)
                progress.phase(f'app.{args.app}.resource_phase.candidate_build')
            if (
                successful_apps == ('linky',)
                and len(results) == 1
                and results[0].get('source_reused')
            ):
                conn.close()
                conn = _open_read_only_source(args.db_path)
                progress.phase('app.linky.source_readonly_reopened')
            analytics = materialize_streamer_analytics_tables(
                conn,
                app_names=successful_apps,
                include_timo_cohorts=False,
                validate_source_schema_only=True,
                refresh_support_tables=(
                    LINKY_STREAMER_ANALYTICS_SUPPORT_TABLES
                    if successful_apps == ('linky',)
                    else True
                ),
                phase_logger=progress.phase,
            )
        else:
            analytics = {'ok': False, 'apps': {}, 'error': 'no_successful_source_sync'}
    except Exception as exc:
        progress.finish(
            'failed',
            error_code=type(exc).__name__,
            error_message=str(exc)[:500],
        )
        raise
    finally:
        conn.close()
    payload = {
        'date_from': start.isoformat(),
        'date_to': end.isoformat(),
        'results': results,
        'analytics': analytics,
        'progress_path': str(progress_path),
    }
    succeeded = (
        all(result.get('ok') for result in results)
        and analytics.get('ok')
    )
    progress.finish(
        'succeeded' if succeeded else 'failed',
        source_reused=any(result.get('source_reused') for result in results),
        analytics_ok=bool(analytics.get('ok')),
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if succeeded else 1


def main() -> int:
    args = _args()
    if args.check_source_complete:
        target = date.fromisoformat(
            args.date_to or (date.today() - timedelta(days=1)).isoformat()
        )
        db_path = Path(args.db_path).resolve()
        conn = sqlite3.connect(
            f'file:{db_path}?mode=ro',
            uri=True,
        )
        conn.row_factory = sqlite3.Row
        try:
            evidence = _complete_linky_source_run(
                conn,
                target_date=target,
            )
        finally:
            conn.close()
        print(json.dumps({
            'ok': evidence is not None,
            'app': 'linky',
            'target_date': target.isoformat(),
            'source': evidence,
        }, ensure_ascii=False, sort_keys=True))
        return 0 if evidence is not None else 1
    assert_managed_batch_runtime(
        'streamer_external_feed',
        required_slice='mcn-batch-linky.slice' if args.app in {'all', 'linky'} else 'mcn-batch.slice',
    )
    return _run(args)


if __name__ == '__main__':
    raise SystemExit(main())
