from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.realtime_approval_state import RealtimeApprovalStateStore


def test_realtime_store_emits_group_patch_when_pending_changes():
    store = RealtimeApprovalStateStore()
    first = {
        'rows': [
            {
                'account_key': 'registration-a',
                'session_state': {'login_state': 'logged_in', 'can_probe': True},
                'group_binding_runtimes': [
                    {
                        'group_id': 'g1@g.us',
                        'group_name': 'Group 1',
                        'next_approval_pending_count': 20,
                        'membership_verifier': {'ready': True, 'status': 'mapped_live_probe_ready'},
                    }
                ],
            }
        ]
    }
    second = {
        'rows': [
            {
                'account_key': 'registration-a',
                'session_state': {'login_state': 'logged_in', 'can_probe': True},
                'group_binding_runtimes': [
                    {
                        'group_id': 'g1@g.us',
                        'group_name': 'Group 1',
                        'next_approval_pending_count': 0,
                        'membership_verifier': {'ready': True, 'status': 'mapped_live_probe_ready'},
                    }
                ],
            }
        ]
    }

    assert store.ingest_snapshot(first)['events'] == []
    result = store.ingest_snapshot(second)

    assert result['snapshot']['snapshot_version'] == 2
    assert len(result['events']) == 1
    event = result['events'][0]
    assert event['type'] == 'group_probe_patch'
    assert event['account_key'] == 'registration-a'
    assert event['group_id'] == 'g1@g.us'
    assert event['patch']['next_approval_pending_count'] == 0
    assert event['patch']['previous_pending_count'] == 20


def test_realtime_store_preserves_successful_manual_probe_against_weak_daemon_snapshot():
    store = RealtimeApprovalStateStore()
    strong = {
        'rows': [
            {
                'account_key': 'registration-a',
                'session_state': {'login_state': 'logged_in', 'login_verified': True, 'can_probe': True},
                'group_binding_runtimes': [
                    {
                        'binding_index': 1,
                        'group_id': 'g1@g.us',
                        'group_name': 'Group 1',
                        'next_approval_pending_count': 0,
                        'membership_verifier': {
                            'ready': True,
                            'status': 'mapped_live_probe_ready',
                            'detail': '实时群状态探针可用。',
                            'probe': {
                                'pending_count': 0,
                                'member_count': 529,
                                'data_quality': 'verified_zero',
                                'probe_data_quality': 'verified_zero',
                            },
                        },
                    }
                ],
            }
        ]
    }
    weak = {
        'rows': [
            {
                'account_key': 'registration-a',
                'session_state': {'login_state': 'logged_in', 'login_verified': True, 'can_probe': True},
                'group_binding_runtimes': [
                    {
                        'binding_index': 1,
                        'group_id': 'g1@g.us',
                        'group_name': 'Group 1',
                        'next_approval_pending_count': 0,
                        'membership_verifier': {
                            'ready': False,
                            'status': 'probe_unavailable',
                            'detail': '当前未拿到可用的实时群状态探针结果。',
                            'probe': {
                                'pending_count': 0,
                                'member_count': None,
                                'data_quality': 'unverified_zero',
                                'probe_data_quality': 'unverified_zero',
                            },
                        },
                    }
                ],
            }
        ]
    }

    store.ingest_snapshot(strong, source='manual_probe')
    result = store.ingest_snapshot(weak, source='daemon')

    group = result['snapshot']['rows'][0]['group_binding_runtimes'][0]
    verifier = group['membership_verifier']
    assert verifier['ready'] is True
    assert verifier['status'] == 'mapped_live_probe_ready'
    assert verifier['probe']['member_count'] == 529
    assert verifier['probe']['data_quality'] == 'verified_zero'
    assert result['events'] == []


def test_realtime_internal_ingest_and_snapshot_endpoint_publish_authoritative_state():
    client = TestClient(create_app({'TESTING': True, 'DB_PATH': ':memory:'}))
    payload = {
        'rows': [
            {
                'account_key': 'registration-a',
                'account_name': '+639****0001',
                'session_state': {'login_state': 'logged_in', 'login_verified': True, 'can_probe': True},
                'group_binding_runtimes': [
                    {
                        'group_id': 'g1@g.us',
                        'group_name': 'Group 1',
                        'next_approval_pending_count': 7,
                        'membership_verifier': {'ready': True, 'status': 'mapped_live_probe_ready'},
                    }
                ],
            }
        ]
    }

    ingest = client.post('/api/internal/whatsapp-approval/realtime-state', json=payload)
    assert ingest.status_code == 200
    assert ingest.json()['snapshot_version'] == 1

    snap = client.get('/api/ops/whatsapp-approval-accounts/realtime-snapshot')
    assert snap.status_code == 200
    data = snap.json()
    assert data['snapshot_mode'] == 'server_authoritative_realtime'
    assert data['rows'][0]['account_key'] == 'registration-a'
    assert data['rows'][0]['group_binding_runtimes'][0]['next_approval_pending_count'] == 7


