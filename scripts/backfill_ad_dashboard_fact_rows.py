#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import io
import os
import shlex
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.sqlite_job_lock import JobLockBusy, acquire_sqlite_job_lock, print_job_lock_skip
from app.batch_runtime import assert_managed_batch_runtime
from app.sqlite_observability import connect_observed_sqlite
from app.ad_dashboard_repository import (
    _ad_materialize_fact_rows,
    ad_dashboard_fact_rows_completeness,
    mark_ad_dashboard_sync_state,
    replace_ad_dashboard_fact_rows_for_dates,
)
from app.sqlite_write_queue import (
    SQLiteWriteQueueError,
    _exact_meta_write_readback,
    _qualified_join_write_readback,
    db_writer_enabled,
    db_writer_required,
    submit_sqlite_write_job,
)
try:
    from mcn_phase_resource_handoff import handoff_network_phase
except ModuleNotFoundError:
    def handoff_network_phase(dependency_unit: str) -> Dict[str, Any]:
        if REPO_ROOT.resolve() == Path('/opt/mcn-ai-automation'):
            raise RuntimeError('mcn_phase_resource_handoff_missing_in_production')
        return {'ok': True, 'changed': False, 'skipped': 'non_production_checkout'}


DEFAULT_DB_PATH = REPO_ROOT / 'data' / 'automation.db'
DEFAULT_ENV_PATHS = (
    Path.home() / '.hermes' / '.env',
    REPO_ROOT / 'data' / 'appsflyer.env',
    REPO_ROOT / 'data' / 'meta_ads.env',
    REPO_ROOT / 'data' / 'bind_success.env',
)


@contextlib.contextmanager
def _direct_write_lock_if_needed():
    lock = None
    if db_writer_enabled():
        # Recommendation persistence runs after the fact writer completes, but
        # another governed batch may briefly own the shared writer lane. Wait
        # for that bounded hand-off instead of turning an otherwise successful
        # dashboard refresh into an immediate sqlite_job_lock_busy failure.
        lock = acquire_sqlite_job_lock('sqlite-writer', timeout_seconds=60.0)
    try:
        yield
    finally:
        if lock is not None:
            lock.release()

