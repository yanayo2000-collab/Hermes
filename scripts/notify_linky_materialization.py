#!/usr/bin/env python3
"""Deliver a durable, signed Linky D-1 completion event to Nova."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any
from urllib import error, request


ROOT = Path(__file__).resolve().parents[1]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _read_ready_state(analytics_path: Path, expected_date: str) -> sqlite3.Row:
    uri = f'file:{analytics_path}?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=3) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        row = conn.execute(
            """
            SELECT status, data_as_of, materialized_at
            FROM streamer_analytics_materialization_state
            WHERE app_name='linky'
            """
        ).fetchone()
    if row is None or str(row['status'] or '') != 'ready':
        raise ValueError('materialization_not_ready')
    if str(row['data_as_of'] or '') != expected_date:
        raise ValueError('materialization_date_not_ready')
    if not str(row['materialized_at'] or '').strip():
        raise ValueError('materialization_timestamp_missing')
    return row


def load_event(
    analytics_path: Path,
    source_db_path: Path,
    *,
    expected_date: str | None = None,
) -> dict[str, Any]:
    data_date = expected_date or (date.today() - timedelta(days=1)).isoformat()
    state = _read_ready_state(analytics_path, data_date)
    uri = f'file:{source_db_path}?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=3) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        rows = conn.execute(
            """
            SELECT guild_name, country, COUNT(*) AS row_count,
                   ROUND(SUM(total_income), 6) AS total_income
            FROM streamer_external_revenue_daily
            WHERE app_name='linky' AND stat_date_bj=?
            GROUP BY guild_name, country
            ORDER BY country, guild_name
            """,
            (data_date,),
        ).fetchall()
    if not rows:
        raise ValueError('materialization_scope_missing')
    scopes: list[dict[str, Any]] = []
    for row in rows:
        row_count = int(row['row_count'] or 0)
        total_income = float(row['total_income'] or 0)
        if not str(row['guild_name'] or '').strip() or not str(row['country'] or '').strip() \
                or row_count <= 0 or total_income <= 0:
            raise ValueError('materialization_scope_invalid')
        scopes.append({
            'guildName': str(row['guild_name']).strip(),
            'country': str(row['country']).strip(),
            'rowCount': row_count,
            'totalIncome': f'{total_income:.6f}',
            'qualityStatus': 'passed',
            'consumable': True,
        })
    checksum = hashlib.sha256(canonical_json(scopes).encode('utf-8')).hexdigest()
    generation = hashlib.sha256(
        f"{data_date}:{state['materialized_at']}:{checksum}".encode('utf-8')
    ).hexdigest()[:20]
    event_id = f'linky:{data_date}:{generation}'
    return {
        'schemaVersion': 1,
        'eventType': 'linky.materialization.completed',
        'eventId': event_id,
        'dataDate': data_date,
        'runId': generation,
        'publishedAt': datetime.now(timezone.utc).isoformat(),
        'scopes': scopes,
        'checksum': checksum,
    }


def send_event(event: dict[str, Any], *, url: str, secret: str, attempts: int = 3) -> dict[str, Any]:
    body = canonical_json(event).encode('utf-8')
    last_error = ''
    for attempt in range(1, attempts + 1):
        timestamp = str(int(time.time()))
        signature = hmac.new(secret.encode('utf-8'), f'{timestamp}.'.encode('utf-8') + body, hashlib.sha256).hexdigest()
        req = request.Request(url, data=body, method='POST', headers={
            'Content-Type': 'application/json',
            'X-Linky-Event-Id': str(event['eventId']),
            'X-Linky-Timestamp': timestamp,
            'X-Linky-Signature': f'sha256={signature}',
            'User-Agent': 'mcn-linky-materialization-notifier/1',
        })
        try:
            with request.urlopen(req, timeout=8) as response:
                payload = json.loads(response.read(4096).decode('utf-8'))
                if response.status == 202 and payload.get('ok') is True:
                    return {
                        'ok': True,
                        'event_id': event['eventId'],
                        'duplicate': bool(payload.get('duplicate')),
                    }
                last_error = f'unexpected_response:{response.status}'
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = type(exc).__name__
        if attempt < attempts:
            time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f'notification_failed:{last_error}')


def _read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--analytics-path', default=os.getenv(
        'STREAMER_ANALYTICS_DB_PATH', str(ROOT / 'data/streamer_analytics.db')))
    parser.add_argument('--source-db-path', default=os.getenv('DB_PATH', str(ROOT / 'data/automation.db')))
    parser.add_argument('--secret-file', default='/etc/mcn-ai-automation/linky-materialization-webhook.secret')
    parser.add_argument('--state-path', default=str(ROOT / 'data/linky_materialization_notification_state.json'))
    parser.add_argument('--url', default=os.getenv(
        'LINKY_MATERIALIZATION_WEBHOOK_URL',
        'https://nova.hoyisr.com/api/internal/linky/materialization-complete'))
    args = parser.parse_args()
    state_path = Path(args.state_path)
    lock_path = state_path.with_suffix(f'{state_path.suffix}.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+', encoding='utf-8') as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'ok': True, 'skipped': 'notifier_already_running'}, sort_keys=True))
            return 0
        try:
            event = load_event(Path(args.analytics_path), Path(args.source_db_path))
        except ValueError as exc:
            print(json.dumps({'ok': True, 'skipped': str(exc)}, sort_keys=True))
            return 0
        state = _read_state(state_path)
        if state.get('event_id') == event['eventId'] and state.get('acknowledged_at'):
            print(json.dumps({'ok': True, 'skipped': 'already_acknowledged', 'event_id': event['eventId']}, sort_keys=True))
            return 0
        secret = Path(args.secret_file).read_text(encoding='utf-8').strip()
        if len(secret) < 32:
            raise RuntimeError('webhook_secret_invalid')
        result = send_event(event, url=args.url, secret=secret)
        _write_state(state_path, {
            'event_id': event['eventId'],
            'data_date': event['dataDate'],
            'acknowledged_at': datetime.now(timezone.utc).isoformat(),
            'duplicate': bool(result.get('duplicate')),
        })
        print(json.dumps(result, sort_keys=True))
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
