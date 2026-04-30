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
    build_success_notifications,
    check_backend_health,
    env_default,
    fetch_json,
    formal_run_result,
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


def _build_formal_approval_command(
    args: argparse.Namespace,
    approved_count: int,
    *,
    registration_group: Optional[str] = None,
    worker_base_url: Optional[str] = None,
    fresh_probe_cmd: Optional[str] = None,
    area: Optional[str] = None,
    remark: Optional[str] = None,
) -> List[str]:
    target_group = str(registration_group or args.registration_group or '').strip()
    target_worker_base_url = str(worker_base_url or args.worker_base_url or '').strip()
    target_fresh_probe_cmd = str(fresh_probe_cmd or args.fresh_probe_cmd or '').strip()
    target_area = str(area or args.area or '').strip() or 'Indonesia'
    target_remark = str(remark or args.remark or '').strip() or 'production auto approval daemon'
    cmd = [
        sys.executable,
        str(ROOT_DIR / 'scripts' / 'run_registration_group_formal_approval.py'),
        '--api-base-url', args.api_base_url,
        '--worker-base-url', target_worker_base_url,
        '--registration-group', target_group,
        '--fresh-probe-cmd', target_fresh_probe_cmd,
        '--restart-cmd', args.worker_restart_cmd,
        '--backend-restart-cmd', args.backend_restart_cmd,
        '--restart-wait-seconds', str(args.restart_wait_seconds),
        '--area', target_area,
        '--remark', target_remark,
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
    result = formal_run_result(payload)
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


def _target_session_config_payload(target: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'registration_group': str(target.get('registration_group') or '').strip(),
        'group_name': str(target.get('group_name') or '').strip(),
        'worker_base_url': str(target.get('worker_base_url') or '').strip(),
        'account_key': str(target.get('account_key') or '').strip(),
        'binding_link': str(target.get('binding_link') or '').strip(),
        'binding_group_name': str(target.get('binding_group_name') or '').strip(),
        'area': str(target.get('area') or '').strip(),
        'approval_count_threshold': int(target.get('approval_count_threshold') or 0),
        'approval_timeout_minutes': int(target.get('approval_timeout_minutes') or 0),
        'auto_recover_worker': bool(target.get('auto_recover_worker')),
        'schedule_runtime': target.get('schedule_runtime') or {},
        'schedule_windows': target.get('schedule_windows') or [],
    }


def _target_session_config_fingerprint(target: Dict[str, Any]) -> str:
    return json.dumps(_target_session_config_payload(target), ensure_ascii=False, sort_keys=True)


def _target_session_key(session_id: str, target: Dict[str, Any]) -> str:
    registration_group = str(target.get('registration_group') or '').strip()
    return f"{session_id or 'default'}::{registration_group}::{_target_session_config_fingerprint(target)}"


def _session_state(
    state: Dict[str, Any],
    *,
    session_id: str,
    registration_group: str,
    checked_at: str,
    target: Dict[str, Any],
) -> Dict[str, Any]:
    session_key = _target_session_key(session_id, target)
    monitoring_sessions = state.setdefault('monitoring_sessions', {})
    monitoring = monitoring_sessions.get(session_key)
    if not isinstance(monitoring, dict):
        monitoring = {
            'session_key': session_key,
            'session_id': session_id,
            'registration_group': registration_group,
            'started_at': checked_at,
            'target_config_fingerprint': _target_session_config_fingerprint(target),
            'startup_initial_batch_done': False,
            'startup_initial_batch_attempts': 0,
            'startup_initial_batch_max_retries': 2,
        }
        monitoring_sessions[session_key] = monitoring
    monitoring.setdefault('startup_initial_batch_attempts', 0)
    monitoring.setdefault('startup_initial_batch_max_retries', 2)
    state['monitoring_session'] = monitoring
    return monitoring


def _ordered_cycle_targets(monitor_target: Dict[str, Any], fallback_target: Dict[str, Any]) -> List[Dict[str, Any]]:
    selected_target = monitor_target.get('selected')
    candidates = list(monitor_target.get('candidates') or [])
    ordered: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in [selected_target, *candidates]:
        if not isinstance(candidate, dict):
            continue
        normalized = _normalize_monitor_target(candidate, str(candidate.get('worker_base_url') or ''))
        if not normalized:
            continue
        key = _target_session_key('', normalized)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(normalized)
    allow_fallback = bool(monitor_target.get('allow_fallback', True))
    return ordered or ([fallback_target] if allow_fallback and fallback_target else [])


def _normalize_monitor_target(target: Dict[str, Any], fallback_worker_base_url: str = '') -> Optional[Dict[str, Any]]:
    registration_group = str(target.get('registration_group') or '').strip()
    if not registration_group:
        return None
    worker_base_url = str(target.get('worker_base_url') or fallback_worker_base_url or '').strip()
    group_name = str(target.get('group_name') or registration_group).strip() or registration_group
    area = str(target.get('area') or '').strip() or 'Indonesia'
    return {
        'registration_group': registration_group,
        'group_name': group_name,
        'worker_base_url': worker_base_url,
        'account_key': str(target.get('account_key') or '').strip() or None,
        'account_name': str(target.get('account_name') or '').strip() or None,
        'binding_link': str(target.get('binding_link') or '').strip() or None,
        'binding_group_name': str(target.get('binding_group_name') or '').strip() or None,
        'area': area,
        'approval_count_threshold': int(target.get('approval_count_threshold') or 0),
        'approval_timeout_minutes': int(target.get('approval_timeout_minutes') or 0),
        'auto_recover_worker': bool(target.get('auto_recover_worker')),
        'schedule_runtime': target.get('schedule_runtime') or {},
        'schedule_windows': target.get('schedule_windows') or [],
        'source': str(target.get('source') or '').strip() or 'fallback_config',
    }


def _resolve_monitor_target(args: argparse.Namespace) -> Dict[str, Any]:
    fallback = _normalize_monitor_target({
        'registration_group': args.registration_group,
        'group_name': args.registration_group,
        'worker_base_url': args.worker_base_url,
        'area': args.area,
        'source': 'fallback_config',
    }, args.worker_base_url)
    try:
        payload = fetch_json(f"{args.api_base_url.rstrip('/')}/api/ops/whatsapp-approval-accounts", timeout=30.0)
    except Exception as exc:
        return {
            'selected': fallback,
            'candidates': [fallback] if fallback else [],
            'selection_reason': f'fallback_accounts_api_error:{exc}',
            'allow_fallback': True,
        }

    rows = payload.get('rows') or []
    candidates: List[Dict[str, Any]] = []
    configured_bindings: List[Dict[str, Any]] = []
    normalized_fallback_group = str(args.registration_group or '').strip().lower()

    def _pick_preferred_target(targets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not targets:
            return None
        if normalized_fallback_group:
            for candidate in targets:
                if str(candidate.get('registration_group') or '').strip().lower() == normalized_fallback_group:
                    return candidate
                if str(candidate.get('binding_link') or '').strip().lower() == normalized_fallback_group:
                    return candidate
                if str(candidate.get('group_name') or '').strip().lower() == normalized_fallback_group:
                    return candidate
        return targets[0]

    for row in rows:
        if str(row.get('responsible_type') or '').strip().lower() != 'registration_group':
            continue
        if not bool(row.get('enabled')):
            continue
        runtime_state = row.get('runtime_state') if isinstance(row.get('runtime_state'), dict) else {}
        worker_base_url = str(runtime_state.get('base_url') or '').strip()
        runtime_active = bool(runtime_state.get('active')) and bool(worker_base_url)
        account_key = str(row.get('account_key') or '').strip()
        account_name = str(row.get('account_name') or '').strip()
        row_area = str(row.get('area') or '').strip() or 'Indonesia'
        for binding in row.get('group_link_bindings') or []:
            if not isinstance(binding, dict):
                continue
            if binding.get('enabled') is False:
                continue
            schedule_runtime = binding.get('schedule_runtime') if isinstance(binding.get('schedule_runtime'), dict) else {}
            if schedule_runtime.get('configured') and not bool(schedule_runtime.get('active_now')):
                continue
            registration_group = (
                str(binding.get('registration_group') or '').strip()
                or str(binding.get('group_id') or '').strip()
                or str(binding.get('link') or '').strip()
                or str(binding.get('group_name') or '').strip()
            )
            normalized = _normalize_monitor_target({
                'registration_group': registration_group,
                'group_name': str(binding.get('group_name') or '').strip() or registration_group,
                'worker_base_url': worker_base_url,
                'account_key': account_key,
                'account_name': account_name,
                'binding_link': str(binding.get('link') or '').strip(),
                'binding_group_name': str(binding.get('group_name') or '').strip(),
                'area': str(binding.get('area') or '').strip() or row_area,
                'approval_count_threshold': binding.get('approval_count_threshold'),
                'approval_timeout_minutes': binding.get('approval_timeout_minutes'),
                'auto_recover_worker': binding.get('auto_recover_worker'),
                'schedule_runtime': schedule_runtime,
                'schedule_windows': binding.get('schedule_windows') or [],
                'source': 'account_binding',
            })
            if not normalized:
                continue
            configured_bindings.append(normalized)
            if runtime_active and normalized.get('worker_base_url'):
                candidates.append(normalized)

    if not candidates:
        if configured_bindings:
            return {
                'selected': _pick_preferred_target(configured_bindings),
                'candidates': configured_bindings,
                'selection_reason': 'configured_binding_runtime_unavailable',
                'allow_fallback': False,
            }
        return {
            'selected': fallback,
            'candidates': [fallback] if fallback else [],
            'selection_reason': 'fallback_no_active_monitored_binding',
            'allow_fallback': True,
        }

    selected = _pick_preferred_target(candidates)
    return {
        'selected': selected,
        'candidates': candidates,
        'selection_reason': 'account_binding_active' if selected and selected.get('source') == 'account_binding' else 'fallback_config',
        'allow_fallback': True,
    }


def _fresh_probe_cmd_for_target(args: argparse.Namespace, registration_group: str) -> str:
    target_group = str(registration_group or '').strip()
    if target_group and target_group != str(args.registration_group or '').strip():
        return _build_default_fresh_probe_cmd(target_group)
    return str(args.fresh_probe_cmd or '').strip() or _build_default_fresh_probe_cmd(target_group)


def _official_group_ready_fingerprint(rows: List[Dict[str, Any]]) -> str:
    normalized_rows = []
    for row in rows:
        normalized_rows.append(
            f"{str(row.get('registration_group') or '').strip()}:{int(row.get('pending_count') or 0)}:{int(row.get('release_count') or 0)}:{str(row.get('reason_code') or '').strip()}"
        )
    return 'official-group-batch:' + '|'.join(sorted(normalized_rows))


def _official_group_trigger_cooldown_seconds(args: argparse.Namespace, rows: List[Dict[str, Any]]) -> int:
    timeout_rows = [max(1, int(row.get('timeout_minutes') or 30)) * 60 for row in rows if str(row.get('reason_code') or '').strip() == 'timeout_flush']
    if timeout_rows:
        return min(timeout_rows)
    return max(1, int(args.trigger_cooldown_seconds))


def _dispatch_ready_official_group_batches(args: argparse.Namespace, state: Dict[str, Any], cycle: Dict[str, Any], *, now: datetime) -> None:
    dispatch_state: Dict[str, Any] = {
        'triggered': False,
        'ok': None,
        'ready_group_count': 0,
    }
    cycle['official_group_dispatch'] = dispatch_state
    try:
        queue = fetch_json(f"{args.api_base_url.rstrip('/')}/api/ops/approval-batch-queue", timeout=30.0)
    except Exception as exc:
        dispatch_state['ok'] = False
        dispatch_state['error'] = f'queue_fetch_failed:{exc}'
        return
    ready_groups = [row for row in list(queue.get('official_groups') or []) if bool(row.get('ready')) and int(row.get('release_count') or 0) > 0]
    dispatch_state['ready_group_count'] = len(ready_groups)
    dispatch_state['ready_groups'] = ready_groups
    if not ready_groups:
        dispatch_state['ok'] = True
        return
    try:
        executor_health = fetch_json(f"{args.api_base_url.rstrip('/')}/api/ops/official-group-approval-executor-health", timeout=30.0)
    except Exception as exc:
        dispatch_state['ok'] = False
        dispatch_state['error'] = f'executor_health_failed:{exc}'
        return
    dispatch_state['executor_health'] = executor_health
    supports = {str(item).strip() for item in (executor_health.get('supports') or []) if str(item).strip()}
    if not bool(executor_health.get('configured')) or str(executor_health.get('status') or '').strip().lower() != 'healthy' or 'approve' not in supports:
        dispatch_state['ok'] = False
        dispatch_state['blocked'] = 'executor_unready'
        return
    fingerprint = _official_group_ready_fingerprint(ready_groups)
    trigger_cooldown_seconds = _official_group_trigger_cooldown_seconds(args, ready_groups)
    dispatch_state['fingerprint'] = fingerprint
    dispatch_state['trigger_cooldown_seconds'] = trigger_cooldown_seconds
    if not should_trigger_action(state, fingerprint=fingerprint, now=now, cooldown_seconds=trigger_cooldown_seconds):
        dispatch_state['ok'] = True
        dispatch_state['cooldown_skip'] = True
        return
    payload = {
        'decided_at': cycle.get('checked_at') or utc_now_iso(),
        'decided_by': args.decided_by,
        'decided_by_name': args.decided_by_name,
    }
    try:
        result = fetch_json(
            f"{args.api_base_url.rstrip('/')}/api/ops/official-group-approval-batches/run-ready",
            method='POST',
            payload=payload,
            timeout=max(30.0, float(args.command_timeout_seconds)),
        )
    except Exception as exc:
        dispatch_state['triggered'] = True
        dispatch_state['ok'] = False
        dispatch_state['error'] = str(exc)
        return
    record_trigger(state, fingerprint=fingerprint, now=now)
    dispatch_state['triggered'] = True
    dispatch_state['ok'] = True
    dispatch_state['result'] = result


def _run_registration_group_cycle(
    args: argparse.Namespace,
    state: Dict[str, Any],
    target: Dict[str, Any],
    *,
    now: datetime,
) -> Dict[str, Any]:
    target_registration_group = str(target.get('registration_group') or args.registration_group or '').strip()
    target_group_name = str(target.get('group_name') or target_registration_group).strip() or target_registration_group
    target_worker_base_url = str(target.get('worker_base_url') or '').strip()
    if not target_worker_base_url and str(target.get('source') or '').strip() == 'fallback_config':
        target_worker_base_url = str(args.worker_base_url or '').strip()
    target_fresh_probe_cmd = _fresh_probe_cmd_for_target(args, target_registration_group)
    target_area = str(target.get('area') or args.area or '').strip() or 'Indonesia'
    cycle: Dict[str, Any] = {
        'registration_group': target_registration_group,
        'monitor_target': {
            **target,
            'group_name': target_group_name,
            'worker_base_url': target_worker_base_url,
            'fresh_probe_cmd_source': 'target_specific_default' if target_registration_group != str(args.registration_group or '').strip() else 'configured_or_default',
        },
    }

    if not target_worker_base_url:
        cycle['worker_state'] = {
            'ok': False,
            'error': 'worker_base_url_missing_for_selected_binding',
        }
        cycle['decision_group_state'] = {
            'source': 'fail_closed',
            'mismatch': False,
            'mismatch_reasons': ['worker_base_url_missing'],
        }
        return cycle

    try:
        worker_payload = fetch_json(
            f"{target_worker_base_url.rstrip('/')}/group-state",
            method='POST',
            payload={'registration_group': target_registration_group},
            timeout=args.worker_timeout_seconds,
        )
        cycle['worker_state'] = {'ok': True, 'payload': worker_payload}
    except Exception as exc:
        cycle['worker_state'] = {'ok': False, 'error': str(exc)}
        return cycle

    use_worker_state_directly = (
        cycle.get('monitor_target', {}).get('source') == 'account_binding'
        and target_worker_base_url != str(args.worker_base_url or '').strip()
    )

    if use_worker_state_directly:
        cycle['fresh_probe'] = {
            'ok': True,
            'skipped': True,
            'reason': 'dedicated_runtime_worker_state',
            'payload': worker_payload,
        }
        decision_group = {
            'payload': worker_payload,
            'source': 'worker_state',
            'worker_signature': _group_state_signature(worker_payload),
            'fresh_signature': _group_state_signature(worker_payload),
            'mismatch': False,
            'mismatch_reasons': [],
        }
        cycle['decision_group_state'] = decision_group
    else:
        try:
            fresh_payload = _run_fresh_probe(target_fresh_probe_cmd, timeout=args.command_timeout_seconds)
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
        registration_group=target_registration_group,
        checked_at=now.isoformat(),
        target=cycle['monitor_target'],
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
            command = _build_formal_approval_command(
                args,
                approved_count=current_pending_count,
                registration_group=target_registration_group,
                worker_base_url=target_worker_base_url,
                fresh_probe_cmd=target_fresh_probe_cmd,
                area=target_area,
                remark=f"production auto approval daemon · {target_group_name}",
            )
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
            monitoring_session['startup_initial_batch_at'] = now.isoformat()
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
                if use_worker_state_directly:
                    recheck_payload = fetch_json(
                        f"{target_worker_base_url.rstrip('/')}/group-state",
                        method='POST',
                        payload={'registration_group': target_registration_group},
                        timeout=args.worker_timeout_seconds,
                    )
                    attempt_entry['recheck_source'] = 'worker_state'
                else:
                    recheck_payload = _run_fresh_probe(target_fresh_probe_cmd, timeout=args.command_timeout_seconds)
                    attempt_entry['recheck_source'] = 'fresh_probe'
                current_payload = recheck_payload
                current_pending_count = max(int(recheck_payload.get('pending_count') or 0), 0)
                attempt_entry['recheck_pending_count'] = current_pending_count
                attempt_entry['recheck_requester_fingerprint'] = requester_fingerprint(recheck_payload)
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
        release = _evaluate_release(args.api_base_url, target_registration_group, authoritative_payload)
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
    command = _build_formal_approval_command(
        args,
        approved_count=release_count,
        registration_group=target_registration_group,
        worker_base_url=target_worker_base_url,
        fresh_probe_cmd=target_fresh_probe_cmd,
        area=target_area,
        remark=f"production auto approval daemon · {target_group_name}",
    )
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


def run_cycle(args: argparse.Namespace, state: Dict[str, Any]) -> Dict[str, Any]:
    now = utc_now()
    fallback_target = _normalize_monitor_target({
        'registration_group': args.registration_group,
        'group_name': args.registration_group,
        'worker_base_url': args.worker_base_url,
        'area': args.area,
        'source': 'fallback_config',
    }, args.worker_base_url) or {
        'registration_group': args.registration_group,
        'group_name': args.registration_group,
        'worker_base_url': args.worker_base_url,
        'area': args.area,
        'source': 'fallback_config',
    }
    monitor_target = _resolve_monitor_target(args)
    ordered_targets = _ordered_cycle_targets(monitor_target, fallback_target)
    primary_target = ordered_targets[0] if ordered_targets else (monitor_target.get('selected') or fallback_target)
    cycle: Dict[str, Any] = {
        'service': SERVICE_NAME,
        'checked_at': now.isoformat(),
        'registration_group': str(primary_target.get('registration_group') or args.registration_group or '').strip(),
        'monitor_target': primary_target,
        'monitor_targets': {
            'selection_reason': monitor_target.get('selection_reason'),
            'candidates': monitor_target.get('candidates') or [],
            'active_count': len(ordered_targets),
            'allow_fallback': bool(monitor_target.get('allow_fallback', True)),
        },
        'registration_group_cycles': [],
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

    target_cycles = [_run_registration_group_cycle(args, state, target, now=now) for target in ordered_targets]
    cycle['registration_group_cycles'] = target_cycles
    if target_cycles:
        primary_cycle = target_cycles[0]
        for key in (
            'registration_group',
            'monitor_target',
            'worker_state',
            'fresh_probe',
            'decision_group_state',
            'startup_initial_batch',
            'release_evaluation',
            'requester_fingerprint',
            'formal_approval',
        ):
            if key in primary_cycle:
                cycle[key] = primary_cycle[key]

    _dispatch_ready_official_group_batches(args, state, cycle, now=now)
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
        success_notifications = build_success_notifications(cycle)
        notifications = _notify_incidents(args, state, cycle, [*incidents, *success_notifications])
        cycle['incidents'] = incidents
        cycle['success_notifications'] = success_notifications
        cycle['notifications'] = notifications
        save_json_state(status_path, cycle)
        save_json_state(state_path, state)
        print(json.dumps({
            'checked_at': cycle.get('checked_at'),
            'pending_incidents': [item.get('code') for item in incidents],
            'success_notifications': [item.get('code') for item in success_notifications],
            'notified': [item.get('code') for item in [*incidents, *success_notifications] if any(n.get('code') == item.get('code') and n.get('status') == 'sent' for n in notifications)],
            'formal_triggered': bool((cycle.get('formal_approval') or {}).get('triggered')),
            'formal_ok': (cycle.get('formal_approval') or {}).get('ok'),
        }, ensure_ascii=False), flush=True)
        if args.run_once:
            return 0
        time.sleep(max(1.0, float(args.interval_seconds)))


if __name__ == '__main__':
    raise SystemExit(main())
