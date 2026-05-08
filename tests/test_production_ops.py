from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.production_ops import (
    build_incidents,
    build_success_notifications,
    expand_notify_profile_targets,
    format_lark_alert,
    record_trigger,
    register_notification,
    requester_fingerprint,
    should_trigger_action,
)


def test_expand_notify_profile_targets_fanouts_broadcast_pair():
    assert expand_notify_profile_targets('wa-approval-broadcast', '审批bot01') == [
        {
            'profile_name': 'wa-approval-broadcast',
            'robot_name': '审批bot01',
        },
        {
            'profile_name': 'wa-approval-broadcast-02',
            'robot_name': '审批Bot02',
        },
    ]


def test_expand_notify_profile_targets_keeps_bot02_single_target():
    assert expand_notify_profile_targets('wa-approval-broadcast-02', '审批Bot02') == [
        {
            'profile_name': 'wa-approval-broadcast-02',
            'robot_name': '审批Bot02',
        }
    ]


def test_expand_notify_profile_targets_keeps_other_profiles_single_target():
    assert expand_notify_profile_targets('custom-profile', '自定义机器人') == [
        {
            'profile_name': 'custom-profile',
            'robot_name': '自定义机器人',
        }
    ]


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


def test_build_incidents_marks_startup_initial_batch_failed_when_approval_passed_but_crm_failed():
    cycle = {
        'checked_at': '2026-05-06T06:58:14+00:00',
        'registration_group': 'RG',
        'backend_health': {'ok': True},
        'worker_state': {'ok': True},
        'release_evaluation': {'ok': True},
        'startup_initial_batch': {
            'triggered': True,
            'ok': True,
            'session_id': 'session-58',
            'pending_count': 2,
            'attempt_results': [
                {
                    'returncode': 0,
                    'result': {
                        'formal_run': {
                            'approval_run_id': 'startup-run-58',
                            'final_status': {
                                'result': {
                                    'verified': True,
                                    'crm_recorded': False,
                                    'result_code': 'approved',
                                }
                            },
                        }
                    },
                }
            ],
        },
    }

    incidents = build_incidents(cycle)

    assert [item['code'] for item in incidents] == ['startup_initial_batch_failed']
    assert incidents[0]['details']['session_id'] == 'session-58'
    assert incidents[0]['details']['last_verified'] is True
    assert incidents[0]['details']['last_crm_recorded'] is False


