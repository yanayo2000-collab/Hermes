from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


LOGIN_STATE_LABELS = {
    'disabled': '账号已停用',
    'runtime_stopped': '运行服务未启动',
    'runtime_starting': '运行服务启动中',
    'runtime_recovering': 'WhatsApp登录态恢复中',
    'initializing': 'WhatsApp登录会话初始化中',
    'login_verifying': '连接已建立，正在验证登录稳定性',
    'waiting_for_scan_qr_ready': '二维码已生成，等待扫码',
    'waiting_for_scan_qr_pending': '二维码生成中，请稍后手动刷新',
    'qr_expired': '二维码已过期，请重新生成',
    'logged_in': '已登录',
    'login_failed': '登录失败，请手动重置登录',
    'runtime_unhealthy': '运行服务异常，请手动恢复',
    'account_restricted': '账号疑似受限',
    'session_mismatch': '登录会话账号不匹配',
    'unknown': '状态待确认',
}


def _bool(value: Any) -> bool:
    return bool(value is True or str(value).strip().lower() in {'1', 'true', 'yes', 'y'})


def _text(value: Any) -> str:
    return str(value or '').strip()


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    raw = _text(value)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_seconds(value: Any, *, now: Optional[datetime] = None) -> Optional[float]:
    dt = _parse_iso_datetime(value)
    if dt is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0.0, (current - dt).total_seconds())


def _has_qr_payload(session_state: Dict[str, Any]) -> bool:
    return bool(
        _bool(session_state.get('qr_available'))
        and (
            _text(session_state.get('qr_text'))
            or _text(session_state.get('qr_ascii'))
            or _text(session_state.get('qr_image_data_url'))
        )
    )


def map_whatsapp_login_state(
    *,
    runtime_state: Optional[Dict[str, Any]] = None,
    session_state: Optional[Dict[str, Any]] = None,
    account_enabled: bool = True,
    startup_grace_seconds: float = 60.0,
    max_initializing_seconds: float = 120.0,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Map raw WhatsApp runtime/session fields to one operator-facing state.

    This function is intentionally side-effect free. It must not call worker health,
    start/stop runtime, probe group-state, or render QR images.
    """

    runtime = dict(runtime_state or {})
    session = dict(session_state or {})
    runtime_status = _text(runtime.get('status')).lower()
    login_check_status = _text(session.get('login_check_status')).lower()
    active = _bool(runtime.get('active'))
    configured = _bool(runtime.get('configured')) or bool(_text(runtime.get('base_url')) or _text(runtime.get('pid')))
    health_error = _text(runtime.get('health_error') or session.get('health_error'))
    login_verified = _bool(session.get('login_verified'))
    authenticated = _bool(session.get('authenticated'))
    ready = _bool(session.get('ready'))
    qr_ready = _has_qr_payload(session)
    reconnect_state = _text(
        session.get('reconnect_state')
        or session.get('reconnectState')
        or runtime.get('reconnect_state')
        or runtime.get('reconnectState')
    ).lower()
    auth_failure_text = ' '.join(
        _text(value).lower()
        for value in (
            session.get('last_disconnect_reason'),
            session.get('last_error'),
            runtime.get('last_disconnect_reason'),
            runtime.get('last_error'),
        )
        if _text(value)
    )
    permanent_auth_failure = bool(
        reconnect_state == 'stopped'
        and any(marker in auth_failure_text for marker in ('401', '403', 'loggedout', 'forbidden'))
    )
    started_age = _age_seconds(runtime.get('started_at'), now=now)
    in_startup_grace = bool(
        started_age is not None
        and started_age <= max(float(startup_grace_seconds), 1.0)
        and (_text(runtime.get('base_url')) or _text(runtime.get('pid')) or _text(runtime.get('port')))
        and not (_text(runtime.get('stopped_at')) and (_age_seconds(runtime.get('stopped_at'), now=now) or 0) <= started_age)
    )
    initializing_expired = bool(
        started_age is not None
        and started_age > max(float(max_initializing_seconds), float(startup_grace_seconds), 1.0)
    )

    if not account_enabled:
        state = 'disabled'
    elif login_verified or (authenticated and ready and login_check_status in {'passed', 'authenticated', 'ready'}):
        state = 'logged_in'
    elif login_check_status == 'account_restricted':
        state = 'account_restricted'
    elif login_check_status == 'session_mismatch':
        state = 'session_mismatch'
    elif login_check_status in {'auth_failed', 'login_failed'} or runtime_status in {'auth_failed', 'auth_failure'} or permanent_auth_failure:
        state = 'login_failed'
    elif runtime_status == 'login_verifying' or login_check_status == 'login_verifying':
        state = 'login_verifying'
    elif qr_ready:
        state = 'waiting_for_scan_qr_ready'
    elif login_check_status == 'qr_expired':
        state = 'qr_expired'
    elif login_check_status in {'waiting_for_scan', 'qr_pending', 'needs_scan'}:
        state = 'waiting_for_scan_qr_pending'
    elif login_check_status in {'runtime_recovering', 'local_auth_recovering'}:
        state = 'runtime_recovering'
    elif runtime_status in {'initializing', 'pending_runtime'} or login_check_status in {'pending_runtime', 'auto_recovering'}:
        state = 'login_failed' if initializing_expired else 'initializing'
    elif not configured:
        state = 'runtime_stopped'
    elif not active or runtime_status in {'stopped', 'unavailable', 'not_started', 'runtime_unavailable'} or health_error:
        state = 'runtime_starting' if in_startup_grace else 'runtime_unhealthy'
    else:
        state = 'unknown'

    can_probe = state == 'logged_in'
    can_show_qr = state == 'waiting_for_scan_qr_ready'
    should_auto_rebuild = state == 'runtime_unhealthy'
    login_action = {
        'disabled': 'none',
        'runtime_stopped': 'start_session',
        'runtime_starting': 'wait_or_refresh',
        'runtime_recovering': 'wait_or_refresh',
        'initializing': 'wait_or_refresh',
        'login_verifying': 'wait_or_refresh',
        'waiting_for_scan_qr_ready': 'scan_qr',
        'waiting_for_scan_qr_pending': 'refresh_session',
        'qr_expired': 'manual_reset',
        'logged_in': 'none',
        'login_failed': 'manual_reset',
        'runtime_unhealthy': 'manual_recover',
        'account_restricted': 'check_phone',
        'session_mismatch': 'manual_reset',
        'unknown': 'refresh_status',
    }.get(state, 'refresh_status')

    return {
        'login_state': state,
        'login_state_label': LOGIN_STATE_LABELS.get(state, LOGIN_STATE_LABELS['unknown']),
        'login_action': login_action,
        'can_probe': can_probe,
        'can_show_qr': can_show_qr,
        'should_auto_rebuild': should_auto_rebuild,
        'startup_grace_active': in_startup_grace,
        'initializing_expired': initializing_expired,
        'qr_available': can_show_qr,
    }


def enrich_whatsapp_login_state(
    session_state: Optional[Dict[str, Any]],
    *,
    runtime_state: Optional[Dict[str, Any]] = None,
    account_enabled: bool = True,
    startup_grace_seconds: float = 60.0,
    max_initializing_seconds: float = 120.0,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    session = dict(session_state or {})
    mapped = map_whatsapp_login_state(
        runtime_state=runtime_state,
        session_state=session,
        account_enabled=account_enabled,
        startup_grace_seconds=startup_grace_seconds,
        max_initializing_seconds=max_initializing_seconds,
        now=now,
    )
    session.update(mapped)
    return session
