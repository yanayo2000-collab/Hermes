from __future__ import annotations

import json
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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



def test_production_ops_qr_modal_requires_real_qr_payload_marker():
    source = Path('app/main.py').read_text()

    assert 'function approvalSessionHasQrPayload(sessionState)' in source
    assert 'function approvalSessionCanOpenQrModal(sessionState, options = {})' in source
    assert 'approvalSessionCanOpenQrModal(mergedSessionState, options)' in source
    assert 'if (sessionState.qr_available)' not in source
    assert 'approvalSessionHasQrPayload(sessionState)' in source


def test_production_ops_truth_refresh_uses_unified_login_state_marker():
    source = Path('app/main.py').read_text()

    assert "const loginState = String(sess.login_state || '').trim();" in source
    assert "['runtime_starting', 'initializing'].includes(loginState)" in source
    assert "runtime.active && !runtime.ready" not in source
    assert "['pending_runtime', 'auto_recovering', 'session_mismatch', 'runtime_unavailable', ''].includes(code)" not in source