def _connect_sqlite(db_path: Path) -> sqlite3.Connection:
    conn = connect_observed_sqlite(str(db_path), source='scripts.backfill_ad_dashboard_fact_rows', timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=60000')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        if line.startswith('export '):
            line = line[len('export '):].strip()
        key, value = line.split('=', 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        try:
            parts = shlex.split(value, posix=True)
            value = parts[0] if len(parts) == 1 else value
        except ValueError:
            value = value.strip('"').strip("'")
        os.environ.setdefault(key, value)


def _latest_complete_utc_date() -> date:
    current_bj = datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
    cutoff_bj = current_bj.replace(hour=9, minute=20, second=0, microsecond=0)
    return current_bj.date() - timedelta(days=1 if current_bj >= cutoff_bj else 2)


def _latest_complete_london_date() -> date:
    return datetime.now(timezone.utc).astimezone(ZoneInfo('Europe/London')).date() - timedelta(days=1)


def _parse_date(value: str) -> date:
    return datetime.fromisoformat(str(value).strip()).date()


def _parse_path_list(value: str) -> List[str]:
    paths: List[str] = []
    for part in str(value or '').split(','):
        item = part.strip()
        if item:
            paths.append(item)
    return paths


def _cached_history_start(db_path: Path) -> Optional[date]:
    if not db_path.exists():
        return None
    try:
        conn = _connect_sqlite(db_path)
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ad_dashboard_snapshot_cache'"
        ).fetchone()
        if not table:
            return None
        rows = conn.execute(
            "SELECT json_extract(payload_json, '$.date_start') FROM ad_dashboard_snapshot_cache"
        ).fetchall()
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    dates = []
    for row in rows or []:
        raw = str(row[0] or '').strip()
        if not raw:
            continue
        try:
            dates.append(_parse_date(raw))
        except Exception:
            continue
    return min(dates) if dates else None


def _config_value(name: str, default: str = '') -> str:
    return str(os.getenv(name) or default).strip()


def _build_snapshot(*, start_date: date, end_date: date) -> Dict[str, Any]:
    os.environ.setdefault('HERMES_QUIET', '1')
    sys.path.insert(0, str(REPO_ROOT))
    with contextlib.redirect_stdout(io.StringIO()):
        from app.main import (  # noqa: PLC0415
            _normalize_meta_api_version,
            _parse_config_list,
            build_ad_data_dashboard_snapshot,
        )

    marketing_env_keys = (
        'MARKETING_DIAGNOSTICS_API_TOKEN',
        'BI_MARKETING_DIAGNOSTICS_API_TOKEN',
    )
    preserved_marketing_env = {key: os.environ.pop(key, None) for key in marketing_env_keys}
    try:
        return build_ad_data_dashboard_snapshot(
            token=_config_value('APPSFLYER_API_TOKEN'),
            app_ids=_parse_config_list(
                os.getenv('APPSFLYER_APP_IDS') or os.getenv('APPSFLYER_APP_ID') or ''
            ),
            timezone_name=_config_value('AD_DASHBOARD_TIMEZONE', 'UTC'),
            base_url=_config_value('APPSFLYER_BASE_URL', 'https://hq1.appsflyer.com'),
            meta_token=_config_value('META_ADS_ACCESS_TOKEN'),
            meta_ad_account_ids=_parse_config_list(
                os.getenv('META_ADS_ACCOUNT_IDS') or os.getenv('META_ADS_ACCOUNT_ID') or ''
            ),
            meta_api_version=_normalize_meta_api_version(_config_value('META_ADS_API_VERSION', 'v25.0')),
            meta_base_url=_config_value('META_ADS_BASE_URL', 'https://graph.facebook.com'),
            bind_success_token=(
                _config_value('BIND_SUCCESS_TOKEN')
                or _config_value('BIND_SUCCESS_API_TOKEN')
                or _config_value('BI_BIND_SUCCESS_API_TOKEN')
            ),
            bind_success_base_url=(
                _config_value('BIND_SUCCESS_BASE_URL')
                or _config_value('BIND_SUCCESS_API_BASE_URL')
                or _config_value('BI_BIND_SUCCESS_API_BASE_URL')
                or 'https://servertest.timetrade.club'
            ),
            bind_success_project=(
                _config_value('BIND_SUCCESS_PROJECT')
                or _config_value('BI_BIND_SUCCESS_PROJECT')
                or 'TUGAO'
            ),
            days=max((end_date - start_date).days + 1, 1),
            date_from=start_date.isoformat(),
            date_to=end_date.isoformat(),
            top_limit=25,
            include_fact_rows=True,
        )
    finally:
        for key, value in preserved_marketing_env.items():
            if value is not None:
                os.environ[key] = value


def _fact_rows_in_window(snapshot: Dict[str, Any], *, start_date: date, end_date: date) -> List[Dict[str, Any]]:
    return [
        row for row in list(snapshot.pop('_fact_rows', []) or [])
        if start_date <= _parse_date(str((row or {}).get('date') or end_date.isoformat())) <= end_date
    ]


def _build_fact_rows_payload(
    *,
    start_date: date,
    end_date: date,
    tugao_report_paths: List[str],
) -> Dict[str, Any]:
    snapshot = _build_snapshot(start_date=start_date, end_date=end_date)
    fact_rows = _fact_rows_in_window(snapshot, start_date=start_date, end_date=end_date)
    tugao_api_result = _load_tugao_funnel_api_rows(
        start_date=start_date,
        end_date=end_date,
    )
    tugao_api_rows = list(tugao_api_result.get('rows') or [])
    if str(tugao_api_result.get('status') or '') != 'ok' or not tugao_api_rows:
        raise RuntimeError('tugao_funnel_not_ready')
    marketing_result = {
        'rows': [],
        'status': 'skipped_tugao_funnel_available',
        'pages': 0,
        'raw_row_count': 0,
        'datasets': {},
        'error': '',
    }
    marketing_rows = list(marketing_result.get('rows') or [])
    if tugao_api_rows:
        fact_rows = [
            row for row in fact_rows
            if str((row or {}).get('data_source') or '').strip().lower() not in {
                'bindsuccess',
                'marketingdiagnostics',
                'marketing_diagnostics',
            }
        ]
    tugao_daily_rows: List[Dict[str, Any]] = []
    fact_rows.extend(tugao_api_rows)
    return {
        'snapshot': snapshot,
        'fact_rows': fact_rows,
        'tugao_api_result': tugao_api_result,
        'tugao_api_rows': tugao_api_rows,
        'marketing_result': marketing_result,
        'marketing_rows': marketing_rows,
        'tugao_daily_rows': tugao_daily_rows,
    }


def _fact_completeness(rows: List[Dict[str, Any]], *, start_date: date, end_date: date, appsflyer_required: Optional[bool] = None) -> Dict[str, Any]:
    return ad_dashboard_fact_rows_completeness(
        _ad_materialize_fact_rows(rows),
        start_date=start_date,
        end_date=end_date,
        appsflyer_required=bool(_config_value('APPSFLYER_API_TOKEN')) if appsflyer_required is None else bool(appsflyer_required),
        tugao_funnel_required=True,
    )


def _missing_appsflyer(completeness: Dict[str, Any]) -> bool:
    return bool(completeness.get('missing_appsflyer')) or 'missing_appsflyer=' in str(completeness.get('error_message') or '')


def _store_fact_rows(
    db_path: Path,
    rows: list[Dict[str, Any]],
    *,
    appsflyer_required: Optional[bool] = None,
    source: str = 'all',
) -> Dict[str, Any]:
    from app.schema_migrations import apply_schema_migration_registry  # noqa: PLC0415

    fact_dates = []
    for row in rows:
        raw_date = str((row or {}).get('date') or '').strip()
        if not raw_date:
            continue
        try:
            fact_dates.append(_parse_date(raw_date))
        except Exception:
            continue
    if not fact_dates:
        return {'stored_rows': 0, 'date_start': None, 'date_end': None}
    db_path.parent.mkdir(parents=True, exist_ok=True)
    synced_at = datetime.now(timezone.utc).isoformat()
    if db_writer_enabled():
        job = {
            'type': 'ad_dashboard_fact_replace',
            'db_path': str(db_path),
            'rows': rows,
            'start_date': min(fact_dates).isoformat(),
            'end_date': max(fact_dates).isoformat(),
            'synced_at': synced_at,
            'source': str(source or 'all'),
            'appsflyer_required': bool(_config_value('APPSFLYER_API_TOKEN')) if appsflyer_required is None else bool(appsflyer_required),
            'tugao_funnel_required': True,
        }
        try:
            result = submit_sqlite_write_job(job, timeout=float(os.getenv('MCN_DB_WRITER_TIMEOUT_SECONDS') or '180'))
            return {
                'stored_rows': int(result.get('stored_rows') or 0),
                'date_start': str(result.get('date_start') or min(fact_dates).isoformat()),
                'date_end': str(result.get('date_end') or max(fact_dates).isoformat()),
                'sync_status': str(result.get('sync_status') or 'partial'),
                'sync_error_message': str(result.get('sync_error_message') or ''),
                'qualified_join_readback': dict(result.get('qualified_join_readback') or {}),
                'exact_meta_readback': dict(result.get('exact_meta_readback') or {}),
                'write_source': 'mcn-db-writer',
            }
        except SQLiteWriteQueueError as exc:
            if db_writer_required():
                raise
            print({
                'warning': 'mcn_db_writer_fallback_direct',
                'error': str(exc)[:200],
            }, flush=True)
    with _direct_write_lock_if_needed():
        conn = _connect_sqlite(db_path)
        try:
            apply_schema_migration_registry(conn)
            stored_count = replace_ad_dashboard_fact_rows_for_dates(
                conn,
                rows,
                start_date=min(fact_dates),
                end_date=max(fact_dates),
                synced_at=synced_at,
                tugao_funnel_required=True,
            )
            fact_completeness = ad_dashboard_fact_rows_completeness(
                _ad_materialize_fact_rows(rows),
                start_date=min(fact_dates),
                end_date=max(fact_dates),
                appsflyer_required=bool(_config_value('APPSFLYER_API_TOKEN')) if appsflyer_required is None else bool(appsflyer_required),
                tugao_funnel_required=True,
            )
            qualified_join_readback = _qualified_join_write_readback(
                conn,
                rows,
                start_date=min(fact_dates),
                end_date=max(fact_dates),
            )
            exact_meta_readback = _exact_meta_write_readback(conn, rows)
            mark_ad_dashboard_sync_state(
                conn,
                source=str(source or 'all'),
                start_date=min(fact_dates),
                end_date=max(fact_dates),
                status=str(fact_completeness.get('status') or 'partial'),
                row_count=stored_count,
                error_message=str(fact_completeness.get('error_message') or ''),
                synced_at=synced_at,
            )
            conn.commit()
        finally:
            conn.close()
    return {
        'stored_rows': stored_count,
        'date_start': min(fact_dates).isoformat(),
        'date_end': max(fact_dates).isoformat(),
        'sync_status': str(fact_completeness.get('status') or 'partial'),
        'sync_error_message': str(fact_completeness.get('error_message') or ''),
        'write_source': 'direct',
        'qualified_join_readback': qualified_join_readback,
        'exact_meta_readback': exact_meta_readback,
    }


def _completion_watermark(db_path: Path, target_date: date) -> Dict[str, Any]:
    conn = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=5.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=5000')
    try:
        sync_row = conn.execute(
            "SELECT status,row_count,error_message,updated_at FROM ad_dashboard_sync_state "
            "WHERE source='all' AND date=?",
            (target_date.isoformat(),),
        ).fetchone()
        fact_count = int(conn.execute(
            "SELECT COUNT(*) FROM ad_dashboard_fact_rows WHERE date=?",
            (target_date.isoformat(),),
        ).fetchone()[0])
    finally:
        conn.close()
    status = str(sync_row['status'] or '') if sync_row else ''
    declared_rows = int(sync_row['row_count'] or 0) if sync_row else 0
    return {
        'ok': bool(sync_row) and status == 'ok' and declared_rows > 0 and fact_count > 0,
        'target_date': target_date.isoformat(),
        'sync_status': status,
        'declared_rows': declared_rows,
        'fact_rows': fact_count,
        'error_message': str(sync_row['error_message'] or '') if sync_row else 'sync_state_missing',
        'updated_at': str(sync_row['updated_at'] or '') if sync_row else '',
    }

def _load_tugao_daily_report_rows(
    report_paths: List[str],
    *,
    start_date: date,
    end_date: date,
) -> List[Dict[str, Any]]:
    if not report_paths:
        return []
    sys.path.insert(0, str(REPO_ROOT))
    with contextlib.redirect_stdout(io.StringIO()):
        from app.tugao_daily_funnel_report import load_tugao_daily_funnel_report_rows  # noqa: PLC0415

    return load_tugao_daily_funnel_report_rows(
        report_paths,
        start_date=start_date,
        end_date=end_date,
    )


def _load_tugao_funnel_api_rows(
    *,
    start_date: date,
    end_date: date,
) -> Dict[str, Any]:
    token = (
        _config_value('TUGAO_FUNNEL_API_TOKEN')
        or _config_value('TIMETRADE_BI_TOKEN')
        or _config_value('BI_API_TOKEN')
        or _config_value('BI_BIND_SUCCESS_API_TOKEN')
        or _config_value('BIND_SUCCESS_API_TOKEN')
    )
    if not token:
        return {'rows': [], 'status': 'not_configured', 'pages': 0, 'raw_row_count': 0, 'error': ''}
    sys.path.insert(0, str(REPO_ROOT))
    with contextlib.redirect_stdout(io.StringIO()):
        from app.tugao_funnel_api import (  # noqa: PLC0415
            DEFAULT_TUGAO_FUNNEL_API_URL,
            TugaoFunnelDailyMetricsClient,
        )
    page_size = 1000
    try:
        page_size = int(_config_value('TUGAO_FUNNEL_API_PAGE_SIZE', '1000') or 1000)
    except ValueError:
        page_size = 1000
    client = TugaoFunnelDailyMetricsClient(
        token=token,
        base_url=_config_value('TUGAO_FUNNEL_API_BASE_URL', DEFAULT_TUGAO_FUNNEL_API_URL),
        auth_header=_config_value('TUGAO_FUNNEL_API_AUTH_HEADER', 'authorization'),
        page_size=page_size,
    )
    try:
        result = client.fetch(start_date=start_date, end_date=end_date)
    except Exception as exc:
        return {
            'rows': [],
            'status': 'error',
            'pages': 0,
            'raw_row_count': 0,
            'error': str(exc.__class__.__name__),
        }
    return {
        'rows': result.rows,
        'status': 'ok',
        'pages': result.pages,
        'raw_row_count': result.raw_row_count,
        'error': '',
    }


def _load_marketing_diagnostics_rows(
    *,
    start_date: date,
    end_date: date,
) -> Dict[str, Any]:
    token = (
        _config_value('MARKETING_DIAGNOSTICS_API_TOKEN')
        or _config_value('BI_MARKETING_DIAGNOSTICS_API_TOKEN')
        or _config_value('TIMETRADE_MARKETING_DIAGNOSTICS_API_TOKEN')
    )
    if not token:
        return {'rows': [], 'status': 'not_configured', 'pages': 0, 'raw_row_count': 0, 'datasets': {}, 'error': ''}
    sys.path.insert(0, str(REPO_ROOT))
    with contextlib.redirect_stdout(io.StringIO()):
        from app.marketing_diagnostics_api import (  # noqa: PLC0415
            DEFAULT_MARKETING_DIAGNOSTICS_DAILY_URL,
            MarketingDiagnosticsDailyClient,
        )
    page_size = 500
    try:
        page_size = int(_config_value('MARKETING_DIAGNOSTICS_API_PAGE_SIZE', '500') or 500)
    except ValueError:
        page_size = 500
    client = MarketingDiagnosticsDailyClient(
        token=token,
        base_url=(
            _config_value('MARKETING_DIAGNOSTICS_DAILY_API_BASE_URL')
            or _config_value('BI_MARKETING_DIAGNOSTICS_DAILY_API_BASE_URL')
            or DEFAULT_MARKETING_DIAGNOSTICS_DAILY_URL
        ),
        page_size=page_size,
    )
    try:
        result = client.fetch(
            start_date=start_date,
            end_date=end_date,
            datasets=['ad_daily', 'natural_im_funnel_daily'],
        )
    except Exception as exc:
        return {
            'rows': [],
            'status': 'error',
            'pages': 0,
            'raw_row_count': 0,
            'datasets': {},
            'error': str(exc.__class__.__name__),
        }
    return {
        'rows': result.rows,
        'status': 'ok',
        'pages': result.pages,
        'raw_row_count': result.raw_row_count,
        'datasets': result.datasets,
        'error': '',
    }


def _build_snapshot_from_fact_rows(
    rows: List[Dict[str, Any]],
    *,
    start_date: date,
    end_date: date,
) -> Dict[str, Any]:
    sys.path.insert(0, str(REPO_ROOT))
    with contextlib.redirect_stdout(io.StringIO()):
        from app.main import build_ad_data_dashboard_snapshot_from_rows  # noqa: PLC0415

    return build_ad_data_dashboard_snapshot_from_rows(
        rows,
        timezone_name=_config_value('AD_DASHBOARD_TIMEZONE', 'UTC'),
        days=max((end_date - start_date).days + 1, 1),
        date_from=start_date.isoformat(),
        date_to=end_date.isoformat(),
        top_limit=25,
        sources={
            'marketing_diagnostics': {
                'configured': True,
                'row_count': len(rows),
                'label': 'Marketing Diagnostics',
            },
        },
        insights=['TimeTrade marketing-diagnostics daily 已作为本窗口主事实行。'],
    )


def _persist_daily_recommendation_report(
    db_path: Path,
    *,
    snapshot: Dict[str, Any],
    report_date: date,
) -> Dict[str, Any]:
    sys.path.insert(0, str(REPO_ROOT))
    with contextlib.redirect_stdout(io.StringIO()):
        from app.ad_creative_intelligence import build_creative_intelligence_payload  # noqa: PLC0415
        from app.ad_daily_report import (  # noqa: PLC0415
            TugaoRealConversionProvider,
            build_daily_report_from_dashboard_snapshot,
            persist_daily_report,
            report_from_dict,
            report_to_dict,
        )
    from app.schema_migrations import apply_schema_migration_registry  # noqa: PLC0415

    with _direct_write_lock_if_needed():
        conn = _connect_sqlite(db_path)
        try:
            apply_schema_migration_registry(conn)
            report = build_daily_report_from_dashboard_snapshot(
                snapshot,
                report_date=report_date.isoformat(),
                data_mode='real',
                provider=TugaoRealConversionProvider(db_path=str(db_path)),
                project=(
                    _config_value('BIND_SUCCESS_PROJECT')
                    or _config_value('BI_BIND_SUCCESS_PROJECT')
                    or 'TUGAO'
                ),
            )
            report = report_from_dict({
                **report_to_dict(report),
                'creative_insights': build_creative_intelligence_payload(report, conn=conn),
            })
            persist_daily_report(conn, report)
            return {
                'daily_report_id': report.report_id,
                'daily_report_date': report.report_date,
                'daily_recommendations': len(report.recommendations),
            }
        finally:
            conn.close()

def main() -> int:
    parser = argparse.ArgumentParser(description='Backfill local ad dashboard fact rows.')
    parser.add_argument('--db-path', default=str(DEFAULT_DB_PATH))
    parser.add_argument('--start-date', default='')
    parser.add_argument('--end-date', default='')
    parser.add_argument('--latest-complete-only', action='store_true')
    parser.add_argument(
        '--tugao-daily-report',
        action='append',
        default=[],
        help='Path to downloaded TimeTrade/TUGAO daily xlsx report. Can be repeated or comma-separated.',
    )
    parser.add_argument('--retry-missing-appsflyer', type=int, default=2)
    parser.add_argument('--retry-delay-seconds', type=int, default=600)
    parser.add_argument(
        '--tugao-reconcile-days',
        type=int,
        default=30,
        help='Re-read and idempotently merge this trailing Tugao window on every run.',
    )
    args = parser.parse_args()

    assert_managed_batch_runtime('ad_dashboard_backfill', required_slice='mcn-batch.slice')
    for env_path in DEFAULT_ENV_PATHS:
        _load_env_file(env_path)

    db_path = Path(args.db_path).expanduser()
    end_date = _parse_date(args.end_date) if args.end_date else _latest_complete_utc_date()
    start_date = end_date if args.latest_complete_only else (
        _parse_date(args.start_date)
        if args.start_date
        else (_cached_history_start(db_path) or end_date)
    )
    if start_date > end_date:
        start_date = end_date

    tugao_report_paths: List[str] = []
    for raw in args.tugao_daily_report or []:
        tugao_report_paths.extend(_parse_path_list(raw))
    tugao_report_paths.extend(_parse_path_list(os.getenv('TUGAO_DAILY_REPORT_PATHS') or ''))

    attempts = max(1, int(args.retry_missing_appsflyer or 0) + 1)
    retry_delay = max(0, int(args.retry_delay_seconds or 0))
    payload: Dict[str, Any] = {}
    completeness: Dict[str, Any] = {}
    retries_used = 0
    for attempt in range(attempts):
        payload = _build_fact_rows_payload(
            start_date=start_date,
            end_date=end_date,
            tugao_report_paths=tugao_report_paths,
        )
        marketing_rows = list(payload.get('marketing_rows') or [])
        completeness = _fact_completeness(
            list(payload.get('fact_rows') or []),
            start_date=start_date,
            end_date=end_date,
            appsflyer_required=False if marketing_rows else None,
        )
        if not _missing_appsflyer(completeness) or attempt >= attempts - 1:
            retries_used = attempt
            break
        retries_used = attempt + 1
        print({
            'retry': retries_used,
            'reason': str(completeness.get('error_message') or 'missing_appsflyer'),
            'delay_seconds': retry_delay,
        }, flush=True)
        if retry_delay:
            time.sleep(retry_delay)

    snapshot = dict(payload.get('snapshot') or {})
    fact_rows = list(payload.get('fact_rows') or [])
    tugao_api_result = dict(payload.get('tugao_api_result') or {})
    tugao_api_rows = list(payload.get('tugao_api_rows') or [])
    marketing_result = dict(payload.get('marketing_result') or {})
    marketing_rows = list(payload.get('marketing_rows') or [])
    tugao_daily_rows = list(payload.get('tugao_daily_rows') or [])
    tugao_reconcile_days = max(int(args.tugao_reconcile_days or 0), 30)
    tugao_reconcile_start = end_date - timedelta(days=tugao_reconcile_days - 1)
    tugao_reconcile_result = _load_tugao_funnel_api_rows(
        start_date=tugao_reconcile_start,
        end_date=end_date,
    )
    tugao_reconcile_rows = list(tugao_reconcile_result.get('rows') or [])
    tugao_reconcile_completeness = _fact_completeness(
        tugao_reconcile_rows,
        start_date=tugao_reconcile_start,
        end_date=end_date,
        appsflyer_required=False,
    )
    if (
        str(tugao_reconcile_result.get('status') or '') != 'ok'
        or not tugao_reconcile_rows
        or not tugao_reconcile_completeness.get('complete')
    ):
        raise RuntimeError(
            'tugao_funnel_reconcile_not_ready:'
            + str(tugao_reconcile_completeness.get('error_message') or tugao_reconcile_result.get('status') or 'empty')
        )
    handoff_network_phase('mcn-ad-dashboard-daily-backfill.service')
    stored = _store_fact_rows(db_path, fact_rows, appsflyer_required=False if marketing_rows else None)
    tugao_reconcile_stored = _store_fact_rows(
        db_path,
        tugao_reconcile_rows,
        appsflyer_required=False,
        source='tugao_funnel',
    )
    completion_watermark = _completion_watermark(db_path, end_date)
    if not completion_watermark['ok']:
        print({
            'ok': False,
            'reason': 'ad_dashboard_completion_watermark_missing',
            'requested_start': start_date.isoformat(),
            'requested_end': end_date.isoformat(),
            **stored,
            'completion_watermark': completion_watermark,
        }, flush=True)
        return 75
    if marketing_rows:
        snapshot = _build_snapshot_from_fact_rows(
            fact_rows,
            start_date=start_date,
            end_date=end_date,
        )
    report_result = _persist_daily_recommendation_report(
        db_path,
        snapshot=snapshot,
        report_date=end_date,
    )
    print({
        'ok': True,
        'requested_start': start_date.isoformat(),
        'requested_end': end_date.isoformat(),
        'fetched_rows': snapshot.get('fetched_row_count'),
        'visible_rows': snapshot.get('row_count'),
        'marketing_diagnostics_status': marketing_result.get('status'),
        'marketing_diagnostics_pages': marketing_result.get('pages'),
        'marketing_diagnostics_raw_rows': marketing_result.get('raw_row_count'),
        'marketing_diagnostics_datasets': marketing_result.get('datasets'),
        'tugao_funnel_api_status': tugao_api_result.get('status'),
        'tugao_funnel_api_rows': len(tugao_api_rows),
        'tugao_funnel_api_pages': tugao_api_result.get('pages'),
        'tugao_daily_report_rows': len(tugao_daily_rows),
        'tugao_reconcile_days': tugao_reconcile_days,
        'tugao_reconcile_rows': len(tugao_reconcile_rows),
        'tugao_reconcile_stored': tugao_reconcile_stored,
        'missing_appsflyer_retries_used': retries_used,
        **stored,
        'completion_watermark': completion_watermark,
        **report_result,
        'errors': len(snapshot.get('errors') or []),
    })
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
