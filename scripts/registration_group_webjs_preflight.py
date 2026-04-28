#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.registration_group_preflight import evaluate_registration_group_webjs_preflight

DEFAULT_WORKER_EVENT_LOG = ROOT_DIR / 'webjs-approval-worker' / 'logs' / 'registration_group_webjs_worker.jsonl'


def _fetch_json(url: str, *, method: str = 'GET', payload: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
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


def _run_json_command(command: str, *, timeout: float = 120.0) -> Dict[str, Any]:
    completed = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(f'command failed ({completed.returncode}): {command}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}')
    stdout = completed.stdout.strip()
    start = stdout.find('{')
    end = stdout.rfind('}')
    if start < 0 or end < start:
        raise RuntimeError(f'command did not emit JSON object: {command}\nstdout:\n{stdout}')
    body = json.loads(stdout[start:end + 1])
    if not isinstance(body, dict):
        raise RuntimeError(f'command emitted non-dict JSON: {command}')
    return body


def _load_last_verified_group_state(registration_group: str, *, event_log_path: Path) -> Dict[str, Any]:
    if not event_log_path.exists():
        return {}
    try:
        lines = event_log_path.read_text(encoding='utf-8').splitlines()
    except Exception:
        return {}
    for raw_line in reversed(lines):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except Exception:
            continue
        if entry.get('kind') != 'approve':
            continue
        if str(entry.get('registration_group') or '').strip() != str(registration_group or '').strip():
            continue
        if not bool(entry.get('verified')):
            continue
        return {
            'approval_run_id': entry.get('approval_run_id'),
            'pending_count': entry.get('pending_after'),
            'member_count': entry.get('member_count_after'),
            'requester_ids': [],
            'target_requester_id': entry.get('target_requester_id'),
            'source': 'worker_event_log',
        }
    return {}


def _collect_snapshot(api_base_url: str, worker_base_url: str, registration_group: str, fresh_probe_cmd: str, worker_event_log: Path, *, allow_fast_probe_skip: bool = True) -> Dict[str, Any]:
    encoded_group = urllib.parse.urlencode({'registration_group': registration_group})
    worker_health = _fetch_json(f'{worker_base_url.rstrip("/")}/health')
    approval_client = worker_health.get('approval_client') if isinstance(worker_health.get('approval_client'), dict) else {}
    worker_ready = (
        bool(worker_health.get('ready'))
        and bool(worker_health.get('authenticated'))
        and str(worker_health.get('status') or '').strip().lower() in {'warm', 'authenticated'}
        and bool(approval_client.get('ready'))
        and bool(approval_client.get('authenticated'))
        and str(approval_client.get('status') or '').strip().lower() in {'warm', 'authenticated'}
    )
    worker_warmup = {
        **worker_health,
        'warmup_outcome': 'ready' if worker_ready else 'unknown',
        'qr_available': bool(worker_health.get('last_qr')),
        'base_url': worker_base_url.rstrip('/'),
        'timeout_seconds': 35.0,
        'warmed': worker_ready,
    }
    if not worker_ready:
        worker_warmup = _fetch_json(f'{api_base_url.rstrip("/")}/api/ops/registration-group-approval-executor-warmup', method='POST', payload={})
        approval_client = worker_warmup.get('approval_client') if isinstance(worker_warmup.get('approval_client'), dict) else approval_client
        worker_ready = (
            bool(worker_warmup.get('ready'))
            and bool(worker_warmup.get('authenticated'))
            and str(worker_warmup.get('status') or '').strip().lower() in {'warm', 'authenticated'}
            and bool(approval_client.get('ready'))
            and bool(approval_client.get('authenticated'))
            and str(approval_client.get('status') or '').strip().lower() in {'warm', 'authenticated'}
        )
    worker_group_state = _fetch_json(
        f'{api_base_url.rstrip("/")}/api/ops/registration-group-approval-executor-group-state?{encoded_group}'
    )
    fast_probe_skipped = False
    if allow_fast_probe_skip and worker_ready and 'dedicated_approval_client' in (worker_health.get('supports') or []):
        fresh_group_state = json.loads(json.dumps(worker_group_state, ensure_ascii=False))
        fresh_group_state['probe_mode'] = 'skipped_using_dual_client_worker_state'
        fast_probe_skipped = True
    else:
        fresh_group_state = _run_json_command(fresh_probe_cmd)
    return {
        'api_health': _fetch_json(f'{api_base_url.rstrip("/")}/health'),
        'worker_health': worker_health,
        'worker_warmup': worker_warmup,
        'worker_group_state': worker_group_state,
        'fresh_group_state': fresh_group_state,
        'fresh_probe_skipped': fast_probe_skipped,
        'last_verified_group_state': _load_last_verified_group_state(registration_group, event_log_path=worker_event_log),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Preflight check for registration-group webjs bridge and optional stale-session recovery.')
    parser.add_argument('--api-base-url', default='http://127.0.0.1:8011')
    parser.add_argument('--worker-base-url', default='http://127.0.0.1:8787')
    parser.add_argument('--registration-group', required=True)
    parser.add_argument('--fresh-probe-cmd', required=True, help='Shell command that prints JSON fresh-session group state.')
    parser.add_argument('--restart-cmd', help='Shell command to restart the 8787 worker with the correct Chrome-profile-copy env.')
    parser.add_argument('--auto-recover', action='store_true')
    parser.add_argument('--restart-wait-seconds', type=float, default=8.0)
    parser.add_argument('--worker-event-log', default=str(DEFAULT_WORKER_EVENT_LOG))
    args = parser.parse_args()

    worker_event_log = Path(args.worker_event_log).expanduser().resolve()
    snapshot = _collect_snapshot(args.api_base_url, args.worker_base_url, args.registration_group, args.fresh_probe_cmd, worker_event_log)
    report = evaluate_registration_group_webjs_preflight(
        registration_group=args.registration_group,
        worker_health=snapshot['worker_health'],
        worker_warmup=snapshot['worker_warmup'],
        worker_group_state=snapshot['worker_group_state'],
        fresh_group_state=snapshot['fresh_group_state'],
        last_verified_group_state=snapshot['last_verified_group_state'],
    )
    if snapshot.get('fresh_probe_skipped') and not report.get('ok'):
        snapshot = _collect_snapshot(args.api_base_url, args.worker_base_url, args.registration_group, args.fresh_probe_cmd, worker_event_log, allow_fast_probe_skip=False)
        report = evaluate_registration_group_webjs_preflight(
            registration_group=args.registration_group,
            worker_health=snapshot['worker_health'],
            worker_warmup=snapshot['worker_warmup'],
            worker_group_state=snapshot['worker_group_state'],
            fresh_group_state=snapshot['fresh_group_state'],
            last_verified_group_state=snapshot['last_verified_group_state'],
        )
    output: Dict[str, Any] = {'preflight': report, 'snapshot': snapshot}

    if report['stale_session_detected'] and args.auto_recover:
        if not args.restart_cmd:
            output['recovery'] = {
                'attempted': False,
                'status': 'skipped',
                'reason': 'restart_cmd_missing',
            }
        else:
            completed = subprocess.run(args.restart_cmd, shell=True, capture_output=True, text=True)
            output['recovery'] = {
                'attempted': True,
                'status': 'ok' if completed.returncode == 0 else 'failed',
                'returncode': completed.returncode,
                'stdout': completed.stdout,
                'stderr': completed.stderr,
                'command': args.restart_cmd,
            }
            if completed.returncode == 0:
                time.sleep(max(1.0, args.restart_wait_seconds))
                post_snapshot = _collect_snapshot(args.api_base_url, args.worker_base_url, args.registration_group, args.fresh_probe_cmd, worker_event_log)
                post_report = evaluate_registration_group_webjs_preflight(
                    registration_group=args.registration_group,
                    worker_health=post_snapshot['worker_health'],
                    worker_warmup=post_snapshot['worker_warmup'],
                    worker_group_state=post_snapshot['worker_group_state'],
                    fresh_group_state=post_snapshot['fresh_group_state'],
                    last_verified_group_state=post_snapshot['last_verified_group_state'],
                )
                output['post_recovery'] = {
                    'preflight': post_report,
                    'snapshot': post_snapshot,
                }
                print(json.dumps(output, ensure_ascii=False, indent=2))
                return 0 if post_report['ok'] else 2

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 2


if __name__ == '__main__':
    sys.exit(main())
