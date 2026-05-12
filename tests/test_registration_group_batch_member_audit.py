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
                'SELECT member_id, approval_run_id, registration_group, registration_group_name, requester_id, display_name, wa_phone_raw, wa_phone_normalized, requested_at, approved_at, batch_index, repair_last_attempt_at, repair_last_result, repair_next_attempt_at, created_at, updated_at FROM registration_group_approval_batch_members WHERE approval_run_id = ? ORDER BY batch_index ASC',
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


def test_repair_registration_group_batch_member_rows_respects_unresolved_cooldown(tmp_path, monkeypatch):
    service = _make_service(tmp_path)
    approval_run_id = 'registration_group_approval_test_repair_cooldown'
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
            }
        ],
    )
    monkeypatch.setattr(service, '_list_registration_group_batch_member_runtime_candidates', lambda **kwargs: [
        {'account_key': 'wa-admin-demo-1', 'auth_path': '/tmp/auth', 'client_id': 'wa-approval-wa-admin-demo-1'}
    ])
    monkeypatch.setattr(service, '_resolve_registration_group_batch_member_contacts_via_runtime', lambda **kwargs: [])

    first = service._repair_registration_group_batch_member_rows(
        rows=_load_run_rows(service, approval_run_id),
        registration_group='120363425215002840@g.us',
        registration_group_name='测试注册群',
    )
    assert first['candidates'] == 1
    assert first['updated'] == 0
    assert first['unresolved'] == 1
    assert first['skipped_cooldown'] == 0

    rows_after_first = _load_run_rows(service, approval_run_id)
    assert rows_after_first[0]['repair_last_result'] == 'unresolved'
    assert rows_after_first[0]['repair_last_attempt_at']
    assert rows_after_first[0]['repair_next_attempt_at']

    second = service._repair_registration_group_batch_member_rows(
        rows=rows_after_first,
        registration_group='120363425215002840@g.us',
        registration_group_name='测试注册群',
    )
    assert second['candidates'] == 0
    assert second['skipped_cooldown'] == 1

    forced = service._repair_registration_group_batch_member_rows(
        rows=rows_after_first,
        registration_group='120363425215002840@g.us',
        registration_group_name='测试注册群',
        force=True,
    )
    assert forced['candidates'] == 1
    assert forced['skipped_cooldown'] == 0


def test_list_registration_group_approval_batch_members_does_not_trigger_repair_on_read_path(tmp_path, monkeypatch):
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
    called = {'count': 0}

    def fake_repair(**kwargs):
        called['count'] += 1
        return {'updated': 0}

    monkeypatch.setattr(service, '_repair_registration_group_batch_member_rows', fake_repair)

    result = service.list_registration_group_approval_batch_members(
        approved_date_start='2026-05-08',
        approved_date_end='2026-05-08',
        limit=30,
        page=1,
    )

    assert result['summary']['total_members'] == 1
    assert called['count'] == 0
    assert result['rows'][0]['display_name'] == ''
    assert result['rows'][0]['wa_phone_raw'] == '+628****3728'


def test_list_registration_group_approval_batch_members_only_enriches_date_matched_unique_phones(tmp_path, monkeypatch):
    service = _make_service(tmp_path)
    service._replace_registration_group_approval_batch_members(
        approval_run_id='registration_group_approval_test_perf_a',
        registration_group='120363425215002840@g.us',
        registration_group_name='测试注册群A',
        approved_at='2026-05-08T01:53:20.168Z',
        selected_candidates=[
            {
                'requesterId': 'requester-a1',
                'displayName': 'Alice',
                'phoneRaw': '+628123450001',
                'phoneNormalized': '+628123450001',
                'requestedAtIso': '2026-05-08T01:42:11.000Z',
            },
            {
                'requesterId': 'requester-a2',
                'displayName': 'Alice-dup',
                'phoneRaw': '+628123450001',
                'phoneNormalized': '+628123450001',
                'requestedAtIso': '2026-05-08T01:45:11.000Z',
            },
        ],
    )
    service._replace_registration_group_approval_batch_members(
        approval_run_id='registration_group_approval_test_perf_b',
        registration_group='120363425215002841@g.us',
        registration_group_name='测试注册群B',
        approved_at='2026-05-09T01:53:20.168Z',
        selected_candidates=[
            {
                'requesterId': 'requester-b1',
                'displayName': 'Bob',
                'phoneRaw': '+628123450999',
                'phoneNormalized': '+628123450999',
                'requestedAtIso': '2026-05-09T01:42:11.000Z',
            }
        ],
    )

    snapshot_calls = []

    def fake_snapshot(conn, *, wa_phone_raw: str, wa_phone_normalized: str, allow_live_crm: bool = True):
        snapshot_calls.append((wa_phone_raw, wa_phone_normalized))
        return {
            'registration_status': 'registered',
            'registration_status_label': '已注册',
            'lead_id': 'lead-demo',
            'lead_current_status': 'synced',
            'submission_count': 1,
            'country': 'Indonesia',
            'area_code': 62,
        }

    monkeypatch.setattr(service, '_registration_group_batch_member_registration_snapshot', fake_snapshot)

    result = service.list_registration_group_approval_batch_members(
        approved_date_start='2026-05-08',
        approved_date_end='2026-05-08',
        limit=30,
        page=1,
    )

    assert result['summary']['total_members'] == 2
    assert len(result['rows']) == 2
    assert snapshot_calls == [('+628123450001', '+628123450001')]


def test_list_registration_group_approval_batch_members_does_not_call_live_crm_on_read_path(tmp_path):
    service = _make_service(tmp_path)
    service._replace_registration_group_approval_batch_members(
        approval_run_id='registration_group_approval_test_perf_crm',
        registration_group='120363425215002842@g.us',
        registration_group_name='测试注册群CRM',
        approved_at='2026-05-08T03:53:20.168Z',
        selected_candidates=[
            {
                'requesterId': 'requester-crm-1',
                'displayName': 'Carol',
                'phoneRaw': '+628123450777',
                'phoneNormalized': '+628123450777',
                'requestedAtIso': '2026-05-08T03:42:11.000Z',
            }
        ],
    )
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO leads (lead_id, trace_id, source_platform, source_campaign, source_page_id, country, area_code, mobile, current_status, matched_customer_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                'lead_crm_read_path',
                'trace_crm_read_path',
                'meta',
                'campaign',
                'page',
                'Indonesia',
                62,
                '8123450777',
                'archived_test_residue',
                'customer_crm_read_path',
                '2026-05-08T03:40:00Z',
                '2026-05-08T03:40:00Z',
            ),
        )
        conn.commit()

    class StubCrmAdapter:
        def __init__(self):
            self.calls = []

        def find_customer(self, *, yw_id=None, mobile=None):
            self.calls.append({'yw_id': yw_id, 'mobile': mobile})
            return None

    crm = StubCrmAdapter()
    service.crm_adapter = crm

    result = service.list_registration_group_approval_batch_members(
        approved_date_start='2026-05-08',
        approved_date_end='2026-05-08',
        limit=30,
        page=1,
    )

    assert result['summary']['total_members'] == 1
    assert crm.calls == []