def test_build_incidents_skips_worker_state_failed_after_retry_recovery():
    cycle = {
        'backend_health': {'ok': True},
        'worker_state': {
            'ok': True,
            'payload': {'group_name': 'RG', 'pending_count': 0},
            'recovered_after_retry': True,
            'retry_attempts': [
                {'attempt': 1, 'error': '<urlopen error [Errno 61] Connection refused>'},
            ],
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



def test_build_success_notifications_aggregates_drained_registration_group_runs_into_one_notice():
    cycle = {
        'checked_at': '2026-05-07T08:31:03+00:00',
        'registration_group': 'RG',
        'formal_approval': {
            'triggered': True,
            'ok': True,
            'fingerprint': 'fp-1',
            'release_count': 4,
            'aggregate_approved_count': 8,
            'final_pending_count': 0,
            'drain_rounds': 2,
            'approval_run_ids': [
                'registration_group_approval_a787e528a6d8',
                'registration_group_approval_af809ce6f284',
            ],
            'reason_code': 'timeout_flush',
            'result': {
                'formal_run': {
                    'approval_run_id': 'registration_group_approval_af809ce6f284',
                    'final_status': {
                        'result': {
                            'verified': True,
                            'crm_recorded': True,
                            'result_code': 'approved',
                            'approved_count': 4,
                            'pending_after': 0,
                            'member_count_after': 444,
                        }
                    },
                }
            },
        },
    }

    notifications = build_success_notifications(cycle)

    assert len(notifications) == 1
    assert notifications[0]['code'] == 'formal_approval_succeeded'
    assert notifications[0]['details']['approval_run_ids'] == [
        'registration_group_approval_a787e528a6d8',
        'registration_group_approval_af809ce6f284',
    ]
    assert notifications[0]['details']['approved_count'] == 8
    assert notifications[0]['details']['pending_after'] == 0
    assert notifications[0]['details']['drain_rounds'] == 2
    assert notifications[0]['dedupe_key'] == 'formal_approval_succeeded:registration_group_approval_a787e528a6d8|registration_group_approval_af809ce6f284'



def test_build_success_notifications_skips_registration_cycle_noop_when_zero_pending_is_unverified():
    cycle = {
        'registration_group_cycles': [
            {
                'registration_group': 'RG',
                'monitor_target': {'group_name': '注册测试1'},
                'release_evaluation': {
                    'ok': True,
                    'payload': {
                        'reason_code': 'waiting_next_cycle',
                        'pending_count': 0,
                        'cycle_started_at': '2026-05-08T01:30:00+00:00',
                        'cycle_ends_at': '2026-05-08T02:00:00+00:00',
                    },
                },
                'decision_group_state': {
                    'source': 'worker_state',
                    'zero_pending_unverified': True,
                },
                'fresh_probe': {
                    'ok': False,
                    'zero_pending_recheck': True,
                    'fallback_used': True,
                },
            }
        ]
    }

    notifications = build_success_notifications(cycle)

    assert notifications == []


def test_build_success_notifications_emits_registration_cycle_noop_notice_once_per_cycle():
    cycle = {
        'checked_at': '2026-05-07T03:30:10+00:00',
        'registration_group_cycles': [
            {
                'registration_group': 'RG',
                'monitor_target': {
                    'group_name': '注册测试1',
                    'registration_group': 'RG',
                    'notify_profile_name': 'wa-approval-broadcast',
                    'notify_robot_name': '审批bot01',
                },
                'release_evaluation': {
                    'ok': True,
                    'payload': {
                        'pending_count': 0,
                        'reason_code': 'waiting_next_cycle',
                        'cycle_started_at': '2026-05-07T03:30:00+00:00',
                        'cycle_ends_at': '2026-05-07T04:00:00+00:00',
                    },
                },
            },
        ],
    }

    notifications = build_success_notifications(cycle)

    assert len(notifications) == 1
    assert notifications[0]['code'] == 'registration_cycle_noop'
    assert notifications[0]['summary'] == '注册群本轮无审批'
    assert notifications[0]['details']['cycle_started_at'] == '2026-05-07T03:30:00+00:00'
    assert notifications[0]['notify_profile_name'] == 'wa-approval-broadcast'
    assert notifications[0]['notify_robot_name'] == '审批bot01'
    assert notifications[0]['dedupe_key'] == 'registration_cycle_noop:注册测试1|2026-05-07T03:30:00+00:00'



def test_build_success_notifications_emits_registration_group_success_from_non_primary_cycle():
    cycle = {
        'checked_at': '2026-05-08T03:30:10+00:00',
        'monitor_target': {
            'group_name': '主群A',
            'notify_profile_name': 'wa-approval-broadcast',
            'notify_robot_name': '审批bot01',
        },
        'registration_group_cycles': [
            {
                'registration_group': 'group-a',
                'monitor_target': {
                    'group_name': '主群A',
                    'notify_profile_name': 'wa-approval-broadcast',
                    'notify_robot_name': '审批bot01',
                },
                'formal_approval': {
                    'triggered': False,
                },
            },
            {
                'registration_group': 'group-b',
                'monitor_target': {
                    'group_name': '副群B',
                    'notify_profile_name': 'wa-approval-broadcast-02',
                    'notify_robot_name': '审批Bot02',
                },
                'formal_approval': {
                    'triggered': True,
                    'ok': True,
                    'fingerprint': 'u1@100',
                    'reason_code': 'timeout_flush',
                    'aggregate_approved_count': 2,
                    'final_pending_count': 0,
                    'approval_run_ids': ['registration_group_approval_groupb'],
                    'result': {
                        'formal_run': {
                            'approval_run_id': 'registration_group_approval_groupb',
                            'result': {
                                'verified': True,
                                'crm_recorded': True,
                                'approved_count': 2,
                                'pending_after': 0,
                                'member_count_after': 15,
                                'result_code': 'approved',
                            },
                        }
                    },
                },
            },
        ],
    }

    notifications = build_success_notifications(cycle)

    target = next(item for item in notifications if item['code'] == 'formal_approval_succeeded')
    assert target['notify_profile_name'] == 'wa-approval-broadcast-02'
    assert target['notify_robot_name'] == '审批Bot02'
    assert target['details']['group_name'] == '副群B'
    assert target['dedupe_key'] == 'formal_approval_succeeded:registration_group_approval_groupb'



def test_build_success_notifications_emits_startup_success_from_non_primary_cycle():
    cycle = {
        'checked_at': '2026-05-08T03:40:10+00:00',
        'monitor_target': {
            'group_name': '主群A',
            'notify_profile_name': 'wa-approval-broadcast',
            'notify_robot_name': '审批bot01',
        },
        'registration_group_cycles': [
            {
                'registration_group': 'group-a',
                'monitor_target': {
                    'group_name': '主群A',
                    'notify_profile_name': 'wa-approval-broadcast',
                    'notify_robot_name': '审批bot01',
                },
                'startup_initial_batch': {
                    'triggered': False,
                },
            },
            {
                'registration_group': 'group-b',
                'monitor_target': {
                    'group_name': '副群B',
                    'notify_profile_name': 'wa-approval-broadcast-02',
                    'notify_robot_name': '审批Bot02',
                },
                'startup_initial_batch': {
                    'triggered': True,
                    'ok': True,
                    'session_id': 'session-group-b',
                    'attempt_results': [
                        {
                            'result': {
                                'formal_run': {
                                    'approval_run_id': 'startup_groupb',
                                    'final_status': {
                                        'result': {
                                            'verified': True,
                                            'crm_recorded': True,
                                            'approved_count': 1,
                                            'pending_after': 0,
                                            'member_count_after': 9,
                                            'result_code': 'approved',
                                        }
                                    },
                                }
                            }
                        }
                    ],
                },
            },
        ],
    }

    notifications = build_success_notifications(cycle)

    target = next(item for item in notifications if item['code'] == 'startup_initial_batch_succeeded')
    assert target['notify_profile_name'] == 'wa-approval-broadcast-02'
    assert target['notify_robot_name'] == '审批Bot02'
    assert target['details']['group_name'] == '副群B'
    assert target['dedupe_key'] == 'startup_initial_batch_succeeded:startup_groupb'



def test_build_success_notifications_skips_registration_cycle_noop_immediately_after_anchor_reset():
    cycle = {
        'checked_at': '2026-05-08T01:53:43+00:00',
        'registration_group_cycles': [
            {
                'registration_group': 'RG',
                'monitor_target': {
                    'group_name': '注册测试1',
                    'registration_group': 'RG',
                },
                'release_evaluation': {
                    'ok': True,
                    'payload': {
                        'pending_count': 0,
                        'reason_code': 'waiting_next_cycle',
                        'cycle_anchor_at': '2026-05-08T01:53:02+00:00',
                        'completed_cycles_since_anchor': 0,
                        'cycle_started_at': '2026-05-08T01:53:02+00:00',
                        'cycle_ends_at': '2026-05-08T02:23:02+00:00',
                    },
                },
                'decision_group_state': {
                    'source': 'worker_state',
                    'mismatch': False,
                },
                'fresh_probe': {
                    'ok': True,
                },
            },
        ],
    }

    notifications = build_success_notifications(cycle)

    assert notifications == []



def test_build_success_notifications_emits_official_group_approval_success_with_notify_profile():
    cycle = {
        'checked_at': '2026-05-06T07:21:10+00:00',
        'registration_group': 'RG',
        'official_group_dispatch': {
            'triggered': True,
            'ok': True,
            'ready_groups': [
                {
                    'target_group': 'official-group-permata',
                    'group_name': '官方测试1',
                    'account_key': 'official-4456-8277',
                    'notify_profile_name': 'wa-approval-broadcast',
                    'notify_robot_name': '审批bot01',
                }
            ],
            'result': {
                'results': [
                    {
                        'lead_id': 'lead_eb073994165d',
                        'target_group': 'official-group-permata',
                        'executed': True,
                        'executor_result': {
                            'status': 'success',
                            'verified': True,
                            'approved_count': 1,
                            'raw_result': {
                                'approval_run_id': 'official_group_approval_583b5427467e',
                                'group_name': '官方测试1',
                                'pending_after': 1,
                                'member_count_after': 4,
                            },
                        },
                    },
                    {
                        'lead_id': 'lead_9f16e7c94d66',
                        'target_group': 'official-group-permata',
                        'executed': True,
                        'executor_result': {
                            'status': 'success',
                            'verified': True,
                            'approved_count': 1,
                            'raw_result': {
                                'approval_run_id': 'official_group_approval_f69307e4acf6',
                                'group_name': '官方测试1',
                                'pending_after': 0,
                                'member_count_after': 5,
                            },
                        },
                    }
                ]
            },
        },
    }

    notifications = build_success_notifications(cycle)

    assert len(notifications) == 1
    assert notifications[0]['code'] == 'official_group_approval_succeeded'
    assert notifications[0]['summary'] == '官方群审批成功'
    assert notifications[0]['notify_profile_name'] == 'wa-approval-broadcast'
    assert notifications[0]['notify_robot_name'] == '审批bot01'
    assert notifications[0]['details']['group_name'] == '官方测试1'
    assert notifications[0]['details']['approved_count'] == 2
    assert notifications[0]['details']['pending_after'] == 0
    assert notifications[0]['details']['member_count_after'] == 5
    assert notifications[0]['details']['approval_run_ids'] == [
        'official_group_approval_583b5427467e',
        'official_group_approval_f69307e4acf6',
    ]
    assert notifications[0]['dedupe_key'] == 'official_group_approval_succeeded:official_group_approval_583b5427467e|official_group_approval_f69307e4acf6'


def test_build_success_notifications_emits_official_group_manual_review_for_crm_gap():
    cycle = {
        'checked_at': '2026-05-06T07:21:10+00:00',
        'registration_group': 'RG',
        'official_group_dispatch': {
            'triggered': True,
            'ok': True,
            'ready_groups': [
                {
                    'target_group': 'official-group-permata',
                    'group_name': '官方测试1',
                    'account_key': 'official-4456-8277',
                    'notify_profile_name': 'wa-approval-broadcast',
                    'notify_robot_name': '审批bot01',
                }
            ],
            'result': {
                'results': [
                    {
                        'lead_id': 'lead_missing_crm',
                        'target_group': 'official-group-permata',
                        'executed': False,
                        'reason_code': 'crm_customer_not_found',
                        'reason_detail': 'No matching CRM customer was found for approval gating.',
                        'next_action': 'manual_review_official_group_approval',
                        'mobile': '+62812345678',
                    }
                ]
            },
        },
    }

    notifications = build_success_notifications(cycle)

    assert len(notifications) == 1
    assert notifications[0]['code'] == 'official_group_manual_review_required'
    assert notifications[0]['severity'] == 'warning'
    assert notifications[0]['summary'] == '官方群审批需人工复核'
    assert notifications[0]['notify_profile_name'] == 'wa-approval-broadcast'
    assert notifications[0]['notify_robot_name'] == '审批bot01'
    assert notifications[0]['details']['group_name'] == '官方测试1'
    assert notifications[0]['details']['lead_id'] == 'lead_missing_crm'
    assert notifications[0]['details']['mobile'] == '+62812345678'
    assert notifications[0]['details']['reason_code'] == 'crm_customer_not_found'
    assert notifications[0]['reason_text'] == 'CRM无记录，请人工复核'
    assert notifications[0]['dedupe_key'] == 'official_group_manual_review_required:official-group-permata:lead_missing_crm:crm_customer_not_found'



def test_build_success_notifications_falls_back_to_requester_phone_for_official_group_manual_review():
    cycle = {
        'checked_at': '2026-05-06T09:29:04+00:00',
        'registration_group': 'RG',
        'official_group_dispatch': {
            'triggered': True,
            'ok': True,
            'ready_groups': [
                {
                    'target_group': 'official-group-permata',
                    'group_name': '官方测试1',
                    'account_key': 'official-4456-8277',
                    'notify_profile_name': 'wa-approval-broadcast',
                    'notify_robot_name': '审批bot01',
                }
            ],
            'result': {
                'results': [
                    {
                        'target_group': 'official-group-permata',
                        'executed': False,
                        'reason_code': 'official_group_requester_unmatched',
                        'next_action': 'manual_review_official_group_approval',
                        'requester': {
                            'phoneNormalized': '+852****5475',
                            'phoneRaw': '+852****5475',
                        },
                    }
                ]
            },
        },
    }

    notifications = build_success_notifications(cycle)

    assert len(notifications) == 1
    assert notifications[0]['code'] == 'official_group_manual_review_required'
    assert notifications[0]['details']['mobile'] == '+852****5475'
    text = format_lark_alert('production-ops-daemon', notifications[0], cycle)
    assert '账号: +852****5475' in text



def test_format_lark_alert_contains_summary_and_reason():
    cycle = {
        'checked_at': '2026-04-28T10:00:00+00:00',
        'registration_group': '120363422719530134@g.us',
        'monitor_target': {'group_name': '注册测试1'},
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

    assert '🚨 生产守护告警｜正式审批失败' in text
    assert '时间: 2026-04-28 18:00:00 UTC+8' in text
    assert '注册群: 注册测试1' in text
    assert '批次人数: 8' in text
    assert '原因: 审批脚本执行失败' in text


def test_format_lark_alert_contains_official_group_manual_review_context():
    cycle = {
        'checked_at': '2026-05-06T07:21:10+00:00',
        'registration_group': '120363422719530134@g.us',
        'monitor_target': {'group_name': '注册测试1'},
    }
    notification = {
        'severity': 'warning',
        'code': 'official_group_manual_review_required',
        'summary': '官方群审批需人工复核',
        'details': {
            'group_name': '官方测试1',
            'lead_id': 'lead_missing_crm',
            'mobile': '+62812345678',
            'reason_code': 'crm_customer_not_found',
            'reason_detail': 'No matching CRM customer was found for approval gating.',
        },
        'reason_text': 'CRM无记录，请人工复核',
    }

    text = format_lark_alert('production-ops-daemon', notification, cycle)

    assert '⚠️🙋🏻‍♀️⚠️官方群审批需人工复核' in text
    assert '时间: 2026-05-06 15:21:10 UTC+8' in text
    assert '官方群: 官方测试1' in text
    assert '账号: +62812345678' in text
    assert '原因: CRM无记录，请人工复核' in text
    assert 'SID:' not in text
    assert '注册群:' not in text



def test_format_lark_alert_contains_compact_success_summary():
    cycle = {
        'checked_at': '2026-04-29T05:57:32+00:00',
        'registration_group': '120363422719530134@g.us',
        'monitor_target': {'group_name': '注册测试1'},
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

    assert '✅ 生产守护通知｜注册群审批成功' in text
    assert '时间: 2026-04-29 13:57:32 UTC+8' in text
    assert '注册群: 注册测试1' in text
    assert '审批类型: 常规轮次' in text
    assert '本次通过人数: 2' in text
    assert '剩余待审批人数: 0' in text
    assert '原因: 已审批通过 2 人' in text



def test_format_lark_alert_contains_precise_registration_manual_success_context():
    cycle = {
        'checked_at': '2026-05-07T05:05:30+00:00',
        'registration_group': '120363425215002840@g.us',
        'monitor_target': {'group_name': '注册测试1'},
    }
    notification = {
        'severity': 'info',
        'code': 'manual_approval_succeeded',
        'summary': '注册群审批成功',
        'details': {'approved_count': 2, 'pending_after': 0, 'member_count_after': 12},
    }

    text = format_lark_alert('production-ops-daemon', notification, cycle)

    assert '✅ 生产守护通知｜注册群审批成功' in text
    assert '注册群: 注册测试1' in text
    assert '审批类型: 人工审批' in text
    assert '本次通过人数: 2' in text
    assert '剩余待审批人数: 0' in text



def test_format_lark_alert_contains_empty_cycle_notice():
    cycle = {
        'checked_at': '2026-05-07T03:30:10+00:00',
        'monitor_target': {'group_name': '注册测试1'},
    }
    notification = {
        'severity': 'info',
        'code': 'registration_cycle_noop',
        'summary': '注册群本轮无审批',
        'details': {
            'cycle_started_at': '2026-05-07T03:30:00+00:00',
            'cycle_ends_at': '2026-05-07T04:00:00+00:00',
            'pending_count': 0,
        },
    }

    text = format_lark_alert('production-ops-daemon', notification, cycle)

    assert '✅ 生产守护通知｜注册群本轮无审批' in text
    assert '注册群: 注册测试1' in text
    assert '审批类型: 常规轮次' in text
    assert '原因: 审批时间已到，未发生实际审批' in text
def test_format_lark_alert_contains_registration_cycle_noop_context():
    cycle = {
        'checked_at': '2026-05-08T01:00:40+00:00',
        'registration_group': 'RG-primary',
        'monitor_target': {'group_name': '主群A'},
    }
    notification = {
        'severity': 'info',
        'code': 'registration_cycle_noop',
        'summary': '注册群本轮无审批',
        'details': {
            'group_name': '副群B',
            'cycle_started_at': '2026-05-08T01:00:00+00:00',
            'cycle_ends_at': '2026-05-08T01:30:00+00:00',
            'reason_code': 'waiting_next_cycle',
        },
    }

    text = format_lark_alert('production-ops-daemon', notification, cycle)

    assert '✅ 生产守护通知｜注册群本轮无审批' in text
    assert '注册群: 副群B' in text
    assert '注册群: 主群A' not in text
    assert '审批类型: 常规轮次' in text
    assert '原因: 审批时间已到，未发生实际审批' in text



def test_format_lark_alert_contains_startup_success_context():
    cycle = {
        'checked_at': '2026-04-30T02:48:58+00:00',
        'registration_group': '120363422719530134@g.us',
        'monitor_target': {'group_name': '🇮🇩3️⃣7️⃣Grup Registrasi Resmi Linky 💎'},
        'startup_initial_batch': {
            'triggered': True,
            'ok': True,
            'pending_count': 6,
            'attempt_results': [
                {
                    'result': {
                        'formal_run': {
                            'approval_run_id': 'startup-success-1',
                            'final_status': {
                                'result': {
                                    'verified': True,
                                    'crm_recorded': True,
                                    'approved_count': 6,
                                    'pending_after': 3,
                                    'member_count_after': 425,
                                }
                            }
                        }
                    }
                }
            ],
        },
    }
    notification = {
        'severity': 'info',
        'code': 'startup_initial_batch_succeeded',
        'summary': '启动首批审批成功',
        'details': {'approved_count': 6, 'pending_after': 3, 'member_count_after': 425},
    }

    text = format_lark_alert('production-ops-daemon', notification, cycle)

    assert '✅ 生产守护通知｜启动首批审批成功' in text
    assert '注册群: 🇮🇩3️⃣7️⃣Grup Registrasi Resmi Linky 💎' in text
    assert '审批类型: 启动首批' in text
    assert '本次通过人数: 6' in text
    assert '剩余待审批人数: 3' in text
    assert '原因: 启动首批审批已通过 6 人' in text



def test_format_lark_alert_contains_official_group_success_context():
    cycle = {
        'checked_at': '2026-05-06T07:21:10+00:00',
        'registration_group': '120363422719530134@g.us',
        'monitor_target': {'group_name': '注册测试1'},
    }
    notification = {
        'severity': 'info',
        'code': 'official_group_approval_succeeded',
        'summary': '官方群审批成功',
        'details': {
            'group_name': '官方测试1',
            'approved_count': 2,
        },
    }

    text = format_lark_alert('production-ops-daemon', notification, cycle)

    assert '✅ 生产守护通知｜官方群审批成功' in text
    assert '时间: 2026-05-06 15:21:10 UTC+8' in text
    assert '官方群: 官方测试1' in text
    assert '注册群:' not in text
    assert '通过人数: 2' in text
    assert '原因: 已审批通过 2 人' in text
    assert '启动首批审批已通过' not in text



def test_format_lark_alert_supports_hot_loaded_templates(tmp_path, monkeypatch):
    template_path = tmp_path / 'production_ops_alert_templates.json'
    template_path.write_text(
        '{"headers":{"info":{"icon":"🟢","label":"极简通知"}},"reasons":{"startup_initial_batch_succeeded":"首批已过 {approved_count} 人"}}',
        encoding='utf-8',
    )
    monkeypatch.setattr('app.production_ops.DEFAULT_ALERT_TEMPLATES_PATH', template_path)
    cycle = {
        'checked_at': '2026-05-06T07:21:10+00:00',
        'registration_group': '120363422719530134@g.us',
        'monitor_target': {'group_name': '注册测试1'},
    }
    incident = {
        'severity': 'info',
        'code': 'startup_initial_batch_succeeded',
        'summary': '启动首批审批成功',
        'details': {'approved_count': 3},
    }

    text = format_lark_alert('production-ops-daemon', incident, cycle)

    assert '🟢 极简通知｜启动首批审批成功' in text
    assert '注册群: 注册测试1' in text
    assert '原因: 首批已过 3 人' in text




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
