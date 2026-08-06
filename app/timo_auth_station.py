from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from app.timo_guild_identity import timo_guild_display_name

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


OTP_RE = re.compile(r'^[A-Za-z0-9]{5}$')
_TRANSIENT_OTP_STORE: Dict[str, Dict[str, Any]] = {}
DEFAULT_OTP_EXTRACT_POLICY = {
    'version': '20260708-refresh-official-chat',
    'candidate_strategy': 'latest_official_message',
    'preferred_source': 'timo_official_assistant_system_message',
    'dismiss_blocking_popup': True,
    'refresh_official_chat_before_read': True,
    'reject_recent_duplicate_code_seconds': 900,
    'include_masked_sample_on_failure': True,
}
OTP_EVIDENCE_FAILURE_REASONS = {
    'delivery_not_observed',
    'page_no_change',
    'page_changed_parse_miss',
    'page_changed_no_otp_semantics',
    'old_otp_only',
    'multiple_otp_candidates',
    'candidate_out_of_window',
    'candidate_already_used',
    'notification_seen_but_ui_no_delta',
    'official_assistant_not_found',
    'system_message_tab_not_found',
    'page_not_ready',
    'observation_not_ready',
    'device_unhealthy',
    'app_version_changed',
    'parse_miss',
    'parse_ambiguous',
}
MIN_RELAY_VERSION_FOR_OTP = 'timo-auth-station-relay-v4.86-prearm-baseline-delta-gate'
# Idle probes run on a 20-second cadence and a slow device dump can take more
# than 15 seconds. Keep the dashboard/runtime lease long enough to bridge one
# probe cycle; the OTP activation path still performs its own fresh prearm.
IDLE_OBSERVATION_TTL_SECONDS = 180
ACTIVE_RECOVERY_STATUSES = {
    'created',
    'chrome_profile_checking',
    'otp_required',
    'otp_request_queued',
    'otp_request_created',
    'otp_reading',
    'otp_received',
    'otp_submitting',
    'ticket_extracting',
    'guild_verifying',
    'precheck_device_ready',
    'station_observation_ready',
    'pre_request_snapshot',
    'timo_send_requested',
    'timo_send_accepted',
    'delivery_waiting',
    'evidence_collecting',
    'page_refreshing',
    'otp_candidate_found',
    'otp_validated',
    'otp_consuming',
    'otp_l4_consumed',
    'ticket_verifying',
    'browser_submit_started',
    'browser_submit_accepted',
    'ticket_candidate_collection_started',
    'ticket_candidate_captured',
    'ticket_probe_passed',
    'ticket_persisted',
    'post_persist_probe_passed',
}
RECOVERY_PHASE_STATUSES = ACTIVE_RECOVERY_STATUSES | {'cooldown', 'blocked'}
COUNTRY_TIMEZONES = {
    'mx': 'America/Mexico_City',
    'mexico': 'America/Mexico_City',
    'br': 'America/Sao_Paulo',
    'brazil': 'America/Sao_Paulo',
    'id': 'Asia/Jakarta',
    'indonesia': 'Asia/Jakarta',
    'ph': 'Asia/Manila',
    'philippines': 'Asia/Manila',
    'tr': 'Europe/Istanbul',
    'turkey': 'Europe/Istanbul',
    'cl': 'America/Santiago',
    'chile': 'America/Santiago',
    'co': 'America/Bogota',
    'colombia': 'America/Bogota',
    've': 'America/Caracas',
    'venezuela': 'America/Caracas',
    'pe': 'America/Lima',
    'peru': 'America/Lima',
    'ar': 'America/Argentina/Buenos_Aires',
    'argentina': 'America/Argentina/Buenos_Aires',
}


class AuthStationHeartbeatRequest(BaseModel):
    station_id: str = Field(min_length=1)
    account_fingerprint: str = ''
    device_id: str = ''
    device_status: str = ''
    adb_status: str = ''
    app_status: str = ''
    page_status: str = ''
    battery_level: Optional[int] = None
    charging: Optional[bool] = None
    app_version: str = ''
    relay_version: str = ''
    last_otp_received_at: str = ''
    last_error_code: Optional[str] = None
    last_error_message: str = ''
    screen_unlocked: Optional[bool] = None
    timo_app_installed: Optional[bool] = None
    timo_package_name: str = ''
    timo_app_version_name: str = ''
    timo_app_version_code: str = ''
    notification_permission_enabled: Optional[bool] = None
    notification_listener_enabled: Optional[bool] = None
    accessibility_enabled: Optional[bool] = None
    battery_optimization_ignored: Optional[bool] = None
    network_connected: Optional[bool] = None
    official_assistant_page_ready: Optional[bool] = None
    last_page_fingerprint: str = ''
    last_message_count: Optional[int] = None
    last_successful_ui_dump_at: str = ''
    device_health: str = ''
    locator_profile_status: str = ''
    ui_probe_status: str = ''
    ui_probe_node_count: Optional[int] = None
    ui_probe_text_count: Optional[int] = None
    ui_probe_text_hashes_json: str = ''
    ui_probe_marker_hits_json: str = ''
    dump_duration_ms: Optional[int] = None
    dump_timeout_count_10m: Optional[int] = None
    dump_timeout_count_1h: Optional[int] = None
    last_dump_error: str = ''
    last_dump_error_at: str = ''
    last_official_assistant_ready_at: str = ''
    observation_ready: Optional[bool] = None
    observation_ready_at: str = ''
    relay_restart_count: Optional[int] = None


class AuthStationOtpResultRequest(BaseModel):
    station_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    otp: str = ''
    otp_fingerprint: str = ''
    otp_code_fingerprint: str = ''
    source: str = ''
    received_at: str = ''
    error_code: str = ''
    error_message: str = ''
    phase: str = ''
    delivery_state: str = ''
    parse_status: str = 'not_run'
    parse_miss_reason: str = ''
    delivery_confidence_level: str = 'L0'
    final_failure_reason: str = ''
    evidence_summary: Dict[str, Any] = Field(default_factory=dict)


class AuthStationRecoveryRunRequest(BaseModel):
    guild_id: str = Field(min_length=1)
    account_fingerprint: str = ''
    trigger_reason: str = 'manual'
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AuthStationRecoveryPhaseRequest(BaseModel):
    status: str = Field(min_length=1)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AuthStationLocatorProfileRequest(BaseModel):
    platform: str = 'android'
    app_version_name: str = ''
    app_version_code: str = ''
    locale: str = ''
    language: str = ''
    brand: str = ''
    model: str = ''
    resolution: str = ''
    orientation: str = 'portrait'
    profile_state: str = 'profile_learning'
    device_resolution_class: str = ''
    official_assistant_locator: Dict[str, Any] = Field(default_factory=dict)
    system_message_tab_locator: Dict[str, Any] = Field(default_factory=dict)
    otp_template_profile_id: str = ''
    status: str = 'testing'


class AuthStationDriftTestRequest(BaseModel):
    station_id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    scenarios: list[str] = Field(default_factory=lambda: ['launcher', 'other_app', 'force_stop', 'screen_off', 'adb_reconnect'])
    rounds: int = Field(default=1, ge=1, le=10)


class AuthStationDriftTestEventRequest(BaseModel):
    station_id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    event: str = Field(min_length=1)
    details: Dict[str, Any] = Field(default_factory=dict)


class AuthStationCreateOtpRequest(BaseModel):
    guild_id: str = Field(min_length=1)
    account_fingerprint: str = ''
    station_id: str = ''
    ttl_seconds: int = 120
    request_channel: str = 'auth_station'
    activate_immediately: bool = True


class AuthStationDeviceBindingRequest(BaseModel):
    station_id: str = Field(min_length=1)
    device_serial: str = Field(min_length=1)
    guild_id: str = ''
    guild_name: str = Field(min_length=1)
    account_fingerprint: str = ''
    status: str = 'active'
    metadata: Dict[str, Any] = Field(default_factory=dict)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace('+00:00', 'Z')


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def fingerprint_secret() -> str:
    secret = str(os.getenv('TIMO_OTP_FINGERPRINT_SECRET') or os.getenv('AUTH_INTERNAL_TOKEN') or '').strip()
    return secret or 'local-dev-timo-otp-fingerprint-secret'


def otp_fingerprint(*, otp_request_id: str, otp: str) -> str:
    normalized = str(otp or '').strip().lower()
    return hmac.new(
        fingerprint_secret().encode('utf-8'),
        f'{otp_request_id}:{normalized}'.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()[:16]


def otp_code_fingerprint(otp: str) -> str:
    normalized = str(otp or '').strip().lower()
    return hmac.new(
        fingerprint_secret().encode('utf-8'),
        f'code:{normalized}'.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()[:16]


def _json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, separators=(',', ':'))


EVIDENCE_ALLOWED_KEYS = {
    'request_id', 'guild_id', 'station_id', 'device_serial', 'relay_version', 'phase', 'page_key', 'page_fingerprint',
    'page_status', 'device_health', 'pre_request_page_fingerprint',
    'post_request_page_fingerprint', 'page_fingerprint_changed', 'message_count_before',
    'message_count_after', 'message_count_delta', 'latest_message_hashes_before',
    'latest_message_hashes_after', 'notification_seen_after_request',
    'notification_count_delta', 'notification_fingerprint', 'otp_candidate_count',
    'otp_candidate_fingerprints', 'new_otp_candidate_count',
    'parse_status', 'parse_miss_reason', 'delivery_confidence_level',
    'final_failure_reason', 'official_assistant_present', 'system_message_tab_present',
    'prearm_page_ready',
    'collected_at', 'attempts', 'waited_seconds', 'last_error_code',
    'evidence_sources', 'old_candidate_only', 'can_consume', 'confidence_reason',
    'refresh_ladder_steps', 'refresh_ladder_runs', 'baseline_skipped_after_timo_send',
    'navigation_actions', 'navigation_action_count', 'navigation_terminal_action',
    'route_replay_count',
    'dump_duration_ms', 'prearm_elapsed_ms', 'navigation_budget_ms',
    'prearm_navigation_attempted', 'prearm_fast_path',
    'prearm_fixed_route_fallback', 'prearm_fixed_route_exact',
    'fast_observation_failure_code', 'fast_observation_timeout_ms',
    'adb_dump_step_timeout_ms',
    'accessibility_snapshot_age_ms', 'accessibility_snapshot_fresh',
    'live_foreground_verified',
    'accessibility_permission_ready', 'accessibility_package_match',
    'accessibility_official_present', 'accessibility_system_present',
    'accessibility_exact_profile', 'accessibility_system_locator_ready',
    'live_foreground_category', 'location_permission_controller_dismissed',
    'granted_location_permission_count', 'remaining_location_permission_count',
    'location_permission_revoked', 'location_permission_redline_ok',
    'screenshot_ocr_available', 'screenshot_ocr_candidate_count',
    'screenshot_ocr_error_code', 'candidate_observed_at_ms',
    'selected_candidate_message_time', 'selected_candidate_line_index',
    'secondary_error_code', 'secondary_error_message',
}


def _safe_evidence_summary(value: Any) -> Dict[str, Any]:
    """Keep the evidence contract structured and prevent accidental raw UI/OTP persistence."""
    if not isinstance(value, dict):
        return {}
    result: Dict[str, Any] = {}
    for key in EVIDENCE_ALLOWED_KEYS:
        if key not in value:
            continue
        item = value.get(key)
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[key] = str(item)[:240] if isinstance(item, str) else item
        elif isinstance(item, list):
            list_limit = 24 if key == 'navigation_actions' else 16 if key == 'refresh_ladder_steps' else 8
            result[key] = [str(entry)[:120] for entry in item[:list_limit]]
    return result


def _evidence_confidence_gate(payload: AuthStationOtpResultRequest) -> tuple[str, bool, str]:
    """Return the server-side consume decision without accepting raw OTP evidence."""
    evidence = payload.evidence_summary if isinstance(payload.evidence_summary, dict) else {}
    declared_level = str(payload.delivery_confidence_level or evidence.get('delivery_confidence_level') or '').strip().upper()
    if not evidence and declared_level in {'', 'L0'} and str(payload.parse_status or 'not_run').strip() in {'', 'not_run'}:
        # Keep the legacy station contract compatible; new relays always send evidence.
        return 'L0', True, 'legacy_no_evidence_contract'
    candidate_count = int(evidence.get('otp_candidate_count') or 0)
    parse_status = str(payload.parse_status or evidence.get('parse_status') or '').strip()
    old_candidate = bool(evidence.get('old_candidate_only'))
    if declared_level == 'L4' and candidate_count in {0, 1} and parse_status in {'', 'candidate_found'} and not old_candidate:
        return 'L4', True, 'unique_new_strong_template'
    if declared_level == 'L4' and candidate_count == 1 and not old_candidate:
        return 'L4', True, 'relay_l4_unique_candidate'
    if (
        declared_level == 'L4'
        and bool(evidence.get('baseline_skipped_after_timo_send'))
        and bool(evidence.get('can_consume'))
        and int(evidence.get('new_otp_candidate_count') or 0) == 1
        and parse_status in {'', 'candidate_found'}
        and not old_candidate
    ):
        return 'L4', True, 'activated_after_timo_send_unique_new_candidate'
    return declared_level or 'L0', False, str(evidence.get('confidence_reason') or 'delivery_evidence_below_l4')[:120]


def _json_dict(value: Any) -> Dict[str, Any]:
    try:
        parsed = json.loads(str(value or '{}'))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or '[]'))
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _db_bool(value: Optional[bool]) -> Optional[int]:
    return 1 if value is True else 0 if value is False else None


def _flag_is_false(value: Any) -> bool:
    return value is False or value == 0 or str(value).strip().lower() in {'false', 'no', 'off'}


def _cooldown_seconds(trigger_reason: str) -> int:
    if str(os.getenv('TIMO_OTP_COOLDOWN_ENABLED') or '1').strip().lower() in {'0', 'false', 'no', 'off'}:
        return 0
    lowered = str(trigger_reason or '').strip().lower()
    if 'proactive' in lowered or 'scheduled' in lowered:
        return max(int(os.getenv('TIMO_OTP_PROACTIVE_COOLDOWN_SECONDS') or 1800), 0)
    return max(int(os.getenv('TIMO_OTP_EMERGENCY_COOLDOWN_SECONDS') or 300), 0)


def _otp_window_budget(now_dt: Optional[datetime] = None) -> Dict[str, Any]:
    started_at = now_dt or utc_now()
    provider_window_seconds = min(
        max(int(os.getenv('TIMO_OTP_PROVIDER_VALIDITY_SECONDS') or 120), 60),
        180,
    )
    min_submit_budget_seconds = max(int(os.getenv('TIMO_OTP_MIN_SUBMIT_BUDGET_SECONDS') or 15), 5)
    min_ticket_probe_budget_seconds = max(int(os.getenv('TIMO_OTP_MIN_TICKET_PROBE_BUDGET_SECONDS') or 10), 5)
    window_deadline = started_at + timedelta(seconds=provider_window_seconds)
    submit_deadline = window_deadline - timedelta(seconds=min_ticket_probe_budget_seconds)
    read_deadline = submit_deadline - timedelta(seconds=min_submit_budget_seconds)
    return {
        'provider_window_seconds': provider_window_seconds,
        'min_submit_budget_seconds': min_submit_budget_seconds,
        'min_ticket_probe_budget_seconds': min_ticket_probe_budget_seconds,
        'window_deadline': window_deadline.isoformat().replace('+00:00', 'Z'),
        'submit_deadline': submit_deadline.isoformat().replace('+00:00', 'Z'),
        'read_deadline': read_deadline.isoformat().replace('+00:00', 'Z'),
    }


