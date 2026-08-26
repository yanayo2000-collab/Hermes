#!/usr/bin/env python3
"""Import one operator-supplied official Timo daily revenue snapshot fail-closed."""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault('MCN_PROCESS_ROLE', 'timo-official-revenue-import')
os.environ.setdefault('MCN_DISABLE_GLOBAL_APP_BOOTSTRAP', '1')

from app.batch_runtime import assert_managed_batch_runtime  # noqa: E402
from app.sqlite_job_lock import JobLockBusy, acquire_sqlite_job_lock, print_job_lock_skip  # noqa: E402
from app.streamer_analytics import materialize_streamer_analytics_tables  # noqa: E402
from app.streamer_data_foundation import archive_raw_bytes  # noqa: E402
from app.timo_guild_identity import require_timo_guild_identity  # noqa: E402
from app.timo_incremental_materialization import materialize_timo_revenue_snapshot  # noqa: E402
from app.timo_official_verification import build_official_verification_evidence  # noqa: E402


REVENUE_FIELDS = {
    'total_income': '1v1总收益',
    'qualified_revenue': '本周1v1主播达标收益',
    'matching_income': '匹配通话收益',
    'private_message_income': '私信消息收益',
    'private_gift_income': '私信礼物收益',
    'call_income': '1v1通话收益',
    'quality_revenue': '优质主播特定场景收益',
}
REQUIRED_HEADERS = {
    '主播昵称', '用户id', '公会id', '公会群名称', '主播注册时间',
    '主播身份', '在线时长(单位：h）', '通话数', '优质主播', *REVENUE_FIELDS.values(),
}
DATE_PATTERN = re.compile(r'(?P<start>\d{8})-(?P<end>\d{8})')
COUNTRY_TIMEZONE = {'Mexico': 'America/Mexico_City'}
MONEY_QUANTUM = Decimal('0.000001')


def _money(value: Any) -> Decimal:
    """Normalize SQLite REAL aggregates to the publication's six-decimal contract."""
    return Decimal(str(value or 0)).quantize(MONEY_QUANTUM)


def _decimal(raw: Any, *, field: str, row_number: int) -> Decimal:
    text = str(raw or '').strip().replace(',', '')
    if not text:
        return Decimal('0')
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f'invalid_decimal:{field}:row={row_number}') from exc
    if not value.is_finite():
        raise ValueError(f'non_finite_decimal:{field}:row={row_number}')
    return value


def _bool_flag(raw: Any) -> int:
    return int(str(raw or '').strip().casefold() in {'1', 'true', 'yes', 'y', '是'})


def _filename_date(path: Path) -> str:
    match = DATE_PATTERN.search(path.name)
    if not match or match.group('start') != match.group('end'):
        raise ValueError('source_filename_daily_date_missing')
    return datetime.strptime(match.group('start'), '%Y%m%d').date().isoformat()


