#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.production_ops import (
    DEFAULT_NOTIFICATION_COOLDOWN_SECONDS,
    DEFAULT_TRIGGER_COOLDOWN_SECONDS,
    FeishuNotifier,
    build_incidents,
    check_backend_health,
    env_default,
    fetch_json,
    format_lark_alert,
    load_json_state,
    maybe_restart,
    oldest_pending_at_iso,
    record_trigger,
    register_notification,
    requester_fingerprint,
    run_formal_approval_command,
    save_json_state,
    should_trigger_action,
    utc_now,
    utc_now_iso,
)


SERVICE_NAME = 'production-ops-daemon'


def _build_default_fresh_probe_cmd(registration_group: str) -> str:
    script = ROOT_DIR / 'scripts' / 'fresh_webjs_group_state.js'
    return f'node {script} {json.dumps(registration_group, ensure_ascii=False)}'


def _evaluate_release(api_base_url: str, registration_group: str, group_state: Dict[str, Any]) -> Dict[str, Any]:
    pending_count = max(int(group_state.get('pending_count') or 0), 0)
    oldest_pending_at = oldest_pending_at_iso(group_state)
    if pending_count <= 0 or not oldest_pending_at:
        return {
            'approval_type': 'registration_group',
            'registration_group': registration_group,
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
        f"{api_base_url.rstrip('/')}/api/ops/approval-batches/evaluate",
        method='POST',
        payload={
            'approval_type': 'registration_group',
            'registration_group': registration_group,
            'pending_count': pending_count,
            'oldest_pending_at': oldest_pending_at,
            'now': utc_now_iso(),
        },
        timeout=30.0,
    )


def _build_formal_approval_command(args: argparse.Namespace, approved_count: int) -> List[str]:
    cmd = [
        sys.executable,
        str(ROOT_DIR / 'scripts' / 'run_registration_group_formal_approval.py'),
        '--api-base-url', args.api_base_url,
        '--worker-base-url', args.worker_base_url,
        '--registration-group', args.registration_group,
        '--fresh-probe-cmd', args.fresh_probe_cmd,
        '--restart-cmd', args.worker_restart_cmd,
        '--backend-restart-cmd', args.backend_restart_cmd,
        '--restart-wait-seconds', str(args.restart_wait_seconds),
        '--area', args.area,
        '--remark', args.remark,
        '--approved-count', str(max(1, int(approved_count))),
        '--poll-interval-seconds', str(args.approval_poll_interval_seconds),
        '--poll-timeout-seconds', str(args.approval_poll_timeout_seconds),
        '--decided-by', args.decided_by,
        '--decided-by-name', args.decided_by_name,
    ]
    if args.auto_recover_worker:
        cmd.append('--auto-recover')
    if args.worker_event_log:
        cmd.extend(['--worker-event-log', args.worker_event_log])
    return cmd


def _formal_run_verified(payload: Dict[str, Any]) -> bool:
    result = (((payload.get('result') or {}).get('formal_run') or {}).get('result') or {})
    return bool(result.get('verified') and result.get('crm_recorded'))


def _trigger_cooldown_seconds(args: argparse.Namespace, release: Dict[str, Any]) -> int:
    reason_code = str(release.get('reason_code') or '').strip()
    if reason_code == 'timeout_flush':
        timeout_minutes = max(1, int(release.get('timeout_minutes') or 30))
        return timeout_minutes * 60
    return max(1, int(args.trigger_cooldown_seconds))


def _run_fresh_probe(fresh_probe_cmd: str, *, timeout: float = 120.0) -> Dict[str, Any]:
    if not str(fresh_probe_cmd or '').strip():
        raise RuntimeError('fresh_probe_cmd_missing')
    completed = subprocess.run(
        fresh_probe_cmd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or '').strip() or f'fresh_probe_exit_{completed.returncode}')
    payload = json.loads((completed.stdout or '').strip())
    if not isinstance(payload, dict):
        raise RuntimeError('fresh_probe_non_dict_response')
    return payload


def _group_state_signature(group_state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'group_id': str(group_state.get('group_id') or '').strip(),
        'group_name': str(group_state.get('group_name') or '').strip(),
        'pending_count': max(int(group_state.get('pending_count') or 0), 0),
        'member_count': max(int(group_state.get('member_count') or 0), 0),
        'fingerprint': requester_fingerprint(group_state),
    }


