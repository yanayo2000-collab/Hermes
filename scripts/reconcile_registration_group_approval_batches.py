#!/usr/bin/env python3
"""Reconcile registration-group approval member retention rows to CRM batch writes.

Fallback rule:
- If member retention has selected requester IDs for an approval_run_id,
- but there is no successful registration_group_approval_batch_runs row / CRM sync,
- and a fresh WhatsApp pending-list read shows those requester IDs are no longer pending,
then replay the registration-group approval batch write to CRM with the original approval_run_id.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT_DIR / 'data' / 'automation.db'
DEFAULT_RUNTIME_DIR = ROOT_DIR / 'data' / 'whatsapp_approval_worker_runtimes'
DEFAULT_BACKEND_URL = os.getenv('MCN_BACKEND_URL', 'http://127.0.0.1:8011').rstrip('/')


def _json_loads(value: Any) -> Dict[str, Any]:
    try:
        obj = json.loads(value or '{}')
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _post_json(url: str, payload: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json; charset=utf-8'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode('utf-8')
    return json.loads(body or '{}')


def _parse_dt(value: str) -> Optional[datetime]:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _successful_approval_results(raw_result: Dict[str, Any]) -> Set[str]:
    result_ids: Set[str] = set()
    for item in raw_result.get('approval_results') or []:
        if not isinstance(item, dict):
            continue
        requester_id = str(item.get('requesterId') or item.get('requester_id') or '').strip()
        if not requester_id:
            continue
        error = item.get('error')
        ok = error in (None, '', 0, '0')
        if not ok:
            try:
                ok = int(error) == 409
            except Exception:
                ok = False
        if ok:
            result_ids.add(requester_id)
    return result_ids


def _load_runtime_urls(runtime_dir: Path) -> List[str]:
    urls: List[str] = []
    if not runtime_dir.exists():
        return urls
    for path in sorted(runtime_dir.glob('*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        base_url = str(data.get('base_url') or '').strip().rstrip('/')
        if base_url and base_url not in urls:
            urls.append(base_url)
    return urls


def _fresh_pending_ids(runtime_urls: Sequence[str], registration_group: str) -> Dict[str, Any]:
    errors: List[str] = []
    for base_url in runtime_urls:
        try:
            payload = _post_json(f'{base_url}/group-state', {'registration_group': registration_group}, timeout=25.0)
        except Exception as exc:
            errors.append(f'{base_url}: {type(exc).__name__}: {exc}')
            continue
        requester_ids = set(str(x or '').strip() for x in (payload.get('requester_ids') or []) if str(x or '').strip())
        return {
            'ok': True,
            'base_url': base_url,
            'pending_count': payload.get('pending_count'),
            'member_count': payload.get('member_count'),
            'group_name': payload.get('group_name'),
            'requester_ids': requester_ids,
        }
    return {'ok': False, 'errors': errors}


def _candidate_runs(conn: sqlite3.Connection, *, since_hours: int, limit: int) -> List[Dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(int(since_hours), 1))
    rows = conn.execute(
        """
        SELECT m.approval_run_id,
               MIN(m.created_at) AS first_member_created_at,
               COUNT(*) AS member_rows,
               GROUP_CONCAT(m.requester_id, ',') AS requester_ids
        FROM registration_group_approval_batch_members m
        LEFT JOIN registration_group_approval_batch_runs r
               ON r.approval_run_id = m.approval_run_id AND r.status = 'success'
        WHERE r.approval_run_id IS NULL
        GROUP BY m.approval_run_id
        ORDER BY first_member_created_at DESC
        LIMIT ?
        """,
        (max(int(limit), 1),),
    ).fetchall()
    candidates: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        created = _parse_dt(str(item.get('first_member_created_at') or ''))
        if created and created < cutoff:
            continue
        candidates.append(item)
    return candidates


def _find_ingress(conn: sqlite3.Connection, approval_run_id: str) -> Optional[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT event_id, payload, result_snapshot, created_at FROM ingress_events WHERE ingress_type = 'registration_group_approval_decision' ORDER BY rowid DESC LIMIT 1000"
    ).fetchall()
    for row in rows:
        payload = _json_loads(row['payload'])
        result = _json_loads(row['result_snapshot'])
        if str(payload.get('approval_run_id') or result.get('approval_run_id') or '').strip() == approval_run_id:
            return {
                'event_id': row['event_id'],
                'payload': payload,
                'result': result,
                'created_at': row['created_at'],
            }
    return None


def reconcile_once(*, db_path: Path, runtime_dir: Path, backend_url: str, since_hours: int, limit: int, dry_run: bool) -> Dict[str, Any]:
    runtime_urls = _load_runtime_urls(runtime_dir)
    out: Dict[str, Any] = {'checked': 0, 'reconciled': 0, 'skipped': [], 'results': []}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        for candidate in _candidate_runs(conn, since_hours=since_hours, limit=limit):
            approval_run_id = str(candidate.get('approval_run_id') or '').strip()
            out['checked'] += 1
            if not approval_run_id:
                continue
            ingress = _find_ingress(conn, approval_run_id)
            if not ingress:
                out['skipped'].append({'approval_run_id': approval_run_id, 'reason': 'ingress_not_found'})
                continue
            payload = ingress['payload']
            result = ingress['result']
            raw_result = result.get('raw_result') if isinstance(result.get('raw_result'), dict) else {}
            selected_ids = set(str(x or '').strip() for x in str(candidate.get('requester_ids') or '').split(',') if str(x or '').strip())
            success_ids = _successful_approval_results(raw_result)
            if success_ids:
                selected_ids = selected_ids.intersection(success_ids) or selected_ids
            if not selected_ids:
                out['skipped'].append({'approval_run_id': approval_run_id, 'reason': 'selected_ids_empty'})
                continue
            registration_group = str(payload.get('registration_group') or result.get('registration_group') or '').strip()
            if not registration_group:
                out['skipped'].append({'approval_run_id': approval_run_id, 'reason': 'registration_group_missing'})
                continue
            state = _fresh_pending_ids(runtime_urls, registration_group)
            if not state.get('ok'):
                out['skipped'].append({'approval_run_id': approval_run_id, 'reason': 'fresh_state_failed', 'state': state})
                continue
            pending_ids = state.get('requester_ids') or set()
            overlap = sorted(selected_ids.intersection(pending_ids))
            if overlap:
                out['skipped'].append({'approval_run_id': approval_run_id, 'reason': 'selected_ids_still_pending', 'overlap': overlap})
                continue
            target_member = result.get('target_member') if isinstance(result.get('target_member'), dict) else {}
            raw_group_name = str(raw_result.get('group_name') or state.get('group_name') or '').strip()
            approved_count = len(selected_ids)
            request_payload = {
                'registration_group': registration_group,
                'registration_group_name': raw_group_name or str(payload.get('registration_group_name') or '').strip(),
                'approved_count': approved_count,
                'approved_by': payload.get('decided_by') or 'Hermes',
                'approved_by_name': payload.get('decided_by_name') or 'Song Yuqi',
                'source_platform': payload.get('source_platform'),
                'source_campaign': payload.get('source_campaign'),
                'source_adset': payload.get('source_adset'),
                'source_ad': ' '.join(x for x in [str(target_member.get('name') or '').strip(), str(target_member.get('phone_raw') or '').strip()] if x) or None,
                'approved_at': result.get('approved_at') or payload.get('decided_at') or ingress.get('created_at'),
                'area': payload.get('area') or 'Indonesia',
                'remark': (str(payload.get('remark') or 'registration approval fallback').strip() + ' [verified by pending requester id reconciliation]').strip(),
                'approval_run_id': approval_run_id,
            }
            if dry_run:
                response = {'dry_run': True}
            else:
                response = _post_json(f'{backend_url}/api/registration-groups/approval-batches', request_payload, timeout=45.0)
            success = response.get('crm_sync_status') == 'success' or response.get('dry_run') is True
            if success and not dry_run:
                out['reconciled'] += 1
            out['results'].append({
                'approval_run_id': approval_run_id,
                'selected_count': len(selected_ids),
                'pending_count': state.get('pending_count'),
                'overlap': overlap,
                'crm_sync_status': response.get('crm_sync_status'),
                'dry_run': dry_run,
            })
    finally:
        conn.close()
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=str(DEFAULT_DB))
    parser.add_argument('--runtime-dir', default=str(DEFAULT_RUNTIME_DIR))
    parser.add_argument('--backend-url', default=DEFAULT_BACKEND_URL)
    parser.add_argument('--since-hours', type=int, default=48)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    result = reconcile_once(
        db_path=Path(args.db),
        runtime_dir=Path(args.runtime_dir),
        backend_url=str(args.backend_url).rstrip('/'),
        since_hours=args.since_hours,
        limit=args.limit,
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=list))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
