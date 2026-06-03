from __future__ import annotations

import json
import threading
import time
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app.main as app_main
from app.main import Database, Service
from app.whatsapp_login_state import map_whatsapp_login_state


def test_logged_in_state_allows_probe_and_hides_qr():
    result = map_whatsapp_login_state(
        runtime_state={'active': True, 'status': 'warm'},
        session_state={
            'login_verified': True,
            'authenticated': True,
            'ready': True,
            'login_check_status': 'passed',
            'qr_available': False,
        },
    )

    assert result['login_state'] == 'logged_in'
    assert result['can_probe'] is True
    assert result['can_show_qr'] is False
    assert result['should_auto_rebuild'] is False


def test_waiting_for_scan_qr_ready_shows_qr_but_does_not_probe_or_rebuild():
    result = map_whatsapp_login_state(
        runtime_state={'active': True, 'status': 'warm'},
        session_state={
            'login_verified': False,
            'authenticated': False,
            'ready': False,
            'login_check_status': 'waiting_for_scan',
            'qr_available': True,
            'qr_text': 'QRDATA',
        },
    )

    assert result['login_state'] == 'waiting_for_scan_qr_ready'
    assert result['can_show_qr'] is True
    assert result['can_probe'] is False
    assert result['should_auto_rebuild'] is False


def test_waiting_for_scan_without_qr_is_qr_pending_not_rebuildable():
    result = map_whatsapp_login_state(
        runtime_state={'active': True, 'status': 'initializing'},
        session_state={
            'login_verified': False,
            'authenticated': False,
            'ready': False,
            'login_check_status': 'waiting_for_scan',
            'qr_available': False,
        },
    )

    assert result['login_state'] == 'waiting_for_scan_qr_pending'
    assert result['can_show_qr'] is False
    assert result['can_probe'] is False
    assert result['should_auto_rebuild'] is False
    assert '二维码生成中' in result['login_state_label']


def test_initializing_has_grace_then_becomes_login_failed_after_timeout():
    recent = datetime.now(timezone.utc) - timedelta(seconds=30)
    old = datetime.now(timezone.utc) - timedelta(seconds=180)

    fresh = map_whatsapp_login_state(
        runtime_state={'active': True, 'status': 'initializing', 'started_at': recent.isoformat()},
        session_state={'login_verified': False, 'authenticated': False, 'ready': False, 'qr_available': False},
        max_initializing_seconds=120,
    )
    expired = map_whatsapp_login_state(
        runtime_state={'active': True, 'status': 'initializing', 'started_at': old.isoformat()},
        session_state={'login_verified': False, 'authenticated': False, 'ready': False, 'qr_available': False},
        max_initializing_seconds=120,
    )

    assert fresh['login_state'] == 'initializing'
    assert fresh['should_auto_rebuild'] is False
    assert expired['login_state'] == 'login_failed'
    assert expired['should_auto_rebuild'] is False


def test_runtime_unhealthy_is_only_state_that_requests_rebuild():
    result = map_whatsapp_login_state(
        runtime_state={'active': False, 'status': 'stopped', 'configured': True, 'health_error': 'connection refused'},
        session_state={'login_verified': False, 'login_check_status': 'runtime_unavailable'},
    )

    assert result['login_state'] == 'runtime_unhealthy'
    assert result['can_probe'] is False
    assert result['should_auto_rebuild'] is True


