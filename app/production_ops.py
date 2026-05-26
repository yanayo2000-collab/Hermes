from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_NOTIFICATION_COOLDOWN_SECONDS = 900
DEFAULT_TRIGGER_COOLDOWN_SECONDS = 120
ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ALERT_TEMPLATES_PATH = ROOT_DIR / 'data' / 'production_ops_alert_templates.json'

NOTIFICATION_POLICY_BY_CODE: Dict[str, Dict[str, str]] = {
    'backend_unhealthy': {
        'family': 'incident',
        'dedupe': 'cooldown',
        'retry': 'after_cooldown',
        'partial_sent': 'n/a',
    },
    'worker_state_failed': {
        'family': 'incident',
        'dedupe': 'cooldown',
        'retry': 'after_cooldown',
        'partial_sent': 'n/a',
    },
    'release_evaluation_failed': {
        'family': 'incident',
        'dedupe': 'cooldown',
        'retry': 'after_cooldown',
        'partial_sent': 'n/a',
    },
    'formal_approval_failed': {
        'family': 'incident',
        'dedupe': 'cooldown',
        'retry': 'after_cooldown',
        'partial_sent': 'n/a',
    },
    'startup_initial_batch_failed': {
        'family': 'incident',
        'dedupe': 'cooldown',
        'retry': 'after_cooldown',
        'partial_sent': 'n/a',
    },
    'daemon_cycle_error': {
        'family': 'incident',
        'dedupe': 'cooldown',
        'retry': 'after_cooldown',
        'partial_sent': 'n/a',
    },
    'formal_approval_succeeded': {
        'family': 'success',
        'dedupe': 'one_shot_per_dedupe_key',
        'retry': 'until_all_targets_sent',
        'partial_sent': 'retry_unsent_targets_only',
    },
    'formal_approval_recovered': {
        'family': 'success',
        'dedupe': 'one_shot_per_dedupe_key',
        'retry': 'until_all_targets_sent',
        'partial_sent': 'retry_unsent_targets_only',
    },
    'worker_probe_recovered': {
        'family': 'success',
        'dedupe': 'one_shot_per_dedupe_key',
        'retry': 'until_all_targets_sent',
        'partial_sent': 'retry_unsent_targets_only',
    },
    'independent_truth_conflict_p0': {
        'family': 'incident',
        'dedupe': 'cooldown',
        'retry': 'after_cooldown',
        'partial_sent': 'n/a',
    },
    'registration_cycle_noop': {
        'family': 'success',
        'dedupe': 'one_shot_per_dedupe_key',
        'retry': 'until_all_targets_sent',
        'partial_sent': 'retry_unsent_targets_only',
    },
    'registration_duplicate_group_request_skipped': {
        'family': 'success',
        'dedupe': 'one_shot_per_dedupe_key',
        'retry': 'until_all_targets_sent',
        'partial_sent': 'retry_unsent_targets_only',
    },
    'official_group_cycle_noop': {
        'family': 'success',
        'dedupe': 'one_shot_per_dedupe_key',
        'retry': 'until_all_targets_sent',
        'partial_sent': 'retry_unsent_targets_only',
    },
    'startup_initial_batch_succeeded': {
        'family': 'success',
        'dedupe': 'one_shot_per_dedupe_key',
        'retry': 'until_all_targets_sent',
        'partial_sent': 'retry_unsent_targets_only',
    },
    'official_group_approval_succeeded': {
        'family': 'success',
        'dedupe': 'one_shot_per_dedupe_key',
        'retry': 'until_all_targets_sent',
        'partial_sent': 'retry_unsent_targets_only',
    },
    'official_group_manual_review_required': {
        'family': 'success',
        'dedupe': 'one_shot_per_dedupe_key',
        'retry': 'until_all_targets_sent',
        'partial_sent': 'retry_unsent_targets_only',
    },
    'manual_approval_succeeded': {
        'family': 'app_direct_success',
        'dedupe': 'app_layer',
        'retry': 'app_layer',
        'partial_sent': 'app_layer',
    },
}


def _default_alert_templates() -> Dict[str, Any]:
    return {
        'headers': {
            'critical': {'icon': '🚨', 'label': '生产守护告警'},
            'warning': {'icon': '⚠️', 'label': '生产守护提醒'},
            'info': {'icon': '✅', 'label': '生产守护通知'},
            'default': {'icon': 'ℹ️', 'label': '生产守护通知'},
        },
        'reasons': {
            'formal_approval_succeeded': '已审批通过 {approved_count} 人',
            'formal_approval_recovered': '自动重试成功，审批已完成，CRM 已写入',
            'worker_probe_recovered': '探针已自动恢复，群状态读取恢复正常',
            'manual_approval_succeeded': '已人工审批通过 {approved_count} 人',
            'startup_initial_batch_succeeded': '启动首批审批已通过 {approved_count} 人',
            'official_group_approval_succeeded': '已审批通过 {approved_count} 人',
            'registration_cycle_noop': '审批时间已到，未发生实际审批',
            'official_group_cycle_noop': '审批时间已到，未发生实际审批',
        },
    }


def load_alert_templates(path: Optional[Path] = None) -> Dict[str, Any]:
    templates = _default_alert_templates()
    target_path = Path(path or DEFAULT_ALERT_TEMPLATES_PATH)
    if not target_path.exists():
        return templates
    try:
        payload = json.loads(target_path.read_text(encoding='utf-8'))
    except Exception:
        return templates
    if not isinstance(payload, dict):
        return templates
    for section in ('headers', 'reasons'):
        incoming = payload.get(section)
        if not isinstance(incoming, dict):
            continue
        base = templates.setdefault(section, {})
        for key, value in incoming.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merged = dict(base[key])
                merged.update({k: v for k, v in value.items() if isinstance(v, str) and v.strip()})
                base[key] = merged
            elif isinstance(value, str) and value.strip():
                base[key] = value
    return templates


def render_alert_reason_template(code: str, *, approved_count: Any = None, fallback: str = '') -> str:
    template = str(load_alert_templates().get('reasons', {}).get(code) or '').strip()
    if not template:
        return fallback
    try:
        approved_value = int(approved_count)
    except (TypeError, ValueError):
        approved_value = None
    if approved_value is not None and approved_value > 0:
        return template.format(approved_count=approved_value)
    return fallback or template.replace(' {approved_count} 人', '').strip()


def expand_notify_profile_targets(profile_name: Optional[str], notify_robot_name: Optional[str] = None) -> List[Dict[str, Optional[str]]]:
    normalized = str(profile_name or '').strip()
    normalized_robot_name = str(notify_robot_name or '').strip()
    if not normalized:
        return []
    if normalized == 'wa-approval-broadcast':
        return [{
            'profile_name': 'wa-approval-broadcast',
            'robot_name': normalized_robot_name or '审批bot01',
        }]
    if normalized == 'wa-approval-broadcast-02':
        return [{
            'profile_name': 'wa-approval-broadcast-02',
            'robot_name': normalized_robot_name or '审批Bot02',
        }]
    return [{
        'profile_name': normalized,
        'robot_name': normalized_robot_name or normalized,
    }]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def load_json_state(path: Path) -> Dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_json_state(path: Path, payload: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def fetch_json(url: str, *, method: str = 'GET', payload: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
    data = None
    headers: Dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    internal_token = str(os.getenv('AUTH_INTERNAL_TOKEN') or '').strip()
    if internal_token and '/api/ops/' in str(url or ''):
        headers['x-ops-internal-token'] = internal_token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode('utf-8'))
    if not isinstance(body, dict):
        raise RuntimeError(f'unexpected non-dict response from {url}')
    return body


def check_backend_health(api_base_url: str, *, timeout: float = 10.0) -> Dict[str, Any]:
    url = f"{api_base_url.rstrip('/')}/health"
    try:
        payload = fetch_json(url, timeout=timeout)
        return {'ok': True, 'payload': payload}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}


def maybe_restart(command: Optional[str], *, timeout: float = 120.0) -> Dict[str, Any]:
    if not command:
        return {'attempted': False, 'ok': False, 'reason': 'restart_command_missing'}
    completed = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
    return {
        'attempted': True,
        'ok': completed.returncode == 0,
        'returncode': completed.returncode,
        'stdout': completed.stdout,
        'stderr': completed.stderr,
        'command': command,
    }


def oldest_pending_at_iso(group_state: Dict[str, Any]) -> Optional[str]:
    requesters = group_state.get('requesters') or []
    values: List[str] = []
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