def _station_device_readiness(row: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    current = now or utc_now()
    heartbeat_at = parse_iso_datetime(str((row or {}).get('last_heartbeat_at') or ''))
    heartbeat_age_seconds = (
        max(0, int((current - heartbeat_at).total_seconds()))
        if heartbeat_at
        else None
    )
    station_online = str((row or {}).get('status') or '').strip().lower() == 'online'
    heartbeat_fresh = heartbeat_age_seconds is not None and heartbeat_age_seconds <= 90
    adb_status = str((row or {}).get('adb_status') or '').strip().lower()
    adb_connected = adb_status == 'connected'
    app_status = str((row or {}).get('app_status') or '').strip().lower()
    app_foreground = app_status == 'foreground'
    app_observation_unknown = app_status in {'', 'unknown', 'unobserved'}
    health_ok = str((row or {}).get('device_health') or '').strip().lower() != 'unhealthy'
    device_reasons = []
    if not station_online:
        device_reasons.append('station 离线')
    if not heartbeat_fresh:
        device_reasons.append('心跳过期')
    if adb_status in {'disconnected', 'offline', 'unauthorized'}:
        device_reasons.append('ADB 未连接')
    elif not adb_connected:
        device_reasons.append('ADB 状态无法确认')
    if not app_foreground:
        device_reasons.append('Timo 状态无法确认' if app_observation_unknown else 'Timo 未在前台')
    if not health_ok:
        device_reasons.append('设备健康异常')
    if _flag_is_false((row or {}).get('screen_unlocked')):
        device_reasons.append('屏幕未解锁')
    if _flag_is_false((row or {}).get('timo_app_installed')):
        device_reasons.append('Timo 未安装')
    if _flag_is_false((row or {}).get('network_connected')):
        device_reasons.append('网络未连接')
    device_ready = not device_reasons

    observation_at = parse_iso_datetime(str((row or {}).get('last_successful_ui_dump_at') or ''))
    observation_age_seconds = (
        max(0, int((current - observation_at).total_seconds()))
        if observation_at
        else None
    )
    observation_reasons = list(device_reasons)
    if not bool((row or {}).get('official_assistant_page_ready')):
        observation_reasons.append('官方助手页未确认')
    if observation_age_seconds is None:
        observation_reasons.append('最近 UI dump 不可用')
    elif observation_age_seconds > IDLE_OBSERVATION_TTL_SECONDS:
        observation_reasons.append('最近 UI dump 已过期')
    if str((row or {}).get('ui_probe_status') or '').strip().lower() in {'dump_error', 'idle_probe_error'}:
        observation_reasons.append('UI observation 失败')
    if str((row or {}).get('last_dump_error') or '').strip():
        observation_reasons.append('最近 UI dump 报错')
    observation_ready = device_ready and not observation_reasons

    otp_reasons = list(observation_reasons)
    locator_status = str((row or {}).get('locator_profile_status') or '').strip().lower()
    if locator_status not in {'active', 'exact_match', 'fallback_ratio', 'ios_wda'}:
        otp_reasons.append('Timo 页面定位配置缺失')
    otp_ready = observation_ready and not otp_reasons
    blocked_reason = ''
    if otp_reasons:
        blocked_reason = str((row or {}).get('last_dump_error') or '').strip() or otp_reasons[0]
    return {
        'heartbeat_age_seconds': heartbeat_age_seconds,
        'transport_ready': device_ready,
        'device_ready': device_ready,
        'device_ready_label': '设备就绪' if device_ready else '设备未就绪',
        'device_ready_reasons': device_reasons,
        'observation_ready': observation_ready,
        'observation_ready_label': '观察就绪' if observation_ready else '观察未就绪',
        'observation_ready_reasons': observation_reasons,
        'observation_ready_age_seconds': observation_age_seconds,
        'otp_ready': otp_ready,
        'otp_ready_label': '取码就绪' if otp_ready else '取码未就绪',
        'otp_ready_reasons': otp_reasons,
        'blocked_reason': blocked_reason,
    }


def _iso_minus_seconds(seconds: int) -> str:
    return (utc_now() - timedelta(seconds=max(int(seconds or 0), 0))).isoformat().replace('+00:00', 'Z')


def _ticket_fingerprint(value: str) -> str:
    normalized = str(value or '').strip()
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16] if normalized else ''


def _relay_version_number(value: str) -> tuple[int, int]:
    match = re.search(r'-v(\d+)(?:\.(\d+))?', str(value or ''))
    if not match:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2) or 0))


def _relay_version_can_read_otp(value: str) -> bool:
    return _relay_version_number(value) >= _relay_version_number(MIN_RELAY_VERSION_FOR_OTP)


def _country_timezone(country: str) -> str:
    normalized = str(country or '').strip().lower()
    if normalized in COUNTRY_TIMEZONES:
        return COUNTRY_TIMEZONES[normalized]
    tokens = {token for token in re.split(r'[^a-z0-9]+', normalized) if token}
    for token in tokens:
        if token in COUNTRY_TIMEZONES:
            return COUNTRY_TIMEZONES[token]
    return 'Asia/Shanghai'


def _timezone(name: str):
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(str(name or 'Asia/Shanghai').strip() or 'Asia/Shanghai')
    except Exception:
        return ZoneInfo('Asia/Shanghai')


