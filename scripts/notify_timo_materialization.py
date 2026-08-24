#!/usr/bin/env python3
"""Publish complete Timo scopes while keeping an incomplete business day fail-closed."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.timo_guild_identity import TIMO_GUILD_IDENTITIES  # noqa: E402
from app.timo_incremental_materialization import timo_external_feed_status  # noqa: E402
from app.timo_partial_settlement import enrich_timo_scope_feed_status  # noqa: E402

COUNTRY_BY_GUILD_ID = {'22000408': 'MX', '11003905': 'ID', '22000448': 'BR'}
SOURCE_MISSING_MARKERS = (
    'source_not_ready',
    'timo_revenue_export_not_ready',
    'export_url_not_ready',
    'circuit open until',
    'ticket',
    'awaiting_source_recovery_notification',
)

FINAL_REVENUE_CONTRACTS = {
    'complete_guild_and_streamer',
    'complete_available_guild_and_streamer',
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _materialization_identity(status: dict[str, Any]) -> dict[str, Any]:
    """Normalize feed, revision and persistent-retry result envelopes."""
    if status.get('status') == 'idle':
        raise ValueError('materialization_idle')
    if isinstance(status.get('results'), list):
        results = status['results']
        if len(results) != 1 or not isinstance(results[0], dict):
            raise ValueError('materialization_retry_scope_ambiguous')
        return _materialization_identity(results[0])
    if status.get('status') in {'success', 'partial'} and status.get('data_date_bj'):
        return status

    scopes = status.get('scopes')
    if (
        int(status.get('guild_count') or 0) != len(TIMO_GUILD_IDENTITIES)
        or not isinstance(scopes, list)
        or len(scopes) != 1
        or not isinstance(scopes[0], dict)
    ):
        raise ValueError('materialization_not_publishable')
    scope = dict(scopes[0])
    root_run_id = str(status.get('run_id') or '')
    root_date_from = str(status.get('date_from') or '')
    root_date_to = str(status.get('date_to') or '')
    if (
        not root_run_id
        or not root_date_from
        or root_date_from != root_date_to
        or str(scope.get('run_id') or '') != root_run_id
        or str(scope.get('data_date_bj') or '') != root_date_to
        or int(scope.get('guild_count') or 0) != int(status.get('guild_count') or 0)
    ):
        raise ValueError('materialization_identity_mismatch')
    scope['errors'] = list(status.get('errors') or scope.get('errors') or [])
    scope['status'] = str(scope.get('status') or ('success' if status.get('ok') is True else 'partial'))
    return scope


def _latest_failures(conn: sqlite3.Connection, data_date: str) -> dict[str, dict[str, str]]:
    failures: dict[str, dict[str, str]] = {}
    rows = conn.execute(
        """
        SELECT guild_name, status, error_code, error, start_time
        FROM timo_sync_run_log
        WHERE stat_date_bj=?
        ORDER BY guild_name, start_time DESC
        """,
        (data_date,),
    ).fetchall()
    for row in rows:
        guild_name = str(row['guild_name'] or '')
        if guild_name in failures:
            continue
        failures[guild_name] = {
            'status': str(row['status'] or ''),
            'errorCode': str(row['error_code'] or '')[:120],
            'error': str(row['error'] or '')[:240],
        }
    return failures


def _quality_status(failure: dict[str, str]) -> str:
    evidence = f"{failure.get('errorCode', '')}:{failure.get('error', '')}".casefold()
    return 'SOURCE_MISSING' if any(marker in evidence for marker in SOURCE_MISSING_MARKERS) else 'ANOMALY'


def build_event(
    *,
    status: dict[str, Any],
    manifests: list[dict[str, Any]],
    failures: dict[str, dict[str, str]],
) -> dict[str, Any]:
    normalized = _materialization_identity(status)
    if normalized.get('provisional') is not False:
        raise ValueError('materialization_not_final_contract')
    revenue_contract = str(normalized.get('revenue_contract') or '')
    if revenue_contract not in FINAL_REVENUE_CONTRACTS:
        raise ValueError('materialization_not_final_contract')
    deferred_by_id: dict[str, str] = {}
    for item in normalized.get('deferred_revenue_guilds') or []:
        if not isinstance(item, dict):
            raise ValueError('materialization_deferred_scope_invalid')
        guild_id = str(item.get('guild_id') or '').strip()
        reason = str(item.get('reason') or '').strip()
        if (
            guild_id not in COUNTRY_BY_GUILD_ID
            or guild_id in deferred_by_id
            or not reason
        ):
            raise ValueError('materialization_deferred_scope_invalid')
        deferred_by_id[guild_id] = reason
    if revenue_contract == 'complete_available_guild_and_streamer' and not deferred_by_id:
        raise ValueError('materialization_deferred_scope_missing')
    if revenue_contract == 'complete_guild_and_streamer' and deferred_by_id:
        raise ValueError('materialization_deferred_scope_unexpected')
    data_date = str(normalized.get('data_date_bj') or '')
    run_id = str(normalized.get('run_id') or '')
    if not data_date or not run_id:
        raise ValueError('materialization_identity_missing')

    manifests_by_name = {str(item.get('guild_name') or ''): item for item in manifests}
    scopes: list[dict[str, Any]] = []
    succeeded = 0
    for identity in TIMO_GUILD_IDENTITIES:
        manifest = manifests_by_name.get(identity.storage_name) or {}
        row_count = int(manifest.get('row_count') or 0)
        total_income = float(manifest.get('total_income') or 0)
        source_checksum = str(manifest.get('checksum') or '')
        revision = int(manifest.get('revision_version') or 0)
        ready = bool(
            manifest.get('publication_ready') is True
            and row_count > 0
            and total_income > 0
            and revision > 0
            and len(source_checksum) == 64
        )
        common = {
            'businessDate': data_date,
            'guildId': identity.guild_id,
            'guildName': identity.display_name,
            'guildStorageName': identity.storage_name,
            'country': COUNTRY_BY_GUILD_ID[identity.guild_id],
        }
        if ready:
            succeeded += 1
            scopes.append({
                **common,
                'qualityStatus': 'COMPLETE',
                'consumable': True,
                'rowCount': row_count,
                'totalIncome': f'{total_income:.6f}',
                'checksum': source_checksum,
                'revision': revision,
                'sourceGeneration': str(manifest.get('last_success_sync_id') or ''),
                'materializedAt': str(
                    manifest.get('last_success_time')
                    or manifest.get('source_snapshot_at')
                    or normalized.get('snapshot_at')
                    or ''
                ),
            })
            continue
        failure = failures.get(identity.storage_name) or {}
        if identity.guild_id in deferred_by_id:
            failure = {
                **failure,
                'errorCode': deferred_by_id[identity.guild_id],
                'error': deferred_by_id[identity.guild_id],
            }
        scopes.append({
            **common,
            'qualityStatus': _quality_status(failure),
            'consumable': False,
            'rowCount': None,
            'totalIncome': None,
            'checksum': None,
            'revision': None,
            'sourceGeneration': None,
            'materializedAt': None,
            'failureReason': failure.get('errorCode') or 'scope_not_publication_ready',
        })
    if succeeded == 0:
        raise ValueError('no_publication_ready_scope')

    failed = len(scopes) - succeeded
    failed_ids = {
        str(scope.get('guildId') or '')
        for scope in scopes
        if scope.get('consumable') is not True
    }
    if (
        revenue_contract == 'complete_available_guild_and_streamer'
        and failed_ids != set(deferred_by_id)
    ):
        raise ValueError('materialization_deferred_scope_mismatch')
    day_status = 'COMPLETE' if failed == 0 else 'PARTIAL'
    checksum = hashlib.sha256(canonical_json(scopes).encode('utf-8')).hexdigest()
    event_id = f'timo:{data_date}:{checksum[:20]}'
    materialized_at = str(normalized.get('snapshot_at') or '')
    if not materialized_at:
        materialized_at = max(
            (str(scope.get('materializedAt') or '') for scope in scopes),
            default='',
        )
    return {
        'schemaVersion': 2,
        'eventType': 'timo.materialization.completed' if day_status == 'COMPLETE' else 'timo.materialization.partial',
        'eventId': event_id,
        'businessDate': data_date,
        'dataDate': data_date,
        'dateContract': 'beijing_business_date_v1',
        'dayStatus': day_status,
        'ready': day_status == 'COMPLETE',
        'consumable': day_status == 'COMPLETE',
        'runId': run_id,
        'sourceGeneration': run_id,
        'materializedAt': materialized_at,
        'publishedAt': materialized_at,
        'expectedScopeCount': len(TIMO_GUILD_IDENTITIES),
        'scopeTotal': len(scopes),
        'scopeSucceeded': succeeded,
        'scopeFailed': failed,
        'failedScopes': [scope['country'] for scope in scopes if not scope['consumable']],
        'scopes': scopes,
        'checksum': checksum,
    }


def load_event(status_path: Path, db_path: Path, started_marker: Path) -> dict[str, Any]:
    if not status_path.is_file() or not started_marker.is_file():
        raise ValueError('fresh_status_missing')
    if status_path.stat().st_mtime_ns <= started_marker.stat().st_mtime_ns:
        raise ValueError('status_not_from_current_invocation')
    raw_status = json.loads(status_path.read_text(encoding='utf-8'))
    normalized = _materialization_identity(raw_status)
    data_date = str(normalized.get('data_date_bj') or '')
    if not data_date:
        raise ValueError('materialization_identity_missing')

    uri = f'file:{db_path}?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=3) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        feed_status = timo_external_feed_status(conn, stat_date_bj=data_date)
        feed_status = enrich_timo_scope_feed_status(
            conn,
            feed_status,
            business_date=data_date,
        )
        failures = _latest_failures(conn, data_date)
        last_success = {
            str(row['guild_name'] or ''): str(row['last_success_time'] or '')
            for row in conn.execute(
                'SELECT guild_name, last_success_time FROM timo_sync_watermark WHERE stat_date_bj=?',
                (data_date,),
            ).fetchall()
        }
    manifests = []
    for item in feed_status.get('scope_manifests') or []:
        manifest = dict(item)
        manifest['last_success_time'] = last_success.get(str(item.get('guild_name') or ''), '')
        manifests.append(manifest)
    return build_event(status=raw_status, manifests=manifests, failures=failures)


def current_event_for_date(conn: sqlite3.Connection, data_date: str) -> dict[str, Any]:
    """Build the currently publishable content identity without mutating source state."""
    latest_run = conn.execute(
        """
        SELECT sync_id, COALESCE(end_time, start_time) AS snapshot_at
        FROM timo_sync_run_log
        WHERE stat_date_bj=? AND status IN ('success', 'no_op')
        ORDER BY start_time DESC
        LIMIT 1
        """,
        (data_date,),
    ).fetchone()
    if latest_run is None:
        raise ValueError('materialization_identity_missing')
    feed_status = enrich_timo_scope_feed_status(
        conn,
        timo_external_feed_status(conn, stat_date_bj=data_date),
        business_date=data_date,
    )
    last_success = {
        str(row['guild_name'] or ''): str(row['last_success_time'] or '')
        for row in conn.execute(
            'SELECT guild_name, last_success_time FROM timo_sync_watermark WHERE stat_date_bj=?',
            (data_date,),
        ).fetchall()
    }
    manifests = []
    for item in feed_status.get('scope_manifests') or []:
        manifest = dict(item)
        manifest['last_success_time'] = last_success.get(str(item.get('guild_name') or ''), '')
        manifests.append(manifest)
    return build_event(
        status={
            'status': 'success',
            'provisional': False,
            'revenue_contract': 'complete_guild_and_streamer',
            'data_date_bj': data_date,
            'run_id': str(latest_run['sync_id'] or ''),
            'snapshot_at': str(latest_run['snapshot_at'] or ''),
        },
        manifests=manifests,
        failures=_latest_failures(conn, data_date),
    )


def write_ack(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    try:
        existing = json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        existing = {}
    acknowledgements = dict(existing.get('acknowledgements') or {}) if isinstance(existing, dict) else {}
    scope_lineage = {
        str(scope.get('guildStorageName') or ''): {
            'checksum': str(scope.get('checksum') or ''),
            'revision': int(scope.get('revision') or 0),
            'source_generation': str(scope.get('sourceGeneration') or ''),
        }
        for scope in event.get('scopes') or []
        if scope.get('consumable') is True
    }
    business_date = str(event.get('businessDate') or '')
    acknowledgements[business_date] = {
        'event_id': str(event.get('eventId') or ''),
        'checksum': str(event.get('checksum') or ''),
        'scope_lineage': scope_lineage,
        'acknowledged_at': datetime.now(timezone.utc).isoformat(),
    }
    temporary.write_text(
        json.dumps({
            'business_date': business_date,
            'event_id': str(event.get('eventId') or ''),
            'checksum': str(event.get('checksum') or ''),
            'scope_lineage': scope_lineage,
            'acknowledgements': acknowledgements,
            'acknowledged_at': datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False, sort_keys=True),
        encoding='utf-8',
    )
    temporary.replace(path)


def send_event(event: dict[str, Any], *, url: str, secret: str, attempts: int = 3) -> dict[str, Any]:
    body = canonical_json(event).encode('utf-8')
    last_error = ''
    for attempt in range(1, attempts + 1):
        timestamp = str(int(time.time()))
        signature = hmac.new(secret.encode('utf-8'), f'{timestamp}.'.encode('utf-8') + body, hashlib.sha256).hexdigest()
        req = request.Request(url, data=body, method='POST', headers={
            'Content-Type': 'application/json',
            'X-Timo-Event-Id': str(event['eventId']),
            'X-Timo-Timestamp': timestamp,
            'X-Timo-Signature': f'sha256={signature}',
            'User-Agent': 'mcn-timo-materialization-notifier/2',
        })
        try:
            with request.urlopen(req, timeout=8) as response:
                payload = json.loads(response.read(4096).decode('utf-8'))
                if response.status == 202 and payload.get('ok') is True:
                    return {'ok': True, 'event_id': event['eventId'], 'duplicate': bool(payload.get('duplicate'))}
                last_error = f'unexpected_response:{response.status}'
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = type(exc).__name__
        if attempt < attempts:
            time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f'notification_failed:{last_error}')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--status-path', default=str(ROOT / 'data/timo_external_feed_status.json'))
    parser.add_argument('--db-path', default=str(ROOT / 'data/automation.db'))
    parser.add_argument('--started-marker', default='/run/mcn-ai-automation/timo-external-feed.started')
    parser.add_argument('--ack-path', default=str(ROOT / 'data/timo_materialization_notification_ack.json'))
    parser.add_argument('--secret-file', default='/etc/mcn-ai-automation/timo-materialization-webhook.secret')
    parser.add_argument('--url', default=os.getenv(
        'TIMO_MATERIALIZATION_WEBHOOK_URL',
        'https://nova.hoyisr.com/api/internal/timo/materialization-complete',
    ))
    args = parser.parse_args()
    try:
        event = load_event(Path(args.status_path), Path(args.db_path), Path(args.started_marker))
    except ValueError as exc:
        print(json.dumps({'ok': True, 'skipped': str(exc)}, sort_keys=True))
        return 0
    secret = Path(args.secret_file).read_text(encoding='utf-8').strip()
    if len(secret) < 32:
        raise RuntimeError('webhook_secret_invalid')
    result = send_event(event, url=args.url, secret=secret)
    write_ack(Path(args.ack_path), event)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