def formal_run_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get('formal_run'), dict):
        return payload.get('formal_run') or {}
    result = payload.get('result')
    if isinstance(result, dict) and isinstance(result.get('formal_run'), dict):
        return result.get('formal_run') or {}
    if 'approval_run_id' in payload or 'final_status' in payload:
        return payload
    return {}



def formal_run_result(payload: Any) -> Dict[str, Any]:
    formal_run = formal_run_payload(payload)
    if not formal_run:
        return {}
    direct = formal_run.get('result')
    if isinstance(direct, dict):
        return direct
    final_status = formal_run.get('final_status')
    if isinstance(final_status, dict):
        final_result = final_status.get('result')
        if isinstance(final_result, dict):
            return final_result
    return {}



def startup_attempts_summary(startup_batch: Any) -> Dict[str, Any]:
    startup = startup_batch if isinstance(startup_batch, dict) else {}
    attempt_results = startup.get('attempt_results') or []
    approval_run_ids: List[str] = []
    aggregate_approved_count = 0
    last_verified_formal_run: Dict[str, Any] = {}
    last_verified_formal_result: Dict[str, Any] = {}
    verified_attempts: List[Dict[str, Any]] = []
    last_attempt = attempt_results[-1] if isinstance(attempt_results, list) and attempt_results else {}
    if not isinstance(attempt_results, list):
        attempt_results = []
    for idx, attempt in enumerate(attempt_results, start=1):
        if not isinstance(attempt, dict):
            continue
        formal_run = formal_run_payload((attempt.get('result') or {}))
        formal_result = formal_run_result(formal_run)
        if not formal_run or formal_result.get('verified') is not True or formal_result.get('crm_recorded') is not True:
            continue
        approval_run_id = str(formal_run.get('approval_run_id') or '').strip()
        if approval_run_id and approval_run_id not in approval_run_ids:
            approval_run_ids.append(approval_run_id)
        approved_count = 0
        try:
            approved_count = max(int(formal_result.get('approved_count') or 0), 0)
            aggregate_approved_count += approved_count
        except (TypeError, ValueError):
            approved_count = 0
        verified_attempts.append({
            'attempt_number': idx,
            'approval_run_id': approval_run_id or None,
            'formal_run': formal_run,
            'formal_result': formal_result,
            'approved_count': approved_count,
        })
        last_verified_formal_run = formal_run
        last_verified_formal_result = formal_result
    if not last_verified_formal_run:
        fallback_formal_run = formal_run_payload((last_attempt or {}).get('result') or startup.get('result') or {})
        fallback_formal_result = formal_run_result(fallback_formal_run)
        if fallback_formal_run and fallback_formal_result.get('verified') is True and fallback_formal_result.get('crm_recorded') is True:
            approval_run_id = str(fallback_formal_run.get('approval_run_id') or '').strip()
            if approval_run_id and approval_run_id not in approval_run_ids:
                approval_run_ids.append(approval_run_id)
            if aggregate_approved_count <= 0:
                try:
                    aggregate_approved_count = max(int(fallback_formal_result.get('approved_count') or 0), 0)
                except (TypeError, ValueError):
                    aggregate_approved_count = 0
            verified_attempts.append({
                'attempt_number': 1,
                'approval_run_id': approval_run_id or None,
                'formal_run': fallback_formal_run,
                'formal_result': fallback_formal_result,
                'approved_count': aggregate_approved_count,
            })
            last_verified_formal_run = fallback_formal_run
            last_verified_formal_result = fallback_formal_result
    return {
        'approval_run_ids': approval_run_ids,
        'aggregate_approved_count': aggregate_approved_count,
        'last_verified_formal_run': last_verified_formal_run,
        'last_verified_formal_result': last_verified_formal_result,
        'verified_attempts': verified_attempts,
    }



def formal_approval_success_attempts(formal_approval: Any) -> List[Dict[str, Any]]:
    action = formal_approval if isinstance(formal_approval, dict) else {}
    attempt_results = action.get('attempt_results') or []
    success_attempts: List[Dict[str, Any]] = []
    seen_run_ids: set[str] = set()
    if not isinstance(attempt_results, list):
        attempt_results = []
    for idx, attempt in enumerate(attempt_results, start=1):
        if not isinstance(attempt, dict):
            continue
        formal_run = formal_run_payload(attempt.get('result') or {})
        formal_result = formal_run_result(formal_run)
        if not formal_run or formal_result.get('verified') is not True or formal_result.get('crm_recorded') is not True:
            continue
        approval_run_id = str(formal_run.get('approval_run_id') or '').strip()
        if approval_run_id and approval_run_id in seen_run_ids:
            continue
        if approval_run_id:
            seen_run_ids.add(approval_run_id)
        approved_count = max(int(formal_result.get('approved_count') or 0), 0)
        success_attempts.append({
            'attempt_number': idx,
            'approval_run_id': approval_run_id or None,
            'formal_run': formal_run,
            'formal_result': formal_result,
            'approved_count': approved_count,
        })
    if success_attempts:
        return success_attempts

    formal_run = formal_run_payload(action.get('result') or {})
    formal_result = formal_run_result(formal_run)
    if formal_run and formal_result.get('verified') is True and formal_result.get('crm_recorded') is True:
        approval_run_id = str(formal_run.get('approval_run_id') or '').strip()
        approved_count = max(int(formal_result.get('approved_count') or 0), 0)
        return [{
            'attempt_number': 1,
            'approval_run_id': approval_run_id or None,
            'formal_run': formal_run,
            'formal_result': formal_result,
            'approved_count': approved_count,
        }]
    return []



def requester_fingerprint(group_state: Dict[str, Any]) -> str:
    requesters = group_state.get('requesters') or []
    parts: List[str] = []
    fallback_name_parts: List[str] = []
    if isinstance(requesters, list):
        for item in requesters:
            if not isinstance(item, dict):
                continue
            rid = str(item.get('requesterId') or '').strip()
            ts = str(item.get('requestedAtUnix') or item.get('requestedAtIso') or '').strip()
            if rid:
                parts.append(f'{rid}@{ts}')
                continue
            display_name = str(item.get('displayName') or item.get('display_name') or '').strip().lower()
            if display_name:
                fallback_name_parts.append(f'name:{display_name}@{ts}')
    if parts:
        return '|'.join(sorted(parts))
    requester_ids = group_state.get('requester_ids') or []
    if isinstance(requester_ids, list):
        normalized_ids = sorted(str(x).strip() for x in requester_ids if str(x).strip())
        if normalized_ids:
            return '|'.join(normalized_ids)
    if fallback_name_parts:
        return '|'.join(sorted(fallback_name_parts))
    return ''


def should_trigger_action(state: Dict[str, Any], *, fingerprint: str, now: datetime, cooldown_seconds: int = DEFAULT_TRIGGER_COOLDOWN_SECONDS) -> bool:
    if not fingerprint:
        return True
    actions = state.get('actions') or {}
    last = actions.get('last_registration_trigger') or {}
    if str(last.get('fingerprint') or '').strip() != fingerprint:
        return True
    last_at = str(last.get('at') or '').strip()
    if not last_at:
        return True
    try:
        last_dt = datetime.fromisoformat(last_at)
    except Exception:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return (now - last_dt).total_seconds() >= max(1, int(cooldown_seconds))


def record_trigger(state: Dict[str, Any], *, fingerprint: str, now: datetime) -> None:
    actions = state.setdefault('actions', {})
    actions['last_registration_trigger'] = {'fingerprint': fingerprint, 'at': now.isoformat()}


def register_notification(
    state: Dict[str, Any],
    *,
    dedupe_key: str,
    now: datetime,
    cooldown_seconds: int = DEFAULT_NOTIFICATION_COOLDOWN_SECONDS,
) -> bool:
    notifications = state.setdefault('notifications', {})
    record = notifications.get(dedupe_key) or {}
    last_sent_at = str(record.get('last_sent_at') or '').strip()
    if last_sent_at:
        try:
            last_dt = datetime.fromisoformat(last_sent_at)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if (now - last_dt).total_seconds() < max(1, int(cooldown_seconds)):
                record['suppressed_count'] = int(record.get('suppressed_count') or 0) + 1
                notifications[dedupe_key] = record
                return False
        except Exception:
            pass
    notifications[dedupe_key] = {
        'last_sent_at': now.isoformat(),
        'sent_count': int(record.get('sent_count') or 0) + 1,
        'suppressed_count': int(record.get('suppressed_count') or 0),
    }
    return True