def test_lightweight_account_list_hides_active_binding_operation(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO whatsapp_approval_accounts (
                account_key, account_name, responsible_type, group_links, area, notify_profile_name,
                approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker,
                schedule_windows, enabled, verification_status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'registration-op-1', '+639****0002', 'registration_group',
                json.dumps([{'link': 'https://chat.whatsapp.com/OPERATION12345', 'area': 'Indonesia', 'notify_profile_name': 'wa-approval-broadcast', 'enabled': True}]),
                'Indonesia', 'wa-approval-broadcast', 'threshold_or_timeout', 100, 200, 1,
                json.dumps([]), 1, 'pending_verification', datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

    monkeypatch.setattr(service, '_production_ops_daemon_snapshot', lambda: {'config': {'enabled': True}, 'runtime': {'status': {}}})
    monkeypatch.setattr(service, '_list_notify_robot_options', lambda: [])
    monkeypatch.setattr(service, '_list_customer_service_options', lambda: [])
    monkeypatch.setattr(service, 'list_whatsapp_approval_area_options', lambda: {'options': [], 'source_options': []})
    monkeypatch.setattr(
        service,
        '_build_whatsapp_approval_runtime_state',
        lambda *args, **kwargs: {
            'account_key': 'registration-op-1',
            'configured': True,
            'active': True,
            'status': 'running',
            'source': 'dedicated',
            'base_url': 'http://127.0.0.1:59999',
            'started_at': datetime.now(timezone.utc).isoformat(),
        },
    )

    service._mark_whatsapp_binding_operation_started(
        'registration-op-1',
        0,
        operation='full_sync',
        detail='正在执行完整同步',
        stage_code='worker_sync',
        stage_label='同步审批队列',
        request_id='approval_op_test_001',
    )
    try:
        payload = service.list_whatsapp_approval_accounts(lightweight=True)
    finally:
        service._clear_whatsapp_binding_operation('registration-op-1', 0)

    binding = payload['rows'][0]['group_binding_runtimes'][0]
    assert 'operation_state' not in binding


def test_lightweight_account_list_does_not_call_worker_health_or_live_probe(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO whatsapp_approval_accounts (
                account_key, account_name, responsible_type, group_links, area, notify_profile_name,
                approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker,
                schedule_windows, enabled, verification_status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'registration-test-1', '+639****0001', 'registration_group',
                json.dumps([{'link': 'https://chat.whatsapp.com/ABCDEFG12345', 'area': 'Indonesia', 'notify_profile_name': 'wa-approval-broadcast', 'enabled': True}]),
                'Indonesia', 'wa-approval-broadcast', 'threshold_or_timeout', 100, 200, 1,
                json.dumps([]), 1, 'pending_verification', datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

    monkeypatch.setattr(service, '_production_ops_daemon_snapshot', lambda: {'config': {'enabled': True}, 'runtime': {'status': {}}})
    monkeypatch.setattr(service, '_list_notify_robot_options', lambda: [])
    monkeypatch.setattr(service, '_list_customer_service_options', lambda: [])
    monkeypatch.setattr(service, 'list_whatsapp_approval_area_options', lambda: {'options': [], 'source_options': []})
    monkeypatch.setattr(
        service,
        '_build_whatsapp_approval_runtime_state',
        lambda *args, **kwargs: {
            'account_key': 'registration-test-1',
            'configured': True,
            'active': True,
            'status': 'running',
            'source': 'dedicated',
            'base_url': 'http://127.0.0.1:59999',
            'started_at': datetime.now(timezone.utc).isoformat(),
        },
    )
    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', lambda *a, **k: (_ for _ in ()).throw(AssertionError('worker health must not be called')))
    monkeypatch.setattr(service, '_current_whatsapp_approval_worker_health', lambda *a, **k: (_ for _ in ()).throw(AssertionError('shared worker health must not be called')))
    monkeypatch.setattr(service, '_apply_live_group_identity_to_binding', lambda *a, **k: (_ for _ in ()).throw(AssertionError('live probe must not be called')))

    payload = service.list_whatsapp_approval_accounts(lightweight=True)

    assert payload['list_mode'] == 'lightweight'
    assert payload['rows'][0]['list_mode'] == 'lightweight'
    assert payload['rows'][0]['session_state']['login_state'] in {'initializing', 'runtime_starting'}
    assert payload['rows'][0]['verification_checks'][0]['code'] == 'group_link_format'
    assert payload['rows'][0]['verification_checks'][0]['ok'] is True
    assert payload['rows'][0]['verification_status'] != 'invalid_group_links'
    assert payload['rows'][0]['status_text'] == '待登录'
    assert payload['rows'][0]['runtime_status'] == 'blocked'



def test_lightweight_account_list_uses_cached_logged_in_session_without_worker_health(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO whatsapp_approval_accounts (
                account_key, account_name, responsible_type, group_links, area, notify_profile_name,
                approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker,
                schedule_windows, enabled, verification_status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'registration-test-cached', '+639****0003', 'registration_group',
                json.dumps([{'link': 'https://chat.whatsapp.com/CACHED12345', 'area': 'Indonesia', 'enabled': True}]),
                'Indonesia', 'wa-approval-broadcast', 'threshold_or_timeout', 100, 200, 1,
                json.dumps([]), 1, 'pending_verification', datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

    monkeypatch.setattr(service, '_production_ops_daemon_snapshot', lambda: {'config': {'enabled': True}, 'runtime': {'status': {}}})
    monkeypatch.setattr(service, '_list_notify_robot_options', lambda: [])
    monkeypatch.setattr(service, '_list_customer_service_options', lambda: [])
    monkeypatch.setattr(service, 'list_whatsapp_approval_area_options', lambda: {'options': [], 'source_options': []})
    monkeypatch.setattr(
        service,
        '_build_whatsapp_approval_runtime_state',
        lambda *args, **kwargs: {
            'account_key': 'registration-test-cached',
            'configured': True,
            'active': True,
            'status': 'running',
            'source': 'dedicated',
            'base_url': 'http://127.0.0.1:59998',
            'started_at': datetime.now(timezone.utc).isoformat(),
        },
    )
    monkeypatch.setattr(service, '_cached_whatsapp_approval_session_snapshot', lambda *a, **k: {
        'account_key': 'registration-test-cached',
        'status': 'warm',
        'ready': True,
        'authenticated': True,
        'login_verified': True,
        'login_check_status': 'passed',
        'login_state': 'logged_in',
        'can_probe': True,
        'can_show_qr': False,
        'should_auto_rebuild': False,
        'from_cached_session': True,
    })
    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', lambda *a, **k: (_ for _ in ()).throw(AssertionError('worker health must not be called')))
    monkeypatch.setattr(service, '_current_whatsapp_approval_worker_health', lambda *a, **k: (_ for _ in ()).throw(AssertionError('shared worker health must not be called')))

    payload = service.list_whatsapp_approval_accounts(lightweight=True)
    row = payload['rows'][0]

    assert row['session_state']['login_state'] == 'logged_in'
    assert row['session_state']['login_verified'] is True
    assert row['session_state']['from_cached_session'] is True
    assert row['session_state']['can_probe'] is True


def test_lightweight_registration_account_list_defaults_provider_mode_to_baileys(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db, account_key='registration-provider-default')
    _patch_lightweight_account_dependencies(monkeypatch, service, account_key='registration-provider-default')

    payload = service.list_whatsapp_approval_accounts(lightweight=True)
    row = payload['rows'][0]
    binding = row['group_binding_runtimes'][0]

    assert row['provider_name'] == 'baileys'
    assert row['provider_mode'] == 'baileys_primary'
    assert row['provider_decision']['provider_name'] == 'baileys'
    assert binding['provider_name'] == 'baileys'
    assert binding['provider_mode'] == 'baileys_primary'
    assert binding['provider_decision']['provider_name'] == 'baileys'
    assert binding['provider_capabilities']['authoritative_read'] is True
    assert binding['provider_capabilities']['manual_approve'] is True


def test_lightweight_logged_in_account_outside_schedule_still_monitors_without_live_probe(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO whatsapp_approval_accounts (
                account_key, account_name, responsible_type, group_links, area, notify_profile_name,
                approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker,
                schedule_windows, enabled, verification_status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'registration-standby', '+639****0005', 'registration_group',
                json.dumps([{
                    'link': 'https://chat.whatsapp.com/STANDBY12345',
                    'area': 'Indonesia',
                    'enabled': True,
                    'schedule_windows': [{'start': '09:00', 'end': '10:00'}],
                }]),
                'Indonesia', 'wa-approval-broadcast', 'threshold_or_timeout', 100, 200, 1,
                json.dumps([]), 1, 'pending_verification', datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

    monkeypatch.setattr(service, '_current_local_minutes', lambda: 12 * 60)
    monkeypatch.setattr(service, '_production_ops_daemon_snapshot', lambda: {'config': {'enabled': True}, 'runtime': {'status': {}, 'launch_agent_installed': True}})
    monkeypatch.setattr(service, '_list_notify_robot_options', lambda: [])
    monkeypatch.setattr(service, '_list_customer_service_options', lambda: [])
    monkeypatch.setattr(service, 'list_whatsapp_approval_area_options', lambda: {'options': [], 'source_options': []})
    monkeypatch.setattr(
        service,
        '_build_whatsapp_approval_runtime_state',
        lambda *args, **kwargs: {
            'account_key': 'registration-standby',
            'configured': True,
            'active': True,
            'ready': True,
            'authenticated': True,
            'login_verified': True,
            'status': 'running',
            'source': 'dedicated',
            'base_url': 'http://127.0.0.1:59997',
        },
    )
    monkeypatch.setattr(service, '_cached_whatsapp_approval_session_snapshot', lambda *a, **k: {
        'account_key': 'registration-standby',
        'status': 'warm',
        'ready': True,
        'authenticated': True,
        'login_verified': True,
        'login_check_status': 'passed',
        'login_state': 'logged_in',
        'can_probe': True,
        'can_show_qr': False,
        'should_auto_rebuild': False,
        'from_cached_session': True,
    })
    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', lambda *a, **k: (_ for _ in ()).throw(AssertionError('lightweight list must not call worker health')))
    monkeypatch.setattr(service, '_current_whatsapp_approval_worker_health', lambda *a, **k: (_ for _ in ()).throw(AssertionError('lightweight list must not call shared worker health')))
    monkeypatch.setattr(service, '_apply_live_group_identity_to_binding', lambda *a, **k: (_ for _ in ()).throw(AssertionError('lightweight list must not call live probe')))

    payload = service.list_whatsapp_approval_accounts(lightweight=True)
    row = payload['rows'][0]
    binding = row['group_binding_runtimes'][0]

    assert row['session_state']['login_state'] == 'logged_in'
    assert row['schedule_runtime']['status'] == 'always_on'
    assert row['runtime_status'] == 'active'
    assert row['status_text'] == '运行中'
    assert binding['schedule_runtime']['status'] == 'outside_window'
    assert binding['monitoring_status_text'] == '监控中'


def test_lightweight_account_list_uses_cached_worker_health_when_session_cache_expired(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO whatsapp_approval_accounts (
                account_key, account_name, responsible_type, group_links, area, notify_profile_name,
                approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker,
                schedule_windows, enabled, verification_status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'registration-cached-health', '+639****0004', 'registration_group',
                json.dumps([{
                    'link': 'https://chat.whatsapp.com/CACHEDHEALTH123',
                    'area': 'Indonesia',
                    'notify_profile_name': 'wa-approval-broadcast',
                    'enabled': True,
                    'registration_group': 'group-cached-health@g.us',
                }]),
                'Indonesia', 'wa-approval-broadcast', 'threshold_or_timeout', 100, 200, 1,
                json.dumps([]), 1, 'pending_verification', datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

    auth_path = str(service._whatsapp_approval_session_auth_path('registration-cached-health'))
    client_id = service._whatsapp_approval_session_client_id('registration-cached-health')
    meta = {
        'pid': 12345,
        'port': 60001,
        'base_url': 'http://127.0.0.1:60001',
        'auth_path': auth_path,
        'client_id': client_id,
        'started_at': datetime.now(timezone.utc).isoformat(),
        # expired session cache should not make the lightweight list forget a fresh authenticated worker snapshot
        'last_session_checked_ts': 1,
        'last_session_state': {
            'login_verified': True,
            'login_check_status': 'passed',
            'login_state': 'logged_in',
            'can_probe': True,
        },
        'last_worker_health': {
            'status': 'warm',
            'ready': True,
            'authenticated': True,
            'approval_client': {
                'status': 'warm',
                'ready': True,
                'authenticated': True,
                'client_id': client_id,
                'auth_path': auth_path,
                'auth_strategy': 'LocalAuth',
            },
        },
    }
    monkeypatch.setattr(service, '_production_ops_daemon_snapshot', lambda: {'config': {'enabled': True}, 'runtime': {'status': {}}})
    monkeypatch.setattr(service, '_list_notify_robot_options', lambda: [])
    monkeypatch.setattr(service, '_list_customer_service_options', lambda: [])
    monkeypatch.setattr(service, 'list_whatsapp_approval_area_options', lambda: {'options': [], 'source_options': []})
    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: meta if key == 'registration-cached-health' else {})
    monkeypatch.setattr(service, '_pid_running', lambda pid: True)
    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', lambda *a, **k: (_ for _ in ()).throw(AssertionError('lightweight list must not call worker health')))
    monkeypatch.setattr(service, '_current_whatsapp_approval_worker_health', lambda *a, **k: (_ for _ in ()).throw(AssertionError('lightweight list must not call shared worker health')))
    monkeypatch.setattr(service, '_apply_live_group_identity_to_binding', lambda *a, **k: (_ for _ in ()).throw(AssertionError('lightweight list must not live probe')))

    payload = service.list_whatsapp_approval_accounts(lightweight=True)
    row = payload['rows'][0]

    assert row['runtime_state']['ready'] is True
    assert row['runtime_state']['authenticated'] is True
    assert row['session_state']['login_state'] == 'logged_in'
    assert row['session_state']['login_verified'] is True
    assert row['session_state']['can_probe'] is True
    assert row['session_state']['from_cached_worker_health'] is True



def test_registration_group_health_uses_dedicated_runtime_when_legacy_8787_is_degraded(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO whatsapp_approval_accounts (
                account_key, account_name, responsible_type, group_links, area, notify_profile_name,
                approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker,
                schedule_windows, enabled, verification_status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'registration-dedicated-1', '+639****0002', 'registration_group',
                json.dumps([{'link': 'RG', 'area': 'Indonesia', 'enabled': True}]),
                'Indonesia', '', 'threshold_or_timeout', 100, 200, 1,
                json.dumps([]), 1, 'verified', datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

    class Legacy8787Executor:
        base_url = 'http://127.0.0.1:8787'

        def health(self):
            raise RuntimeError('127.0.0.1:8787 connection refused')

    service.registration_group_approval_executor = Legacy8787Executor()
    monkeypatch.setattr(service, '_production_ops_daemon_snapshot', lambda: {'config': {'enabled': True}, 'runtime': {'status': {}}})
    monkeypatch.setattr(service, '_registration_group_active_monitor_target_health', lambda: None)
    monkeypatch.setattr(
        service,
        '_build_whatsapp_approval_runtime_state',
        lambda account_key, **kwargs: {
            'account_key': account_key,
            'configured': True,
            'active': True,
            'ready': True,
            'authenticated': True,
            'login_verified': True,
            'status': 'running',
            'source': 'dedicated',
            'base_url': 'http://127.0.0.1:62001',
        },
    )
    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', lambda base_url: {
        'status': 'warm',
        'ready': True,
        'authenticated': True,
        'supports': ['approve'],
    })

    health = service.registration_group_approval_executor_health()

    assert health['status'] == 'warm'
    assert health['source'] == 'dedicated_approval_account_runtime'
    assert health['base_url'] == 'http://127.0.0.1:62001'
    assert health['legacy_shared_worker_ignored'] is True
    assert '8787' not in str(health.get('error', ''))



def test_registration_group_health_ignores_legacy_8787_when_no_dedicated_runtime(monkeypatch):
    db = Database(':memory:')
    service = Service(db)

    class Legacy8787Executor:
        base_url = 'http://127.0.0.1:8787'

        def health(self):
            raise AssertionError('legacy 8787 health must not be called')

    service.registration_group_approval_executor = Legacy8787Executor()
    monkeypatch.setattr(service, '_production_ops_daemon_snapshot', lambda: {'config': {'enabled': True}, 'runtime': {'status': {}}})
    monkeypatch.setattr(service, '_registration_group_active_monitor_target_health', lambda: None)
    monkeypatch.setattr(service, '_registration_group_dedicated_runtime_health', lambda: None)

    health = service.registration_group_approval_executor_health()

    assert health['status'] == 'idle'
    assert health['source'] == 'dedicated_approval_account_runtime'
    assert health['legacy_shared_worker_ignored'] is True
    assert health['base_url'] is None



def test_runtime_capacity_limit_queues_new_runtime_when_two_same_bucket_are_active(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    monkeypatch.setenv('WHATSAPP_APPROVAL_MAX_ACTIVE_RUNTIMES', '2')
    monkeypatch.setattr(service, '_active_whatsapp_approval_runtime_entries', lambda exclude_account_key='': [
        {'account_key': 'registration-a', 'pid': 1001},
        {'account_key': 'registration-b', 'pid': 1002},
    ])
    monkeypatch.setattr(service, '_whatsapp_approval_runtime_capacity_bucket', lambda account_key: 'registration_group')

    with pytest.raises(Exception) as exc_info:
        service._ensure_whatsapp_approval_runtime_capacity('registration-c')

    exc = exc_info.value
    assert getattr(exc, 'status_code', None) == 429
    assert exc.detail['code'] == 'queued_runtime_start'
    assert exc.detail['max_active_runtimes'] == 2
    assert exc.detail['active_runtime_count'] == 2


def test_runtime_capacity_does_not_block_learning_bot_with_registration_runtimes(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    monkeypatch.setenv('WHATSAPP_APPROVAL_MAX_ACTIVE_RUNTIMES', '2')
    monkeypatch.setattr(service, '_active_whatsapp_approval_runtime_entries', lambda exclude_account_key='': [
        {'account_key': 'registration-a', 'pid': 1001},
        {'account_key': 'registration-b', 'pid': 1002},
    ])
    buckets = {
        'registration-a': 'registration_group',
        'registration-b': 'registration_group',
        'learn-indo-01': 'group_atmosphere',
    }
    monkeypatch.setattr(service, '_whatsapp_approval_runtime_capacity_bucket', lambda account_key: buckets.get(account_key, 'registration_group'))

    service._ensure_whatsapp_approval_runtime_capacity('learn-indo-01')



def test_binding_probe_matches_registration_group_cycle_top_level_target():
    production_ops = {
        'runtime': {
            'status': {
                'registration_group_cycles': [
                    {
                        'registration_group': '120363400336474261@g.us',
                        'monitor_target': {
                            'account_key': 'registration-639974974871',
                            'binding_link': 'https://chat.whatsapp.com/LGIF0iCDo2D0LzgyfjeV3D',
                        },
                        'decision_group_state': {
                            'source': 'group_state',
                            'payload': {
                                'group_name': '🇮🇩24-Grup Registrasi Resmi  ✘ Linky 💎',
                                'pending_count': 30,
                                'probe_data_quality': 'confirmed_pending',
                                'zero_pending_unverified': False,
                            },
                        },
                    }
                ]
            }
        }
    }

    probe = Service._binding_probe_from_production_ops_status(
        production_ops,
        responsible_type='registration_group',
        account_key='registration-639974974871',
        binding={
            'group_id': '120363400336474261@g.us',
            'group_name': '🇮🇩24-Grup Registrasi Resmi  ✘ Linky 💎',
            'link': 'https://chat.whatsapp.com/LGIF0iCDo2D0LzgyfjeV3D',
        },
    )

    assert probe['group_id'] == '120363400336474261@g.us'
    assert probe['pending_count'] == 30
    assert probe['probe_data_quality'] == 'confirmed_pending'

    verifier = Service._binding_membership_verifier_state(
        {
            'enabled': True,
            'group_id': '120363400336474261@g.us',
            'group_name': '🇮🇩24-Grup Registrasi Resmi  ✘ Linky 💎',
            'link': 'https://chat.whatsapp.com/LGIF0iCDo2D0LzgyfjeV3D',
        },
        {'ready': False, 'status': 'probe_unavailable', 'detail': '探针未就绪'},
        responsible_type='registration_group',
        production_ops=production_ops,
        live_probe=probe,
    )

    assert verifier['ready'] is True
    assert verifier['status'] == 'mapped_live_probe_ready'
    assert verifier['probe']['pending_count'] == 30



def test_runtime_queue_rows_read_snapshots_without_live_group_state(monkeypatch):
    service = Service(Database(':memory:'))
    monkeypatch.setattr(service, '_production_ops_daemon_snapshot', lambda: {'runtime': {'status': {}}})
    monkeypatch.setattr(service, '_request_whatsapp_approval_group_state_with_retry', lambda *a, **k: (_ for _ in ()).throw(AssertionError('summary must not call live group-state')))
    monkeypatch.setattr(service, 'list_whatsapp_approval_accounts', lambda *a, **k: {
        'rows': [
            {
                'account_key': 'registration-a',
                'account_name': 'WA A',
                'responsible_type': 'registration_group',
                'enabled': True,
                'runtime_state': {'active': True, 'base_url': 'http://127.0.0.1:60001'},
                'group_binding_runtimes': [
                    {
                        'enabled': True,
                        'group_id': 'g1',
                        'group_name': '注册群1',
                        'schedule_runtime': {'active_now': True},
                        'next_approval_pending_count': 7,
                        'next_approval_oldest_pending_at': '2026-05-20T00:00:00+00:00',
                    }
                ],
            }
        ]
    })

    rows = service._registration_group_runtime_queue_rows(now_iso='2026-05-20T00:10:00+00:00')

    assert rows[0]['pending_count'] == 7
    assert rows[0]['group_name'] == '注册群1'



def test_official_runtime_queue_rows_read_snapshots_without_live_group_state(monkeypatch):
    service = Service(Database(':memory:'))
    monkeypatch.setattr(service, '_production_ops_daemon_snapshot', lambda: {'runtime': {'status': {}}})
    monkeypatch.setattr(service, '_request_whatsapp_approval_group_state_with_retry', lambda *a, **k: (_ for _ in ()).throw(AssertionError('summary must not call live group-state')))
    monkeypatch.setattr(service, 'list_whatsapp_approval_accounts', lambda *a, **k: {
        'rows': [
            {
                'account_key': 'official-a',
                'account_name': 'WA Official',
                'responsible_type': 'official_group',
                'enabled': True,
                'runtime_state': {'active': True, 'base_url': 'http://127.0.0.1:60002'},
                'group_binding_runtimes': [
                    {
                        'enabled': True,
                        'group_id': 'og1',
                        'group_name': '官方群1',
                        'schedule_runtime': {'active_now': True},
                        'next_approval_pending_count': 3,
                        'next_approval_oldest_pending_at': '2026-05-20T00:00:00+00:00',
                    }
                ],
            }
        ]
    })

    rows = service._official_group_runtime_queue_rows(now_iso='2026-05-20T00:10:00+00:00')

    assert rows[0]['pending_count'] == 3
    assert rows[0]['registration_group'] == '官方群1'



def test_production_ops_page_load_uses_snapshot_only_and_does_not_auto_session_refresh():
    source = Path('app/main.py').read_text()

    assert "/api/ops/whatsapp-approval-accounts/realtime-snapshot" in source
    assert "scheduleApprovalAccountsTruthRefresh(data.rows || [])" not in source
    assert "后台列表只读服务器快照" in source



def test_runtime_directory_endpoints_use_lightweight_snapshot_rows():
    source = Path('app/main.py').read_text()

    assert "def _ops_whatsapp_approval_account_directory_rows" in source
    assert "def ops_whatsapp_approval_accounts_runtime_directory" in source
    assert "def ops_whatsapp_approval_accounts_registration_runtime_directory" in source
    assert "def ops_whatsapp_approval_accounts_binding_directory" in source
    assert "def ops_whatsapp_approval_accounts_official_binding_directory" in source
    assert "skip_health_check=True" in source
    directory_block = source.split("def _ops_whatsapp_approval_account_directory_rows", 1)[1].split("@app.get('/api/ops/whatsapp-approval-accounts/{account_key}/runtime')", 1)[0]
    assert "list_whatsapp_approval_accounts" not in directory_block
    assert "_request_whatsapp_approval_group_state" not in directory_block



def test_production_ops_qr_modal_keep_open_during_scan_pending_marker():
    source = Path('app/main.py').read_text()

    assert 'function approvalSessionShouldKeepQrModalOpen(sessionState, options = {})' in source
    assert 'const shouldKeepOpen = currentState.open && currentState.accountKey === normalized && approvalSessionShouldKeepQrModalOpen(mergedSessionState, options);' in source
    assert "renderApprovalQrModal();\n      return;" in source
    assert 'if (refreshCount >= 3) return;' not in source
    assert "showToast('扫码登录成功，账号已可用', 'success');" in source
    assert 'closeApprovalQrModal();' in source
    assert "reloadApprovalAccounts().catch(err => console.warn('reload approval accounts after qr login success failed', err));" in source


def test_production_ops_qr_modal_requires_real_qr_payload_marker():
    source = Path('app/main.py').read_text()

    assert 'function approvalSessionHasQrPayload(sessionState)' in source
    assert 'function approvalSessionCanOpenQrModal(sessionState, options = {})' in source
    assert 'approvalSessionCanOpenQrModal(mergedSessionState, options)' in source
    assert 'if (sessionState.qr_available)' not in source
    assert 'approvalSessionHasQrPayload(sessionState)' in source


def test_production_ops_qr_modal_invalidates_stale_qr_and_live_refreshes_before_reuse_marker():
    source = Path('app/main.py').read_text()

    assert 'function approvalSessionShouldInvalidateQrPayload(sessionState) {' in source
    assert "if (session.qr_available === false || session.can_show_qr === false) return true;" in source
    assert "merged.qr_image_data_url = '';" in source
    assert "merged.qr_ascii = '';" in source
    assert "merged.qr_text = '';" in source
    assert "if (options.loading) {" in source
    assert "const liveSessionData = await refreshApprovalAccountSession(normalized);" in source
    assert "console.warn('approval session live refresh before qr reuse failed'" in source


def test_production_ops_truth_refresh_uses_unified_login_state_marker():
    source = Path('app/main.py').read_text()

    assert "const loginState = String(sess.login_state || '').trim();" in source
    assert "['runtime_starting', 'initializing'].includes(loginState)" in source
    assert "runtime.active && !runtime.ready" not in source
    assert "['pending_runtime', 'auto_recovering', 'session_mismatch', 'runtime_unavailable', ''].includes(code)" not in source


def test_production_ops_runtime_recovery_button_and_qr_gate_marker():
    source = Path('app/main.py').read_text()

    assert 'function approvalAccountRuntimeIsProductionReady(row, sessionState)' in source
    assert 'function approvalAccountCanRequestQr(row, sessionState)' in source
    assert "['awaiting_qr', 'qr_pending', 'waiting_for_scan'].includes(runtimeStatus)" in source
    assert "const pendingButWorkerHasQr = loginStatus === 'pending_runtime' && qrRuntimeReady;" in source
    assert 'function approvalAccountQrActionDisabled(row, sessionState, isSessionLoading)' in source
    assert 'function recoverApprovalAccountRuntime(accountKey)' in source
    assert "runtime/recover`, {method: 'POST'}" in source
    assert 'def recover_whatsapp_approval_account_runtime(self, account_key: str)' in source
    assert "manual_recovery_in_progress" in source
    assert 'window.__approvalRuntimeRecoveryPendingByAccount[normalized] = true;' in source
    assert "recoveryButtonText = recoveryPending && !isProductionReady ? '恢复中' : '恢复服务'" in source
    assert "const recoveryNeeded = approvalAccountRuntimeNeedsRecovery(row, sessionState);" in source
    assert "const recoveryButtonDisabled = isProductionReady || recoveryPending || !recoveryNeeded;" in source
    assert "onclick=\"recoverApprovalAccountRuntime('${accountKeyEscaped}')\"" in source
    assert "${qrActionDisabled ? 'disabled' : ''} onclick=\"startApprovalAccountSession" in source
    assert "showToast('服务已恢复至可投产状态', 'success')" in source


def test_truth_state_zero_without_empty_queue_evidence_is_unverified():
    from app.registration_group_truth import build_truth_state
    truth = build_truth_state(status={'decision_group_state': {'source': 'group_state', 'payload': {'pending_count': 0, 'pending_zero_confidence': 'unverified'}}})
    assert truth['status'] == 'empty_unverified'
    assert truth['zero_pending_unverified'] is True
    assert truth['pending_zero_confidence'] == 'unverified'


def test_binding_verifier_blocks_not_member_and_not_admin_before_ready():
    from app.main import Service
    binding = {'enabled': True, 'link': 'https://chat.whatsapp.com/new', 'group_id': 'g1@g.us', 'group_name': 'Group 1'}
    account_verifier = {'ready': False, 'status': 'probe_unavailable'}
    not_member = Service._binding_membership_verifier_state(
        binding,
        account_verifier,
        responsible_type='registration_group',
        production_ops={},
        live_probe={'group_id': 'g1@g.us', 'group_name': 'Group 1', 'pending_count': 0, 'member_count': 100, 'participants_load_status': 'complete', 'participants_count_raw': 100, 'self_participant_found': False, 'source': 'group_state'},
    )
    assert not_member['ready'] is False
    assert not_member['status'] == 'not_group_member'
    assert '未出现在已完整读取' in not_member['detail']

    not_admin = Service._binding_membership_verifier_state(
        binding,
        account_verifier,
        responsible_type='registration_group',
        production_ops={},
        live_probe={'group_id': 'g1@g.us', 'group_name': 'Group 1', 'pending_count': 2, 'member_count': 100, 'self_participant_found': True, 'self_is_admin': False, 'can_manage_membership_requests': False, 'source': 'group_state'},
    )
    assert not_admin['ready'] is False
    assert not_admin['status'] == 'admin_unconfirmed'
    assert '管理员身份未被探针可靠确认' in not_admin['detail']


def test_binding_probe_target_prefers_authoritative_group_id_over_invite_link():
    from app.main import Service
    assert Service._whatsapp_binding_probe_target({
        'link': 'https://chat.whatsapp.com/newInvite',
        'group_id': '120363old@g.us',
        'group_name': '旧群',
    }) == '120363old@g.us'


def test_binding_probe_candidates_use_only_group_id_when_present():
    from app.main import Service

    binding = {
        'link': 'https://chat.whatsapp.com/newInvite',
        'registration_group': '120363old@g.us',
        'group_id': '120363old@g.us',
        'group_name': '旧群',
    }

    assert Service._whatsapp_binding_probe_candidates(binding) == ['120363old@g.us']


def _insert_registration_account_with_binding(db, account_key='registration-truth', link='https://chat.whatsapp.com/TRUTH12345', registration_group='truth-group@g.us', binding_id='wabind_truth_binding', identity_status='resolved'):
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO whatsapp_approval_accounts (
                account_key, account_name, responsible_type, group_links, area, notify_profile_name,
                approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker,
                schedule_windows, enabled, verification_status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_key,
                '+639****truth',
                'registration_group',
                json.dumps([{
                    'binding_id': binding_id,
                    'link': link,
                    'area': 'Indonesia',
                    'notify_profile_name': 'wa-approval-broadcast',
                    'enabled': True,
                    'registration_group': registration_group,
                    'group_id': registration_group,
                    'group_name': 'Truth Group',
                    'identity_status': identity_status,
                }]),
                'Indonesia',
                'wa-approval-broadcast',
                'threshold_or_timeout',
                100,
                200,
                1,
                json.dumps([]),
                1,
                'pending_verification',
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def _patch_lightweight_account_dependencies(monkeypatch, service, account_key='registration-truth'):
    monkeypatch.setattr(service, '_production_ops_daemon_snapshot', lambda: {'config': {'enabled': True}, 'runtime': {'status': {}, 'launch_agent_installed': True}})
    monkeypatch.setattr(service, '_list_notify_robot_options', lambda: [])
    monkeypatch.setattr(service, '_list_customer_service_options', lambda: [])
    monkeypatch.setattr(service, 'list_whatsapp_approval_area_options', lambda: {'options': [], 'source_options': []})
    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', lambda *a, **k: (_ for _ in ()).throw(AssertionError('lightweight list must not call worker health')))
    monkeypatch.setattr(service, '_current_whatsapp_approval_worker_health', lambda *a, **k: (_ for _ in ()).throw(AssertionError('lightweight list must not call shared worker health')))
    monkeypatch.setattr(service, '_apply_live_group_identity_to_binding', lambda *a, **k: (_ for _ in ()).throw(AssertionError('lightweight list must not call live probe')))
    monkeypatch.setattr(
        service,
        '_build_whatsapp_approval_runtime_state',
        lambda *args, **kwargs: {
            'account_key': account_key,
            'configured': True,
            'active': True,
            'ready': True,
            'authenticated': True,
            'login_verified': True,
            'status': 'running',
            'source': 'dedicated',
            'base_url': 'http://127.0.0.1:59996',
        },
    )
    monkeypatch.setattr(service, '_cached_whatsapp_approval_session_snapshot', lambda *a, **k: {
        'account_key': account_key,
        'status': 'warm',
        'ready': True,
        'authenticated': True,
        'login_verified': True,
        'login_check_status': 'passed',
        'login_state': 'logged_in',
        'can_probe': True,
        'can_show_qr': False,
        'should_auto_rebuild': False,
        'from_cached_session': True,
    })


def _insert_queue_snapshot(db, *, account_key='registration-truth', link='https://chat.whatsapp.com/TRUTH12345', binding_id='', snapshot_type='approval_queue_current_truth', trust_status='TRUSTED_CONFIRMED_PENDING', pending_count=15, checked_at=None, expires_at=None, source_priority=100, syncing=False, api_pending_count=None):
    checked_at = checked_at or datetime.now(timezone.utc).isoformat()
    facts = {
        'trust_status': trust_status,
        'trusted_pending_count': pending_count if trust_status.startswith('TRUSTED') else None,
        'pending_count': pending_count,
        'display_trusted': trust_status.startswith('TRUSTED'),
        'can_manual_approve': trust_status == 'TRUSTED_CONFIRMED_PENDING',
        'manual_approve_allowed': trust_status == 'TRUSTED_CONFIRMED_PENDING',
        'syncing': syncing,
    }
    if api_pending_count is not None:
        facts['api_pending_count'] = api_pending_count
    object_key = f'{account_key}:binding:{binding_id}' if binding_id else f'{account_key}:{link}'
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO mcn_truth_snapshots (
                snapshot_id, object_type, object_key, snapshot_type, truth_status,
                confidence, confidence_reason, facts_json, source_json, checked_at,
                expires_at, recommended_action, updated_at
            ) VALUES (?, 'registration_group_binding', ?, ?, ?, 'verified', '', ?, ?, ?, ?, '', ?)
            """,
            (
                f'{snapshot_type}:{object_key}',
                object_key,
                snapshot_type,
                trust_status,
                json.dumps(facts, ensure_ascii=False),
                json.dumps({'source_priority': source_priority}, ensure_ascii=False),
                checked_at,
                expires_at,
                checked_at,
            ),
        )
        conn.commit()


def _insert_pending_truth_snapshot(
    db,
    *,
    account_key='registration-truth',
    link='https://chat.whatsapp.com/TRUTH12345',
    binding_id='',
    registration_group='truth-group@g.us',
    truth_status='confirmed_empty',
    confidence='verified',
    confidence_reason='empty_queue_confirmed',
    pending_count=0,
    checked_at=None,
    expires_at=None,
    actual_group_name='Truth Group',
    requester_ids=None,
    requesters=None,
):
    checked_at = checked_at or datetime.now(timezone.utc).isoformat()
    expires_at = expires_at or (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    object_key = f'{account_key}:binding:{binding_id}' if binding_id else f'{account_key}:{link}'
    normalized_requester_ids = [str(item).strip() for item in (requester_ids or []) if str(item).strip()]
    normalized_requesters = list(requesters or [])
    facts = {
        'configured_registration_group': registration_group,
        'configured_group_id': registration_group,
        'configured_link': link,
        'actual_group_id': registration_group,
        'actual_group_name': actual_group_name,
        'pending_count': pending_count,
        'member_count': 123,
        'requester_ids': normalized_requester_ids,
        'requesters': normalized_requesters,
        'login_verified': True,
        'runtime_active': True,
        'runtime_authenticated': True,
        'runtime_ready': True,
        'session_target_match': True,
        'review_surface_ready': bool(normalized_requester_ids),
        'empty_queue_visible': False,
        'zero_pending_unverified': False,
        'zero_pending_verified_by': 'consecutive_group_state_refresh',
        'can_manage_membership_requests': True,
        'self_is_admin': True,
        'self_participant_found': True,
    }
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO mcn_truth_snapshots (
                snapshot_id, object_type, object_key, snapshot_type, truth_status,
                confidence, confidence_reason, facts_json, source_json, checked_at,
                expires_at, recommended_action, updated_at
            ) VALUES (?, 'registration_group_binding', ?, 'pending_truth', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f'pending_truth:{object_key}',
                object_key,
                truth_status,
                confidence,
                confidence_reason,
                json.dumps(facts, ensure_ascii=False),
                json.dumps({'source': 'pending_truth_probe'}, ensure_ascii=False),
                checked_at,
                expires_at,
                'none',
                checked_at,
            ),
        )
        conn.commit()


def _insert_probe_observed_event(
    db,
    *,
    account_key='registration-truth',
    link='https://chat.whatsapp.com/TRUTH12345',
    binding_id='',
    registration_group='truth-group@g.us',
    trust_status='TRUSTED_CONFIRMED_PENDING',
    pending_count=0,
    checked_at=None,
    actual_group_name='Truth Group',
    requester_ids=None,
    requesters=None,
):
    checked_at = checked_at or datetime.now(timezone.utc).isoformat()
    object_key = f'{account_key}:binding:{binding_id}' if binding_id else f'{account_key}:{link}'
    normalized_requester_ids = [str(item).strip() for item in (requester_ids or []) if str(item).strip()]
    normalized_requesters = list(requesters or [])
    payload = {
        'configured_registration_group': registration_group,
        'configured_group_id': registration_group,
        'configured_link': link,
        'actual_group_id': registration_group,
        'actual_group_name': actual_group_name,
        'pending_count': pending_count,
        'member_count': 123,
        'requester_ids': normalized_requester_ids,
        'requesters': normalized_requesters,
        'login_verified': True,
        'runtime_active': True,
        'runtime_authenticated': True,
        'runtime_ready': True,
        'session_target_match': True,
        'review_surface_ready': bool(normalized_requester_ids),
        'empty_queue_visible': pending_count == 0,
        'zero_pending_unverified': False,
        'zero_pending_verified_by': 'consecutive_group_state_refresh',
        'can_manage_membership_requests': True,
        'self_is_admin': True,
        'self_participant_found': True,
        'reason_code': 'pending_detected' if pending_count else 'empty_queue_confirmed',
        'source_ts': checked_at,
        'source': {'mode': 'worker_full_queue_sync'},
    }
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO mcn_event_ledger (
                event_id, event_type, object_type, object_key, actor_type, actor_id,
                status, evidence_level, external_id, payload_json, created_at
            ) VALUES (?, 'approval_queue_probe_observed', 'registration_group_binding', ?, 'system', '', ?, 'verified', '', ?, ?)
            """,
            (
                f'evt:{object_key}:{checked_at}:{trust_status}',
                object_key,
                trust_status,
                json.dumps(payload, ensure_ascii=False),
                checked_at,
            ),
        )
        conn.commit()


def test_lightweight_registration_binding_uses_current_truth_for_display_and_manual_gate(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    _insert_queue_snapshot(db, trust_status='TRUSTED_CONFIRMED_PENDING', pending_count=15)
    _patch_lightweight_account_dependencies(monkeypatch, service)

    payload = service.list_whatsapp_approval_accounts(lightweight=True)
    binding = payload['rows'][0]['group_binding_runtimes'][0]

    assert binding['approval_queue_truth']['current_truth']['trust_status'] == 'TRUSTED_CONFIRMED_PENDING'
    assert binding['approval_queue_truth']['status'] == 'count'
    assert binding['approval_queue_truth']['freshness_level'] == 'FRESH'
    assert binding['approval_queue_truth']['pending_count'] == 15
    assert binding['approval_queue_truth']['can_manual_approve'] is True
    assert binding['approval_queue_truth']['auto_approval_enabled'] is False


def test_lightweight_registration_binding_keeps_stale_truth_visible_but_blocks_manual_approve(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    old = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    _insert_queue_snapshot(
        db,
        trust_status='TRUSTED_CONFIRMED_PENDING',
        pending_count=7,
        checked_at=old,
        expires_at=(datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
    )
    _patch_lightweight_account_dependencies(monkeypatch, service)

    payload = service.list_whatsapp_approval_accounts(lightweight=True)
    truth = payload['rows'][0]['group_binding_runtimes'][0]['approval_queue_truth']

    assert truth['freshness_level'] == 'STALE'
    assert truth['status'] == 'stale'
    assert truth['pending_count'] == 7
    assert truth['can_manual_approve'] is False
    assert truth['display_text'] == '当前审批列表 7 人'
    assert truth['display']['primary_text'] == truth['display_text']
    assert truth['display']['secondary_text'] == ''


def test_lightweight_account_list_orders_by_created_at_not_recent_updates(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    older_created = '2026-05-01T00:00:00+00:00'
    newer_created = '2026-05-02T00:00:00+00:00'
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO whatsapp_approval_accounts (
                account_key, account_name, responsible_type, group_links, area, notify_profile_name,
                approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker,
                schedule_windows, enabled, verification_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'registration-older', '+639****older', 'registration_group',
                json.dumps([{'link': 'https://chat.whatsapp.com/OLDER12345', 'area': 'Indonesia', 'notify_profile_name': 'wa-approval-broadcast', 'enabled': True}]),
                'Indonesia', 'wa-approval-broadcast', 'threshold_or_timeout', 100, 200, 1,
                json.dumps([]), 1, 'pending_verification', older_created, '2026-05-10T00:00:00+00:00',
            ),
        )
        conn.execute(
            """
            INSERT INTO whatsapp_approval_accounts (
                account_key, account_name, responsible_type, group_links, area, notify_profile_name,
                approval_rule, approval_count_threshold, approval_timeout_minutes, auto_recover_worker,
                schedule_windows, enabled, verification_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'registration-newer', '+639****newer', 'registration_group',
                json.dumps([{'link': 'https://chat.whatsapp.com/NEWER12345', 'area': 'Indonesia', 'notify_profile_name': 'wa-approval-broadcast', 'enabled': True}]),
                'Indonesia', 'wa-approval-broadcast', 'threshold_or_timeout', 100, 200, 1,
                json.dumps([]), 1, 'pending_verification', newer_created, '2026-05-03T00:00:00+00:00',
            ),
        )
        conn.commit()

    monkeypatch.setattr(service, '_production_ops_daemon_snapshot', lambda: {'config': {'enabled': True}, 'runtime': {'status': {}, 'launch_agent_installed': True}})
    monkeypatch.setattr(service, '_list_notify_robot_options', lambda: [])
    monkeypatch.setattr(service, '_list_customer_service_options', lambda: [])
    monkeypatch.setattr(service, 'list_whatsapp_approval_area_options', lambda: {'options': [], 'source_options': []})
    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', lambda *a, **k: (_ for _ in ()).throw(AssertionError('lightweight list must not call worker health')))
    monkeypatch.setattr(service, '_current_whatsapp_approval_worker_health', lambda *a, **k: (_ for _ in ()).throw(AssertionError('lightweight list must not call shared worker health')))
    monkeypatch.setattr(service, '_apply_live_group_identity_to_binding', lambda *a, **k: (_ for _ in ()).throw(AssertionError('lightweight list must not call live probe')))
    monkeypatch.setattr(
        service,
        '_build_whatsapp_approval_runtime_state',
        lambda account_key, *args, **kwargs: {
            'account_key': account_key,
            'configured': True,
            'active': True,
            'ready': True,
            'authenticated': True,
            'login_verified': True,
            'status': 'running',
            'source': 'dedicated',
            'base_url': f'http://127.0.0.1/{account_key}',
        },
    )
    monkeypatch.setattr(service, '_cached_whatsapp_approval_session_snapshot', lambda account_key, *a, **k: {
        'account_key': account_key,
        'status': 'warm',
        'ready': True,
        'authenticated': True,
        'login_verified': True,
        'login_check_status': 'passed',
        'login_state': 'logged_in',
        'can_probe': True,
        'can_show_qr': False,
        'should_auto_rebuild': False,
        'from_cached_session': True,
    })

    payload = service.list_whatsapp_approval_accounts(lightweight=True)

    assert [row['account_key'] for row in payload['rows']] == ['registration-older', 'registration-newer']


def test_lightweight_probe_never_becomes_current_truth_or_manual_approve_source(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    _insert_queue_snapshot(db, snapshot_type='approval_queue_latest_probe', trust_status='UNTRUSTED_API_STALE', pending_count=7, api_pending_count=7)
    _patch_lightweight_account_dependencies(monkeypatch, service)

    payload = service.list_whatsapp_approval_accounts(lightweight=True)
    truth = payload['rows'][0]['group_binding_runtimes'][0]['approval_queue_truth']

    assert truth['current_truth'] is None
    assert truth['latest_probe']['trust_status'] == 'UNTRUSTED_API_STALE'
    assert truth['freshness_level'] == 'UNKNOWN'
    assert truth['status'] == 'unknown'
    assert truth['display']['state'] == 'UNKNOWN'
    assert truth['display']['count'] is None
    assert truth['display']['primary_text'] == '审批队列待刷新'
    assert truth['display']['secondary_text'] == '暂无法确认当前待审批人数'
    assert truth['pending_count'] is None
    assert truth['can_manual_approve'] is False
    assert truth['latest_probe']['api_pending_count'] == 7


def test_newer_latest_probe_pending_overrides_older_current_empty_in_display(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    old = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    _insert_queue_snapshot(db, trust_status='TRUSTED_CONFIRMED_EMPTY', pending_count=0, checked_at=old)
    _insert_queue_snapshot(
        db,
        snapshot_type='approval_queue_latest_probe',
        trust_status='UNTRUSTED_API_STALE',
        pending_count=7,
        api_pending_count=7,
        checked_at=fresh,
    )
    _patch_lightweight_account_dependencies(monkeypatch, service)

    payload = service.list_whatsapp_approval_accounts(lightweight=True)
    truth = payload['rows'][0]['group_binding_runtimes'][0]['approval_queue_truth']

    assert truth['current_truth']['trust_status'] == 'TRUSTED_CONFIRMED_EMPTY'
    assert truth['latest_probe']['api_pending_count'] == 7
    assert truth['status'] == 'count'
    assert truth['display']['state'] == 'COUNT'
    assert truth['display']['count'] == 0
    assert truth['display']['debug_count'] is None
    assert truth['display']['primary_text'] == '待审批 0 人'
    assert truth['display']['secondary_text'] == ''
    assert truth['pending_count'] == 0
    assert truth['can_manual_approve'] is False


def test_newer_latest_probe_zero_overrides_older_current_pending_in_display(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    old = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    _insert_queue_snapshot(db, trust_status='TRUSTED_CONFIRMED_PENDING', pending_count=5, checked_at=old)
    _insert_queue_snapshot(
        db,
        snapshot_type='approval_queue_latest_probe',
        trust_status='UNTRUSTED_API_STALE',
        pending_count=0,
        api_pending_count=0,
        checked_at=fresh,
    )
    _patch_lightweight_account_dependencies(monkeypatch, service)

    payload = service.list_whatsapp_approval_accounts(lightweight=True)
    truth = payload['rows'][0]['group_binding_runtimes'][0]['approval_queue_truth']

    assert truth['current_truth']['trust_status'] == 'TRUSTED_CONFIRMED_PENDING'
    assert truth['latest_probe']['api_pending_count'] == 0
    assert truth['status'] == 'count'
    assert truth['display']['state'] == 'COUNT'
    assert truth['display']['count'] == 5
    assert truth['display']['debug_count'] is None
    assert truth['display']['primary_text'] == '待审批 5 人'
    assert truth['display']['secondary_text'] == ''
    assert truth['pending_count'] == 5
    assert truth['can_manual_approve'] is True



def test_lightweight_registration_binding_exposes_display_payload_and_safe_verifier_detail(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    _insert_queue_snapshot(db, trust_status='TRUSTED_CONFIRMED_EMPTY', pending_count=0)
    _patch_lightweight_account_dependencies(monkeypatch, service)

    payload = service.list_whatsapp_approval_accounts(lightweight=True)
    binding = payload['rows'][0]['group_binding_runtimes'][0]
    truth = binding['approval_queue_truth']
    verifier = binding['membership_verifier']

    assert truth['display_schema_version'] == 1
    assert truth['display']['state'] == 'COUNT'
    assert truth['display']['count'] == 0
    assert truth['display']['show_count'] is True
    assert verifier['safe_detail']
    assert '待审批' not in verifier['safe_detail']



def test_manual_approval_truth_invalidation_without_pending_after_only_records_event_ledger():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db, binding_id='wabind-manual-refresh')
    binding = {
        'binding_id': 'wabind-manual-refresh',
        'link': 'https://chat.whatsapp.com/TRUTH12345',
        'registration_group': 'truth-group@g.us',
        'group_id': 'truth-group@g.us',
        'group_name': 'Truth Group',
    }

    service.invalidate_approval_queue_truth_after_mutation(
        account_key='registration-truth',
        binding=binding,
        invalidated_reason='approval_completed',
        approved_count=11,
        approval_run_id='registration_group_approval_run_1',
        action_ts='2026-06-01T04:26:23.049Z',
    )

    snapshots = service._load_approval_binding_queue_snapshots('registration-truth', binding)
    truth = service._approval_queue_truth_view(snapshots.get('current_truth'), snapshots.get('latest_probe'))

    assert snapshots['latest_probe'] is None
    assert truth['status'] == 'unknown'
    assert truth['display']['state'] == 'UNKNOWN'
    assert truth['display']['show_count'] is False
    assert truth['display']['secondary_text'] == '暂无法确认当前待审批人数'
    assert truth['last_approval_action_ts'] is None
    with service.db.connect() as conn:
        row = conn.execute(
            """
            SELECT payload_json FROM mcn_event_ledger
            WHERE event_type='approval_truth_invalidated'
              AND object_type='registration_group_binding'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
    assert row is not None
    payload = json.loads(row['payload_json'])
    assert payload['invalidated_reason'] == 'approval_completed'
    assert payload['last_approved_count'] == 11



def test_manual_approval_truth_invalidation_uses_post_approval_probe_when_pending_after_available():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db, binding_id='wabind-manual-refresh-count')
    binding = {
        'binding_id': 'wabind-manual-refresh-count',
        'link': 'https://chat.whatsapp.com/TRUTH12345',
        'registration_group': 'truth-group@g.us',
        'group_id': 'truth-group@g.us',
        'group_name': 'Truth Group',
    }

    service.invalidate_approval_queue_truth_after_mutation(
        account_key='registration-truth',
        binding=binding,
        invalidated_reason='approval_completed',
        approved_count=2,
        pending_count=0,
        approval_run_id='registration_group_approval_run_2',
        action_ts=datetime.now(timezone.utc).isoformat(),
    )

    snapshots = service._load_approval_binding_queue_snapshots('registration-truth', binding)
    truth = service._approval_queue_truth_view(snapshots.get('current_truth'), snapshots.get('latest_probe'))

    assert snapshots['latest_probe'] is None
    assert snapshots['current_truth']['reason_code'] == 'approval_result_pending_after'
    assert snapshots['current_truth']['invalidated_reason'] is None
    assert truth['status'] == 'count'
    assert truth['display']['state'] == 'COUNT'
    assert truth['display']['primary_text'] == '待审批 0 人'
    assert truth['pending_count'] == 0
    assert truth['can_manual_approve'] is False



def test_manual_approval_truth_invalidation_post_approval_probe_overrides_older_current_count():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db, binding_id='wabind-manual-refresh-override')
    binding = {
        'binding_id': 'wabind-manual-refresh-override',
        'link': 'https://chat.whatsapp.com/TRUTH12345',
        'registration_group': 'truth-group@g.us',
        'group_id': 'truth-group@g.us',
        'group_name': 'Truth Group',
    }
    old = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat()
    service.upsert_approval_queue_current_truth(
        account_key='registration-truth',
        binding=binding,
        sync_result={
            'ok': True,
            'trust_status': 'TRUSTED_CONFIRMED_PENDING',
            'trusted_pending_count': 5,
            'pending_count': 5,
            'ui_pending_count': 5,
            'api_pending_count': 5,
            'requester_ids': [f'u{i}' for i in range(5)],
            'group_identity_verified': True,
            'runtime_identity_match': True,
            'session_authenticated': True,
            'self_participant_found': True,
            'self_is_admin': True,
            'can_manage_membership_requests': True,
            'review_surface_ready': True,
            'can_manual_approve': True,
            'reason_code': 'trusted_pending',
        },
        source_priority=100,
        observed_at=old,
        force=True,
    )

    service.invalidate_approval_queue_truth_after_mutation(
        account_key='registration-truth',
        binding=binding,
        invalidated_reason='approval_completed',
        approved_count=5,
        pending_count=0,
        approval_run_id='registration_group_approval_run_3',
        action_ts=datetime.now(timezone.utc).isoformat(),
    )

    snapshots = service._load_approval_binding_queue_snapshots('registration-truth', binding)
    truth = service._approval_queue_truth_view(snapshots.get('current_truth'), snapshots.get('latest_probe'))

    assert snapshots['latest_probe'] is None
    assert snapshots['current_truth']['reason_code'] == 'approval_result_pending_after'
    assert snapshots['current_truth']['invalidated_reason'] is None
    assert truth['status'] == 'count'
    assert truth['display']['state'] == 'COUNT'
    assert truth['display']['primary_text'] == '待审批 0 人'
    assert truth['display']['debug_count'] is None
    assert truth['pending_count'] == 0
    assert truth['can_manual_approve'] is False


def test_current_truth_guard_rejects_executor_fallback_empty_without_strong_evidence():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)

    result = service.upsert_approval_queue_current_truth(
        account_key='registration-truth',
        binding={'link': 'https://chat.whatsapp.com/TRUTH12345'},
        sync_result={
            'ok': False,
            'trust_status': 'TRUSTED_CONFIRMED_EMPTY',
            'trusted_pending_count': 0,
            'pending_count': 0,
            'ui_pending_count': 0,
            'api_pending_count': 0,
            'empty_queue_visible': False,
            'strong_empty_evidence': False,
            'reason_code': 'executor_group_state_fallback',
            'source': {'mode': 'executor_group_state_fallback'},
        },
        source_priority=100,
        observed_at=datetime.now(timezone.utc).isoformat(),
    )

    assert result['written'] is False
    assert result['reason'] in {'strong_empty_evidence_required', 'executor_fallback_empty_forbidden'}
    snapshots = service._load_approval_binding_queue_snapshots('registration-truth', {'link': 'https://chat.whatsapp.com/TRUTH12345'})
    assert snapshots['current_truth'] is None


def test_current_truth_guard_rejects_pending_without_complete_requester_ids():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)

    result = service.upsert_approval_queue_current_truth(
        account_key='registration-truth',
        binding={'link': 'https://chat.whatsapp.com/TRUTH12345'},
        sync_result={
            'ok': True,
            'trust_status': 'TRUSTED_CONFIRMED_PENDING',
            'trusted_pending_count': 3,
            'pending_count': 3,
            'ui_pending_count': 3,
            'api_pending_count': 3,
            'requester_ids': ['u1', 'u2'],
            'source': 'manual_full_sync',
        },
        source_priority=100,
        observed_at=datetime.now(timezone.utc).isoformat(),
    )

    assert result['written'] is False
    assert result['reason'] == 'requester_ids_incomplete'


def test_current_truth_guard_rejects_pending_without_strong_runtime_capability_evidence():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)

    result = service.upsert_approval_queue_current_truth(
        account_key='registration-truth',
        binding={'link': 'https://chat.whatsapp.com/TRUTH12345'},
        sync_result={
            'ok': True,
            'trust_status': 'TRUSTED_CONFIRMED_PENDING',
            'trusted_pending_count': 2,
            'pending_count': 2,
            'ui_pending_count': 2,
            'api_pending_count': 2,
            'requester_ids': ['u1', 'u2'],
            'group_identity_verified': True,
            'runtime_identity_match': True,
            'session_authenticated': True,
            'self_participant_found': True,
            'self_is_admin': False,
            'can_manage_membership_requests': False,
            'review_surface_ready': True,
            'source': 'manual_full_sync',
        },
        source_priority=100,
        observed_at=datetime.now(timezone.utc).isoformat(),
    )

    assert result['written'] is False
    assert result['reason'] == 'approval_capability_required'



def test_current_truth_guard_accepts_promoted_authoritative_requester_ids_without_review_surface():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)

    result = service.upsert_approval_queue_current_truth(
        account_key='registration-truth',
        binding={'link': 'https://chat.whatsapp.com/TRUTH12345'},
        sync_result={
            'ok': True,
            'trust_status': 'TRUSTED_CONFIRMED_PENDING',
            'trusted_pending_count': 2,
            'pending_count': 2,
            'ui_pending_count': 0,
            'api_pending_count': 2,
            'requester_ids': ['u1', 'u2'],
            'group_identity_verified': True,
            'runtime_identity_match': True,
            'session_authenticated': True,
            'self_participant_found': True,
            'self_is_admin': True,
            'can_manage_membership_requests': True,
            'review_surface_ready': False,
            'reason_code': 'authoritative_requester_ids_confirmed_pending',
            'source': {
                'mode': 'requester_ids_direct',
                'fallback_mode': 'executor_group_state_fallback',
            },
            'authoritative_requester_ids_promoted': True,
            'fingerprint_quality': 'strong',
        },
        source_priority=100,
        observed_at=datetime.now(timezone.utc).isoformat(),
    )

    assert result['written'] is True
    snapshots = service._load_approval_binding_queue_snapshots('registration-truth', {'link': 'https://chat.whatsapp.com/TRUTH12345'})
    assert snapshots['current_truth']['trust_status'] == 'TRUSTED_CONFIRMED_PENDING'
    assert snapshots['current_truth']['trusted_pending_count'] == 2
    assert snapshots['current_truth']['can_manual_approve'] is True


def test_current_truth_low_priority_can_replace_expired_high_priority_snapshot():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    old = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    _insert_queue_snapshot(db, trust_status='TRUSTED_CONFIRMED_PENDING', pending_count=3, checked_at=old, source_priority=100)

    result = service.upsert_approval_queue_current_truth(
        account_key='registration-truth',
        binding={'link': 'https://chat.whatsapp.com/TRUTH12345'},
        sync_result={
            'ok': True,
            'trust_status': 'TRUSTED_CONFIRMED_PENDING',
            'trusted_pending_count': 9,
            'ui_pending_count': 9,
            'api_pending_count': 9,
            'requester_ids': [f'u{i}' for i in range(9)],
            'group_identity_verified': True,
            'runtime_identity_match': True,
            'session_authenticated': True,
            'self_participant_found': True,
            'self_is_admin': True,
            'can_manage_membership_requests': True,
            'review_surface_ready': True,
            'fingerprint': 'fresh-9',
            'source': 'scheduled_full_sync',
        },
        source_priority=60,
        observed_at=datetime.now(timezone.utc).isoformat(),
    )

    assert result['written'] is True
    current = service._load_approval_binding_queue_snapshots('registration-truth', {'link': 'https://chat.whatsapp.com/TRUTH12345'})['current_truth']
    assert current['trusted_pending_count'] == 9
    assert current['source_priority'] == 60


def test_current_truth_lower_priority_does_not_overwrite_fresh_manual_snapshot():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    _insert_queue_snapshot(db, trust_status='TRUSTED_CONFIRMED_PENDING', pending_count=15, source_priority=100)

    result = service.upsert_approval_queue_current_truth(
        account_key='registration-truth',
        binding={'link': 'https://chat.whatsapp.com/TRUTH12345'},
        sync_result={
            'ok': True,
            'trust_status': 'TRUSTED_CONFIRMED_PENDING',
            'trusted_pending_count': 4,
            'ui_pending_count': 4,
            'api_pending_count': 4,
            'requester_ids': ['u1', 'u2', 'u3', 'u4'],
        },
        source_priority=60,
        observed_at=datetime.now(timezone.utc).isoformat(),
    )

    assert result['written'] is False
    current = service._load_approval_binding_queue_snapshots('registration-truth', {'link': 'https://chat.whatsapp.com/TRUTH12345'})['current_truth']
    assert current['trusted_pending_count'] == 15
    assert current['source_priority'] == 100


def test_full_queue_sync_writes_current_truth_and_latest_probe(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'full_queue_sync', lambda **kwargs: {
        'ok': True,
        'trust_status': 'TRUSTED_CONFIRMED_PENDING',
        'trusted_pending_count': 11,
        'ui_pending_count': 11,
        'api_pending_count': 11,
        'requester_ids': [f'u{i}' for i in range(11)],
        'group_identity_verified': True,
        'runtime_identity_match': True,
        'session_authenticated': True,
        'self_participant_found': True,
        'self_is_admin': True,
        'can_manage_membership_requests': True,
        'review_surface_ready': True,
        'fingerprint': 'fp-11',
        'converged': True,
    })

    result = service.full_sync_whatsapp_approval_binding('registration-truth', 0, source='manual_full_sync')

    assert result['ok'] is True
    assert result['trust_status'] == 'TRUSTED_CONFIRMED_PENDING'
    snapshots = service._load_approval_binding_queue_snapshots('registration-truth', {'link': 'https://chat.whatsapp.com/TRUTH12345'})
    assert snapshots['current_truth']['trusted_pending_count'] == 11
    assert snapshots['latest_probe']['trusted_pending_count'] == 11


def test_full_queue_sync_returns_structured_truth_acquisition_result(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'full_queue_sync', lambda **kwargs: {
        'ok': True,
        'trust_status': 'TRUSTED_CONFIRMED_PENDING',
        'trusted_pending_count': 3,
        'pending_count': 3,
        'ui_pending_count': 3,
        'api_pending_count': 3,
        'requester_ids': ['u1', 'u2', 'u3'],
        'group_identity_verified': True,
        'runtime_identity_match': True,
        'session_authenticated': True,
        'self_participant_found': True,
        'self_is_admin': True,
        'can_manage_membership_requests': True,
        'review_surface_ready': True,
        'fingerprint': 'fp-3',
        'converged': True,
    })

    result = service.full_sync_whatsapp_approval_binding('registration-truth', 0, source='manual_full_sync')

    assert result['truth_acquisition_id']
    assert result['final_state'] == 'COMMIT_TRUTH_PENDING'
    assert result['failure_class'] == 'NONE'
    assert result['recommended_action'] == 'NONE'
    assert result['authoritative_source'] == 'worker_full_queue_sync'
    assert result['current_truth_written'] is True
    assert result['latest_probe_written'] is True
    assert result['commit_target'] == 'current_truth'
    assert result['trigger'] == 'manual_full_sync'
    assert isinstance(result['stages'], list)
    assert [stage['stage'] for stage in result['stages']]
    assert any(stage['stage'] == 'worker_sync' and stage['status'] == 'completed' for stage in result['stages'])
    assert any(stage['stage'] == 'write_current_truth' and stage['status'] == 'completed' for stage in result['stages'])


def test_full_queue_sync_singleflight_reuses_inflight_acquisition(tmp_path, monkeypatch):
    db = Database(str(tmp_path / 'truth_singleflight.sqlite3'))
    service = Service(db)
    _insert_registration_account_with_binding(db)
    started = threading.Event()
    release = threading.Event()
    call_count = {'count': 0}

    def blocking_full_sync(**kwargs):
        call_count['count'] += 1
        started.set()
        release.wait(timeout=5)
        return {
            'ok': True,
            'trust_status': 'TRUSTED_CONFIRMED_PENDING',
            'trusted_pending_count': 2,
            'pending_count': 2,
            'ui_pending_count': 2,
            'api_pending_count': 2,
            'requester_ids': ['u1', 'u2'],
            'group_identity_verified': True,
            'runtime_identity_match': True,
            'session_authenticated': True,
            'self_participant_found': True,
            'self_is_admin': True,
            'can_manage_membership_requests': True,
            'review_surface_ready': True,
            'fingerprint': 'fp-2',
            'converged': True,
        }

    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'full_queue_sync', blocking_full_sync)
    results = []

    def run_sync():
        results.append(service.full_sync_whatsapp_approval_binding('registration-truth', 0, source='manual_full_sync'))

    worker = threading.Thread(target=run_sync)
    worker.start()
    assert started.wait(timeout=5)

    second_result = service.full_sync_whatsapp_approval_binding('registration-truth', 0, source='manual_full_sync')
    release.set()
    worker.join(timeout=5)

    assert call_count['count'] == 1
    assert len(results) == 1
    first_result = results[0]
    assert first_result['truth_acquisition_id'] == second_result['truth_acquisition_id']
    assert first_result['final_state'] == 'COMMIT_TRUTH_PENDING'
    assert second_result['final_state'] == 'COMMIT_TRUTH_PENDING'
    assert second_result['truth_acquisition_reused'] is True


def test_full_queue_sync_falls_back_to_executor_group_state_when_worker_sync_throws(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)

    def blow_up(**kwargs):
        raise RuntimeError('worker full sync 500')

    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'full_queue_sync', blow_up)
    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'registration_group_executor_state', lambda **kwargs: {
        'group_id': 'truth-group@g.us',
        'group_name': 'Truth Group',
        'pending_count': 3,
        'member_count': 123,
        'requester_ids': ['u1', 'u2', 'u3'],
        'requesters': [{'id': 'u1'}, {'id': 'u2'}, {'id': 'u3'}],
    })

    result = service.full_sync_whatsapp_approval_binding('registration-truth', 0, source='manual_approve_preflight')

    assert result['ok'] is False
    assert result['trust_status'] == 'TRUTH_UNKNOWN'
    assert result['reason_code'] == 'api_pending_ui_not_converged'
    assert result['pending_count'] == 3
    assert result['can_manual_approve'] is False
    assert result['final_state'] == 'TRUTH_ACQUISITION_FAILED'
    assert result['failure_class'] == 'UI_NOT_CONVERGED'
    assert result['recommended_action'] == 'REPAIR_UI_ACTION_SURFACE'
    snapshots = service._load_approval_binding_queue_snapshots('registration-truth', {'link': 'https://chat.whatsapp.com/TRUTH12345'})
    assert snapshots['current_truth'] is None
    assert snapshots['latest_probe']['pending_count'] == 3
    assert snapshots['latest_probe']['reason_code'] == 'api_pending_ui_not_converged'


def test_full_queue_sync_falls_back_to_executor_group_state_when_worker_sync_is_inconclusive(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'full_queue_sync', lambda **kwargs: {
        'ok': False,
        'trust_status': 'UNTRUSTED_SYNC_INCONCLUSIVE',
        'reason_code': 'ui_api_not_converged',
        'ui_pending_count': 0,
        'api_pending_count': 0,
        'requester_ids': [],
        'fingerprint': 'truth-group@g.us|0|0',
        'converged': False,
    })
    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'registration_group_executor_state', lambda **kwargs: {
        'group_id': 'truth-group@g.us',
        'group_name': 'Truth Group',
        'pending_count': 2,
        'member_count': 123,
        'requester_ids': ['u1', 'u2'],
        'requesters': [{'id': 'u1'}, {'id': 'u2'}],
    })

    result = service.full_sync_whatsapp_approval_binding('registration-truth', 0, source='manual_approve_preflight')

    assert result['ok'] is False
    assert result['trust_status'] == 'TRUTH_UNKNOWN'
    assert result['reason_code'] == 'api_pending_ui_not_converged'
    assert result['pending_count'] == 2
    assert result['can_manual_approve'] is False
    assert result['final_state'] == 'TRUTH_ACQUISITION_FAILED'
    assert result['failure_class'] == 'UI_NOT_CONVERGED'
    assert result['recommended_action'] == 'REPAIR_UI_ACTION_SURFACE'
    snapshots = service._load_approval_binding_queue_snapshots('registration-truth', {'link': 'https://chat.whatsapp.com/TRUTH12345'})
    assert snapshots['current_truth'] is None
    assert snapshots['latest_probe']['pending_count'] == 2
    assert snapshots['latest_probe']['reason_code'] == 'api_pending_ui_not_converged'


def test_full_queue_sync_manual_preflight_blocks_manual_approve_after_slow_fallback(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'full_queue_sync', lambda **kwargs: {
        'ok': False,
        'trust_status': 'UNTRUSTED_API_STALE',
        'reason_code': 'ui_empty_api_has_historical_requests',
        'ui_pending_count': 0,
        'api_pending_count': 0,
        'requester_ids': [],
        'fingerprint': 'truth-group@g.us|0|0',
        'converged': False,
    })
    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'registration_group_executor_state', lambda **kwargs: {
        'group_id': 'truth-group@g.us',
        'group_name': 'Truth Group',
        'pending_count': 4,
        'member_count': 123,
        'requester_ids': ['u1', 'u2', 'u3', 'u4'],
        'requesters': [{'id': 'u1'}, {'id': 'u2'}, {'id': 'u3'}, {'id': 'u4'}],
    })
    observed_at = '2026-05-27T04:38:04.133737+00:00'
    monkeypatch.setattr(app_main, 'utc_now', lambda: observed_at)

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 5, 27, 4, 38, 41, 215634, tzinfo=timezone.utc)

    monkeypatch.setattr(app_main, 'datetime', FakeDateTime)

    result = service.full_sync_whatsapp_approval_binding('registration-truth', 0, source='manual_approve_preflight')

    assert result['ok'] is False
    assert result['trust_status'] == 'TRUTH_UNKNOWN'
    assert result['reason_code'] == 'api_pending_ui_not_converged'
    assert result['pending_count'] == 4
    assert result['can_manual_approve'] is False
    assert result['final_state'] == 'TRUTH_ACQUISITION_FAILED'
    assert result['failure_class'] == 'UI_NOT_CONVERGED'
    assert result['recommended_action'] == 'REPAIR_UI_ACTION_SURFACE'


def test_full_queue_sync_reclassifies_api_positive_ui_zero_as_ui_not_converged(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    observed_at = datetime.now(timezone.utc).isoformat()
    binding = dict(service._get_whatsapp_approval_account_runtime_row('registration-truth').get('group_binding_runtimes')[0])
    for _ in range(2):
        service.upsert_approval_queue_latest_probe(
            account_key='registration-truth',
            binding=binding,
            probe_result={
                'ok': False,
                'trust_status': 'UNTRUSTED_API_STALE',
                'reason_code': 'ui_empty_api_has_historical_requests',
                'ui_pending_count': 0,
                'api_pending_count': 4,
                'requester_ids': ['u1', 'u2', 'u3', 'u4'],
                'group_identity_verified': True,
                'runtime_identity_match': True,
                'session_authenticated': True,
                'self_participant_found': True,
                'self_is_admin': True,
                'can_manage_membership_requests': True,
                'fingerprint': 'stable-ui-gap',
                'fingerprint_quality': 'strong',
            },
            observed_at=observed_at,
        )
    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'full_queue_sync', lambda **kwargs: {
        'ok': False,
        'trust_status': 'UNTRUSTED_API_STALE',
        'reason_code': 'ui_empty_api_has_historical_requests',
        'ui_pending_count': 0,
        'api_pending_count': 4,
        'pending_count': 4,
        'requester_ids': ['u1', 'u2', 'u3', 'u4'],
        'group_identity_verified': True,
        'runtime_identity_match': True,
        'session_authenticated': True,
        'self_participant_found': True,
        'self_is_admin': True,
        'can_manage_membership_requests': True,
        'review_surface_ready': False,
        'fingerprint': 'stable-ui-gap',
        'fingerprint_quality': 'strong',
        'converged': False,
    })
    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'registration_group_executor_state', lambda **kwargs: {})

    result = service.full_sync_whatsapp_approval_binding('registration-truth', 0, source='manual_approve_preflight')

    assert result['trust_status'] == 'UNTRUSTED_UI_NOT_CONVERGED'
    assert result['reason_code'] == 'untrusted_ui_not_converged'
    assert result['failure_class'] == 'UI_NOT_CONVERGED'
    assert result['recommended_action'] == 'REPAIR_UI_ACTION_SURFACE'
    assert result['manual_override_eligible'] is True
    assert result['fingerprint_stable'] is True


def test_full_queue_sync_executor_fallback_reuses_prior_capability_evidence_for_manual_approve(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    observed_at = datetime.now(timezone.utc).isoformat()
    binding = dict(service._get_whatsapp_approval_account_runtime_row('registration-truth').get('group_binding_runtimes')[0])
    service.upsert_approval_queue_latest_probe(
        account_key='registration-truth',
        binding=binding,
        probe_result={
            'ok': True,
            'trust_status': 'TRUSTED_CONFIRMED_PENDING',
            'reason_code': 'trusted_pending',
            'trusted_pending_count': 2,
            'pending_count': 2,
            'ui_pending_count': 2,
            'api_pending_count': 2,
            'requester_ids': ['u1', 'u2'],
            'requesters': [{'id': 'u1'}, {'id': 'u2'}],
            'group_id': 'truth-group@g.us',
            'group_name': 'Truth Group',
            'group_identity_verified': True,
            'runtime_identity_match': True,
            'session_authenticated': True,
            'self_participant_found': True,
            'self_is_admin': True,
            'can_manage_membership_requests': True,
            'review_surface_ready': True,
            'fingerprint': 'prior-strong-capability',
            'fingerprint_quality': 'strong',
        },
        observed_at=observed_at,
    )
    service.upsert_approval_queue_latest_probe(
        account_key='registration-truth',
        binding=binding,
        probe_result={
            'ok': False,
            'trust_status': 'TRUTH_UNKNOWN',
            'reason_code': 'executor_group_state_fallback_pending_only',
            'pending_count': 2,
            'ui_pending_count': 0,
            'api_pending_count': 2,
            'requester_ids': ['u1', 'u2'],
            'requesters': [{'id': 'u1'}, {'id': 'u2'}],
            'group_identity_verified': True,
            'runtime_identity_match': True,
            'session_authenticated': True,
            'self_participant_found': None,
            'self_is_admin': None,
            'can_manage_membership_requests': None,
            'fingerprint': 'prior-strong-capability',
            'fingerprint_quality': 'strong',
        },
        observed_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
    )
    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'full_queue_sync', lambda **kwargs: (_ for _ in ()).throw(RuntimeError('worker full sync 500')))
    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'registration_group_executor_state', lambda **kwargs: {
        'pending_count': 2,
        'requester_ids': ['u1', 'u2'],
        'requesters': [{'id': 'u1'}, {'id': 'u2'}],
    })

    result = service.full_sync_whatsapp_approval_binding('registration-truth', 0, source='manual_approve_preflight')

    assert result['trust_status'] == 'TRUSTED_CONFIRMED_PENDING'
    assert result['reason_code'] == 'authoritative_requester_ids_confirmed_pending'
    assert result['manual_override_eligible'] is True
    assert result['can_manual_approve'] is True
    assert result['self_participant_found'] is True
    assert result['self_is_admin'] is True
    assert result['can_manage_membership_requests'] is True
    snapshots = service._load_approval_binding_queue_snapshots('registration-truth', {'link': 'https://chat.whatsapp.com/TRUTH12345'})
    assert snapshots['current_truth']['trust_status'] == 'TRUSTED_CONFIRMED_PENDING'
    assert snapshots['current_truth']['trusted_pending_count'] == 2


def test_manual_approve_allows_api_positive_override_when_enabled(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    service.whatsapp_approval_api_positive_override_enabled = True
    _insert_registration_account_with_binding(db)
    approval_payloads = []
    full_sync_sources = []

    def _full_sync(*args, **kwargs):
        full_sync_sources.append(kwargs.get('source'))
        if kwargs.get('source') == 'approval_after_sync':
            return {
                'ok': True,
                'trust_status': 'TRUSTED_CONFIRMED_EMPTY',
                'reason_code': 'queue_drained_after_approval',
                'pending_count': 0,
                'trusted_pending_count': 0,
                'ui_pending_count': 0,
                'api_pending_count': 0,
                'member_count': 5,
                'requester_ids': [],
                'requesters': [],
                'group_id': 'truth-group@g.us',
                'group_name': 'Truth Group',
                'group_identity_verified': True,
                'runtime_identity_match': True,
                'session_authenticated': True,
                'self_participant_found': True,
                'self_is_admin': True,
                'can_manage_membership_requests': True,
                'fingerprint_quality': 'strong',
                'recommended_action': 'NONE',
            }
        return {
            'ok': False,
            'trust_status': 'UNTRUSTED_UI_NOT_CONVERGED',
            'reason_code': 'api_pending_ui_not_converged',
            'can_manual_approve': False,
            'manual_override_eligible': True,
            'manual_override_mode': 'requester_ids_direct',
            'manual_override_issues': [],
            'pending_count': 2,
            'api_pending_count': 2,
            'ui_pending_count': 0,
            'member_count': 5,
            'requester_ids': ['r1', 'r2'],
            'requesters': [{'requesterId': 'r1'}, {'requesterId': 'r2'}],
            'group_identity_verified': True,
            'runtime_identity_match': True,
            'session_authenticated': True,
            'self_participant_found': True,
            'self_is_admin': True,
            'can_manage_membership_requests': True,
            'fingerprint_quality': 'strong',
            'recommended_action': 'REPAIR_UI_ACTION_SURFACE',
        }

    monkeypatch.setattr(service, 'refresh_whatsapp_approval_binding_probe', lambda *a, **k: {
        'binding_runtime': {
            'binding_id': 'wabind-1',
            'identity_status': 'resolved',
            'registration_group': 'truth-group@g.us',
            'group_id': 'truth-group@g.us',
            'group_name': 'Truth Group',
        },
        'probe': {'group_id': 'truth-group@g.us', 'group_name': 'Truth Group', 'pending_count': 2},
    })
    monkeypatch.setattr(service, 'full_sync_whatsapp_approval_binding', _full_sync)
    monkeypatch.setattr(
        service.whatsapp_approval_runtime_adapter,
        'execute_registration_group_approval',
        lambda *, service, payload: (
            approval_payloads.append(payload) or {
                'status': 'success',
                'verified': True,
                'crm_recorded': True,
                'result_code': 'approved',
                'approval_run_id': 'run-override',
                'approved_count': 2,
                'raw_result': {'pending_after': 0, 'member_count_after': 5},
            }
        ),
    )
    monkeypatch.setattr(
        service.whatsapp_approval_runtime_adapter,
        'registration_group_executor_state',
        lambda **kwargs: (_ for _ in ()).throw(AssertionError('post-approve should reuse full_sync verify before executor fallback')),
    )
    monkeypatch.setattr(service, '_sync_manual_registration_group_approval_to_production_ops_state', lambda *a, **k: None)
    monkeypatch.setattr(service, '_send_registration_group_binding_notification', lambda *a, **k: {'status': 'skipped_not_success', 'code': 'manual_approval_succeeded'})

    result = service.manual_approve_whatsapp_approval_binding(
        'registration-truth',
        0,
        audit_context={
            'operator': {'user_id': 'ops', 'username': 'ops', 'display_name': 'ops', 'role': 'super_admin'},
            'request': {'request_id': 'approval-override', 'allow_api_positive_override': True},
        },
    )

    assert approval_payloads
    assert approval_payloads[0].registration_group == 'truth-group@g.us'
    assert result['manual_override_used'] is True
    assert result['manual_override_mode'] == 'requester_ids_direct'
    assert result['approval_run_id'] == 'run-override'
    assert full_sync_sources == ['manual_approve_preflight', 'approval_after_sync']


def test_manual_approve_preflight_block_returns_structured_message(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    monkeypatch.setattr(service, 'refresh_whatsapp_approval_binding_probe', lambda *a, **k: {
        'binding_runtime': {
            'binding_id': 'wabind-1',
            'identity_status': 'resolved',
            'registration_group': 'truth-group@g.us',
            'group_id': 'truth-group@g.us',
            'group_name': 'Truth Group',
        },
        'probe': {'group_id': 'truth-group@g.us', 'group_name': 'Truth Group', 'pending_count': 2},
    })
    monkeypatch.setattr(service, 'full_sync_whatsapp_approval_binding', lambda *a, **k: {
        'ok': False,
        'trust_status': 'UNTRUSTED_UI_NOT_CONVERGED',
        'reason_code': 'api_pending_ui_not_converged',
        'can_manual_approve': False,
        'manual_override_eligible': True,
        'manual_override_mode': 'requester_ids_direct',
        'manual_override_issues': [],
        'recommended_action': 'REPAIR_UI_ACTION_SURFACE',
        'failure_class': 'UI_NOT_CONVERGED',
    })

    with pytest.raises(app_main.HTTPException) as exc_info:
        service.manual_approve_whatsapp_approval_binding(
            'registration-truth',
            0,
            audit_context={
                'operator': {'user_id': 'ops', 'username': 'ops', 'display_name': 'ops', 'role': 'super_admin'},
                'request': {'request_id': 'approval-blocked'},
            },
        )

    detail = exc_info.value.detail
    assert exc_info.value.status_code == 409
    assert detail['reason'] == 'manual_approval_full_sync_not_trusted'
    assert detail['failure_class'] == 'UI_NOT_CONVERGED'
    assert detail['stage_code'] == 'preflight_blocked'
    assert detail['recommended_action'] == 'REPAIR_UI_ACTION_SURFACE'
    assert '审批面未收敛' in detail['message']
    assert '完整同步' in detail['message']


def test_runtime_actor_serializes_refresh_and_full_sync_for_same_account(tmp_path, monkeypatch):
    db = Database(str(tmp_path / 'runtime_actor.sqlite3'))
    service = Service(db)
    _insert_registration_account_with_binding(db)
    probe_started = threading.Event()
    release_probe = threading.Event()
    full_sync_started = threading.Event()
    errors = []

    monkeypatch.setattr(service, '_production_ops_daemon_snapshot', lambda: {})
    original_get_runtime_row = service._get_whatsapp_approval_account_runtime_row

    def _runtime_row(account_key):
        row = dict(original_get_runtime_row(account_key))
        row['provider_name'] = 'webjs'
        row['runtime_state'] = {'provider_name': 'webjs', 'ready': True, 'authenticated': True, 'base_url': 'http://127.0.0.1:57617'}
        row['session_state'] = {'login_verified': True, 'authenticated': True, 'ready': True}
        return row

    monkeypatch.setattr(service, '_get_whatsapp_approval_account_runtime_row', _runtime_row)

    def blocking_probe(**kwargs):
        probe_started.set()
        release_probe.wait(timeout=5)
        return {
            'group_id': 'truth-group@g.us',
            'group_name': 'Truth Group',
            'pending_count': 0,
            'member_count': 5,
            'self_participant_found': True,
            'self_is_admin': True,
            'can_manage_membership_requests': True,
        }

    def tracked_full_sync(**kwargs):
        full_sync_started.set()
        return {
            'ok': True,
            'trust_status': 'TRUSTED_CONFIRMED_PENDING',
            'trusted_pending_count': 1,
            'pending_count': 1,
            'ui_pending_count': 1,
            'api_pending_count': 1,
            'requester_ids': ['u1'],
            'group_identity_verified': True,
            'runtime_identity_match': True,
            'session_authenticated': True,
            'self_participant_found': True,
            'self_is_admin': True,
            'can_manage_membership_requests': True,
            'review_surface_ready': True,
            'fingerprint': 'fp-actor',
            'fingerprint_quality': 'strong',
            'converged': True,
        }

    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'probe_binding_group_state', blocking_probe)
    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'full_queue_sync', tracked_full_sync)

    def run_refresh():
        try:
            service.refresh_whatsapp_approval_binding_probe('registration-truth', 0)
        except Exception as exc:
            errors.append(exc)

    def run_full_sync():
        try:
            service.full_sync_whatsapp_approval_binding('registration-truth', 0, source='manual_full_sync')
        except Exception as exc:
            errors.append(exc)

    refresh_thread = threading.Thread(target=run_refresh)
    sync_thread = threading.Thread(target=run_full_sync)
    refresh_thread.start()
    assert probe_started.wait(timeout=5)
    sync_thread.start()
    time.sleep(0.3)
    assert full_sync_started.is_set() is False
    release_probe.set()
    refresh_thread.join(timeout=5)
    sync_thread.join(timeout=5)

    assert not errors
    assert full_sync_started.is_set() is True


def test_full_queue_sync_zero_fallback_becomes_empty_unverified_not_trusted_zero(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'full_queue_sync', lambda **kwargs: {
        'ok': False,
        'trust_status': 'UNTRUSTED_SYNC_INCONCLUSIVE',
        'reason_code': 'ui_api_not_converged',
        'ui_pending_count': 0,
        'api_pending_count': 0,
        'requester_ids': [],
        'fingerprint': 'truth-group@g.us|0|0',
        'converged': False,
        'source': 'manual_full_sync',
    })
    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'registration_group_executor_state', lambda **kwargs: {
        'group_id': 'truth-group@g.us',
        'group_name': 'Truth Group',
        'pending_count': 0,
        'member_count': 123,
        'requester_ids': [],
        'requesters': [],
    })
    monkeypatch.setattr(service, 'recover_whatsapp_approval_account_runtime', lambda account_key: {'started': False})

    result = service.full_sync_whatsapp_approval_binding('registration-truth', 0, source='manual_full_sync')

    assert result['trust_status'] == 'EMPTY_UNVERIFIED'
    assert result['can_manual_approve'] is False
    snapshots = service._load_approval_binding_queue_snapshots('registration-truth', {'link': 'https://chat.whatsapp.com/TRUTH12345'})
    assert snapshots['current_truth'] is None
    assert snapshots['latest_probe']['trust_status'] == 'EMPTY_UNVERIFIED'