def test_realtime_snapshot_promotes_session_and_runtime_fields_for_account_rows():
    client = TestClient(create_app({'TESTING': True, 'DB_PATH': ':memory:'}))
    payload = {
        'rows': [
            {
                'account_key': 'registration-a',
                'account_name': '+639****0001',
                'login_state': None,
                'ready': None,
                'authenticated': None,
                'login_verified': None,
                'can_probe': None,
                'login_check_status': None,
                'monitor_runtime_active': False,
                'runtime_state': {'active': True, 'ready': True, 'authenticated': True, 'status': 'warm'},
                'session_state': {
                    'login_state': 'logged_in',
                    'ready': True,
                    'authenticated': True,
                    'login_verified': True,
                    'can_probe': True,
                    'login_check_status': 'passed',
                },
            }
        ]
    }

    ingest = client.post('/api/internal/whatsapp-approval/realtime-state', json=payload)
    assert ingest.status_code == 200
    row = client.get('/api/ops/whatsapp-approval-accounts/realtime-snapshot').json()['rows'][0]

    assert row['login_state'] == 'logged_in'
    assert row['ready'] is True
    assert row['authenticated'] is True
    assert row['login_verified'] is True
    assert row['can_probe'] is True
    assert row['login_check_status'] == 'passed'
    assert row['monitor_runtime_active'] is True


def test_realtime_websocket_receives_patch_event_with_low_latency_contract():
    client = TestClient(create_app({'TESTING': True, 'DB_PATH': ':memory:'}))
    first = {
        'rows': [
            {
                'account_key': 'registration-a',
                'session_state': {'login_state': 'logged_in', 'can_probe': True},
                'group_binding_runtimes': [{'group_id': 'g1@g.us', 'next_approval_pending_count': 20}],
            }
        ]
    }
    second = {
        'rows': [
            {
                'account_key': 'registration-a',
                'session_state': {'login_state': 'logged_in', 'can_probe': True},
                'group_binding_runtimes': [{'group_id': 'g1@g.us', 'next_approval_pending_count': 0}],
            }
        ]
    }
    client.post('/api/internal/whatsapp-approval/realtime-state', json=first)

    with client.websocket_connect('/api/ops/whatsapp-approval-accounts/realtime-ws') as ws:
        hello = ws.receive_json()
        assert hello['type'] == 'hello'
        client.post('/api/internal/whatsapp-approval/realtime-state', json=second)
        event = ws.receive_json()

    assert event['type'] == 'group_probe_patch'
    assert event['patch']['next_approval_pending_count'] == 0
    assert event['delivery_target_ms'] == 100


def test_production_ops_page_connects_realtime_websocket_and_patches_groups():
    source = Path('app/main.py').read_text()

    assert 'connectApprovalRealtimeWebSocket' in source
    assert '/api/ops/whatsapp-approval-accounts/realtime-ws' in source
    assert 'applyApprovalRealtimeEvent' in source
    assert 'applyApprovalGroupRealtimePatch' in source
    assert 'delivery_target_ms' in source
    assert 'data-realtime-group-id' in source


def test_realtime_snapshot_endpoint_refreshes_lightweight_server_snapshot():
    source = Path('app/main.py').read_text()
    store_source = Path('app/realtime_approval_state.py').read_text()

    assert 'lightweight_snapshot_refresh' in source
    assert 'returning the first in-memory' in source
    assert 'service.list_whatsapp_approval_accounts(lightweight=True)' in source
    assert "source: str = 'backend'" in store_source
    assert "lightweight_snapshot_refresh" in store_source
    assert "return False" in store_source


def test_daemon_can_publish_realtime_snapshot_without_browser_probe():
    source = Path('scripts/production_ops_daemon.py').read_text()

    assert 'publish_realtime_state_snapshot' in source
    assert '/api/internal/whatsapp-approval/realtime-state' in source
    assert 'realtime_state_publish' in source