def build_observation_warnings(cycle: Dict[str, Any]) -> List[Dict[str, Any]]:
    warnings: List[Dict[str, Any]] = []
    registration_cycles = list(cycle.get('registration_group_cycles') or []) if isinstance(cycle.get('registration_group_cycles'), list) else []
    for cycle_row in registration_cycles:
        if not isinstance(cycle_row, dict):
            continue
        row_release = cycle_row.get('release_evaluation') or {}
        row_payload = row_release.get('payload') if isinstance(row_release.get('payload'), dict) else {}
        if not row_release.get('ok') or str(row_payload.get('reason_code') or '').strip() != 'waiting_next_cycle':
            continue
        try:
            row_pending_count = int(row_payload.get('pending_count') or 0)
        except (TypeError, ValueError):
            row_pending_count = 0
        if row_pending_count != 0:
            continue
        decision_group = cycle_row.get('decision_group_state') if isinstance(cycle_row.get('decision_group_state'), dict) else {}
        if not decision_group.get('zero_pending_unverified'):
            continue
        group_label = str(
            ((cycle_row.get('monitor_target') or {}).get('group_name'))
            or ((cycle_row.get('monitor_target') or {}).get('binding_group_name'))
            or ((cycle_row.get('monitor_target') or {}).get('registration_group'))
            or cycle_row.get('registration_group')
            or ''
        ).strip()
        cycle_started_at = str(row_payload.get('cycle_started_at') or '').strip()
        cycle_ends_at = str(row_payload.get('cycle_ends_at') or '').strip()
        dedupe_suffix = '|'.join(part for part in [group_label, cycle_started_at or cycle_ends_at] if part) or 'unknown'
        warnings.append({
            'severity': 'warning',
            'category': 'observation_warning',
            'code': 'registration_zero_pending_unverified',
            'summary': '注册群零待审批待核验',
            'notify_disabled': True,
            'details': {
                'group_name': group_label or None,
                'pending_count': 0,
                'reason': str(decision_group.get('zero_pending_unverified_reason') or '').strip() or None,
                'cycle_started_at': cycle_started_at or None,
                'cycle_ends_at': cycle_ends_at or None,
            },
            'notify_profile_name': str(((cycle_row.get('monitor_target') or {}).get('notify_profile_name')) or '').strip() or None,
            'notify_robot_name': str(((cycle_row.get('monitor_target') or {}).get('notify_robot_name')) or '').strip() or None,
            'dedupe_key': f'registration_zero_pending_unverified:{dedupe_suffix}',
        })
    return warnings


def build_incidents(cycle: Dict[str, Any]) -> List[Dict[str, Any]]:
    incidents: List[Dict[str, Any]] = []
    backend = cycle.get('backend_health') or {}
    if not backend.get('ok'):
        incidents.append({
            'severity': 'critical',
            'code': 'backend_unhealthy',
            'summary': '后端健康检查失败',
            'details': backend,
            'dedupe_key': 'backend_unhealthy',
        })
    worker = cycle.get('worker_state') or {}
    if worker.get('ok') is False and not bool(worker.get('suppress_incident')):
        incidents.append({
            'severity': 'critical',
            'code': 'worker_state_failed',
            'summary': '群状态探测失败',
            'details': worker,
            'dedupe_key': 'worker_state_failed',
        })
    release = cycle.get('release_evaluation') or {}
    if release.get('ok') is False:
        incidents.append({
            'severity': 'critical',
            'code': 'release_evaluation_failed',
            'summary': '批次放行评估失败',
            'details': release,
            'dedupe_key': 'release_evaluation_failed',
        })
    action = cycle.get('formal_approval') or {}
    if action.get('triggered') and not action.get('ok'):
        fingerprint = str(action.get('fingerprint') or '').strip()
        formal_run = formal_run_payload(action.get('result') or {})
        approval_run_id = str(formal_run.get('approval_run_id') or '').strip()
        dedupe_suffix = fingerprint or 'unknown'
        formal_result = formal_run_result(formal_run)
        verified = formal_result.get('verified')
        crm_recorded = formal_result.get('crm_recorded')
        returncode = action.get('returncode')
        result_code = str(formal_result.get('result_code') or '').strip()
        if result_code == 'duplicate_registration_group_request':
            return incidents
        confirmed_failure = returncode not in (None, 0) or verified is False or crm_recorded is False
        if confirmed_failure:
            summary = '正式审批已中止' if result_code == 'requester_fingerprint_changed_before_approval' else '正式审批未闭环'
            incidents.append({
                'severity': 'critical',
                'code': 'formal_approval_failed',
                'summary': summary,
                'details': {
                    'fingerprint': fingerprint,
                    'pending_count': action.get('pending_count'),
                    'release_count': action.get('release_count'),
                    'reason_code': action.get('reason_code'),
                    'returncode': returncode,
                    'approval_run_id': approval_run_id,
                    'verified': verified,
                    'crm_recorded': crm_recorded,
                    'result_code': result_code or None,
                },
                'dedupe_key': f'formal_approval_failed:{dedupe_suffix}',
            })
    startup_batch = cycle.get('startup_initial_batch') or {}
    if startup_batch.get('triggered'):
        session_id = str(startup_batch.get('session_id') or '').strip()
        formal_run = formal_run_payload(startup_batch.get('result') or {})
        approval_run_id = str(formal_run.get('approval_run_id') or '').strip()
        dedupe_suffix = session_id or 'unknown'
        attempt_results = startup_batch.get('attempt_results') or []
        last_attempt = attempt_results[-1] if isinstance(attempt_results, list) and attempt_results else {}
        last_result = (last_attempt.get('result') or {}) if isinstance(last_attempt, dict) else {}
        last_formal_run = formal_run_payload(last_result)
        last_formal_result = formal_run_result(last_formal_run)
        verified = last_formal_result.get('verified')
        crm_recorded = last_formal_result.get('crm_recorded')
        last_returncode = last_attempt.get('returncode') if isinstance(last_attempt, dict) else None
        confirmed_failure = (
            (not startup_batch.get('ok'))
            or last_returncode not in (None, 0)
            or verified is False
            or crm_recorded is False
        )
        if confirmed_failure:
            incidents.append({
                'severity': 'critical',
                'code': 'startup_initial_batch_failed',
                'summary': '启动首批审批失败',
                'details': {
                    'session_id': session_id,
                    'pending_count': startup_batch.get('pending_count'),
                    'attempts': startup_batch.get('attempts'),
                    'max_retries': startup_batch.get('max_retries'),
                    'retries_exhausted': startup_batch.get('retries_exhausted'),
                    'last_returncode': last_returncode,
                    'last_approval_run_id': str(last_formal_run.get('approval_run_id') or '').strip() or approval_run_id or None,
                    'last_verified': verified,
                    'last_crm_recorded': crm_recorded,
                    'last_result_code': last_formal_result.get('result_code'),
                },
                'dedupe_key': f'startup_initial_batch_failed:{dedupe_suffix}',
            })
    cycle_error = str(cycle.get('cycle_error') or '').strip()
    registration_cycles = list(cycle.get('registration_group_cycles') or []) if isinstance(cycle.get('registration_group_cycles'), list) else []
    for cycle_row in registration_cycles:
        if not isinstance(cycle_row, dict):
            continue
        independent_probe = cycle_row.get('independent_truth_probe') if isinstance(cycle_row.get('independent_truth_probe'), dict) else {}
        conflict = independent_probe.get('conflict') if isinstance(independent_probe.get('conflict'), dict) else {}
        if not conflict.get('present') or str(conflict.get('severity') or '').strip() != 'critical':
            continue
        monitor_target = cycle_row.get('monitor_target') if isinstance(cycle_row.get('monitor_target'), dict) else {}
        group_label = str(
            monitor_target.get('group_name')
            or monitor_target.get('binding_group_name')
            or monitor_target.get('registration_group')
            or cycle_row.get('registration_group')
            or ''
        ).strip()
        object_key = '|'.join(part for part in [str(monitor_target.get('account_key') or '').strip(), group_label] if part) or 'unknown'
        incidents.append({
            'severity': 'critical',
            'code': 'independent_truth_conflict_p0',
            'summary': '独立巡检发现注册群状态冲突',
            'details': {
                'group_name': group_label or None,
                'conflict': conflict,
                'side_channel_only': True,
                'authoritative_action_blocked': True,
            },
            'notify_profile_name': str(monitor_target.get('notify_profile_name') or '').strip() or None,
            'notify_robot_name': str(monitor_target.get('notify_robot_name') or '').strip() or None,
            'dedupe_key': f'independent_truth_conflict_p0:{object_key}',
        })
    if cycle_error:
        incidents.append({
            'severity': 'critical',
            'code': 'daemon_cycle_error',
            'summary': '守护任务异常',
            'details': {'error': cycle_error},
            'dedupe_key': f'daemon_cycle_error:{cycle_error}',
        })
    return incidents