def test_refresh_probe_resolves_registration_binding_via_executor_fallback_when_live_probe_has_name_only(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(
        db,
        registration_group='120363399574259864@g.us',
        identity_status='unresolved',
        binding_id='wabind_probe_fallback',
    )
    with db.connect() as conn:
        row = conn.execute('SELECT group_links FROM whatsapp_approval_accounts WHERE account_key=?', ('registration-truth',)).fetchone()
        bindings = json.loads(row['group_links'])
        bindings[0].update({
            'registration_group': '',
            'group_id': '120363399574259864@g.us',
            'group_name': 'Stale 23 Group Name',
            'identity_status': 'unresolved',
            'runtime_probe_group_id': '',
            'runtime_probe_group_name': '',
            'last_probe_status': 'identity_unresolved',
            'last_probe_reason': 'identity_unresolved',
        })
        conn.execute(
            'UPDATE whatsapp_approval_accounts SET group_links=?, updated_at=? WHERE account_key=?',
            (json.dumps(bindings, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), 'registration-truth'),
        )
        conn.commit()

    account_row = {
        'account_key': 'registration-truth',
        'responsible_type': 'registration_group',
        'group_binding_runtimes': [dict(bindings[0])],
        'runtime_state': {'base_url': 'http://127.0.0.1:57617'},
        'session_state': {},
        'membership_verifier': {},
    }

    monkeypatch.setattr(service, '_get_whatsapp_approval_account_runtime_row', lambda account_key: account_row)
    monkeypatch.setattr(service, '_production_ops_daemon_snapshot', lambda: {})
    monkeypatch.setattr(
        service.whatsapp_approval_runtime_adapter,
        'probe_binding_group_state',
        lambda **kwargs: {
            'group_name': '🇮🇩 23- Grup Registrasi Resmi ✘ Linky 💎',
            'pending_count': 7,
            'member_count': 551,
            'self_participant_found': True,
            'self_is_admin': True,
            'can_manage_membership_requests': True,
        },
    )
    monkeypatch.setattr(
        service,
        'registration_group_approval_executor_group_state',
        lambda group, allow_legacy_target=True: {
            'group_id': '120363399574259864@g.us',
            'group_name': '🇮🇩 23- Grup Registrasi Resmi ✘ Linky 💎',
            'pending_count': 7,
            'member_count': 551,
            'requester_ids': ['u1', 'u2'],
            'requesters': [{'requesterId': 'u1'}, {'requesterId': 'u2'}],
            'self_participant_found': True,
            'self_is_admin': True,
            'can_manage_membership_requests': True,
            'approval_state_status': 'confirmed_pending',
        },
    )

    result = service.refresh_whatsapp_approval_binding_probe('registration-truth', 0)

    runtime = result['binding_runtime']
    assert runtime['identity_status'] == 'resolved'
    assert runtime['registration_group'] == '120363399574259864@g.us'
    assert runtime['group_id'] == '120363399574259864@g.us'
    assert runtime['runtime_probe_group_id'] == '120363399574259864@g.us'
    assert runtime['runtime_probe_group_name'] == '🇮🇩 23- Grup Registrasi Resmi ✘ Linky 💎'

    with db.connect() as conn:
        row = conn.execute('SELECT group_links FROM whatsapp_approval_accounts WHERE account_key=?', ('registration-truth',)).fetchone()
    persisted = json.loads(row['group_links'])[0]
    assert persisted['identity_status'] == 'resolved'
    assert persisted['registration_group'] == '120363399574259864@g.us'
    assert persisted['runtime_probe_group_id'] == '120363399574259864@g.us'
    assert persisted['last_probe_reason'] == 'resolved'



def test_refresh_whatsapp_approval_binding_probe_uses_single_fast_probe_when_truth_is_fresh(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(
        db,
        registration_group='truth-group@g.us',
        identity_status='resolved',
        binding_id='wabind_probe_fast_refresh',
    )
    binding = {'link': 'https://chat.whatsapp.com/TRUTH12345', 'registration_group': 'truth-group@g.us'}
    service.upsert_approval_queue_current_truth(
        account_key='registration-truth',
        binding=binding,
        sync_result={
            'ok': True,
            'trust_status': 'TRUSTED_CONFIRMED_PENDING',
            'trusted_pending_count': 1,
            'pending_count': 1,
            'ui_pending_count': 1,
            'api_pending_count': 1,
            'requester_ids': ['u1'],
            'group_identity_verified': True,
            'runtime_identity_match': True,
            'session_authenticated': True,
            'self_participant_found': True,
            'self_is_admin': True,
            'can_manage_membership_requests': True,
            'review_surface_ready': True,
            'fingerprint': 'fresh-probe',
            'fingerprint_quality': 'strong',
            'source': 'manual_full_sync',
        },
        source_priority=100,
        observed_at=datetime.now(timezone.utc).isoformat(),
    )
    monkeypatch.setattr(service, '_production_ops_daemon_snapshot', lambda: {})
    original_get_runtime_row = service._get_whatsapp_approval_account_runtime_row

    def _runtime_row(account_key):
        row = dict(original_get_runtime_row(account_key))
        row['provider_name'] = 'webjs'
        row['runtime_state'] = {'provider_name': 'webjs', 'ready': True, 'authenticated': True, 'base_url': 'http://127.0.0.1:57617'}
        row['session_state'] = {'login_verified': True, 'authenticated': True, 'ready': True}
        return row

    monkeypatch.setattr(service, '_get_whatsapp_approval_account_runtime_row', _runtime_row)
    captured = {}

    def _probe(**kwargs):
        captured['attempts'] = kwargs.get('attempts')
        captured['timeout_seconds'] = kwargs.get('timeout_seconds')
        return {
            'group_id': 'truth-group@g.us',
            'group_name': 'Truth Group',
            'pending_count': 1,
            'member_count': 5,
            'self_participant_found': True,
            'self_is_admin': True,
            'can_manage_membership_requests': True,
        }

    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'probe_binding_group_state', _probe)
    monkeypatch.setattr(service, 'registration_group_approval_executor_group_state', lambda *a, **k: {})

    result = service.refresh_whatsapp_approval_binding_probe('registration-truth', 0)

    assert result['binding_runtime']['identity_status'] == 'resolved'
    assert captured['attempts'] == 1
    assert captured['timeout_seconds'] == 12.0


def test_refresh_whatsapp_approval_binding_probe_blocks_unhealthy_baileys_runtime_before_hitting_worker(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db, binding_id='wabind_probe_blocked')

    with db.connect() as conn:
        row = conn.execute('SELECT group_links FROM whatsapp_approval_accounts WHERE account_key=?', ('registration-truth',)).fetchone()
        bindings = json.loads(row['group_links'])
        bindings[0]['provider_name'] = 'baileys'
        conn.execute(
            'UPDATE whatsapp_approval_accounts SET group_links=?, updated_at=? WHERE account_key=?',
            (json.dumps(bindings, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), 'registration-truth'),
        )
        conn.commit()

    account_row = {
        'account_key': 'registration-truth',
        'enabled': True,
        'provider_name': 'baileys',
        'responsible_type': 'registration_group',
        'group_binding_runtimes': [dict(bindings[0])],
        'runtime_state': {
            'provider_name': 'baileys',
            'base_url': 'http://127.0.0.1:57617',
            'configured': True,
            'active': False,
            'status': 'stopped',
            'health_error': 'connection refused',
        },
        'session_state': {
            'login_verified': False,
            'authenticated': False,
            'ready': False,
            'login_check_status': 'runtime_unavailable',
        },
        'membership_verifier': {},
    }

    monkeypatch.setattr(service, '_get_whatsapp_approval_account_runtime_row', lambda account_key: account_row)

    called = {'probe': 0}

    def _unexpected_probe(**kwargs):
        called['probe'] += 1
        raise AssertionError('worker probe should not run when runtime is not probe-ready')

    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'probe_binding_group_state', _unexpected_probe)

    with pytest.raises(app_main.HTTPException) as exc_info:
        service.refresh_whatsapp_approval_binding_probe('registration-truth', 0)

    detail = exc_info.value.detail
    assert exc_info.value.status_code == 409
    assert detail['reason'] == 'whatsapp_runtime_not_probe_ready'
    assert detail['reason_code'] == 'runtime_unhealthy'
    assert detail['login_action'] == 'manual_recover'
    assert detail['login_state_label'] == '运行服务异常，请手动恢复'
    assert called['probe'] == 0



