from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.main import Database, Service, utc_now


def test_production_ops_daemon_config_prefers_registration_group_truth_snapshots():
    service = Service(Database(':memory:'))
    checked_at = datetime.now(timezone.utc).isoformat()
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()
    facts = {
        'configured_registration_group': '120363417671114118@g.us',
        'configured_link': 'https://chat.whatsapp.com/example',
        'configured_group_id': '120363417671114118@g.us',
        'configured_group_name': 'Old configured name',
        'actual_group_id': '120363417671114118@g.us',
        'actual_group_name': 'Snapshot authoritative group',
        'pending_count': 4,
        'member_count': 520,
        'requester_ids': ['r1', 'r2', 'r3', 'r4'],
        'requesters': [{'requesterId': 'r1'}, {'requesterId': 'r2'}],
        'zero_pending_unverified': False,
        'review_surface_ready': False,
        'empty_queue_visible': False,
        'has_pending_section': False,
        'has_pending_request_row': False,
        'runtime_active': True,
        'runtime_ready': True,
        'runtime_authenticated': True,
        'session_target_match': True,
        'login_verified': True,
    }
    source = {
        'monitor_target': {
            'account_key': 'registration-a',
            'registration_group': '120363417671114118@g.us',
            'group_id': '120363417671114118@g.us',
            'binding_link': 'https://chat.whatsapp.com/example',
            'group_name': 'Old configured name',
        }
    }
    with service.db.connect() as conn:
        conn.execute(
            """
            INSERT INTO mcn_truth_snapshots (
                snapshot_id, object_type, object_key, snapshot_type, truth_status,
                confidence, confidence_reason, facts_json, source_json, checked_at,
                expires_at, recommended_action, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'snap-1',
                'registration_group_binding',
                'registration-a:120363417671114118@g.us',
                'pending_truth',
                'confirmed_pending',
                'verified',
                'pending_detected',
                json.dumps(facts, ensure_ascii=False),
                json.dumps(source, ensure_ascii=False),
                checked_at,
                expires_at,
                'review_or_wait_for_release_rule',
                utc_now(),
            ),
        )

    payload = service.get_production_ops_daemon_config()
    status = payload['runtime']['status']

    assert status['truth_snapshots']['count'] == 1
    assert status['truth_state']['status'] == 'confirmed_pending'
    assert status['truth_state']['pending_count'] == 4
    assert status['registration_group_cycles'][0]['truth_snapshot']['object_key'] == 'registration-a:120363417671114118@g.us'
    assert status['registration_group_cycles'][0]['decision_group_state']['source'] == 'mcn_truth_snapshots'
    assert status['registration_group_cycles'][0]['decision_group_state']['payload']['group_name'] == 'Snapshot authoritative group'


def test_binding_probe_uses_truth_snapshot_cycle_before_daemon_json():
    service = Service(Database(':memory:'))
    checked_at = datetime.now(timezone.utc).isoformat()
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()
    facts = {
        'configured_registration_group': 'group-new@g.us',
        'configured_link': 'https://chat.whatsapp.com/new',
        'configured_group_id': 'group-new@g.us',
        'configured_group_name': 'Configured',
        'actual_group_id': 'group-new@g.us',
        'actual_group_name': 'Truth Snapshot Group',
        'pending_count': 2,
        'member_count': 99,
        'requester_ids': ['r1', 'r2'],
        'requesters': [{'requesterId': 'r1'}, {'requesterId': 'r2'}],
        'runtime_active': True,
        'runtime_ready': True,
        'runtime_authenticated': True,
        'session_target_match': True,
        'login_verified': True,
    }
    source = {
        'monitor_target': {
            'account_key': 'registration-a',
            'registration_group': 'group-new@g.us',
            'group_id': 'group-new@g.us',
            'binding_link': 'https://chat.whatsapp.com/new',
            'group_name': 'Configured',
        }
    }
    with service.db.connect() as conn:
        conn.execute(
            """
            INSERT INTO mcn_truth_snapshots (
                snapshot_id, object_type, object_key, snapshot_type, truth_status,
                confidence, confidence_reason, facts_json, source_json, checked_at,
                expires_at, recommended_action, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'snap-2',
                'registration_group_binding',
                'registration-a:group-new@g.us',
                'pending_truth',
                'confirmed_pending',
                'verified',
                'pending_detected',
                json.dumps(facts, ensure_ascii=False),
                json.dumps(source, ensure_ascii=False),
                checked_at,
                expires_at,
                'review_or_wait_for_release_rule',
                utc_now(),
            ),
        )

    production_ops = service.get_production_ops_daemon_config()
    probe = Service._binding_probe_from_production_ops_status(
        production_ops,
        responsible_type='registration_group',
        account_key='registration-a',
        binding={
            'registration_group': 'group-new@g.us',
            'group_id': 'group-new@g.us',
            'link': 'https://chat.whatsapp.com/new',
            'group_name': 'Configured',
        },
    )

    assert probe['source'] == 'mcn_truth_snapshots'
    assert probe['group_name'] == 'Truth Snapshot Group'
    assert probe['pending_count'] == 2
    assert probe['truth_snapshot']['object_key'] == 'registration-a:group-new@g.us'