def _preferred_success_pending_after(action: Any, formal_result: Any, *, is_last_attempt: bool) -> Any:
    action_payload = action if isinstance(action, dict) else {}
    formal_payload = formal_result if isinstance(formal_result, dict) else {}
    formal_pending_after = formal_payload.get('pending_after')
    if formal_pending_after is not None:
        return formal_pending_after
    evidence_summary = formal_payload.get('evidence_summary') if isinstance(formal_payload.get('evidence_summary'), dict) else {}
    evidence_pending_after = evidence_summary.get('pending_after')
    if evidence_pending_after is not None:
        return evidence_pending_after
    if is_last_attempt:
        return action_payload.get('final_pending_count')
    return None



def build_success_notifications(cycle: Dict[str, Any]) -> List[Dict[str, Any]]:
    notifications: List[Dict[str, Any]] = []
    seen_dedupe_keys: set[str] = set()

    def append_notification(notification: Dict[str, Any]) -> None:
        code = str(notification.get('code') or '').strip()
        details = notification.get('details') if isinstance(notification.get('details'), dict) else {}
        approval_scope = str(notification.get('approval_scope') or '').strip()
        if not approval_scope:
            approval_scope = 'official_group' if code.startswith('official_group_') else 'registration_group'
            notification['approval_scope'] = approval_scope
        target_group_label = str(notification.get('target_group_label') or '').strip()
        if not target_group_label:
            target_group_label = str(
                details.get('group_name')
                or details.get('target_group')
                or ((cycle.get('monitor_target') or {}).get('group_name') if isinstance(cycle.get('monitor_target'), dict) else '')
                or ((cycle.get('monitor_target') or {}).get('binding_group_name') if isinstance(cycle.get('monitor_target'), dict) else '')
                or ((cycle.get('monitor_target') or {}).get('registration_group') if isinstance(cycle.get('monitor_target'), dict) else '')
                or cycle.get('registration_group')
                or ''
            ).strip()
            if target_group_label:
                notification['target_group_label'] = target_group_label
        if details:
            details.setdefault('approval_scope', approval_scope)
            if target_group_label:
                details.setdefault('target_group_label', target_group_label)
            notification['details'] = details
        dedupe_key = str(notification.get('dedupe_key') or '').strip()
        if dedupe_key and dedupe_key in seen_dedupe_keys:
            return
        if dedupe_key:
            seen_dedupe_keys.add(dedupe_key)
        notifications.append(notification)

    def append_duplicate_group_request_notification(action_payload: Dict[str, Any], monitor_target: Dict[str, Any], group_name: str) -> None:
        formal_run = formal_run_payload(action_payload.get('result') or {})
        formal_result = formal_run_result(formal_run)
        if str(formal_result.get('result_code') or '').strip() != 'duplicate_registration_group_request':
            return
        approval_run_id = str(formal_run.get('approval_run_id') or '').strip()
        fingerprint = str(action_payload.get('fingerprint') or '').strip()
        requested_group = str(formal_result.get('registration_group') or group_name or '').strip()
        active_group = str(formal_result.get('active_registration_group') or '').strip()
        append_notification({
            'severity': 'warning',
            'code': 'registration_duplicate_group_request_skipped',
            'summary': '注册群重复申请已拦截',
            'details': {
                'approval_run_id': approval_run_id or None,
                'fingerprint': fingerprint or None,
                'group_name': requested_group or None,
                'active_registration_group': active_group or None,
                'result_code': formal_result.get('result_code'),
                'result_reason': formal_result.get('result_reason'),
            },
            'notify_profile_name': str(monitor_target.get('notify_profile_name') or '').strip() or None,
            'notify_robot_name': str(monitor_target.get('notify_robot_name') or '').strip() or None,
            'dedupe_key': f"registration_duplicate_group_request_skipped:{approval_run_id or fingerprint or requested_group or 'unknown'}",
        })

    action = cycle.get('formal_approval') or {}
    top_monitor_target = cycle.get('monitor_target') if isinstance(cycle.get('monitor_target'), dict) else {}
    top_group_name = str(
        top_monitor_target.get('group_name')
        or top_monitor_target.get('binding_group_name')
        or top_monitor_target.get('registration_group')
        or cycle.get('registration_group')
        or ''
    ).strip()
    if action.get('triggered'):
        append_duplicate_group_request_notification(action, top_monitor_target, top_group_name)
    if action.get('triggered') and action.get('ok'):
        success_attempts = formal_approval_success_attempts(action)
        if success_attempts:
            fingerprint = str(action.get('fingerprint') or '').strip()
            monitor_target = cycle.get('monitor_target') if isinstance(cycle.get('monitor_target'), dict) else {}
            group_name = str(
                monitor_target.get('group_name')
                or monitor_target.get('binding_group_name')
                or monitor_target.get('registration_group')
                or cycle.get('registration_group')
                or ''
            ).strip()
            for idx, attempt in enumerate(success_attempts, start=1):
                formal_run = attempt.get('formal_run') if isinstance(attempt.get('formal_run'), dict) else {}
                formal_result = attempt.get('formal_result') if isinstance(attempt.get('formal_result'), dict) else {}
                approval_run_id = str(attempt.get('approval_run_id') or formal_run.get('approval_run_id') or '').strip()
                approval_batch_display_id = str(
                    formal_run.get('approval_batch_display_id')
                    or formal_result.get('approval_batch_display_id')
                    or ''
                ).strip()
                pending_after = _preferred_success_pending_after(
                    action,
                    formal_result,
                    is_last_attempt=idx == len(success_attempts),
                )
                dedupe_suffix = approval_run_id or fingerprint or f'unknown-{idx}'
                append_notification({
                    'severity': 'info',
                    'code': 'formal_approval_succeeded',
                    'summary': '注册群审批成功',
                    'details': {
                        'approval_run_id': approval_run_id or None,
                        'approval_batch_display_id': approval_batch_display_id or None,
                        'approval_run_ids': [approval_run_id] if approval_run_id else [],
                        'fingerprint': fingerprint or None,
                        'group_name': group_name or None,
                        'approved_count': attempt.get('approved_count') or formal_result.get('approved_count', action.get('release_count')),
                        'pending_after': pending_after,
                        'member_count_after': formal_result.get('member_count_after'),
                        'result_code': formal_result.get('result_code'),
                        'reason_code': action.get('reason_code'),
                        'drain_rounds': action.get('drain_rounds'),
                    },
                    'notify_profile_name': str(monitor_target.get('notify_profile_name') or '').strip() or None,
                    'notify_robot_name': str(monitor_target.get('notify_robot_name') or '').strip() or None,
                    'dedupe_key': f'formal_approval_succeeded:{dedupe_suffix}',
                })

    registration_cycles = list(cycle.get('registration_group_cycles') or []) if isinstance(cycle.get('registration_group_cycles'), list) else []
    for cycle_row in registration_cycles:
        if not isinstance(cycle_row, dict):
            continue
        release = cycle_row.get('release_evaluation') or {}
        payload = release.get('payload') if isinstance(release.get('payload'), dict) else {}
        if not release.get('ok') or payload.get('reason_code') != 'waiting_next_cycle':
            continue
        try:
            pending_count = int(payload.get('pending_count') or 0)
        except (TypeError, ValueError):
            pending_count = 0
        if pending_count != 0:
            continue
        decision_group = cycle_row.get('decision_group_state') if isinstance(cycle_row.get('decision_group_state'), dict) else {}
        fresh_probe = cycle_row.get('fresh_probe') if isinstance(cycle_row.get('fresh_probe'), dict) else {}
        if decision_group.get('zero_pending_unverified'):
            continue
        if fresh_probe.get('zero_pending_recheck') and not fresh_probe.get('ok'):
            continue
        cycle_started_at = str(payload.get('cycle_started_at') or '').strip()
        cycle_ends_at = str(payload.get('cycle_ends_at') or '').strip()
        cycle_anchor_at = str(payload.get('cycle_anchor_at') or '').strip()
        try:
            completed_cycles_since_anchor = max(int(payload.get('completed_cycles_since_anchor') or 0), 0)
        except (TypeError, ValueError):
            completed_cycles_since_anchor = 0
        if cycle_anchor_at and cycle_started_at and cycle_started_at == cycle_anchor_at and completed_cycles_since_anchor <= 0:
            continue
        group_label = str(
            ((cycle_row.get('monitor_target') or {}).get('group_name'))
            or ((cycle_row.get('monitor_target') or {}).get('binding_group_name'))
            or ((cycle_row.get('monitor_target') or {}).get('registration_group'))
            or cycle_row.get('registration_group')
            or ''
        ).strip()
        dedupe_suffix = '|'.join(part for part in [group_label, cycle_started_at or cycle_ends_at] if part) or 'unknown'
        append_notification({
            'severity': 'info',
            'code': 'registration_cycle_noop',
            'summary': '注册群本轮无审批',
            'details': {
                'group_name': group_label or None,
                'pending_count': 0,
                'cycle_started_at': cycle_started_at or None,
                'cycle_ends_at': cycle_ends_at or None,
                'reason_code': payload.get('reason_code'),
            },
            'notify_profile_name': str(((cycle_row.get('monitor_target') or {}).get('notify_profile_name')) or '').strip() or None,
            'notify_robot_name': str(((cycle_row.get('monitor_target') or {}).get('notify_robot_name')) or '').strip() or None,
            'dedupe_key': f'registration_cycle_noop:{dedupe_suffix}',
        })

    startup = cycle.get('startup_initial_batch') or {}
    if startup.get('triggered') and startup.get('ok'):
        startup_summary = startup_attempts_summary(startup)
        verified_attempts = list(startup_summary.get('verified_attempts') or [])
        monitor_target = cycle.get('monitor_target') if isinstance(cycle.get('monitor_target'), dict) else {}
        group_name = str(
            monitor_target.get('group_name')
            or monitor_target.get('binding_group_name')
            or monitor_target.get('registration_group')
            or cycle.get('registration_group')
            or ''
        ).strip()
        session_id = str(startup.get('session_id') or '').strip()
        for idx, attempt in enumerate(verified_attempts, start=1):
            formal_run = attempt.get('formal_run') if isinstance(attempt.get('formal_run'), dict) else {}
            formal_result = attempt.get('formal_result') if isinstance(attempt.get('formal_result'), dict) else {}
            if not formal_run or formal_result.get('verified') is not True or formal_result.get('crm_recorded') is not True:
                continue
            approval_run_id = str(attempt.get('approval_run_id') or formal_run.get('approval_run_id') or '').strip()
            code = 'startup_initial_batch_succeeded' if idx == 1 else 'formal_approval_succeeded'
            summary = '启动首批审批成功' if idx == 1 else '注册群审批成功'
            pending_after = _preferred_success_pending_after(
                startup,
                formal_result,
                is_last_attempt=idx == len(verified_attempts),
            )
            append_notification({
                'severity': 'info',
                'code': code,
                'summary': summary,
                'details': {
                    'approval_run_id': approval_run_id or None,
                    'approval_run_ids': [approval_run_id] if approval_run_id else [],
                    'session_id': session_id or None,
                    'group_name': group_name or None,
                    'approved_count': attempt.get('approved_count') or formal_result.get('approved_count', startup.get('pending_count')),
                    'pending_after': pending_after,
                    'member_count_after': formal_result.get('member_count_after'),
                    'result_code': formal_result.get('result_code'),
                },
                'notify_profile_name': str(monitor_target.get('notify_profile_name') or '').strip() or None,
                'notify_robot_name': str(monitor_target.get('notify_robot_name') or '').strip() or None,
                'dedupe_key': f'{code}:{approval_run_id or session_id or idx}',
            })

    for cycle_row in registration_cycles:
        if not isinstance(cycle_row, dict):
            continue
        monitor_target = cycle_row.get('monitor_target') if isinstance(cycle_row.get('monitor_target'), dict) else {}
        group_name = str(
            monitor_target.get('group_name')
            or monitor_target.get('binding_group_name')
            or monitor_target.get('registration_group')
            or cycle_row.get('registration_group')
            or ''
        ).strip()

        row_formal = cycle_row.get('formal_approval') or {}
        if row_formal.get('triggered'):
            append_duplicate_group_request_notification(row_formal, monitor_target, group_name)
        if row_formal.get('triggered') and row_formal.get('ok'):
            success_attempts = formal_approval_success_attempts(row_formal)
            if success_attempts:
                fingerprint = str(row_formal.get('fingerprint') or '').strip()
                for idx, attempt in enumerate(success_attempts, start=1):
                    formal_run = attempt.get('formal_run') if isinstance(attempt.get('formal_run'), dict) else {}
                    formal_result = attempt.get('formal_result') if isinstance(attempt.get('formal_result'), dict) else {}
                    approval_run_id = str(attempt.get('approval_run_id') or formal_run.get('approval_run_id') or '').strip()
                    pending_after = _preferred_success_pending_after(
                        row_formal,
                        formal_result,
                        is_last_attempt=idx == len(success_attempts),
                    )
                    dedupe_suffix = approval_run_id or fingerprint or f'unknown-{idx}'
                    append_notification({
                        'severity': 'info',
                        'code': 'formal_approval_succeeded',
                        'summary': '注册群审批成功',
                        'details': {
                            'approval_run_id': approval_run_id or None,
                            'approval_run_ids': [approval_run_id] if approval_run_id else [],
                            'fingerprint': fingerprint or None,
                            'group_name': group_name or None,
                            'approved_count': attempt.get('approved_count') or formal_result.get('approved_count', row_formal.get('release_count')),
                            'pending_after': pending_after,
                            'member_count_after': formal_result.get('member_count_after'),
                            'result_code': formal_result.get('result_code'),
                            'reason_code': row_formal.get('reason_code'),
                            'drain_rounds': row_formal.get('drain_rounds'),
                        },
                        'notify_profile_name': str(monitor_target.get('notify_profile_name') or '').strip() or None,
                        'notify_robot_name': str(monitor_target.get('notify_robot_name') or '').strip() or None,
                        'dedupe_key': f'formal_approval_succeeded:{dedupe_suffix}',
                    })

        row_startup = cycle_row.get('startup_initial_batch') or {}
        if row_startup.get('triggered') and row_startup.get('ok'):
            startup_summary = startup_attempts_summary(row_startup)
            verified_attempts = list(startup_summary.get('verified_attempts') or [])
            session_id = str(row_startup.get('session_id') or '').strip()
            for idx, attempt in enumerate(verified_attempts, start=1):
                formal_run = attempt.get('formal_run') if isinstance(attempt.get('formal_run'), dict) else {}
                formal_result = attempt.get('formal_result') if isinstance(attempt.get('formal_result'), dict) else {}
                if not formal_run or formal_result.get('verified') is not True or formal_result.get('crm_recorded') is not True:
                    continue
                approval_run_id = str(attempt.get('approval_run_id') or formal_run.get('approval_run_id') or '').strip()
                code = 'startup_initial_batch_succeeded' if idx == 1 else 'formal_approval_succeeded'
                summary = '启动首批审批成功' if idx == 1 else '注册群审批成功'
                pending_after = _preferred_success_pending_after(
                    row_startup,
                    formal_result,
                    is_last_attempt=idx == len(verified_attempts),
                )
                append_notification({
                    'severity': 'info',
                    'code': code,
                    'summary': summary,
                    'details': {
                        'approval_run_id': approval_run_id or None,
                        'approval_run_ids': [approval_run_id] if approval_run_id else [],
                        'session_id': session_id or None,
                        'group_name': group_name or None,
                        'approved_count': attempt.get('approved_count') or formal_result.get('approved_count', row_startup.get('pending_count')),
                        'pending_after': pending_after,
                        'member_count_after': formal_result.get('member_count_after'),
                        'result_code': formal_result.get('result_code'),
                    },
                    'notify_profile_name': str(monitor_target.get('notify_profile_name') or '').strip() or None,
                    'notify_robot_name': str(monitor_target.get('notify_robot_name') or '').strip() or None,
                    'dedupe_key': f'{code}:{approval_run_id or session_id or idx}',
                })

    official_dispatch = cycle.get('official_group_dispatch') or {}
    if official_dispatch.get('triggered') and official_dispatch.get('ok'):
        ready_groups = list(official_dispatch.get('ready_groups') or []) if isinstance(official_dispatch.get('ready_groups'), list) else []
        ready_group_by_target = {
            str(item.get('target_group') or '').strip(): item
            for item in ready_groups
            if isinstance(item, dict) and str(item.get('target_group') or '').strip()
        }
        dispatch_result = official_dispatch.get('result') or {}
        result_rows = list(dispatch_result.get('results') or []) if isinstance(dispatch_result.get('results'), list) else []
        grouped_successes: Dict[str, Dict[str, Any]] = {}
        for item in result_rows:
            if not isinstance(item, dict) or not item.get('executed'):
                continue
            executor_result = item.get('executor_result') if isinstance(item.get('executor_result'), dict) else {}
            if str(executor_result.get('status') or '').strip().lower() != 'success':
                continue
            if executor_result.get('verified') is False:
                continue
            raw_result = executor_result.get('raw_result') if isinstance(executor_result.get('raw_result'), dict) else {}
            target_group = str(item.get('target_group') or raw_result.get('target_group') or '').strip()
            ready_group = ready_group_by_target.get(target_group) or {}
            group_name = str(raw_result.get('group_name') or ready_group.get('group_name') or '').strip()
            group_key = target_group or group_name or 'unknown'
            bucket = grouped_successes.setdefault(group_key, {
                'target_group': target_group or None,
                'group_name': group_name or None,
                'account_key': str(ready_group.get('account_key') or '').strip() or None,
                'approved_count': 0,
                'pending_after': raw_result.get('pending_after'),
                'member_count_after': raw_result.get('member_count_after'),
                'notify_profile_name': str(ready_group.get('notify_profile_name') or '').strip() or None,
                'notify_robot_name': str(ready_group.get('notify_robot_name') or '').strip() or None,
                'approval_run_ids': [],
            })
            try:
                bucket['approved_count'] += max(int(executor_result.get('approved_count', 1) or 1), 0)
            except Exception:
                bucket['approved_count'] += 1
            approval_run_id = str(raw_result.get('approval_run_id') or '').strip()
            if approval_run_id:
                bucket['approval_run_ids'].append(approval_run_id)
            if raw_result.get('pending_after') is not None:
                bucket['pending_after'] = raw_result.get('pending_after')
            if raw_result.get('member_count_after') is not None:
                bucket['member_count_after'] = raw_result.get('member_count_after')
        for bucket in grouped_successes.values():
            dedupe_suffix = '|'.join(bucket['approval_run_ids']) or str(bucket.get('target_group') or bucket.get('group_name') or 'unknown')
            append_notification({
                'severity': 'info',
                'code': 'official_group_approval_succeeded',
                'summary': '官方群审批成功',
                'details': {
                    'approval_run_ids': bucket['approval_run_ids'],
                    'target_group': bucket.get('target_group'),
                    'group_name': bucket.get('group_name'),
                    'account_key': bucket.get('account_key'),
                    'approved_count': bucket.get('approved_count', 0),
                    'pending_after': bucket.get('pending_after'),
                    'member_count_after': bucket.get('member_count_after'),
                },
                'notify_profile_name': bucket.get('notify_profile_name'),
                'notify_robot_name': bucket.get('notify_robot_name'),
                'dedupe_key': f'official_group_approval_succeeded:{dedupe_suffix}',
            })
        for item in result_rows:
            if not isinstance(item, dict) or item.get('executed'):
                continue
            if str(item.get('next_action') or '').strip() != 'manual_review_official_group_approval':
                continue
            target_group = str(item.get('target_group') or '').strip()
            ready_group = ready_group_by_target.get(target_group) or {}
            group_name = str(item.get('group_name') or ready_group.get('group_name') or target_group).strip()
            lead_id = str(item.get('lead_id') or '').strip() or None
            reason_code = str(item.get('reason_code') or '').strip() or 'manual_review_official_group_approval'
            requester = item.get('requester') if isinstance(item.get('requester'), dict) else {}
            mobile = str(
                item.get('mobile')
                or requester.get('phoneNormalized')
                or requester.get('phone_normalized')
                or requester.get('phoneRaw')
                or requester.get('phone_raw')
                or requester.get('debugLidPhoneRaw')
                or ''
            ).strip() or None
            requester_id = str(
                requester.get('requesterId')
                or requester.get('requester_id')
                or item.get('target_requester_id')
                or ''
            ).strip() or None
            success_bucket = grouped_successes.get(target_group or group_name or 'unknown') or {}
            remaining_pending_count = item.get('remaining_pending_count')
            if remaining_pending_count is None:
                remaining_pending_count = success_bucket.get('pending_after')
            if remaining_pending_count is None:
                remaining_pending_count = ready_group.get('pending_count')
            if remaining_pending_count is None and isinstance(ready_group.get('requesters'), list):
                remaining_pending_count = len(ready_group.get('requesters') or [])
            try:
                remaining_pending_count = max(int(remaining_pending_count), 0)
            except (TypeError, ValueError):
                remaining_pending_count = None
            dedupe_target = target_group or group_name or 'unknown'
            dedupe_identity = lead_id or requester_id or mobile or 'unknown'
            dedupe_remaining = remaining_pending_count if remaining_pending_count is not None else 'na'
            reason_text = (
                'CRM无记录，请人工复核' if reason_code == 'crm_customer_not_found'
                else '申请账号未匹配到当前有效收口记录，请人工复核' if reason_code == 'official_group_requester_unmatched'
                else '申请记录命中异常标记，请人工复核' if reason_code == 'abnormal_flagged'
                else 'CRM服务未就绪，请人工复核' if reason_code == 'crm_adapter_not_configured'
                else str(item.get('reason_detail') or '').strip() or '官方群审批需人工复核'
            )
            append_notification({
                'severity': 'warning',
                'code': 'official_group_manual_review_required',
                'summary': '官方群审批需人工复核',
                'details': {
                    'target_group': target_group or None,
                    'group_name': group_name or None,
                    'lead_id': lead_id,
                    'mobile': mobile,
                    'requester_id': requester_id,
                    'reason_code': reason_code,
                    'reason_detail': str(item.get('reason_detail') or '').strip() or None,
                    'next_action': str(item.get('next_action') or '').strip() or None,
                    'remaining_pending_count': remaining_pending_count,
                },
                'reason_text': reason_text,
                'notify_profile_name': str(item.get('notify_profile_name') or ready_group.get('notify_profile_name') or '').strip() or None,
                'notify_robot_name': str(item.get('notify_robot_name') or ready_group.get('notify_robot_name') or '').strip() or None,
                'dedupe_key': f'official_group_manual_review_required:{dedupe_target}:{dedupe_identity}:{reason_code}:{dedupe_remaining}',
            })

    return notifications


