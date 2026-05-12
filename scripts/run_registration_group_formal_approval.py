#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.registration_group_formal_run import execute_formal_registration_group_approval
from app.registration_group_preflight import evaluate_registration_group_webjs_preflight
from scripts.registration_group_webjs_preflight import (
    DEFAULT_WORKER_EVENT_LOG,
    _collect_snapshot,
    _resolve_expected_auth_strategy,
)


def _fetch_json(url: str, *, method: str = 'GET', payload: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    internal_token = str(os.getenv('AUTH_INTERNAL_TOKEN') or '').strip()
    if internal_token and '/api/ops/' in str(url or ''):
        headers['x-ops-internal-token'] = internal_token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode('utf-8'))
    if not isinstance(body, dict):
        raise RuntimeError(f'unexpected non-dict response from {url}')
    return body


def _run_preflight(
    *,
    api_base_url: str,
    worker_base_url: str,
    registration_group: str,
    fresh_probe_cmd: str,
    worker_event_log: Path,
    expected_auth_strategy: str,
) -> Dict[str, Any]:
    snapshot = _collect_snapshot(api_base_url, worker_base_url, registration_group, fresh_probe_cmd, worker_event_log)
    worker_health = snapshot.get('worker_health') if isinstance(snapshot.get('worker_health'), dict) else {}
    approval_client = worker_health.get('approval_client') if isinstance(worker_health.get('approval_client'), dict) else {}
    requested_expected_auth_strategy = str(expected_auth_strategy or '').strip()
    inferred_expected_auth_strategy = (
        str(approval_client.get('auth_strategy') or '').strip()
        or str(worker_health.get('auth_strategy') or '').strip()
    )
    effective_expected_auth_strategy = requested_expected_auth_strategy or inferred_expected_auth_strategy or _resolve_expected_auth_strategy(requested_expected_auth_strategy)
    report = evaluate_registration_group_webjs_preflight(
        registration_group=registration_group,
        worker_health=snapshot['worker_health'],
        worker_warmup=snapshot['worker_warmup'],
        worker_group_state=snapshot['worker_group_state'],
        fresh_group_state=snapshot['fresh_group_state'],
        last_verified_group_state=snapshot.get('last_verified_group_state') or {},
        expected_auth_strategy=effective_expected_auth_strategy,
    )
    if snapshot.get('fresh_probe_skipped') and not report.get('ok'):
        snapshot = _collect_snapshot(api_base_url, worker_base_url, registration_group, fresh_probe_cmd, worker_event_log, allow_fast_probe_skip=False)
        report = evaluate_registration_group_webjs_preflight(
            registration_group=registration_group,
            worker_health=snapshot['worker_health'],
            worker_warmup=snapshot['worker_warmup'],
            worker_group_state=snapshot['worker_group_state'],
            fresh_group_state=snapshot['fresh_group_state'],
            last_verified_group_state=snapshot.get('last_verified_group_state') or {},
            expected_auth_strategy=effective_expected_auth_strategy,
        )
    return {'preflight': report, 'snapshot': snapshot}


def _ensure_backend_healthy(
    *,
    api_base_url: str,
    fetch_json: Callable[..., Dict[str, Any]],
    restart_cmd: Optional[str],
    sleep_fn: Callable[[float], None] = time.sleep,
    health_timeout: float = 10.0,
    restart_wait_seconds: float = 8.0,
) -> Dict[str, Any]:
    health_url = f"{api_base_url.rstrip('/')}/health"
    attempts = []

    def _try_health(stage: str) -> Optional[Dict[str, Any]]:
        try:
            payload = fetch_json(health_url, timeout=health_timeout)
            attempts.append({'stage': stage, 'ok': True, 'payload': payload})
            return payload
        except Exception as exc:
            attempts.append({'stage': stage, 'ok': False, 'error': str(exc)})
            return None

    payload = _try_health('initial')
    if payload is not None:
        return {'ok': True, 'restarted': False, 'health': payload, 'attempts': attempts}

    if not restart_cmd:
        return {'ok': False, 'restarted': False, 'health': None, 'attempts': attempts, 'reason': 'backend_unhealthy_and_restart_cmd_missing'}

    completed = subprocess.run(restart_cmd, shell=True, capture_output=True, text=True)
    restart_result = {
        'command': restart_cmd,
        'returncode': completed.returncode,
        'stdout': completed.stdout,
        'stderr': completed.stderr,
    }
    if completed.returncode != 0:
        return {'ok': False, 'restarted': True, 'health': None, 'attempts': attempts, 'restart': restart_result, 'reason': 'backend_restart_failed'}

    sleep_fn(max(1.0, restart_wait_seconds))
    payload = _try_health('after_restart')
    if payload is not None:
        return {'ok': True, 'restarted': True, 'health': payload, 'attempts': attempts, 'restart': restart_result}
    return {'ok': False, 'restarted': True, 'health': None, 'attempts': attempts, 'restart': restart_result, 'reason': 'backend_unhealthy_after_restart'}


