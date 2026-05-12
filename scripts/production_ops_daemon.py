#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.production_ops import (
    DEFAULT_NOTIFICATION_COOLDOWN_SECONDS,
    DEFAULT_TRIGGER_COOLDOWN_SECONDS,
    FeishuNotifier,
    NOTIFICATION_POLICY_BY_CODE,
    build_incidents,
    build_success_notifications,
    check_backend_health,
    env_default,
    expand_notify_profile_targets,
    fetch_json,
    formal_run_payload,
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
from scripts.webjs_temp_cleanup import build_stat_map, collect_cleanup_targets, execute_cleanup, get_protected_pid_set, get_ps_rows
from app.registration_group_truth import build_truth_state


SERVICE_NAME = 'production-ops-daemon'


def _build_default_fresh_probe_cmd(registration_group: str) -> str:
    script = ROOT_DIR / 'scripts' / 'fresh_webjs_group_state.js'
    return f'node {script} {json.dumps(registration_group, ensure_ascii=False)}'


def _build_default_independent_truth_probe_cmd(group_name: str) -> str:
    normalized_group_name = str(group_name or '').strip()
    if not normalized_group_name:
        return ''
    script = ROOT_DIR / 'scripts' / 'live_truth_group_state.py'
    preferred_candidates = [
        ROOT_DIR / '.venv' / 'bin' / 'python',
        ROOT_DIR / '.venv-live-truth' / 'bin' / 'python',
    ]
    preferred_python = next((candidate for candidate in preferred_candidates if candidate.exists()), None)
    python_executable = str(preferred_python) if preferred_python is not None else (sys.executable or 'python3')
    python_bin = shlex.quote(python_executable)
    script_path = shlex.quote(str(script))
    return f"{python_bin} {script_path} --group-name {shlex.quote(normalized_group_name)}"



def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed



def _maybe_auto_cleanup_temp_profiles(args: argparse.Namespace, state: Dict[str, Any], *, now: datetime) -> Dict[str, Any]:
    enabled = bool(getattr(args, 'temp_cleanup_enabled', True))
    interval_seconds = max(float(getattr(args, 'temp_cleanup_interval_seconds', 600.0) or 600.0), 0.0)
    min_age_hours = max(float(getattr(args, 'temp_cleanup_min_age_hours', 1.0) or 1.0), 0.0)
    temp_cleanup_state = state.setdefault('temp_cleanup', {})
    if not isinstance(temp_cleanup_state, dict):
        temp_cleanup_state = {}
        state['temp_cleanup'] = temp_cleanup_state

    payload: Dict[str, Any] = {
        'enabled': enabled,
        'interval_seconds': interval_seconds,
        'min_age_hours': min_age_hours,
    }
    if not enabled:
        payload['skipped'] = True
        payload['reason'] = 'disabled'
        return payload

    last_checked_at = _parse_iso_datetime(temp_cleanup_state.get('last_checked_at'))
    if interval_seconds > 0 and last_checked_at is not None:
        elapsed_seconds = max((now - last_checked_at).total_seconds(), 0.0)
        if elapsed_seconds < interval_seconds:
            payload['skipped'] = True
            payload['reason'] = 'interval_not_due'
            payload['last_checked_at'] = last_checked_at.isoformat()
            payload['next_check_after_seconds'] = max(int(interval_seconds - elapsed_seconds), 0)
            return payload

    ps_rows = get_ps_rows()
    protected_pids = get_protected_pid_set(
        protected_ports=[8011, 55801],
        protected_cmd_substrings=['production_ops_daemon.py', 'src/server.js'],
        ps_rows=ps_rows,
    )
    temp_root = Path(tempfile.gettempdir()).expanduser()
    stat_map = build_stat_map(temp_root)
    targets = collect_cleanup_targets(
        temp_root=temp_root,
        ps_rows=ps_rows,
        protected_pids=protected_pids,
        min_age_hours=min_age_hours,
        now=now.timestamp(),
        stat_map=stat_map,
    )
    summary = {
        'target_count': len(targets),
        'target_size_kb': sum(int(item.get('size_kb') or 0) for item in targets),
    }
    payload.update({
        'checked_at': now.isoformat(),
        'protected_pid_count': len(protected_pids),
        'summary': summary,
        'targets': targets,
    })
    temp_cleanup_state['last_checked_at'] = now.isoformat()
    temp_cleanup_state['last_summary'] = summary
    temp_cleanup_state['last_target_dirs'] = [str(item.get('user_data_dir') or '').strip() for item in targets if str(item.get('user_data_dir') or '').strip()]

    if not targets:
        payload['applied'] = False
        payload['reason'] = 'no_stale_targets'
        return payload

    cleanup = execute_cleanup(
        targets=targets,
        temp_root=temp_root,
        min_age_hours=min_age_hours,
        now=now.timestamp(),
        stat_map=stat_map,
        ps_rows_provider=get_ps_rows,
    )
    payload['applied'] = True
    payload['cleanup'] = cleanup
    temp_cleanup_state['last_applied_at'] = now.isoformat()
    temp_cleanup_state['last_cleanup'] = cleanup
    return payload