def _format_alert_time(checked_at: str) -> str:
    raw = str(checked_at or '').strip() or utc_now_iso()
    try:
        normalized = raw.replace('Z', '+00:00')
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone(timedelta(hours=8)))
        return dt.strftime('%Y-%m-%d %H:%M:%S UTC+8')
    except Exception:
        return raw



def _compact_reason_text(incident: Dict[str, Any], cycle: Dict[str, Any]) -> str:
    code = str(incident.get('code') or '').strip()
    details = incident.get('details') or {}
    action = cycle.get('formal_approval') or {}
    formal_run = formal_run_payload(action.get('result') or {}) if isinstance(action, dict) else {}
    formal_result = formal_run_result(formal_run)

    if code == 'formal_approval_failed':
        returncode = action.get('returncode') if action.get('returncode') is not None else details.get('returncode')
        verified = formal_result.get('verified')
        if verified is None:
            verified = details.get('verified')
        crm_recorded = formal_result.get('crm_recorded')
        if crm_recorded is None:
            crm_recorded = details.get('crm_recorded')

        result_code = str(details.get('result_code') or formal_result.get('result_code') or '').strip()
        if result_code == 'requester_fingerprint_changed_before_approval':
            return '审批队列指纹不一致，系统已中止本轮审批以避免错批'

        if returncode not in (None, 0):
            return '审批脚本执行失败'
        if verified is True and crm_recorded is True:
            return '审批已执行并成功闭环，本条告警应视为误报'
        if verified is True and crm_recorded is False:
            return '审批成功，但 CRM 写入失败'
        if verified is False and crm_recorded is True:
            return 'CRM 已写入，但审批结果未核验成功'
        if verified is False and crm_recorded is False:
            return '审批与 CRM 写入结果均未确认成功'
        if verified is False:
            return '审批结果未核验成功'
        if crm_recorded is False:
            return '审批已提交，但 CRM 未确认写入'
        return '审批已执行，但未形成成功闭环'

    if code == 'release_evaluation_failed':
        error = str(details.get('error') or '').strip()
        return error or '批次放行评估接口调用失败'

    if code == 'registration_zero_pending_unverified':
        return '零待审批仅来自同源 runtime，已阻止发送无审批通知'

    if code == 'backend_unhealthy':
        error = str(details.get('error') or '').strip()
        return error or '后端健康检查失败'

    if code == 'worker_state_failed':
        recovery = details.get('recovery') if isinstance(details.get('recovery'), dict) else {}
        error = str(details.get('error') or '').strip()
        recovery_reason = str(recovery.get('reason') or '').strip()
        login_status = str(recovery.get('login_check_status') or '').strip()
        if error == 'whatsapp_account_waiting_for_scan' or recovery_reason == 'whatsapp_account_waiting_for_scan' or login_status == 'waiting_for_scan':
            return 'WhatsApp账号待登录，无法探测注册群'
        if error == 'whatsapp_qr_initializing' or recovery_reason == 'whatsapp_qr_initializing':
            return 'WhatsApp账号登录会话正在初始化，请稍后扫码后再探测注册群'
        if recovery.get('attempted'):
            return '自动重连失败，已重试后仍不可用'
        if error == 'worker_base_url_missing_for_selected_binding':
            return 'WhatsApp账号运行态未就绪，暂时无法探测该注册群'
        return error or 'worker 群状态探测失败'

    if code == 'startup_initial_batch_failed':
        retries_exhausted = details.get('retries_exhausted')
        last_verified = details.get('last_verified')
        last_crm_recorded = details.get('last_crm_recorded')
        if last_verified is True and last_crm_recorded is False:
            return '启动首批审批成功，但 CRM 写入失败'
        if last_verified is False and last_crm_recorded is True:
            return '启动首批 CRM 已写入，但审批结果未核验成功'
        if retries_exhausted:
            return '启动首批审批失败，自动重试已结束并转入常规监控'
        return '启动首批审批失败'

    if code == 'formal_approval_succeeded':
        approved_count = formal_result.get('approved_count')
        if approved_count is None:
            approved_count = details.get('approved_count')

        fallback = '已审批通过'
        try:
            approved_value = int(approved_count)
        except (TypeError, ValueError):
            approved_value = None
        if approved_value is not None and approved_value > 0:
            fallback = f'已审批通过 {approved_value} 人'
        return render_alert_reason_template(code, approved_count=approved_count, fallback=fallback)

    if code == 'manual_approval_succeeded':
        approved_count = details.get('approved_count')

        fallback = '已人工审批通过'
        try:
            approved_value = int(approved_count)
        except (TypeError, ValueError):
            approved_value = None
        if approved_value is not None and approved_value > 0:
            fallback = f'已人工审批通过 {approved_value} 人'
        return render_alert_reason_template(code, approved_count=approved_count, fallback=fallback)

    if code == 'startup_initial_batch_succeeded':
        approved_count = details.get('approved_count')

        fallback = '启动首批审批已通过'
        try:
            approved_value = int(approved_count)
        except (TypeError, ValueError):
            approved_value = None
        if approved_value is not None and approved_value > 0:
            fallback = f'启动首批审批已通过 {approved_value} 人'
        return render_alert_reason_template(code, approved_count=approved_count, fallback=fallback)

    if code == 'official_group_approval_succeeded':
        approved_count = details.get('approved_count')

        fallback = '已审批通过'
        try:
            approved_value = int(approved_count)
        except (TypeError, ValueError):
            approved_value = None
        if approved_value is not None and approved_value > 0:
            fallback = f'已审批通过 {approved_value} 人'
        return render_alert_reason_template(code, approved_count=approved_count, fallback=fallback)

    if code in {'registration_cycle_noop', 'official_group_cycle_noop'}:
        fallback = '审批时间已到，未发生实际审批'
        return render_alert_reason_template(code, fallback=fallback)

    if code == 'official_group_manual_review_required':
        reason_code = str(details.get('reason_code') or '').strip()
        if reason_code == 'crm_customer_not_found':
            return 'CRM无记录，请人工复核'
        if reason_code == 'official_group_requester_unmatched':
            return '申请账号未匹配到当前有效收口记录，请人工复核'
        if reason_code == 'abnormal_flagged':
            return '申请记录命中异常标记，请人工复核'
        if reason_code == 'crm_adapter_not_configured':
            return 'CRM服务未就绪，请人工复核'
        reason_detail = str(details.get('reason_detail') or '').strip()
        return reason_detail or '官方群审批需人工复核'

    snippet = json.dumps(details, ensure_ascii=False) if details else ''
    if len(snippet) > 120:
        snippet = snippet[:120] + '...'
    return snippet or '未知原因'



