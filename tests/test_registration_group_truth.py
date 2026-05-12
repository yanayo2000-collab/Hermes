from app.registration_group_truth import build_truth_state


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
