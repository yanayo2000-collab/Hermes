from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.production_ops import (
    build_incidents,
    build_success_notifications,
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
            'returncode': 0,
            'result': {
                'formal_run': {
                    'approval_run_id': 'run-123',
                    'final_status': {
                        'result': {
                            'verified': False,
                            'crm_recorded': False,
                        }
                    },
                }
            },
        },
        'startup_initial_batch': {
            'triggered': True,
            'ok': False,
            'session_id': 'session-1',
            'result': {'formal_run': {'approval_run_id': 'startup-run-1'}},
        },
    }

    incidents = build_incidents(cycle)

    assert [item['code'] for item in incidents] == ['backend_unhealthy', 'formal_approval_failed', 'startup_initial_batch_failed']
    assert incidents[0]['summary'] == '后端健康检查失败'
    assert incidents[1]['summary'] == '正式审批未闭环'
    assert incidents[1]['dedupe_key'] == 'formal_approval_failed:abc'
    assert incidents[1]['details']['fingerprint'] == 'abc'
    assert incidents[2]['summary'] == '启动首批审批失败'
    assert incidents[2]['dedupe_key'] == 'startup_initial_batch_failed:session-1'
    assert incidents[2]['details']['session_id'] == 'session-1'


def test_build_incidents_uses_stable_unknown_dedupe_when_ids_missing():
    cycle = {
        'backend_health': {'ok': True},
        'worker_state': {'ok': True},
        'release_evaluation': {'ok': True},
        'formal_approval': {
            'triggered': True,
            'ok': False,
            'returncode': 0,
            'result': {
                'formal_run': {
                    'approval_run_id': 'run-123',
                    'final_status': {
                        'result': {
                            'verified': False,
                            'crm_recorded': False,
                        }
                    },
                }
            },
        },
        'startup_initial_batch': {
            'triggered': True,
            'ok': False,
            'result': {'formal_run': {'approval_run_id': 'startup-run-1'}},
        },
    }

    incidents = build_incidents(cycle)

    assert incidents[0]['dedupe_key'] == 'formal_approval_failed:unknown'
    assert incidents[1]['dedupe_key'] == 'startup_initial_batch_failed:unknown'


def test_build_incidents_skips_false_positive_when_final_status_already_succeeded():
    cycle = {
        'checked_at': '2026-04-29T05:57:19+00:00',
        'registration_group': 'RG',
        'backend_health': {'ok': True},
        'worker_state': {'ok': True},
        'release_evaluation': {'ok': True},
        'formal_approval': {
            'triggered': True,
            'ok': False,
            'fingerprint': 'fp-1',
            'pending_count': 2,
            'release_count': 2,
            'returncode': 0,
            'result': {
                'formal_run': {
                    'approval_run_id': 'run-4654',
                    'final_status': {
                        'result': {
                            'verified': True,
                            'crm_recorded': True,
                            'result_code': 'approved',
                        }
                    },
                }
            },
        },
    }

    incidents = build_incidents(cycle)

    assert incidents == []


def test_build_success_notifications_emits_registration_group_approval_success_once_per_run():
    cycle = {
        'checked_at': '2026-04-29T05:57:32+00:00',
        'registration_group': 'https://chat.whatsapp.com/Bp1WKsmpcbC2RkAyIACeRv',
        'formal_approval': {
            'triggered': True,
            'ok': True,
            'fingerprint': 'fp-85',
            'pending_count': 2,
            'release_count': 2,
            'reason_code': 'timeout_flush',
            'result': {
                'formal_run': {
                    'approval_run_id': 'registration_group_approval_4654cdd3a95b',
                    'final_status': {
                        'result': {
                            'verified': True,
                            'crm_recorded': True,
                            'result_code': 'approved',
                            'approved_count': 2,
                            'pending_after': 0,
                            'member_count_after': 6,
                        }
                    },
                }
            },
        },
    }

    notifications = build_success_notifications(cycle)

    assert len(notifications) == 1
    assert notifications[0]['code'] == 'formal_approval_succeeded'
    assert notifications[0]['severity'] == 'info'
    assert notifications[0]['summary'] == '注册群审批成功'
    assert notifications[0]['dedupe_key'] == 'formal_approval_succeeded:registration_group_approval_4654cdd3a95b'
    assert notifications[0]['details']['approved_count'] == 2
    assert notifications[0]['details']['pending_after'] == 0