def parse_official_snapshot(
    content: bytes,
    *,
    source_name: str,
    business_date: str,
    guild_id: str,
    country: str,
) -> dict[str, Any]:
    if content.startswith(b'PK\x03\x04'):
        raise ValueError('official_snapshot_true_xlsx_not_supported_by_this_importer')
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('official_snapshot_not_utf8_csv') from exc
    reader = csv.DictReader(io.StringIO(text, newline=''))
    headers = {str(item or '').strip() for item in (reader.fieldnames or [])}
    missing = sorted(REQUIRED_HEADERS - headers)
    if missing:
        raise ValueError('official_snapshot_required_headers_missing:' + ','.join(missing[:5]))

    cutoff = datetime.combine(
        date.fromisoformat(business_date.replace('/', '-')) + timedelta(days=1),
        time.min,
        tzinfo=ZoneInfo(COUNTRY_TIMEZONE[country]),
    ).astimezone(ZoneInfo('Asia/Shanghai'))
    all_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    source_rows = 0
    source_guild_names: set[str] = set()
    total_income = Decimal('0')
    for row_number, source in enumerate(reader, start=2):
        source_rows += 1
        observed_guild_id = str(source.get('公会id') or '').strip()
        if observed_guild_id != guild_id:
            raise ValueError(f'official_snapshot_guild_mismatch:row={row_number}')
        timo_id = str(source.get('用户id') or '').strip()
        if not timo_id:
            raise ValueError(f'official_snapshot_blank_streamer_id:row={row_number}')
        if timo_id in all_ids:
            raise ValueError(f'official_snapshot_duplicate_streamer_id:{timo_id}')
        all_ids.add(timo_id)
        source_guild_names.add(str(source.get('公会群名称') or '').strip())
        amounts = {
            target: _decimal(source.get(header), field=header, row_number=row_number)
            for target, header in REVENUE_FIELDS.items()
        }
        if any(value < 0 for value in amounts.values()):
            raise ValueError(f'official_snapshot_negative_income:row={row_number}')
        has_income = any(value != 0 for value in amounts.values())
        if not has_income:
            continue
        registered_at = str(source.get('主播注册时间') or '').strip()
        try:
            registered_bj = datetime.strptime(registered_at[:19], '%Y-%m-%d %H:%M:%S').replace(
                tzinfo=ZoneInfo('Asia/Shanghai')
            )
        except ValueError as exc:
            raise ValueError(f'official_snapshot_registration_time_invalid:row={row_number}') from exc
        if registered_bj >= cutoff:
            raise ValueError(f'official_snapshot_post_period_income:row={row_number}')
        total_income += amounts['total_income']
        rows.append({
            'timo_id': timo_id,
            'user_uuid': '',
            'nick_name': str(source.get('主播昵称') or '').strip(),
            'host_role': str(source.get('主播身份') or '').strip(),
            'joined_guild_at_bj': registered_at,
            'quality_host': _bool_flag(source.get('优质主播')),
            **{key: float(value) for key, value in amounts.items()},
            'online_hours': float(_decimal(source.get('在线时长(单位：h）'), field='在线时长', row_number=row_number)),
            'call_count': int(_decimal(source.get('通话数'), field='通话数', row_number=row_number)),
            'source_payload': json.dumps({
                'source_kind': 'official_android_manual_export',
                'source_file': source_name,
                'source_row_number': row_number,
                'official_fields': source,
            }, ensure_ascii=False, sort_keys=True),
        })
    if not rows:
        raise ValueError('official_snapshot_no_effective_revenue_rows')
    if len(source_guild_names) != 1 or '' in source_guild_names:
        raise ValueError('official_snapshot_guild_name_ambiguous')
    return {
        'rows': rows,
        'source_row_count': source_rows,
        'source_unique_id_count': len(all_ids),
        'effective_row_count': len(rows),
        'total_income': total_income,
        'source_guild_name': next(iter(source_guild_names)),
        'cutoff_bj': cutoff.isoformat(),
    }


def _connect_factory(db_path: Path):
    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=30000')
        return conn
    return connect