def _next_ticket_refresh_for_guild(
    *,
    guild_name: str,
    country: str,
    ticket_updated_at: str = '',
    schedule_state: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now_utc = now or utc_now()
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    max_ticket_age_hours = max(int(os.getenv('TIMO_TICKET_REFRESH_MAX_TICKET_AGE_HOURS') or 22), 0)
    tz_name = _country_timezone(country or guild_name)
    tz = _timezone(tz_name)
    age_due_utc = None
    ticket_updated = parse_iso_datetime(ticket_updated_at)
    if max_ticket_age_hours and ticket_updated:
        age_due_utc = ticket_updated + timedelta(hours=max_ticket_age_hours)
    start_utc = max(age_due_utc or now_utc, now_utc)
    start = start_utc.astimezone(tz)
    reason = 'ticket_age_exceeded' if age_due_utc and age_due_utc <= now_utc else 'ticket_age_limit'
    if not ticket_updated:
        reason = 'ticket_timestamp_missing'
    return {
        'timezone': tz_name,
        'local_date': start.date().isoformat(),
        'scheduled_local_time': '',
        'next_local_at': start.isoformat(),
        'next_utc_at': start_utc.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'reason': reason,
        'max_ticket_age_hours': max_ticket_age_hours,
        'age_due_utc_at': age_due_utc.isoformat().replace('+00:00', 'Z') if age_due_utc else '',
        'claimed_today': False,
        'last_claimed_at': '',
        'window_minutes': 0,
    }


def _load_json_file(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


class TimoAuthStationService:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def connect(self) -> sqlite3.Connection:
        from app.sqlite_write_window import connect_short_write_sqlite  # noqa: PLC0415

        conn = connect_short_write_sqlite(
            self.db_path,
            lock_name='sqlite-writer',
            source='timo-auth-station',
            busy_timeout_ms_override=3000,
            write_window_timeout_seconds=3.0,
            write_lock_timeout_seconds=3.0,
        )
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _canonical_timo_guild_identity(
        conn: sqlite3.Connection,
        guild_id: str,
    ) -> tuple[str, tuple[str, ...], Dict[str, Any]]:
        """Resolve every executor alias to one recovery/lock identity."""
        normalized = str(guild_id or '').strip()
        if not normalized:
            return '', (), {}
        try:
            row = conn.execute(
                """
                SELECT guild_name, cms_guild_id, cms_guild_sid, country,
                       platform_authorization
                FROM guild_executors
                WHERE LOWER(COALESCE(app_name, 'linky')) = 'timo'
                  AND (
                        LOWER(TRIM(COALESCE(cms_guild_sid, ''))) = LOWER(TRIM(?))
                     OR LOWER(TRIM(COALESCE(cms_guild_id, ''))) = LOWER(TRIM(?))
                     OR LOWER(TRIM(COALESCE(guild_name, ''))) = LOWER(TRIM(?))
                  )
                ORDER BY COALESCE(enabled, 0) DESC, updated_at DESC
                LIMIT 1
                """,
                (normalized, normalized, normalized),
            ).fetchone()
        except sqlite3.Error:
            row = None
        executor = dict(row) if row else {}
        canonical = str(
            executor.get('cms_guild_sid')
            or executor.get('cms_guild_id')
            or executor.get('guild_name')
            or normalized
        ).strip()
        aliases: list[str] = []
        for value in (
            canonical,
            normalized,
            executor.get('cms_guild_sid'),
            executor.get('cms_guild_id'),
            executor.get('guild_name'),
        ):
            text = str(value or '').strip()
            if text and text.casefold() not in {item.casefold() for item in aliases}:
                aliases.append(text)
        return canonical, tuple(aliases), executor

    @staticmethod
    def _guild_alias_clause(column: str, aliases: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
        normalized = tuple(str(value or '').strip().casefold() for value in aliases if str(value or '').strip())
        if not normalized:
            return '1=0', ()
        return f"LOWER(TRIM(COALESCE({column}, ''))) IN ({','.join('?' for _ in normalized)})", normalized

    def _expire_stale_otp_requests(self, conn: sqlite3.Connection, now: str) -> None:
        stale_rows = conn.execute(
            """
            SELECT otp_request_id, recovery_id FROM timo_otp_requests
            WHERE status IN ('queued', 'pending', 'reading') AND expires_at <= ?
            """,
            (now,),
        ).fetchall()
        if not stale_rows:
            return
        recovery_ids = [str(row['recovery_id'] or '').strip() for row in stale_rows if str(row['recovery_id'] or '').strip()]
        conn.execute(
            """
            UPDATE timo_otp_requests
            SET status='expired', error_code='otp_request_expired', updated_at=?
            WHERE status IN ('queued', 'pending', 'reading') AND expires_at <= ?
            """,
            (now, now),
        )
        for recovery_id in recovery_ids:
            conn.execute(
                """
                UPDATE timo_recovery_runs
                SET status='failed', error_code='otp_request_expired', error_message='OTP request expired before Auth Station submitted a code.', updated_at=?
                WHERE recovery_id=? AND status IN ({})
                """.format(','.join('?' for _ in ACTIVE_RECOVERY_STATUSES)),
                (now, recovery_id, *sorted(ACTIVE_RECOVERY_STATUSES)),
            )
            conn.execute('DELETE FROM timo_guild_operation_locks WHERE operation_id=?', (recovery_id,))

    @staticmethod
    def _ensure_device_heartbeat_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS timo_auth_station_device_heartbeats (
                station_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                station_name TEXT NOT NULL DEFAULT '',
                account_fingerprint TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                last_heartbeat_at TEXT NOT NULL DEFAULT '',
                device_status TEXT NOT NULL DEFAULT '',
                adb_status TEXT NOT NULL DEFAULT '',
                app_status TEXT NOT NULL DEFAULT '',
                page_status TEXT NOT NULL DEFAULT '',
                battery_level INTEGER,
                charging INTEGER,
                app_version TEXT NOT NULL DEFAULT '',
                relay_version TEXT NOT NULL DEFAULT '',
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error_message TEXT NOT NULL DEFAULT '',
                screen_unlocked INTEGER,
                timo_app_installed INTEGER,
                timo_package_name TEXT NOT NULL DEFAULT '',
                timo_app_version_name TEXT NOT NULL DEFAULT '',
                timo_app_version_code TEXT NOT NULL DEFAULT '',
                notification_permission_enabled INTEGER,
                notification_listener_enabled INTEGER,
                accessibility_enabled INTEGER,
                battery_optimization_ignored INTEGER,
                network_connected INTEGER,
                official_assistant_page_ready INTEGER,
                last_page_fingerprint TEXT NOT NULL DEFAULT '',
                last_message_count INTEGER,
                last_successful_ui_dump_at TEXT NOT NULL DEFAULT '',
                device_health TEXT NOT NULL DEFAULT '',
                locator_profile_status TEXT NOT NULL DEFAULT '',
                ui_probe_status TEXT NOT NULL DEFAULT '',
                ui_probe_node_count INTEGER,
                ui_probe_text_count INTEGER,
                ui_probe_text_hashes_json TEXT NOT NULL DEFAULT '[]',
                ui_probe_marker_hits_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (station_id, device_id)
            )
            """
        )
        existing = {
            str(row['name'])
            for row in conn.execute('PRAGMA table_info(timo_auth_station_device_heartbeats)').fetchall()
        }
        additions = {
            'screen_unlocked': 'INTEGER',
            'timo_app_installed': 'INTEGER',
            'timo_package_name': "TEXT NOT NULL DEFAULT ''",
            'timo_app_version_name': "TEXT NOT NULL DEFAULT ''",
            'timo_app_version_code': "TEXT NOT NULL DEFAULT ''",
            'notification_permission_enabled': 'INTEGER',
            'notification_listener_enabled': 'INTEGER',
            'accessibility_enabled': 'INTEGER',
            'battery_optimization_ignored': 'INTEGER',
            'network_connected': 'INTEGER',
            'official_assistant_page_ready': 'INTEGER',
            'last_page_fingerprint': "TEXT NOT NULL DEFAULT ''",
            'last_message_count': 'INTEGER',
            'last_successful_ui_dump_at': "TEXT NOT NULL DEFAULT ''",
            'device_health': "TEXT NOT NULL DEFAULT ''",
            'locator_profile_status': "TEXT NOT NULL DEFAULT ''",
            'ui_probe_status': "TEXT NOT NULL DEFAULT ''",
            'ui_probe_node_count': 'INTEGER',
            'ui_probe_text_count': 'INTEGER',
            'ui_probe_text_hashes_json': "TEXT NOT NULL DEFAULT '[]'",
            'ui_probe_marker_hits_json': "TEXT NOT NULL DEFAULT '{}'",
            'dump_duration_ms': 'INTEGER',
            'dump_timeout_count_10m': 'INTEGER NOT NULL DEFAULT 0',
            'dump_timeout_count_1h': 'INTEGER NOT NULL DEFAULT 0',
            'last_dump_error': "TEXT NOT NULL DEFAULT ''",
            'last_dump_error_at': "TEXT NOT NULL DEFAULT ''",
            'last_official_assistant_ready_at': "TEXT NOT NULL DEFAULT ''",
            'observation_ready': 'INTEGER NOT NULL DEFAULT 0',
            'observation_ready_at': "TEXT NOT NULL DEFAULT ''",
            'relay_restart_count': 'INTEGER NOT NULL DEFAULT 0',
        }
        for column, declaration in additions.items():
            if column not in existing:
                conn.execute(f'ALTER TABLE timo_auth_station_device_heartbeats ADD COLUMN {column} {declaration}')

    def heartbeat(self, payload: AuthStationHeartbeatRequest) -> Dict[str, Any]:
        from app.sqlite_write_queue import (  # noqa: PLC0415
            SQLiteWriteQueueError,
            db_writer_enabled,
            db_writer_required,
            submit_sqlite_write_job,
        )

        if db_writer_enabled():
            try:
                model_payload = payload.model_dump() if hasattr(payload, 'model_dump') else payload.dict()
                return submit_sqlite_write_job({
                    'type': 'timo_auth_station_heartbeat',
                    'payload': model_payload,
                }, timeout=20.0)
            except SQLiteWriteQueueError:
                if db_writer_required():
                    raise
        return self._heartbeat_direct(payload)

    def _heartbeat_direct(self, payload: AuthStationHeartbeatRequest) -> Dict[str, Any]:
        now = iso_now()
        device_id = str(payload.device_id or '').strip()
        hint_row = None
        with self.connect() as conn:
            self._ensure_device_heartbeat_schema(conn)
            profile_status = str(payload.locator_profile_status or '').strip()
            profile_version = str(payload.timo_app_version_name or payload.app_version or '').strip()
            if profile_version and profile_status in {'', 'unknown'}:
                profile_status = 'known' if conn.execute(
                    "SELECT 1 FROM timo_app_locator_profiles WHERE app_version_name=? AND status='active' LIMIT 1",
                    (profile_version,),
                ).fetchone() else 'missing'
            existing = conn.execute(
                'SELECT app_version, relay_version FROM timo_auth_stations WHERE station_id=?',
                (payload.station_id,),
            ).fetchone()
            app_version = payload.app_version
            relay_version = payload.relay_version
            if existing:
                existing_relay_version = str(existing['relay_version'] or '').strip()
                incoming_relay_version = str(payload.relay_version or '').strip()
                if (
                    existing_relay_version
                    and incoming_relay_version
                    and _relay_version_number(incoming_relay_version) < _relay_version_number(existing_relay_version)
                ):
                    relay_version = existing_relay_version
                    app_version = str(existing['app_version'] or '').strip() or app_version
            conn.execute(
                """
                INSERT INTO timo_auth_stations (
                    station_id, station_name, account_fingerprint, status, last_heartbeat_at,
                    device_id, device_status, adb_status, app_status, page_status, battery_level,
                    charging, app_version, relay_version, last_error_code, last_error_message,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, 'online', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(station_id) DO UPDATE SET
                    account_fingerprint=excluded.account_fingerprint,
                    status='online',
                    last_heartbeat_at=excluded.last_heartbeat_at,
                    device_id=excluded.device_id,
                    device_status=excluded.device_status,
                    adb_status=CASE WHEN excluded.adb_status NOT IN ('', 'unknown') THEN excluded.adb_status ELSE adb_status END,
                    app_status=CASE WHEN excluded.app_status NOT IN ('', 'unknown') THEN excluded.app_status ELSE app_status END,
                    page_status=excluded.page_status,
                    battery_level=excluded.battery_level,
                    charging=excluded.charging,
                    app_version=excluded.app_version,
                    relay_version=excluded.relay_version,
                    last_error_code=COALESCE(NULLIF(excluded.last_error_code, ''), last_error_code),
                    last_error_message=COALESCE(NULLIF(excluded.last_error_message, ''), last_error_message),
                    updated_at=excluded.updated_at
                """,
                (
                    payload.station_id,
                    payload.station_id,
                    payload.account_fingerprint,
                    now,
                    payload.device_id,
                    payload.device_status,
                    payload.adb_status,
                    payload.app_status,
                    payload.page_status,
                    payload.battery_level,
                    1 if payload.charging else 0 if payload.charging is not None else None,
                    app_version,
                    relay_version,
                    payload.last_error_code or '',
                    payload.last_error_message or '',
                    now,
                    now,
                ),
            )
            if device_id:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS timo_auth_station_device_heartbeats (
                        station_id TEXT NOT NULL,
                        device_id TEXT NOT NULL,
                        station_name TEXT NOT NULL DEFAULT '',
                        account_fingerprint TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT '',
                        last_heartbeat_at TEXT NOT NULL DEFAULT '',
                        device_status TEXT NOT NULL DEFAULT '',
                        adb_status TEXT NOT NULL DEFAULT '',
                        app_status TEXT NOT NULL DEFAULT '',
                        page_status TEXT NOT NULL DEFAULT '',
                        battery_level INTEGER,
                        charging INTEGER,
                        app_version TEXT NOT NULL DEFAULT '',
                        relay_version TEXT NOT NULL DEFAULT '',
                        last_error_code TEXT NOT NULL DEFAULT '',
                        last_error_message TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (station_id, device_id)
                    )
                    """
                )
                existing_device = conn.execute(
                    """
                    SELECT app_version, relay_version
                    FROM timo_auth_station_device_heartbeats
                    WHERE station_id=? AND device_id=?
                    """,
                    (payload.station_id, device_id),
                ).fetchone()
                device_app_version = app_version
                device_relay_version = relay_version
                if existing_device:
                    existing_device_relay = str(existing_device['relay_version'] or '').strip()
                    incoming_device_relay = str(payload.relay_version or '').strip()
                    if (
                        existing_device_relay
                        and incoming_device_relay
                        and _relay_version_number(incoming_device_relay) < _relay_version_number(existing_device_relay)
                    ):
                        device_relay_version = existing_device_relay
                        device_app_version = str(existing_device['app_version'] or '').strip() or device_app_version
                conn.execute(
                    """
                    INSERT INTO timo_auth_station_device_heartbeats (
                        station_id, device_id, station_name, account_fingerprint, status, last_heartbeat_at,
                        device_status, adb_status, app_status, page_status, battery_level,
                        charging, app_version, relay_version, last_error_code, last_error_message,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, 'online', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(station_id, device_id) DO UPDATE SET
                        account_fingerprint=excluded.account_fingerprint,
                        status='online',
                        last_heartbeat_at=excluded.last_heartbeat_at,
                        device_status=excluded.device_status,
                        adb_status=CASE WHEN excluded.adb_status NOT IN ('', 'unknown') THEN excluded.adb_status ELSE adb_status END,
                        app_status=CASE WHEN excluded.app_status NOT IN ('', 'unknown') THEN excluded.app_status ELSE app_status END,
                        page_status=excluded.page_status,
                        battery_level=excluded.battery_level,
                        charging=excluded.charging,
                        app_version=excluded.app_version,
                        relay_version=excluded.relay_version,
                        last_error_code=COALESCE(NULLIF(excluded.last_error_code, ''), last_error_code),
                        last_error_message=COALESCE(NULLIF(excluded.last_error_message, ''), last_error_message),
                        updated_at=excluded.updated_at
                    """,
                    (
                        payload.station_id,
                        device_id,
                        payload.station_id,
                        payload.account_fingerprint,
                        now,
                        payload.device_status,
                        payload.adb_status,
                        payload.app_status,
                        payload.page_status,
                        payload.battery_level,
                        1 if payload.charging else 0 if payload.charging is not None else None,
                        device_app_version,
                        device_relay_version,
                        payload.last_error_code or '',
                        payload.last_error_message or '',
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE timo_auth_station_device_heartbeats
                    SET screen_unlocked=?,
                        timo_app_installed=CASE
                            WHEN ? = 'dump_error' AND ? IS NULL THEN NULL
                            WHEN ? IS NOT NULL THEN ?
                            ELSE timo_app_installed
                        END,
                        timo_app_version_name=?,
                        timo_app_version_code=?, notification_permission_enabled=?,
                        notification_listener_enabled=?, accessibility_enabled=?,
                        battery_optimization_ignored=?, network_connected=?,
                        official_assistant_page_ready=COALESCE(?, official_assistant_page_ready),
                        last_page_fingerprint=COALESCE(NULLIF(?, ''), last_page_fingerprint),
                        last_message_count=COALESCE(?, last_message_count),
                        last_successful_ui_dump_at=COALESCE(NULLIF(?, ''), last_successful_ui_dump_at),
                        device_health=COALESCE(NULLIF(?, ''), device_health),
                        locator_profile_status=?,
                        ui_probe_status=COALESCE(NULLIF(?, ''), ui_probe_status),
                        ui_probe_node_count=COALESCE(?, ui_probe_node_count),
                        ui_probe_text_count=COALESCE(?, ui_probe_text_count),
                        ui_probe_text_hashes_json=COALESCE(NULLIF(?, '[]'), ui_probe_text_hashes_json),
                        ui_probe_marker_hits_json=COALESCE(NULLIF(?, '{}'), ui_probe_marker_hits_json),
                        dump_duration_ms=COALESCE(?, dump_duration_ms),
                        dump_timeout_count_10m=COALESCE(?, dump_timeout_count_10m),
                        dump_timeout_count_1h=COALESCE(?, dump_timeout_count_1h),
                        last_dump_error=CASE WHEN ? <> '' THEN ? WHEN ? = 1 THEN '' ELSE last_dump_error END,
                        last_dump_error_at=CASE WHEN ? <> '' THEN ? WHEN ? = 1 THEN '' ELSE last_dump_error_at END,
                        last_official_assistant_ready_at=COALESCE(NULLIF(?, ''), last_official_assistant_ready_at),
                        observation_ready=COALESCE(?, observation_ready),
                        observation_ready_at=COALESCE(NULLIF(?, ''), observation_ready_at),
                        relay_restart_count=COALESCE(?, relay_restart_count),
                        last_error_code=CASE
                            WHEN ? = 1 THEN ''
                            WHEN ? NOT IN ('', 'dump_error') THEN ''
                            ELSE last_error_code
                        END,
                        last_error_message=CASE
                            WHEN ? = 1 THEN ''
                            WHEN ? NOT IN ('', 'dump_error') THEN ''
                            ELSE last_error_message
                        END,
                        updated_at=?
                    WHERE station_id=? AND device_id=?
                    """,
                    (
                        _db_bool(payload.screen_unlocked),
                        str(payload.ui_probe_status or '')[:40],
                        _db_bool(payload.timo_app_installed),
                        _db_bool(payload.timo_app_installed),
                        _db_bool(payload.timo_app_installed),
                        payload.timo_app_version_name or app_version,
                        payload.timo_app_version_code or '',
                        _db_bool(payload.notification_permission_enabled),
                        _db_bool(payload.notification_listener_enabled),
                        _db_bool(payload.accessibility_enabled),
                        _db_bool(payload.battery_optimization_ignored),
                        _db_bool(payload.network_connected),
                        _db_bool(payload.official_assistant_page_ready),
                        str(payload.last_page_fingerprint or '')[:240],
                        payload.last_message_count,
                        str(payload.last_successful_ui_dump_at or '')[:80],
                        str(payload.device_health or '')[:40],
                        profile_status[:40],
                        str(payload.ui_probe_status or '')[:40],
                        payload.ui_probe_node_count,
                        payload.ui_probe_text_count,
                        str(payload.ui_probe_text_hashes_json or '[]')[:1200],
                        str(payload.ui_probe_marker_hits_json or '{}')[:1200],
                        payload.dump_duration_ms,
                        payload.dump_timeout_count_10m,
                        payload.dump_timeout_count_1h,
                        str(payload.last_dump_error or '')[:240],
                        str(payload.last_dump_error or '')[:240],
                        1 if payload.observation_ready is True else 0,
                        str(payload.last_dump_error or '')[:240],
                        str(payload.last_dump_error_at or '')[:80],
                        1 if payload.observation_ready is True else 0,
                        str(payload.last_official_assistant_ready_at or '')[:80],
                        _db_bool(payload.observation_ready),
                        str(payload.observation_ready_at or '')[:80],
                        payload.relay_restart_count,
                        1 if payload.observation_ready is True else 0,
                        str(payload.ui_probe_status or '')[:40],
                        1 if payload.observation_ready is True else 0,
                        str(payload.ui_probe_status or '')[:40],
                        now,
                        payload.station_id,
                        device_id,
                    ),
                )
                package_name = str(payload.timo_package_name or '').strip()
                if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+', package_name):
                    conn.execute(
                        """
                        UPDATE timo_auth_station_device_heartbeats
                        SET timo_package_name=?, updated_at=?
                        WHERE station_id=? AND device_id=?
                        """,
                        (package_name, now, payload.station_id, device_id),
                    )
                hint_row = conn.execute(
                    """
                    SELECT timo_package_name, timo_app_version_name
                    FROM timo_auth_station_device_heartbeats
                    WHERE station_id=? AND device_id=?
                      AND COALESCE(timo_package_name, '') <> ''
                      AND COALESCE(timo_app_version_code, '') <> ''
                    ORDER BY last_heartbeat_at DESC, updated_at DESC
                    LIMIT 1
                    """,
                    (payload.station_id, device_id),
                ).fetchone()
        return {
            'ok': True,
            'server_time': now,
            'timo_package_hint': str(hint_row['timo_package_name'] or '') if hint_row else '',
            'timo_version_name_hint': str(hint_row['timo_app_version_name'] or '') if hint_row else '',
        }

    def list_device_bindings(self, *, station_id: str = '') -> Dict[str, Any]:
        station_id = str(station_id or '').strip()
        with self.connect() as conn:
            if station_id:
                rows = conn.execute(
                    """
                    SELECT * FROM timo_auth_station_device_bindings
                    WHERE station_id = ? AND status = 'active'
                    ORDER BY updated_at DESC
                    """,
                    (station_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM timo_auth_station_device_bindings
                    WHERE status = 'active'
                    ORDER BY updated_at DESC
                    LIMIT 200
                    """
                ).fetchall()
        return {'ok': True, 'rows': self._dedupe_active_device_bindings([dict(row) for row in rows])}

    def status_station_rows(self) -> list[Dict[str, Any]]:
        """Return one heartbeat row per device, retaining legacy station fallback rows."""
        with self.connect() as conn:
            legacy_rows = [
                dict(row)
                for row in conn.execute(
                    'SELECT * FROM timo_auth_stations ORDER BY updated_at DESC LIMIT 50'
                ).fetchall()
            ]
            try:
                device_rows = [
                    dict(row)
                    for row in conn.execute(
                        'SELECT * FROM timo_auth_station_device_heartbeats ORDER BY updated_at DESC LIMIT 200'
                    ).fetchall()
                ]
            except sqlite3.Error:
                device_rows = []
        rows = list(device_rows)
        seen = {
            (
                str(row.get('station_id') or '').strip(),
                str(row.get('device_id') or '').strip(),
            )
            for row in rows
        }
        for row in legacy_rows:
            key = (
                str(row.get('station_id') or '').strip(),
                str(row.get('device_id') or '').strip(),
            )
            if key not in seen:
                rows.append(row)
        now = utc_now()
        for row in rows:
            row.update(_station_device_readiness(row, now=now))
        return rows

    def disable_device_binding(self, binding_id: str, *, updated_by: str = '') -> Dict[str, Any]:
        normalized = str(binding_id or '').strip()
        if not normalized:
            raise ValueError('binding_id_required')
        now = iso_now()
        with self.connect() as conn:
            row = conn.execute(
                'SELECT * FROM timo_auth_station_device_bindings WHERE binding_id=?',
                (normalized,),
            ).fetchone()
            if not row:
                raise KeyError('binding_not_found')
            deleted = dict(row)
            deleted['status'] = 'deleted'
            deleted['deleted_by'] = str(updated_by or '').strip()
            deleted['deleted_at'] = now
            conn.execute(
                'DELETE FROM timo_auth_station_device_bindings WHERE binding_id=?',
                (normalized,),
            )
        return {'ok': True, 'binding': deleted}

    def ops_status_summary(self) -> Dict[str, Any]:
        schedule_state = _load_json_file(Path(self.db_path).resolve().parent / 'timo_ticket_refresh_schedule_state.json')
        now = utc_now()
        with self.connect() as conn:
            guild_rows = conn.execute(
                """
                SELECT guild_name, country, platform_authorization, cms_guild_id, cms_guild_sid,
                       updated_at, enabled
                FROM guild_executors
                WHERE LOWER(COALESCE(app_name, 'linky')) = 'timo'
                ORDER BY guild_name
                """
            ).fetchall()
            station_rows = conn.execute(
                """
                SELECT * FROM timo_auth_stations
                ORDER BY last_heartbeat_at DESC, updated_at DESC
                LIMIT 50
                """
            ).fetchall()
            try:
                device_station_rows = conn.execute(
                    """
                    SELECT *
                    FROM timo_auth_station_device_heartbeats
                    ORDER BY last_heartbeat_at DESC, updated_at DESC
                    LIMIT 200
                    """
                ).fetchall()
            except sqlite3.Error:
                device_station_rows = []
            recovery_rows = conn.execute(
                """
                SELECT * FROM timo_recovery_runs
                ORDER BY updated_at DESC
                LIMIT 80
                """
            ).fetchall()
            otp_rows = conn.execute(
                """
                SELECT * FROM timo_otp_requests
                ORDER BY updated_at DESC
                LIMIT 80
                """
            ).fetchall()
            version_rows = conn.execute(
                """
                SELECT * FROM timo_ticket_versions
                ORDER BY created_at DESC
                LIMIT 80
                """
            ).fetchall()
            try:
                binding_rows = conn.execute(
                    """
                    SELECT * FROM timo_auth_station_device_bindings
                    WHERE status='active'
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
            except sqlite3.Error:
                binding_rows = []

        stations = [dict(row) for row in device_station_rows]
        seen_station_devices = {
            (
                str(row.get('station_id') or '').strip(),
                str(row.get('device_id') or '').strip(),
            )
            for row in stations
        }
        for row in station_rows:
            item = dict(row)
            key = (
                str(item.get('station_id') or '').strip(),
                str(item.get('device_id') or '').strip(),
            )
            if key not in seen_station_devices:
                stations.append(item)
        for station in stations:
            station.update(_station_device_readiness(station, now=now))
        recoveries = [dict(row) for row in recovery_rows]
        otp_requests = [dict(row) for row in otp_rows]
        ticket_versions = [dict(row) for row in version_rows]
        bindings = self._dedupe_active_device_bindings([dict(row) for row in binding_rows])
        binding_by_guild: Dict[str, Dict[str, Any]] = {}
        for binding in bindings:
            for key in (binding.get('guild_id'), binding.get('guild_name')):
                normalized_key = str(key or '').strip()
                if normalized_key and normalized_key not in binding_by_guild:
                    binding_by_guild[normalized_key] = binding
        recovery_by_guild: Dict[str, Dict[str, Any]] = {}
        success_recovery_by_guild: Dict[str, Dict[str, Any]] = {}
        otp_by_guild: Dict[str, Dict[str, Any]] = {}
        ticket_version_by_guild: Dict[str, Dict[str, Any]] = {}
        for row in recoveries:
            guild = str(row.get('guild_id') or '').strip()
            if guild and guild not in recovery_by_guild:
                recovery_by_guild[guild] = row
            if guild and guild not in success_recovery_by_guild and str(row.get('status') or '') == 'restored':
                success_recovery_by_guild[guild] = row
        for row in otp_requests:
            guild = str(row.get('guild_id') or '').strip()
            if guild and guild not in otp_by_guild:
                otp_by_guild[guild] = row
        for row in ticket_versions:
            guild = str(row.get('guild_id') or '').strip()
            if guild and guild not in ticket_version_by_guild:
                ticket_version_by_guild[guild] = row

        guilds = []
        active_recovery_count = sum(1 for row in recoveries if str(row.get('status') or '') in ACTIVE_RECOVERY_STATUSES)
        pending_otp_count = sum(
            1
            for row in otp_requests
            if str(row.get('status') or '') in {'pending', 'reading', 'received'}
            and (not parse_iso_datetime(row.get('expires_at')) or parse_iso_datetime(row.get('expires_at')) > now)
        )
        latest_success_at = ''
        for row in guild_rows:
            guild_name = str(row['guild_name'] or '').strip()
            guild_id = str(row['cms_guild_sid'] or row['cms_guild_id'] or guild_name).strip()
            country = str(row['country'] or '').strip()
            ticket = str(row['platform_authorization'] or '').strip()
            identifiers = (guild_id, guild_name, str(row['cms_guild_id'] or '').strip(), str(row['cms_guild_sid'] or '').strip())
            latest_recovery = next((recovery_by_guild.get(key) for key in identifiers if key and recovery_by_guild.get(key)), {})
            latest_success = next((success_recovery_by_guild.get(key) for key in identifiers if key and success_recovery_by_guild.get(key)), {})
            latest_otp = next((otp_by_guild.get(key) for key in identifiers if key and otp_by_guild.get(key)), {})
            latest_version = next((ticket_version_by_guild.get(key) for key in identifiers if key and ticket_version_by_guild.get(key)), {})
            binding = next((binding_by_guild.get(key) for key in identifiers if key and binding_by_guild.get(key)), {})
            station = next((
                item for item in stations
                if str(item.get('station_id') or '') == str(binding.get('station_id') or '')
                and str(item.get('device_id') or '') == str(binding.get('device_serial') or '')
            ), {})
            active_recovery = str(latest_recovery.get('status') or '') in ACTIVE_RECOVERY_STATUSES
            cooldown_until = parse_iso_datetime(latest_recovery.get('cooldown_until'))
            cooldown_clear = not cooldown_until or cooldown_until <= now
            transport_ready = bool(station.get('transport_ready'))
            observation_ready = bool(station.get('observation_ready'))
            otp_ready = bool(station.get('otp_ready')) and cooldown_clear and not active_recovery
            otp_deadline = parse_iso_datetime(latest_otp.get('otp_window_deadline_at'))
            otp_remaining_seconds = max(0, int((otp_deadline - now).total_seconds())) if otp_deadline else None
            runtime_state = {
                'guild_id': guild_id,
                'ticket_status': 'configured' if ticket else 'missing',
                'ticket_fingerprint': _ticket_fingerprint(ticket),
                'ticket_last_verified_at': str(latest_success.get('post_persist_probe_passed_at') or latest_success.get('finished_at') or ''),
                'ticket_last_probe_result': 'passed' if latest_success else '',
                'station_id': str(binding.get('station_id') or ''),
                'device_serial': str(binding.get('device_serial') or ''),
                'transport_ready': transport_ready,
                'observation_ready': observation_ready,
                'otp_ready': otp_ready,
                'device_health': str(station.get('device_health') or 'unknown'),
                'blocked_reason': (
                    'active_recovery' if active_recovery
                    else 'cooldown_required' if not cooldown_clear
                    else str(station.get('blocked_reason') or 'device_binding_missing') if not otp_ready
                    else ''
                ),
                'last_successful_observation_at': str(station.get('last_successful_ui_dump_at') or ''),
                'last_observation_error_code': str(station.get('last_dump_error') or station.get('last_error_code') or ''),
                'last_observation_error_at': str(station.get('last_dump_error_at') or ''),
                'observation_ready_age_seconds': station.get('observation_ready_age_seconds'),
                'active_otp_request_id': str(latest_otp.get('otp_request_id') or '') if str(latest_otp.get('status') or '') in {'queued', 'pending', 'reading', 'received'} else '',
                'otp_status': str(latest_otp.get('status') or 'idle'),
                'otp_window_deadline_at': str(latest_otp.get('otp_window_deadline_at') or ''),
                'otp_remaining_seconds': otp_remaining_seconds,
                'last_otp_result': str(latest_otp.get('final_failure_reason') or latest_otp.get('status') or ''),
                'recovery_status': str(latest_recovery.get('status') or 'idle'),
                'active_recovery_id': str(latest_recovery.get('recovery_id') or '') if active_recovery else '',
                'last_recovery_id': str(latest_recovery.get('recovery_id') or ''),
                'last_recovery_result': str(latest_recovery.get('error_code') or latest_recovery.get('status') or ''),
                'recovery_started_at': str(latest_recovery.get('started_at') or ''),
                'recovery_finished_at': str(latest_recovery.get('finished_at') or ''),
                'next_allowed_recovery_at': str(latest_recovery.get('cooldown_until') or ''),
                'can_operate': transport_ready and not active_recovery,
                'can_request_otp': otp_ready,
                'can_recover': otp_ready and bool(ticket),
            }
            success_at = (
                str(latest_success.get('finished_at') or '').strip()
                or str(latest_success.get('updated_at') or '').strip()
                or str(latest_version.get('activated_at') or '').strip()
                or str(latest_version.get('created_at') or '').strip()
                or (str(row['updated_at'] or '').strip() if ticket else '')
            )
            if success_at and (not latest_success_at or success_at > latest_success_at):
                latest_success_at = success_at
            guilds.append({
                'guild_name': guild_name,
                'guild_display_name': timo_guild_display_name(
                    guild_name,
                    guild_id=row['cms_guild_id'],
                    guild_sid=row['cms_guild_sid'],
                ),
                'guild_id': guild_id,
                'country': country,
                'enabled': bool(row['enabled']),
                'ticket_configured': bool(ticket),
                'ticket_fingerprint': _ticket_fingerprint(ticket),
                'ticket_updated_at': str(row['updated_at'] or ''),
                'latest_ticket_version': latest_version,
                'latest_recovery': latest_recovery,
                'latest_successful_recovery': latest_success,
                'latest_otp_request': latest_otp,
                'runtime_state': runtime_state,
                'next_ticket_refresh': _next_ticket_refresh_for_guild(
                    guild_name=guild_name,
                    country=country,
                    ticket_updated_at=str(row['updated_at'] or ''),
                    schedule_state=schedule_state,
                    now=now,
                ),
            })
        if guilds:
            with self.connect() as conn:
                for guild in guilds:
                    state = guild['runtime_state']
                    conn.execute(
                        """
                        INSERT INTO timo_guild_runtime_state (
                            guild_id, ticket_status, ticket_fingerprint, ticket_last_verified_at,
                            ticket_last_probe_result, station_id, device_serial, transport_ready,
                            observation_ready, otp_ready, device_health, blocked_reason,
                            last_successful_observation_at, last_observation_error_code,
                            last_observation_error_at, observation_ready_age_seconds,
                            active_otp_request_id, otp_status, otp_window_deadline_at,
                            otp_remaining_seconds, last_otp_result, recovery_status,
                            active_recovery_id, last_recovery_id, last_recovery_result,
                            recovery_started_at, recovery_finished_at, next_allowed_recovery_at,
                            can_operate, can_request_otp, can_recover, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(guild_id) DO UPDATE SET
                            ticket_status=excluded.ticket_status, ticket_fingerprint=excluded.ticket_fingerprint,
                            ticket_last_verified_at=excluded.ticket_last_verified_at,
                            ticket_last_probe_result=excluded.ticket_last_probe_result,
                            station_id=excluded.station_id, device_serial=excluded.device_serial,
                            transport_ready=excluded.transport_ready, observation_ready=excluded.observation_ready,
                            otp_ready=excluded.otp_ready, device_health=excluded.device_health,
                            blocked_reason=excluded.blocked_reason,
                            last_successful_observation_at=excluded.last_successful_observation_at,
                            last_observation_error_code=excluded.last_observation_error_code,
                            last_observation_error_at=excluded.last_observation_error_at,
                            observation_ready_age_seconds=excluded.observation_ready_age_seconds,
                            active_otp_request_id=excluded.active_otp_request_id,
                            otp_status=excluded.otp_status, otp_window_deadline_at=excluded.otp_window_deadline_at,
                            otp_remaining_seconds=excluded.otp_remaining_seconds,
                            last_otp_result=excluded.last_otp_result, recovery_status=excluded.recovery_status,
                            active_recovery_id=excluded.active_recovery_id, last_recovery_id=excluded.last_recovery_id,
                            last_recovery_result=excluded.last_recovery_result,
                            recovery_started_at=excluded.recovery_started_at,
                            recovery_finished_at=excluded.recovery_finished_at,
                            next_allowed_recovery_at=excluded.next_allowed_recovery_at,
                            can_operate=excluded.can_operate, can_request_otp=excluded.can_request_otp,
                            can_recover=excluded.can_recover, updated_at=excluded.updated_at
                        """,
                        (
                            state['guild_id'], state['ticket_status'], state['ticket_fingerprint'],
                            state['ticket_last_verified_at'], state['ticket_last_probe_result'],
                            state['station_id'], state['device_serial'], int(state['transport_ready']),
                            int(state['observation_ready']), int(state['otp_ready']), state['device_health'],
                            state['blocked_reason'], state['last_successful_observation_at'],
                            state['last_observation_error_code'], state['last_observation_error_at'],
                            state['observation_ready_age_seconds'], state['active_otp_request_id'],
                            state['otp_status'], state['otp_window_deadline_at'],
                            state['otp_remaining_seconds'], state['last_otp_result'],
                            state['recovery_status'], state['active_recovery_id'],
                            state['last_recovery_id'], state['last_recovery_result'],
                            state['recovery_started_at'], state['recovery_finished_at'],
                            state['next_allowed_recovery_at'], int(state['can_operate']),
                            int(state['can_request_otp']), int(state['can_recover']),
                            now.isoformat().replace('+00:00', 'Z'),
                        ),
                    )
        relay_versions = sorted({str(row.get('relay_version') or '').strip() for row in stations if str(row.get('relay_version') or '').strip()})
        online_stations = [
            row for row in stations
            if parse_iso_datetime(str(row.get('last_heartbeat_at') or ''))
            and (now - parse_iso_datetime(str(row.get('last_heartbeat_at') or ''))).total_seconds() <= 90
        ]
        return {
            'ok': True,
            'server_time': now.isoformat().replace('+00:00', 'Z'),
            'guilds': guilds,
            'stations': stations,
            'summary': {
                'guild_count': len(guilds),
                'ticket_configured_count': sum(1 for row in guilds if row.get('ticket_configured')),
                'station_online_count': len(online_stations),
                'station_count': len(stations),
                'active_recovery_count': active_recovery_count,
                'pending_otp_count': pending_otp_count,
                'latest_ticket_success_at': latest_success_at,
                'relay_versions': relay_versions,
            },
        }

    def _dedupe_active_device_bindings(self, rows: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        latest_active_by_guild: Dict[str, Dict[str, Any]] = {}
        result: list[Dict[str, Any]] = []
        for row in rows:
            status = str(row.get('status') or '').strip().lower()
            guild_key = str(row.get('guild_name') or '').strip().lower()
            if status == 'active' and guild_key:
                if guild_key in latest_active_by_guild:
                    latest_active_by_guild[guild_key]['duplicate_active_count'] = int(latest_active_by_guild[guild_key].get('duplicate_active_count') or 1) + 1
                    continue
                row['duplicate_active_count'] = 1
                latest_active_by_guild[guild_key] = row
            result.append(row)
        return result

    def upsert_device_binding(self, payload: AuthStationDeviceBindingRequest, *, created_by: str = '') -> Dict[str, Any]:
        station_id = str(payload.station_id or '').strip()
        device_serial = str(payload.device_serial or '').strip()
        guild_name = str(payload.guild_name or '').strip()
        if not station_id:
            raise ValueError('station_id_required')
        if not device_serial:
            raise ValueError('device_serial_required')
        if not guild_name:
            raise ValueError('guild_name_required')
        status = str(payload.status or 'active').strip().lower()
        if status not in {'active', 'disabled'}:
            raise ValueError('invalid_binding_status')
        now = iso_now()
        binding_id = f"tas_bind_{uuid.uuid4().hex[:16]}"
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT binding_id FROM timo_auth_station_device_bindings
                WHERE
                    (station_id = ? AND device_serial = ?)
                    OR (status = 'active' AND lower(guild_name) = lower(?))
                ORDER BY CASE WHEN station_id = ? AND device_serial = ? THEN 0 ELSE 1 END, updated_at DESC
                LIMIT 1
                """,
                (station_id, device_serial, guild_name, station_id, device_serial),
            ).fetchone()
            if existing:
                binding_id = str(existing['binding_id'])
            conn.execute(
                """
                UPDATE timo_auth_station_device_bindings
                SET status='disabled', updated_at=?
                WHERE binding_id <> ?
                  AND status='active'
                  AND (
                    (station_id = ? AND device_serial = ?)
                    OR lower(guild_name) = lower(?)
                  )
                """,
                (now, binding_id, station_id, device_serial, guild_name),
            )
            values = (
                station_id,
                device_serial,
                str(payload.guild_id or '').strip(),
                guild_name,
                str(payload.account_fingerprint or '').strip(),
                status,
                str(created_by or '').strip(),
                _json_dumps(payload.metadata),
                now,
            )
            if existing:
                conn.execute(
                    """
                    UPDATE timo_auth_station_device_bindings
                    SET station_id=?, device_serial=?, guild_id=?, guild_name=?,
                        account_fingerprint=?, status=?, created_by=?,
                        metadata_json=?, updated_at=?
                    WHERE binding_id=?
                    """,
                    (*values, binding_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO timo_auth_station_device_bindings (
                        binding_id, station_id, device_serial, guild_id, guild_name,
                        account_fingerprint, status, created_by, metadata_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (binding_id, *values, now),
                )
            row = conn.execute('SELECT * FROM timo_auth_station_device_bindings WHERE binding_id=?', (binding_id,)).fetchone()
        return {'ok': True, 'binding': dict(row)}

    def create_recovery_run(
        self,
        *,
        guild_id: str,
        account_fingerprint: str,
        trigger_reason: str,
        status: str = 'created',
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        requested_guild_id = str(guild_id or '').strip()
        if not requested_guild_id:
            raise ValueError('guild_id_required')
        now = iso_now()
        with self.connect() as conn:
            self._expire_stale_otp_requests(conn, now)
            guild_id, guild_aliases, executor = self._canonical_timo_guild_identity(conn, requested_guild_id)
            alias_clause, alias_params = self._guild_alias_clause('guild_id', guild_aliases)
            existing = conn.execute(
                f"""
                SELECT * FROM timo_recovery_runs
                WHERE {alias_clause} AND status IN ({','.join('?' for _ in ACTIVE_RECOVERY_STATUSES)})
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (*alias_params, *sorted(ACTIVE_RECOVERY_STATUSES)),
            ).fetchone()
            if existing:
                return {'created': False, 'recovery_run': dict(existing)}
            manual_trigger = (
                str(trigger_reason or '').strip().casefold().startswith('manual_')
            )
            current_ticket = str(executor.get('platform_authorization') or '').strip()
            current_ticket_fingerprint = (
                hashlib.sha256(current_ticket.encode('utf-8')).hexdigest()[:16]
                if current_ticket else ''
            )
            if current_ticket_fingerprint and not manual_trigger:
                max_automated_attempts = max(
                    int(os.getenv('TIMO_OTP_MAX_AUTOMATED_ATTEMPTS_PER_TICKET') or 3),
                    1,
                )
                attempts = int(conn.execute(
                    f"""
                    SELECT COUNT(*) FROM timo_recovery_runs AS recovery
                    WHERE {self._guild_alias_clause('recovery.guild_id', guild_aliases)[0]}
                      AND recovery.ticket_fingerprint_before=?
                      AND LOWER(COALESCE(recovery.trigger_reason, '')) NOT LIKE 'manual_%'
                      AND recovery.status IN ('failed', 'manual_required')
                      AND EXISTS (
                          SELECT 1 FROM timo_otp_requests AS otp_request
                          WHERE otp_request.recovery_id=recovery.recovery_id
                            AND COALESCE(otp_request.otp_provider_accepted_at, '') <> ''
                      )
                    """,
                    (*alias_params, current_ticket_fingerprint),
                ).fetchone()[0])
                if attempts >= max_automated_attempts:
                    previous_block = conn.execute(
                        f"""
                        SELECT * FROM timo_recovery_runs
                        WHERE {alias_clause}
                          AND ticket_fingerprint_before=?
                          AND status='blocked'
                          AND error_code='automated_retry_exhausted'
                        ORDER BY started_at DESC LIMIT 1
                        """,
                        (*alias_params, current_ticket_fingerprint),
                    ).fetchone()
                    if previous_block:
                        blocked = dict(previous_block)
                        blocked['error_code'] = 'automated_retry_exhausted_suppressed'
                        return {'created': False, 'recovery_run': blocked}
                    blocked_id = f"rec_{uuid.uuid4().hex[:16]}"
                    message = (
                        f'Automated OTP recovery stopped after {attempts} attempts for the unchanged ticket.'
                    )
                    conn.execute(
                        """
                        INSERT INTO timo_recovery_runs (
                            recovery_id, guild_id, account_fingerprint, trigger_reason, status,
                            started_at, finished_at, chrome_profile_result, otp_required, otp_source,
                            otp_requested_at, otp_received_at, otp_submitted_at,
                            ticket_fingerprint_before, ticket_fingerprint_after,
                            guild_verify_result, error_code, error_message, metadata_json,
                            created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, 'blocked', ?, ?, '', 0, '', '', '', '', ?, '', '',
                                'automated_retry_exhausted', ?, ?, ?, ?)
                        """,
                        (
                            blocked_id,
                            guild_id,
                            account_fingerprint,
                            trigger_reason,
                            now,
                            now,
                            current_ticket_fingerprint,
                            message,
                            _json_dumps({
                                **dict(metadata or {}),
                                'automated_attempt_count': attempts,
                                'automated_attempt_limit': max_automated_attempts,
                            }),
                            now,
                            now,
                        ),
                    )
                    blocked = conn.execute(
                        'SELECT * FROM timo_recovery_runs WHERE recovery_id=?',
                        (blocked_id,),
                    ).fetchone()
                    return {'created': False, 'recovery_run': dict(blocked)}
            if (
                str(executor.get('country') or '').strip().casefold() == 'mexico'
                and not manual_trigger
            ):
                observation_cooldown_seconds = max(
                    int(os.getenv('TIMO_MX_OBSERVATION_COOLDOWN_SECONDS') or 1800),
                    60,
                )
                cutoff = (
                    utc_now() - timedelta(seconds=observation_cooldown_seconds)
                ).isoformat().replace('+00:00', 'Z')
                previous_observation_failure = conn.execute(
                    f"""
                    SELECT * FROM timo_recovery_runs
                    WHERE {alias_clause}
                      AND status IN ('manual_required', 'failed')
                      AND (
                            error_code = 'observation_not_ready'
                         OR final_failure_reason = 'observation_not_ready'
                      )
                      AND updated_at >= ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (*alias_params, cutoff),
                ).fetchone()
                if previous_observation_failure:
                    cooldown_until = (
                        (parse_iso_datetime(previous_observation_failure['updated_at']) or utc_now())
                        + timedelta(seconds=observation_cooldown_seconds)
                    ).isoformat().replace('+00:00', 'Z')
                    conn.execute(
                        """
                        UPDATE timo_recovery_runs
                        SET cooldown_until=?
                        WHERE recovery_id=?
                        """,
                        (cooldown_until, previous_observation_failure['recovery_id']),
                    )
                    blocked = dict(previous_observation_failure)
                    blocked['cooldown_until'] = cooldown_until
                    return {'created': False, 'recovery_run': blocked}
            recovery_id = f"rec_{uuid.uuid4().hex[:16]}"
            conn.execute('DELETE FROM timo_guild_operation_locks WHERE lease_expires_at<=?', (now,))
            operation_lock = conn.execute(
                f'SELECT operation_type, operation_id, lease_expires_at FROM timo_guild_operation_locks WHERE {alias_clause}',
                alias_params,
            ).fetchone()
            if operation_lock:
                raise ValueError('timo_guild_operation_in_progress')
            conn.execute(
                """
                INSERT INTO timo_recovery_runs (
                    recovery_id, guild_id, account_fingerprint, trigger_reason, status,
                    started_at, finished_at, chrome_profile_result, otp_required, otp_source,
                    otp_requested_at, otp_received_at, otp_submitted_at,
                    ticket_fingerprint_before, ticket_fingerprint_after,
                    guild_verify_result, error_code, error_message, metadata_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, '', '', 0, '', '', '', '', ?, '', '', '', '', ?, ?, ?)
                """,
                (
                    recovery_id, guild_id, account_fingerprint, trigger_reason, status, now,
                    current_ticket_fingerprint, _json_dumps(metadata or {}), now, now,
                ),
            )
            lease_seconds = max(int(os.getenv('TIMO_GUILD_OPERATION_LEASE_SECONDS') or 600), 60)
            lease_expires_at = (utc_now() + timedelta(seconds=lease_seconds)).isoformat().replace('+00:00', 'Z')
            conn.execute(
                """
                INSERT INTO timo_guild_operation_locks (
                    guild_id, operation_type, operation_id, locked_by,
                    locked_at, lease_expires_at, created_at, updated_at
                ) VALUES (?, 'otp_recovery', ?, 'timo_otp_recovery', ?, ?, ?, ?)
                """,
                (guild_id, recovery_id, now, lease_expires_at, now, now),
            )
            row = conn.execute('SELECT * FROM timo_recovery_runs WHERE recovery_id = ?', (recovery_id,)).fetchone()
        return {'created': True, 'recovery_run': dict(row)}

    def create_otp_request(
        self,
        *,
        recovery_id: str,
        guild_id: str,
        account_fingerprint: str,
        station_id: str = '',
        ttl_seconds: int = 120,
        request_channel: str = 'auth_station',
        activate_immediately: bool = True,
    ) -> Dict[str, Any]:
        now_dt = utc_now()
        now = now_dt.isoformat().replace('+00:00', 'Z')
        request_ttl_seconds = max(int(ttl_seconds or 120), 30)
        expires_at = (now_dt + timedelta(seconds=request_ttl_seconds)).isoformat().replace('+00:00', 'Z')
        initial_window = _otp_window_budget(now_dt) if activate_immediately else None
        if initial_window:
            expires_at = str(initial_window['window_deadline'])
        with self.connect() as conn:
            self._expire_stale_otp_requests(conn, now)
            guild_id, guild_aliases, _executor = self._canonical_timo_guild_identity(conn, guild_id)
            alias_clause, alias_params = self._guild_alias_clause('guild_id', guild_aliases)
            current_recovery = conn.execute(
                'SELECT trigger_reason, guild_id FROM timo_recovery_runs WHERE recovery_id=?',
                (recovery_id,),
            ).fetchone()
            if current_recovery:
                recovery_guild_id, recovery_aliases, _recovery_executor = self._canonical_timo_guild_identity(
                    conn,
                    str(current_recovery['guild_id'] or ''),
                )
                if recovery_guild_id and recovery_guild_id.casefold() != guild_id.casefold():
                    raise ValueError('timo_recovery_guild_mismatch')
                if recovery_aliases:
                    guild_aliases = tuple(dict.fromkeys((*guild_aliases, *recovery_aliases)))
                    alias_clause, alias_params = self._guild_alias_clause('guild_id', guild_aliases)
            cooldown_seconds = _cooldown_seconds(str(current_recovery['trigger_reason'] or '') if current_recovery else '')
            if cooldown_seconds:
                cutoff = (now_dt - timedelta(seconds=cooldown_seconds)).isoformat().replace('+00:00', 'Z')
                previous_failure = conn.execute(
                    f"""
                    SELECT recovery_id, updated_at, error_code
                    FROM timo_recovery_runs
                    WHERE {alias_clause}
                      AND recovery_id<>?
                      AND status IN ('manual_required', 'failed')
                      AND error_code IN ({','.join('?' for _ in OTP_EVIDENCE_FAILURE_REASONS)})
                      AND COALESCE(otp_requested_at, '')<>''
                      AND updated_at>=?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (
                        *alias_params,
                        recovery_id,
                        *sorted(OTP_EVIDENCE_FAILURE_REASONS),
                        cutoff,
                    ),
                ).fetchone()
                if previous_failure:
                    cooldown_until = (
                        parse_iso_datetime(previous_failure['updated_at']) or now_dt
                    ) + timedelta(seconds=cooldown_seconds)
                    cooldown_until_text = cooldown_until.isoformat().replace('+00:00', 'Z')
                    conn.execute(
                        """
                        UPDATE timo_recovery_runs
                        SET status='failed', error_code='cooldown_required',
                            error_message='Timo OTP request is temporarily rate-limited after a controlled delivery failure.',
                            cooldown_until=?, updated_at=?
                        WHERE recovery_id=?
                        """,
                        (cooldown_until_text, now, recovery_id),
                    )
                    conn.execute('DELETE FROM timo_guild_operation_locks WHERE operation_id=?', (recovery_id,))
                    conn.commit()
                    raise ValueError('timo_otp_cooldown_required')
            resolved_station_id = str(station_id or '').strip()
            try:
                bound_station_id = self._resolve_ready_station_for_guild(
                    conn,
                    guild_id=guild_id,
                    requested_station_id=resolved_station_id,
                )
            except ValueError as exc:
                # Do not leave a created recovery run blocking the next
                # scheduled attempt when its bound device is unavailable.
                error_code = str(exc)[:120] or 'timo_auth_station_device_not_ready'
                conn.execute(
                    """
                    UPDATE timo_recovery_runs
                    SET status='failed', error_code=?, error_message=?, updated_at=?
                    WHERE recovery_id=? AND status IN ({})
                    """.format(','.join('?' for _ in ACTIVE_RECOVERY_STATUSES)),
                    (
                        error_code,
                        'OTP request blocked before Timo trigger because the bound Auth Station device was not ready.',
                        now,
                        recovery_id,
                        *sorted(ACTIVE_RECOVERY_STATUSES),
                    ),
                )
                conn.execute('DELETE FROM timo_guild_operation_locks WHERE operation_id=?', (recovery_id,))
                conn.commit()
                raise
            if bound_station_id:
                resolved_station_id = bound_station_id
            existing = conn.execute(
                """
                SELECT * FROM timo_otp_requests
                WHERE recovery_id = ? AND status IN ('queued', 'pending', 'reading')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (recovery_id,),
            ).fetchone()
            if existing:
                return {'created': False, 'otp_request': dict(existing)}
            otp_request_id = f"otp_req_{uuid.uuid4().hex[:16]}"
            request_status = 'pending' if activate_immediately else 'queued'
            recovery_status = 'otp_request_created' if activate_immediately else 'otp_request_queued'
            conn.execute(
                """
                INSERT INTO timo_otp_requests (
                    otp_request_id, recovery_id, guild_id, account_fingerprint, station_id,
                    status, request_channel, otp_fingerprint, otp_code_fingerprint,
                    expires_at, received_at, used_at, source, error_code, error_message,
                    created_at, updated_at, otp_requested_at, otp_provider_accepted_at,
                    otp_window_deadline_at, otp_read_deadline_at, otp_submit_deadline_at,
                    otp_remaining_seconds, min_submit_budget_seconds,
                    min_ticket_probe_budget_seconds, window_abort_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, '', '', ?, '', '', '', '', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                """,
                (
                    otp_request_id, recovery_id, guild_id, account_fingerprint,
                    resolved_station_id, request_status, request_channel, expires_at,
                    now, now,
                    now if initial_window else '',
                    now if initial_window else '',
                    str(initial_window['window_deadline']) if initial_window else '',
                    str(initial_window['read_deadline']) if initial_window else '',
                    str(initial_window['submit_deadline']) if initial_window else '',
                    int(initial_window['provider_window_seconds']) if initial_window else None,
                    int(initial_window['min_submit_budget_seconds']) if initial_window else 15,
                    int(initial_window['min_ticket_probe_budget_seconds']) if initial_window else 10,
                ),
            )
            conn.execute(
                """
                UPDATE timo_recovery_runs
                SET status=?, otp_required=1, otp_requested_at=?, updated_at=?
                WHERE recovery_id=?
                """,
                (recovery_status, now if activate_immediately else '', now, recovery_id),
            )
            row = conn.execute('SELECT * FROM timo_otp_requests WHERE otp_request_id = ?', (otp_request_id,)).fetchone()
        return {'created': True, 'otp_request': dict(row)}

    def activate_otp_request(self, otp_request_id: str) -> Dict[str, Any]:
        otp_request_id = str(otp_request_id or '').strip()
        if not otp_request_id:
            raise ValueError('otp_request_id_required')
        now_dt = utc_now()
        now = now_dt.isoformat().replace('+00:00', 'Z')
        window = _otp_window_budget(now_dt)
        provider_window_seconds = int(window['provider_window_seconds'])
        min_submit_budget_seconds = int(window['min_submit_budget_seconds'])
        min_ticket_probe_budget_seconds = int(window['min_ticket_probe_budget_seconds'])
        window_deadline_text = str(window['window_deadline'])
        submit_deadline_text = str(window['submit_deadline'])
        read_deadline_text = str(window['read_deadline'])
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM timo_otp_requests WHERE otp_request_id=?', (otp_request_id,)).fetchone()
            if not row:
                raise KeyError('otp_request_not_found')
            status = str(row['status'] or '')
            if status == 'queued':
                conn.execute(
                    """
                    UPDATE timo_otp_requests
                    SET status='pending', source='activated_after_timo_send',
                        expires_at=?, otp_requested_at=?, otp_provider_accepted_at=?,
                        otp_window_deadline_at=?, otp_read_deadline_at=?, otp_submit_deadline_at=?,
                        otp_remaining_seconds=?, min_submit_budget_seconds=?,
                        min_ticket_probe_budget_seconds=?, window_abort_reason='', updated_at=?
                    WHERE otp_request_id=? AND status='queued'
                    """,
                    (
                        window_deadline_text, now, now, window_deadline_text,
                        read_deadline_text, submit_deadline_text, provider_window_seconds,
                        min_submit_budget_seconds, min_ticket_probe_budget_seconds,
                        now, otp_request_id,
                    ),
                )
                conn.execute(
                    """
                    UPDATE timo_recovery_runs
                    SET status='otp_request_created', otp_requested_at=?, updated_at=?
                    WHERE recovery_id=?
                    """,
                    (now, now, row['recovery_id']),
                )
                conn.commit()
                row = conn.execute('SELECT * FROM timo_otp_requests WHERE otp_request_id=?', (otp_request_id,)).fetchone()
            elif status == 'reading' and str(row['source'] or '') == 'prearmed_before_timo_send':
                if str(row['delivery_state'] or '') != 'observation_ready':
                    raise RuntimeError('otp_preflight_not_ready')
                conn.execute(
                    """
                    UPDATE timo_otp_requests
                    SET source='activated_after_timo_send_prearmed', expires_at=?,
                        otp_requested_at=?, otp_provider_accepted_at=?,
                        otp_window_deadline_at=?, otp_read_deadline_at=?, otp_submit_deadline_at=?,
                        otp_remaining_seconds=?, min_submit_budget_seconds=?,
                        min_ticket_probe_budget_seconds=?, window_abort_reason='', updated_at=?
                    WHERE otp_request_id=? AND status='reading' AND source='prearmed_before_timo_send'
                    """,
                    (
                        window_deadline_text, now, now, window_deadline_text,
                        read_deadline_text, submit_deadline_text, provider_window_seconds,
                        min_submit_budget_seconds, min_ticket_probe_budget_seconds,
                        now, otp_request_id,
                    ),
                )
                conn.execute(
                    """
                    UPDATE timo_recovery_runs
                    SET status='otp_request_created', otp_requested_at=?, updated_at=?
                    WHERE recovery_id=?
                    """,
                    (now, now, row['recovery_id']),
                )
                conn.commit()
                row = conn.execute('SELECT * FROM timo_otp_requests WHERE otp_request_id=?', (otp_request_id,)).fetchone()
            elif status not in {'pending', 'reading'}:
                raise RuntimeError('otp_request_already_closed')
        return {'ok': True, 'otp_request': self._otp_request_payload(dict(row))}

    def get_otp_request(self, otp_request_id: str) -> Dict[str, Any]:
        otp_request_id = str(otp_request_id or '').strip()
        if not otp_request_id:
            raise ValueError('otp_request_id_required')
        with self.connect() as conn:
            self._expire_stale_otp_requests(conn, iso_now())
            row = conn.execute('SELECT * FROM timo_otp_requests WHERE otp_request_id=?', (otp_request_id,)).fetchone()
            if not row:
                raise KeyError('otp_request_not_found')
        return {'ok': True, 'otp_request': self._otp_request_payload(dict(row))}

    def update_recovery_phase(
        self,
        recovery_id: str,
        *,
        status: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized_id = str(recovery_id or '').strip()
        normalized_status = str(status or '').strip()
        if not normalized_id:
            raise ValueError('recovery_id_required')
        if normalized_status not in RECOVERY_PHASE_STATUSES and normalized_status not in {
            'created', 'otp_request_created', 'otp_received', 'otp_submitting', 'restored', 'failed', 'manual_required',
        }:
            raise ValueError('invalid_recovery_phase')
        now = iso_now()
        with self.connect() as conn:
            row = conn.execute(
                'SELECT * FROM timo_recovery_runs WHERE recovery_id=?',
                (normalized_id,),
            ).fetchone()
            if not row:
                raise KeyError('recovery_run_not_found')
            merged_metadata = _json_dict(row['metadata_json'])
            merged_metadata.update({
                str(key): value
                for key, value in (metadata or {}).items()
                if isinstance(key, str) and isinstance(value, (str, int, float, bool, type(None)))
            })
            phase_timestamp_columns = {
                'otp_l4_consumed': 'otp_l4_consumed_at',
                'browser_submit_accepted': 'browser_submit_accepted_at',
                'ticket_candidate_captured': 'ticket_candidate_captured_at',
                'ticket_probe_passed': 'ticket_probe_passed_at',
                'ticket_persisted': 'ticket_persisted_at',
                'post_persist_probe_passed': 'post_persist_probe_passed_at',
            }
            timestamp_column = phase_timestamp_columns.get(normalized_status)
            timestamp_assignment = f', {timestamp_column}=?' if timestamp_column else ''
            timestamp_params = (now,) if timestamp_column else ()
            conn.execute(
                f"""
                UPDATE timo_recovery_runs
                SET status=?, metadata_json=?, updated_at=?{timestamp_assignment}
                WHERE recovery_id=?
                """,
                (normalized_status, _json_dumps(merged_metadata), now, *timestamp_params, normalized_id),
            )
            if normalized_status in {'restored', 'failed', 'manual_required', 'blocked'}:
                conn.execute(
                    'DELETE FROM timo_guild_operation_locks WHERE operation_id=?',
                    (normalized_id,),
                )
            updated = conn.execute(
                'SELECT * FROM timo_recovery_runs WHERE recovery_id=?',
                (normalized_id,),
            ).fetchone()
        return {'ok': True, 'recovery_run': dict(updated)}

    def _record_delivery_evidence(
        self,
        conn: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        payload: AuthStationOtpResultRequest,
        now: str,
    ) -> Dict[str, Any]:
        evidence = _safe_evidence_summary(payload.evidence_summary)
        evidence_id = f'evidence_{uuid.uuid4().hex[:16]}'
        post_fingerprint = str(evidence.get('post_request_page_fingerprint') or '').strip()
        if not post_fingerprint:
            post_fingerprint = str(evidence.get('page_fingerprint') or '').strip()
        latest_hashes = evidence.get('latest_message_hashes_after')
        if not isinstance(latest_hashes, list):
            latest_hashes = []
        collected_at = str(evidence.get('collected_at') or now)[:80]
        conn.execute(
            """
            INSERT INTO timo_otp_delivery_evidence (
                evidence_id, request_id, recovery_id, guild_id, station_id, device_serial,
                source, phase, page_key, page_fingerprint, latest_message_hashes_json,
                message_count, message_count_delta, notification_count_delta,
                notification_fingerprint, candidate_count, parse_status, parse_miss_reason,
                delivery_confidence_level, final_failure_reason, metadata_json,
                collected_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_id,
                row['otp_request_id'],
                row['recovery_id'],
                row['guild_id'],
                row['station_id'],
                evidence.get('device_serial') or '',
                'auth_station_relay',
                payload.phase or evidence.get('phase') or 'final_summary',
                evidence.get('page_key') or '',
                post_fingerprint,
                _json_dumps({'hashes': latest_hashes}),
                evidence.get('message_count_after'),
                evidence.get('message_count_delta'),
                evidence.get('notification_count_delta'),
                evidence.get('notification_fingerprint') or '',
                int(evidence.get('otp_candidate_count') or 0),
                payload.parse_status or evidence.get('parse_status') or 'not_run',
                payload.parse_miss_reason or evidence.get('parse_miss_reason') or '',
                payload.delivery_confidence_level or evidence.get('delivery_confidence_level') or 'L0',
                payload.final_failure_reason or evidence.get('final_failure_reason') or '',
                _json_dumps(evidence),
                collected_at,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE timo_otp_requests
            SET delivery_state=?, parse_status=?, parse_miss_reason=?,
                delivery_confidence_level=?, final_failure_reason=?, evidence_summary_json=?
            WHERE otp_request_id=?
            """,
            (
                payload.delivery_state or '',
                payload.parse_status or 'not_run',
                payload.parse_miss_reason or '',
                payload.delivery_confidence_level or 'L0',
                payload.final_failure_reason or '',
                _json_dumps(evidence),
                row['otp_request_id'],
            ),
        )
        return {'evidence_id': evidence_id, 'summary': evidence}

    def list_delivery_evidence(self, otp_request_id: str, *, limit: int = 20) -> list[Dict[str, Any]]:
        normalized = str(otp_request_id or '').strip()
        if not normalized:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT evidence_id, request_id, recovery_id, guild_id, station_id, device_serial,
                       source, phase, page_key, page_fingerprint, latest_message_hashes_json,
                       message_count, message_count_delta, notification_count_delta,
                       notification_fingerprint, candidate_count, parse_status, parse_miss_reason,
                       delivery_confidence_level, final_failure_reason, metadata_json,
                       collected_at, created_at
                FROM timo_otp_delivery_evidence
                WHERE request_id=?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (normalized, max(min(int(limit or 20), 100), 1)),
            ).fetchall()
        return [dict(row) for row in rows]

    def delivery_evidence_summary(self, *, guild_id: str = '', hours: int = 24) -> Dict[str, Any]:
        window_hours = max(min(int(hours or 24), 24 * 30), 1)
        cutoff = (utc_now() - timedelta(hours=window_hours)).isoformat().replace('+00:00', 'Z')
        clauses = ['created_at >= ?']
        params: list[Any] = [cutoff]
        if str(guild_id or '').strip():
            clauses.append('guild_id=?')
            params.append(str(guild_id).strip())
        where = ' AND '.join(clauses)
        with self.connect() as conn:
            total = int(conn.execute(f'SELECT COUNT(*) FROM timo_otp_delivery_evidence WHERE {where}', params).fetchone()[0])
            by_failure = [
                {'reason': str(row['final_failure_reason'] or 'unknown'), 'count': int(row['count'])}
                for row in conn.execute(
                    f"SELECT final_failure_reason, COUNT(*) AS count FROM timo_otp_delivery_evidence WHERE {where} GROUP BY final_failure_reason ORDER BY count DESC",
                    params,
                ).fetchall()
            ]
            by_confidence = [
                {'level': str(row['delivery_confidence_level'] or 'L0'), 'count': int(row['count'])}
                for row in conn.execute(
                    f"SELECT delivery_confidence_level, COUNT(*) AS count FROM timo_otp_delivery_evidence WHERE {where} GROUP BY delivery_confidence_level ORDER BY delivery_confidence_level",
                    params,
                ).fetchall()
            ]
            by_phase = [
                {'phase': str(row['phase'] or 'unknown'), 'count': int(row['count'])}
                for row in conn.execute(
                    f"SELECT phase, COUNT(*) AS count FROM timo_otp_delivery_evidence WHERE {where} GROUP BY phase ORDER BY count DESC LIMIT 30",
                    params,
                ).fetchall()
            ]
        return {
            'ok': True,
            'window_hours': window_hours,
            'guild_id': str(guild_id or '').strip(),
            'total_evidence': total,
            'by_failure_reason': by_failure,
            'by_confidence_level': by_confidence,
            'by_phase': by_phase,
            'automatic_consume_threshold': 'L4',
        }

    def upsert_locator_profile(self, payload: AuthStationLocatorProfileRequest) -> Dict[str, Any]:
        status = str(payload.status or 'testing').strip().lower()
        if status not in {'active', 'testing', 'disabled'}:
            raise ValueError('invalid_locator_profile_status')
        now = iso_now()
        profile_id = f'locator_{uuid.uuid4().hex[:16]}'
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT locator_profile_id FROM timo_app_locator_profiles
                WHERE platform=? AND app_version_name=? AND app_version_code=?
                  AND language=? AND brand=? AND model=? AND resolution=? AND orientation=?
                """,
                (
                    payload.platform, payload.app_version_name, payload.app_version_code,
                    payload.language or payload.locale, payload.brand, payload.model,
                    payload.resolution, payload.orientation,
                ),
            ).fetchone()
            if existing:
                profile_id = str(existing['locator_profile_id'])
                conn.execute(
                    """
                    UPDATE timo_app_locator_profiles
                    SET official_assistant_locator_json=?, system_message_tab_locator_json=?,
                        otp_template_profile_id=?, status=?, locale=?, device_resolution_class=?,
                        profile_state=?, updated_at=?
                    WHERE locator_profile_id=?
                    """,
                    (
                        _json_dumps(payload.official_assistant_locator),
                        _json_dumps(payload.system_message_tab_locator),
                        payload.otp_template_profile_id,
                        status,
                        payload.locale,
                        payload.device_resolution_class,
                        'exact_match' if status == 'active' else payload.profile_state,
                        now,
                        profile_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO timo_app_locator_profiles (
                        locator_profile_id, app_version_name, app_version_code, locale,
                        device_resolution_class, official_assistant_locator_json,
                        system_message_tab_locator_json, otp_template_profile_id, status,
                        created_at, updated_at, platform, language, brand, model,
                        resolution, orientation, profile_state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile_id,
                        payload.app_version_name,
                        payload.app_version_code,
                        payload.locale,
                        payload.device_resolution_class,
                        _json_dumps(payload.official_assistant_locator),
                        _json_dumps(payload.system_message_tab_locator),
                        payload.otp_template_profile_id,
                        status,
                        now,
                        now,
                        payload.platform,
                        payload.language or payload.locale,
                        payload.brand,
                        payload.model,
                        payload.resolution,
                        payload.orientation,
                        'exact_match' if status == 'active' else payload.profile_state,
                    ),
                )
            row = conn.execute(
                'SELECT * FROM timo_app_locator_profiles WHERE locator_profile_id=?',
                (profile_id,),
            ).fetchone()
        return {'ok': True, 'profile': dict(row)}

    def list_locator_profiles(self, *, app_version_name: str = '', status: str = '') -> list[Dict[str, Any]]:
        clauses = []
        params: list[str] = []
        if app_version_name:
            clauses.append('app_version_name=?')
            params.append(app_version_name)
        if status:
            clauses.append('status=?')
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ''
        with self.connect() as conn:
            rows = conn.execute(
                f'SELECT * FROM timo_app_locator_profiles {where} ORDER BY updated_at DESC LIMIT 100',
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def _resolve_ready_station_for_guild(
        self,
        conn: sqlite3.Connection,
        *,
        guild_id: str,
        requested_station_id: str = '',
    ) -> str:
        """Resolve the active guild binding before Timo sends an OTP."""
        normalized_guild_id = str(guild_id or '').strip()
        if not normalized_guild_id:
            return ''
        rows = conn.execute(
            """
            SELECT
                b.station_id,
                b.device_serial,
                COALESCE(h.status, s.status, '') AS heartbeat_status,
                COALESCE(h.last_heartbeat_at, s.last_heartbeat_at, '') AS heartbeat_at,
                COALESCE(h.adb_status, s.adb_status, '') AS adb_status,
                COALESCE(h.app_status, s.app_status, '') AS app_status,
                COALESCE(h.device_health, '') AS device_health
            FROM timo_auth_station_device_bindings b
            LEFT JOIN timo_auth_station_device_heartbeats h
              ON h.station_id = b.station_id AND h.device_id = b.device_serial
            LEFT JOIN timo_auth_stations s
              ON s.station_id = b.station_id AND s.device_id = b.device_serial
            WHERE b.status = 'active'
              AND (
                    LOWER(TRIM(COALESCE(b.guild_id, ''))) = LOWER(TRIM(?))
                 OR LOWER(TRIM(COALESCE(b.guild_name, ''))) = LOWER(TRIM(?))
                 OR EXISTS (
                        SELECT 1
                        FROM guild_executors ge
                        WHERE LOWER(COALESCE(ge.app_name, 'linky')) = 'timo'
                          AND (
                                ge.cms_guild_sid = ?
                             OR ge.cms_guild_id = ?
                             OR LOWER(TRIM(ge.guild_name)) = LOWER(TRIM(?))
                          )
                          AND LOWER(TRIM(ge.guild_name)) = LOWER(TRIM(b.guild_name))
                    )
              )
            ORDER BY b.updated_at DESC
            LIMIT 10
            """,
            (normalized_guild_id, normalized_guild_id, normalized_guild_id, normalized_guild_id, normalized_guild_id),
        ).fetchall()
        if not rows:
            return ''

        requested = str(requested_station_id or '').strip()
        if requested:
            rows = [row for row in rows if str(row['station_id'] or '').strip() == requested]
            if not rows:
                raise ValueError('timo_auth_station_binding_mismatch')
        row = rows[0]
        heartbeat_at = parse_iso_datetime(str(row['heartbeat_at'] or ''))
        heartbeat_fresh = bool(heartbeat_at and (utc_now() - heartbeat_at).total_seconds() <= 90)
        ready = (
            str(row['heartbeat_status'] or '').strip().lower() == 'online'
            and heartbeat_fresh
            and str(row['adb_status'] or '').strip().lower() == 'connected'
            and str(row['app_status'] or '').strip().lower() == 'foreground'
            and str(row['device_health'] or '').strip().lower() != 'unhealthy'
        )
        if not ready:
            raise ValueError('timo_auth_station_device_not_ready')
        return str(row['station_id'] or '').strip()

    def next_otp_request(self, station_id: str, *, relay_version: str = '', device_serial: str = '') -> Dict[str, Any]:
        station_id = str(station_id or '').strip()
        device_serial = str(device_serial or '').strip()
        if not station_id:
            raise ValueError('station_id_required')
        relay_version = str(relay_version or '').strip()
        if not _relay_version_can_read_otp(relay_version):
            return {
                'ok': True,
                'has_request': False,
                'active_recovery': False,
                'next_poll_after_ms': 30000,
                'relay_upgrade_required': True,
                'min_relay_version': MIN_RELAY_VERSION_FOR_OTP,
            }
        now = iso_now()
        with self.connect() as conn:
            self._expire_stale_otp_requests(conn, now)
            if device_serial:
                row = conn.execute(
                    """
                    SELECT r.*
                    FROM timo_otp_requests r
                    JOIN guild_executors ge
                      ON LOWER(COALESCE(ge.app_name, 'linky')) = 'timo'
                     AND (
                        r.guild_id = COALESCE(NULLIF(ge.cms_guild_sid, ''), ge.cms_guild_id)
                        OR r.guild_id = ge.cms_guild_sid
                        OR r.guild_id = ge.cms_guild_id
                        OR LOWER(r.guild_id) = LOWER(ge.guild_name)
                     )
                    JOIN timo_auth_station_device_bindings b
                      ON b.status = 'active'
                     AND b.station_id = ?
                     AND b.device_serial = ?
                     AND (
                        LOWER(b.guild_name) = LOWER(ge.guild_name)
                        OR (COALESCE(TRIM(b.guild_id), '') <> '' AND b.guild_id = r.guild_id)
                        OR LOWER(b.guild_name) = LOWER(r.guild_id)
                     )
                    WHERE r.status='pending' AND (r.station_id='' OR r.station_id=?)
                    ORDER BY r.created_at ASC
                    LIMIT 1
                    """,
                    (station_id, device_serial, station_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM timo_otp_requests
                    WHERE
                        status='pending' AND (station_id='' OR station_id=?)
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (station_id,),
                ).fetchone()
            active_recovery = conn.execute(
                """
                SELECT 1 FROM timo_recovery_runs
                WHERE status IN ({})
                LIMIT 1
                """.format(','.join('?' for _ in ACTIVE_RECOVERY_STATUSES)),
                tuple(sorted(ACTIVE_RECOVERY_STATUSES)),
            ).fetchone() is not None
            if not row:
                if device_serial:
                    row = conn.execute(
                        """
                        SELECT r.*
                        FROM timo_otp_requests r
                        JOIN guild_executors ge
                          ON LOWER(COALESCE(ge.app_name, 'linky')) = 'timo'
                         AND (
                            r.guild_id = COALESCE(NULLIF(ge.cms_guild_sid, ''), ge.cms_guild_id)
                            OR r.guild_id = ge.cms_guild_sid
                            OR r.guild_id = ge.cms_guild_id
                            OR LOWER(r.guild_id) = LOWER(ge.guild_name)
                         )
                        JOIN timo_auth_station_device_bindings b
                          ON b.status = 'active'
                         AND b.station_id = ?
                         AND b.device_serial = ?
                         AND (
                            LOWER(b.guild_name) = LOWER(ge.guild_name)
                            OR (COALESCE(TRIM(b.guild_id), '') <> '' AND b.guild_id = r.guild_id)
                            OR LOWER(b.guild_name) = LOWER(r.guild_id)
                         )
                        WHERE r.status='queued' AND (r.station_id='' OR r.station_id=?)
                        ORDER BY r.created_at ASC
                        LIMIT 1
                        """,
                        (station_id, device_serial, station_id),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT * FROM timo_otp_requests
                        WHERE status='queued' AND (station_id='' OR station_id=?)
                        ORDER BY created_at ASC
                        LIMIT 1
                        """,
                        (station_id,),
                    ).fetchone()
                if row:
                    conn.execute(
                        """
                        UPDATE timo_otp_requests
                        SET station_id=?, status='reading', source='prearmed_before_timo_send', updated_at=?
                        WHERE otp_request_id=? AND status='queued'
                        """,
                        (station_id, now, row['otp_request_id']),
                    )
                    row = conn.execute('SELECT * FROM timo_otp_requests WHERE otp_request_id=?', (row['otp_request_id'],)).fetchone()
                    return {
                        'ok': True,
                        'has_request': True,
                        'active_recovery': True,
                        'next_poll_after_ms': 1000,
                        'otp_request': self._otp_request_payload(dict(row)),
                    }
                return {
                    'ok': True,
                    'has_request': False,
                    'active_recovery': active_recovery,
                    'next_poll_after_ms': 2000 if active_recovery else 10000,
                }
            conn.execute(
                """
                UPDATE timo_otp_requests
                SET station_id=?, status='reading', updated_at=?
                WHERE otp_request_id=? AND status='pending'
                """,
                (station_id, now, row['otp_request_id']),
            )
            row = conn.execute('SELECT * FROM timo_otp_requests WHERE otp_request_id=?', (row['otp_request_id'],)).fetchone()
        return {
            'ok': True,
            'has_request': True,
            'active_recovery': True,
            'next_poll_after_ms': 1000,
            'otp_request': self._otp_request_payload(dict(row)),
        }

    def submit_otp_result(self, otp_request_id: str, payload: AuthStationOtpResultRequest) -> Dict[str, Any]:
        otp_request_id = str(otp_request_id or '').strip()
        status = str(payload.status or '').strip()
        if status not in {'preflight_ready', 'otp_received', 'failed', 'manual_required'}:
            raise ValueError('invalid_otp_result_status')
        now = iso_now()
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM timo_otp_requests WHERE otp_request_id=?', (otp_request_id,)).fetchone()
            if not row:
                raise KeyError('otp_request_not_found')
            current_status = str(row['status'] or '')
            if current_status in {'used', 'received', 'failed', 'manual_required', 'expired', 'aborted'}:
                incoming_error = str(payload.error_code or '').strip()[:120]
                primary_error = str(row['error_code'] or '').strip()[:120]
                primary_reason = str(row['final_failure_reason'] or primary_error or current_status).strip()[:120]
                if incoming_error and incoming_error != primary_error:
                    recovery = conn.execute(
                        'SELECT metadata_json FROM timo_recovery_runs WHERE recovery_id=?',
                        (row['recovery_id'],),
                    ).fetchone()
                    metadata = _json_dict(recovery['metadata_json'] if recovery else '{}')
                    metadata.update({
                        'secondary_error_code': incoming_error,
                        'secondary_error_message': str(payload.error_message or incoming_error).strip()[:240],
                        'secondary_error_recorded_at': now,
                    })
                    conn.execute(
                        'UPDATE timo_recovery_runs SET metadata_json=?, updated_at=? WHERE recovery_id=?',
                        (_json_dumps(metadata), now, row['recovery_id']),
                    )
                return {
                    'ok': current_status in {'received', 'used'},
                    'status': current_status,
                    'idempotent_terminal_result': True,
                    'error_code': primary_error,
                    'final_failure_reason': primary_reason,
                    'secondary_error_code': incoming_error if incoming_error != primary_error else '',
                }
            assigned_station = str(row['station_id'] or '').strip()
            if assigned_station and assigned_station != str(payload.station_id or '').strip():
                raise RuntimeError('otp_request_assigned_to_other_station')
            evidence_record = self._record_delivery_evidence(
                conn,
                row=row,
                payload=payload,
                now=now,
            )
            if parse_iso_datetime(row['expires_at']) and parse_iso_datetime(row['expires_at']) < utc_now():
                conn.execute(
                    "UPDATE timo_otp_requests SET status='expired', error_code='otp_request_expired', updated_at=? WHERE otp_request_id=?",
                    (now, otp_request_id),
                )
                conn.execute(
                    "UPDATE timo_recovery_runs SET status='failed', error_code='otp_request_expired', updated_at=? WHERE recovery_id=?",
                    (now, row['recovery_id']),
                )
                conn.execute('DELETE FROM timo_guild_operation_locks WHERE operation_id=?', (row['recovery_id'],))
                conn.commit()
                raise RuntimeError('otp_request_expired')
            if status == 'preflight_ready':
                evidence = _safe_evidence_summary(payload.evidence_summary)
                preflight_ready = bool(
                    current_status == 'reading'
                    and str(row['source'] or '') == 'prearmed_before_timo_send'
                    and str(payload.delivery_state or '') == 'observation_ready'
                    and evidence.get('prearm_page_ready')
                    and evidence.get('official_assistant_present')
                    and evidence.get('system_message_tab_present')
                    and str(evidence.get('device_health') or '').lower() != 'unhealthy'
                )
                if not preflight_ready:
                    raise ValueError('otp_preflight_evidence_not_ready')
                conn.execute(
                    """
                    UPDATE timo_otp_requests
                    SET delivery_state='observation_ready', parse_status=?, parse_miss_reason='',
                        delivery_confidence_level='L2', evidence_summary_json=?, updated_at=?
                    WHERE otp_request_id=? AND status='reading'
                    """,
                    (payload.parse_status or 'no_candidate', _json_dumps(evidence), now, otp_request_id),
                )
                conn.execute(
                    """
                    UPDATE timo_recovery_runs
                    SET status='station_observation_ready', delivery_state='observation_ready',
                        evidence_summary_json=?, error_code='', error_message='', updated_at=?
                    WHERE recovery_id=?
                    """,
                    (_json_dumps(evidence), now, row['recovery_id']),
                )
                return {
                    'ok': True,
                    'status': 'preflight_ready',
                    'delivery_state': 'observation_ready',
                    'evidence_id': evidence_record['evidence_id'],
                }
            if status == 'otp_received':
                confidence_level, can_consume, confidence_reason = _evidence_confidence_gate(payload)
                if not can_consume:
                    error_message = 'Auth Station evidence confidence is below the automatic-consume threshold.'
                    conn.execute(
                        """
                        UPDATE timo_otp_requests
                        SET status='manual_required', source=?, error_code='otp_evidence_confidence_below_l4',
                            error_message=?, final_failure_reason='low_confidence_candidate',
                            delivery_confidence_level=?, updated_at=?
                        WHERE otp_request_id=?
                        """,
                        (payload.source or 'auth_station', error_message, confidence_level, now, otp_request_id),
                    )
                    conn.execute(
                        """
                        UPDATE timo_recovery_runs
                        SET status='manual_required', error_code='otp_evidence_confidence_below_l4',
                            error_message=?, final_failure_reason='low_confidence_candidate',
                            delivery_state='otp_visible', evidence_summary_json=?, updated_at=?
                        WHERE recovery_id=?
                        """,
                        (f'{error_message} reason={confidence_reason}', _json_dumps(_safe_evidence_summary(payload.evidence_summary)), now, row['recovery_id']),
                    )
                    conn.execute('DELETE FROM timo_guild_operation_locks WHERE operation_id=?', (row['recovery_id'],))
                    conn.commit()
                    return {
                        'ok': False,
                        'status': 'manual_required',
                        'error_code': 'otp_evidence_confidence_below_l4',
                        'final_failure_reason': 'low_confidence_candidate',
                        'evidence_id': evidence_record['evidence_id'],
                    }
                normalized_otp = str(payload.otp or '').strip().lower()
                if not OTP_RE.fullmatch(normalized_otp):
                    raise ValueError('invalid_otp_format')
                request_fp = otp_fingerprint(otp_request_id=otp_request_id, otp=normalized_otp)
                code_fp = otp_code_fingerprint(normalized_otp)
                provided_request_fp = str(payload.otp_fingerprint or '').strip()
                provided_code_fp = str(payload.otp_code_fingerprint or '').strip()
                if provided_request_fp and provided_request_fp != request_fp:
                    raise ValueError('otp_fingerprint_mismatch')
                if provided_code_fp and provided_code_fp != code_fp:
                    raise ValueError('otp_code_fingerprint_mismatch')
                duplicate_window_seconds = int(DEFAULT_OTP_EXTRACT_POLICY.get('reject_recent_duplicate_code_seconds') or 0)
                if duplicate_window_seconds > 0:
                    duplicate_cutoff = _iso_minus_seconds(duplicate_window_seconds)
                    duplicate = conn.execute(
                        """
                        SELECT otp_request_id, status, updated_at
                        FROM timo_otp_requests
                        WHERE otp_request_id <> ?
                          AND otp_code_fingerprint = ?
                          AND status IN ('received', 'used')
                          AND updated_at >= ?
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """,
                        (otp_request_id, code_fp, duplicate_cutoff),
                    ).fetchone()
                    if duplicate:
                        conn.execute(
                            """
                            UPDATE timo_otp_requests
                            SET status='failed', otp_fingerprint=?, otp_code_fingerprint=?,
                                received_at=?, source=?, error_code='stale_duplicate_otp',
                                error_message='Auth Station submitted an OTP code recently used by another request; likely stale official-assistant chat content.',
                                updated_at=?
                            WHERE otp_request_id=?
                            """,
                            (request_fp, code_fp, payload.received_at or now, payload.source or 'auth_station', now, otp_request_id),
                        )
                        conn.execute(
                            """
                            UPDATE timo_recovery_runs
                            SET status='failed', error_code='stale_duplicate_otp',
                                error_message='Auth Station submitted an OTP code recently used by another request; likely stale official-assistant chat content.',
                                final_failure_reason='candidate_already_used',
                                delivery_state='otp_visible',
                                updated_at=?
                            WHERE recovery_id=?
                            """,
                            (now, row['recovery_id']),
                        )
                        conn.execute('DELETE FROM timo_guild_operation_locks WHERE operation_id=?', (row['recovery_id'],))
                        conn.commit()
                        return {
                            'ok': False,
                            'status': 'failed',
                            'error_code': 'stale_duplicate_otp',
                            'final_failure_reason': 'candidate_already_used',
                            'evidence_id': evidence_record['evidence_id'],
                        }
                _TRANSIENT_OTP_STORE[otp_request_id] = {
                    'otp': normalized_otp,
                    'expires_at': row['expires_at'],
                    'station_id': str(payload.station_id or '').strip(),
                    'otp_fingerprint': request_fp,
                    'otp_code_fingerprint': code_fp,
                }
                conn.execute(
                    """
                    UPDATE timo_otp_requests
                    SET status='received', otp_fingerprint=?, otp_code_fingerprint=?,
                        received_at=?, source=?, error_code='', error_message='', updated_at=?
                    WHERE otp_request_id=?
                    """,
                    (request_fp, code_fp, payload.received_at or now, payload.source or 'auth_station', now, otp_request_id),
                )
                conn.execute(
                    """
                    UPDATE timo_recovery_runs
                    SET status='otp_received', otp_source=?, otp_received_at=?,
                        delivery_state=?, final_failure_reason='', updated_at=?
                    WHERE recovery_id=?
                    """,
                    (
                        payload.source or 'auth_station',
                        payload.received_at or now,
                        payload.delivery_state or 'otp_visible',
                        now,
                        row['recovery_id'],
                    ),
                )
                return {
                    'ok': True,
                    'status': 'otp_received',
                    'otp_fingerprint': request_fp,
                    'otp_code_fingerprint': code_fp,
                    'evidence_id': evidence_record['evidence_id'],
                }
            failure_reason = str(payload.final_failure_reason or payload.error_code or status).strip()[:120]
            if failure_reason not in OTP_EVIDENCE_FAILURE_REASONS and failure_reason not in {
                'device_locked', 'otp_request_expired', 'otp_trigger_failed', 'station_request_failed',
            }:
                failure_reason = str(payload.error_code or status or 'station_request_failed').strip()[:120]
            error_message = str(payload.error_message or failure_reason).strip()[:500]
            trigger_row = conn.execute(
                'SELECT trigger_reason, guild_id FROM timo_recovery_runs WHERE recovery_id=?',
                (row['recovery_id'],),
            ).fetchone()
            cooldown_seconds = _cooldown_seconds(str(trigger_row['trigger_reason'] or '') if trigger_row else '')
            _canonical_guild_id, _guild_aliases, executor = self._canonical_timo_guild_identity(
                conn,
                str((trigger_row['guild_id'] if trigger_row else '') or row['guild_id'] or ''),
            )
            mx_observation_failure = bool(
                str(executor.get('country') or '').strip().casefold() == 'mexico'
                and failure_reason == 'observation_not_ready'
            )
            if mx_observation_failure:
                cooldown_seconds = max(
                    cooldown_seconds,
                    max(int(os.getenv('TIMO_MX_OBSERVATION_COOLDOWN_SECONDS') or 1800), 60),
                )
            cooldown_until = ''
            if (
                cooldown_seconds
                and failure_reason in OTP_EVIDENCE_FAILURE_REASONS
                and (str(row['otp_requested_at'] or '').strip() or mx_observation_failure)
            ):
                cooldown_until = (utc_now() + timedelta(seconds=cooldown_seconds)).isoformat().replace('+00:00', 'Z')
            conn.execute(
                """
                UPDATE timo_otp_requests
                SET status=?, source=?, error_code=?, error_message=?, final_failure_reason=?,
                    cooldown_until=?, updated_at=?
                WHERE otp_request_id=?
                """,
                (
                    status,
                    payload.source or 'auth_station',
                    failure_reason,
                    error_message,
                    failure_reason,
                    cooldown_until,
                    now,
                    otp_request_id,
                ),
            )
            conn.execute('DELETE FROM timo_guild_operation_locks WHERE operation_id=?', (row['recovery_id'],))
            conn.execute(
                """
                UPDATE timo_recovery_runs
                SET status=?, error_code=?, error_message=?, final_failure_reason=?,
                    delivery_state=?, cooldown_until=?, evidence_summary_json=?, updated_at=?
                WHERE recovery_id=?
                """,
                (
                    'manual_required' if status == 'manual_required' else 'failed',
                    failure_reason,
                    error_message,
                    failure_reason,
                    payload.delivery_state or 'otp_not_visible',
                    cooldown_until,
                    _json_dumps(_safe_evidence_summary(payload.evidence_summary)),
                    now,
                    row['recovery_id'],
                ),
            )
        return {
            'ok': True,
            'status': status,
            'evidence_id': evidence_record['evidence_id'],
            'final_failure_reason': failure_reason,
        }

    def consume_otp(self, otp_request_id: str) -> Dict[str, Any]:
        otp_request_id = str(otp_request_id or '').strip()
        now = iso_now()
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM timo_otp_requests WHERE otp_request_id=?', (otp_request_id,)).fetchone()
            if not row:
                raise KeyError('otp_request_not_found')
            if str(row['status'] or '') == 'used':
                raise RuntimeError('otp_request_already_used')
            if str(row['status'] or '') != 'received':
                raise RuntimeError('otp_request_not_received')
            if parse_iso_datetime(row['expires_at']) and parse_iso_datetime(row['expires_at']) < utc_now():
                conn.execute(
                    "UPDATE timo_otp_requests SET status='expired', error_code='otp_request_expired', updated_at=? WHERE otp_request_id=?",
                    (now, otp_request_id),
                )
                _TRANSIENT_OTP_STORE.pop(otp_request_id, None)
                conn.commit()
                raise RuntimeError('otp_request_expired')
            window_deadline = parse_iso_datetime(row['otp_window_deadline_at'])
            remaining_seconds = int((window_deadline - utc_now()).total_seconds()) if window_deadline else 0
            required_seconds = int(row['min_submit_budget_seconds'] or 15) + int(row['min_ticket_probe_budget_seconds'] or 10)
            if not window_deadline or remaining_seconds < required_seconds:
                abort_reason = 'otp_window_budget_exhausted'
                conn.execute(
                    """
                    UPDATE timo_otp_requests
                    SET status='aborted', otp_remaining_seconds=?, window_abort_reason=?,
                        error_code='abort_window_exhausted', final_failure_reason=?, updated_at=?
                    WHERE otp_request_id=?
                    """,
                    (max(remaining_seconds, 0), abort_reason, abort_reason, now, otp_request_id),
                )
                conn.execute(
                    """
                    UPDATE timo_recovery_runs
                    SET status='failed', error_code='abort_window_exhausted',
                        error_message='OTP window does not have enough submit and ticket-probe budget.',
                        final_failure_reason=?, updated_at=?
                    WHERE recovery_id=?
                    """,
                    (abort_reason, now, row['recovery_id']),
                )
                conn.execute('DELETE FROM timo_guild_operation_locks WHERE operation_id=?', (row['recovery_id'],))
                _TRANSIENT_OTP_STORE.pop(otp_request_id, None)
                conn.commit()
                raise RuntimeError('abort_window_exhausted')
            transient = _TRANSIENT_OTP_STORE.pop(otp_request_id, None)
            if not transient or not transient.get('otp'):
                raise RuntimeError('otp_plaintext_not_available')
            conn.execute(
                """
                UPDATE timo_otp_requests
                SET status='used', used_at=?, otp_remaining_seconds=?, updated_at=?
                WHERE otp_request_id=?
                """,
                (now, max(remaining_seconds, 0), now, otp_request_id),
            )
            conn.execute(
                """
                UPDATE timo_recovery_runs
                SET status='otp_l4_consumed', otp_submitted_at=?, otp_l4_consumed_at=?, updated_at=?
                WHERE recovery_id=?
                """,
                (now, now, now, row['recovery_id']),
            )
        return {
            'ok': True,
            'otp': transient['otp'],
            'otp_fingerprint': transient.get('otp_fingerprint') or row['otp_fingerprint'],
            'otp_code_fingerprint': transient.get('otp_code_fingerprint') or row['otp_code_fingerprint'],
        }

    @staticmethod
    def _ensure_drift_test_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS timo_auth_station_drift_tests (
                test_id TEXT PRIMARY KEY,
                station_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                scenarios_json TEXT NOT NULL DEFAULT '[]',
                rounds INTEGER NOT NULL DEFAULT 1,
                step_index INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                current_scenario TEXT NOT NULL DEFAULT '',
                applied_at TEXT NOT NULL DEFAULT '',
                recovered_at TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '[]',
                error_code TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    def create_drift_test(self, payload: AuthStationDriftTestRequest) -> Dict[str, Any]:
        allowed = {'launcher', 'other_app', 'force_stop', 'screen_off', 'adb_reconnect', 'relay_restart', 'phone_reboot'}
        requested = [str(item or '').strip().lower() for item in payload.scenarios]
        scenarios = [item for item in requested if item in allowed]
        if not scenarios or scenarios != requested:
            raise ValueError('unsupported_drift_test_scenario')
        expanded = scenarios * int(payload.rounds)
        now = iso_now()
        test_id = f'drift_{uuid.uuid4().hex[:16]}'
        with self.connect() as conn:
            self._ensure_drift_test_schema(conn)
            active = conn.execute(
                """
                SELECT test_id FROM timo_auth_station_drift_tests
                WHERE station_id=? AND device_id=? AND status IN ('pending', 'recovering')
                LIMIT 1
                """,
                (payload.station_id, payload.device_id),
            ).fetchone()
            if active:
                raise RuntimeError('drift_test_already_active')
            conn.execute(
                """
                INSERT INTO timo_auth_station_drift_tests (
                    test_id, station_id, device_id, scenarios_json, rounds, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (test_id, payload.station_id, payload.device_id, _json_dumps(expanded), payload.rounds, now, now),
            )
        return {'ok': True, 'test_id': test_id, 'scenario_count': len(expanded), 'status': 'pending'}

    def next_drift_test_command(self, *, station_id: str, device_id: str) -> Dict[str, Any]:
        now = iso_now()
        with self.connect() as conn:
            self._ensure_drift_test_schema(conn)
            self._ensure_device_heartbeat_schema(conn)
            row = conn.execute(
                """
                SELECT * FROM timo_auth_station_drift_tests
                WHERE station_id=? AND device_id=? AND status IN ('pending', 'recovering')
                ORDER BY created_at LIMIT 1
                """,
                (station_id, device_id),
            ).fetchone()
            if not row:
                return {'ok': True, 'has_command': False}
            current = dict(row)
            scenarios = list(_json_list(current.get('scenarios_json')))
            index = int(current.get('step_index') or 0)
            results = list(_json_list(current.get('result_json')))
            if current.get('status') == 'recovering':
                heartbeat = conn.execute(
                    """
                    SELECT observation_ready, observation_ready_at, last_heartbeat_at,
                           last_page_fingerprint, page_status, timo_package_name
                    FROM timo_auth_station_device_heartbeats
                    WHERE station_id=? AND device_id=?
                    """,
                    (station_id, device_id),
                ).fetchone()
                ready_at = str(heartbeat['observation_ready_at'] or heartbeat['last_heartbeat_at'] or '') if heartbeat else ''
                if heartbeat and int(heartbeat['observation_ready'] or 0) == 1 and ready_at > str(current.get('applied_at') or ''):
                    applied = parse_iso_datetime(str(current.get('applied_at') or ''))
                    recovered = parse_iso_datetime(ready_at)
                    duration = round((recovered - applied).total_seconds(), 3) if applied and recovered else None
                    results.append({
                        'scenario': current.get('current_scenario') or '',
                        'duration_seconds': duration,
                        'page_fingerprint': str(heartbeat['last_page_fingerprint'] or '')[:120],
                        'page_status': str(heartbeat['page_status'] or '')[:80],
                        'timo_package': str(heartbeat['timo_package_name'] or '')[:120],
                        'status': 'passed',
                    })
                    index += 1
                    if index >= len(scenarios):
                        conn.execute(
                            """
                            UPDATE timo_auth_station_drift_tests
                            SET step_index=?, status='succeeded', recovered_at=?, result_json=?, updated_at=?
                            WHERE test_id=?
                            """,
                            (index, ready_at, _json_dumps(results), now, current['test_id']),
                        )
                        return {'ok': True, 'has_command': False, 'test_completed': True, 'test_id': current['test_id']}
                    conn.execute(
                        """
                        UPDATE timo_auth_station_drift_tests
                        SET step_index=?, status='pending', current_scenario='', recovered_at=?, result_json=?, updated_at=?
                        WHERE test_id=?
                        """,
                        (index, ready_at, _json_dumps(results), now, current['test_id']),
                    )
                else:
                    return {'ok': True, 'has_command': False, 'recovering': True, 'test_id': current['test_id']}
            scenario = str(scenarios[index] or '')
            return {
                'ok': True,
                'has_command': True,
                'test_id': current['test_id'],
                'scenario': scenario,
                'step_index': index,
                'scenario_count': len(scenarios),
            }

    def report_drift_test_event(self, test_id: str, payload: AuthStationDriftTestEventRequest) -> Dict[str, Any]:
        now = iso_now()
        with self.connect() as conn:
            self._ensure_drift_test_schema(conn)
            row = conn.execute('SELECT * FROM timo_auth_station_drift_tests WHERE test_id=?', (test_id,)).fetchone()
            if not row:
                raise KeyError('drift_test_not_found')
            if str(row['station_id']) != payload.station_id or str(row['device_id']) != payload.device_id:
                raise ValueError('drift_test_device_mismatch')
            event = str(payload.event or '').strip().lower()
            if event == 'applied':
                conn.execute(
                    """
                    UPDATE timo_auth_station_drift_tests
                    SET status='recovering', current_scenario=?, applied_at=?, error_code='', updated_at=?
                    WHERE test_id=?
                    """,
                    (payload.scenario, now, now, test_id),
                )
            elif event == 'failed':
                conn.execute(
                    """
                    UPDATE timo_auth_station_drift_tests
                    SET status='failed', current_scenario=?, error_code=?, updated_at=?
                    WHERE test_id=?
                    """,
                    (payload.scenario, str(payload.details.get('error_code') or 'drift_apply_failed')[:80], now, test_id),
                )
            else:
                raise ValueError('unsupported_drift_test_event')
        return {'ok': True, 'test_id': test_id, 'event': event}

    def get_drift_test(self, test_id: str) -> Dict[str, Any]:
        with self.connect() as conn:
            self._ensure_drift_test_schema(conn)
            row = conn.execute('SELECT * FROM timo_auth_station_drift_tests WHERE test_id=?', (test_id,)).fetchone()
        if not row:
            raise KeyError('drift_test_not_found')
        payload = dict(row)
        payload['scenarios'] = _json_list(payload.pop('scenarios_json', '[]'))
        payload['results'] = _json_list(payload.pop('result_json', '[]'))
        return {'ok': True, 'drift_test': payload}

    @staticmethod
    def _otp_request_payload(row: Dict[str, Any]) -> Dict[str, Any]:
        source = str(row.get('source') or '')
        provider_window_started_at = row.get('created_at')
        if source.startswith('activated_after_timo_send'):
            provider_window_started_at = row.get('otp_provider_accepted_at') or row.get('updated_at') or row.get('created_at')
        return {
            'otp_request_id': row.get('otp_request_id'),
            'recovery_id': row.get('recovery_id'),
            'guild_id': row.get('guild_id'),
            'account_fingerprint': row.get('account_fingerprint'),
            'status': row.get('status') or '',
            'created_at': row.get('created_at'),
            'expires_at': row.get('expires_at'),
            'station_id': row.get('station_id'),
            'source': source,
            'prearm_required': row.get('status') == 'reading' and source == 'prearmed_before_timo_send',
            'skip_pre_request_baseline': source.startswith('activated_after_timo_send'),
            'provider_window_started_at': provider_window_started_at,
            'delivery_state': row.get('delivery_state') or '',
            'parse_status': row.get('parse_status') or 'not_run',
            'parse_miss_reason': row.get('parse_miss_reason') or '',
            'delivery_confidence_level': row.get('delivery_confidence_level') or 'L0',
            'final_failure_reason': row.get('final_failure_reason') or '',
            'otp_requested_at': row.get('otp_requested_at') or '',
            'otp_provider_accepted_at': row.get('otp_provider_accepted_at') or '',
            'otp_window_deadline_at': row.get('otp_window_deadline_at') or '',
            'otp_read_deadline_at': row.get('otp_read_deadline_at') or '',
            'otp_submit_deadline_at': row.get('otp_submit_deadline_at') or '',
            'otp_remaining_seconds': (
                max(0, int((parse_iso_datetime(row.get('otp_window_deadline_at')) - utc_now()).total_seconds()))
                if parse_iso_datetime(row.get('otp_window_deadline_at'))
                else row.get('otp_remaining_seconds')
            ),
            'min_submit_budget_seconds': row.get('min_submit_budget_seconds') or 15,
            'min_ticket_probe_budget_seconds': row.get('min_ticket_probe_budget_seconds') or 10,
            'window_abort_reason': row.get('window_abort_reason') or '',
            'evidence_summary': _safe_evidence_summary(_json_dict(row.get('evidence_summary_json'))),
            'expected_code_format': 'alnum_5',
            'preferred_source': 'timo_official_assistant_system_message',
            'extract_policy': dict(DEFAULT_OTP_EXTRACT_POLICY),
        }


def validate_station_token(expected_token: str, provided_token: str, *, require_configured: bool = False) -> None:
    expected = str(expected_token or '').strip()
    if not expected:
        if require_configured:
            raise HTTPException(status_code=503, detail='timo_auth_station_token_not_configured')
        return
    provided = str(provided_token or '').strip()
    if not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=403, detail='timo_auth_station_token_required')


def create_timo_auth_station_router(*, db_path: str, station_token: str = '') -> APIRouter:
    router = APIRouter(prefix='/api/internal/timo/auth-station', tags=['timo-auth-station'])
    service = TimoAuthStationService(db_path)

    @router.post('/heartbeat')
    def heartbeat(payload: AuthStationHeartbeatRequest, x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token)
        return service.heartbeat(payload)

    @router.get('/otp-requests/next')
    def next_otp_request(station_id: str, relay_version: str = '', device_serial: str = '', x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token)
        try:
            return service.next_otp_request(station_id, relay_version=relay_version, device_serial=device_serial)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post('/drift-tests')
    def create_drift_test(payload: AuthStationDriftTestRequest, x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token)
        try:
            return service.create_drift_test(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get('/drift-tests/{test_id}')
    def get_drift_test(test_id: str, x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token)
        try:
            return service.get_drift_test(test_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post('/otp-requests/{otp_request_id}/result')
    def otp_result(otp_request_id: str, payload: AuthStationOtpResultRequest, x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token)
        try:
            return service.submit_otp_result(otp_request_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post('/recovery-runs')
    def create_recovery_run(payload: AuthStationRecoveryRunRequest):
        try:
            return service.create_recovery_run(
                guild_id=payload.guild_id,
                account_fingerprint=payload.account_fingerprint,
                trigger_reason=payload.trigger_reason,
                metadata=payload.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post('/recovery-runs/{recovery_id}/otp-requests')
    def create_otp_request(recovery_id: str, payload: AuthStationCreateOtpRequest):
        try:
            return service.create_otp_request(
                recovery_id=recovery_id,
                guild_id=payload.guild_id,
                account_fingerprint=payload.account_fingerprint,
                station_id=payload.station_id,
                ttl_seconds=payload.ttl_seconds,
                request_channel=payload.request_channel,
                activate_immediately=payload.activate_immediately,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post('/otp-requests/{otp_request_id}/activate')
    def activate_otp_request(otp_request_id: str):
        try:
            return service.activate_otp_request(otp_request_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get('/otp-requests/{otp_request_id}')
    def get_otp_request(otp_request_id: str):
        try:
            return service.get_otp_request(otp_request_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post('/recovery-runs/{recovery_id}/phase')
    def update_recovery_phase(recovery_id: str, payload: AuthStationRecoveryPhaseRequest):
        try:
            return service.update_recovery_phase(
                recovery_id,
                status=payload.status,
                metadata=payload.metadata,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post('/otp-requests/{otp_request_id}/consume')
    def consume_otp_request(otp_request_id: str):
        try:
            return service.consume_otp(otp_request_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get('/otp-requests/{otp_request_id}/evidence')
    def otp_evidence(otp_request_id: str, limit: int = 20, x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token)
        return {'ok': True, 'evidence': service.list_delivery_evidence(otp_request_id, limit=limit)}

    @router.get('/evidence/summary')
    def evidence_summary(guild_id: str = '', hours: int = 24, x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token)
        return service.delivery_evidence_summary(guild_id=guild_id, hours=hours)

    @router.get('/locator-profiles')
    def locator_profiles(app_version_name: str = '', status: str = '', x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token)
        return {'ok': True, 'profiles': service.list_locator_profiles(app_version_name=app_version_name, status=status)}

    @router.post('/locator-profiles')
    def upsert_locator_profile(payload: AuthStationLocatorProfileRequest, x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token)
        try:
            return service.upsert_locator_profile(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get('/status')
    def status(x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token)
        stations = service.status_station_rows()
        with service.connect() as conn:
            recoveries = [dict(row) for row in conn.execute('SELECT * FROM timo_recovery_runs ORDER BY updated_at DESC LIMIT 20').fetchall()]
            otp_requests = [dict(row) for row in conn.execute('SELECT otp_request_id, recovery_id, guild_id, station_id, status, request_channel, otp_fingerprint, otp_code_fingerprint, expires_at, received_at, used_at, source, error_code, error_message, delivery_state, parse_status, parse_miss_reason, delivery_confidence_level, final_failure_reason, cooldown_until, evidence_summary_json, created_at, updated_at FROM timo_otp_requests ORDER BY updated_at DESC LIMIT 20').fetchall()]
        return {'ok': True, 'stations': stations, 'recoveries': recoveries, 'otp_requests': otp_requests}

    return router


def create_timo_auth_station_public_router(*, db_path: str, station_token: str = '') -> APIRouter:
    router = APIRouter(prefix='/api/timo/auth-station', tags=['timo-auth-station-public'])
    service = TimoAuthStationService(db_path)

    @router.post('/heartbeat')
    def heartbeat(payload: AuthStationHeartbeatRequest, x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token, require_configured=True)
        return service.heartbeat(payload)

    @router.get('/otp-requests/next')
    def next_otp_request(station_id: str, relay_version: str = '', device_serial: str = '', x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token, require_configured=True)
        try:
            return service.next_otp_request(station_id, relay_version=relay_version, device_serial=device_serial)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get('/drift-tests/next-command')
    def next_drift_test_command(station_id: str, device_serial: str, x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token, require_configured=True)
        return service.next_drift_test_command(station_id=station_id, device_id=device_serial)

    @router.post('/drift-tests/{test_id}/events')
    def report_drift_test_event(test_id: str, payload: AuthStationDriftTestEventRequest, x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token, require_configured=True)
        try:
            return service.report_drift_test_event(test_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post('/drift-tests')
    def create_public_drift_test(payload: AuthStationDriftTestRequest, x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token, require_configured=True)
        try:
            return service.create_drift_test(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get('/drift-tests/{test_id}')
    def get_public_drift_test(test_id: str, x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token, require_configured=True)
        try:
            return service.get_drift_test(test_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post('/otp-requests/{otp_request_id}/result')
    def otp_result(otp_request_id: str, payload: AuthStationOtpResultRequest, x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token, require_configured=True)
        try:
            return service.submit_otp_result(otp_request_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get('/otp-requests/{otp_request_id}')
    def get_otp_request(otp_request_id: str, x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token, require_configured=True)
        try:
            return service.get_otp_request(otp_request_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get('/otp-requests/{otp_request_id}/evidence')
    def otp_evidence(otp_request_id: str, limit: int = 20, x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token, require_configured=True)
        return {'ok': True, 'evidence': service.list_delivery_evidence(otp_request_id, limit=limit)}

    @router.get('/evidence/summary')
    def evidence_summary(guild_id: str = '', hours: int = 24, x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token, require_configured=True)
        return service.delivery_evidence_summary(guild_id=guild_id, hours=hours)

    @router.get('/locator-profiles')
    def locator_profiles(app_version_name: str = '', status: str = '', x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token, require_configured=True)
        return {'ok': True, 'profiles': service.list_locator_profiles(app_version_name=app_version_name, status=status)}

    @router.post('/locator-profiles')
    def upsert_locator_profile(payload: AuthStationLocatorProfileRequest, x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token, require_configured=True)
        try:
            return service.upsert_locator_profile(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get('/status')
    def status(x_timo_auth_station_token: str = Header(default='')):
        validate_station_token(station_token, x_timo_auth_station_token, require_configured=True)
        stations = service.status_station_rows()
        with service.connect() as conn:
            bindings = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM timo_auth_station_device_bindings WHERE status='active' ORDER BY updated_at DESC LIMIT 50"
                ).fetchall()
            ]
        return {'ok': True, 'stations': stations, 'device_bindings': bindings}

    return router
