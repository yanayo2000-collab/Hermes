#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = ROOT / 'registration_group_production_release_latest.json'
API_ROOT = 'http://127.0.0.1:8011'
REGISTRATION_GROUP = '🇮🇩3️⃣7️⃣Grup Registrasi Resmi Linky 💎'
POLL_INTERVAL_SECONDS = 15
STATUS_POLL_INTERVAL_SECONDS = 2
STATUS_POLL_TIMEOUT_SECONDS = 180
AREA = 'Indonesia'
DECIDED_BY = 'Hermes'
DECIDED_BY_NAME = 'Song Yuqi'


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_json(url: str, *, method: str = 'GET', payload: Optional[Dict[str, Any]] = None, timeout: float = 60.0) -> Dict[str, Any]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode('utf-8'))
    if not isinstance(body, dict):
        raise RuntimeError(f'unexpected non-dict response from {url}')
    return body


def write_output(payload: Dict[str, Any]) -> None:
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def worker_group_state() -> Dict[str, Any]:
    return fetch_json(
        f'{API_ROOT}/api/ops/registration-group-approval-executor-group-state?registration_group={urllib.parse.quote(REGISTRATION_GROUP, safe="")}',
        timeout=90.0,
    )


def oldest_pending_at_iso(group_state: Dict[str, Any]) -> Optional[str]:
    requesters = group_state.get('requesters') or []
    values = []
    if isinstance(requesters, list):
        for item in requesters:
            if not isinstance(item, dict):
                continue
            iso = str(item.get('requestedAtIso') or '').strip()
            if iso:
                values.append(iso)
                continue
            raw = item.get('requestedAtUnix')
            try:
                if raw is not None:
                    values.append(datetime.fromtimestamp(int(raw), tz=timezone.utc).isoformat())
            except Exception:
                continue
    if not values:
        return None
    return min(values)


def evaluate_release(group_state: Dict[str, Any]) -> Dict[str, Any]:
    pending_count = max(int(group_state.get('pending_count') or 0), 0)
    oldest_pending_at = oldest_pending_at_iso(group_state)
    if pending_count <= 0 or not oldest_pending_at:
        return {
            'approval_type': 'registration_group',
            'registration_group': REGISTRATION_GROUP,
            'pending_count': pending_count,
            'oldest_pending_at': oldest_pending_at,
            'ready': False,
            'release_count': 0,
            'reason_code': 'waiting_for_batch',
            'batch_size': 30,
            'timeout_minutes': 30,
            'elapsed_minutes': 0,
        }
    return fetch_json(
        f'{API_ROOT}/api/ops/approval-batches/evaluate',
        method='POST',
        payload={
            'approval_type': 'registration_group',
            'registration_group': REGISTRATION_GROUP,
            'pending_count': pending_count,
            'oldest_pending_at': oldest_pending_at,
            'now': utc_now_iso(),
        },
        timeout=30.0,
    )


def trigger_release(release_count: int, reason_code: str) -> Dict[str, Any]:
    decided_at = utc_now_iso()
    accepted = fetch_json(
        f'{API_ROOT}/api/registration-groups/approval-decisions',
        method='POST',
        payload={
            'registration_group': REGISTRATION_GROUP,
            'decision': 'approve',
            'decided_at': decided_at,
            'decided_by': DECIDED_BY,
            'decided_by_name': DECIDED_BY_NAME,
            'approved_count': max(1, int(release_count)),
            'area': AREA,
            'remark': f'production_auto_release:{reason_code}',
            'force_immediate': False,
        },
        timeout=60.0,
    )
    approval_run_id = str(accepted.get('approval_run_id') or '').strip()
    if not approval_run_id:
        return {'accepted': accepted, 'status': None}
    deadline = time.time() + STATUS_POLL_TIMEOUT_SECONDS
    last_status: Dict[str, Any] = {}
    while time.time() < deadline:
        last_status = fetch_json(
            f'{API_ROOT}/api/registration-groups/approval-decisions/{approval_run_id}',
            timeout=30.0,
        )
        status = str(last_status.get('status') or '').strip().lower()
        if status not in {'queued', 'processing', 'accepted', 'pending'}:
            return {'accepted': accepted, 'status': last_status}
        time.sleep(STATUS_POLL_INTERVAL_SECONDS)
    return {'accepted': accepted, 'status': last_status, 'timed_out': True}


