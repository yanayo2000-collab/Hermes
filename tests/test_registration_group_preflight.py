from app.registration_group_preflight import evaluate_registration_group_webjs_preflight


def test_preflight_marks_session_stale_on_count_mismatch():
    result = evaluate_registration_group_webjs_preflight(
        registration_group='8️⃣5️⃣',
        worker_health={
            'status': 'warm',
            'auth_strategy': 'ChromeProfileCopy+NoAuth',
            'ready': True,
            'authenticated': True,
        },
        worker_warmup={'status': 'warm', 'warmup_outcome': 'ready'},
        worker_group_state={'pending_count': 0, 'member_count': 6},
        fresh_group_state={'pending_count': 2, 'member_count': 4},
    )

    assert result['ok'] is False
    assert result['stale_session_detected'] is True
    assert 'pending_count_mismatch' in result['reasons']
    assert 'member_count_mismatch' in result['reasons']


def test_preflight_marks_not_ready_when_worker_auth_strategy_falls_back_to_localauth():
    result = evaluate_registration_group_webjs_preflight(
        registration_group='8️⃣5️⃣',
        worker_health={
            'status': 'awaiting_qr',
            'auth_strategy': 'LocalAuth',
            'ready': False,
            'authenticated': False,
        },
        worker_warmup={'status': 'awaiting_qr', 'warmup_outcome': 'qr'},
        worker_group_state={},
        fresh_group_state={'pending_count': 2, 'member_count': 4},
    )

    assert result['ok'] is False
    assert result['stale_session_detected'] is False
    assert 'unexpected_auth_strategy' in result['reasons']
    assert 'worker_not_ready' in result['reasons']


def test_preflight_passes_when_counts_and_health_match():
    result = evaluate_registration_group_webjs_preflight(
        registration_group='8️⃣5️⃣',
        worker_health={
            'status': 'warm',
            'auth_strategy': 'ChromeProfileCopy+NoAuth',
            'ready': True,
            'authenticated': True,
        },
        worker_warmup={'status': 'warm', 'warmup_outcome': 'ready'},
        worker_group_state={'pending_count': 2, 'member_count': 4},
        fresh_group_state={'pending_count': 2, 'member_count': 4},
    )

    assert result['ok'] is True
    assert result['stale_session_detected'] is False
    assert result['reasons'] == []


def test_preflight_blocks_when_worker_regresses_below_last_verified_state_and_fresh_probe_does_not_confirm_it():
    result = evaluate_registration_group_webjs_preflight(
        registration_group='8️⃣5️⃣',
        worker_health={
            'status': 'warm',
            'auth_strategy': 'ChromeProfileCopy+NoAuth',
            'ready': True,
            'authenticated': True,
        },
        worker_warmup={'status': 'warm', 'warmup_outcome': 'ready'},
        worker_group_state={'pending_count': 2, 'member_count': 4},
        fresh_group_state={'pending_count': 1, 'member_count': 5, 'requester_ids': ['64163187581105@lid']},
        last_verified_group_state={'pending_count': 1, 'member_count': 5, 'requester_ids': ['64163187581105@lid']},
    )

    assert result['ok'] is False
    assert result['stale_session_detected'] is True
    assert 'worker_regressed_from_last_verified' in result['reasons']


def test_preflight_allows_consistent_new_queue_even_if_it_regresses_vs_last_verified():
    result = evaluate_registration_group_webjs_preflight(
        registration_group='8️⃣5️⃣',
        worker_health={
            'status': 'warm',
            'auth_strategy': 'ChromeProfileCopy+NoAuth',
            'ready': True,
            'authenticated': True,
        },
        worker_warmup={'status': 'warm', 'warmup_outcome': 'ready'},
        worker_group_state={'pending_count': 2, 'member_count': 4, 'requester_ids': ['216067590889549@lid', '64163187581105@lid']},
        fresh_group_state={'pending_count': 2, 'member_count': 4, 'requester_ids': ['216067590889549@lid', '64163187581105@lid']},
        last_verified_group_state={'pending_count': 0, 'member_count': 6, 'requester_ids': []},
    )

    assert result['ok'] is True
    assert result['stale_session_detected'] is False
    assert 'new_queue_detected_since_last_verified' in result['warnings']