def test_full_queue_sync_falls_back_to_pending_truth_snapshot_when_live_group_state_times_out(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)

    def blow_up(**kwargs):
        raise RuntimeError('worker full sync 500')

    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'full_queue_sync', blow_up)
    monkeypatch.setattr(service.whatsapp_approval_runtime_adapter, 'registration_group_executor_state', lambda **kwargs: (_ for _ in ()).throw(RuntimeError('group_state timeout')))
    checked_at = datetime.now(timezone.utc).isoformat()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO mcn_truth_snapshots (
                snapshot_id, object_type, object_key, snapshot_type, truth_status,
                confidence, confidence_reason, facts_json, source_json, checked_at,
                expires_at, recommended_action, updated_at
            ) VALUES (?, 'registration_group_binding', ?, 'pending_truth', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'pending-truth:registration-truth:https://chat.whatsapp.com/TRUTH12345',
                'registration-truth:https://chat.whatsapp.com/TRUTH12345',
                'confirmed_pending',
                'verified',
                'pending_detected',
                json.dumps({
                    'configured_registration_group': 'truth-group@g.us',
                    'configured_group_id': 'truth-group@g.us',
                    'configured_link': 'https://chat.whatsapp.com/TRUTH12345',
                    'actual_group_id': 'truth-group@g.us',
                    'actual_group_name': 'Truth Group',
                    'pending_count': 6,
                    'member_count': 123,
                    'requester_ids': ['u1', 'u2', 'u3', 'u4', 'u5', 'u6'],
                    'requesters': [{'id': 'u1'}, {'id': 'u2'}, {'id': 'u3'}, {'id': 'u4'}, {'id': 'u5'}, {'id': 'u6'}],
                }, ensure_ascii=False),
                json.dumps({'monitor_target': {'account_key': 'registration-truth', 'registration_group': 'truth-group@g.us'}}, ensure_ascii=False),
                checked_at,
                expires_at,
                'review_or_wait_for_release_rule',
                checked_at,
            ),
        )
        conn.commit()

    result = service.full_sync_whatsapp_approval_binding('registration-truth', 0, source='manual_approve_preflight')

    assert result['ok'] is False
    assert result['trust_status'] == 'TRUTH_UNKNOWN'
    assert result['reason_code'] == 'api_pending_ui_not_converged'
    assert result['pending_count'] == 6
    assert result['can_manual_approve'] is False
    assert result['final_state'] == 'TRUTH_ACQUISITION_FAILED'
    assert result['failure_class'] == 'UI_NOT_CONVERGED'
    assert result['recommended_action'] == 'REPAIR_UI_ACTION_SURFACE'


