from fastapi.testclient import TestClient

from app.main import OfficialGroupBatchRunRequest, create_app


def _make_app(tmp_path):
    return create_app({
        'DB_PATH': str(tmp_path / 'automation.db'),
        'AUTO_LARK_REPLY': False,
        'GROUP_ATMOSPHERE_SCHEDULER_ENABLED': False,
    })


def _make_service(tmp_path):
    app = _make_app(tmp_path)
    return app.state.service


def test_production_ops_page_exposes_official_group_runtime_diagnostic_panel(tmp_path):
    app = _make_app(tmp_path)
    client = TestClient(app)

    response = client.get('/ops/production-ops')

    assert response.status_code == 200
    text = response.text
    assert '官方群运行诊断' in text
    assert 'officialGroupDiagnosticTargetGroup' in text
    assert 'officialGroupDiagnosticMeta' in text
    assert 'lookupOfficialGroupRuntimeMember' in text
    assert '/api/ops/group-approvals/executor/group-metadata' in text
    assert '/api/ops/group-approvals/executor/member-lookup' in text
    assert text.index('WhatsApp 审批账号') < text.index('官方群运行诊断')


def test_run_ready_official_group_batches_unmatched_requester_includes_runtime_snapshot(tmp_path):
    service = _make_service(tmp_path)
    service.approval_batch_queue = lambda: {
        'official_groups': [{
            'ready': True,
            'registration_group': 'rg-id',
            'target_group': 'official-group-id',
            'requesters': [{
                'requester_id': 'req-1',
                'phoneNormalized': '628111',
                'display_name': 'Alice',
            }],
            'release_count': 1,
        }]
    }
    service._match_official_group_requesters_to_leads = lambda **kwargs: ([], list(kwargs.get('requesters') or []))
    service._group_approval_executor_lookup_snapshot = lambda **kwargs: {
        'group_metadata': {
            'group_name': 'Official Group ID',
            'pending_count': 3,
        },
        'runtime_member_lookup': {
            'match_count': 1,
            'matches': [{'matched_by': ['phone_hint']}],
            'requester_ids': ['req-1'],
            'lookup': {'phone_hint': '628111'},
        },
    }
    service._send_official_group_success_notifications = lambda **kwargs: []

    result = service.run_ready_official_group_batches(OfficialGroupBatchRunRequest(decided_at='2026-06-02T18:00:00+00:00'))

    assert result['skipped_count'] == 1
    detail = result['results'][0]
    assert detail['reason_code'] == 'official_group_requester_unmatched'
    assert detail['group_metadata']['group_name'] == 'Official Group ID'
    assert detail['runtime_member_lookup']['match_count'] == 1


def test_run_ready_official_group_batches_manual_review_result_includes_runtime_snapshot(tmp_path):
    service = _make_service(tmp_path)
    with service.db.connect() as conn:
        conn.execute(
            """
            INSERT INTO leads (
                lead_id, trace_id, source_platform, source_page_id, country, area_code,
                mobile, pendaftaran_group, current_status, matched_customer_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                'lead-1',
                'trace-1',
                'facebook',
                'page-1',
                'ID',
                62,
                '628222',
                'rg-id',
                'bind_success',
                'cust-1',
                '2026-06-02T18:00:00+00:00',
                '2026-06-02T18:00:00+00:00',
            ),
        )
        conn.commit()
    service.approval_batch_queue = lambda: {
        'official_groups': [{
            'ready': True,
            'registration_group': 'rg-id',
            'target_group': 'official-group-id',
            'requesters': [],
            'release_count': 1,
        }]
    }
    service._lead_eligible_for_official_group_runtime_matching = lambda **kwargs: True
    service._match_official_group_requesters_to_leads = lambda **kwargs: (list(kwargs.get('lead_rows') or []), [])
    service._resolve_official_group_target_group = lambda **kwargs: 'official-group-id'
    service.official_group_approval_decision = lambda payload: {
        'executed': False,
        'next_action': 'manual_review_official_group_approval',
        'target_requester_id': 'req-2',
        'target_phone_hint': '628222',
        'target_name_hint': 'Bob',
    }
    service._resolve_official_group_display_name = lambda **kwargs: 'Official Group ID'
    service._group_approval_executor_lookup_snapshot = lambda **kwargs: {
        'group_metadata': {
            'group_name': 'Official Group ID',
            'pending_count': 2,
        },
        'runtime_member_lookup': {
            'match_count': 1,
            'matches': [{'matched_by': ['requester_id']}],
            'requester_ids': ['req-2'],
            'lookup': {'requester_id': 'req-2'},
        },
    }
    service._send_official_group_success_notifications = lambda **kwargs: []

    result = service.run_ready_official_group_batches(OfficialGroupBatchRunRequest(decided_at='2026-06-02T18:00:00+00:00'))

    detail = result['results'][0]
    assert detail['next_action'] == 'manual_review_official_group_approval'
    assert detail['group_name'] == 'Official Group ID'
    assert detail['runtime_member_lookup']['requester_ids'] == ['req-2']