def _cycle_next_boundary(now: datetime, timeout_minutes: int) -> datetime:
    interval_seconds = max(int(timeout_minutes or 0), 1) * 60
    local_tz = timezone(timedelta(hours=8))
    localized_now = now.astimezone(local_tz)
    local_day_start = localized_now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_seconds = int((localized_now - local_day_start).total_seconds())
    next_boundary_seconds = ((elapsed_seconds // interval_seconds) + 1) * interval_seconds
    next_boundary_local = local_day_start + timedelta(seconds=next_boundary_seconds)
    return next_boundary_local.astimezone(now.tzinfo or timezone.utc)


def _cycle_window(now: datetime, timeout_minutes: int, *, cycle_anchor_at: Optional[str] = None) -> Dict[str, datetime]:
    interval_seconds = max(int(timeout_minutes or 0), 1) * 60
    anchor = now
    if cycle_anchor_at:
        try:
            parsed_anchor = datetime.fromisoformat(str(cycle_anchor_at).replace('Z', '+00:00'))
            if parsed_anchor.tzinfo is None:
                parsed_anchor = parsed_anchor.replace(tzinfo=timezone.utc)
            anchor = parsed_anchor
        except Exception:
            anchor = now
    if anchor > now:
        anchor = now
    elapsed_seconds = max(int((now - anchor).total_seconds()), 0)
    completed_cycles = elapsed_seconds // interval_seconds
    cycle_start = anchor + timedelta(seconds=completed_cycles * interval_seconds)
    cycle_end = cycle_start + timedelta(seconds=interval_seconds)
    return {
        'anchor_at': anchor,
        'completed_cycles_since_anchor': completed_cycles,
        'cycle_started_at': cycle_start,
        'cycle_ends_at': cycle_end,
    }


def _store_cycle_anchor(state: Dict[str, Any], *, bucket_name: str, at: datetime, keys: List[Optional[str]]) -> None:
    bucket = state.setdefault(bucket_name, {})
    if not isinstance(bucket, dict):
        bucket = {}
        state[bucket_name] = bucket
    anchor_text = at.isoformat()
    for raw_key in keys:
        normalized_key = str(raw_key or '').strip()
        if normalized_key:
            bucket[normalized_key] = anchor_text


def _store_cycle_anchor_text(state: Dict[str, Any], *, bucket_name: str, anchor_text: str, keys: List[Optional[str]]) -> None:
    bucket = state.setdefault(bucket_name, {})
    if not isinstance(bucket, dict):
        bucket = {}
        state[bucket_name] = bucket
    normalized_anchor = str(anchor_text or '').strip()
    if not normalized_anchor:
        return
    for raw_key in keys:
        normalized_key = str(raw_key or '').strip()
        if normalized_key:
            bucket[normalized_key] = normalized_anchor


def _evaluate_release(api_base_url: str, registration_group: str, group_state: Dict[str, Any], *, batch_size: Optional[int] = None, timeout_minutes: Optional[int] = None, cycle_anchor_at: Optional[str] = None) -> Dict[str, Any]:
    pending_count = max(int(group_state.get('pending_count') or 0), 0)
    oldest_pending_at = oldest_pending_at_iso(group_state)
    resolved_batch_size = max(1, int(batch_size or 30))
    resolved_timeout_minutes = max(1, int(timeout_minutes or 30))
    now = datetime.fromisoformat(utc_now_iso().replace('Z', '+00:00'))
    if pending_count <= 0:
        cycle_anchor_text = None
        completed_cycles_since_anchor = 0
        if cycle_anchor_at:
            cycle_window = _cycle_window(now, resolved_timeout_minutes, cycle_anchor_at=cycle_anchor_at)
            cycle_anchor_text = cycle_window['anchor_at'].isoformat()
            completed_cycles_since_anchor = max(int(cycle_window.get('completed_cycles_since_anchor') or 0), 0)
            cycle_end = cycle_window['cycle_ends_at']
            cycle_start = cycle_window['cycle_started_at']
        else:
            cycle_end = _cycle_next_boundary(now, resolved_timeout_minutes)
            cycle_start = cycle_end - timedelta(minutes=resolved_timeout_minutes)
        remaining_seconds = max(int((cycle_end - now).total_seconds()), 0)
        remaining_minutes = max((remaining_seconds + 59) // 60, 0)
        return {
            'approval_type': 'registration_group',
            'registration_group': registration_group,
            'pending_count': pending_count,
            'oldest_pending_at': oldest_pending_at,
            'ready': False,
            'release_count': 0,
            'reason_code': 'waiting_next_cycle',
            'batch_size': resolved_batch_size,
            'timeout_minutes': resolved_timeout_minutes,
            'elapsed_minutes': max(0, int((now - cycle_start).total_seconds() // 60)),
            'remaining_minutes': remaining_minutes,
            'remaining_seconds': remaining_seconds,
            'cycle_anchor_at': cycle_anchor_text,
            'completed_cycles_since_anchor': completed_cycles_since_anchor,
            'cycle_started_at': cycle_start.isoformat(),
            'cycle_ends_at': cycle_end.isoformat(),
        }
    if not oldest_pending_at:
        return {
            'approval_type': 'registration_group',
            'registration_group': registration_group,
            'pending_count': pending_count,
            'oldest_pending_at': oldest_pending_at,
            'ready': False,
            'release_count': 0,
            'reason_code': 'waiting_for_batch',
            'batch_size': resolved_batch_size,
            'timeout_minutes': resolved_timeout_minutes,
            'elapsed_minutes': 0,
            'remaining_minutes': resolved_timeout_minutes,
            'remaining_seconds': resolved_timeout_minutes * 60,
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
            'batch_size': resolved_batch_size,
            'timeout_minutes': resolved_timeout_minutes,
            'cycle_anchor_at': cycle_anchor_at,
        },
        timeout=30.0,
    )


def _parse_schedule_hhmm(value: str) -> Optional[tuple[int, int]]:
    text = str(value or '').strip()
    if not text or ':' not in text:
        return None
    hour_text, minute_text = text.split(':', 1)
    try:
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour, minute



def _active_schedule_window_token(target: Dict[str, Any], now: Optional[datetime]) -> Optional[str]:
    if now is None:
        return None
    schedule_runtime = target.get('schedule_runtime') if isinstance(target.get('schedule_runtime'), dict) else {}
    if not bool(schedule_runtime.get('configured')) or not bool(schedule_runtime.get('active_now')):
        return None
    schedule_windows = target.get('schedule_windows') or []
    if not isinstance(schedule_windows, list) or not schedule_windows:
        return None
    local_tz = timezone(timedelta(hours=8))
    localized_now = now.astimezone(local_tz)
    for item in schedule_windows:
        if not isinstance(item, dict):
            continue
        start_parts = _parse_schedule_hhmm(str(item.get('start') or ''))
        end_parts = _parse_schedule_hhmm(str(item.get('end') or ''))
        if not start_parts or not end_parts:
            continue
        start_hour, start_minute = start_parts
        end_hour, end_minute = end_parts
        for day_offset in (0, -1):
            base_day = (localized_now + timedelta(days=day_offset)).date()
            start_at = datetime(base_day.year, base_day.month, base_day.day, start_hour, start_minute, tzinfo=local_tz)
            end_at = datetime(base_day.year, base_day.month, base_day.day, end_hour, end_minute, tzinfo=local_tz)
            if (end_hour, end_minute) <= (start_hour, start_minute):
                end_at += timedelta(days=1)
            if start_at <= localized_now < end_at:
                return start_at.isoformat()
    return None



def _schedule_end_flush_due(target: Dict[str, Any], now: datetime, *, poll_interval_seconds: float) -> bool:
    schedule_runtime = target.get('schedule_runtime') if isinstance(target.get('schedule_runtime'), dict) else {}
    if not bool(schedule_runtime.get('configured')) or bool(schedule_runtime.get('active_now')):
        return False
    schedule_windows = target.get('schedule_windows') or []
    if not isinstance(schedule_windows, list) or not schedule_windows:
        return False
    local_tz = timezone(timedelta(hours=8))
    localized_now = now.astimezone(local_tz)
    grace_seconds = max(int(float(poll_interval_seconds or 0)), 1) + 60
    for item in schedule_windows:
        if not isinstance(item, dict):
            continue
        start_parts = _parse_schedule_hhmm(str(item.get('start') or ''))
        end_parts = _parse_schedule_hhmm(str(item.get('end') or ''))
        if not start_parts or not end_parts:
            continue
        start_hour, start_minute = start_parts
        end_hour, end_minute = end_parts
        for day_offset in (0, -1):
            base_day = (localized_now + timedelta(days=day_offset)).date()
            start_at = datetime(base_day.year, base_day.month, base_day.day, start_hour, start_minute, tzinfo=local_tz)
            end_at = datetime(base_day.year, base_day.month, base_day.day, end_hour, end_minute, tzinfo=local_tz)
            if (end_hour, end_minute) < (start_hour, start_minute):
                end_at += timedelta(days=1)
            if localized_now < end_at:
                continue
            delta_seconds = (localized_now - end_at).total_seconds()
            if 0 <= delta_seconds <= grace_seconds:
                return True
    return False



def _schedule_end_flush_release(target: Dict[str, Any], registration_group: str, group_state: Dict[str, Any]) -> Dict[str, Any]:
    pending_count = max(int(group_state.get('pending_count') or 0), 0)
    oldest_pending_at = oldest_pending_at_iso(group_state)
    resolved_batch_size = max(1, int(target.get('approval_count_threshold') or 30))
    resolved_timeout_minutes = max(1, int(target.get('approval_timeout_minutes') or 30))
    return {
        'approval_type': 'registration_group',
        'registration_group': registration_group,
        'pending_count': pending_count,
        'oldest_pending_at': oldest_pending_at,
        'ready': pending_count > 0,
        'release_count': pending_count,
        'reason_code': 'schedule_window_end_flush',
        'batch_size': resolved_batch_size,
        'timeout_minutes': resolved_timeout_minutes,
        'elapsed_minutes': resolved_timeout_minutes,
        'remaining_minutes': 0,
        'remaining_seconds': 0,
    }


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


def _run_independent_truth_probe(independent_truth_probe_cmd: str, *, timeout: float = 120.0) -> Dict[str, Any]:
    if not str(independent_truth_probe_cmd or '').strip():
        raise RuntimeError('independent_truth_probe_cmd_missing')
    completed = subprocess.run(
        independent_truth_probe_cmd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or '').strip() or f'independent_truth_probe_exit_{completed.returncode}')
    payload = json.loads((completed.stdout or '').strip())
    if not isinstance(payload, dict):
        raise RuntimeError('independent_truth_probe_non_dict_response')
    return payload


def _recheck_authoritative_group_state(
    *,
    worker_base_url: str,
    registration_group: str,
    fresh_probe_cmd: str,
    use_worker_state_directly: bool,
    worker_timeout_seconds: float,
    command_timeout_seconds: float,
) -> Dict[str, Any]:
    worker_payload = fetch_json(
        f"{worker_base_url.rstrip('/')}/group-state",
        method='POST',
        payload={'registration_group': registration_group},
        timeout=worker_timeout_seconds,
    )
    if use_worker_state_directly:
        return {
            'worker_payload': worker_payload,
            'fresh_payload': worker_payload,
            'decision_group': {
                'payload': worker_payload,
                'source': 'worker_state',
                'worker_signature': _group_state_signature(worker_payload),
                'fresh_signature': _group_state_signature(worker_payload),
                'mismatch': False,
                'mismatch_reasons': [],
            },
        }
    fresh_payload = _run_fresh_probe(fresh_probe_cmd, timeout=command_timeout_seconds)
    return {
        'worker_payload': worker_payload,
        'fresh_payload': fresh_payload,
        'decision_group': _resolve_decision_group_state(worker_payload, fresh_payload),
    }


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

    decision_payload = fresh_payload
    decision_source = 'fresh_probe'
    same_group = bool(worker_sig.get('group_id')) and worker_sig.get('group_id') == fresh_sig.get('group_id')
    if same_group and worker_sig.get('pending_count', 0) > fresh_sig.get('pending_count', 0):
        merged_payload = dict(worker_payload or {})
        merged_requesters = list(worker_payload.get('requesters') or []) if isinstance(worker_payload.get('requesters'), list) else []
        fresh_requesters = list(fresh_payload.get('requesters') or []) if isinstance(fresh_payload.get('requesters'), list) else []
        seen_requester_ids = {
            str(item.get('requesterId') or '').strip()
            for item in merged_requesters
            if isinstance(item, dict) and str(item.get('requesterId') or '').strip()
        }
        for item in fresh_requesters:
            if not isinstance(item, dict):
                continue
            requester_id = str(item.get('requesterId') or '').strip()
            if requester_id and requester_id in seen_requester_ids:
                continue
            if requester_id:
                seen_requester_ids.add(requester_id)
            merged_requesters.append(item)
        if merged_requesters:
            merged_payload['requesters'] = merged_requesters
        worker_requester_ids = list(worker_payload.get('requester_ids') or []) if isinstance(worker_payload.get('requester_ids'), list) else []
        fresh_requester_ids = list(fresh_payload.get('requester_ids') or []) if isinstance(fresh_payload.get('requester_ids'), list) else []
        merged_requester_ids = list(dict.fromkeys([*worker_requester_ids, *fresh_requester_ids]))
        if merged_requester_ids:
            merged_payload['requester_ids'] = merged_requester_ids
        merged_payload['pending_count'] = max(int(worker_sig.get('pending_count') or 0), int(fresh_sig.get('pending_count') or 0))
        merged_payload['member_count'] = max(int(worker_sig.get('member_count') or 0), int(fresh_sig.get('member_count') or 0))
        decision_payload = merged_payload
        decision_source = 'reconciled_max_pending'

    return {
        'payload': decision_payload,
        'source': decision_source,
        'worker_signature': worker_sig,
        'fresh_signature': fresh_sig,
        'mismatch': bool(mismatch_reasons),
        'mismatch_reasons': mismatch_reasons,
    }


def _review_surface_positive_suspected_residue(review_surface_payload: Optional[Dict[str, Any]]) -> bool:
    payload = dict(review_surface_payload or {})
    try:
        pending_count = max(int(payload.get('pending_count') or 0), 0)
    except (TypeError, ValueError):
        pending_count = 0
    if pending_count <= 0:
        return False
    if bool(payload.get('has_pending_section')):
        return False
    requester_ids = [str(item or '').strip() for item in (payload.get('requester_ids') or []) if str(item or '').strip()]
    if requester_ids:
        return False
    suspicious = payload.get('suspected_review_surface_residue')
    if suspicious is not None:
        return bool(suspicious)
    requesters = payload.get('requesters') or []
    requester_names: List[str] = []
    for item in requesters:
        if isinstance(item, dict):
            candidate = str(item.get('displayName') or item.get('requesterId') or '').strip()
        else:
            candidate = str(item or '').strip()
        if candidate:
            requester_names.append(candidate)
    if not requester_names:
        return True
    return all(bool(str(name).strip()) and str(name).strip().startswith('~') for name in requester_names)


def _recover_worker_for_target(
    args: argparse.Namespace,
    target: Dict[str, Any],
    *,
    failed_worker_base_url: str,
    error: Exception,
    trigger_reason: str = '',
) -> Dict[str, Any]:
    recovery: Dict[str, Any] = {
        'attempted': False,
        'status': 'skipped',
        'reason': 'auto_recover_disabled' if not getattr(args, 'auto_recover_worker', False) else 'unhandled_target',
        'trigger_reason': str(trigger_reason or '').strip() or None,
        'failed_worker_base_url': failed_worker_base_url,
        'error': str(error),
    }
    if not getattr(args, 'auto_recover_worker', False):
        return recovery

    source = str(target.get('source') or '').strip()
    account_key = str(target.get('account_key') or '').strip()
    if source != 'account_binding':
        recovery['reason'] = 'non_binding_target_recovery_disabled'
        return recovery

    normalized_trigger_reason = str(trigger_reason or '').strip()
    if source == 'account_binding' and account_key:
        if normalized_trigger_reason in {'session_mismatch', 'runtime_unhealthy'}:
            recovery['attempted'] = True
            recovery['mode'] = 'account_runtime_rebuild'
            recovery['account_key'] = account_key
            try:
                stop_payload = fetch_json(
                    f"{args.api_base_url.rstrip('/')}/api/ops/whatsapp-approval-accounts/{urllib.parse.quote(account_key, safe='')}/runtime/internal/stop",
                    method='POST',
                    payload={},
                    timeout=args.command_timeout_seconds,
                )
                recovery['stop_payload'] = stop_payload
            except Exception as exc:
                recovery['stop_error'] = str(exc)
            try:
                start_payload = fetch_json(
                    f"{args.api_base_url.rstrip('/')}/api/ops/whatsapp-approval-accounts/{urllib.parse.quote(account_key, safe='')}/runtime/internal/start",
                    method='POST',
                    payload={},
                    timeout=args.command_timeout_seconds,
                )
                recovery['start_payload'] = start_payload
                runtime = start_payload.get('runtime') if isinstance(start_payload.get('runtime'), dict) else {}
                recovered_worker_base_url = str(runtime.get('base_url') or '').strip()
                if not recovered_worker_base_url:
                    recovery['status'] = 'failed'
                    recovery['reason'] = 'runtime_start_returned_empty_base_url'
                    return recovery
                session_payload = fetch_json(
                    f"{args.api_base_url.rstrip('/')}/api/ops/whatsapp-approval-accounts/{urllib.parse.quote(account_key, safe='')}/session/internal/start",
                    method='POST',
                    payload={},
                    timeout=args.command_timeout_seconds,
                )
                recovery['session_payload'] = session_payload
                session_runtime = session_payload.get('runtime') if isinstance(session_payload.get('runtime'), dict) else {}
                session_state = session_payload.get('session') if isinstance(session_payload.get('session'), dict) else {}
                recovered_worker_base_url = str(session_runtime.get('base_url') or recovered_worker_base_url).strip()
                recovery['recovered_worker_base_url'] = recovered_worker_base_url
                recovery['recovered_session_state'] = session_state
                if recovered_worker_base_url and bool(session_state.get('login_verified')) and bool(session_state.get('session_target_match')):
                    recovery['status'] = 'ok'
                    time.sleep(max(1.0, float(args.restart_wait_seconds)))
                else:
                    recovery['status'] = 'failed'
                    recovery['reason'] = 'session_start_did_not_restore_verified_target_matched_session'
            except Exception as exc:
                recovery['status'] = 'failed'
                recovery['reason'] = str(exc)
            return recovery
        recovery['attempted'] = True
        recovery['mode'] = 'account_runtime_start'
        recovery['account_key'] = account_key
        try:
            payload = fetch_json(
                f"{args.api_base_url.rstrip('/')}/api/ops/whatsapp-approval-accounts/{urllib.parse.quote(account_key, safe='')}/runtime/internal/start",
                method='POST',
                payload={},
                timeout=args.command_timeout_seconds,
            )
            runtime = payload.get('runtime') if isinstance(payload.get('runtime'), dict) else {}
            recovery['payload'] = payload
            recovery['recovered_worker_base_url'] = str(runtime.get('base_url') or '').strip()
            if recovery['recovered_worker_base_url']:
                recovery['status'] = 'ok'
                time.sleep(max(1.0, float(args.restart_wait_seconds)))
            else:
                recovery['status'] = 'failed'
                recovery['reason'] = 'runtime_start_returned_empty_base_url'
        except Exception as exc:
            recovery['status'] = 'failed'
            recovery['reason'] = str(exc)
        return recovery

    worker_restart_cmd = str(getattr(args, 'worker_restart_cmd', '') or '').strip()
    if worker_restart_cmd:
        recovery['attempted'] = True
        recovery['mode'] = 'worker_restart_cmd'
        restart_result = maybe_restart(worker_restart_cmd, timeout=args.restart_command_timeout_seconds)
        recovery['restart'] = restart_result
        if restart_result.get('ok'):
            recovery['status'] = 'ok'
            recovery['recovered_worker_base_url'] = failed_worker_base_url
            time.sleep(max(1.0, float(args.restart_wait_seconds)))
        else:
            recovery['status'] = 'failed'
            recovery['reason'] = 'worker_restart_cmd_failed'
        return recovery

    recovery['reason'] = 'worker_restart_cmd_missing'
    return recovery


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


def _target_session_key(session_id: str, target: Dict[str, Any], now: Optional[datetime] = None) -> str:
    registration_group = str(target.get('registration_group') or '').strip()
    schedule_window_token = _active_schedule_window_token(target, now)
    if schedule_window_token:
        return f"{session_id or 'default'}::{registration_group}::{schedule_window_token}::{_target_session_config_fingerprint(target)}"
    return f"{session_id or 'default'}::{registration_group}::{_target_session_config_fingerprint(target)}"


def _session_state(
    state: Dict[str, Any],
    *,
    session_id: str,
    registration_group: str,
    checked_at: str,
    target: Dict[str, Any],
) -> Dict[str, Any]:
    checked_at_dt: Optional[datetime] = None
    try:
        checked_at_dt = datetime.fromisoformat(str(checked_at).replace('Z', '+00:00'))
        if checked_at_dt.tzinfo is None:
            checked_at_dt = checked_at_dt.replace(tzinfo=timezone.utc)
    except Exception:
        checked_at_dt = None
    session_key = _target_session_key(session_id, target, checked_at_dt)
    monitoring_sessions = state.setdefault('monitoring_sessions', {})
    monitoring = monitoring_sessions.get(session_key)
    if not isinstance(monitoring, dict):
        monitoring = {
            'session_key': session_key,
            'session_id': session_id,
            'registration_group': registration_group,
            'started_at': checked_at,
            'schedule_window_token': _active_schedule_window_token(target, checked_at_dt),
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


def _ordered_cycle_targets(monitor_target: Dict[str, Any], fallback_target: Dict[str, Any], *, now: Optional[datetime] = None, poll_interval_seconds: float = 0.0) -> List[Dict[str, Any]]:
    selected_target = monitor_target.get('selected')
    if str(monitor_target.get('selection_reason') or '').strip() == 'configured_binding_outside_schedule':
        normalized_selected = _normalize_monitor_target(selected_target, str((selected_target or {}).get('worker_base_url') or '')) if isinstance(selected_target, dict) else None
        if normalized_selected and now and _schedule_end_flush_due(normalized_selected, now, poll_interval_seconds=poll_interval_seconds):
            return [normalized_selected]
        return []
    candidates = list(monitor_target.get('candidates') or [])
    ordered: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in [selected_target, *candidates]:
        if not isinstance(candidate, dict):
            continue
        normalized = _normalize_monitor_target(candidate, str(candidate.get('worker_base_url') or ''))
        if not normalized:
            continue
        key = _target_session_key('', normalized, now)
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
        'notify_profile_name': str(target.get('notify_profile_name') or '').strip() or None,
        'notify_robot_name': str(target.get('notify_robot_name') or '').strip() or None,
        'area': area,
        'approval_count_threshold': int(target.get('approval_count_threshold') or 0),
        'approval_timeout_minutes': int(target.get('approval_timeout_minutes') or 0),
        'auto_recover_worker': bool(target.get('auto_recover_worker')),
        'schedule_runtime': target.get('schedule_runtime') or {},
        'schedule_windows': target.get('schedule_windows') or [],
        'runtime_state': target.get('runtime_state') if isinstance(target.get('runtime_state'), dict) else {},
        'session_state': target.get('session_state') if isinstance(target.get('session_state'), dict) else {},
        'source': str(target.get('source') or '').strip() or 'fallback_config',
    }


def _runtime_in_startup_grace(runtime_state: Dict[str, Any], now: datetime, *, grace_seconds: float = 45.0) -> bool:
    if not isinstance(runtime_state, dict) or not runtime_state:
        return False
    started_at = _parse_iso_datetime(runtime_state.get('started_at'))
    if started_at is None:
        return False
    elapsed = (now - started_at).total_seconds()
    if elapsed < 0 or elapsed >= grace_seconds:
        return False
    stopped_at = _parse_iso_datetime(runtime_state.get('stopped_at'))
    if stopped_at is not None and stopped_at >= started_at:
        return False
    status_text = str(runtime_state.get('status') or '').strip().lower()
    if status_text == 'not_started':
        return False
    has_runtime_identity = bool(str(runtime_state.get('base_url') or '').strip()) or bool(runtime_state.get('pid')) or bool(runtime_state.get('port'))
    return has_runtime_identity



def _clear_binding_rebuild_gate(monitoring_session: Optional[Dict[str, Any]]) -> None:
    if isinstance(monitoring_session, dict):
        monitoring_session.pop('binding_rebuild_gate', None)



def _binding_rebuild_gate_decision(
    args: argparse.Namespace,
    monitoring_session: Optional[Dict[str, Any]],
    *,
    trigger_reason: str,
    now: datetime,
) -> Dict[str, Any]:
    threshold = max(int(getattr(args, 'worker_recovery_rebuild_threshold', 2) or 2), 1)
    cooldown_seconds = max(float(getattr(args, 'worker_recovery_rebuild_cooldown_seconds', 120.0) or 120.0), 0.0)
    normalized_reason = str(trigger_reason or '').strip() or 'runtime_unavailable'
    if not isinstance(monitoring_session, dict):
        return {
            'allowed': True,
            'reason': 'no_monitoring_session',
            'trigger_reason': normalized_reason,
            'streak_count': threshold,
            'threshold': threshold,
            'cooldown_seconds': cooldown_seconds,
        }
    gate = monitoring_session.get('binding_rebuild_gate')
    if not isinstance(gate, dict):
        gate = {}
        monitoring_session['binding_rebuild_gate'] = gate

    previous_reason = str(gate.get('trigger_reason') or '').strip()
    previous_count = int(gate.get('streak_count') or 0) if previous_reason == normalized_reason else 0
    streak_count = previous_count + 1
    first_seen_at = str(gate.get('first_seen_at') or '').strip() if previous_reason == normalized_reason else ''
    if not first_seen_at:
        first_seen_at = now.isoformat()
    last_attempted_at = _parse_iso_datetime(gate.get('last_attempted_at')) if previous_reason == normalized_reason else None

    gate.update({
        'trigger_reason': normalized_reason,
        'streak_count': streak_count,
        'threshold': threshold,
        'cooldown_seconds': cooldown_seconds,
        'first_seen_at': first_seen_at,
        'last_seen_at': now.isoformat(),
    })

    if streak_count < threshold:
        return {
            'allowed': False,
            'reason': 'awaiting_consecutive_rebuild_signal',
            'trigger_reason': normalized_reason,
            'streak_count': streak_count,
            'threshold': threshold,
            'cooldown_seconds': cooldown_seconds,
            'first_seen_at': first_seen_at,
            'last_seen_at': gate.get('last_seen_at'),
        }

    if last_attempted_at is not None and cooldown_seconds > 0:
        elapsed = max((now - last_attempted_at).total_seconds(), 0.0)
        if elapsed < cooldown_seconds:
            return {
                'allowed': False,
                'reason': 'rebuild_cooldown_active',
                'trigger_reason': normalized_reason,
                'streak_count': streak_count,
                'threshold': threshold,
                'cooldown_seconds': cooldown_seconds,
                'remaining_cooldown_seconds': max(int(cooldown_seconds - elapsed), 0),
                'first_seen_at': first_seen_at,
                'last_seen_at': gate.get('last_seen_at'),
                'last_attempted_at': last_attempted_at.isoformat(),
            }

    gate['last_attempted_at'] = now.isoformat()
    return {
        'allowed': True,
        'reason': 'threshold_met',
        'trigger_reason': normalized_reason,
        'streak_count': streak_count,
        'threshold': threshold,
        'cooldown_seconds': cooldown_seconds,
        'first_seen_at': first_seen_at,
        'last_seen_at': gate.get('last_seen_at'),
        'last_attempted_at': gate.get('last_attempted_at'),
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
        payload = fetch_json(f"{args.api_base_url.rstrip('/')}/api/ops/whatsapp-approval-accounts/registration-runtime-directory", timeout=30.0)
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
    inactive_bindings: List[Dict[str, Any]] = []
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
        session_state = row.get('session_state') if isinstance(row.get('session_state'), dict) else {}
        worker_base_url = str(runtime_state.get('base_url') or '').strip()
        runtime_active = bool(runtime_state.get('active')) and bool(worker_base_url)
        runtime_ready = bool(runtime_state.get('ready')) if 'ready' in runtime_state else True
        runtime_authenticated = bool(runtime_state.get('authenticated')) if 'authenticated' in runtime_state else True
        session_target_match = (
            bool(session_state.get('session_target_match'))
            if 'session_target_match' in session_state
            else (
                bool(runtime_state.get('session_target_match'))
                if 'session_target_match' in runtime_state
                else True
            )
        )
        login_verified = (
            bool(session_state.get('login_verified'))
            if 'login_verified' in session_state
            else (
                bool(runtime_state.get('login_verified'))
                if 'login_verified' in runtime_state
                else True
            )
        )
        runtime_candidate_ready = runtime_active and runtime_ready and runtime_authenticated and session_target_match and login_verified
        account_key = str(row.get('account_key') or '').strip()
        account_name = str(row.get('account_name') or '').strip()
        row_area = str(row.get('area') or '').strip() or 'Indonesia'
        for binding in row.get('group_link_bindings') or []:
            if not isinstance(binding, dict):
                continue
            if binding.get('enabled') is False:
                continue
            schedule_runtime = binding.get('schedule_runtime') if isinstance(binding.get('schedule_runtime'), dict) else {}
            registration_group = (
                str(binding.get('registration_group') or '').strip()
                or str(binding.get('group_id') or '').strip()
                or str(binding.get('link') or '').strip()
                or str(binding.get('group_name') or '').strip()
            )
            normalized = _normalize_monitor_target({
                'registration_group': registration_group,
                'group_name': str(binding.get('group_name') or '').strip() or registration_group,
                'worker_base_url': worker_base_url if runtime_candidate_ready else '',
                'account_key': account_key,
                'account_name': account_name,
                'binding_link': str(binding.get('link') or '').strip(),
                'binding_group_name': str(binding.get('group_name') or '').strip(),
                'notify_profile_name': str(binding.get('notify_profile_name') or '').strip(),
                'notify_robot_name': str(binding.get('notify_robot_name') or '').strip(),
                'area': str(binding.get('area') or '').strip() or row_area,
                'approval_count_threshold': binding.get('approval_count_threshold'),
                'approval_timeout_minutes': binding.get('approval_timeout_minutes'),
                'auto_recover_worker': binding.get('auto_recover_worker'),
                'schedule_runtime': schedule_runtime,
                'schedule_windows': binding.get('schedule_windows') or [],
                'runtime_state': runtime_state,
                'session_state': session_state,
                'source': 'account_binding',
            })
            if not normalized:
                continue
            if schedule_runtime.get('configured') and not bool(schedule_runtime.get('active_now')):
                inactive_bindings.append(normalized)
                continue
            configured_bindings.append(normalized)
            if runtime_candidate_ready and normalized.get('worker_base_url'):
                candidates.append(normalized)

    if not candidates:
        if configured_bindings:
            return {
                'selected': _pick_preferred_target(configured_bindings),
                'candidates': configured_bindings,
                'selection_reason': 'configured_binding_runtime_unavailable',
                'allow_fallback': False,
            }
        if inactive_bindings:
            return {
                'selected': _pick_preferred_target(inactive_bindings),
                'candidates': [],
                'selection_reason': 'configured_binding_outside_schedule',
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
    dispatch_state['all_groups'] = list(queue.get('official_groups') or []) if isinstance(queue.get('official_groups'), list) else []
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
        'allow_crm_only_test_match': True,
        'suppress_success_notifications': True,
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
    for row in ready_groups:
        _store_cycle_anchor(
            state,
            bucket_name='official_cycle_anchors',
            at=now,
            keys=[
                str(row.get('target_group') or '').strip(),
                str(row.get('binding_registration_group') or '').strip(),
                str(row.get('group_id') or '').strip(),
                str(row.get('binding_link') or '').strip(),
                str(row.get('group_name') or '').strip(),
                str(row.get('registration_group') or '').strip(),
            ],
        )
    dispatch_state['triggered'] = True
    dispatch_state['ok'] = True
    dispatch_state['result'] = result


def _fetch_worker_group_state_with_passive_retry(
    worker_base_url: str,
    registration_group: str,
    *,
    timeout_seconds: float,
    passive_retry_wait_seconds: float,
    passive_retry_count: int = 2,
) -> Dict[str, Any]:
    attempts: List[Dict[str, Any]] = []
    last_error: Exception | None = None
    total_attempts = max(1, passive_retry_count) + 1
    for attempt in range(1, total_attempts + 1):
        try:
            payload = fetch_json(
                f"{worker_base_url.rstrip('/')}/group-state",
                method='POST',
                payload={'registration_group': registration_group},
                timeout=timeout_seconds,
            )
            return {
                'ok': True,
                'payload': payload,
                'attempts': attempts,
                'retry_count': len(attempts),
                'total_attempts': attempt,
                'recovered_after_retry': bool(attempts),
            }
        except Exception as exc:
            last_error = exc
            if attempt >= total_attempts:
                break
            attempts.append({'attempt': attempt, 'error': str(exc)})
            time.sleep(passive_retry_wait_seconds)
    assert last_error is not None
    return {
        'ok': False,
        'error': str(last_error),
        'attempts': attempts,
        'retry_count': len(attempts),
        'total_attempts': total_attempts,
        'recovered_after_retry': False,
    }


def _fetch_worker_review_surface_state(
    worker_base_url: str,
    registration_group: str,
    *,
    timeout_seconds: float,
) -> Dict[str, Any]:
    return fetch_json(
        f"{worker_base_url.rstrip('/')}/review-surface-state",
        method='POST',
        payload={'registration_group': registration_group},
        timeout=timeout_seconds,
    )



def _evaluate_release_with_backend_recovery(
    args: argparse.Namespace,
    registration_group: str,
    group_state: Dict[str, Any],
    *,
    batch_size: Optional[int] = None,
    timeout_minutes: Optional[int] = None,
    cycle_anchor_at: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        payload = _evaluate_release(
            args.api_base_url,
            registration_group,
            group_state,
            batch_size=batch_size,
            timeout_minutes=timeout_minutes,
            cycle_anchor_at=cycle_anchor_at,
        )
        return {'ok': True, 'payload': payload}
    except Exception as exc:
        initial_error = exc

    passive_retry_wait_seconds = max(1.0, min(float(args.restart_wait_seconds or 1.0), 3.0))
    health_after_error = check_backend_health(args.api_base_url, timeout=args.health_timeout_seconds)
    if health_after_error.get('ok'):
        time.sleep(passive_retry_wait_seconds)
        try:
            payload = _evaluate_release(
                args.api_base_url,
                registration_group,
                group_state,
                batch_size=batch_size,
                timeout_minutes=timeout_minutes,
                cycle_anchor_at=cycle_anchor_at,
            )
            return {
                'ok': True,
                'payload': payload,
                'recovered_after_retry': True,
                'health_after_error': health_after_error,
            }
        except Exception as retry_exc:
            initial_error = retry_exc

    restart_result = None
    after_restart = None
    if not health_after_error.get('ok') and args.backend_restart_cmd:
        restart_result = maybe_restart(args.backend_restart_cmd, timeout=args.restart_command_timeout_seconds)
        time.sleep(max(1.0, float(args.restart_wait_seconds)))
        after_restart = check_backend_health(args.api_base_url, timeout=args.health_timeout_seconds)
        if after_restart.get('ok'):
            try:
                payload = _evaluate_release(
                    args.api_base_url,
                    registration_group,
                    group_state,
                    batch_size=batch_size,
                    timeout_minutes=timeout_minutes,
                    cycle_anchor_at=cycle_anchor_at,
                )
                return {
                    'ok': True,
                    'payload': payload,
                    'recovered_after_restart': True,
                    'health_after_error': health_after_error,
                    'restart': restart_result,
                    'after_restart': after_restart,
                }
            except Exception as restart_exc:
                initial_error = restart_exc

    result: Dict[str, Any] = {
        'ok': False,
        'error': str(initial_error),
        'health_after_error': health_after_error,
    }
    if restart_result is not None:
        result['restart'] = restart_result
    if after_restart is not None:
        result['after_restart'] = after_restart
    return result



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
    target_independent_truth_probe_cmd = str(getattr(args, 'independent_truth_probe_cmd', '') or '').strip()
    target_area = str(target.get('area') or args.area or '').strip() or 'Indonesia'
    registration_cycle_anchors = state.setdefault('registration_cycle_anchors', {})
    if not isinstance(registration_cycle_anchors, dict):
        registration_cycle_anchors = {}
        state['registration_cycle_anchors'] = registration_cycle_anchors
    target_cycle_anchor_at = next((
        str(registration_cycle_anchors.get(candidate) or '').strip()
        for candidate in (
            target_registration_group,
            str(target.get('binding_link') or '').strip(),
            str(target.get('binding_group_name') or '').strip(),
            target_group_name,
        )
        if str(candidate or '').strip() and str(registration_cycle_anchors.get(candidate) or '').strip()
    ), None)
    cycle: Dict[str, Any] = {
        'registration_group': target_registration_group,
        'monitor_target': {
            **target,
            'group_name': target_group_name,
            'worker_base_url': target_worker_base_url,
            'fresh_probe_cmd_source': 'target_specific_default' if target_registration_group != str(args.registration_group or '').strip() else 'configured_or_default',
            'independent_truth_probe_cmd': target_independent_truth_probe_cmd,
        },
    }

    checked_at = now.isoformat()
    monitoring_session = _session_state(
        state,
        session_id=str(getattr(args, 'monitoring_session_id', '') or ''),
        registration_group=target_registration_group,
        checked_at=checked_at,
        target=target,
    )
    cycle['monitoring_session'] = {
        'session_key': monitoring_session.get('session_key'),
        'started_at': monitoring_session.get('started_at'),
        'schedule_window_token': monitoring_session.get('schedule_window_token'),
    }

    worker_payload: Dict[str, Any] | None = None
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
        if str(target.get('source') or '').strip() == 'account_binding':
            runtime_state = dict(target.get('runtime_state') or {}) if isinstance(target.get('runtime_state'), dict) else {}
            session_state = dict(target.get('session_state') or {}) if isinstance(target.get('session_state'), dict) else {}
            if _runtime_in_startup_grace(runtime_state, now):
                cycle['worker_state']['startup_grace'] = {
                    'active': True,
                    'status': str(runtime_state.get('status') or '').strip() or 'starting',
                    'started_at': runtime_state.get('started_at'),
                    'reason': 'runtime_startup_grace_skip_auto_rebuild',
                }
                return cycle
            gate_reason = 'worker_base_url_missing'
            if session_state and session_state.get('session_target_match') is False:
                gate_reason = 'session_mismatch'
            elif runtime_state and any(runtime_state.get(flag) is False for flag in ('active', 'ready', 'authenticated')):
                gate_reason = 'runtime_unhealthy'
            rebuild_gate = _binding_rebuild_gate_decision(
                args,
                monitoring_session,
                trigger_reason=gate_reason,
                now=now,
            )
            if gate_reason in {'session_mismatch', 'runtime_unhealthy'} and not rebuild_gate.get('allowed'):
                cycle['worker_state']['recovery'] = {
                    'attempted': False,
                    'status': 'skipped',
                    'reason': str(rebuild_gate.get('reason') or 'rebuild_gate_blocked'),
                    'trigger_reason': gate_reason,
                    'gate': rebuild_gate,
                }
                return cycle
            recovery = _recover_worker_for_target(
                args,
                target,
                failed_worker_base_url='',
                error=RuntimeError(cycle['worker_state']['error']),
                trigger_reason=gate_reason,
            )
            cycle['worker_state']['recovery'] = recovery
            recovered_worker_base_url = str(recovery.get('recovered_worker_base_url') or '').strip()
            if recovery.get('status') == 'ok' and recovered_worker_base_url:
                recovery_retry_wait_seconds = max(2.0, min(float(args.restart_wait_seconds or 2.0), 5.0))
                recovery_worker_fetch = _fetch_worker_group_state_with_passive_retry(
                    recovered_worker_base_url,
                    target_registration_group,
                    timeout_seconds=args.worker_timeout_seconds,
                    passive_retry_wait_seconds=recovery_retry_wait_seconds,
                    passive_retry_count=4,
                )
                if recovery_worker_fetch.get('ok'):
                    worker_payload = recovery_worker_fetch['payload']
                    _clear_binding_rebuild_gate(monitoring_session)
                    target_worker_base_url = recovered_worker_base_url
                    cycle['monitor_target']['worker_base_url'] = target_worker_base_url
                    cycle['worker_state'] = {
                        'ok': True,
                        'payload': worker_payload,
                        'recovered_after_restart': True,
                        'recovery': recovery,
                        'recovery_probe': {
                            'retry_attempts': recovery_worker_fetch.get('attempts') or [],
                            'retry_count': int(recovery_worker_fetch.get('retry_count') or 0),
                            'total_attempts': int(recovery_worker_fetch.get('total_attempts') or 1),
                            'wait_seconds': recovery_retry_wait_seconds,
                        },
                    }
                else:
                    recovery['retry_error'] = str(recovery_worker_fetch.get('error') or 'worker_group_state_failed_after_recovery')
                    recovery['recovery_probe'] = {
                        'retry_attempts': recovery_worker_fetch.get('attempts') or [],
                        'retry_count': int(recovery_worker_fetch.get('retry_count') or 0),
                        'total_attempts': int(recovery_worker_fetch.get('total_attempts') or 1),
                        'wait_seconds': recovery_retry_wait_seconds,
                    }
                    cycle['worker_state'] = {
                        'ok': False,
                        'error': recovery['retry_error'],
                        'recovery': recovery,
                    }
                    return cycle
            else:
                return cycle
        else:
            return cycle

    if worker_payload is None:
        passive_retry_wait_seconds = max(1.0, min(float(args.restart_wait_seconds or 1.0), 3.0))
        passive_retry_count = 3 if str(target.get('source') or '').strip() == 'account_binding' else 2
        worker_fetch = _fetch_worker_group_state_with_passive_retry(
            target_worker_base_url,
            target_registration_group,
            timeout_seconds=args.worker_timeout_seconds,
            passive_retry_wait_seconds=passive_retry_wait_seconds,
            passive_retry_count=passive_retry_count,
        )
        if worker_fetch.get('ok'):
            worker_payload = worker_fetch['payload']
            _clear_binding_rebuild_gate(monitoring_session)
            cycle['worker_state'] = {'ok': True, 'payload': worker_payload}
            if worker_fetch.get('recovered_after_retry'):
                cycle['worker_state']['recovered_after_retry'] = True
                cycle['worker_state']['retry_attempts'] = worker_fetch.get('attempts') or []
        else:
            exc = RuntimeError(str(worker_fetch.get('error') or 'worker_group_state_failed'))
            recovery = _recover_worker_for_target(
                args,
                target,
                failed_worker_base_url=target_worker_base_url,
                error=exc,
            )
            recovered_worker_base_url = str(recovery.get('recovered_worker_base_url') or '').strip() or target_worker_base_url
            if recovery.get('status') == 'ok' and recovered_worker_base_url:
                recovery_retry_wait_seconds = max(2.0, min(float(args.restart_wait_seconds or 2.0), 5.0))
                recovery_worker_fetch = _fetch_worker_group_state_with_passive_retry(
                    recovered_worker_base_url,
                    target_registration_group,
                    timeout_seconds=args.worker_timeout_seconds,
                    passive_retry_wait_seconds=recovery_retry_wait_seconds,
                    passive_retry_count=4,
                )
                if recovery_worker_fetch.get('ok'):
                    worker_payload = recovery_worker_fetch['payload']
                    _clear_binding_rebuild_gate(monitoring_session)
                    target_worker_base_url = recovered_worker_base_url
                    cycle['monitor_target']['worker_base_url'] = target_worker_base_url
                    cycle['worker_state'] = {
                        'ok': True,
                        'payload': worker_payload,
                        'recovered_after_restart': True,
                        'recovery': recovery,
                        'recovery_probe': {
                            'retry_attempts': recovery_worker_fetch.get('attempts') or [],
                            'retry_count': int(recovery_worker_fetch.get('retry_count') or 0),
                            'total_attempts': int(recovery_worker_fetch.get('total_attempts') or 1),
                            'wait_seconds': recovery_retry_wait_seconds,
                        },
                    }
                    if worker_fetch.get('attempts'):
                        cycle['worker_state']['retry_attempts'] = worker_fetch.get('attempts') or []
                else:
                    recovery['retry_error'] = str(recovery_worker_fetch.get('error') or 'worker_group_state_failed_after_recovery')
                    recovery['recovery_probe'] = {
                        'retry_attempts': recovery_worker_fetch.get('attempts') or [],
                        'retry_count': int(recovery_worker_fetch.get('retry_count') or 0),
                        'total_attempts': int(recovery_worker_fetch.get('total_attempts') or 1),
                        'wait_seconds': recovery_retry_wait_seconds,
                    }
                    cycle['worker_state'] = {
                        'ok': False,
                        'error': recovery['retry_error'],
                        'recovery': recovery,
                        'retry_attempts': worker_fetch.get('attempts') or [],
                    }
                    return cycle
            else:
                cycle['worker_state'] = {
                    'ok': False,
                    'error': str(exc),
                    'recovery': recovery,
                    'retry_attempts': worker_fetch.get('attempts') or [],
                }
                return cycle

    use_worker_state_directly = True
    try:
        worker_pending_count = max(int(worker_payload.get('pending_count') or 0), 0)
    except (TypeError, ValueError):
        worker_pending_count = 0
    zero_pending_recheck = worker_pending_count <= 0

    cycle['fresh_probe'] = {
        'ok': False,
        'skipped': True,
        'reason': 'group_state_is_authoritative_source',
    }
    cycle['review_surface_probe'] = {
        'ok': False,
        'skipped': True,
        'reason': 'diagnostics_only_not_authoritative',
    }
    cycle['independent_truth_probe'] = {
        'ok': False,
        'skipped': True,
        'reason': 'async_reconcile_only_not_authoritative',
    }
    decision_group = {
        'payload': {
            **worker_payload,
            'source': 'group_state',
            'pending_zero_confidence': 'unverified' if zero_pending_recheck else None,
            'data_quality': 'fresh',
            'session_health': 'healthy',
        },
        'source': 'group_state',
        'worker_signature': _group_state_signature(worker_payload),
        'fresh_signature': None,
        'mismatch': False,
        'mismatch_reasons': [],
        'pending_zero_confidence': 'unverified' if zero_pending_recheck else None,
        'data_quality': 'fresh',
        'session_health': 'healthy',
        'needs_async_reconcile': bool(zero_pending_recheck),
    }
    if zero_pending_recheck:
        cycle['fresh_probe'] = {
            'ok': False,
            'skipped': True,
            'reason': 'group_state_is_authoritative_source',
            'zero_pending_recheck': True,
            'recheck_source': 'group_state',
        }
        try:
            recheck = _recheck_authoritative_group_state(
                worker_base_url=target_worker_base_url,
                registration_group=target_registration_group,
                fresh_probe_cmd=target_fresh_probe_cmd,
                use_worker_state_directly=True,
                worker_timeout_seconds=args.worker_timeout_seconds,
                command_timeout_seconds=args.command_timeout_seconds,
            )
            recheck_payload = dict((recheck.get('decision_group') or {}).get('payload') or {})
            try:
                recheck_pending_count = max(int(recheck_payload.get('pending_count') or 0), 0)
            except (TypeError, ValueError):
                recheck_pending_count = 0
            if recheck_pending_count > 0:
                decision_group = {
                    **(recheck.get('decision_group') or {}),
                    'pending_zero_confidence': None,
                    'data_quality': 'fresh',
                    'session_health': 'healthy',
                    'needs_async_reconcile': False,
                    'zero_pending_unverified': False,
                    'zero_pending_unverified_reason': None,
                }
            else:
                decision_group = {
                    **decision_group,
                    'zero_pending_unverified': True,
                    'zero_pending_unverified_reason': 'same_runtime_family_zero_pending',
                    'needs_async_reconcile': True,
                }
        except Exception as exc:
            decision_group = {
                **decision_group,
                'zero_pending_unverified': True,
                'zero_pending_unverified_reason': 'group_state_recheck_failed',
            }
            cycle['fresh_probe'] = {
                'ok': False,
                'error': str(exc),
                'skipped': True,
                'reason': 'group_state_is_authoritative_source',
                'zero_pending_recheck': True,
                'recheck_source': 'group_state',
            }
    cycle['decision_group_state'] = decision_group

    cycle['truth_state'] = build_truth_state(
        status={
            'truth_state': cycle.get('truth_state'),
            'decision_group_state': cycle.get('decision_group_state'),
            'review_surface_probe': cycle.get('review_surface_probe'),
            'fresh_probe': cycle.get('fresh_probe'),
            'worker_state': cycle.get('worker_state'),
        },
        runtime_state=dict(target.get('runtime_state') or {
            'active': True,
            'ready': True,
            'authenticated': True,
            'base_url': target_worker_base_url,
        }),
        session_state=dict(target.get('session_state') or {}),
        monitor_target=cycle.get('monitor_target'),
    )

    authoritative_payload = decision_group['payload']
    session_id = str(getattr(args, 'monitoring_session_id', '') or '').strip()
    schedule_end_flush_due = _schedule_end_flush_due(
        cycle['monitor_target'],
        now,
        poll_interval_seconds=float(getattr(args, 'approval_poll_interval_seconds', 0.0) or 0.0),
    )
    monitoring_session = _session_state(
        state,
        session_id=session_id,
        registration_group=target_registration_group,
        checked_at=now.isoformat(),
        target=cycle['monitor_target'],
    )
    pending_count = max(int(authoritative_payload.get('pending_count') or 0), 0)
    cycle_anchor_keys = [
        target_registration_group,
        str(target.get('binding_link') or '').strip(),
        str(target.get('binding_group_name') or '').strip(),
        target_group_name,
        str(authoritative_payload.get('group_id') or '').strip(),
        str(authoritative_payload.get('group_name') or '').strip(),
    ]
    previous_pending_count_raw = monitoring_session.get('last_observed_pending_count')
    try:
        previous_pending_count = max(int(previous_pending_count_raw or 0), 0)
    except (TypeError, ValueError):
        previous_pending_count = 0
    monitoring_session['last_observed_pending_count'] = pending_count
    monitoring_session['last_observed_requester_fingerprint'] = requester_fingerprint(authoritative_payload)
    if pending_count > 0 and previous_pending_count <= 0:
        pending_wave_anchor_at = oldest_pending_at_iso(authoritative_payload) or now.isoformat()
        _store_cycle_anchor_text(
            state,
            bucket_name='registration_cycle_anchors',
            anchor_text=pending_wave_anchor_at,
            keys=cycle_anchor_keys,
        )
        target_cycle_anchor_at = pending_wave_anchor_at
    cycle['schedule_end_flush'] = {
        'due': schedule_end_flush_due,
        'pending_count': pending_count,
    }
    cycle['startup_initial_batch'] = {
        'session_id': session_id,
        'startup_initial_batch_done': bool(monitoring_session.get('startup_initial_batch_done')),
        'pending_count': pending_count,
        'attempts': int(monitoring_session.get('startup_initial_batch_attempts') or 0),
        'max_retries': int(monitoring_session.get('startup_initial_batch_max_retries') or 2),
        'last_initial_pending_count': monitoring_session.get('startup_initial_batch_last_initial_pending_count'),
        'last_final_pending_count': monitoring_session.get('startup_initial_batch_last_final_pending_count'),
        'last_initial_requester_fingerprint': monitoring_session.get('startup_initial_batch_last_initial_requester_fingerprint'),
        'last_final_requester_fingerprint': monitoring_session.get('startup_initial_batch_last_final_requester_fingerprint'),
        'startup_probe_rechecks': monitoring_session.get('startup_initial_batch_last_probe_rechecks') or [],
    }
    if session_id and not schedule_end_flush_due and not bool(monitoring_session.get('startup_initial_batch_done')):
        attempts = int(monitoring_session.get('startup_initial_batch_attempts') or 0)
        max_retries = int(monitoring_session.get('startup_initial_batch_max_retries') or 2)
        max_attempts = max(1, max_retries + 1)
        attempt_results: List[Dict[str, Any]] = []
        current_payload = authoritative_payload
        current_pending_count = pending_count
        initial_pending_count = current_pending_count
        initial_requester_fingerprint = requester_fingerprint(current_payload)
        startup_probe_rechecks: List[Dict[str, Any]] = []
        stable_queue_after_no_pending = False
        if current_pending_count > 0 and use_worker_state_directly:
            for probe_attempt in range(1, 3):
                try:
                    probe_payload = fetch_json(
                        f"{target_worker_base_url.rstrip('/')}/group-state",
                        method='POST',
                        payload={'registration_group': target_registration_group},
                        timeout=args.worker_timeout_seconds,
                    )
                    probe_pending_count = max(int(probe_payload.get('pending_count') or 0), 0)
                    startup_probe_rechecks.append({
                        'attempt': probe_attempt,
                        'pending_count': probe_pending_count,
                        'requester_fingerprint': requester_fingerprint(probe_payload),
                    })
                    if probe_pending_count > current_pending_count:
                        current_payload = probe_payload
                        current_pending_count = probe_pending_count
                except Exception as exc:
                    startup_probe_rechecks.append({'attempt': probe_attempt, 'error': str(exc)})
                if probe_attempt < 2:
                    time.sleep(0.5)
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
            formal_run = formal_run_payload(result.get('result') or {})
            formal_result = formal_run_result(formal_run)
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
            monitoring_session['startup_initial_batch_last_initial_pending_count'] = initial_pending_count
            monitoring_session['startup_initial_batch_last_final_pending_count'] = current_pending_count
            monitoring_session['startup_initial_batch_last_initial_requester_fingerprint'] = initial_requester_fingerprint
            monitoring_session['startup_initial_batch_last_final_requester_fingerprint'] = requester_fingerprint(current_payload)
            monitoring_session['startup_initial_batch_last_probe_rechecks'] = startup_probe_rechecks
            if ok:
                success_formal_result = formal_result
                try:
                    verified_pending_after = int(success_formal_result.get('pending_after'))
                except (TypeError, ValueError):
                    verified_pending_after = None
                if verified_pending_after is None:
                    try:
                        success_recheck = _recheck_authoritative_group_state(
                            worker_base_url=target_worker_base_url,
                            registration_group=target_registration_group,
                            fresh_probe_cmd=target_fresh_probe_cmd,
                            use_worker_state_directly=use_worker_state_directly,
                            worker_timeout_seconds=args.worker_timeout_seconds,
                            command_timeout_seconds=args.command_timeout_seconds,
                        )
                        success_payload = success_recheck.get('decision_group', {}).get('payload') or {}
                        success_recheck_pending = success_payload.get('pending_count')
                        if success_recheck_pending is not None:
                            verified_pending_after = max(int(success_recheck_pending), 0)
                            current_payload = success_payload
                    except Exception:
                        verified_pending_after = None
                if verified_pending_after is not None and verified_pending_after >= 0:
                    current_pending_count = verified_pending_after
                    monitoring_session['startup_initial_batch_pending_count'] = current_pending_count
                    monitoring_session['startup_initial_batch_last_final_pending_count'] = current_pending_count
                monitoring_session['startup_initial_batch_done'] = True
                record_trigger(state, fingerprint=requester_fingerprint(current_payload), now=now)
                _store_cycle_anchor(
                    state,
                    bucket_name='registration_cycle_anchors',
                    at=now,
                    keys=[
                        target_registration_group,
                        str(target.get('binding_link') or '').strip(),
                        str(target.get('binding_group_name') or '').strip(),
                        target_group_name,
                        str(current_payload.get('group_id') or '').strip(),
                        str(current_payload.get('group_name') or '').strip(),
                    ],
                )
                target_cycle_anchor_at = now.isoformat()
                aggregate_approved_count = sum(
                    max(int(((formal_run_result((entry.get('result') or {})) or {}).get('approved_count') or 0)), 0)
                    for entry in attempt_results
                    if isinstance(entry, dict)
                )
                approval_run_ids = [
                    str((formal_run_payload((entry.get('result') or {})) or {}).get('approval_run_id') or '').strip()
                    for entry in attempt_results
                    if isinstance(entry, dict)
                ]
                approval_run_ids = [item for item in approval_run_ids if item]
                cycle['startup_initial_batch'] = {
                    'triggered': True,
                    'ok': True,
                    'session_id': session_id,
                    'initial_pending_count': initial_pending_count,
                    'initial_requester_fingerprint': initial_requester_fingerprint,
                    'final_pending_count': current_pending_count,
                    'final_requester_fingerprint': requester_fingerprint(current_payload),
                    'pending_count': current_pending_count,
                    'attempts': attempts,
                    'max_retries': max_retries,
                    'startup_probe_rechecks': startup_probe_rechecks,
                    'attempt_results': attempt_results,
                    'aggregate_approved_count': aggregate_approved_count,
                    'approval_run_ids': approval_run_ids,
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
                recheck_fingerprint = requester_fingerprint(recheck_payload)
                attempt_entry['recheck_pending_count'] = current_pending_count
                attempt_entry['recheck_requester_fingerprint'] = recheck_fingerprint
                if current_pending_count <= 0:
                    monitoring_session['startup_initial_batch_done'] = True
                    _store_cycle_anchor(
                        state,
                        bucket_name='registration_cycle_anchors',
                        at=now,
                        keys=[
                            target_registration_group,
                            str(target.get('binding_link') or '').strip(),
                            str(target.get('binding_group_name') or '').strip(),
                            target_group_name,
                            str(recheck_payload.get('group_id') or '').strip(),
                            str(recheck_payload.get('group_name') or '').strip(),
                        ],
                    )
                    target_cycle_anchor_at = now.isoformat()
                    aggregate_approved_count = sum(
                        max(int(((formal_run_result((entry.get('result') or {})) or {}).get('approved_count') or 0)), 0)
                        for entry in attempt_results
                        if isinstance(entry, dict)
                    )
                    approval_run_ids = [
                        str((formal_run_payload((entry.get('result') or {})) or {}).get('approval_run_id') or '').strip()
                        for entry in attempt_results
                        if isinstance(entry, dict)
                    ]
                    approval_run_ids = [item for item in approval_run_ids if item]
                    cycle['startup_initial_batch'] = {
                        'triggered': True,
                        'ok': True,
                        'session_id': session_id,
                        'initial_pending_count': initial_pending_count,
                        'initial_requester_fingerprint': initial_requester_fingerprint,
                        'final_pending_count': 0,
                        'final_requester_fingerprint': requester_fingerprint(recheck_payload),
                        'pending_count': 0,
                        'attempts': attempts,
                        'max_retries': max_retries,
                        'startup_probe_rechecks': startup_probe_rechecks,
                        'attempt_results': attempt_results,
                        'aggregate_approved_count': aggregate_approved_count,
                        'approval_run_ids': approval_run_ids,
                        'cleared_after_recheck': True,
                    }
                    return cycle
                if (
                    str(formal_result.get('result_code') or '').strip() == 'no_pending_request'
                    and current_pending_count > 0
                    and recheck_fingerprint
                    and recheck_fingerprint == initial_requester_fingerprint
                ):
                    attempt_entry['stable_queue_after_no_pending'] = True
                    stable_queue_after_no_pending = True
                    break
            except Exception as exc:
                attempt_entry['recheck_error'] = str(exc)
                current_pending_count = 0
                break
        monitoring_session['startup_initial_batch_done'] = True
        cycle['startup_initial_batch'] = {
            'triggered': bool(attempt_results),
            'ok': False if attempt_results else True,
            'session_id': session_id,
            'initial_pending_count': initial_pending_count,
            'initial_requester_fingerprint': initial_requester_fingerprint,
            'final_pending_count': current_pending_count,
            'final_requester_fingerprint': requester_fingerprint(current_payload),
            'pending_count': current_pending_count,
            'attempts': attempts,
            'max_retries': max_retries,
            'startup_probe_rechecks': startup_probe_rechecks,
            'attempt_results': attempt_results,
            'stable_queue_after_no_pending': stable_queue_after_no_pending,
            'retries_exhausted': bool(attempt_results and current_pending_count > 0 and not stable_queue_after_no_pending),
        }
        return cycle

    if schedule_end_flush_due and pending_count > 0:
        release = _schedule_end_flush_release(target, target_registration_group, authoritative_payload)
        cycle['release_evaluation'] = {'ok': True, 'payload': release}
    else:
        release_result = _evaluate_release_with_backend_recovery(
            args,
            target_registration_group,
            authoritative_payload,
            batch_size=int(target.get('approval_count_threshold') or 0),
            timeout_minutes=int(target.get('approval_timeout_minutes') or 0),
            cycle_anchor_at=target_cycle_anchor_at,
        )
        cycle['release_evaluation'] = release_result
        if not release_result.get('ok'):
            return cycle
        release = release_result['payload']

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

    attempts: List[Dict[str, Any]] = []
    approval_run_ids: List[str] = []
    seen_fingerprints = {fingerprint} if fingerprint else set()
    current_payload = authoritative_payload
    current_release = release
    current_fingerprint = fingerprint
    current_pending_count = pending_count
    drain_rechecks: List[Dict[str, Any]] = []
    max_drain_rounds = 3
    release_count = max(1, int(current_release.get('release_count') or 0))
    total_approved_count = 0
    ok = True

    for drain_round in range(1, max_drain_rounds + 1):
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
        formal_result = formal_run_result(result.get('result') or {})
        approved_count = max(int(formal_result.get('approved_count', release_count) or release_count), 0)
        approval_run_id = str((result.get('result') or {}).get('formal_run', {}).get('approval_run_id') or '').strip()
        if approval_run_id:
            approval_run_ids.append(approval_run_id)
        total_approved_count += approved_count
        attempts.append({
            'drain_round': drain_round,
            'fingerprint': current_fingerprint,
            'pending_count': current_pending_count,
            'release_count': release_count,
            'reason_code': str(current_release.get('reason_code') or ''),
            'command': command,
            'result': result.get('result'),
            'returncode': result.get('returncode'),
            'stderr': result.get('stderr'),
            'stdout': result.get('stdout'),
            'ok': ok,
            'approved_count': approved_count,
        })
        record_trigger(state, fingerprint=current_fingerprint, now=now)
        _store_cycle_anchor(
            state,
            bucket_name='registration_cycle_anchors',
            at=now,
            keys=[
                target_registration_group,
                str(target.get('binding_link') or '').strip(),
                str(target.get('binding_group_name') or '').strip(),
                target_group_name,
                str(current_payload.get('group_id') or '').strip(),
                str(current_payload.get('group_name') or '').strip(),
            ],
        )
        target_cycle_anchor_at = now.isoformat()
        if not ok:
            break
        if drain_round >= max_drain_rounds:
            break

        next_payload = current_payload
        next_release = current_release
        settled = False
        for recheck_attempt in range(1, 4):
            try:
                recheck = _recheck_authoritative_group_state(
                    worker_base_url=target_worker_base_url,
                    registration_group=target_registration_group,
                    fresh_probe_cmd=target_fresh_probe_cmd,
                    use_worker_state_directly=use_worker_state_directly,
                    worker_timeout_seconds=args.worker_timeout_seconds,
                    command_timeout_seconds=args.command_timeout_seconds,
                )
                next_payload = recheck['decision_group']['payload']
                next_release = _evaluate_release(
                    args.api_base_url,
                    target_registration_group,
                    next_payload,
                    batch_size=int(target.get('approval_count_threshold') or 0),
                    timeout_minutes=int(target.get('approval_timeout_minutes') or 0),
                    cycle_anchor_at=target_cycle_anchor_at,
                )
                next_fingerprint = requester_fingerprint(next_payload)
                next_pending_count = max(int(next_payload.get('pending_count') or 0), 0)
                next_ready = bool(next_release.get('ready')) and int(next_release.get('release_count') or 0) > 0
                recheck_entry: Dict[str, Any] = {
                    'drain_round': drain_round,
                    'recheck_attempt': recheck_attempt,
                    'pending_count': next_pending_count,
                    'fingerprint': next_fingerprint,
                    'ready': next_ready,
                    'release_count': int(next_release.get('release_count') or 0),
                    'reason_code': str(next_release.get('reason_code') or ''),
                }
                if next_pending_count <= 0 or not next_ready or (next_fingerprint and next_fingerprint in seen_fingerprints):
                    recheck_entry['stop'] = True
                    drain_rechecks.append(recheck_entry)
                    settled = True
                    current_payload = next_payload
                    current_release = next_release
                    current_pending_count = next_pending_count
                    current_fingerprint = next_fingerprint
                    break
                drain_rechecks.append(recheck_entry)
                current_payload = next_payload
                current_release = next_release
                current_pending_count = next_pending_count
                current_fingerprint = next_fingerprint
                seen_fingerprints.add(next_fingerprint)
                release_count = max(1, int(current_release.get('release_count') or 0))
                settled = True
                break
            except Exception as exc:
                drain_rechecks.append({
                    'drain_round': drain_round,
                    'recheck_attempt': recheck_attempt,
                    'error': str(exc),
                })
            if recheck_attempt < 3:
                time.sleep(1.0)
        if not settled:
            break
        if current_pending_count <= 0:
            break
        if not (bool(current_release.get('ready')) and int(current_release.get('release_count') or 0) > 0):
            break
        if current_fingerprint and current_fingerprint in {entry.get('fingerprint') for entry in attempts}:
            break

    last_attempt = attempts[-1] if attempts else {}
    cycle['formal_approval'] = {
        'triggered': True,
        'ok': ok,
        'fingerprint': fingerprint,
        'release_count': max(1, int(release.get('release_count') or 0)),
        'command': last_attempt.get('command'),
        'result': last_attempt.get('result'),
        'returncode': last_attempt.get('returncode'),
        'stderr': last_attempt.get('stderr'),
        'stdout': last_attempt.get('stdout'),
        'reason_code': str(release.get('reason_code') or ''),
        'trigger_cooldown_seconds': trigger_cooldown_seconds,
        'attempt_results': attempts,
        'approval_run_ids': approval_run_ids,
        'aggregate_approved_count': total_approved_count,
        'drain_rounds': len(attempts),
        'drain_rechecks': drain_rechecks,
        'final_pending_count': current_pending_count,
        'final_fingerprint': current_fingerprint,
        'result': {
            'formal_run': last_attempt.get('result', {}).get('formal_run') if isinstance(last_attempt.get('result'), dict) else None,
            'formal_runs': [attempt.get('result', {}).get('formal_run') for attempt in attempts if isinstance(attempt.get('result'), dict) and isinstance(attempt.get('result', {}).get('formal_run'), dict)],
        },
    }
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
    ordered_targets = _ordered_cycle_targets(
        monitor_target,
        fallback_target,
        now=now,
        poll_interval_seconds=float(getattr(args, 'approval_poll_interval_seconds', 0.0) or 0.0),
    )
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
    if not backend_health.get('ok'):
        passive_retry_wait_seconds = max(1.0, min(float(args.restart_wait_seconds or 1.0), 3.0))
        passive_retry_attempts: List[Dict[str, Any]] = []
        for attempt in range(1, 3):
            time.sleep(passive_retry_wait_seconds)
            retry_health = check_backend_health(args.api_base_url, timeout=args.health_timeout_seconds)
            passive_retry_attempts.append({'attempt': attempt, **retry_health})
            if retry_health.get('ok'):
                recovered_health = dict(retry_health)
                recovered_health['recovered_after_retry'] = True
                recovered_health['retry_attempts'] = passive_retry_attempts
                cycle['backend_health_recovery'] = {
                    'before_retry': backend_health,
                    'retry_attempts': passive_retry_attempts,
                }
                backend_health = recovered_health
                break
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
                recovered_health['retry_attempts'] = passive_retry_attempts
                cycle['backend_health_recovery'] = {
                    'before_restart': backend_health,
                    'retry_attempts': passive_retry_attempts,
                    'after_restart': after_restart,
                }
                backend_health = recovered_health
    cycle['backend_health'] = backend_health
    if not backend_health.get('ok'):
        return cycle

    cycle['temp_cleanup'] = _maybe_auto_cleanup_temp_profiles(args, state, now=now)

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


def _load_notify_profile_env(profile_name: str) -> Dict[str, str]:
    normalized = str(profile_name or '').strip()
    if not normalized:
        return {}
    env_path = Path.home() / '.hermes' / 'profiles' / normalized / '.env'
    if not env_path.exists():
        return {}
    values: Dict[str, str] = {}
    try:
        for raw_line in env_path.read_text(encoding='utf-8').splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    except Exception:
        return {}
    return values



def _expand_notify_profile_targets(profile_name: str, notify_robot_name: str = '') -> List[Dict[str, str]]:
    return [
        {
            'profile_name': str(item.get('profile_name') or '').strip(),
            'robot_name': str(item.get('robot_name') or '').strip(),
        }
        for item in expand_notify_profile_targets(profile_name, notify_robot_name)
    ]



def _build_notifier_from_args(args: argparse.Namespace, cycle: Optional[Dict[str, Any]] = None) -> Optional[FeishuNotifier]:
    if not args.notify_enabled:
        return None
    target = cycle.get('monitor_target') if isinstance(cycle, dict) else {}
    profile_name = str((target or {}).get('notify_profile_name') or '').strip()
    profile_env = _load_notify_profile_env(profile_name) if profile_name else {}
    app_id = str(profile_env.get('FEISHU_APP_ID') or args.feishu_app_id or '').strip()
    app_secret = str(profile_env.get('FEISHU_APP_SECRET') or args.feishu_app_secret or '').strip()
    chat_id = str(profile_env.get('FEISHU_HOME_CHANNEL') or args.notify_chat_id or '').strip()
    domain = str(profile_env.get('FEISHU_DOMAIN') or args.feishu_domain or 'lark').strip() or 'lark'
    if not app_id or not app_secret or not chat_id:
        return None
    return FeishuNotifier(
        app_id=app_id,
        app_secret=app_secret,
        chat_id=chat_id,
        domain=domain,
    )


SUCCESS_NOTIFICATION_CODES = {
    code
    for code, policy in NOTIFICATION_POLICY_BY_CODE.items()
    if policy.get('family') == 'success'
}


def _normalize_notification_state(state: Dict[str, Any], *, now_iso: str | None = None) -> bool:
    notification_bucket = state.get('notifications')
    if not isinstance(notification_bucket, dict):
        return False
    repaired = False
    repaired_at = str(now_iso or utc_now_iso())
    for dedupe_key, record in list(notification_bucket.items()):
        if not isinstance(record, dict):
            continue
        updated = dict(record)
        had_backfill_marker = 'backfilled_from' in updated or 'backfilled' in updated
        if 'backfilled_from' in updated:
            updated.pop('backfilled_from', None)
            repaired = True
        if 'backfilled' in updated:
            updated.pop('backfilled', None)
            repaired = True
        if str(updated.get('last_status') or '').strip() == 'backfilled':
            updated.pop('last_sent_at', None)
            updated.pop('deliveries', None)
            updated.pop('sent_count', None)
            updated['last_status'] = 'legacy_backfill_cleared'
            updated['state_repaired_from_backfill'] = True
            updated['state_repaired_at'] = repaired_at
            repaired = True
        elif had_backfill_marker:
            updated['state_repaired_from_backfill'] = True
            updated['state_repaired_at'] = repaired_at
        notification_bucket[dedupe_key] = updated
    return repaired



def _incident_alert_threshold(incident: Dict[str, Any]) -> int:
    code = str(incident.get('code') or '').strip()
    if code == 'worker_state_failed':
        return 3
    return 1


def _notify_incidents(args: argparse.Namespace, state: Dict[str, Any], cycle: Dict[str, Any], incidents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    now = datetime.fromisoformat(cycle['checked_at']) if cycle.get('checked_at') else utc_now()
    _normalize_notification_state(state, now_iso=now.isoformat())
    sent: List[Dict[str, Any]] = []
    streaks = state.setdefault('incident_streaks', {})
    current_keys = {str(item.get('dedupe_key') or item.get('code') or 'incident') for item in incidents}
    for stale_key in list(streaks.keys()):
        if stale_key not in current_keys:
            streaks.pop(stale_key, None)
    for incident in incidents:
        if incident.get('notify_disabled'):
            continue
        dedupe_key = str(incident.get('dedupe_key') or incident.get('code') or 'incident')
        threshold = max(1, int(_incident_alert_threshold(incident)))
        streak_record = streaks.get(dedupe_key) or {}
        streak_count = int(streak_record.get('count') or 0) + 1
        streaks[dedupe_key] = {
            'count': streak_count,
            'threshold': threshold,
            'code': str(incident.get('code') or '').strip(),
            'last_seen_at': now.isoformat(),
        }
        if streak_count < threshold:
            continue
        notification_code = str(incident.get('code') or '').strip()
        success_notification = notification_code in SUCCESS_NOTIFICATION_CODES
        notification_record = (state.get('notifications') or {}).get(dedupe_key) or {}
        prior_deliveries = notification_record.get('deliveries') if isinstance(notification_record.get('deliveries'), list) else []
        previously_sent_targets = {
            (
                f"profile::{str(item.get('notify_profile_name') or '').strip()}"
                if str(item.get('notify_profile_name') or '').strip()
                else f"robot::{str(item.get('notify_robot_name') or '').strip()}"
            )
            for item in prior_deliveries
            if isinstance(item, dict) and str(item.get('status') or '').strip() == 'sent'
        }
        if success_notification and notification_record.get('last_sent_at') and str(notification_record.get('last_status') or '').strip() == 'sent':
            continue
        if not success_notification:
            if not register_notification(state, dedupe_key=dedupe_key, now=now, cooldown_seconds=args.notify_cooldown_seconds):
                continue
        notify_profile_name = str(incident.get('notify_profile_name') or '').strip()
        notify_robot_name = str(incident.get('notify_robot_name') or '').strip()
        effective_cycle = cycle
        if notify_profile_name or notify_robot_name:
            monitor_target = dict(cycle.get('monitor_target') or {}) if isinstance(cycle.get('monitor_target'), dict) else {}
            if notify_profile_name:
                monitor_target['notify_profile_name'] = notify_profile_name
            if notify_robot_name:
                monitor_target['notify_robot_name'] = notify_robot_name
            effective_cycle = {**cycle, 'monitor_target': monitor_target}
        resolved_notify_profile_name = str(notify_profile_name or (effective_cycle.get('monitor_target') or {}).get('notify_profile_name') or '').strip()
        resolved_notify_robot_name = str(notify_robot_name or (effective_cycle.get('monitor_target') or {}).get('notify_robot_name') or '').strip()
        payload = {
            'dedupe_key': dedupe_key,
            'sent_at': now.isoformat(),
            'code': incident.get('code'),
            'severity': incident.get('severity'),
            'streak_count': streak_count,
            'threshold': threshold,
        }
        if resolved_notify_profile_name:
            payload['notify_profile_name'] = resolved_notify_profile_name
        if resolved_notify_robot_name:
            payload['notify_robot_name'] = resolved_notify_robot_name
        preserved_deliveries: List[Dict[str, Any]] = [
            dict(item)
            for item in prior_deliveries
            if isinstance(item, dict)
            and str(item.get('status') or '').strip() == 'sent'
        ] if success_notification else []
        deliveries: List[Dict[str, Any]] = []
        targets = _expand_notify_profile_targets(resolved_notify_profile_name, resolved_notify_robot_name)
        if not targets:
            targets = [{'profile_name': resolved_notify_profile_name or None, 'robot_name': resolved_notify_robot_name or None}]
        for target in targets:
            target_profile_name = str(target.get('profile_name') or '').strip()
            target_robot_name = str(target.get('robot_name') or '').strip()
            target_key = f'profile::{target_profile_name}' if target_profile_name else f'robot::{target_robot_name}'
            if success_notification and target_key in previously_sent_targets:
                continue
            monitor_target = dict(effective_cycle.get('monitor_target') or {}) if isinstance(effective_cycle.get('monitor_target'), dict) else {}
            if target_profile_name:
                monitor_target['notify_profile_name'] = target_profile_name
            if target_robot_name:
                monitor_target['notify_robot_name'] = target_robot_name
            target_cycle = {**effective_cycle, 'monitor_target': monitor_target}
            delivery = {
                'notify_profile_name': target_profile_name or None,
                'notify_robot_name': target_robot_name or None,
            }
            notifier = _build_notifier_from_args(args, target_cycle)
            if notifier is None:
                delivery['status'] = 'skipped_no_notifier'
                deliveries.append(delivery)
                continue
            try:
                response = notifier.send_text(format_lark_alert(SERVICE_NAME, incident, target_cycle))
                delivery['status'] = 'sent'
                delivery['response'] = response
            except Exception as exc:
                delivery['status'] = 'failed'
                delivery['error'] = str(exc)
            deliveries.append(delivery)
        all_deliveries = [*preserved_deliveries, *deliveries]
        payload['deliveries'] = all_deliveries
        statuses = {str(item.get('status') or '') for item in all_deliveries}
        if statuses == {'sent'}:
            payload['status'] = 'sent'
        elif 'sent' in statuses and ('failed' in statuses or 'skipped_no_notifier' in statuses):
            payload['status'] = 'partial_sent'
        elif 'failed' in statuses:
            payload['status'] = 'failed'
        else:
            payload['status'] = 'skipped_no_notifier'
        if success_notification:
            notification_bucket = state.setdefault('notifications', {})
            existing_record = notification_bucket.get(dedupe_key) or {}
            preserved_record = dict(existing_record)
            if payload['status'] == 'sent':
                register_notification(state, dedupe_key=dedupe_key, now=now, cooldown_seconds=args.notify_cooldown_seconds)
                existing_record = {**preserved_record, **((state.get('notifications') or {}).get(dedupe_key) or {})}
            updated_record = dict(existing_record)
            updated_record['last_status'] = payload['status']
            updated_record['deliveries'] = [
                {
                    'notify_profile_name': item.get('notify_profile_name'),
                    'notify_robot_name': item.get('notify_robot_name'),
                    'status': item.get('status'),
                    'error': item.get('error'),
                }
                for item in all_deliveries
                if isinstance(item, dict)
            ]
            notification_bucket[dedupe_key] = updated_record
        else:
            notification_bucket = state.setdefault('notifications', {})
            existing_record = notification_bucket.get(dedupe_key) or {}
            updated_record = dict(existing_record)
            updated_record['last_status'] = payload['status']
            notification_bucket[dedupe_key] = updated_record
        sent.append(payload)
    return sent


def _notification_delivery_summary(notifications: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    summary: List[Dict[str, Any]] = []
    for item in notifications:
        deliveries = item.get('deliveries') or []
        delivery_summary: List[Dict[str, Any]] = []
        if isinstance(deliveries, list):
            for delivery in deliveries:
                if not isinstance(delivery, dict):
                    continue
                delivery_summary.append({
                    'notify_profile_name': delivery.get('notify_profile_name'),
                    'notify_robot_name': delivery.get('notify_robot_name'),
                    'status': delivery.get('status'),
                    'error': delivery.get('error'),
                })
        summary.append({
            'code': item.get('code'),
            'status': item.get('status'),
            'notify_profile_name': item.get('notify_profile_name'),
            'notify_robot_name': item.get('notify_robot_name'),
            'deliveries': delivery_summary,
        })
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description='Background production operator for registration-group live monitoring and formal approval.')
    parser.add_argument('--api-base-url', default='http://127.0.0.1:8011')
    parser.add_argument('--worker-base-url', default='')
    parser.add_argument('--registration-group', default='')
    parser.add_argument('--fresh-probe-cmd', default='')
    parser.add_argument('--independent-truth-probe-cmd', default='')
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
    parser.add_argument('--worker-recovery-rebuild-threshold', type=int, default=2)
    parser.add_argument('--worker-recovery-rebuild-cooldown-seconds', type=float, default=120.0)
    parser.add_argument('--area', default='Indonesia')
    parser.add_argument('--remark', default='production auto approval daemon')
    parser.add_argument('--approved-count', type=int, default=1)
    parser.add_argument('--approval-poll-interval-seconds', type=float, default=0.1)
    parser.add_argument('--approval-poll-timeout-seconds', type=float, default=60.0)
    parser.add_argument('--decided-by', default='Hermes')
    parser.add_argument('--decided-by-name', default='Song Yuqi')
    parser.add_argument('--auto-recover-worker', action='store_true', default=True)
    parser.add_argument('--no-auto-recover-worker', dest='auto_recover_worker', action='store_false')
    parser.add_argument('--temp-cleanup-enabled', action='store_true', default=True)
    parser.add_argument('--temp-cleanup-disabled', dest='temp_cleanup_enabled', action='store_false')
    parser.add_argument('--temp-cleanup-interval-seconds', type=float, default=600.0)
    parser.add_argument('--temp-cleanup-min-age-hours', type=float, default=1.0)
    parser.add_argument('--monitoring-session-id', default='')
    args = parser.parse_args()

    if not args.fresh_probe_cmd:
        args.fresh_probe_cmd = _build_default_fresh_probe_cmd(args.registration_group)
    if not args.worker_event_log:
        args.worker_event_log = str(ROOT_DIR / 'webjs-approval-worker' / 'logs' / 'registration_group_webjs_worker.jsonl')

    state_path = Path(args.state_path).expanduser().resolve()
    status_path = Path(args.status_path).expanduser().resolve()
    state = load_json_state(state_path)
    _normalize_notification_state(state)

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
        cycle['notification_delivery_summary'] = _notification_delivery_summary(notifications)
        save_json_state(status_path, cycle)
        save_json_state(state_path, state)
        print(json.dumps({
            'checked_at': cycle.get('checked_at'),
            'pending_incidents': [item.get('code') for item in incidents],
            'success_notifications': [item.get('code') for item in success_notifications],
            'notified': [item.get('code') for item in [*incidents, *success_notifications] if any(n.get('code') == item.get('code') and n.get('status') == 'sent' for n in notifications)],
            'notification_delivery_summary': cycle.get('notification_delivery_summary') or [],
            'formal_triggered': bool((cycle.get('formal_approval') or {}).get('triggered')),
            'formal_ok': (cycle.get('formal_approval') or {}).get('ok'),
        }, ensure_ascii=False), flush=True)
        if args.run_once:
            return 0
        time.sleep(max(1.0, float(args.interval_seconds)))


if __name__ == '__main__':
    raise SystemExit(main())