def test_manual_approve_requires_successful_preflight_full_sync(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    monkeypatch.setattr(service, 'refresh_whatsapp_approval_binding_probe', lambda *a, **k: {
        'binding_runtime': {
            'binding_id': 'wabind_truth_binding',
            'identity_status': 'resolved',
            'group_id': 'truth-group@g.us',
            'registration_group': 'truth-group@g.us',
            'group_name': 'Truth Group',
        },
        'probe': {'group_id': 'truth-group@g.us', 'group_name': 'Truth Group', 'pending_count': 0},
    })
    monkeypatch.setattr(service, 'full_sync_whatsapp_approval_binding', lambda *a, **k: {
        'ok': False,
        'trust_status': 'SYNC_TIMEOUT',
        'can_manual_approve': False,
        'reason_code': 'full_sync_hard_timeout',
    })
    monkeypatch.setattr(service, '_registration_group_approval_decision_sync', lambda *a, **k: (_ for _ in ()).throw(AssertionError('approval must not run without trusted full_sync')))

    with pytest.raises(Exception) as exc:
        service.manual_approve_whatsapp_approval_binding('registration-truth', 0)

    assert 'full_sync' in str(exc.value) or 'SYNC_TIMEOUT' in str(exc.value)


def test_manual_approve_auto_resolves_identity_before_preflight(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(
        db,
        registration_group='',
        identity_status='unresolved',
        binding_id='wabind_manual_resolve',
    )

    refresh_calls = []
    full_sync_calls = []
    approval_payloads = []

    monkeypatch.setattr(service, 'refresh_whatsapp_approval_binding_probe', lambda account_key, binding_index, **kwargs: (
        refresh_calls.append((account_key, binding_index)) or {
            'account_key': account_key,
            'binding_index': binding_index,
            'binding_runtime': {
                'binding_id': 'wabind_manual_resolve',
                'identity_status': 'resolved',
                'link': 'https://chat.whatsapp.com/TRUTH12345',
                'group_id': 'resolved-group@g.us',
                'registration_group': 'resolved-group@g.us',
                'group_name': 'Resolved Group',
                'runtime_probe_group_id': 'resolved-group@g.us',
                'runtime_probe_group_name': 'Resolved Group',
            },
            'probe': {
                'group_id': 'resolved-group@g.us',
                'group_name': 'Resolved Group',
                'pending_count': 2,
            },
        }
    ))

    monkeypatch.setattr(service, 'full_sync_whatsapp_approval_binding', lambda account_key, binding_index, **kwargs: (
        full_sync_calls.append((account_key, binding_index, kwargs.get('source'))) or {
            'ok': True,
            'trust_status': 'TRUSTED_CONFIRMED_PENDING',
            'can_manual_approve': True,
            'reason_code': 'trusted_pending',
            'trusted_pending_count': 2,
            'ui_pending_count': 2,
            'pending_count': 2,
            'member_count': 5,
            'requester_ids': ['r1', 'r2'],
            'requesters': [{'requesterId': 'r1'}, {'requesterId': 'r2'}],
        }
    ))

    monkeypatch.setattr(
        service.whatsapp_approval_runtime_adapter,
        'execute_registration_group_approval',
        lambda *, service, payload: (
            approval_payloads.append(payload) or {
                'status': 'success',
                'verified': True,
                'crm_recorded': True,
                'result_code': 'approved',
                'approval_run_id': 'run-1',
                'approved_count': 2,
                'raw_result': {'pending_after': 0, 'member_count_after': 5},
            }
        ),
    )
    monkeypatch.setattr(
        service.whatsapp_approval_runtime_adapter,
        'registration_group_executor_state',
        lambda **kwargs: {'group_id': 'resolved-group@g.us', 'group_name': 'Resolved Group', 'pending_count': 0, 'member_count': 5},
    )
    monkeypatch.setattr(service, '_sync_manual_registration_group_approval_to_production_ops_state', lambda *a, **k: None)
    monkeypatch.setattr(service, '_send_registration_group_binding_notification', lambda *a, **k: {'status': 'skipped_not_success', 'code': 'manual_approval_succeeded'})

    result = service.manual_approve_whatsapp_approval_binding('registration-truth', 0)

    assert refresh_calls == [('registration-truth', 0)]
    assert full_sync_calls == [
        ('registration-truth', 0, 'manual_approve_preflight'),
        ('registration-truth', 0, 'approval_after_sync'),
    ]
    assert approval_payloads
    assert approval_payloads[0].registration_group == 'resolved-group@g.us'
    assert result['approval_run_id'] == 'run-1'



def test_manual_approve_blocks_when_identity_remains_unresolved_after_refresh(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(
        db,
        registration_group='',
        identity_status='unresolved',
        binding_id='wabind_manual_block',
    )

    monkeypatch.setattr(service, 'refresh_whatsapp_approval_binding_probe', lambda account_key, binding_index, **kwargs: {
        'account_key': account_key,
        'binding_index': binding_index,
        'binding_runtime': {
            'binding_id': 'wabind_manual_block',
            'identity_status': 'unresolved',
            'link': 'https://chat.whatsapp.com/TRUTH12345',
            'group_id': '',
            'registration_group': '',
            'group_name': '',
        },
        'probe': {'pending_count': 0},
    })
    monkeypatch.setattr(service, 'full_sync_whatsapp_approval_binding', lambda *a, **k: (_ for _ in ()).throw(AssertionError('full sync must not run when identity is unresolved')))

    with pytest.raises(app_main.HTTPException) as exc:
        service.manual_approve_whatsapp_approval_binding('registration-truth', 0)

    assert exc.value.status_code == 409
    assert exc.value.detail['reason'] == 'binding_identity_not_resolved'
    assert exc.value.detail['reason_code'] == 'identity_unresolved'



def test_approval_queue_truth_snapshots_use_binding_id_object_key_and_fallback_to_legacy_rows():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db, binding_id='wabind_truth_binding')

    binding = {
        'binding_id': 'wabind_truth_binding',
        'link': 'https://chat.whatsapp.com/TRUTH12345',
        'registration_group': 'truth-group@g.us',
        'group_id': 'truth-group@g.us',
        'group_name': 'Truth Group',
    }
    _insert_queue_snapshot(db, snapshot_type='approval_queue_current_truth', pending_count=9)

    legacy_loaded = service._load_approval_binding_queue_snapshots('registration-truth', binding)
    assert legacy_loaded['current_truth']['object_key'] == 'registration-truth:https://chat.whatsapp.com/TRUTH12345'
    assert legacy_loaded['current_truth']['pending_count'] == 9

    written = service.upsert_approval_queue_current_truth(
        account_key='registration-truth',
        binding=binding,
        sync_result={
            'ok': True,
            'trust_status': 'TRUSTED_CONFIRMED_PENDING',
            'trusted_pending_count': 7,
            'pending_count': 7,
            'requester_ids': [f'u{i}' for i in range(7)],
            'group_identity_verified': True,
            'runtime_identity_match': True,
            'session_authenticated': True,
            'self_participant_found': True,
            'self_is_admin': True,
            'can_manage_membership_requests': True,
            'review_surface_ready': True,
            'can_manual_approve': True,
            'reason_code': 'trusted_pending',
        },
        source_priority=100,
        force=True,
    )

    assert written['object_key'] == 'registration-truth:binding:wabind_truth_binding'
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT object_key, truth_status FROM mcn_truth_snapshots WHERE object_type='registration_group_binding' AND snapshot_type='approval_queue_current_truth' ORDER BY object_key ASC"
        ).fetchall()
    object_keys = [row['object_key'] for row in rows]
    assert 'registration-truth:binding:wabind_truth_binding' in object_keys
    reloaded = service._load_approval_binding_queue_snapshots('registration-truth', binding)
    assert reloaded['current_truth']['object_key'] == 'registration-truth:binding:wabind_truth_binding'
    assert reloaded['current_truth']['pending_count'] == 7


def test_approval_queue_truth_promotes_verified_pending_truth_confirmed_empty_to_current_truth():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db, binding_id='wabind_truth_binding')

    binding = {
        'binding_id': 'wabind_truth_binding',
        'link': 'https://chat.whatsapp.com/TRUTH12345',
        'registration_group': 'truth-group@g.us',
        'group_id': 'truth-group@g.us',
        'group_name': 'Truth Group',
    }
    stale = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    _insert_queue_snapshot(
        db,
        snapshot_type='approval_queue_current_truth',
        trust_status='TRUSTED_CONFIRMED_PENDING',
        pending_count=3,
        checked_at=stale,
        expires_at=(datetime.now(timezone.utc) - timedelta(hours=23, minutes=55)).isoformat(),
    )
    _insert_pending_truth_snapshot(db, binding_id='wabind_truth_binding')

    loaded = service._load_approval_binding_queue_snapshots('registration-truth', binding)

    assert loaded['current_truth']['trust_status'] == 'TRUSTED_CONFIRMED_EMPTY'
    assert loaded['current_truth']['pending_count'] == 0
    assert loaded['current_truth']['trusted_pending_count'] == 0
    assert loaded['current_truth']['source']['mode'] == 'pending_truth_promotion'
    assert loaded['current_truth']['source']['promotion_reason'] == 'confirmed_empty_promotion'