def _compact_count_line(incident: Dict[str, Any], cycle: Dict[str, Any]) -> Optional[str]:
    code = str(incident.get('code') or '').strip()
    details = incident.get('details') or {}
    action = cycle.get('formal_approval') or {}
    startup = cycle.get('startup_initial_batch') or {}
    release = cycle.get('release_evaluation') or {}

    label = '人数'
    candidates: List[Any] = []
    if code == 'formal_approval_failed':
        candidates = [action.get('release_count'), details.get('release_count'), action.get('pending_count'), details.get('pending_count')]
        label = '批次人数'
    elif code == 'startup_initial_batch_failed':
        candidates = [startup.get('pending_count'), details.get('pending_count')]
        label = '待审批人数'
    elif code == 'release_evaluation_failed':
        payload = release.get('payload') if isinstance(release.get('payload'), dict) else {}
        candidates = [payload.get('release_count'), release.get('release_count'), details.get('release_count')]
        label = '待放行人数'
    elif code == 'formal_approval_succeeded':
        formal_result = formal_run_result(action.get('result') or {})
        candidates = [formal_result.get('approved_count'), details.get('approved_count'), action.get('release_count')]
        label = '通过人数'
    elif code == 'manual_approval_succeeded':
        candidates = [details.get('approved_count')]
        label = '通过人数'
    elif code == 'startup_initial_batch_succeeded':
        candidates = [details.get('approved_count'), startup.get('pending_count')]
        label = '通过人数'
    elif code == 'official_group_approval_succeeded':
        candidates = [details.get('approved_count')]
        label = '本次通过人数'

    for value in candidates:
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            return f'{label}: {count}'
    return None



