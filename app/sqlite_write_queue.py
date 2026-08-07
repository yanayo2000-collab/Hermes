from __future__ import annotations

import hashlib
import heapq
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.ad_dashboard_repository import (
    ad_dashboard_fact_rows_completeness,
    mark_ad_dashboard_sync_state,
    replace_ad_dashboard_fact_rows_for_dates,
)
from app.sqlite_job_lock import acquire_sqlite_job_lock
from app.sqlite_observability import connect_observed_sqlite


DEFAULT_WRITER_URL = 'http://127.0.0.1:8765'


class SQLiteWriteQueueError(RuntimeError):
    pass


def db_writer_enabled() -> bool:
    return str(os.getenv('MCN_DB_WRITER_ENABLED') or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def db_writer_required() -> bool:
    return str(os.getenv('MCN_DB_WRITER_REQUIRE') or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _parse_date(value: Any):
    return datetime.fromisoformat(str(value or '').strip()[:10]).date()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = connect_observed_sqlite(str(db_path), source='mcn-db-writer', timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=60000')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def sqlite_write_job_idempotency_key(job: Dict[str, Any]) -> str:
    explicit = str((job or {}).get('idempotency_key') or '').strip()
    if explicit:
        return explicit[:200]
    job_type = str((job or {}).get('type') or '').strip()
    if job_type == 'truth_acquisition_log':
        acquisition_id = str((job or {}).get('acquisition_id') or '').strip()
        if acquisition_id:
            return f'truth_acquisition_log:{acquisition_id}'
    canonical = json.dumps(job or {}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return f'{job_type or "unknown"}:{hashlib.sha256(canonical.encode("utf-8")).hexdigest()}'


def apply_sqlite_write_job(*, db_path: str, job: Dict[str, Any]) -> Dict[str, Any]:
    job_type = str((job or {}).get('type') or '').strip()
    if job_type == 'ad_dashboard_fact_replace':
        return _apply_ad_dashboard_fact_replace(db_path=db_path, job=job)
    if job_type == 'ad_dashboard_schema_ensure':
        return _apply_ad_dashboard_schema_ensure(db_path=db_path)
    if job_type == 'ad_dashboard_fact_restore_window':
        return _apply_ad_dashboard_fact_restore_window(db_path=db_path, job=job)
    if job_type == 'ad_dashboard_fact_reclassify_country':
        return _apply_ad_dashboard_fact_reclassify_country(db_path=db_path, job=job)
    if job_type == 'truth_acquisition_log':
        return _apply_truth_acquisition_log(db_path=db_path, job=job)
    if job_type == 'timo_auth_station_heartbeat':
        return _apply_timo_auth_station_heartbeat(db_path=db_path, job=job)
    if job_type in {'im_llm_claim_next', 'im_llm_claim'}:
        return _apply_im_llm_claim(db_path=db_path, job=job)
    if job_type in {'creative_image2_claim_next', 'creative_image2_claim'}:
        return _apply_creative_image2_claim(db_path=db_path, job=job)
    if job_type == 'creative_reference_upsert':
        return _apply_creative_reference_upsert(db_path=db_path, job=job)
    raise SQLiteWriteQueueError(f'unsupported_sqlite_write_job:{job_type or "missing"}')


def _apply_creative_reference_upsert(*, db_path: str, job: Dict[str, Any]) -> Dict[str, Any]:
    """Persist only live-access creative references through the single writer."""
    from app.growth.creative_reference_service import (  # noqa: PLC0415
        ensure_creative_reference_table,
        persist_creative_reference,
    )

    references = [dict(item or {}) for item in list(job.get('references') or [])]
    allowed = {
        str(item or '').strip().removeprefix('act_')
        for item in list(job.get('accessible_account_ids') or [])
        if str(item or '').strip()
    }
    if not references:
        raise SQLiteWriteQueueError('creative_reference_upsert_empty')
    for reference in references:
        ids = dict(reference.get('actual_meta_ids') or {})
        account_id = str(ids.get('account_id') or '').strip().removeprefix('act_')
        if reference.get('status') != 'ACTIVE_REFERENCE' or not account_id or account_id not in allowed:
            raise SQLiteWriteQueueError('creative_reference_upsert_access_not_verified')

    with acquire_sqlite_job_lock('sqlite-writer'):
        conn = _connect(db_path)
        try:
            ensure_creative_reference_table(conn)
            persisted = [persist_creative_reference(conn, reference) for reference in references]
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {
        'ok': True,
        'type': 'creative_reference_upsert',
        'stored': len(persisted),
        'ad_ids': sorted(str((item.get('actual_meta_ids') or {}).get('ad_id') or '') for item in references),
        'source': 'mcn-db-writer',
    }


def _apply_ad_dashboard_schema_ensure(*, db_path: str) -> Dict[str, Any]:
    """Apply the additive advertising fact schema through the single writer."""
    from app.ad_dashboard_repository import ensure_ad_dashboard_fact_tables  # noqa: PLC0415
    from app.schema_migrations import apply_schema_migration_registry  # noqa: PLC0415

    with acquire_sqlite_job_lock('sqlite-writer'):
        conn = _connect(db_path)
        try:
            apply_schema_migration_registry(conn)
            ensure_ad_dashboard_fact_tables(conn)
            columns = {
                str(item[1])
                for item in conn.execute('PRAGMA table_info(ad_dashboard_fact_rows)').fetchall()
            }
            required = {'account_id', 'account_name', 'campaign_id', 'adset_id', 'ad_id'}
            missing = sorted(required - columns)
            if missing:
                raise SQLiteWriteQueueError(
                    'ad_dashboard_schema_ensure_missing_columns:' + ','.join(missing)
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {
        'ok': True,
        'type': 'ad_dashboard_schema_ensure',
        'lineage_columns': sorted(required),
        'source': 'mcn-db-writer',
    }


def _apply_ad_dashboard_fact_replace(*, db_path: str, job: Dict[str, Any]) -> Dict[str, Any]:
    from app.schema_migrations import apply_schema_migration_registry  # noqa: PLC0415

    rows = list(job.get('rows') or [])
    start_date = _parse_date(job.get('start_date'))
    end_date = _parse_date(job.get('end_date'))
    synced_at = str(job.get('synced_at') or datetime.now(timezone.utc).isoformat())
    appsflyer_required = job.get('appsflyer_required')
    if appsflyer_required is not None:
        appsflyer_required = bool(appsflyer_required)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with acquire_sqlite_job_lock('sqlite-writer', metadata={'stage': 'ad_dashboard_fact_replace', 'job_type': 'ad_dashboard_fact_replace'}):
        conn = _connect(db_path)
        try:
            apply_schema_migration_registry(conn)
            stored_count = replace_ad_dashboard_fact_rows_for_dates(
                conn,
                rows,
                start_date=start_date,
                end_date=end_date,
                synced_at=synced_at,
            )
            completeness = ad_dashboard_fact_rows_completeness(
                rows,
                start_date=start_date,
                end_date=end_date,
                appsflyer_required=appsflyer_required,
            )
            mark_ad_dashboard_sync_state(
                conn,
                source=str(job.get('source') or 'all'),
                start_date=start_date,
                end_date=end_date,
                status=str(completeness.get('status') or 'partial'),
                row_count=stored_count,
                error_message=str(completeness.get('error_message') or ''),
                synced_at=synced_at,
            )
            conn.commit()
        finally:
            conn.close()
    return {
        'ok': True,
        'type': 'ad_dashboard_fact_replace',
        'write_mode': 'immutable_history_merge',
        'stored_rows': stored_count,
        'date_start': start_date.isoformat(),
        'date_end': end_date.isoformat(),
        'sync_status': str(completeness.get('status') or 'partial'),
        'sync_error_message': str(completeness.get('error_message') or ''),
        'source': 'mcn-db-writer',
    }


def _apply_ad_dashboard_fact_restore_window(*, db_path: str, job: Dict[str, Any]) -> Dict[str, Any]:
    """Restore an exact bounded fact-row preimage through the dedicated writer."""
    from app.ad_dashboard_repository import ensure_ad_dashboard_fact_tables  # noqa: PLC0415
    from app.schema_migrations import apply_schema_migration_registry  # noqa: PLC0415

    rows = list(job.get('rows') or [])
    start_date = _parse_date(job.get('start_date'))
    end_date = _parse_date(job.get('end_date'))
    expected_rows = int(job.get('expected_rows') or 0)
    if start_date > end_date:
        raise SQLiteWriteQueueError('ad_dashboard_fact_restore_window_invalid_range')
    if expected_rows <= 0 or expected_rows != len(rows):
        raise SQLiteWriteQueueError('ad_dashboard_fact_restore_window_row_count_mismatch')
    if any(not isinstance(row, dict) for row in rows):
        raise SQLiteWriteQueueError('ad_dashboard_fact_restore_window_invalid_row')
    for row in rows:
        row_date = _parse_date(row.get('date'))
        if row_date < start_date or row_date > end_date:
            raise SQLiteWriteQueueError('ad_dashboard_fact_restore_window_row_outside_range')

    with acquire_sqlite_job_lock(
        'sqlite-writer',
        metadata={
            'stage': 'ad_dashboard_fact_restore_window',
            'job_type': 'ad_dashboard_fact_restore_window',
        },
    ):
        conn = _connect(db_path)
        try:
            apply_schema_migration_registry(conn)
            ensure_ad_dashboard_fact_tables(conn)
            columns = [
                str(item[1])
                for item in conn.execute('PRAGMA table_info(ad_dashboard_fact_rows)').fetchall()
            ]
            if not columns or any(any(column not in row for column in columns) for row in rows):
                raise SQLiteWriteQueueError('ad_dashboard_fact_restore_window_schema_mismatch')
            placeholders = ','.join('?' for _ in columns)
            insert_sql = (
                f"INSERT INTO ad_dashboard_fact_rows ({','.join(columns)}) "
                f"VALUES ({placeholders})"
            )
            conn.commit()
            conn.execute('BEGIN IMMEDIATE')
            deleted_rows = int(conn.execute(
                'SELECT COUNT(*) FROM ad_dashboard_fact_rows WHERE date BETWEEN ? AND ?',
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchone()[0])
            conn.execute(
                'DELETE FROM ad_dashboard_fact_rows WHERE date BETWEEN ? AND ?',
                (start_date.isoformat(), end_date.isoformat()),
            )
            conn.executemany(
                insert_sql,
                [[row[column] for column in columns] for row in rows],
            )
            restored_rows = int(conn.execute(
                'SELECT COUNT(*) FROM ad_dashboard_fact_rows WHERE date BETWEEN ? AND ?',
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchone()[0])
            if restored_rows != expected_rows:
                raise SQLiteWriteQueueError('ad_dashboard_fact_restore_window_verification_failed')
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {
        'ok': True,
        'type': 'ad_dashboard_fact_restore_window',
        'write_mode': 'exact_bounded_preimage_restore',
        'date_start': start_date.isoformat(),
        'date_end': end_date.isoformat(),
        'deleted_rows': deleted_rows,
        'restored_rows': restored_rows,
        'source': 'mcn-db-writer',
    }


def _apply_ad_dashboard_fact_reclassify_country(*, db_path: str, job: Dict[str, Any]) -> Dict[str, Any]:
    """Change only country lineage while proving all fact metrics are invariant."""
    from app.ad_dashboard_repository import (  # noqa: PLC0415
        AD_DASHBOARD_FACT_COLUMNS,
        _ad_fact_row_id,
        ensure_ad_dashboard_fact_tables,
    )
    from app.schema_migrations import apply_schema_migration_registry  # noqa: PLC0415

    items = list(job.get('items') or [])
    start_date = _parse_date(job.get('start_date'))
    end_date = _parse_date(job.get('end_date'))
    expected_items = int(job.get('expected_items') or 0)
    if start_date > end_date:
        raise SQLiteWriteQueueError('ad_dashboard_fact_reclassify_country_invalid_range')
    if expected_items <= 0 or expected_items != len(items):
        raise SQLiteWriteQueueError('ad_dashboard_fact_reclassify_country_item_count_mismatch')
    source_ids = {str(item.get('row_id') or '').strip() for item in items if isinstance(item, dict)}
    if len(source_ids) != len(items) or '' in source_ids:
        raise SQLiteWriteQueueError('ad_dashboard_fact_reclassify_country_duplicate_or_missing_row_id')

    with acquire_sqlite_job_lock(
        'sqlite-writer',
        metadata={
            'stage': 'ad_dashboard_fact_reclassify_country',
            'job_type': 'ad_dashboard_fact_reclassify_country',
        },
    ):
        conn = _connect(db_path)
        try:
            apply_schema_migration_registry(conn)
            ensure_ad_dashboard_fact_tables(conn)
            columns = [
                str(item[1])
                for item in conn.execute('PRAGMA table_info(ad_dashboard_fact_rows)').fetchall()
            ]
            metric_columns = ['row_count', *AD_DASHBOARD_FACT_COLUMNS]
            placeholders = ','.join('?' for _ in columns)
            insert_sql = (
                f"INSERT INTO ad_dashboard_fact_rows ({','.join(columns)}) "
                f"VALUES ({placeholders})"
            )
            before_count = int(conn.execute(
                'SELECT COUNT(*) FROM ad_dashboard_fact_rows WHERE date BETWEEN ? AND ?',
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchone()[0])
            before_metrics = tuple(float(value or 0.0) for value in conn.execute(
                f"SELECT {','.join(f'SUM({column})' for column in metric_columns)} "
                "FROM ad_dashboard_fact_rows WHERE date BETWEEN ? AND ?",
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchone())
            replacements: list[Dict[str, Any]] = []
            target_ids: set[str] = set()
            now = str(job.get('synced_at') or datetime.now(timezone.utc).isoformat())
            for item in items:
                row = conn.execute(
                    'SELECT * FROM ad_dashboard_fact_rows WHERE row_id = ?',
                    (str(item.get('row_id') or '').strip(),),
                ).fetchone()
                if row is None:
                    raise SQLiteWriteQueueError('ad_dashboard_fact_reclassify_country_source_missing')
                stored = dict(row)
                row_date = _parse_date(stored.get('date'))
                if row_date < start_date or row_date > end_date:
                    raise SQLiteWriteQueueError('ad_dashboard_fact_reclassify_country_row_outside_range')
                if str(stored.get('data_source') or '').strip().lower() not in {'appsflyer', 'meta'}:
                    raise SQLiteWriteQueueError('ad_dashboard_fact_reclassify_country_source_not_allowed')
                country = str(item.get('country') or '').strip() or 'Unknown'
                stored['country'] = country
                payload = json.loads(str(stored.get('payload_json') or '{}'))
                if not isinstance(payload, dict):
                    payload = {}
                payload['country'] = country
                for key in (
                    'country_attribution_status',
                    'country_attribution_source',
                    'country_attribution_grain',
                ):
                    value = str(item.get(key) or '').strip()
                    if value:
                        payload[key] = value
                    else:
                        payload.pop(key, None)
                stored['payload_json'] = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                stored['updated_at'] = now
                stored['row_id'] = _ad_fact_row_id(stored)
                if stored['row_id'] in target_ids:
                    raise SQLiteWriteQueueError('ad_dashboard_fact_reclassify_country_target_collision')
                target_ids.add(stored['row_id'])
                if stored['row_id'] not in source_ids:
                    collision = conn.execute(
                        'SELECT 1 FROM ad_dashboard_fact_rows WHERE row_id = ?',
                        (stored['row_id'],),
                    ).fetchone()
                    if collision:
                        raise SQLiteWriteQueueError('ad_dashboard_fact_reclassify_country_existing_target')
                replacements.append(stored)

            conn.commit()
            conn.execute('BEGIN IMMEDIATE')
            conn.executemany(
                'DELETE FROM ad_dashboard_fact_rows WHERE row_id = ?',
                [(row_id,) for row_id in source_ids],
            )
            conn.executemany(
                insert_sql,
                [[row[column] for column in columns] for row in replacements],
            )
            after_count = int(conn.execute(
                'SELECT COUNT(*) FROM ad_dashboard_fact_rows WHERE date BETWEEN ? AND ?',
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchone()[0])
            after_metrics = tuple(float(value or 0.0) for value in conn.execute(
                f"SELECT {','.join(f'SUM({column})' for column in metric_columns)} "
                "FROM ad_dashboard_fact_rows WHERE date BETWEEN ? AND ?",
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchone())
            metrics_unchanged = all(
                abs(before - after) <= 1e-8
                for before, after in zip(before_metrics, after_metrics)
            )
            if after_count != before_count or not metrics_unchanged:
                raise SQLiteWriteQueueError('ad_dashboard_fact_reclassify_country_invariant_failed')
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {
        'ok': True,
        'type': 'ad_dashboard_fact_reclassify_country',
        'write_mode': 'country_lineage_only',
        'date_start': start_date.isoformat(),
        'date_end': end_date.isoformat(),
        'reclassified_rows': len(replacements),
        'fact_rows_before': before_count,
        'fact_rows_after': after_count,
        'metrics_unchanged': metrics_unchanged,
        'source': 'mcn-db-writer',
    }


def _apply_truth_acquisition_log(*, db_path: str, job: Dict[str, Any]) -> Dict[str, Any]:
    acquisition_id = str(job.get('acquisition_id') or '').strip()
    if not acquisition_id:
        raise SQLiteWriteQueueError('truth_acquisition_log_missing_acquisition_id')
    now = str(job.get('now') or datetime.now(timezone.utc).isoformat())
    with acquire_sqlite_job_lock('sqlite-writer', metadata={'stage': 'truth_acquisition_log', 'job_type': 'truth_acquisition_log'}):
        conn = _connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO truth_acquisition_logs (
                    acquisition_id, account_key, binding_id, trigger, final_state, trust_status,
                    current_truth_written, latest_probe_written, result_json, stages_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(acquisition_id) DO UPDATE SET
                    account_key=excluded.account_key,
                    binding_id=excluded.binding_id,
                    trigger=excluded.trigger,
                    final_state=excluded.final_state,
                    trust_status=excluded.trust_status,
                    current_truth_written=excluded.current_truth_written,
                    latest_probe_written=excluded.latest_probe_written,
                    result_json=excluded.result_json,
                    stages_json=excluded.stages_json,
                    updated_at=excluded.updated_at
                """,
                (
                    acquisition_id,
                    str(job.get('account_key') or '').strip(),
                    str(job.get('binding_id') or '').strip(),
                    str(job.get('trigger') or '').strip(),
                    str(job.get('final_state') or '').strip(),
                    str(job.get('trust_status') or '').strip(),
                    1 if job.get('current_truth_written') else 0,
                    1 if job.get('latest_probe_written') else 0,
                    json.dumps(job.get('result') or {}, ensure_ascii=False),
                    json.dumps(list(job.get('stages') or []), ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return {'ok': True, 'type': 'truth_acquisition_log', 'acquisition_id': acquisition_id, 'source': 'mcn-db-writer'}


def _apply_timo_auth_station_heartbeat(*, db_path: str, job: Dict[str, Any]) -> Dict[str, Any]:
    from app.timo_auth_station import AuthStationHeartbeatRequest, TimoAuthStationService  # noqa: PLC0415

    payload = AuthStationHeartbeatRequest(**dict(job.get('payload') or {}))
    with acquire_sqlite_job_lock(
        'sqlite-writer',
        metadata={
            'stage': 'timo_auth_station_heartbeat',
            'job_type': 'timo_auth_station_heartbeat',
            'source': payload.station_id,
        },
    ):
        return TimoAuthStationService(db_path)._heartbeat_direct(payload)


def _apply_im_llm_claim(*, db_path: str, job: Dict[str, Any]) -> Dict[str, Any]:
    from app.im_diagnostics import claim_im_llm_diagnosis_task, next_im_llm_diagnosis_task  # noqa: PLC0415

    job_type = str(job.get('type') or '')
    with acquire_sqlite_job_lock(
        'sqlite-writer',
        metadata={'stage': job_type, 'job_type': job_type, 'task_id': job.get('task_id')},
    ):
        conn = _connect(db_path)
        try:
            if job_type == 'im_llm_claim_next':
                task = next_im_llm_diagnosis_task(
                    conn,
                    claim=True,
                    lease_owner=str(job.get('lease_owner') or 'hermes-llm-agent'),
                    lease_seconds=int(job.get('lease_seconds') or 900),
                )
            else:
                task = claim_im_llm_diagnosis_task(
                    conn,
                    str(job.get('task_id') or ''),
                    lease_owner=str(job.get('lease_owner') or 'hermes-llm-agent'),
                    lease_seconds=int(job.get('lease_seconds') or 900),
                )
        finally:
            conn.close()
    return {'ok': True, 'type': job_type, 'task': task, 'source': 'mcn-db-writer'}


def _apply_creative_image2_claim(*, db_path: str, job: Dict[str, Any]) -> Dict[str, Any]:
    from app.creative_image_generation import (  # noqa: PLC0415
        claim_hermes_image2_generation_task,
        next_hermes_image2_generation_task,
    )

    job_type = str(job.get('type') or '')
    with acquire_sqlite_job_lock(
        'sqlite-writer',
        metadata={'stage': job_type, 'job_type': job_type, 'task_id': job.get('task_id')},
    ):
        conn = _connect(db_path)
        try:
            if job_type == 'creative_image2_claim_next':
                task = next_hermes_image2_generation_task(
                    conn,
                    claim=True,
                    lease_owner=str(job.get('lease_owner') or 'hermes_image2_agent'),
                    lease_seconds=int(job.get('lease_seconds') or 900),
                )
            else:
                task = claim_hermes_image2_generation_task(
                    conn,
                    str(job.get('task_id') or ''),
                    lease_owner=str(job.get('lease_owner') or 'hermes_image2_agent'),
                    lease_seconds=int(job.get('lease_seconds') or 900),
                )
        finally:
            conn.close()
    return {'ok': True, 'type': job_type, 'task': task, 'source': 'mcn-db-writer'}


def submit_sqlite_write_job(
    job: Dict[str, Any],
    *,
    url: Optional[str] = None,
    timeout: float = 120.0,
    max_attempts: int = 3,
    retry_delay_seconds: float = 0.5,
) -> Dict[str, Any]:
    target = str(url or os.getenv('MCN_DB_WRITER_URL') or DEFAULT_WRITER_URL).rstrip('/') + '/write'
    body = json.dumps(job, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    idempotency_key = sqlite_write_job_idempotency_key(job)
    if str(job.get('type') or '').strip() == 'im_llm_claim_next' and not str(job.get('idempotency_key') or '').strip():
        idempotency_key = f'im_llm_claim_next:{uuid.uuid4().hex}'
    attempts = max(1, int(max_attempts))
    raw = ''
    status = 0
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            target,
            data=body,
            method='POST',
            headers={
                'content-type': 'application/json; charset=utf-8',
                'accept': 'application/json',
                'x-idempotency-key': idempotency_key,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode('utf-8', 'replace')
                status = int(response.status)
            break
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode('utf-8', 'replace')
            if exc.code == 409 and attempt < attempts:
                time.sleep(max(0.0, float(retry_delay_seconds)) * attempt)
                continue
            raise SQLiteWriteQueueError(f'db_writer_http_{exc.code}:{raw[:500]}') from exc
        except Exception as exc:  # noqa: BLE001
            raise SQLiteWriteQueueError(f'db_writer_unavailable:{type(exc).__name__}:{str(exc)[:200]}') from exc
    try:
        payload = json.loads(raw or '{}')
    except json.JSONDecodeError as exc:
        raise SQLiteWriteQueueError(f'db_writer_invalid_json:{raw[:200]}') from exc
    if status >= 400 or not payload.get('ok'):
        raise SQLiteWriteQueueError(str(payload.get('error') or f'db_writer_http_{status}'))
    return payload


class SQLiteWriterServer:
    def __init__(self, *, db_path: str) -> None:
        self.db_path = str(db_path)
        self.started_at = time.time()
        self.condition = threading.Condition()
        self.waiting: list[tuple[int, int]] = []
        self.active = False
        self.sequence = 0
        self.handled = 0
        self.failed = 0
        self.completed: Dict[str, Dict[str, Any]] = {}

    def health(self) -> Dict[str, Any]:
        return {
            'ok': True,
            'service': 'mcn-db-writer',
            'db_path': self.db_path,
            'handled': self.handled,
            'failed': self.failed,
            'waiting': len(self.waiting),
            'started_at': datetime.fromtimestamp(self.started_at, tz=timezone.utc).isoformat(),
        }

    def write(self, job: Dict[str, Any], *, idempotency_key: str = '') -> Dict[str, Any]:
        key = str(idempotency_key or sqlite_write_job_idempotency_key(job)).strip()
        raw_priority = job.get('priority')
        if raw_priority is None:
            priority = {
                'timo_auth_station_heartbeat': 0,
                'truth_acquisition_log': 0,
                'im_llm_claim_next': 10,
                'im_llm_claim': 10,
                'creative_image2_claim_next': 10,
                'creative_image2_claim': 10,
                'ad_dashboard_fact_replace': 100,
            }.get(str(job.get('type') or '').strip(), 50)
        else:
            priority = max(0, min(100, int(raw_priority)))
        with self.condition:
            self.sequence += 1
            ticket = (priority, self.sequence)
            heapq.heappush(self.waiting, ticket)
            while self.active or self.waiting[0] != ticket:
                self.condition.wait()
            heapq.heappop(self.waiting)
            self.active = True
        try:
            if key and key in self.completed:
                return {**self.completed[key], 'deduplicated': True}
            try:
                result = apply_sqlite_write_job(db_path=self.db_path, job=job)
                self.handled += 1
                if key:
                    if len(self.completed) >= 1000:
                        self.completed.pop(next(iter(self.completed)))
                    self.completed[key] = dict(result)
                return result
            except Exception:
                self.failed += 1
                raise
        finally:
            with self.condition:
                self.active = False
                self.condition.notify_all()