def main() -> int:
    parser = argparse.ArgumentParser(description='Run registration-group formal approval only after passing preflight, with optional stale-session recovery.')
    parser.add_argument('--api-base-url', default='http://127.0.0.1:8011')
    parser.add_argument('--worker-base-url', default='')
    parser.add_argument('--registration-group', required=True)
    parser.add_argument('--fresh-probe-cmd', required=True)
    parser.add_argument('--restart-cmd')
    parser.add_argument('--auto-recover', action='store_true')
    parser.add_argument('--restart-wait-seconds', type=float, default=8.0)
    parser.add_argument('--area', default='Indonesia')
    parser.add_argument('--remark', default='formal approval with preflight gate')
    parser.add_argument('--approved-count', type=int, default=1)
    parser.add_argument('--poll-interval-seconds', type=float, default=0.1)
    parser.add_argument('--poll-timeout-seconds', type=float, default=60.0)
    parser.add_argument('--decided-by', default='Hermes')
    parser.add_argument('--decided-by-name', default='Song Yuqi')
    parser.add_argument('--worker-event-log', default=str(DEFAULT_WORKER_EVENT_LOG))
    parser.add_argument('--backend-restart-cmd', default='./scripts/ensure_registration_group_backend.sh')
    parser.add_argument('--expected-auth-strategy', default='')
    args = parser.parse_args()

    worker_event_log = Path(args.worker_event_log).expanduser().resolve()
    expected_auth_strategy = str(args.expected_auth_strategy or '').strip()

    output: Dict[str, Any] = {
        'backend_health_before': _ensure_backend_healthy(
            api_base_url=args.api_base_url,
            fetch_json=_fetch_json,
            restart_cmd=args.backend_restart_cmd,
            restart_wait_seconds=args.restart_wait_seconds,
        )
    }
    if not output['backend_health_before']['ok']:
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 2

    output['preflight_before'] = _run_preflight(
        api_base_url=args.api_base_url,
        worker_base_url=args.worker_base_url,
        registration_group=args.registration_group,
        fresh_probe_cmd=args.fresh_probe_cmd,
        worker_event_log=worker_event_log,
        expected_auth_strategy=expected_auth_strategy,
    )
    preflight = output['preflight_before']['preflight']

    if not preflight['ok'] and preflight.get('stale_session_detected') and args.auto_recover:
        if not args.restart_cmd:
            output['recovery'] = {'attempted': False, 'status': 'skipped', 'reason': 'restart_cmd_missing'}
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 2
        import subprocess
        completed = subprocess.run(args.restart_cmd, shell=True, capture_output=True, text=True)
        output['recovery'] = {
            'attempted': True,
            'status': 'ok' if completed.returncode == 0 else 'failed',
            'returncode': completed.returncode,
            'stdout': completed.stdout,
            'stderr': completed.stderr,
            'command': args.restart_cmd,
        }
        if completed.returncode != 0:
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 2
        time.sleep(max(1.0, args.restart_wait_seconds))
        output['backend_health_after_recovery'] = _ensure_backend_healthy(
            api_base_url=args.api_base_url,
            fetch_json=_fetch_json,
            restart_cmd=args.backend_restart_cmd,
            restart_wait_seconds=args.restart_wait_seconds,
        )
        if not output['backend_health_after_recovery']['ok']:
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 2
        output['preflight_after_recovery'] = _run_preflight(
            api_base_url=args.api_base_url,
            worker_base_url=args.worker_base_url,
            registration_group=args.registration_group,
            fresh_probe_cmd=args.fresh_probe_cmd,
            worker_event_log=worker_event_log,
            expected_auth_strategy=expected_auth_strategy,
        )
        preflight = output['preflight_after_recovery']['preflight']

    if not preflight['ok']:
        output['final'] = {'executed': False, 'reason': 'preflight_failed'}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 2

    output['backend_health_before_formal_run'] = output.get('backend_health_after_recovery') or output['backend_health_before']
    if not output['backend_health_before_formal_run']['ok']:
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 2

    output['formal_run'] = execute_formal_registration_group_approval(
        api_base_url=args.api_base_url,
        registration_group=args.registration_group,
        area=args.area,
        remark=args.remark,
        fetch_json=_fetch_json,
        decided_by=args.decided_by,
        decided_by_name=args.decided_by_name,
        approved_count=args.approved_count,
        expected_group_state=(output['preflight_after_recovery']['snapshot']['worker_group_state'] if output.get('preflight_after_recovery') else output['preflight_before']['snapshot']['worker_group_state']),
        poll_interval_seconds=args.poll_interval_seconds,
        poll_timeout_seconds=args.poll_timeout_seconds,
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    result = ((output['formal_run'].get('final_status') or {}).get('result') or {})
    return 0 if result.get('verified') and result.get('crm_recorded') else 2


if __name__ == '__main__':
    sys.exit(main())