def _resolve_decision_group_state(worker_payload: Dict[str, Any], fresh_payload: Dict[str, Any]) -> Dict[str, Any]:
    worker_sig = _group_state_signature(worker_payload)
    fresh_sig = _group_state_signature(fresh_payload)
    mismatch_reasons: List[str] = []
    for key in ('group_id', 'pending_count', 'member_count', 'fingerprint'):
        if worker_sig.get(key) != fresh_sig.get(key):
            mismatch_reasons.append(key)
    return {
        'payload': fresh_payload,
        'source': 'fresh_probe',
        'worker_signature': worker_sig,
        'fresh_signature': fresh_sig,
        'mismatch': bool(mismatch_reasons),
        'mismatch_reasons': mismatch_reasons,
    }


def _session_state(state: Dict[str, Any], *, session_id: str, registration_group: str, checked_at: str) -> Dict[str, Any]:
    monitoring = state.setdefault('monitoring_session', {})
    current_session_id = str(monitoring.get('session_id') or '').strip()
    if session_id and current_session_id != session_id:
        monitoring.clear()
        monitoring.update({
            'session_id': session_id,
            'registration_group': registration_group,
            'started_at': checked_at,
            'startup_initial_batch_done': False,
            'startup_initial_batch_attempts': 0,
            'startup_initial_batch_max_retries': 2,
        })
    elif session_id and not monitoring:
        monitoring.update({
            'session_id': session_id,
            'registration_group': registration_group,
            'started_at': checked_at,
            'startup_initial_batch_done': False,
            'startup_initial_batch_attempts': 0,
            'startup_initial_batch_max_retries': 2,
        })
    monitoring.setdefault('startup_initial_batch_attempts', 0)
    monitoring.setdefault('startup_initial_batch_max_retries', 2)
    return monitoring