def test_format_lark_alert_contains_summary_and_reason():
    cycle = {
        'checked_at': '2026-04-28T10:00:00+00:00',
        'registration_group': 'RG',
        'formal_approval': {
            'triggered': True,
            'fingerprint': 'abc',
            'returncode': 2,
            'release_count': 8,
        },
    }
    incident = {
        'severity': 'critical',
        'code': 'formal_approval_failed',
        'summary': '正式审批失败',
        'details': {'returncode': 2, 'release_count': 8},
    }

    text = format_lark_alert('production-ops-daemon', incident, cycle)

    assert '[production-ops-daemon] CRITICAL formal_approval_failed' in text
    assert '时间: 2026-04-28 18:00:00 UTC+8' in text
    assert '注册群: RG' in text
    assert '批次人数: 8' in text
    assert '原因: 审批脚本执行失败' in text



def test_format_lark_alert_contains_compact_success_summary():
    cycle = {
        'checked_at': '2026-04-29T05:57:32+00:00',
        'registration_group': 'RG',
        'formal_approval': {
            'triggered': True,
            'ok': True,
            'release_count': 2,
            'result': {
                'formal_run': {
                    'approval_run_id': 'run-success',
                    'final_status': {
                        'result': {
                            'verified': True,
                            'crm_recorded': True,
                            'approved_count': 2,
                            'pending_after': 0,
                            'member_count_after': 6,
                            'result_code': 'approved',
                        }
                    },
                }
            },
        },
    }
    notification = {
        'severity': 'info',
        'code': 'formal_approval_succeeded',
        'summary': '注册群审批成功',
        'details': {'approved_count': 2, 'pending_after': 0, 'member_count_after': 6},
    }

    text = format_lark_alert('production-ops-daemon', notification, cycle)

    assert '[production-ops-daemon] INFO formal_approval_succeeded' in text
    assert '时间: 2026-04-29 13:57:32 UTC+8' in text
    assert '注册群: RG' in text
    assert '通过人数: 2' in text
    assert '原因: 已审批通过 2 人，当前待审批 0 人，群成员 6 人' in text


def test_format_lark_alert_uses_compact_startup_reason_without_raw_details():
    cycle = {
        'checked_at': '2026-04-28T10:00:00+00:00',
        'registration_group': 'RG',
        'startup_initial_batch': {
            'triggered': True,
            'ok': False,
            'pending_count': 12,
            'retries_exhausted': True,
        },
    }
    incident = {
        'severity': 'critical',
        'code': 'startup_initial_batch_failed',
        'summary': '启动首批审批失败',
        'details': {
            'pending_count': 12,
            'retries_exhausted': True,
            'blob': 'x' * 1000,
        },
    }

    text = format_lark_alert('production-ops-daemon', incident, cycle)

    assert '待审批人数: 12' in text
    assert '原因: 启动首批审批失败，自动重试已结束并转入常规监控' in text
    assert '详情:' not in text
    assert 'blob' not in text


def test_format_lark_alert_uses_compact_release_reason_without_raw_details():
    cycle = {
        'checked_at': '2026-04-28T10:00:00+00:00',
        'registration_group': 'RG',
        'release_evaluation': {
            'ok': False,
            'payload': {'release_count': 14},
        },
    }
    incident = {
        'severity': 'critical',
        'code': 'release_evaluation_failed',
        'summary': '批次放行评估失败',
        'details': {'error': '<urlopen error [Errno 61] Connection refused>', 'release_count': 14},
    }

    text = format_lark_alert('production-ops-daemon', incident, cycle)

    assert '待放行人数: 14' in text
    assert '原因: <urlopen error [Errno 61] Connection refused>' in text
    assert '详情:' not in text


def test_format_lark_alert_uses_compact_worker_recovery_failed_reason():
    cycle = {
        'checked_at': '2026-04-30T06:37:42+00:00',
        'registration_group': 'RG',
    }
    incident = {
        'severity': 'critical',
        'code': 'worker_state_failed',
        'summary': '群状态探测失败',
        'details': {
            'error': '<urlopen error [Errno 61] Connection refused>',
            'recovery': {
                'attempted': True,
                'mode': 'account_runtime_start',
                'reason': 'runtime_start_returned_empty_base_url',
            },
        },
    }

    text = format_lark_alert('production-ops-daemon', incident, cycle)

    assert '原因: 自动重连失败，已重试后仍不可用' in text
    assert '<urlopen error [Errno 61] Connection refused>' not in text