def test_approval_queue_truth_promotes_verified_pending_truth_confirmed_pending_over_older_empty_current_truth():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db, binding_id='wabind_truth_binding')

    binding = {
        'binding_id': 'wabind_truth_binding',
        'link': 'https://chat.whatsapp.com/TRUTH12345',
        'registration_group': 'truth-group@g.us',
        'group_id': 'truth-group@g.us',
        'group_name': 'Truth Group',
    }
    stale = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    _insert_queue_snapshot(
        db,
        binding_id='wabind_truth_binding',
        snapshot_type='approval_queue_current_truth',
        trust_status='TRUSTED_CONFIRMED_EMPTY',
        pending_count=0,
        checked_at=stale,
        expires_at=(datetime.now(timezone.utc) - timedelta(hours=23, minutes=22)).isoformat(),
    )
    checked_at = datetime.now(timezone.utc).isoformat()
    requester_ids = [f'user-{idx}@lid' for idx in range(1, 23)]
    _insert_pending_truth_snapshot(
        db,
        binding_id='wabind_truth_binding',
        truth_status='confirmed_pending',
        confidence='verified',
        confidence_reason='pending_detected',
        pending_count=22,
        checked_at=checked_at,
        requester_ids=requester_ids,
        requesters=[{'requesterId': requester_id} for requester_id in requester_ids],
    )

    loaded = service._load_approval_binding_queue_snapshots('registration-truth', binding)

    assert loaded['current_truth']['trust_status'] == 'TRUSTED_CONFIRMED_PENDING'
    assert loaded['current_truth']['pending_count'] == 22
    assert loaded['current_truth']['trusted_pending_count'] == 22
    assert loaded['current_truth']['facts']['requester_ids'] == requester_ids
    assert loaded['current_truth']['source']['mode'] == 'pending_truth_promotion'
    assert loaded['current_truth']['source']['promotion_reason'] == 'confirmed_pending_promotion'
    assert loaded['current_truth']['source']['invalidated_reason'] == 'newer_pending_truth_detected'


