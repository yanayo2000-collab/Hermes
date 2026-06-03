from app.registration_group_truth import (
    build_approval_queue_display,
    build_truth_state,
    serialize_membership_verifier,
)


def test_build_truth_state_prefers_latest_decision_group_over_stale_truth_state():
    truth_state = build_truth_state(
        status={
            'truth_state': {
                'status': 'empty_unverified',
                'source': 'fresh_probe',
                'payload': {
                    'group_name': 'RG',
                    'group_id': 'g',
                    'pending_count': 0,
                    'member_count': 100,
                    'requester_ids': [],
                    'requesters': [],
                },
                'zero_pending_unverified': True,
                'zero_pending_unverified_reason': 'stale_zero',
            },
            'decision_group_state': {
                'source': 'review_surface_state',
                'payload': {
                    'group_name': 'RG',
                    'group_id': 'g',
                    'pending_count': 5,
                    'member_count': 105,
                    'requester_ids': ['req-1'],
                    'requesters': [{'requesterId': 'req-1'}],
                    'review_surface_ready': True,
                    'has_pending_section': True,
                    'has_pending_request_row': True,
                },
            },
        },
        runtime_state={'active': True, 'ready': True, 'authenticated': True},
    )

    assert truth_state['status'] == 'confirmed_pending'
    assert truth_state['source'] == 'review_surface_state'
    assert truth_state['pending_count'] == 5
    assert truth_state['zero_pending_unverified'] is False


def test_build_truth_state_treats_zero_recheck_resolved_zero_as_confirmed_empty():
    truth_state = build_truth_state(
        status={
            'decision_group_state': {
                'source': 'worker_state',
                'payload': {
                    'group_name': 'RG',
                    'group_id': 'g',
                    'pending_count': 0,
                    'member_count': 100,
                    'requester_ids': [],
                    'requesters': [],
                    'zero_pending_unverified': False,
                    'zero_pending_recheck_attempted': True,
                    'zero_pending_recheck_resolved': True,
                    'zero_pending_recheck_count': 1,
                },
            },
        },
        runtime_state={'active': True, 'ready': True, 'authenticated': True},
    )

    assert truth_state['status'] == 'confirmed_empty'
    assert truth_state['reason_code'] == 'zero_pending_recheck_confirmed'
    assert truth_state['pending_count'] == 0
    assert truth_state['zero_pending_unverified'] is False


def test_build_approval_queue_display_marks_stale_zero_as_non_count_state():
    display = build_approval_queue_display({
        'truth_status': 'confirmed_empty',
        'pending_count': 0,
        'stale': True,
    })

    assert display['state'] == 'STALE'
    assert display['show_count'] is False
    assert display['count'] is None
    assert display['primary_text'] == '当前审批列表 0 人'
    assert display['secondary_text'] == ''
    assert display['debug_count'] == 0


def test_build_approval_queue_display_keeps_current_list_pending_as_stale_primary_text():
    display = build_approval_queue_display({
        'truth_status': 'confirmed_pending',
        'pending_count': 5,
        'stale': True,
    })

    assert display['state'] == 'STALE'
    assert display['show_count'] is False
    assert display['count'] is None
    assert display['primary_text'] == '当前审批列表 5 人'
    assert display['secondary_text'] == ''
    assert display['debug_count'] == 5


def test_build_approval_queue_display_keeps_confirmed_pending_copy_on_same_line():
    display = build_approval_queue_display({
        'truth_status': 'confirmed_pending',
        'pending_count': 8,
        'freshness_level': 'FRESH',
        'display_trusted': True,
    })

    assert display['state'] == 'COUNT'
    assert display['show_count'] is True
    assert display['count'] == 8
    assert display['primary_text'] == '待审批 8 人'
    assert display['secondary_text'] == ''


def test_build_approval_queue_display_uses_verifying_for_api_ui_not_converged():
    display = build_approval_queue_display({
        'truth_status': 'TRUTH_UNKNOWN',
        'confidence_reason': 'api_pending_ui_not_converged',
        'api_pending_count': 11,
        'ui_pending_count': 0,
        'freshness_level': 'FRESH',
        'display_trusted': False,
    })

    assert display['state'] == 'UNKNOWN'
    assert display['show_count'] is False
    assert display['debug_count'] is None
    assert display['primary_text'] == '审批队列待刷新'


def test_serialize_membership_verifier_strips_pending_numbers_from_detail():
    verifier = serialize_membership_verifier({
        'probe_connected': True,
        'has_admin_permission': True,
        'group_name': '🇮🇩 31- Grup Registrasi Resmi Linky 💎',
        'detail': '已接探针：待审批 0 人。已有管理员权限。当前群：🇮🇩 31- Grup Registrasi Resmi Linky 💎',
        'pending_count': 0,
        'api_pending_count': 0,
        'probe_pending_count': 0,
    })

    assert verifier['safe_detail'] == '已接探针。已有管理员权限。当前群：🇮🇩 31- Grup Registrasi Resmi Linky 💎'
    assert '待审批 0 人' not in verifier['detail']
    assert verifier['detail_deprecated'] is True
    assert 'pending_count' not in verifier
