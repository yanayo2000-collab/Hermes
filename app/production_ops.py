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



def requester_fingerprint(group_state: Dict[str, Any]) -> str:
    requesters = group_state.get('requesters') or []
    parts: List[str] = []
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
    if worker.get('ok') is False:
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
        confirmed_failure = returncode not in (None, 0) or verified is False or crm_recorded is False
        if confirmed_failure:
            incidents.append({
                'severity': 'critical',
                'code': 'formal_approval_failed',
                'summary': '正式审批未闭环',
                'details': {
                    'fingerprint': fingerprint,
                    'pending_count': action.get('pending_count'),
                    'release_count': action.get('release_count'),
                    'reason_code': action.get('reason_code'),
                    'returncode': returncode,
                    'approval_run_id': approval_run_id,
                    'verified': verified,
                    'crm_recorded': crm_recorded,
                    'result_code': formal_result.get('result_code'),
                },
                'dedupe_key': f'formal_approval_failed:{dedupe_suffix}',
            })
    startup_batch = cycle.get('startup_initial_batch') or {}
    if startup_batch.get('triggered') and not startup_batch.get('ok'):
        session_id = str(startup_batch.get('session_id') or '').strip()
        formal_run = formal_run_payload(startup_batch.get('result') or {})
        approval_run_id = str(formal_run.get('approval_run_id') or '').strip()
        dedupe_suffix = session_id or 'unknown'
        attempt_results = startup_batch.get('attempt_results') or []
        last_attempt = attempt_results[-1] if isinstance(attempt_results, list) and attempt_results else {}
        last_result = (last_attempt.get('result') or {}) if isinstance(last_attempt, dict) else {}
        last_formal_run = formal_run_payload(last_result)
        last_formal_result = formal_run_result(last_formal_run)
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
                'last_returncode': last_attempt.get('returncode') if isinstance(last_attempt, dict) else None,
                'last_approval_run_id': str(last_formal_run.get('approval_run_id') or '').strip() or approval_run_id or None,
                'last_verified': last_formal_result.get('verified'),
                'last_crm_recorded': last_formal_result.get('crm_recorded'),
                'last_result_code': last_formal_result.get('result_code'),
            },
            'dedupe_key': f'startup_initial_batch_failed:{dedupe_suffix}',
        })
    cycle_error = str(cycle.get('cycle_error') or '').strip()
    if cycle_error:
        incidents.append({
            'severity': 'critical',
            'code': 'daemon_cycle_error',
            'summary': '守护任务异常',
            'details': {'error': cycle_error},
            'dedupe_key': f'daemon_cycle_error:{cycle_error}',
        })
    return incidents