def format_lark_alert(service_name: str, incident: Dict[str, Any], cycle: Dict[str, Any]) -> str:
    checked_at = _format_alert_time(str(cycle.get('checked_at') or utc_now_iso()))
    severity_key = str(incident.get('severity') or 'info').strip().lower()
    summary = str(incident.get('summary') or '').strip()
    code = str(incident.get('code') or 'incident').strip()
    if code == 'official_group_manual_review_required':
        details = incident.get('details') or {}
        official_group_name = str(details.get('group_name') or details.get('target_group') or '').strip()
        mobile = str(details.get('mobile') or '').strip()
        remaining_pending_count = details.get('remaining_pending_count')
        reason = str(incident.get('reason_text') or '').strip() or _compact_reason_text(incident, cycle)
        lines = ['⚠️🙋🏻‍♀️⚠️官方群审批需人工复核']
        lines.append(f'时间: {checked_at}')
        if official_group_name:
            lines.append(f'官方群: {official_group_name}')
        if mobile:
            lines.append(f'账号: {mobile}')
        try:
            remaining_pending_value = max(int(remaining_pending_count), 0)
        except (TypeError, ValueError):
            remaining_pending_value = None
        if remaining_pending_value is not None:
            lines.append(f'待放行人数: {remaining_pending_value}')
        if reason:
            lines.append(f'原因: {reason}')
        return '\n'.join(lines)
    if code == 'registration_duplicate_group_request_skipped':
        details = incident.get('details') if isinstance(incident.get('details'), dict) else {}
        requested_group = str(
            details.get('group_name')
            or details.get('target_group_label')
            or ((cycle.get('monitor_target') or {}).get('group_name') if isinstance(cycle.get('monitor_target'), dict) else '')
            or cycle.get('registration_group')
            or ''
        ).strip()
        active_group = str(details.get('active_registration_group') or '').strip()
        reason = str(details.get('result_reason') or '').strip()
        lines = [
            '⚠️ 生产守护提醒｜注册群重复申请已拦截',
            f'时间: {checked_at}',
        ]
        if requested_group:
            lines.append(f'注册群: {requested_group}')
        if active_group:
            lines.append(f'已归属注册群: {active_group}')
        lines.append('结果: 已跳过自动审批，不会放入第二个注册群')
        if reason:
            lines.append(f'原因: {reason}')
        return '\n'.join(line for line in lines if line)

    if code == 'worker_probe_recovered':
        details = incident.get('details') if isinstance(incident.get('details'), dict) else {}
        registration_group_label = str(
            details.get('group_name')
            or details.get('target_group_label')
            or ((cycle.get('monitor_target') or {}).get('group_name') if isinstance(cycle.get('monitor_target'), dict) else '')
            or ((cycle.get('monitor_target') or {}).get('binding_group_name') if isinstance(cycle.get('monitor_target'), dict) else '')
            or cycle.get('registration_group')
            or ''
        ).strip()
        original_failed_at = _format_alert_time(str(details.get('original_failed_at') or details.get('failed_at') or ''))
        streak_count = details.get('streak_count')
        lines = [
            '✅ 生产守护恢复｜探针已恢复',
            f'时间: {checked_at}',
        ]
        if registration_group_label:
            lines.append(f'注册群: {registration_group_label}')
        if original_failed_at:
            lines.append(f'原告警: {original_failed_at}')
        try:
            streak_value = int(streak_count)
        except (TypeError, ValueError):
            streak_value = None
        if streak_value is not None and streak_value > 0:
            lines.append(f'异常轮次: {streak_value}')
        lines.append('结果: 探针已自动恢复，群状态读取恢复正常')
        reason = str(incident.get('reason_text') or '').strip()
        if reason:
            lines.append(f'原因: {reason}')
        return '\n'.join(line for line in lines if line)

    if code == 'formal_approval_recovered':
        details = incident.get('details') if isinstance(incident.get('details'), dict) else {}
        registration_group_label = str(
            details.get('group_name')
            or details.get('target_group_label')
            or ((cycle.get('monitor_target') or {}).get('group_name') if isinstance(cycle.get('monitor_target'), dict) else '')
            or ((cycle.get('monitor_target') or {}).get('binding_group_name') if isinstance(cycle.get('monitor_target'), dict) else '')
            or cycle.get('registration_group')
            or ''
        ).strip()
        approved_count = details.get('approved_count')
        display_batch_id = str(details.get('approval_batch_display_id') or '').strip()
        original_failed_at = _format_alert_time(str(details.get('original_failed_at') or details.get('failed_at') or ''))
        reason = str(incident.get('reason_text') or '').strip() or '首次审批调用超时，client reset 后重试成功'
        lines = [
            '✅ 生产守护恢复｜正式审批已闭环',
            f'时间: {checked_at}',
        ]
        if registration_group_label:
            lines.append(f'注册群: {registration_group_label}')
        if original_failed_at:
            lines.append(f'原告警: {original_failed_at}')
        try:
            approved_value = int(approved_count)
        except (TypeError, ValueError):
            approved_value = None
        if approved_value is not None and approved_value >= 0:
            lines.append(f'批次人数: {approved_value}')
        lines.append('结果: 自动重试成功，审批已完成，CRM 已写入')
        if reason:
            lines.append(f'原因: {reason}')
        if display_batch_id:
            lines.append(f'批次ID: {display_batch_id}')
        return '\n'.join(line for line in lines if line)

    templates = load_alert_templates()
    header_map = templates.get('headers') if isinstance(templates.get('headers'), dict) else {}
    header_entry = header_map.get(severity_key) if isinstance(header_map.get(severity_key), dict) else {}
    default_header_entry = header_map.get('default') if isinstance(header_map.get('default'), dict) else {}
    icon = str(header_entry.get('icon') or default_header_entry.get('icon') or 'ℹ️').strip()
    label = str(header_entry.get('label') or default_header_entry.get('label') or '生产守护通知').strip()
    title = summary or code or service_name
    lines = [
        f'{icon} {label}｜{title}',
        f'时间: {checked_at}',
    ]
    monitor_target = cycle.get('monitor_target') if isinstance(cycle.get('monitor_target'), dict) else {}
    if code in {'official_group_approval_succeeded', 'official_group_manual_review_required'}:
        official_group_name = str((incident.get('details') or {}).get('group_name') or (incident.get('details') or {}).get('target_group') or '').strip()
        if official_group_name:
            lines.append(f'官方群: {official_group_name}')
    else:
        registration_group_label = str(
            (incident.get('details') or {}).get('group_name')
            or monitor_target.get('group_name')
            or monitor_target.get('binding_group_name')
            or monitor_target.get('registration_group')
            or cycle.get('registration_group')
            or ''
        ).strip()
        if registration_group_label:
            lines.append(f'注册群: {registration_group_label}')
    if code == 'official_group_manual_review_required':
        mobile = str((incident.get('details') or {}).get('mobile') or '').strip()
        if mobile:
            lines.append(f'账号: {mobile}')
    details = incident.get('details') if isinstance(incident.get('details'), dict) else {}
    if code in {'formal_approval_succeeded', 'manual_approval_succeeded', 'startup_initial_batch_succeeded', 'registration_cycle_noop', 'official_group_cycle_noop'}:
        approval_type = '常规轮次' if code in {'formal_approval_succeeded', 'registration_cycle_noop', 'official_group_cycle_noop'} else ('人工审批' if code == 'manual_approval_succeeded' else '启动首批')
        lines.append(f'审批类型: {approval_type}')
        approved_count = details.get('approved_count')
        try:
            approved_value = int(approved_count)
        except (TypeError, ValueError):
            approved_value = None
        if approved_value is not None and approved_value >= 0:
            lines.append(f'本次通过人数: {approved_value}')
        pending_after = details.get('pending_after')
        try:
            pending_after_value = int(pending_after)
        except (TypeError, ValueError):
            pending_after_value = None
        if pending_after_value is not None and pending_after_value >= 0:
            lines.append(f'剩余待审批人数: {pending_after_value}')
    else:
        count_line = _compact_count_line(incident, cycle)
        if count_line:
            lines.append(count_line)
        if code == 'official_group_approval_succeeded':
            pending_after = details.get('pending_after')
            try:
                pending_after_value = int(pending_after)
            except (TypeError, ValueError):
                pending_after_value = None
            if pending_after_value is not None and pending_after_value >= 0:
                lines.append(f'剩余待审批人数: {pending_after_value}')
    reason = str(incident.get('reason_text') or '').strip() or _compact_reason_text(incident, cycle)
    if reason:
        lines.append(f'原因: {reason}')
    return '\n'.join(line for line in lines if line)