def _requester_fingerprint(group_state: Dict[str, Any]) -> str:
    requesters = group_state.get('requesters') or []
    parts = []
    if isinstance(requesters, list):
        for item in requesters:
            if not isinstance(item, dict):
                continue
            rid = str(item.get('requesterId') or '').strip()
            ts = str(item.get('requestedAtUnix') or item.get('requestedAtIso') or '').strip()
            if rid:
                parts.append(f'{rid}@{ts}')
    if parts:
        return '|'.join(sorted(parts))
    requester_ids = group_state.get('requester_ids') or []
    if isinstance(requester_ids, list):
        return '|'.join(sorted(str(x).strip() for x in requester_ids if str(x).strip()))
    return ''


def main() -> int:
    anchor_utc = utc_now_iso()
    last_trigger_fingerprint = ''
    last_trigger_at_epoch = 0.0
    print(json.dumps({
        'status': 'watching',
        'registration_group': REGISTRATION_GROUP,
        'output_path': str(OUTPUT_PATH),
        'anchor_utc': anchor_utc,
        'poll_interval_seconds': POLL_INTERVAL_SECONDS,
    }, ensure_ascii=False), flush=True)
    while True:
        checked_at = utc_now_iso()
        try:
            group_state = worker_group_state()
            release = evaluate_release(group_state)
            fingerprint = _requester_fingerprint(group_state)
            snapshot = {
                'status': 'waiting',
                'anchor_utc': anchor_utc,
                'checked_at_utc': checked_at,
                'registration_group': REGISTRATION_GROUP,
                'group_state': group_state,
                'release': release,
                'requester_fingerprint': fingerprint,
            }
            write_output(snapshot)
            print(json.dumps({
                'status': 'poll',
                'checked_at_utc': checked_at,
                'pending_count': group_state.get('pending_count'),
                'member_count': group_state.get('member_count'),
                'ready': release.get('ready'),
                'release_count': release.get('release_count'),
                'reason_code': release.get('reason_code'),
                'elapsed_minutes': release.get('elapsed_minutes'),
            }, ensure_ascii=False), flush=True)
            should_trigger = bool(release.get('ready')) and int(release.get('release_count') or 0) > 0
            same_fingerprint_recent = fingerprint and fingerprint == last_trigger_fingerprint and (time.time() - last_trigger_at_epoch) < 120
            if should_trigger and not same_fingerprint_recent:
                result = {
                    'status': 'triggering_release',
                    'anchor_utc': anchor_utc,
                    'checked_at_utc': checked_at,
                    'registration_group': REGISTRATION_GROUP,
                    'group_state': group_state,
                    'release': release,
                    'requester_fingerprint': fingerprint,
                }
                write_output(result)
                approval = trigger_release(int(release.get('release_count') or 0), str(release.get('reason_code') or 'ready'))
                result['approval'] = approval
                result['status'] = 'completed'
                write_output(result)
                last_trigger_fingerprint = fingerprint
                last_trigger_at_epoch = time.time()
                print(json.dumps({
                    'status': 'completed',
                    'approval_run_id': (approval.get('accepted') or {}).get('approval_run_id'),
                    'verified': ((approval.get('status') or {}).get('verified') if isinstance(approval.get('status'), dict) else None),
                    'crm_recorded': ((approval.get('status') or {}).get('crm_recorded') if isinstance(approval.get('status'), dict) else None),
                    'result_code': ((approval.get('status') or {}).get('result_code') if isinstance(approval.get('status'), dict) else None),
                }, ensure_ascii=False), flush=True)
            elif should_trigger and same_fingerprint_recent:
                skipped_payload = {
                    'status': 'cooldown_skip',
                    'anchor_utc': anchor_utc,
                    'checked_at_utc': checked_at,
                    'registration_group': REGISTRATION_GROUP,
                    'group_state': group_state,
                    'release': release,
                    'requester_fingerprint': fingerprint,
                }
                write_output(skipped_payload)
                print(json.dumps({'status': 'cooldown_skip', 'checked_at_utc': checked_at, 'pending_count': group_state.get('pending_count')}, ensure_ascii=False), flush=True)
        except Exception as exc:
            error_payload = {
                'status': 'error',
                'anchor_utc': anchor_utc,
                'checked_at_utc': checked_at,
                'registration_group': REGISTRATION_GROUP,
                'error': str(exc),
            }
            write_output(error_payload)
            print(json.dumps(error_payload, ensure_ascii=False), flush=True)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == '__main__':
    raise SystemExit(main())