def test_approval_queue_truth_promotes_from_probe_history_when_pending_truth_single_slot_was_overwritten():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db, binding_id='wabind_truth_binding')

    binding = {
        'binding_id': 'wabind_truth_binding',
        'link': 'https://chat.whatsapp.com/TRUTH12345',
        'registration_group': 'truth-group@g.us',
        'group_id': 'truth-group@g.us',
        'group_name': 'Truth Group',
    }
    stale = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    _insert_queue_snapshot(
        db,
        binding_id='wabind_truth_binding',
        snapshot_type='approval_queue_current_truth',
        trust_status='TRUSTED_CONFIRMED_EMPTY',
        pending_count=0,
        checked_at=stale,
        expires_at=(datetime.now(timezone.utc) - timedelta(hours=23, minutes=22)).isoformat(),
    )
    checked_at = datetime.now(timezone.utc).isoformat()
    requester_ids = [f'user-{idx}@lid' for idx in range(1, 23)]
    _insert_pending_truth_snapshot(
        db,
        binding_id='wabind_truth_binding',
        truth_status='probe_unavailable',
        confidence='untrusted',
        confidence_reason='sync_timeout',
        pending_count=0,
        checked_at=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat(),
    )
    _insert_probe_observed_event(
        db,
        binding_id='wabind_truth_binding',
        trust_status='TRUSTED_CONFIRMED_PENDING',
        pending_count=22,
        checked_at=checked_at,
        requester_ids=requester_ids,
        requesters=[{'requesterId': requester_id} for requester_id in requester_ids],
    )

    loaded = service._load_approval_binding_queue_snapshots('registration-truth', binding)

    assert loaded['current_truth']['trust_status'] == 'TRUSTED_CONFIRMED_PENDING'
    assert loaded['current_truth']['pending_count'] == 22
    assert loaded['current_truth']['facts']['requester_ids'] == requester_ids
    assert loaded['current_truth']['source']['mode'] == 'pending_truth_promotion'


def test_pending_truth_snapshot_group_state_falls_back_to_probe_history_when_single_slot_is_overwritten():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db, binding_id='wabind_truth_binding')

    binding = {
        'binding_id': 'wabind_truth_binding',
        'link': 'https://chat.whatsapp.com/TRUTH12345',
        'registration_group': 'truth-group@g.us',
        'group_id': 'truth-group@g.us',
        'group_name': 'Truth Group',
    }
    requester_ids = [f'user-{idx}@lid' for idx in range(1, 6)]
    checked_at = datetime.now(timezone.utc).isoformat()
    _insert_pending_truth_snapshot(
        db,
        binding_id='wabind_truth_binding',
        truth_status='probe_unavailable',
        confidence='untrusted',
        confidence_reason='sync_timeout',
        pending_count=0,
        checked_at=(datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(),
    )
    _insert_probe_observed_event(
        db,
        binding_id='wabind_truth_binding',
        trust_status='TRUSTED_CONFIRMED_PENDING',
        pending_count=5,
        checked_at=checked_at,
        requester_ids=requester_ids,
        requesters=[{'requesterId': requester_id} for requester_id in requester_ids],
    )

    state = service._load_pending_truth_snapshot_group_state(
        account_key='registration-truth',
        binding=binding,
        registration_group='truth-group@g.us',
    )

    assert state['pending_count'] == 5
    assert state['requester_ids'] == requester_ids
    assert state['source'] == 'mcn_event_ledger'


def test_lightweight_registration_binding_prefers_promoted_confirmed_pending_truth_over_stale_empty(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db, binding_id='wabind_truth_binding')
    _insert_queue_snapshot(
        db,
        binding_id='wabind_truth_binding',
        snapshot_type='approval_queue_current_truth',
        trust_status='TRUSTED_CONFIRMED_EMPTY',
        pending_count=0,
        checked_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        expires_at=(datetime.now(timezone.utc) - timedelta(hours=23, minutes=55)).isoformat(),
    )
    _insert_queue_snapshot(
        db,
        binding_id='wabind_truth_binding',
        snapshot_type='approval_queue_latest_probe',
        trust_status='SYNC_TIMEOUT',
        pending_count=0,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )
    requester_ids = [f'user-{idx}@lid' for idx in range(1, 20)]
    _insert_pending_truth_snapshot(
        db,
        binding_id='wabind_truth_binding',
        truth_status='confirmed_pending',
        confidence='verified',
        confidence_reason='pending_detected',
        pending_count=19,
        requester_ids=requester_ids,
        requesters=[{'requesterId': requester_id} for requester_id in requester_ids],
    )
    _patch_lightweight_account_dependencies(monkeypatch, service)

    payload = service.list_whatsapp_approval_accounts(lightweight=True)
    truth = payload['rows'][0]['group_binding_runtimes'][0]['approval_queue_truth']

    assert truth['current_truth']['trust_status'] == 'TRUSTED_CONFIRMED_PENDING'
    assert truth['pending_count'] == 19
    assert truth['display']['state'] == 'COUNT'
    assert truth['display']['count'] == 19
    assert truth['display']['primary_text'] == '待审批 19 人'
    assert truth['display']['secondary_text'] == ''


def test_approval_queue_truth_promotion_accepts_nested_pending_truth_membership_evidence():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    binding = {
        'link': 'https://chat.whatsapp.com/TRUTH12345',
        'registration_group': 'truth-group@g.us',
        'group_id': 'truth-group@g.us',
        'group_name': 'Truth Group',
    }
    _insert_queue_snapshot(
        db,
        snapshot_type='approval_queue_current_truth',
        trust_status='TRUSTED_CONFIRMED_PENDING',
        pending_count=3,
        checked_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        expires_at=(datetime.now(timezone.utc) - timedelta(hours=23, minutes=55)).isoformat(),
    )
    checked_at = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO mcn_truth_snapshots (
                snapshot_id, object_type, object_key, snapshot_type, truth_status,
                confidence, confidence_reason, facts_json, source_json, checked_at,
                expires_at, recommended_action, updated_at
            ) VALUES (?, 'registration_group_binding', ?, 'pending_truth', 'confirmed_empty', 'verified', 'empty_queue_confirmed', ?, ?, ?, ?, 'none', ?)
            """,
            (
                'pending_truth:registration-truth:https://chat.whatsapp.com/TRUTH12345',
                'registration-truth:https://chat.whatsapp.com/TRUTH12345',
                json.dumps({
                    'configured_registration_group': 'truth-group@g.us',
                    'configured_link': 'https://chat.whatsapp.com/TRUTH12345',
                    'actual_group_id': 'truth-group@g.us',
                    'pending_count': 0,
                    'login_verified': True,
                    'runtime_active': True,
                    'runtime_authenticated': True,
                    'runtime_ready': True,
                    'session_target_match': True,
                    'zero_pending_unverified': False,
                    'zero_pending_verified_by': 'consecutive_group_state_refresh',
                }, ensure_ascii=False),
                json.dumps({
                    'decision_group_state': {
                        'payload': {
                            'can_manage_membership_requests': True,
                            'self_is_admin': True,
                            'self_participant_found': True,
                            'review_surface_ready': False,
                            'empty_queue_visible': False,
                        }
                    }
                }, ensure_ascii=False),
                checked_at,
                (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
                checked_at,
            ),
        )
        conn.commit()

    loaded = service._load_approval_binding_queue_snapshots('registration-truth', binding)

    assert loaded['current_truth']['trust_status'] == 'TRUSTED_CONFIRMED_EMPTY'
    assert loaded['current_truth']['source']['mode'] == 'pending_truth_promotion'


def test_lightweight_registration_binding_prefers_promoted_confirmed_empty_truth(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db, binding_id='wabind_truth_binding')
    _insert_queue_snapshot(
        db,
        snapshot_type='approval_queue_current_truth',
        trust_status='TRUSTED_CONFIRMED_PENDING',
        pending_count=3,
        checked_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        expires_at=(datetime.now(timezone.utc) - timedelta(hours=23, minutes=55)).isoformat(),
    )
    _insert_pending_truth_snapshot(db, binding_id='wabind_truth_binding')
    _patch_lightweight_account_dependencies(monkeypatch, service)

    payload = service.list_whatsapp_approval_accounts(lightweight=True)
    truth = payload['rows'][0]['group_binding_runtimes'][0]['approval_queue_truth']

    assert truth['current_truth']['trust_status'] == 'TRUSTED_CONFIRMED_EMPTY'
    assert truth['display']['count'] == 0
    assert truth['display']['state'] == 'COUNT'
    assert truth['display_text']
    assert truth['can_manual_approve'] is False


def test_expired_approval_queue_truth_enqueues_lightweight_self_heal_full_sync(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db, binding_id='wabind_truth_binding')
    _insert_queue_snapshot(
        db,
        snapshot_type='approval_queue_current_truth',
        trust_status='TRUSTED_CONFIRMED_PENDING',
        pending_count=3,
        checked_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        expires_at=(datetime.now(timezone.utc) - timedelta(hours=23, minutes=55)).isoformat(),
    )
    _patch_lightweight_account_dependencies(monkeypatch, service)

    payload = service.list_whatsapp_approval_accounts(lightweight=True)
    result = service.maybe_enqueue_expired_approval_queue_self_heal(payload['rows'], created_by='pytest-self-heal')

    assert result['queued_count'] == 1
    assert result['results'][0]['reason'] == 'enqueued_lightweight_probe_escalation'
    with db.connect() as conn:
        task = conn.execute(
            """
            SELECT task_type, status, created_by, input_json
            FROM mcn_operation_tasks
            WHERE object_key = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            ('registration-truth:0',),
        ).fetchone()
    assert task is not None
    task_input = json.loads(task['input_json'])
    assert task['task_type'] == 'whatsapp_full_sync'
    assert task['status'] == 'pending'
    assert task['created_by'] == 'pytest-self-heal'
    assert task_input['source'] == 'lightweight_probe_escalation'
    assert task_input['reason'] == 'expired_truth_self_heal'