def build_success_notifications(cycle: Dict[str, Any]) -> List[Dict[str, Any]]:
    notifications: List[Dict[str, Any]] = []

    action = cycle.get('formal_approval') or {}
    if action.get('triggered') and action.get('ok'):
        formal_run = formal_run_payload(action.get('result') or {})
        formal_result = formal_run_result(formal_run)
        if formal_run and formal_result.get('verified') is True and formal_result.get('crm_recorded') is True:
            approval_run_id = str(formal_run.get('approval_run_id') or '').strip()
            fingerprint = str(action.get('fingerprint') or '').strip()
            dedupe_suffix = approval_run_id or fingerprint or 'unknown'
            notifications.append({
                'severity': 'info',
                'code': 'formal_approval_succeeded',
                'summary': '注册群审批成功',
                'details': {
                    'approval_run_id': approval_run_id or None,
                    'fingerprint': fingerprint or None,
                    'approved_count': formal_result.get('approved_count', action.get('release_count')),
                    'pending_after': formal_result.get('pending_after'),
                    'member_count_after': formal_result.get('member_count_after'),
                    'result_code': formal_result.get('result_code'),
                    'reason_code': action.get('reason_code'),
                },
                'dedupe_key': f'formal_approval_succeeded:{dedupe_suffix}',
            })

    startup = cycle.get('startup_initial_batch') or {}
    if startup.get('triggered') and startup.get('ok'):
        attempt_results = startup.get('attempt_results') or []
        last_attempt = attempt_results[-1] if isinstance(attempt_results, list) and attempt_results else {}
        startup_formal_run = formal_run_payload((last_attempt or {}).get('result') or startup.get('result') or {})
        startup_formal_result = formal_run_result(startup_formal_run)
        if startup_formal_run and startup_formal_result.get('verified') is True and startup_formal_result.get('crm_recorded') is True:
            approval_run_id = str(startup_formal_run.get('approval_run_id') or '').strip()
            session_id = str(startup.get('session_id') or '').strip()
            dedupe_suffix = approval_run_id or session_id or 'unknown'
            notifications.append({
                'severity': 'info',
                'code': 'startup_initial_batch_succeeded',
                'summary': '启动首批审批成功',
                'details': {
                    'approval_run_id': approval_run_id or None,
                    'session_id': session_id or None,
                    'approved_count': startup_formal_result.get('approved_count', startup.get('pending_count')),
                    'pending_after': startup_formal_result.get('pending_after'),
                    'member_count_after': startup_formal_result.get('member_count_after'),
                    'result_code': startup_formal_result.get('result_code'),
                },
                'dedupe_key': f'startup_initial_batch_succeeded:{dedupe_suffix}',
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

    if code == 'backend_unhealthy':
        error = str(details.get('error') or '').strip()
        return error or '后端健康检查失败'

    if code == 'worker_state_failed':
        error = str(details.get('error') or '').strip()
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
        pending_after = formal_result.get('pending_after')
        if pending_after is None:
            pending_after = details.get('pending_after')
        member_count_after = formal_result.get('member_count_after')
        if member_count_after is None:
            member_count_after = details.get('member_count_after')

        approved_text = '已审批通过'
        try:
            approved_value = int(approved_count)
        except (TypeError, ValueError):
            approved_value = None
        if approved_value is not None and approved_value > 0:
            approved_text = f'已审批通过 {approved_value} 人'

        suffix_parts: List[str] = []
        try:
            pending_after_value = int(pending_after)
            suffix_parts.append(f'当前待审批 {pending_after_value} 人')
        except (TypeError, ValueError):
            pass
        try:
            member_after_value = int(member_count_after)
            suffix_parts.append(f'群成员 {member_after_value} 人')
        except (TypeError, ValueError):
            pass
        if suffix_parts:
            return approved_text + '，' + '，'.join(suffix_parts)
        return approved_text

    if code == 'startup_initial_batch_succeeded':
        approved_count = details.get('approved_count')
        pending_after = details.get('pending_after')
        member_count_after = details.get('member_count_after')

        approved_text = '启动首批审批已通过'
        try:
            approved_value = int(approved_count)
        except (TypeError, ValueError):
            approved_value = None
        if approved_value is not None and approved_value > 0:
            approved_text = f'启动首批审批已通过 {approved_value} 人'

        suffix_parts: List[str] = []
        try:
            pending_after_value = int(pending_after)
            suffix_parts.append(f'当前待审批 {pending_after_value} 人')
        except (TypeError, ValueError):
            pass
        try:
            member_after_value = int(member_count_after)
            suffix_parts.append(f'群成员 {member_after_value} 人')
        except (TypeError, ValueError):
            pass
        if suffix_parts:
            return approved_text + '，' + '，'.join(suffix_parts)
        return approved_text

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
    elif code == 'startup_initial_batch_succeeded':
        candidates = [details.get('approved_count'), startup.get('pending_count')]
        label = '通过人数'

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
    severity = str(incident.get('severity') or 'info').upper()
    lines = [
        f'[{service_name}] {severity} {incident.get("code", "incident")}',
        str(incident.get('summary') or '').strip(),
        f'时间: {checked_at}',
    ]
    registration_group = str(cycle.get('registration_group') or '').strip()
    if registration_group:
        lines.append(f'注册群: {registration_group}')
    count_line = _compact_count_line(incident, cycle)
    if count_line:
        lines.append(count_line)
    reason = _compact_reason_text(incident, cycle)
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
