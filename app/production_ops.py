from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
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
            'summary': 'intake backend health check failed',
            'details': backend,
            'dedupe_key': 'backend_unhealthy',
        })
    worker = cycle.get('worker_state') or {}
    if worker.get('ok') is False:
        incidents.append({
            'severity': 'critical',
            'code': 'worker_state_failed',
            'summary': 'worker group-state probe failed',
            'details': worker,
            'dedupe_key': 'worker_state_failed',
        })
    release = cycle.get('release_evaluation') or {}
    if release.get('ok') is False:
        incidents.append({
            'severity': 'critical',
            'code': 'release_evaluation_failed',
            'summary': 'release evaluation failed',
            'details': release,
            'dedupe_key': 'release_evaluation_failed',
        })
    action = cycle.get('formal_approval') or {}
    if action.get('triggered') and not action.get('ok'):
        approval_run_id = str(((action.get('result') or {}).get('formal_run') or {}).get('approval_run_id') or '')
        dedupe_suffix = approval_run_id or str(action.get('fingerprint') or 'unknown')
        incidents.append({
            'severity': 'critical',
            'code': 'formal_approval_failed',
            'summary': 'formal approval run finished without verified+crm_recorded success',
            'details': action,
            'dedupe_key': f'formal_approval_failed:{dedupe_suffix}',
        })
    cycle_error = str(cycle.get('cycle_error') or '').strip()
    if cycle_error:
        incidents.append({
            'severity': 'critical',
            'code': 'daemon_cycle_error',
            'summary': cycle_error,
            'details': {'error': cycle_error},
            'dedupe_key': f'daemon_cycle_error:{cycle_error}',
        })
    return incidents


def format_lark_alert(service_name: str, incident: Dict[str, Any], cycle: Dict[str, Any]) -> str:
    checked_at = str(cycle.get('checked_at') or utc_now_iso())
    lines = [
        f'[{service_name}] {incident.get("severity", "info").upper()} {incident.get("code", "incident")}',
        str(incident.get('summary') or '').strip(),
        f'checked_at: {checked_at}',
    ]
    registration_group = str(cycle.get('registration_group') or '').strip()
    if registration_group:
        lines.append(f'registration_group: {registration_group}')
    action = cycle.get('formal_approval') or {}
    if action.get('triggered'):
        lines.append(f'fingerprint: {action.get("fingerprint") or ""}')
    details = incident.get('details')
    if details:
        snippet = json.dumps(details, ensure_ascii=False)
        if len(snippet) > 1200:
            snippet = snippet[:1200] + '...'
        lines.append(f'details: {snippet}')
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