def test_expired_approval_queue_truth_self_heal_respects_recent_full_sync_cooldown(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db, binding_id='wabind_truth_binding')
    _insert_queue_snapshot(
        db,
        snapshot_type='approval_queue_current_truth',
        trust_status='TRUSTED_CONFIRMED_PENDING',
        pending_count=3,
        checked_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        expires_at=(datetime.now(timezone.utc) - timedelta(hours=23, minutes=55)).isoformat(),
    )
    _patch_lightweight_account_dependencies(monkeypatch, service)
    now_iso = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO mcn_operation_tasks (
                task_id, task_type, object_type, object_key, idempotency_key,
                status, stage, priority, retry_count, max_retries, input_json, result_json,
                error_code, error_message, created_by, created_at, available_at, lease_owner,
                lease_until, timeout_seconds, started_at, finished_at
            ) VALUES (?, 'whatsapp_full_sync', 'registration_group_binding', ?, ?, 'success', 'completed', 25, 0, 2, ?, '{}', '', '', ?, ?, ?, '', '', 45, ?, ?)
            """,
            (
                'wa_task_recent_full_sync',
                'registration-truth:0',
                'whatsapp_full_sync:registration-truth:0',
                json.dumps({'source': 'lightweight_probe_escalation', 'reason': 'expired_truth_self_heal'}, ensure_ascii=False),
                'pytest-self-heal',
                now_iso,
                now_iso,
                now_iso,
                now_iso,
            ),
        )
        conn.commit()

    payload = service.list_whatsapp_approval_accounts(lightweight=True)
    result = service.maybe_enqueue_expired_approval_queue_self_heal(payload['rows'], created_by='pytest-self-heal')

    assert result['queued_count'] == 0
    assert result['results'][0]['reason'] == 'recent_full_sync_cooldown'
    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM mcn_operation_tasks WHERE object_key = ? AND task_type = 'whatsapp_full_sync'",
            ('registration-truth:0',),
        ).fetchone()[0]
    assert count == 1


def test_downgraded_polluted_empty_truth_enqueues_scheduled_full_sync(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db, binding_id='wabind_truth_binding')
    checked_at = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO mcn_truth_snapshots (
                snapshot_id, object_type, object_key, snapshot_type, truth_status,
                confidence, confidence_reason, facts_json, source_json, checked_at,
                expires_at, recommended_action, updated_at
            ) VALUES (?, 'registration_group_binding', ?, 'approval_queue_current_truth', ?, 'verified', '', ?, ?, ?, ?, '', ?)
            """,
            (
                'approval_queue_current_truth:registration-truth:0',
                'registration-truth:0',
                'TRUSTED_CONFIRMED_EMPTY',
                json.dumps({
                    'trust_status': 'TRUSTED_CONFIRMED_EMPTY',
                    'trusted_pending_count': 0,
                    'pending_count': 0,
                    'display_trusted': True,
                    'can_manual_approve': False,
                    'manual_approve_allowed': False,
                    'strong_empty_evidence': False,
                }, ensure_ascii=False),
                json.dumps({
                    'mode': 'executor_group_state_fallback',
                    'fallback_reason': 'worker_untrusted:UNTRUSTED_SYNC_INCONCLUSIVE:ui_api_not_converged',
                    'source_priority': 100,
                }, ensure_ascii=False),
                checked_at,
                checked_at,
                checked_at,
            ),
        )
        conn.commit()

    _patch_lightweight_account_dependencies(monkeypatch, service)

    downgrade = service.downgrade_polluted_approval_queue_current_truth()
    payload = service.list_whatsapp_approval_accounts(lightweight=True)
    truth = payload['rows'][0]['group_binding_runtimes'][0]['approval_queue_truth']
    result = service.maybe_enqueue_expired_approval_queue_self_heal(payload['rows'], created_by='pytest-auto-refresh')

    assert downgrade['changed'] == 1
    assert truth['current_truth']['reason_code'] == 'historical_polluted_empty_downgraded'
    assert truth['freshness_level'] == 'UNKNOWN'
    assert truth['status'] == 'unknown'
    assert truth['display']['state'] == 'UNKNOWN'
    assert result['queued_count'] == 1
    assert result['results'][0]['reason'] == 'enqueued_scheduled_full_sync'
    with db.connect() as conn:
        task = conn.execute(
            """
            SELECT task_type, status, created_by, input_json
            FROM mcn_operation_tasks
            WHERE object_key = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            ('registration-truth:0',),
        ).fetchone()
    assert task is not None
    task_input = json.loads(task['input_json'])
    assert task['task_type'] == 'whatsapp_full_sync'
    assert task['status'] == 'pending'
    assert task['created_by'] == 'pytest-auto-refresh'
    assert task_input['source'] == 'scheduled_full_sync'
    assert task_input['reason'] == 'auto_refresh_truth_reconciliation'


def test_downgrade_polluted_current_truth_snapshots():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    object_key = 'registration-truth:https://chat.whatsapp.com/TRUTH12345'
    checked_at = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO mcn_truth_snapshots (
                snapshot_id, object_type, object_key, snapshot_type, truth_status,
                confidence, confidence_reason, facts_json, source_json, checked_at,
                expires_at, recommended_action, updated_at
            ) VALUES (?, 'registration_group_binding', ?, 'approval_queue_current_truth', ?, 'verified', '', ?, ?, ?, ?, '', ?)
            """,
            (
                f'approval_queue_current_truth:{object_key}',
                object_key,
                'TRUSTED_CONFIRMED_EMPTY',
                json.dumps({
                    'trust_status': 'TRUSTED_CONFIRMED_EMPTY',
                    'trusted_pending_count': 0,
                    'pending_count': 0,
                    'display_trusted': True,
                    'can_manual_approve': False,
                    'manual_approve_allowed': False,
                }, ensure_ascii=False),
                json.dumps({
                    'mode': 'executor_group_state_fallback',
                    'fallback_reason': 'worker_untrusted:UNTRUSTED_SYNC_INCONCLUSIVE:ui_api_not_converged',
                    'source_priority': 100,
                }, ensure_ascii=False),
                checked_at,
                None,
                checked_at,
            ),
        )
        conn.commit()

    result = service.downgrade_polluted_approval_queue_current_truth()
    assert result['changed'] == 1
    truth = service._load_approval_binding_queue_snapshots('registration-truth', {'link': 'https://chat.whatsapp.com/TRUTH12345'})['current_truth']
    assert truth['trust_status'] == 'EMPTY_UNVERIFIED'
    assert truth['strong_empty_evidence'] is False


def test_current_truth_write_rejects_stale_runtime_generation():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    service.upsert_approval_queue_current_truth(
        account_key='registration-truth',
        binding={'link': 'https://chat.whatsapp.com/TRUTH12345'},
        sync_result={
            'ok': True,
            'trust_status': 'TRUSTED_CONFIRMED_PENDING',
            'trusted_pending_count': 2,
            'pending_count': 2,
            'ui_pending_count': 2,
            'api_pending_count': 2,
            'requester_ids': ['u1', 'u2'],
            'group_identity_verified': True,
            'runtime_identity_match': True,
            'session_authenticated': True,
            'self_participant_found': True,
            'self_is_admin': True,
            'can_manage_membership_requests': True,
            'review_surface_ready': True,
            'runtime_generation': 5,
            'source': 'manual_full_sync',
        },
        source_priority=100,
        observed_at=datetime.now(timezone.utc).isoformat(),
        force=True,
    )

    stale = service.upsert_approval_queue_current_truth(
        account_key='registration-truth',
        binding={'link': 'https://chat.whatsapp.com/TRUTH12345'},
        sync_result={
            'ok': True,
            'trust_status': 'TRUSTED_CONFIRMED_PENDING',
            'trusted_pending_count': 3,
            'pending_count': 3,
            'ui_pending_count': 3,
            'api_pending_count': 3,
            'requester_ids': ['u1', 'u2', 'u3'],
            'group_identity_verified': True,
            'runtime_identity_match': True,
            'session_authenticated': True,
            'self_participant_found': True,
            'self_is_admin': True,
            'can_manage_membership_requests': True,
            'review_surface_ready': True,
            'runtime_generation': 4,
            'source': 'manual_full_sync',
        },
        source_priority=100,
        observed_at=datetime.now(timezone.utc).isoformat(),
        force=True,
    )
    assert stale['written'] is False
    assert stale['reason'] == 'stale_runtime_generation'


def test_detect_stale_probe_records_recovery_event_and_timeout_latest_probe():
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)
    checked = datetime.now(timezone.utc).isoformat()
    for idx in range(3):
        service.upsert_approval_queue_latest_probe(
            account_key='registration-truth',
            binding={'link': 'https://chat.whatsapp.com/TRUTH12345'},
            probe_result={
                'ok': False,
                'trust_status': 'UNTRUSTED_API_STALE',
                'api_pending_count': 7,
                'fingerprint': 'stuck-7',
                'reason_code': 'ui_empty_api_has_historical_requests',
            },
            observed_at=checked,
        )
    result = service.evaluate_approval_queue_staleness(
        account_key='registration-truth',
        binding={'link': 'https://chat.whatsapp.com/TRUTH12345'},
        external_signal='manual_abnormal_mark',
    )

    assert result['stale_detected'] is True
    assert result['recovery_action'] == 'soft_reload'
    with db.connect() as conn:
        events = conn.execute("SELECT event_type, payload_json FROM mcn_event_ledger WHERE event_type='approval_queue_recovery_event'").fetchall()
    assert len(events) == 1
    payload = json.loads(events[0]['payload_json'])
    assert payload['recovery_action'] == 'soft_reload'


def test_enqueue_whatsapp_approval_task_dedupes_same_pending_manual_approve():
    db = Database(':memory:')
    service = Service(db)

    first = service.enqueue_whatsapp_approval_task(
        account_key='registration-queue',
        binding_index=0,
        operation='manual_approve',
        input_payload={'request_id': 'approval-op-001'},
    )
    second = service.enqueue_whatsapp_approval_task(
        account_key='registration-queue',
        binding_index=0,
        operation='manual_approve',
        input_payload={'request_id': 'approval-op-002'},
    )

    assert first['task_id'] == second['task_id']
    assert second['deduped'] is True
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT task_id, task_type, status FROM mcn_operation_tasks WHERE task_type = 'whatsapp_manual_approve'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]['status'] == 'pending'


def test_process_operation_tasks_once_skips_pending_task_for_account_with_running_job(monkeypatch):
    db = Database(':memory:')
    service = Service(db)

    running = service.enqueue_whatsapp_approval_task(
        account_key='account-a',
        binding_index=0,
        operation='probe_refresh',
        input_payload={'request_id': 'running-task'},
    )
    pending_same_account = service.enqueue_whatsapp_approval_task(
        account_key='account-a',
        binding_index=1,
        operation='full_sync',
        input_payload={'request_id': 'pending-same-account'},
    )
    pending_other_account = service.enqueue_whatsapp_approval_task(
        account_key='account-b',
        binding_index=0,
        operation='full_sync',
        input_payload={'request_id': 'pending-other-account'},
    )

    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        conn.execute(
            "UPDATE mcn_operation_tasks SET status='running', started_at=?, lease_until=?, lease_owner=? WHERE task_id=?",
            (
                now.isoformat(),
                (now + timedelta(seconds=30)).isoformat(),
                'worker-test',
                running['task_id'],
            ),
        )
        conn.commit()

    executed = []

    def _fake_execute(task_id: str, *, user=None):
        executed.append(task_id)
        service._set_operation_task_status(task_id, status='success', stage='done', result={'ok': True})

    monkeypatch.setattr(service, '_execute_operation_task', _fake_execute)

    result = service.process_operation_tasks_once(limit=2)

    assert pending_other_account['task_id'] in executed
    assert pending_same_account['task_id'] not in executed
    assert result['processed'] == 1


def test_process_operation_tasks_once_retries_then_dead_letters_failed_whatsapp_task(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    task = service.enqueue_whatsapp_approval_task(
        account_key='account-retry',
        binding_index=0,
        operation='probe_refresh',
        input_payload={'request_id': 'retry-task'},
        max_retries=2,
    )

    def _always_fail(task_id: str, *, user=None):
        current = service.get_operation_task(task_id)
        service._requeue_or_fail_operation_task(
            current,
            error_code='probe_failed',
            error_message='probe failed',
        )

    monkeypatch.setattr(service, '_execute_operation_task', _always_fail)

    first = service.process_operation_tasks_once(limit=1)
    second = service.process_operation_tasks_once(limit=1)

    row = service.get_operation_task(task['task_id'])
    assert first['processed'] == 1
    assert second['processed'] == 1
    assert row['status'] == 'dead_letter'
    assert row['retry_count'] == 2
    assert row['error_code'] == 'probe_failed'


def test_full_sync_whatsapp_approval_binding_uses_runtime_adapter(monkeypatch):
    db = Database(':memory:')
    service = Service(db)
    _insert_registration_account_with_binding(db)

    calls = []

    class StubRuntimeAdapter:
        def full_queue_sync(self, *, service, account, binding, timeout_seconds):
            calls.append({
                'account_key': account.get('account_key'),
                'binding_link': binding.get('link'),
                'timeout_seconds': timeout_seconds,
            })
            return {
                'ok': True,
                'trust_status': 'TRUSTED_CONFIRMED_PENDING',
                'pending_count': 3,
                'trusted_pending_count': 3,
                'ui_pending_count': 3,
                'api_pending_count': 3,
                'member_count': 20,
                'group_id': '120363000000000000@g.us',
                'group_name': 'Test Group',
                'requester_ids': ['user-1'],
                'requesters': [{'id': 'user-1'}],
                'fingerprint': 'fp-1',
                'fingerprint_quality': 'strong',
                'converged': True,
                'reason_code': 'adapter_ok',
                'source': {'source': 'adapter'},
            }

        def registration_group_executor_state(self, *, service, registration_group, allow_legacy_target=True):
            raise AssertionError('fallback should not be used when adapter full_queue_sync succeeds')

    service.whatsapp_approval_runtime_adapter = StubRuntimeAdapter()
    monkeypatch.setattr(service, '_production_ops_daemon_snapshot', lambda: {'config': {'enabled': True}, 'runtime': {'status': {}}})

    result = service.full_sync_whatsapp_approval_binding('registration-truth', 0, source='manual_full_sync')

    assert result['trust_status'] == 'TRUSTED_CONFIRMED_PENDING'
    assert calls and calls[0]['account_key'] == 'registration-truth'


def test_run_whatsapp_approval_task_sync_processes_queue_and_returns_result(tmp_path, monkeypatch):
    db = Database(str(tmp_path / 'ops.db'))
    service = Service(db)
    service.task_engine_enabled = False

    def _fake_execute(task_id: str, *, user=None):
        task = service.get_operation_task(task_id)
        service._set_operation_task_status(task_id, status='success', stage='completed', result={'task_id': task_id, 'operation': task['input']['operation']})

    monkeypatch.setattr(service, '_execute_operation_task', _fake_execute)

    result = service.run_whatsapp_approval_task_sync(
        account_key='sync-account',
        binding_index=0,
        operation='probe_refresh',
        input_payload={'request_id': 'sync-001'},
        wait_timeout_seconds=5.0,
    )

    assert result['operation'] == 'probe_refresh'
    assert result['task_id']


def test_run_whatsapp_approval_task_sync_surfaces_structured_dead_letter_detail(tmp_path, monkeypatch):
    db = Database(str(tmp_path / 'ops.db'))
    service = Service(db)
    service.task_engine_enabled = False

    def _fake_execute(task_id: str, *, user=None):
        task = service.get_operation_task(task_id)
        service._requeue_or_fail_operation_task(
            task,
            error_code='manual_approval_full_sync_not_trusted',
            error_message='blocked',
            result={
                'http_status': 409,
                'detail': {
                    'reason': 'manual_approval_full_sync_not_trusted',
                    'message': '当前审批前同步已拦截：API 已看到待审批，但审批面未收敛。审批面未收敛，请先修复审批面或执行完整同步后重试',
                    'stage_code': 'preflight_blocked',
                },
            },
        )

    monkeypatch.setattr(service, '_execute_operation_task', _fake_execute)

    with pytest.raises(app_main.HTTPException) as exc_info:
        service.run_whatsapp_approval_task_sync(
            account_key='sync-account',
            binding_index=0,
            operation='manual_approve',
            input_payload={'request': {'request_id': 'sync-dead-letter-001'}},
            wait_timeout_seconds=5.0,
        )

    detail = exc_info.value.detail
    assert exc_info.value.status_code == 409
    assert detail['reason'] == 'manual_approval_full_sync_not_trusted'
    assert detail['stage_code'] == 'preflight_blocked'
    assert '审批面未收敛' in detail['message']
    assert detail['task_id']
    assert detail['status'] == 'dead_letter'


def test_run_whatsapp_approval_task_sync_self_heals_when_task_worker_not_alive(tmp_path, monkeypatch):
    db = Database(str(tmp_path / 'ops.db'))
    service = Service(db)
    service.task_engine_enabled = True
    service._operation_task_worker_thread = threading.Thread(target=lambda: None)

    started = []

    def _fake_start_worker():
        started.append('started')

    def _fake_execute(task_id: str, *, user=None):
        task = service.get_operation_task(task_id)
        service._set_operation_task_status(task_id, status='success', stage='completed', result={'task_id': task_id, 'operation': task['input']['operation']})

    monkeypatch.setattr(service, '_start_operation_task_worker', _fake_start_worker)
    monkeypatch.setattr(service, '_execute_operation_task', _fake_execute)

    result = service.run_whatsapp_approval_task_sync(
        account_key='sync-account',
        binding_index=0,
        operation='manual_approve',
        input_payload={'request': {'request_id': 'sync-heal-001'}},
        wait_timeout_seconds=5.0,
    )

    assert started == ['started']
    assert result['operation'] == 'manual_approve'
    assert result['task_id']