class FeishuNotifier:
    def __init__(self, *, app_id: str, app_secret: str, chat_id: str, domain: str = 'lark') -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.chat_id = chat_id
        self.base_url = 'https://open.larksuite.com' if domain == 'lark' else 'https://open.feishu.cn'
        self._tenant_access_token: Optional[str] = None

    def _get_tenant_access_token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token
        request = urllib.request.Request(
            f'{self.base_url}/open-apis/auth/v3/tenant_access_token/internal',
            data=json.dumps({'app_id': self.app_id, 'app_secret': self.app_secret}).encode('utf-8'),
            headers={'Content-Type': 'application/json; charset=utf-8'},
            method='POST',
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode('utf-8'))
        if body.get('code') != 0:
            raise RuntimeError(f'tenant_access_token failed: {body}')
        self._tenant_access_token = str(body['tenant_access_token'])
        return self._tenant_access_token

    def send_text(self, text: str) -> Dict[str, Any]:
        token = self._get_tenant_access_token()
        payload = {
            'receive_id': self.chat_id,
            'msg_type': 'text',
            'content': json.dumps({'text': str(text or '')}, ensure_ascii=False),
        }
        request = urllib.request.Request(
            f'{self.base_url}/open-apis/im/v1/messages?receive_id_type=chat_id',
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json; charset=utf-8',
            },
            method='POST',
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode('utf-8'))
        if body.get('code') != 0:
            raise RuntimeError(f'im_message failed: {body}')
        return body


def run_formal_approval_command(command: List[str], *, timeout: float = 180.0) -> Dict[str, Any]:
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    payload: Dict[str, Any] = {
        'command': command,
        'returncode': completed.returncode,
        'stdout': completed.stdout,
        'stderr': completed.stderr,
    }
    stdout_text = str(completed.stdout or '').strip()
    if stdout_text:
        try:
            payload['result'] = json.loads(stdout_text)
        except Exception:
            payload['result_parse_error'] = 'stdout_not_json'
    return payload


def env_default(name: str, fallback: str = '') -> str:
    return str(os.getenv(name) or fallback)