def _load_job(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('job_spec_not_object')
    return payload


def _verify_expected(actual: Any, expected: Any, name: str) -> None:
    if str(actual) != str(expected):
        raise ValueError(f'{name}_mismatch:expected={expected}:actual={actual}')


def run(job: dict[str, Any]) -> dict[str, Any]:
    source_path = Path(str(job['source_file'])).resolve()
    db_path = Path(str(job.get('db_path') or ROOT / 'data/automation.db')).resolve()
    business_date = str(job['business_date'])
    guild_id = str(job['guild_id'])
    identity = require_timo_guild_identity(guild_id=guild_id)
    guild_executor_key = f'timo:cms_guild_sid:{identity.guild_sid}'
    if _filename_date(source_path) != business_date:
        raise ValueError('source_filename_business_date_mismatch')
    content = source_path.read_bytes()
    source_sha = hashlib.sha256(content).hexdigest()
    _verify_expected(source_sha, str(job['expected_sha256']).lower(), 'source_sha256')
    parsed = parse_official_snapshot(
        content,
        source_name=source_path.name,
        business_date=business_date,
        guild_id=guild_id,
        country='Mexico',
    )
    _verify_expected(parsed['source_row_count'], int(job['expected_source_row_count']), 'source_row_count')
    _verify_expected(parsed['source_unique_id_count'], int(job['expected_source_row_count']), 'source_unique_id_count')
    _verify_expected(parsed['effective_row_count'], int(job['expected_effective_row_count']), 'effective_row_count')
    _verify_expected(parsed['total_income'], Decimal(str(job['expected_total_income'])), 'total_income')
    official_verification = build_official_verification_evidence(
        mode=str(job.get('verification_mode') or ''),
        business_date=business_date,
        guild_id=guild_id,
        source_sha256=source_sha,
        source_row_count=int(parsed['source_row_count']),
        effective_row_count=int(parsed['effective_row_count']),
        total_income=parsed['total_income'],
    )

    base_sync_id = f"timo_manual_official_{business_date.replace('-', '')}_{guild_id}_{source_sha[:12]}"
    observation_id = max(1, int(job.get('observation_id') or 1))
    sync_id = base_sync_id if observation_id == 1 else f'{base_sync_id}_obs{observation_id}'
    snapshot_at = datetime.now(timezone.utc).isoformat()
    connect = _connect_factory(db_path)
    with connect() as preflight:
        current = preflight.execute(
            """
            SELECT COUNT(*) AS row_count, COALESCE(SUM(total_income),0) AS total_income
            FROM timo_external_revenue_daily
            WHERE guild_executor_key=? AND stat_date_bj=?
            """,
            (guild_executor_key, business_date),
        ).fetchone()
        watermark = preflight.execute(
            """
            SELECT checksum,last_success_sync_id,row_count,total_income,data_status,revision_version
            FROM timo_sync_watermark WHERE guild_executor_key=? AND stat_date_bj=?
            """,
            (guild_executor_key, business_date),
        ).fetchone()
        fact_lineage = preflight.execute(
            """
            SELECT COUNT(DISTINCT last_sync_id) AS sync_ids,MIN(last_sync_id) AS sync_id,
                   MIN(revision_version) AS min_revision,MAX(revision_version) AS max_revision
            FROM timo_external_revenue_daily
            WHERE guild_executor_key=? AND stat_date_bj=?
            """,
            (guild_executor_key, business_date),
        ).fetchone()
        provenance = preflight.execute(
            "SELECT gate_evidence_json FROM timo_sync_run_log WHERE sync_id=? AND status IN ('success','no_op')",
            (base_sync_id,),
        ).fetchone()
    preimage = {
        'guild_id': guild_id,
        'business_date': business_date,
        'row_count': int(current['row_count'] or 0),
        'total_income': f"{float(current['total_income'] or 0):.6f}",
        'watermark_count': int(watermark is not None),
    }
    if preimage['row_count'] != 0 or preimage['watermark_count'] != 0:
        if observation_id < 2 or watermark is None or provenance is None:
            raise ValueError('target_scope_not_empty_manual_review_required')
        try:
            source_provenance = json.loads(str(provenance['gate_evidence_json'] or '{}')).get('source_provenance') or {}
        except (TypeError, ValueError):
            source_provenance = {}
        exact_reobservation = bool(
            preimage['row_count'] == int(parsed['effective_row_count'])
            and _money(current['total_income']) == _money(parsed['total_income'])
            and int(watermark['row_count'] or 0) == int(parsed['effective_row_count'])
            and _money(watermark['total_income']) == _money(parsed['total_income'])
            and str(watermark['data_status'] or '') == 'complete'
            and int(watermark['revision_version'] or 0) > 0
            and len(str(watermark['checksum'] or '')) == 64
            and str(watermark['last_success_sync_id'] or '') == base_sync_id
            and int(fact_lineage['sync_ids'] or 0) == 1
            and str(fact_lineage['sync_id'] or '') == base_sync_id
            and int(fact_lineage['min_revision'] or 0) == int(watermark['revision_version'] or 0)
            and int(fact_lineage['max_revision'] or 0) == int(watermark['revision_version'] or 0)
            and str(source_provenance.get('raw_response_sha256') or '') == source_sha
        )
        if not exact_reobservation:
            raise ValueError('target_scope_lineage_mismatch_manual_review_required')
        preimage['mode'] = 'exact_reobservation'
        preimage['watermark_checksum'] = str(watermark['checksum'])
        preimage['watermark_revision'] = int(watermark['revision_version'])
        preimage['watermark_sync_id'] = str(watermark['last_success_sync_id'])
    preimage_path = Path(str(job['preimage_path']))
    if observation_id > 1:
        preimage_path = preimage_path.with_name(
            f'{preimage_path.stem}-obs{observation_id}{preimage_path.suffix}'
        )
    preimage_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = preimage_path.with_suffix(preimage_path.suffix + '.tmp')
    temporary.write_text(json.dumps(preimage, sort_keys=True) + '\n', encoding='utf-8')
    temporary.replace(preimage_path)

    with connect() as archive_conn:
        archive = archive_raw_bytes(
            archive_conn,
            run_id=sync_id,
            app_name='timo',
            dataset='official_android_manual_revenue',
            endpoint='operator_supplied_android_app_export',
            content=content,
            media_type='text/csv; charset=utf-8',
            extension='.csv',
            guild_executor_key=guild_executor_key,
            guild_name=identity.storage_name,
            business_date=business_date,
            source_timezone='Asia/Shanghai',
            request_params={
                'declared_business_date': business_date,
                'declared_guild_id': guild_id,
                'original_filename': source_path.name,
                'original_extension': source_path.suffix,
            },
            row_count=int(parsed['source_row_count']),
            retrieved_at=snapshot_at,
        )
        archive_conn.commit()
    materialized = materialize_timo_revenue_snapshot(
        connect,
        sync_id=sync_id,
        parent_run_id=sync_id,
        guild_executor_key=guild_executor_key,
        guild_name=identity.storage_name,
        country='Mexico',
        stat_date_bj=business_date,
        provisional=False,
        revenue_rows=parsed['rows'],
        snapshot_at=snapshot_at,
        idempotency_key=f'official-android:{business_date}:{guild_id}:{source_sha}:obs{observation_id}',
        source_provenance={
            'source_kind': 'official_android_manual_export',
            'business_date': business_date,
            'guild_id': guild_id,
            'raw_response_sha256': source_sha,
            'raw_object_id': archive['raw_object_id'],
            'artifact_path': archive['artifact_path'],
            'source_row_count': parsed['source_row_count'],
            'effective_row_count': parsed['effective_row_count'],
            'source_total_income': f"{parsed['total_income']:.6f}",
            **({'official_verification': official_verification} if official_verification else {}),
        },
    )
    if materialized.get('status') not in {'success', 'no_op'}:
        raise RuntimeError('official_snapshot_materialization_not_successful')
    with connect() as analytics_conn:
        analytics = materialize_streamer_analytics_tables(
            analytics_conn,
            app_names=('timo',),
            include_timo_cohorts=True,
        )
    if analytics.get('ok') is not True:
        raise RuntimeError('official_snapshot_analytics_materialization_failed')
    return {
        'ok': True,
        'sync_id': sync_id,
        'business_date': business_date,
        'guild_id': guild_id,
        'source_sha256': source_sha,
        'observation_id': observation_id,
        'source_row_count': parsed['source_row_count'],
        'effective_row_count': parsed['effective_row_count'],
        'total_income': f"{parsed['total_income']:.6f}",
        'official_verification': official_verification,
        'archive': archive,
        'materialization': materialized,
        'analytics': analytics,
        'preimage_path': str(preimage_path),
    }


def notify(job: dict[str, Any]) -> dict[str, Any]:
    from scripts.notify_timo_materialization import (
        complete_event_ready_for_downstream,
        current_event_for_date,
        notification_skip_result,
        send_event,
        write_ack,
    )

    db_path = Path(str(job.get('db_path') or ROOT / 'data/automation.db')).resolve()
    business_date = str(job['business_date'])
    with sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        event = current_event_for_date(conn, business_date)
    if not complete_event_ready_for_downstream(event):
        return {
            **notification_skip_result(event, state='PENDING_REOBSERVE'),
            'scopes': event['scopes'],
        }
    secret = Path(str(job.get('secret_file') or '/etc/mcn-ai-automation/timo-materialization-webhook.secret')).read_text(encoding='utf-8').strip()
    if len(secret) < 32:
        raise RuntimeError('webhook_secret_invalid')
    result = send_event(
        event,
        url=str(job.get('webhook_url') or 'https://nova.hoyisr.com/api/internal/timo/materialization-complete'),
        secret=secret,
    )
    write_ack(Path(str(job.get('ack_path') or ROOT / 'data/timo_materialization_notification_ack.json')), event)
    return {
        **result,
        'checksum': event['checksum'],
        'day_status': event['dayStatus'],
        'scope_succeeded': event['scopeSucceeded'],
        'scope_failed': event['scopeFailed'],
        'scopes': event['scopes'],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--job-spec', required=True)
    parser.add_argument('--fail-on-lock-busy', action='store_true')
    args = parser.parse_args()
    assert_managed_batch_runtime('timo_official_revenue_import', required_slice='mcn-batch.slice')
    job = _load_job(Path(args.job_spec))
    try:
        lock = acquire_sqlite_job_lock('sqlite-etl', timeout_seconds=0)
    except JobLockBusy as exc:
        print_job_lock_skip(exc)
        return 75 if args.fail_on_lock_busy else 0
    with lock:
        result = run(job)
    result['notification'] = notify(job)
    status_path = Path(str(job['status_path']))
    status_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = status_path.with_suffix(status_path.suffix + '.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str) + '\n', encoding='utf-8')
    temporary.replace(status_path)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