def test_preflight_blocks_when_counts_match_but_requester_fingerprint_differs():
    result = evaluate_registration_group_webjs_preflight(
        registration_group='8️⃣5️⃣',
        worker_health={
            'status': 'warm',
            'auth_strategy': 'ChromeProfileCopy+NoAuth',
            'ready': True,
            'authenticated': True,
        },
        worker_warmup={'status': 'warm', 'warmup_outcome': 'ready'},
        worker_group_state={
            'pending_count': 2,
            'member_count': 4,
            'requester_ids': ['aaa@lid', 'bbb@lid'],
            'requesters': [
                {'requesterId': 'aaa@lid', 'requestedAtUnix': 100},
                {'requesterId': 'bbb@lid', 'requestedAtUnix': 200},
            ],
        },
        fresh_group_state={
            'pending_count': 2,
            'member_count': 4,
            'requester_ids': ['ccc@lid', 'ddd@lid'],
            'requesters': [
                {'requesterId': 'ccc@lid', 'requestedAtUnix': 300},
                {'requesterId': 'ddd@lid', 'requestedAtUnix': 400},
            ],
        },
    )

    assert result['ok'] is False
    assert result['stale_session_detected'] is True
    assert 'requester_fingerprint_mismatch' in result['reasons']


def test_preflight_prefers_worker_when_ids_match_but_worker_request_times_are_newer():
    result = evaluate_registration_group_webjs_preflight(
        registration_group='8️⃣5️⃣',
        worker_health={
            'status': 'warm',
            'auth_strategy': 'ChromeProfileCopy+NoAuth',
            'ready': True,
            'authenticated': True,
        },
        worker_warmup={'status': 'warm', 'warmup_outcome': 'ready'},
        worker_group_state={
            'pending_count': 2,
            'member_count': 4,
            'requester_ids': ['aaa@lid', 'bbb@lid'],
            'requesters': [
                {'requesterId': 'aaa@lid', 'requestedAtUnix': 500},
                {'requesterId': 'bbb@lid', 'requestedAtUnix': 600},
            ],
        },
        fresh_group_state={
            'pending_count': 2,
            'member_count': 4,
            'requester_ids': ['aaa@lid', 'bbb@lid'],
            'requesters': [
                {'requesterId': 'aaa@lid', 'requestedAtUnix': 100},
                {'requesterId': 'bbb@lid', 'requestedAtUnix': 200},
            ],
        },
        last_verified_group_state={'pending_count': 0, 'member_count': 6, 'requester_ids': []},
    )

    assert result['ok'] is True
    assert result['stale_session_detected'] is False
    assert 'fresh_probe_requester_timestamps_stale' in result['warnings']


def test_preflight_treats_matching_requester_fingerprint_as_same_new_queue():
    result = evaluate_registration_group_webjs_preflight(
        registration_group='8️⃣5️⃣',
        worker_health={
            'status': 'warm',
            'auth_strategy': 'ChromeProfileCopy+NoAuth',
            'ready': True,
            'authenticated': True,
        },
        worker_warmup={'status': 'warm', 'warmup_outcome': 'ready'},
        worker_group_state={
            'pending_count': 2,
            'member_count': 4,
            'requester_ids': ['aaa@lid', 'bbb@lid'],
            'requesters': [
                {'requesterId': 'aaa@lid', 'requestedAtUnix': 100},
                {'requesterId': 'bbb@lid', 'requestedAtUnix': 200},
            ],
        },
        fresh_group_state={
            'pending_count': 2,
            'member_count': 4,
            'requester_ids': ['aaa@lid', 'bbb@lid'],
            'requesters': [
                {'requesterId': 'aaa@lid', 'requestedAtUnix': 100},
                {'requesterId': 'bbb@lid', 'requestedAtUnix': 200},
            ],
        },
        last_verified_group_state={'pending_count': 0, 'member_count': 6, 'requester_ids': []},
    )

    assert result['ok'] is True
    assert result['stale_session_detected'] is False
    assert 'new_queue_detected_since_last_verified' in result['warnings']


def test_preflight_allows_dedicated_localauth_when_expected_auth_strategy_matches():
    result = evaluate_registration_group_webjs_preflight(
        registration_group='8️⃣5️⃣',
        worker_health={
            'status': 'warm',
            'auth_strategy': 'LocalAuth',
            'ready': True,
            'authenticated': True,
        },
        worker_warmup={'status': 'warm', 'warmup_outcome': 'ready'},
        worker_group_state={'pending_count': 2, 'member_count': 4},
        fresh_group_state={'pending_count': 2, 'member_count': 4},
        expected_auth_strategy='LocalAuth',
    )

    assert result['ok'] is True
    assert result['expected_auth_strategy'] == 'LocalAuth'
    assert result['reasons'] == []
