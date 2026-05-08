from __future__ import annotations

import sqlite3

from app.main import Service, create_app


def _make_service(tmp_path):
    app = create_app({'DB_PATH': str(tmp_path / 'automation.db')})
    return app.state.service


def _load_run_rows(service, approval_run_id: str):
    with service.db.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                'SELECT member_id, approval_run_id, registration_group, registration_group_name, requester_id, display_name, wa_phone_raw, wa_phone_normalized, requested_at, approved_at, batch_index, created_at, updated_at FROM registration_group_approval_batch_members WHERE approval_run_id = ? ORDER BY batch_index ASC',
                (approval_run_id,),
            ).fetchall()
        ]


def test_registration_group_batch_member_name_needs_repair_detects_blank_and_placeholder_names():
    assert Service._registration_group_batch_member_name_needs_repair('') is True
    assert Service._registration_group_batch_member_name_needs_repair('   ') is True
    assert Service._registration_group_batch_member_name_needs_repair('.') is True
    assert Service._registration_group_batch_member_name_needs_repair('--') is True
    assert Service._registration_group_batch_member_name_needs_repair('~') is True
    assert Service._registration_group_batch_member_name_needs_repair('Dini Lubis') is False


def test_repair_registration_group_batch_member_rows_updates_resolved_names_and_phones(tmp_path, monkeypatch):
    service = _make_service(tmp_path)
    approval_run_id = 'registration_group_approval_test_repair_1'
    service._replace_registration_group_approval_batch_members(
        approval_run_id=approval_run_id,
        registration_group='120363425215002840@g.us',
        registration_group_name='测试注册群',
        approved_at='2026-05-08T01:53:20.168Z',
        selected_candidates=[
            {
                'requesterId': '105677921984582@lid',
                'displayName': '',
                'phoneRaw': '+628****3728',
                'phoneNormalized': '+628****3728',
                'requestedAtIso': '2026-05-08T01:42:11.000Z',
            },
            {
                'requesterId': '2706332733540@lid',
                'displayName': '.',
                'phoneRaw': '+628****9030',
                'phoneNormalized': '+628****9030',
                'requestedAtIso': '2026-05-08T02:11:28.000Z',
            },
        ],
    )
    rows = _load_run_rows(service, approval_run_id)

    monkeypatch.setattr(service, '_list_registration_group_batch_member_runtime_candidates', lambda **kwargs: [
        {'account_key': 'wa-admin-demo-1', 'auth_path': '/tmp/auth', 'client_id': 'wa-approval-wa-admin-demo-1'}
    ])
    monkeypatch.setattr(service, '_resolve_registration_group_batch_member_contacts_via_runtime', lambda **kwargs: [
        {
            'requester_id': '105677921984582@lid',
            'display_name': 'Dini Lubis',
            'phone_from_lid': '6281378053728@c.us',
            'phone_from_contact_id': '6281378053728',
        },
        {
            'requester_id': '2706332733540@lid',
            'display_name': '.',
            'phone_from_lid': '62895341529030@c.us',
            'phone_from_contact_id': '62895341529030',
        },
    ])

    result = service._repair_registration_group_batch_member_rows(
        rows=rows,
        registration_group='120363425215002840@g.us',
        registration_group_name='测试注册群',
    )

    assert result['candidates'] == 2
    assert result['updated'] == 2
    updated_rows = _load_run_rows(service, approval_run_id)
    assert updated_rows[0]['display_name'] == 'Dini Lubis'
    assert updated_rows[0]['wa_phone_raw'] == '+6281378053728'
    assert updated_rows[0]['wa_phone_normalized'] == '+6281378053728'
    assert updated_rows[1]['display_name'] == '.'
    assert updated_rows[1]['wa_phone_raw'] == '+62895341529030'
    assert updated_rows[1]['wa_phone_normalized'] == '+62895341529030'


def test_list_registration_group_approval_batch_members_triggers_repair_for_bad_names(tmp_path, monkeypatch):
    service = _make_service(tmp_path)
    service._replace_registration_group_approval_batch_members(
        approval_run_id='registration_group_approval_test_repair_2',
        registration_group='120363425215002840@g.us',
        registration_group_name='测试注册群',
        approved_at='2026-05-08T01:53:20.168Z',
        selected_candidates=[
            {
                'requesterId': '105677921984582@lid',
                'displayName': '',
                'phoneRaw': '+628****3728',
                'phoneNormalized': '+628****3728',
                'requestedAtIso': '2026-05-08T01:42:11.000Z',
            }
        ],
    )
    called = {}

    def fake_repair(**kwargs):
        called['rows'] = kwargs['rows']
        called['registration_group'] = kwargs['registration_group']
        called['registration_group_name'] = kwargs['registration_group_name']
        return {'updated': 0}

    monkeypatch.setattr(service, '_repair_registration_group_batch_member_rows', fake_repair)

    result = service.list_registration_group_approval_batch_members(
        approved_date_start='2026-05-08',
        approved_date_end='2026-05-08',
        limit=30,
        page=1,
    )

    assert result['summary']['total_members'] == 1
    assert len(called['rows']) == 1
    assert called['registration_group'] == '120363425215002840@g.us'
    assert called['registration_group_name'] == '测试注册群'