def _insert_ops_intake_item(service: Service, *, item_id: str, system_status: str, result_code: str = '', result_reason: str = '', snapshot: dict | None = None) -> None:
    now = utc_now()
    with service.db.connect() as conn:
        conn.execute(
            """
            INSERT INTO ops_intake_items (
                item_id, guild_name, submitted_by_user_id, submitted_by_username, raw_text,
                parsed_phone, parsed_account_id, parsed_group, parsed_code, parsed_app, parsed_agency,
                system_status, feedback_status, reply_text, result_code, result_reason, result_snapshot,
                created_at, processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                'Carote',
                'u1',
                'kefu001',
                'Phone: 8123\nID: 53341442',
                '8123',
                '53341442',
                '注册群A',
                '',
                'Linky',
                'Carote',
                system_status,
                'pending_feedback' if system_status == 'fully_success' else 'not_feedbackable',
                '',
                result_code,
                result_reason,
                json.dumps(snapshot or {}, ensure_ascii=False),
                now,
                now,
            ),
        )


def test_binding_current_truth_snapshot_is_written_and_loaded_on_item():
    service = Service(Database(':memory:'))
    _insert_ops_intake_item(
        service,
        item_id='intake_item_success',
        system_status='fully_success',
        result_code='bind_success',
        result_reason='CRM verified',
        snapshot={'crm_verified': True, 'task_id': 'task-1', 'lead_id': 'lead-1'},
    )
    item = service._get_ops_intake_item('intake_item_success')

    service._upsert_binding_current_truth_snapshot(item, {'crm_verified': True, 'task_id': 'task-1', 'lead_id': 'lead-1'})
    loaded = service._get_ops_intake_item('intake_item_success')

    assert loaded['current_truth']['truth_status'] == 'verified_success'
    assert loaded['current_truth']['confidence'] == 'verified'
    assert loaded['current_truth']['facts']['account_id'] == '53341442'
    with service.db.connect() as conn:
        row = conn.execute(
            "SELECT truth_status, confidence FROM mcn_truth_snapshots WHERE object_type='binding_submission' AND object_key='intake_item_success' AND snapshot_type='binding_current_truth'"
        ).fetchone()
    assert tuple(row) == ('verified_success', 'verified')


def test_binding_current_truth_snapshot_marks_already_in_target_guild_as_current_fact():
    service = Service(Database(':memory:'))
    _insert_ops_intake_item(
        service,
        item_id='intake_item_previous',
        system_status='bind_failed',
        result_code='already_in_target_guild',
        result_reason='Previously registered in this agency',
    )
    item = service._get_ops_intake_item('intake_item_previous')

    service._upsert_binding_current_truth_snapshot(item, {})
    loaded = service._get_ops_intake_item('intake_item_previous')

    assert loaded['current_truth']['truth_status'] == 'previously_registered'
    assert loaded['current_truth']['confidence'] == 'current_fact'
    assert loaded['current_truth']['recommended_action'] == 'manual_review_or_recheck'



def test_verify_binding_current_truth_creates_task_and_marks_success(monkeypatch):
    service = Service(Database(':memory:'))
    _insert_ops_intake_item(service, item_id='intake_item_verify', system_status='bind_failed', result_code='cms_bind_not_verified')
    calls = []

    def fake_recheck(*, item_id, fields, user):
        calls.append(item_id)
        item = service._get_ops_intake_item(item_id)
        service._upsert_binding_current_truth_snapshot(item, {'result_code': 'already_in_target_guild', 'result_reason': 'Previously registered in this agency'})
        return {'ok': True, 'item_id': item_id, 'recheck': {'result_code': 'already_in_target_guild'}, 'item': service._get_ops_intake_item(item_id)}

    monkeypatch.setattr(service, 'recheck_ops_intake_bind_failed_item', fake_recheck)

    created = service.create_verify_binding_current_truth_task(item_id='intake_item_verify', fields={'phone': '8123'}, user={'username': 'task_engine', 'role': 'internal'})
    task = service.get_operation_task(created['task_id'])

    assert created['task_type'] == 'verify_binding_current_truth'
    assert created['status'] == 'success'
    assert task['status'] == 'success'
    assert calls == ['intake_item_verify']
    item = service._get_ops_intake_item('intake_item_verify')
    assert item['current_truth']['truth_status'] == 'previously_registered'


def test_verify_binding_current_truth_task_records_failure(monkeypatch):
    service = Service(Database(':memory:'))
    _insert_ops_intake_item(service, item_id='intake_item_verify_fail', system_status='bind_failed', result_code='cms_bind_not_verified')

    def fake_recheck(*, item_id, fields, user):
        raise HTTPException(status_code=400, detail='cms_probe_unavailable')

    from app.main import HTTPException
    monkeypatch.setattr(service, 'recheck_ops_intake_bind_failed_item', fake_recheck)

    created = service.create_verify_binding_current_truth_task(item_id='intake_item_verify_fail', fields={}, user={'username': 'task_engine', 'role': 'internal'})
    task = service.get_operation_task(created['task_id'])

    assert created['status'] == 'failed'
    assert task['status'] == 'failed'
    assert task['error_code'] == 'cms_probe_unavailable'
    item = service._get_ops_intake_item('intake_item_verify_fail')
    assert item['current_truth']['truth_status'] == 'failed'


def test_binding_history_page_contains_verify_current_truth_ui_markers():
    from app.main import OPS_BIND_FAILED_USERS_PAGE_HTML
    assert 'verifyCurrentTruth' in OPS_BIND_FAILED_USERS_PAGE_HTML
    assert 'verify-current-truth' in OPS_BIND_FAILED_USERS_PAGE_HTML
    assert '核验中' in OPS_BIND_FAILED_USERS_PAGE_HTML


def test_binding_history_page_maps_current_truth_to_operator_labels():
    from app.main import OPS_BIND_FAILED_USERS_PAGE_HTML

    assert 'function currentTruthStatusMeta(row)' in OPS_BIND_FAILED_USERS_PAGE_HTML
    for label in ['已核验成功', '曾注册', '证据不足', 'CRM异常', '核验中', '需复核', '失败需处理']:
        assert label in OPS_BIND_FAILED_USERS_PAGE_HTML
    assert 'current_truth' in OPS_BIND_FAILED_USERS_PAGE_HTML
    assert '${esc(truth.truth_status' not in OPS_BIND_FAILED_USERS_PAGE_HTML
    assert '${truth.truth_status' not in OPS_BIND_FAILED_USERS_PAGE_HTML


def test_build_truth_state_marks_positive_count_without_requester_ids_unverified_pending():
    from app.registration_group_truth import build_truth_state

    truth = build_truth_state(
        status={
            'decision_group_state': {
                'source': 'worker_state',
                'payload': {
                    'pending_count': 3,
                    'requester_ids': [],
                    'approval_state_status': 'unverified_pending',
                    'unverified_pending_reason': 'pending_without_requester_ids',
                },
            }
        },
        runtime_state={'active': True, 'ready': True, 'authenticated': True},
        session_state={'session_target_match': True, 'login_verified': True},
    )

    assert truth['status'] == 'pending_unverified'
    assert truth['reason_code'] == 'pending_without_requester_ids'
    assert truth['pending_count'] == 3
    assert truth['requester_ids'] == []


def test_build_truth_state_honors_worker_confirmed_empty_status_without_dom_evidence():
    from app.registration_group_truth import build_truth_state

    truth = build_truth_state(
        status={
            'decision_group_state': {
                'source': 'worker_state',
                'payload': {
                    'pending_count': 0,
                    'requester_ids': [],
                    'approval_state_status': 'confirmed_empty',
                    'zero_pending_verified_by': 'consecutive_group_state_refresh',
                    'pending_zero_confidence': 'confirmed',
                },
            }
        },
        runtime_state={'active': True, 'ready': True, 'authenticated': True},
        session_state={'session_target_match': True, 'login_verified': True},
    )

    assert truth['status'] == 'confirmed_empty'
    assert truth['reason_code'] == 'empty_queue_confirmed'
    assert truth['zero_pending_verified_by'] == 'consecutive_group_state_refresh'