def run_cycle(args: argparse.Namespace, state: Dict[str, Any]) -> Dict[str, Any]:
    now = utc_now()
    cycle: Dict[str, Any] = {
        'service': SERVICE_NAME,
        'checked_at': now.isoformat(),
        'registration_group': args.registration_group,
    }

    backend_health = check_backend_health(args.api_base_url, timeout=args.health_timeout_seconds)
    if not backend_health.get('ok') and args.backend_restart_cmd:
        restart_result = maybe_restart(args.backend_restart_cmd, timeout=args.restart_command_timeout_seconds)
        backend_health['restart'] = restart_result
        time.sleep(max(1.0, float(args.restart_wait_seconds)))
        after_restart = check_backend_health(args.api_base_url, timeout=args.health_timeout_seconds)
        backend_health['after_restart'] = after_restart
        if after_restart.get('ok'):
            recovered_health = dict(after_restart)
            recovered_health['restart'] = restart_result
            recovered_health['recovered_after_restart'] = True
            cycle['backend_health_recovery'] = {
                'before_restart': backend_health,
                'after_restart': after_restart,
            }
            backend_health = recovered_health
    cycle['backend_health'] = backend_health
    if not backend_health.get('ok'):
        return cycle

    try:
        worker_payload = fetch_json(
            f"{args.worker_base_url.rstrip('/')}/group-state",
            method='POST',
            payload={'registration_group': args.registration_group},
            timeout=args.worker_timeout_seconds,
        )
        cycle['worker_state'] = {'ok': True, 'payload': worker_payload}
    except Exception as exc:
        cycle['worker_state'] = {'ok': False, 'error': str(exc)}
        return cycle

    try:
        fresh_payload = _run_fresh_probe(args.fresh_probe_cmd, timeout=args.command_timeout_seconds)
        decision_group = _resolve_decision_group_state(worker_payload, fresh_payload)
        cycle['fresh_probe'] = {'ok': True, 'payload': fresh_payload}
        cycle['decision_group_state'] = decision_group
    except Exception as exc:
        cycle['fresh_probe'] = {'ok': False, 'error': str(exc)}
        cycle['decision_group_state'] = {
            'source': 'fail_closed',
            'mismatch': False,
            'mismatch_reasons': [],
        }
        return cycle

    authoritative_payload = decision_group['payload']

    session_id = str(getattr(args, 'monitoring_session_id', '') or '').strip()
    monitoring_session = _session_state(
        state,
        session_id=session_id,
        registration_group=args.registration_group,
        checked_at=cycle['checked_at'],
    )
    pending_count = max(int(authoritative_payload.get('pending_count') or 0), 0)
    cycle['startup_initial_batch'] = {
        'session_id': session_id,
        'startup_initial_batch_done': bool(monitoring_session.get('startup_initial_batch_done')),
        'pending_count': pending_count,
        'attempts': int(monitoring_session.get('startup_initial_batch_attempts') or 0),
        'max_retries': int(monitoring_session.get('startup_initial_batch_max_retries') or 2),
    }
    if session_id and not bool(monitoring_session.get('startup_initial_batch_done')):
        attempts = int(monitoring_session.get('startup_initial_batch_attempts') or 0)
        max_retries = int(monitoring_session.get('startup_initial_batch_max_retries') or 2)
        max_attempts = max(1, max_retries + 1)
        attempt_results: List[Dict[str, Any]] = []
        current_payload = authoritative_payload
        current_pending_count = pending_count
        while current_pending_count > 0 and attempts < max_attempts:
            attempts += 1
            command = _build_formal_approval_command(args, approved_count=current_pending_count)
            result = run_formal_approval_command(command, timeout=args.command_timeout_seconds)
            ok = result.get('returncode') == 0 and _formal_run_verified(result)
            attempt_entry: Dict[str, Any] = {
                'attempt_number': attempts,
                'pending_count': current_pending_count,
                'command': command,
                'result': result.get('result'),
                'returncode': result.get('returncode'),
                'stderr': result.get('stderr'),
                'stdout': result.get('stdout'),
                'ok': ok,
            }
            attempt_results.append(attempt_entry)
            monitoring_session['startup_initial_batch_attempts'] = attempts
            monitoring_session['startup_initial_batch_pending_count'] = current_pending_count
            monitoring_session['startup_initial_batch_at'] = cycle['checked_at']
            if ok:
                monitoring_session['startup_initial_batch_done'] = True
                record_trigger(state, fingerprint=requester_fingerprint(current_payload), now=now)
                cycle['startup_initial_batch'] = {
                    'triggered': True,
                    'ok': True,
                    'session_id': session_id,
                    'pending_count': current_pending_count,
                    'attempts': attempts,
                    'max_retries': max_retries,
                    'attempt_results': attempt_results,
                }
                return cycle
            try:
                recheck_payload = _run_fresh_probe(args.fresh_probe_cmd, timeout=args.command_timeout_seconds)
                current_payload = recheck_payload
                current_pending_count = max(int(recheck_payload.get('pending_count') or 0), 0)
                attempt_entry['recheck_pending_count'] = current_pending_count
                attempt_entry['recheck_requester_fingerprint'] = requester_fingerprint(recheck_payload)
                attempt_entry['recheck_source'] = 'fresh_probe'
                if current_pending_count <= 0:
                    monitoring_session['startup_initial_batch_done'] = True
                    cycle['startup_initial_batch'] = {
                        'triggered': True,
                        'ok': True,
                        'session_id': session_id,
                        'pending_count': 0,
                        'attempts': attempts,
                        'max_retries': max_retries,
                        'attempt_results': attempt_results,
                        'cleared_after_recheck': True,
                    }
                    return cycle
            except Exception as exc:
                attempt_entry['recheck_error'] = str(exc)
                current_pending_count = 0
                break
        monitoring_session['startup_initial_batch_done'] = True
        cycle['startup_initial_batch'] = {
            'triggered': bool(attempt_results),
            'ok': False if attempt_results else True,
            'session_id': session_id,
            'pending_count': current_pending_count,
            'attempts': attempts,
            'max_retries': max_retries,
            'attempt_results': attempt_results,
            'retries_exhausted': bool(attempt_results and current_pending_count > 0),
        }
        return cycle

    try:
        release = _evaluate_release(args.api_base_url, args.registration_group, authoritative_payload)
        cycle['release_evaluation'] = {'ok': True, 'payload': release}
    except Exception as exc:
        cycle['release_evaluation'] = {'ok': False, 'error': str(exc)}
        return cycle

    fingerprint = requester_fingerprint(authoritative_payload)
    cycle['requester_fingerprint'] = fingerprint
    pending_count = max(int(authoritative_payload.get('pending_count') or 0), 0)
    ready = bool(release.get('ready')) and int(release.get('release_count') or 0) > 0
    trigger_cooldown_seconds = _trigger_cooldown_seconds(args, release)
    should_trigger = ready and should_trigger_action(
        state,
        fingerprint=fingerprint,
        now=now,
        cooldown_seconds=trigger_cooldown_seconds,
    )
    cycle['formal_approval'] = {
        'triggered': False,
        'ready': ready,
        'pending_count': pending_count,
        'fingerprint': fingerprint,
        'reason_code': str(release.get('reason_code') or ''),
        'trigger_cooldown_seconds': trigger_cooldown_seconds,
    }
    if not should_trigger:
        cycle['formal_approval']['cooldown_skip'] = bool(ready)
        return cycle

    if not ready:
        return cycle

    release_count = max(1, int(release.get('release_count') or 0))
    cycle['formal_approval']['release_count'] = release_count
    command = _build_formal_approval_command(args, approved_count=release_count)
    result = run_formal_approval_command(command, timeout=args.command_timeout_seconds)
    ok = result.get('returncode') == 0 and _formal_run_verified(result)
    cycle['formal_approval'] = {
        'triggered': True,
        'ok': ok,
        'fingerprint': fingerprint,
        'release_count': release_count,
        'command': command,
        'result': result.get('result'),
        'returncode': result.get('returncode'),
        'stderr': result.get('stderr'),
        'stdout': result.get('stdout'),
        'reason_code': str(release.get('reason_code') or ''),
        'trigger_cooldown_seconds': trigger_cooldown_seconds,
    }
    record_trigger(state, fingerprint=fingerprint, now=now)
    return cycle


