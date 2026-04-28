from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.production_ops import (
    build_incidents,
    format_lark_alert,
    record_trigger,
    register_notification,
    requester_fingerprint,
    should_trigger_action,
)


def test_requester_fingerprint_prefers_requester_id_and_timestamp():
    group_state = {
        'requesters': [
            {'requesterId': 'u2', 'requestedAtUnix': 200},
            {'requesterId': 'u1', 'requestedAtUnix': 100},
        ]
    }

    assert requester_fingerprint(group_state) == 'u1@100|u2@200'


def test_should_trigger_action_respects_recent_same_fingerprint_cooldown():
    now = datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc)
    state = {}
    record_trigger(state, fingerprint='abc', now=now)

    assert should_trigger_action(state, fingerprint='abc', now=now + timedelta(seconds=30), cooldown_seconds=120) is False
    assert should_trigger_action(state, fingerprint='abc', now=now + timedelta(seconds=121), cooldown_seconds=120) is True
    assert should_trigger_action(state, fingerprint='xyz', now=now + timedelta(seconds=30), cooldown_seconds=120) is True


def test_register_notification_suppresses_duplicate_within_cooldown():
    now = datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc)
    state = {}

    assert register_notification(state, dedupe_key='backend_unhealthy', now=now, cooldown_seconds=900) is True
    assert register_notification(state, dedupe_key='backend_unhealthy', now=now + timedelta(seconds=60), cooldown_seconds=900) is False
    assert register_notification(state, dedupe_key='backend_unhealthy', now=now + timedelta(seconds=901), cooldown_seconds=900) is True


def test_build_incidents_emits_backend_and_formal_approval_failures():
    cycle = {
        'checked_at': '2026-04-28T10:00:00+00:00',
        'registration_group': 'RG',
        'backend_health': {'ok': False, 'error': 'connection refused'},
        'formal_approval': {
            'triggered': True,
            'ok': False,
            'fingerprint': 'abc',
            'result': {'formal_run': {'approval_run_id': 'run-123'}},
        },
    }

    incidents = build_incidents(cycle)

    assert [item['code'] for item in incidents] == ['backend_unhealthy', 'formal_approval_failed']
    assert incidents[1]['dedupe_key'] == 'formal_approval_failed:run-123'


def test_format_lark_alert_contains_summary_and_details():
    cycle = {
        'checked_at': '2026-04-28T10:00:00+00:00',
        'registration_group': 'RG',
        'formal_approval': {'triggered': True, 'fingerprint': 'abc'},
    }
    incident = {
        'severity': 'critical',
        'code': 'formal_approval_failed',
        'summary': 'formal approval run failed',
        'details': {'returncode': 2},
    }

    text = format_lark_alert('production-ops-daemon', incident, cycle)

    assert '[production-ops-daemon] CRITICAL formal_approval_failed' in text
    assert 'registration_group: RG' in text
    assert 'fingerprint: abc' in text
    assert '"returncode": 2' in text