def _build_notifier_from_args(args: argparse.Namespace) -> Optional[FeishuNotifier]:
    if not args.notify_enabled:
        return None
    if not args.feishu_app_id or not args.feishu_app_secret or not args.notify_chat_id:
        return None
    return FeishuNotifier(
        app_id=args.feishu_app_id,
        app_secret=args.feishu_app_secret,
        chat_id=args.notify_chat_id,
        domain=args.feishu_domain,
    )


def _notify_incidents(args: argparse.Namespace, state: Dict[str, Any], cycle: Dict[str, Any], incidents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    notifier = _build_notifier_from_args(args)
    now = datetime.fromisoformat(cycle['checked_at']) if cycle.get('checked_at') else utc_now()
    sent: List[Dict[str, Any]] = []
    for incident in incidents:
        dedupe_key = str(incident.get('dedupe_key') or incident.get('code') or 'incident')
        if not register_notification(state, dedupe_key=dedupe_key, now=now, cooldown_seconds=args.notify_cooldown_seconds):
            continue
        payload = {
            'dedupe_key': dedupe_key,
            'sent_at': now.isoformat(),
            'code': incident.get('code'),
            'severity': incident.get('severity'),
        }
        if notifier is None:
            payload['status'] = 'skipped_no_notifier'
            sent.append(payload)
            continue
        try:
            response = notifier.send_text(format_lark_alert(SERVICE_NAME, incident, cycle))
            payload['status'] = 'sent'
            payload['response'] = response
        except Exception as exc:
            payload['status'] = 'failed'
            payload['error'] = str(exc)
        sent.append(payload)
    return sent


def main() -> int:
    parser = argparse.ArgumentParser(description='Background production operator for registration-group live monitoring and formal approval.')
    parser.add_argument('--api-base-url', default='http://127.0.0.1:8011')
    parser.add_argument('--worker-base-url', default='http://127.0.0.1:8787')
    parser.add_argument('--registration-group', default='🇮🇩3️⃣7️⃣Grup Registrasi Resmi Linky 💎')
    parser.add_argument('--fresh-probe-cmd', default='')
    parser.add_argument('--worker-restart-cmd', default=str(ROOT_DIR / 'scripts' / 'restart_registration_group_webjs_worker.sh'))
    parser.add_argument('--backend-restart-cmd', default=str(ROOT_DIR / 'scripts' / 'ensure_registration_group_backend.sh'))
    parser.add_argument('--worker-event-log', default='')
    parser.add_argument('--interval-seconds', type=float, default=20.0)
    parser.add_argument('--run-once', action='store_true')
    parser.add_argument('--state-path', default=str(ROOT_DIR / 'data' / 'production_ops_daemon_state.json'))
    parser.add_argument('--status-path', default=str(ROOT_DIR / 'data' / 'production_ops_daemon_status.json'))
    parser.add_argument('--notify-enabled', action='store_true', default=True)
    parser.add_argument('--notify-disabled', dest='notify_enabled', action='store_false')
    parser.add_argument('--notify-chat-id', default=env_default('FEISHU_HOME_CHANNEL'))
    parser.add_argument('--feishu-app-id', default=env_default('FEISHU_APP_ID'))
    parser.add_argument('--feishu-app-secret', default=env_default('FEISHU_APP_SECRET'))
    parser.add_argument('--feishu-domain', default=env_default('FEISHU_DOMAIN', 'lark') or 'lark')
    parser.add_argument('--notify-cooldown-seconds', type=int, default=DEFAULT_NOTIFICATION_COOLDOWN_SECONDS)
    parser.add_argument('--trigger-cooldown-seconds', type=int, default=DEFAULT_TRIGGER_COOLDOWN_SECONDS)
    parser.add_argument('--command-timeout-seconds', type=float, default=240.0)
    parser.add_argument('--health-timeout-seconds', type=float, default=10.0)
    parser.add_argument('--worker-timeout-seconds', type=float, default=60.0)
    parser.add_argument('--restart-command-timeout-seconds', type=float, default=120.0)
    parser.add_argument('--restart-wait-seconds', type=float, default=8.0)
    parser.add_argument('--area', default='Indonesia')
    parser.add_argument('--remark', default='production auto approval daemon')
    parser.add_argument('--approved-count', type=int, default=1)
    parser.add_argument('--approval-poll-interval-seconds', type=float, default=0.1)
    parser.add_argument('--approval-poll-timeout-seconds', type=float, default=60.0)
    parser.add_argument('--decided-by', default='Hermes')
    parser.add_argument('--decided-by-name', default='Song Yuqi')
    parser.add_argument('--auto-recover-worker', action='store_true', default=True)
    parser.add_argument('--no-auto-recover-worker', dest='auto_recover_worker', action='store_false')
    parser.add_argument('--monitoring-session-id', default='')
    args = parser.parse_args()

    if not args.fresh_probe_cmd:
        args.fresh_probe_cmd = _build_default_fresh_probe_cmd(args.registration_group)
    if not args.worker_event_log:
        args.worker_event_log = str(ROOT_DIR / 'webjs-approval-worker' / 'logs' / 'registration_group_webjs_worker.jsonl')

    state_path = Path(args.state_path).expanduser().resolve()
    status_path = Path(args.status_path).expanduser().resolve()
    state = load_json_state(state_path)

    while True:
        cycle: Dict[str, Any]
        try:
            cycle = run_cycle(args, state)
        except Exception as exc:
            cycle = {
                'service': SERVICE_NAME,
                'checked_at': utc_now_iso(),
                'registration_group': args.registration_group,
                'cycle_error': str(exc),
            }
        incidents = build_incidents(cycle)
        notifications = _notify_incidents(args, state, cycle, incidents)
        cycle['incidents'] = incidents
        cycle['notifications'] = notifications
        save_json_state(status_path, cycle)
        save_json_state(state_path, state)
        print(json.dumps({
            'checked_at': cycle.get('checked_at'),
            'pending_incidents': [item.get('code') for item in incidents],
            'notified': [item.get('code') for item in incidents if any(n.get('code') == item.get('code') and n.get('status') == 'sent' for n in notifications)],
            'formal_triggered': bool((cycle.get('formal_approval') or {}).get('triggered')),
            'formal_ok': (cycle.get('formal_approval') or {}).get('ok'),
        }, ensure_ascii=False), flush=True)
        if args.run_once:
            return 0
        time.sleep(max(1.0, float(args.interval_seconds)))


if __name__ == '__main__':
    raise SystemExit(main())
