from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.production_ops import build_incidents, build_success_notifications, format_lark_alert
from scripts.production_ops_daemon import SUCCESS_NOTIFICATION_CODES, _build_formal_approval_command, _build_recovery_notifications, _build_worker_probe_recovery_notifications, _evaluate_release, _fetch_worker_group_state_with_passive_retry, _notification_delivery_summary, _notify_incidents, _run_registration_group_cycle, _session_state, _target_session_key, _worker_probe_timeout_seconds, run_cycle


class Args:
    api_base_url = 'http://127.0.0.1:8011'
    worker_base_url = 'http://127.0.0.1:8787'
    registration_group = 'RG'
    backend_restart_cmd = 'restart-backend'
    restart_command_timeout_seconds = 5.0
    restart_wait_seconds = 0.0
    health_timeout_seconds = 1.0
    worker_timeout_seconds = 1.0
    trigger_cooldown_seconds = 120
    area = 'Indonesia'
    remark = 'test'
    approved_count = 1
    approval_poll_interval_seconds = 0.1
    approval_poll_timeout_seconds = 60.0
    decided_by = 'Hermes'
    decided_by_name = 'Song Yuqi'
    fresh_probe_cmd = ''
    worker_restart_cmd = ''
    worker_event_log = ''
    command_timeout_seconds = 60.0
    auto_recover_worker = True
    worker_probe_recovery_threshold = 2
    worker_probe_failure_threshold = 10
    monitoring_session_id = ''


def test_fetch_worker_group_state_uses_fast_mode_by_default(monkeypatch):
    calls = []

    def fake_fetch_json(url, *, method, payload, timeout):
        calls.append({'url': url, 'method': method, 'payload': payload, 'timeout': timeout})
        return {'pending_count': 2, 'requester_ids': ['r1', 'r2']}

    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)

    result = _fetch_worker_group_state_with_passive_retry(
        'http://worker-1',
        'RG',
        timeout_seconds=10.0,
        passive_retry_wait_seconds=0.0,
        passive_retry_count=0,
    )

    assert result['ok'] is True
    assert calls == [{
        'url': 'http://worker-1/group-state',
        'method': 'POST',
        'payload': {'registration_group': 'RG', 'mode': 'fast'},
        'timeout': 10.0,
    }]


def test_fetch_worker_group_state_can_request_full_verify(monkeypatch):
    calls = []

    def fake_fetch_json(url, *, method, payload, timeout):
        calls.append(payload)
        return {'pending_count': 0, 'requester_ids': [], 'zero_pending_unverified': False}

    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)

    result = _fetch_worker_group_state_with_passive_retry(
        'http://worker-1',
        'RG',
        timeout_seconds=10.0,
        passive_retry_wait_seconds=0.0,
        passive_retry_count=0,
        probe_mode='full_verify',
    )

    assert result['ok'] is True
    assert calls == [{'registration_group': 'RG', 'mode': 'full_verify'}]


def test_run_production_ops_daemon_script_exposes_worker_timeout_knob():
    script = Path('scripts/run_production_ops_daemon.sh').read_text()

    assert '--worker-timeout-seconds "${PRODUCTION_OPS_WORKER_TIMEOUT_SECONDS:-90}"' in script


def test_worker_probe_timeout_adapts_from_group_duration_history():
    monitoring = {
        'worker_probe_duration_stats': {
            'samples': [18.0, 25.0, 48.0],
            'p90': 48.0,
            'p95': 48.0,
        }
    }

    timeout = _worker_probe_timeout_seconds(SimpleNamespace(**Args.__dict__), monitoring)

    assert timeout == 86.4


def test_group_cycle_records_probe_duration_and_marks_slow_without_incident(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.worker_timeout_seconds = 90.0
    state = {}
    target = {
        'registration_group': 'RG',
        'group_name': '注册群',
        'worker_base_url': 'http://worker-1',
        'account_key': 'registration-test',
        'source': 'account_binding',
        'auto_recover_worker': True,
        'runtime_state': {'active': True, 'ready': True, 'authenticated': True},
        'session_state': {'session_target_match': True, 'login_verified': True},
    }
    captured = []

    durations = [50.0, 80.0]

    def fake_fetch(*_args, **kwargs):
        captured.append(kwargs['timeout_seconds'])
        duration = durations.pop(0)
        return {
            'ok': True,
            'duration_seconds': duration,
            'payload': {'group_id': 'RG', 'group_name': '注册群', 'pending_count': 1, 'member_count': 10, 'requester_ids': ['u1']},
            'attempts': [],
            'retry_count': 0,
            'total_attempts': 1,
        }

    monkeypatch.setattr('scripts.production_ops_daemon._fetch_worker_group_state_with_passive_retry', fake_fetch)
    monkeypatch.setattr('scripts.production_ops_daemon._evaluate_release_with_backend_recovery', lambda *a, **k: {'ready': False, 'reason_code': 'waiting_next_cycle'})

    first = _run_registration_group_cycle(args, state, target, now=datetime(2026, 5, 14, 1, 0, tzinfo=timezone.utc))
    second = _run_registration_group_cycle(args, state, target, now=datetime(2026, 5, 14, 1, 1, tzinfo=timezone.utc))

    assert first['worker_state']['probe_duration_seconds'] == 50.0
    assert first['worker_state']['probe_status'] == 'fresh_confirmed'
    assert second['worker_state']['probe_status'] == 'probe_slow'
    assert second['worker_state']['status_text'] == '探测较慢'
    assert second['worker_state']['probe_timeout_seconds'] >= 90.0
    assert build_incidents({'backend_health': {'ok': True}, 'worker_state': second['worker_state']}) == []
    assert captured[0] == 90.0


def test_worker_probe_timeout_retries_then_restarts_and_escalates_after_ten_rounds(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.restart_wait_seconds = 0.0
    state = {}
    target = {
        'registration_group': 'RG',
        'group_name': '注册群',
        'worker_base_url': 'http://worker-1',
        'account_key': 'registration-test',
        'source': 'account_binding',
        'auto_recover_worker': True,
        'runtime_state': {'active': True, 'ready': True, 'authenticated': True},
        'session_state': {'session_target_match': True, 'login_verified': True},
    }
    recoveries = []

    monkeypatch.setattr('scripts.production_ops_daemon.time.sleep', lambda seconds: None)
    monkeypatch.setattr('scripts.production_ops_daemon._fetch_worker_group_state_with_passive_retry', lambda *a, **k: {
        'ok': False,
        'error': 'timed out',
        'attempts': [{'attempt': 1, 'error': 'timed out'}],
        'retry_count': 1,
        'total_attempts': 2,
    })

    def fake_recover(*args_, **kwargs_):
        recoveries.append(kwargs_.get('trigger_reason'))
        return {'attempted': True, 'status': 'failed', 'reason': 'still timed out'}

    monkeypatch.setattr('scripts.production_ops_daemon._recover_worker_for_target', fake_recover)

    first = _run_registration_group_cycle(args, state, target, now=datetime(2026, 5, 14, 1, 0, tzinfo=timezone.utc))
    assert first['worker_state']['ok'] is False
    assert first['worker_state']['probe_failure_gate']['streak_count'] == 1
    assert first['worker_state']['recovery']['attempted'] is False
    assert first['worker_state']['suppress_incident'] is True
    assert build_incidents({'backend_health': {'ok': True}, 'worker_state': first['worker_state']}) == []
    assert recoveries == []

    second = _run_registration_group_cycle(args, state, target, now=datetime(2026, 5, 14, 1, 1, tzinfo=timezone.utc))
    assert second['worker_state']['probe_failure_gate']['streak_count'] == 2
    assert second['worker_state']['recovery']['attempted'] is True
    assert second['worker_state']['recovery']['trigger_reason'] == 'worker_probe_timeout'
    assert second['worker_state']['suppress_incident'] is True
    assert build_incidents({'backend_health': {'ok': True}, 'worker_state': second['worker_state']}) == []
    assert recoveries == ['worker_probe_timeout']

    tenth = second
    for minute in range(2, 10):
        tenth = _run_registration_group_cycle(args, state, target, now=datetime(2026, 5, 14, 1, minute, tzinfo=timezone.utc))

    assert tenth['worker_state']['probe_failure_gate']['streak_count'] == 10
    assert tenth['worker_state']['failure_confirmed'] is True
    assert tenth['worker_state']['suppress_incident'] is False
    incidents = build_incidents({'backend_health': {'ok': True}, 'worker_state': tenth['worker_state']})
    assert [item['code'] for item in incidents] == ['worker_state_failed']


def test_worker_probe_recovery_clears_failure_gate_and_builds_recovery_notification(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.restart_wait_seconds = 0.0
    state = {
        'notifications': {
            'worker_state_failed': {
                'last_status': 'sent',
                'last_sent_at': '2026-05-14T01:10:00+00:00',
                'incident': {'details': {'error': 'timed out'}},
            }
        },
    }
    target = {
        'registration_group': 'RG',
        'group_name': '注册群',
        'worker_base_url': 'http://worker-1',
        'account_key': 'registration-test',
        'source': 'account_binding',
        'auto_recover_worker': True,
        'runtime_state': {'active': True, 'ready': True, 'authenticated': True},
        'session_state': {'session_target_match': True, 'login_verified': True},
    }
    monitoring = _session_state(
        state,
        session_id='',
        registration_group='RG',
        checked_at='2026-05-14T01:10:00+00:00',
        target=target,
    )
    monitoring['worker_probe_failure_gate'] = {'streak_count': 10, 'trigger_reason': 'worker_probe_timeout'}

    monkeypatch.setattr('scripts.production_ops_daemon.time.sleep', lambda seconds: None)
    monkeypatch.setattr('scripts.production_ops_daemon._fetch_worker_group_state_with_passive_retry', lambda *a, **k: {
        'ok': True,
        'payload': {'group_id': 'g', 'group_name': '注册群', 'pending_count': 0, 'member_count': 1, 'requesters': [], 'requester_ids': []},
        'attempts': [],
        'retry_count': 0,
        'total_attempts': 1,
    })
    monkeypatch.setattr('scripts.production_ops_daemon._evaluate_release_with_backend_recovery', lambda *a, **k: {
        'ok': True,
        'payload': {'ready': False, 'reason_code': 'waiting_for_batch', 'pending_count': 0},
    })

    cycle = _run_registration_group_cycle(args, state, target, now=datetime(2026, 5, 14, 1, 11, tzinfo=timezone.utc))

    assert cycle['worker_state']['ok'] is True
    assert cycle['worker_state']['probe_failure_recovered'] is True
    assert 'worker_probe_failure_gate' not in monitoring
    recovered = _build_worker_probe_recovery_notifications(state, cycle)
    assert len(recovered) == 1
    assert recovered[0]['code'] == 'worker_probe_recovered'
    assert recovered[0]['dedupe_key'] == 'worker_probe_recovered:RG'


def test_outside_schedule_target_does_not_increment_worker_probe_failure_gate():
    args = SimpleNamespace(**Args.__dict__)
    outside_target = {
        'registration_group': 'RG',
        'group_name': '注册群',
        'worker_base_url': 'http://worker-1',
        'source': 'account_binding',
        'schedule_runtime': {'configured': True, 'active_now': False},
        'schedule_windows': [{'start': '09:00', 'end': '12:00'}],
    }
    state = {}
    monitor_target = {
        'selected': outside_target,
        'candidates': [],
        'selection_reason': 'configured_binding_outside_schedule',
        'allow_fallback': False,
    }
    cycle = run_cycle(args, state) if False else None
    from scripts.production_ops_daemon import _ordered_cycle_targets
    ordered = _ordered_cycle_targets(monitor_target, {}, now=datetime(2026, 5, 14, 1, 0, tzinfo=timezone.utc), poll_interval_seconds=20)
    assert ordered == []
    assert 'monitoring_sessions' not in state


def test_formal_approval_command_uses_dynamic_poll_timeout_for_large_batch():
    args = Args()
    command = _build_formal_approval_command(args, approved_count=13)

    timeout_index = command.index('--poll-timeout-seconds') + 1

    assert float(command[timeout_index]) >= 164.0



def test_build_recovery_notifications_closes_prior_formal_failure_with_display_batch_id():
    state = {
        'notifications': {
            'formal_approval_failed:fp-1': {
                'last_sent_at': '2026-05-13T01:00:17+00:00',
                'last_status': 'sent',
                'incident': {
                    'details': {
                        'reason_text': '审批脚本执行失败',
                    },
                },
            }
        }
    }
    cycle = {
        'checked_at': '2026-05-13T01:01:32+00:00',
        'monitor_target': {'group_name': '🇮🇩2️⃣4️⃣Grup Registrasi Resmi  ✘ Linky 💎'},
    }
    success_notifications = [{
        'code': 'formal_approval_succeeded',
        'severity': 'info',
        'summary': '注册群审批成功',
        'details': {
            'fingerprint': 'fp-1',
            'group_name': '🇮🇩2️⃣4️⃣Grup Registrasi Resmi  ✘ Linky 💎',
            'approved_count': 13,
            'approval_batch_display_id': '2026051301',
            'approval_run_id': 'registration_group_approval_xxx',
        },
        'dedupe_key': 'formal_approval_succeeded:registration_group_approval_xxx',
    }]

    recovered = _build_recovery_notifications(state, cycle, success_notifications)

    assert len(recovered) == 1
    assert recovered[0]['code'] == 'formal_approval_recovered'
    assert recovered[0]['details']['approval_batch_display_id'] == '2026051301'
    text = format_lark_alert('mcn-production-ops', recovered[0], cycle)
    assert '✅ 生产守护恢复｜正式审批已闭环' in text
    assert '原告警: 2026-05-13 09:00:17 UTC+8' in text
    assert '批次人数: 13' in text
    assert '批次ID: 2026051301' in text
    assert 'registration_group_approval_xxx' not in text



def test_target_session_key_rotates_with_new_active_schedule_window():
    target = {
        'registration_group': 'RG',
        'schedule_runtime': {'configured': True, 'active_now': True},
        'schedule_windows': [{'start': '09:00', 'end': '18:00'}],
    }

    day1_key = _target_session_key('default', target, datetime(2026, 5, 7, 1, 5, tzinfo=timezone.utc))
    same_window_key = _target_session_key('default', target, datetime(2026, 5, 7, 5, 30, tzinfo=timezone.utc))
    day2_key = _target_session_key('default', target, datetime(2026, 5, 8, 1, 5, tzinfo=timezone.utc))

    assert day1_key == same_window_key
    assert day1_key != day2_key



def test_session_state_resets_startup_initial_batch_for_new_schedule_window():
    state = {}
    target = {
        'registration_group': 'RG',
        'group_name': '测试群',
        'schedule_runtime': {'configured': True, 'active_now': True},
        'schedule_windows': [{'start': '09:00', 'end': '18:00'}],
    }

    day1 = _session_state(
        state,
        session_id='default',
        registration_group='RG',
        checked_at='2026-05-07T01:05:00+00:00',
        target=target,
    )
    day1['startup_initial_batch_done'] = True
    day1['startup_initial_batch_attempts'] = 1

    same_window = _session_state(
        state,
        session_id='default',
        registration_group='RG',
        checked_at='2026-05-07T03:00:00+00:00',
        target=target,
    )
    next_day = _session_state(
        state,
        session_id='default',
        registration_group='RG',
        checked_at='2026-05-08T01:05:00+00:00',
        target=target,
    )

    assert same_window is day1
    assert same_window['startup_initial_batch_done'] is True
    assert next_day is not day1
    assert next_day['startup_initial_batch_done'] is False
    assert next_day['startup_initial_batch_attempts'] == 0
    assert next_day['schedule_window_token'] != day1['schedule_window_token']



def test_evaluate_release_uses_cycle_anchor_for_waiting_next_cycle(monkeypatch):
    monkeypatch.setattr('scripts.production_ops_daemon.utc_now_iso', lambda: '2026-05-07T03:20:00Z')
    payload = _evaluate_release(
        'http://127.0.0.1:8011',
        'RG',
        {'pending_count': 0, 'requesters': []},
        batch_size=20,
        timeout_minutes=30,
        cycle_anchor_at='2026-05-07T03:18:00+00:00',
    )
    assert payload['reason_code'] == 'waiting_next_cycle'
    assert payload['cycle_anchor_at'] == '2026-05-07T03:18:00+00:00'
    assert payload['completed_cycles_since_anchor'] == 0
    assert payload['cycle_started_at'] == '2026-05-07T03:18:00+00:00'
    assert payload['cycle_ends_at'] == '2026-05-07T03:48:00+00:00'
    assert payload['remaining_seconds'] == 28 * 60



def test_registration_cycle_resets_stale_anchor_when_pending_reappears_after_zero(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    target = {
        'registration_group': 'RG',
        'group_name': '测试群',
        'worker_base_url': 'http://worker-1',
        'source': 'fallback_config',
        'runtime_state': {'active': True, 'ready': True, 'authenticated': True},
        'session_state': {'session_target_match': True, 'login_verified': True},
    }
    state = {
        'registration_cycle_anchors': {
            'RG': '2026-05-09T08:20:00+00:00',
            '测试群': '2026-05-09T08:20:00+00:00',
        }
    }
    evaluate_payloads = []
    worker_payloads = iter([
        {
            'group_id': 'g',
            'group_name': '测试群',
            'pending_count': 0,
            'member_count': 5,
            'requesters': [],
            'requester_ids': [],
        },
        {
            'group_id': 'g',
            'group_name': '测试群',
            'pending_count': 0,
            'member_count': 5,
            'requesters': [],
            'requester_ids': [],
        },
        {
            'group_id': 'g',
            'group_name': '测试群',
            'pending_count': 1,
            'member_count': 4,
            'requesters': [{'requesterId': 'u1', 'requestedAtUnix': 1778315459}],
            'requester_ids': ['u1'],
        },
    ])

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url == 'http://worker-1/group-state':
            return next(worker_payloads)
        if url.endswith('/api/ops/approval-batches/evaluate'):
            evaluate_payloads.append(dict(payload or {}))
            return {
                'approval_type': 'registration_group',
                'registration_group': 'RG',
                'pending_count': int((payload or {}).get('pending_count') or 0),
                'oldest_pending_at': (payload or {}).get('oldest_pending_at'),
                'ready': False,
                'release_count': 0,
                'reason_code': 'waiting_for_batch',
                'batch_size': 30,
                'timeout_minutes': 5,
                'elapsed_minutes': 0,
                'remaining_minutes': 5,
                'remaining_seconds': 300,
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *_args, **_kwargs: {
        'group_id': 'g',
        'group_name': '测试群',
        'pending_count': 0,
        'member_count': 5,
        'requesters': [],
        'requester_ids': [],
    })

    first_cycle = _run_registration_group_cycle(
        args,
        state,
        target,
        now=datetime(2026, 5, 9, 8, 30, 41, tzinfo=timezone.utc),
    )
    second_cycle = _run_registration_group_cycle(
        args,
        state,
        target,
        now=datetime(2026, 5, 9, 8, 31, 6, tzinfo=timezone.utc),
    )

    assert len(evaluate_payloads) == 1
    assert evaluate_payloads[0]['cycle_anchor_at'] in (None, '2026-05-09T08:30:59+00:00')
    assert state['registration_cycle_anchors']['RG'] == '2026-05-09T08:30:59+00:00'
    assert state['registration_cycle_anchors']['测试群'] == '2026-05-09T08:30:59+00:00'
    assert second_cycle['release_evaluation']['payload']['reason_code'] == 'waiting_for_batch'



def test_notification_delivery_summary_keeps_target_and_status_fields():
    summary = _notification_delivery_summary([
        {
            'code': 'backend_unhealthy',
            'status': 'partial_sent',
            'notify_profile_name': 'wa-approval-broadcast',
            'notify_robot_name': '审批bot01',
            'deliveries': [
                {
                    'notify_profile_name': 'wa-approval-broadcast',
                    'notify_robot_name': '审批bot01',
                    'status': 'sent',
                    'response': {'code': 0},
                },
                {
                    'notify_profile_name': 'wa-approval-broadcast-02',
                    'notify_robot_name': '审批bot02',
                    'status': 'failed',
                    'error': 'timeout',
                },
            ],
        }
    ])

    assert summary == [
        {
            'code': 'backend_unhealthy',
            'status': 'partial_sent',
            'notify_profile_name': 'wa-approval-broadcast',
            'notify_robot_name': '审批bot01',
            'deliveries': [
                {
                    'notify_profile_name': 'wa-approval-broadcast',
                    'notify_robot_name': '审批bot01',
                    'status': 'sent',
                    'error': None,
                },
                {
                    'notify_profile_name': 'wa-approval-broadcast-02',
                    'notify_robot_name': '审批bot02',
                    'status': 'failed',
                    'error': 'timeout',
                },
            ],
        }
    ]



def test_duplicate_registration_group_request_notifies_customer_service_without_critical_incident():
    cycle = {
        'checked_at': '2026-05-14T10:00:00+00:00',
        'backend_health': {'ok': True},
        'worker_state': {'ok': True},
        'release_evaluation': {'ok': True},
        'registration_group': 'Carote-02',
        'monitor_target': {
            'group_name': 'Carote-02',
            'notify_profile_name': 'wa-approval-broadcast',
            'notify_robot_name': '审批bot01',
        },
        'formal_approval': {
            'triggered': True,
            'ok': False,
            'fingerprint': 'fp-duplicate',
            'release_count': 1,
            'returncode': 0,
            'reason_code': 'batch_size_reached',
            'result': {
                'formal_run': {
                    'approval_run_id': 'registration_group_approval_dup001',
                    'result': {
                        'registration_group': 'Carote-02',
                        'active_registration_group': 'Permata-31',
                        'executed': False,
                        'verified': False,
                        'crm_recorded': False,
                        'status': 'skipped',
                        'result_code': 'duplicate_registration_group_request',
                        'result_reason': 'phone already has an active registration group; skip approving another registration group',
                        'approved_count': 0,
                    },
                }
            },
        },
    }

    assert build_incidents(cycle) == []
    notifications = build_success_notifications(cycle)

    assert [item['code'] for item in notifications] == ['registration_duplicate_group_request_skipped']
    notification = notifications[0]
    assert notification['severity'] == 'warning'
    assert notification['notify_profile_name'] == 'wa-approval-broadcast'
    assert notification['notify_robot_name'] == '审批bot01'
    assert notification['details']['group_name'] == 'Carote-02'
    assert notification['details']['active_registration_group'] == 'Permata-31'
    text = format_lark_alert('mcn-production-ops', notification, cycle)
    assert '注册群重复申请已拦截' in text
    assert '注册群: Carote-02' in text
    assert '已归属注册群: Permata-31' in text
    assert '结果: 已跳过自动审批，不会放入第二个注册群' in text



def test_success_notification_codes_exact_match_guardrail():
    assert SUCCESS_NOTIFICATION_CODES == {
        'formal_approval_succeeded',
        'formal_approval_recovered',
        'worker_probe_recovered',
        'registration_cycle_noop',
        'registration_duplicate_group_request_skipped',
        'startup_initial_batch_succeeded',
        'official_group_approval_succeeded',
        'official_group_manual_review_required',
        'official_group_cycle_noop',
    }



def test_run_cycle_backend_recovery_does_not_raise_after_restart(monkeypatch):
    calls = {'n': 0}

    def fake_check_backend_health(api_base_url, *, timeout):
        calls['n'] += 1
        if calls['n'] <= 3:
            return {'ok': False, 'error': 'connection refused'}
        return {'ok': True, 'payload': {'status': 'ok'}}

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', fake_check_backend_health)
    monkeypatch.setattr('scripts.production_ops_daemon.maybe_restart', lambda command, timeout: {'attempted': True, 'ok': True, 'command': command})
    monkeypatch.setattr('scripts.production_ops_daemon.time.sleep', lambda seconds: None)
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', lambda *args, **kwargs: {'group_id': 'g', 'group_name': 'RG', 'pending_count': 0, 'member_count': 1, 'requesters': []})
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: {'group_id': 'g', 'group_name': 'RG', 'pending_count': 0, 'member_count': 1, 'requesters': []})

    cycle = run_cycle(Args(), {})

    assert cycle['backend_health']['ok'] is True
    assert cycle['backend_health']['recovered_after_restart'] is True
    assert cycle['backend_health']['restart']['attempted'] is True
    assert cycle['worker_state']['ok'] is True
    assert cycle['fresh_probe']['skipped'] is True
    assert cycle['fresh_probe']['reason'] == 'group_state_is_authoritative_source'


def test_run_cycle_backend_transient_refusal_recovers_without_restart(monkeypatch):
    calls = {'n': 0}
    restart_calls = []

    def fake_check_backend_health(api_base_url, *, timeout):
        calls['n'] += 1
        if calls['n'] == 1:
            return {'ok': False, 'error': 'connection refused'}
        return {'ok': True, 'payload': {'status': 'ok'}}

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', fake_check_backend_health)
    monkeypatch.setattr('scripts.production_ops_daemon.maybe_restart', lambda command, timeout: restart_calls.append((command, timeout)) or {'attempted': True, 'ok': True, 'command': command})
    monkeypatch.setattr('scripts.production_ops_daemon.time.sleep', lambda seconds: None)
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', lambda *args, **kwargs: {'group_id': 'g', 'group_name': 'RG', 'pending_count': 0, 'member_count': 1, 'requesters': []})
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: {'group_id': 'g', 'group_name': 'RG', 'pending_count': 0, 'member_count': 1, 'requesters': []})

    cycle = run_cycle(Args(), {})

    assert cycle['backend_health']['ok'] is True
    assert cycle['backend_health']['recovered_after_retry'] is True
    assert restart_calls == []
    assert cycle['worker_state']['ok'] is True
    assert cycle['fresh_probe']['skipped'] is True
    assert cycle['fresh_probe']['reason'] == 'group_state_is_authoritative_source'


def test_registration_group_cycle_transient_worker_refusal_recovers_without_runtime_restart(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.restart_wait_seconds = 0.0
    target = {
        'registration_group': 'https://chat.whatsapp.com/test',
        'group_name': '测试群',
        'worker_base_url': 'http://127.0.0.1:61150',
        'account_key': 'wa-admin-demo-1',
        'account_name': 'WA Admin',
        'binding_link': 'https://chat.whatsapp.com/test',
        'binding_group_name': '测试群',
        'notify_profile_name': 'wa-approval-broadcast',
        'notify_robot_name': '审批bot01',
        'area': 'Indonesia',
        'approval_count_threshold': 30,
        'approval_timeout_minutes': 30,
        'auto_recover_worker': True,
        'schedule_runtime': {'configured': True, 'active_now': True},
        'schedule_windows': [],
        'source': 'account_binding',
    }
    calls = []
    attempts = {'group_state': 0, 'runtime_start': 0}

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        calls.append((url, method, payload))
        if url == 'http://127.0.0.1:61150/group-state':
            attempts['group_state'] += 1
            if attempts['group_state'] < 3:
                raise RuntimeError('<urlopen error [Errno 61] Connection refused>')
            return {
                'group_id': 'g',
                'group_name': '测试群',
                'pending_count': 0,
                'member_count': 5,
                'requesters': [],
            }
        if url == 'http://127.0.0.1:8011/api/ops/whatsapp-approval-accounts/wa-admin-demo-1/runtime/internal/start':
            attempts['runtime_start'] += 1
            return {'runtime': {'active': True, 'base_url': 'http://127.0.0.1:62000'}}
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon.time.sleep', lambda seconds: None)

    cycle = _run_registration_group_cycle(args, {}, target, now=datetime(2026, 4, 30, 7, 0, tzinfo=timezone.utc))

    assert cycle['worker_state']['ok'] is True
    assert cycle['worker_state']['recovered_after_retry'] is True
    assert cycle['worker_state'].get('recovered_after_restart') is not True
    assert len(cycle['worker_state']['retry_attempts']) == 2
    assert attempts['runtime_start'] == 0
    assert [entry[0] for entry in calls] == [
        'http://127.0.0.1:61150/group-state',
        'http://127.0.0.1:61150/group-state',
        'http://127.0.0.1:61150/group-state',
        'http://127.0.0.1:61150/group-state',
    ]


def test_registration_group_cycle_recovered_runtime_waits_through_warmup_refusals(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.restart_wait_seconds = 0.0
    args.worker_probe_recovery_threshold = 1
    target = {
        'registration_group': 'https://chat.whatsapp.com/test',
        'group_name': '测试群',
        'worker_base_url': 'http://127.0.0.1:61150',
        'account_key': 'wa-admin-demo-1',
        'account_name': 'WA Admin',
        'binding_link': 'https://chat.whatsapp.com/test',
        'binding_group_name': '测试群',
        'notify_profile_name': 'wa-approval-broadcast',
        'notify_robot_name': '审批bot01',
        'area': 'Indonesia',
        'approval_count_threshold': 30,
        'approval_timeout_minutes': 30,
        'auto_recover_worker': True,
        'schedule_runtime': {'configured': True, 'active_now': True},
        'schedule_windows': [],
        'source': 'account_binding',
    }
    calls = []
    recovered_attempts = {'n': 0}

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        calls.append((url, method, payload))
        if url == 'http://127.0.0.1:61150/group-state':
            raise RuntimeError('<urlopen error [Errno 61] Connection refused>')
        if url == 'http://127.0.0.1:8011/api/ops/whatsapp-approval-accounts/wa-admin-demo-1/runtime/internal/start':
            return {'runtime': {'active': True, 'base_url': 'http://127.0.0.1:62000'}}
        if url == 'http://127.0.0.1:62000/group-state':
            recovered_attempts['n'] += 1
            if recovered_attempts['n'] < 3:
                raise RuntimeError('<urlopen error [Errno 61] Connection refused>')
            return {
                'group_id': 'g',
                'group_name': '测试群',
                'pending_count': 0,
                'member_count': 5,
                'requesters': [],
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon.time.sleep', lambda seconds: None)

    cycle = _run_registration_group_cycle(args, {}, target, now=datetime(2026, 4, 30, 7, 0, tzinfo=timezone.utc))

    assert cycle['worker_state']['ok'] is True
    assert cycle['worker_state']['recovered_after_restart'] is True
    assert cycle['worker_state']['recovery']['mode'] == 'account_runtime_start'
    assert cycle['worker_state']['recovery_probe']['retry_count'] == 2
    assert len(cycle['worker_state']['recovery_probe']['retry_attempts']) == 2
    assert cycle['monitor_target']['worker_base_url'] == 'http://127.0.0.1:62000'
    assert [entry[0] for entry in calls] == [
        'http://127.0.0.1:61150/group-state',
        'http://127.0.0.1:61150/group-state',
        'http://127.0.0.1:61150/group-state',
        'http://127.0.0.1:61150/group-state',
        'http://127.0.0.1:8011/api/ops/whatsapp-approval-accounts/wa-admin-demo-1/runtime/internal/start',
        'http://127.0.0.1:62000/group-state',
        'http://127.0.0.1:62000/group-state',
        'http://127.0.0.1:62000/group-state',
        'http://127.0.0.1:62000/group-state',
    ]



def test_registration_group_cycle_waiting_for_scan_skips_rebuild_and_reports_login_unready(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    target = {
        'registration_group': 'https://chat.whatsapp.com/new',
        'group_name': 'https://chat.whatsapp.com/new',
        'worker_base_url': '',
        'account_key': 'registration-639974974871',
        'account_name': 'WA Admin',
        'binding_link': 'https://chat.whatsapp.com/new',
        'binding_group_name': '',
        'notify_profile_name': 'wa-approval-broadcast-02',
        'notify_robot_name': '审批bot02',
        'area': 'Indonesia',
        'approval_count_threshold': 1000,
        'approval_timeout_minutes': 900,
        'auto_recover_worker': True,
        'schedule_runtime': {'configured': True, 'active_now': True},
        'schedule_windows': [{'start': '00:00', 'end': '24:00'}],
        'source': 'account_binding',
        'runtime_state': {'active': True, 'ready': False, 'authenticated': False, 'base_url': ''},
        'session_state': {
            'login_verified': False,
            'login_check_status': 'waiting_for_scan',
            'qr_available': True,
            'session_target_match': True,
        },
    }

    def fake_fetch_json(*args, **kwargs):
        raise AssertionError('waiting-for-scan should not restart runtime or probe group-state')

    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)

    cycle = _run_registration_group_cycle(args, {}, target, now=datetime(2026, 5, 13, 11, 14, 7, tzinfo=timezone.utc))

    assert cycle['worker_state']['ok'] is False
    assert cycle['worker_state']['error'] == 'whatsapp_account_waiting_for_scan'
    assert cycle['worker_state']['recovery']['attempted'] is False
    assert cycle['worker_state']['recovery']['reason'] == 'whatsapp_account_waiting_for_scan'
    assert cycle['worker_state']['recovery']['login_check_status'] == 'waiting_for_scan'
    assert cycle['decision_group_state']['mismatch_reasons'] == ['whatsapp_account_waiting_for_scan']



def test_registration_group_cycle_pending_runtime_initializing_skips_rebuild_and_reports_login_unready(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    target = {
        'registration_group': '120363417671114118@g.us',
        'group_name': '🇮🇩2️⃣2️⃣Grup Registrasi Resmi  ✘ Linky 💎',
        'worker_base_url': '',
        'account_key': 'registration-639974974871',
        'account_name': 'WA Admin',
        'binding_link': 'https://chat.whatsapp.com/IJ8Fhs0UqMCKtnbDFbIPZH',
        'binding_group_name': '🇮🇩2️⃣2️⃣Grup Registrasi Resmi  ✘ Linky 💎',
        'notify_profile_name': 'wa-approval-broadcast-02',
        'notify_robot_name': '审批bot02',
        'area': 'Indonesia',
        'approval_count_threshold': 9000,
        'approval_timeout_minutes': 9000,
        'auto_recover_worker': True,
        'schedule_runtime': {'configured': True, 'active_now': True},
        'schedule_windows': [{'start': '00:00', 'end': '24:00'}],
        'source': 'account_binding',
        'runtime_state': {
            'active': True,
            'ready': False,
            'authenticated': False,
            'base_url': 'http://127.0.0.1:33157',
            'started_at': '2026-05-14T06:50:52+00:00',
            'status': 'initializing',
        },
        'session_state': {
            'login_verified': False,
            'login_check_status': 'pending_runtime',
            'status': 'initializing',
            'qr_available': False,
            'session_target_match': True,
        },
    }

    def fake_fetch_json(*args, **kwargs):
        raise AssertionError('pending-runtime QR initialization should not restart runtime or probe group-state')

    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)

    cycle = _run_registration_group_cycle(args, {}, target, now=datetime(2026, 5, 14, 6, 53, 10, tzinfo=timezone.utc))

    assert cycle['worker_state']['ok'] is False
    assert cycle['worker_state']['error'] == 'whatsapp_qr_initializing'
    assert cycle['worker_state']['recovery']['attempted'] is False
    assert cycle['worker_state']['recovery']['reason'] == 'whatsapp_qr_initializing'
    assert cycle['worker_state']['recovery']['login_check_status'] == 'pending_runtime'
    assert cycle['decision_group_state']['mismatch_reasons'] == ['whatsapp_qr_initializing']



def test_registration_group_cycle_auth_failed_initializing_skips_rebuild_and_reports_login_unready(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    target = {
        'registration_group': '120363417671114118@g.us',
        'group_name': '🇮🇩2️⃣2️⃣Grup Registrasi Resmi  ✘ Linky 💎',
        'worker_base_url': '',
        'account_key': 'registration-639974974871',
        'account_name': 'WA Admin',
        'binding_link': 'https://chat.whatsapp.com/IJ8Fhs0UqMCKtnbDFbIPZH',
        'binding_group_name': '🇮🇩2️⃣2️⃣Grup Registrasi Resmi  ✘ Linky 💎',
        'notify_profile_name': 'wa-approval-broadcast-02',
        'notify_robot_name': '审批bot02',
        'area': 'Indonesia',
        'approval_count_threshold': 9000,
        'approval_timeout_minutes': 9000,
        'auto_recover_worker': True,
        'schedule_runtime': {'configured': True, 'active_now': True},
        'schedule_windows': [{'start': '00:00', 'end': '24:00'}],
        'source': 'account_binding',
        'runtime_state': {
            'active': True,
            'ready': False,
            'authenticated': False,
            'base_url': 'http://127.0.0.1:35355',
            'started_at': '2026-05-14T07:13:38+00:00',
            'status': 'initializing',
        },
        'session_state': {
            'login_verified': False,
            'login_check_status': 'auth_failed',
            'status': 'initializing',
            'qr_available': False,
            'session_target_match': True,
        },
    }

    def fake_fetch_json(*args, **kwargs):
        raise AssertionError('auth-failed initializing login state should not restart runtime or probe group-state')

    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)

    cycle = _run_registration_group_cycle(args, {}, target, now=datetime(2026, 5, 14, 7, 16, 10, tzinfo=timezone.utc))

    assert cycle['worker_state']['ok'] is False
    assert cycle['worker_state']['error'] == 'whatsapp_qr_initializing'
    assert cycle['worker_state']['recovery']['attempted'] is False
    assert cycle['worker_state']['recovery']['reason'] == 'whatsapp_qr_initializing'
    assert cycle['worker_state']['recovery']['login_check_status'] == 'auth_failed'
    assert cycle['decision_group_state']['mismatch_reasons'] == ['whatsapp_qr_initializing']



def test_registration_group_cycle_restarts_account_runtime_before_reporting_worker_state_failed(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.restart_wait_seconds = 0.0
    args.worker_probe_recovery_threshold = 1
    target = {
        'registration_group': 'https://chat.whatsapp.com/test',
        'group_name': '测试群',
        'worker_base_url': 'http://127.0.0.1:61150',
        'account_key': 'wa-admin-demo-1',
        'account_name': 'WA Admin',
        'binding_link': 'https://chat.whatsapp.com/test',
        'binding_group_name': '测试群',
        'notify_profile_name': 'wa-approval-broadcast',
        'notify_robot_name': '审批bot01',
        'area': 'Indonesia',
        'approval_count_threshold': 30,
        'approval_timeout_minutes': 30,
        'auto_recover_worker': True,
        'schedule_runtime': {'configured': True, 'active_now': True},
        'schedule_windows': [],
        'source': 'account_binding',
    }
    calls = []

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        calls.append((url, method, payload))
        if url == 'http://127.0.0.1:61150/group-state':
            raise RuntimeError('<urlopen error [Errno 61] Connection refused>')
        if url == 'http://127.0.0.1:8011/api/ops/whatsapp-approval-accounts/wa-admin-demo-1/runtime/internal/start':
            return {'runtime': {'active': True, 'base_url': 'http://127.0.0.1:62000'}}
        if url == 'http://127.0.0.1:62000/group-state':
            return {
                'group_id': 'g',
                'group_name': '测试群',
                'pending_count': 0,
                'member_count': 5,
                'requesters': [],
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon.time.sleep', lambda seconds: None)

    cycle = _run_registration_group_cycle(args, {}, target, now=datetime(2026, 4, 30, 7, 0, tzinfo=timezone.utc))

    assert cycle['worker_state']['ok'] is True
    assert cycle['worker_state']['recovered_after_restart'] is True
    assert cycle['worker_state']['recovery']['mode'] == 'account_runtime_start'
    assert cycle['worker_state']['recovery']['account_key'] == 'wa-admin-demo-1'
    assert cycle['worker_state']['recovery_probe']['retry_count'] == 0
    assert cycle['monitor_target']['worker_base_url'] == 'http://127.0.0.1:62000'
    assert [entry[0] for entry in calls] == [
        'http://127.0.0.1:61150/group-state',
        'http://127.0.0.1:61150/group-state',
        'http://127.0.0.1:61150/group-state',
        'http://127.0.0.1:61150/group-state',
        'http://127.0.0.1:8011/api/ops/whatsapp-approval-accounts/wa-admin-demo-1/runtime/internal/start',
        'http://127.0.0.1:62000/group-state',
        'http://127.0.0.1:62000/group-state',
    ]


def test_registration_group_cycle_does_not_restart_legacy_shared_worker_for_fallback_target(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.restart_wait_seconds = 0.0
    args.worker_base_url = 'http://127.0.0.1:61150'
    args.worker_restart_cmd = '/Users/chauncey/work/mcn-ai-automation/scripts/restart_registration_group_webjs_worker.sh'
    target = {
        'registration_group': 'https://chat.whatsapp.com/test',
        'group_name': '测试群',
        'worker_base_url': 'http://127.0.0.1:61150',
        'area': 'Indonesia',
        'approval_count_threshold': 0,
        'approval_timeout_minutes': 0,
        'auto_recover_worker': True,
        'schedule_runtime': {},
        'schedule_windows': [],
        'source': 'fallback_config',
    }
    restart_calls = []

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url == 'http://127.0.0.1:61150/group-state':
            raise RuntimeError('<urlopen error [Errno 61] Connection refused>')
        raise AssertionError(url)

    def fake_restart(command, *, timeout):
        restart_calls.append((command, timeout))
        return {'attempted': True, 'ok': True, 'command': command}

    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon.maybe_restart', fake_restart)
    monkeypatch.setattr('scripts.production_ops_daemon.time.sleep', lambda seconds: None)

    cycle = _run_registration_group_cycle(args, {}, target, now=datetime(2026, 4, 30, 7, 0, tzinfo=timezone.utc))

    assert cycle['worker_state']['ok'] is False
    assert cycle['worker_state']['recovery']['attempted'] is False
    assert cycle['worker_state']['recovery']['reason'] == 'non_binding_target_recovery_disabled'
    assert restart_calls == []


def test_run_cycle_runs_startup_initial_batch_once_for_new_monitoring_session(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-1'

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/group-state'):
            return {
                'group_id': 'g',
                'group_name': 'RG',
                'pending_count': 7,
                'member_count': 100,
                'requesters': [
                    {'requesterId': 'u1', 'requestedAtUnix': 100},
                    {'requesterId': 'u2', 'requestedAtUnix': 101},
                ],
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: {
        'group_id': 'g',
        'group_name': 'RG',
        'pending_count': 7,
        'member_count': 100,
        'requesters': [
            {'requesterId': 'u1', 'requestedAtUnix': 100},
            {'requesterId': 'u2', 'requestedAtUnix': 101},
        ],
    })

    captured = {}

    def fake_run_formal_approval_command(command, timeout):
        captured['command'] = command
        return {
            'returncode': 0,
            'result': {
                'formal_run': {
                    'approval_run_id': 'startup-run-1',
                    'result': {
                        'verified': True,
                        'crm_recorded': True,
                    },
                },
            },
        }

    monkeypatch.setattr('scripts.production_ops_daemon.run_formal_approval_command', fake_run_formal_approval_command)

    state = {}
    cycle = run_cycle(args, state)

    assert cycle['startup_initial_batch']['triggered'] is True
    assert cycle['startup_initial_batch']['pending_count'] == 7
    assert cycle['startup_initial_batch']['attempts'] == 1
    assert '--approved-count' in captured['command']
    assert captured['command'][captured['command'].index('--approved-count') + 1] == '7'
    assert state['monitoring_session']['startup_initial_batch_done'] is True
    assert state['monitoring_session']['startup_initial_batch_attempts'] == 1
    assert cycle.get('formal_approval', {}).get('triggered') is not True

    second_cycle = run_cycle(args, state)
    assert second_cycle['startup_initial_batch']['startup_initial_batch_done'] is True
    assert second_cycle['startup_initial_batch'].get('triggered') is not True
    assert second_cycle['startup_initial_batch']['last_initial_pending_count'] == 7
    assert second_cycle['startup_initial_batch']['last_final_pending_count'] == 7


def test_run_cycle_startup_initial_batch_rechecks_worker_state_and_uses_larger_pending_count(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-startup-recheck-1'
    args.worker_base_url = ''

    worker_states = iter([
        {
            'group_id': 'g',
            'group_name': 'RG',
            'pending_count': 6,
            'member_count': 100,
            'requesters': [{'requesterId': f'w{i}', 'requestedAtUnix': 100 + i} for i in range(6)],
        },
        {
            'group_id': 'g',
            'group_name': 'RG',
            'pending_count': 6,
            'member_count': 100,
            'requesters': [{'requesterId': f'w{i}', 'requestedAtUnix': 100 + i} for i in range(6)],
        },
        {
            'group_id': 'g',
            'group_name': 'RG',
            'pending_count': 9,
            'member_count': 100,
            'requesters': [{'requesterId': f'w{i}', 'requestedAtUnix': 100 + i} for i in range(9)],
        },
    ])

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/api/ops/whatsapp-approval-accounts/registration-runtime-directory'):
            return {
                'rows': [
                    {
                        'account_key': 'wa-admin-demo-1',
                        'account_name': 'WA Admin',
                        'responsible_type': 'registration_group',
                        'enabled': True,
                        'area': 'Indonesia',
                        'runtime_state': {
                            'base_url': 'http://worker-1',
                            'active': True,
                        },
                        'group_link_bindings': [
                            {
                                'link': 'RG',
                                'group_name': 'RG',
                                'enabled': True,
                                'area': 'Indonesia',
                                'approval_count_threshold': 30,
                                'approval_timeout_minutes': 30,
                                'schedule_runtime': {'configured': True, 'active_now': True},
                            }
                        ],
                    }
                ]
            }
        if url == 'http://worker-1/group-state':
            return next(worker_states)
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon.time.sleep', lambda *_args, **_kwargs: None)

    captured = {}

    def fake_run_formal_approval_command(command, timeout):
        captured['command'] = command
        return {
            'returncode': 0,
            'result': {
                'formal_run': {
                    'approval_run_id': 'startup-recheck-1',
                    'result': {
                        'verified': True,
                        'crm_recorded': True,
                    },
                },
            },
        }

    monkeypatch.setattr('scripts.production_ops_daemon.run_formal_approval_command', fake_run_formal_approval_command)

    cycle = run_cycle(args, {})

    assert cycle['monitor_target']['source'] == 'account_binding'
    assert cycle['worker_state']['ok'] is True
    assert cycle['decision_group_state']['source'] == 'group_state'
    assert cycle['startup_initial_batch']['initial_pending_count'] == 6
    assert cycle['startup_initial_batch']['final_pending_count'] == 9
    assert cycle['startup_initial_batch']['pending_count'] == 9
    assert [item['pending_count'] for item in cycle['startup_initial_batch']['startup_probe_rechecks']] == [6, 9]
    assert '--approved-count' in captured['command']
    assert captured['command'][captured['command'].index('--approved-count') + 1] == '9'


def test_run_cycle_startup_initial_batch_uses_larger_pending_count_when_worker_and_fresh_probe_mismatch(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-mismatch-1'

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', lambda *args, **kwargs: {
        'group_id': 'g',
        'group_name': 'RG',
        'pending_count': 9,
        'member_count': 100,
        'requesters': [{'requesterId': f'w{i}', 'requestedAtUnix': 100 + i} for i in range(9)],
    })
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: {
        'group_id': 'g',
        'group_name': 'RG',
        'pending_count': 6,
        'member_count': 97,
        'requesters': [{'requesterId': f'f{i}', 'requestedAtUnix': 200 + i} for i in range(6)],
    })

    captured = {}

    def fake_run_formal_approval_command(command, timeout):
        captured['command'] = command
        return {
            'returncode': 0,
            'result': {
                'formal_run': {
                    'approval_run_id': 'startup-mismatch-1',
                    'result': {
                        'verified': True,
                        'crm_recorded': True,
                    },
                },
            },
        }

    monkeypatch.setattr('scripts.production_ops_daemon.run_formal_approval_command', fake_run_formal_approval_command)

    cycle = run_cycle(args, {})

    assert cycle['decision_group_state']['source'] == 'group_state'
    assert cycle['decision_group_state']['mismatch'] is False
    assert cycle['fresh_probe']['skipped'] is True
    assert cycle['fresh_probe']['reason'] == 'group_state_is_authoritative_source'
    assert cycle['startup_initial_batch']['pending_count'] == 9
    assert '--approved-count' in captured['command']
    assert captured['command'][captured['command'].index('--approved-count') + 1] == '9'



def test_run_cycle_account_binding_zero_pending_rechecks_with_authoritative_group_state(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-zero-recheck-1'
    invite_link = 'RG'

    group_state_calls = {'n': 0}

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/api/ops/whatsapp-approval-accounts/registration-runtime-directory'):
            return {
                'rows': [
                    {
                        'account_key': 'wa-admin-demo-1',
                        'account_name': 'WA Admin',
                        'responsible_type': 'registration_group',
                        'enabled': True,
                        'area': 'Indonesia',
                        'runtime_state': {
                            'active': True,
                            'base_url': 'http://worker-1',
                        },
                        'group_link_bindings': [
                            {
                                'link': invite_link,
                                'group_name': 'RG',
                                'enabled': True,
                                'area': 'Indonesia',
                                'schedule_runtime': {'configured': True, 'active_now': True},
                            }
                        ],
                    }
                ]
            }
        if url == 'http://worker-1/group-state':
            group_state_calls['n'] += 1
            if group_state_calls['n'] == 1:
                return {
                    'group_id': 'g',
                    'group_name': 'RG',
                    'pending_count': 0,
                    'member_count': 100,
                    'requesters': [],
                }
            return {
                'group_id': 'g',
                'group_name': 'RG',
                'pending_count': 3,
                'member_count': 103,
                'requesters': [{'requesterId': f'f{i}', 'requestedAtUnix': 300 + i} for i in range(3)],
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('fresh probe should not run')))
    monkeypatch.setattr('scripts.production_ops_daemon.time.sleep', lambda *_args, **_kwargs: None)
    monkeypatch.setattr('scripts.production_ops_daemon.run_formal_approval_command', lambda *args, **kwargs: {'returncode': 0, 'result': {'formal_run': {'approval_run_id': 'approval-1', 'result': {'verified': True, 'crm_recorded': True}}}})

    cycle = run_cycle(args, {})

    cycle_row = cycle['registration_group_cycles'][0]
    assert group_state_calls['n'] >= 2
    assert cycle_row['fresh_probe']['skipped'] is True
    assert cycle_row['fresh_probe']['zero_pending_recheck'] is True
    assert cycle_row['fresh_probe']['recheck_source'] == 'group_state'
    assert cycle_row['decision_group_state']['payload']['pending_count'] == 3



def test_run_cycle_account_binding_ignores_suspected_review_surface_positive_residue(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-zero-recheck-residue'
    invite_link = 'RG'

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/api/ops/whatsapp-approval-accounts/registration-runtime-directory'):
            return {
                'rows': [
                    {
                        'account_key': 'wa-admin-demo-1',
                        'account_name': 'WA Admin',
                        'responsible_type': 'registration_group',
                        'enabled': True,
                        'area': 'Indonesia',
                        'runtime_state': {
                            'active': True,
                            'base_url': 'http://worker-1',
                        },
                        'group_link_bindings': [
                            {
                                'link': invite_link,
                                'group_name': 'RG',
                                'enabled': True,
                                'area': 'Indonesia',
                                'schedule_runtime': {'configured': True, 'active_now': True},
                            }
                        ],
                    }
                ]
            }
        if url == 'http://worker-1/group-state':
            return {
                'group_id': 'g',
                'group_name': 'RG',
                'pending_count': 0,
                'member_count': 100,
                'requesters': [],
                'requester_ids': [],
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon.time.sleep', lambda *_args, **_kwargs: None)

    cycle = run_cycle(args, {})
    cycle_row = cycle['registration_group_cycles'][0]

    assert cycle_row['fresh_probe']['skipped'] is True
    assert cycle_row['review_surface_probe']['skipped'] is True
    assert cycle_row['decision_group_state']['source'] == 'group_state'
    assert cycle_row['decision_group_state']['payload']['pending_count'] == 0


def test_run_cycle_account_binding_false_zero_restarts_runtime_and_uses_recovered_pending(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-false-zero-recover'
    args.worker_recovery_rebuild_threshold = 1
    args.worker_recovery_rebuild_cooldown_seconds = 0
    args.false_zero_recovery_threshold = 1
    args.false_zero_recovery_cooldown_seconds = 0
    invite_link = 'RG'
    calls = {'worker_zero': 0, 'stop': 0, 'start': 0, 'session': 0, 'recovered': 0, 'formal': 0}

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/api/ops/whatsapp-approval-accounts/registration-runtime-directory'):
            return {
                'rows': [
                    {
                        'account_key': 'wa-admin-demo-1',
                        'account_name': 'WA Admin',
                        'responsible_type': 'registration_group',
                        'enabled': True,
                        'area': 'Indonesia',
                        'runtime_state': {
                            'active': True,
                            'ready': True,
                            'authenticated': True,
                            'base_url': 'http://worker-1',
                        },
                        'session_state': {
                            'login_verified': True,
                            'session_target_match': True,
                        },
                        'group_link_bindings': [
                            {
                                'link': invite_link,
                                'group_name': 'RG',
                                'enabled': True,
                                'area': 'Indonesia',
                                'auto_recover_worker': True,
                                'schedule_runtime': {'configured': True, 'active_now': True},
                            }
                        ],
                    }
                ]
            }
        if url == 'http://worker-1/group-state':
            calls['worker_zero'] += 1
            return {
                'group_id': 'g',
                'group_name': 'RG',
                'pending_count': 0,
                'member_count': 100,
                'requesters': [],
                'requester_ids': [],
                'zero_pending_unverified': True,
                'zero_pending_unverified_reason': 'same_runtime_family_zero_pending',
            }
        if url == 'http://worker-1/review-surface-state':
            return {
                'group_id': 'g',
                'group_name': 'RG',
                'pending_count': 0,
                'review_surface_ready': False,
                'empty_queue_visible': False,
                'requesters': [],
                'requester_ids': [],
            }
        if url.endswith('/runtime/internal/stop'):
            calls['stop'] += 1
            return {'ok': True}
        if url.endswith('/runtime/internal/start'):
            calls['start'] += 1
            return {'ok': True, 'runtime': {'base_url': 'http://worker-2'}}
        if url.endswith('/session/internal/start'):
            calls['session'] += 1
            return {
                'ok': True,
                'runtime': {'base_url': 'http://worker-2'},
                'session': {'login_verified': True, 'session_target_match': True},
            }
        if url == 'http://worker-2/group-state':
            calls['recovered'] += 1
            return {
                'group_id': 'g',
                'group_name': 'RG',
                'pending_count': 4,
                'member_count': 104,
                'requesters': [{'requesterId': f'r{i}', 'requestedAtUnix': 100 + i} for i in range(4)],
                'requester_ids': [f'r{i}' for i in range(4)],
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon.time.sleep', lambda *_args, **_kwargs: None)

    def fake_run_formal_approval_command(command, timeout):
        calls['formal'] += 1
        return {'returncode': 0, 'result': {'formal_run': {'approval_run_id': 'approval-false-zero-1', 'result': {'verified': True, 'crm_recorded': True, 'pending_after': 0}}}}

    monkeypatch.setattr('scripts.production_ops_daemon.run_formal_approval_command', fake_run_formal_approval_command)

    cycle = run_cycle(args, {})
    cycle_row = cycle['registration_group_cycles'][0]

    assert calls['worker_zero'] >= 2
    assert calls['stop'] == 1
    assert calls['start'] == 1
    assert calls['session'] == 1
    assert calls['recovered'] >= 1
    assert cycle_row['worker_state']['recovered_after_restart'] is True
    assert cycle_row['worker_state']['recovery']['mode'] == 'account_runtime_rebuild'
    assert cycle_row['worker_state']['recovery']['trigger_reason'] == 'healthy_false_zero_stale_session'
    assert cycle_row['decision_group_state']['payload']['pending_count'] == 4
    assert cycle_row['truth_state']['status'] == 'confirmed_pending'
    assert calls['formal'] == 1



def test_run_cycle_account_binding_false_zero_waits_for_ten_consecutive_signals(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-false-zero-threshold-10'
    invite_link = 'RG'
    state = {}
    calls = {'stop': 0, 'start': 0, 'session': 0, 'recovered': 0}

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/api/ops/whatsapp-approval-accounts/registration-runtime-directory'):
            return {
                'rows': [
                    {
                        'account_key': 'wa-admin-demo-1',
                        'account_name': 'WA Admin',
                        'responsible_type': 'registration_group',
                        'enabled': True,
                        'area': 'Indonesia',
                        'runtime_state': {
                            'active': True,
                            'ready': True,
                            'authenticated': True,
                            'base_url': 'http://worker-1',
                        },
                        'session_state': {
                            'login_verified': True,
                            'session_target_match': True,
                        },
                        'group_link_bindings': [
                            {
                                'link': invite_link,
                                'group_name': 'RG',
                                'enabled': True,
                                'area': 'Indonesia',
                                'auto_recover_worker': True,
                                'schedule_runtime': {'configured': True, 'active_now': True},
                            }
                        ],
                    }
                ]
            }
        if url == 'http://worker-1/group-state':
            return {
                'group_id': 'g',
                'group_name': 'RG',
                'pending_count': 0,
                'member_count': 100,
                'requesters': [],
                'requester_ids': [],
                'zero_pending_unverified': True,
                'zero_pending_unverified_reason': 'same_runtime_family_zero_pending',
            }
        if url == 'http://worker-1/review-surface-state':
            return {
                'group_id': 'g',
                'group_name': 'RG',
                'pending_count': 0,
                'review_surface_ready': False,
                'empty_queue_visible': False,
                'requesters': [],
                'requester_ids': [],
            }
        if url.endswith('/runtime/internal/stop'):
            calls['stop'] += 1
            return {'ok': True}
        if url.endswith('/runtime/internal/start'):
            calls['start'] += 1
            return {'ok': True, 'runtime': {'base_url': 'http://worker-2'}}
        if url.endswith('/session/internal/start'):
            calls['session'] += 1
            return {
                'ok': True,
                'runtime': {'base_url': 'http://worker-2'},
                'session': {'login_verified': True, 'session_target_match': True},
            }
        if url == 'http://worker-2/group-state':
            calls['recovered'] += 1
            return {
                'group_id': 'g',
                'group_name': 'RG',
                'pending_count': 0,
                'member_count': 100,
                'requesters': [],
                'requester_ids': [],
                'zero_pending_unverified': True,
                'zero_pending_unverified_reason': 'same_runtime_family_zero_pending',
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon.time.sleep', lambda *_args, **_kwargs: None)

    for _ in range(9):
        cycle_before_rebuild = run_cycle(args, state)
    assert calls['stop'] == 0
    assert calls['start'] == 0
    assert calls['session'] == 0
    assert cycle_before_rebuild['registration_group_cycles'][0]['worker_state']['false_zero_recovery']['gate']['streak_count'] == 9

    tenth_cycle = run_cycle(args, state)
    cycle_row = tenth_cycle['registration_group_cycles'][0]

    assert calls['stop'] == 1
    assert calls['start'] == 1
    assert calls['session'] == 1
    assert cycle_row['worker_state']['recovery']['trigger_reason'] == 'healthy_false_zero_stale_session'
    assert cycle_row['worker_state']['recovery']['mode'] == 'account_runtime_rebuild'


def test_run_cycle_account_binding_false_zero_rebuild_keeps_recovered_unverified_zero_fail_closed(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-false-zero-recovered-still-zero'
    args.worker_recovery_rebuild_threshold = 1
    args.worker_recovery_rebuild_cooldown_seconds = 0
    args.false_zero_recovery_threshold = 1
    args.false_zero_recovery_cooldown_seconds = 0
    invite_link = 'RG'
    calls = {'stop': 0, 'start': 0, 'session': 0, 'recovered': 0, 'formal': 0}

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/api/ops/whatsapp-approval-accounts/registration-runtime-directory'):
            return {
                'rows': [
                    {
                        'account_key': 'wa-admin-demo-1',
                        'account_name': 'WA Admin',
                        'responsible_type': 'registration_group',
                        'enabled': True,
                        'area': 'Indonesia',
                        'runtime_state': {
                            'active': True,
                            'ready': True,
                            'authenticated': True,
                            'base_url': 'http://worker-1',
                        },
                        'session_state': {
                            'login_verified': True,
                            'session_target_match': True,
                        },
                        'group_link_bindings': [
                            {
                                'link': invite_link,
                                'group_name': 'RG',
                                'enabled': True,
                                'area': 'Indonesia',
                                'auto_recover_worker': True,
                                'schedule_runtime': {'configured': True, 'active_now': True},
                            }
                        ],
                    }
                ]
            }
        if url == 'http://worker-1/group-state':
            return {
                'group_id': 'g',
                'group_name': 'RG',
                'pending_count': 0,
                'member_count': 100,
                'requesters': [],
                'requester_ids': [],
                'zero_pending_unverified': True,
                'zero_pending_unverified_reason': 'same_runtime_family_zero_pending',
            }
        if url == 'http://worker-1/review-surface-state':
            return {
                'group_id': 'g',
                'group_name': 'RG',
                'pending_count': 0,
                'review_surface_ready': False,
                'empty_queue_visible': False,
                'requesters': [],
                'requester_ids': [],
            }
        if url.endswith('/runtime/internal/stop'):
            calls['stop'] += 1
            return {'ok': True}
        if url.endswith('/runtime/internal/start'):
            calls['start'] += 1
            return {'ok': True, 'runtime': {'base_url': 'http://worker-2'}}
        if url.endswith('/session/internal/start'):
            calls['session'] += 1
            return {
                'ok': True,
                'runtime': {'base_url': 'http://worker-2'},
                'session': {'login_verified': True, 'session_target_match': True},
            }
        if url == 'http://worker-2/group-state':
            calls['recovered'] += 1
            return {
                'group_id': 'g',
                'group_name': 'RG',
                'pending_count': 0,
                'member_count': 100,
                'requesters': [],
                'requester_ids': [],
                'zero_pending_unverified': True,
                'zero_pending_unverified_reason': 'same_runtime_family_zero_pending',
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon.time.sleep', lambda *_args, **_kwargs: None)

    def fake_run_formal_approval_command(command, timeout):
        calls['formal'] += 1
        return {'returncode': 0, 'result': {}}

    monkeypatch.setattr('scripts.production_ops_daemon.run_formal_approval_command', fake_run_formal_approval_command)

    cycle = run_cycle(args, {})
    cycle_row = cycle['registration_group_cycles'][0]

    assert calls['stop'] == 1
    assert calls['start'] == 1
    assert calls['session'] == 1
    assert calls['recovered'] >= 1
    assert calls['formal'] == 0
    assert cycle_row['worker_state']['recovery']['status'] == 'ok'
    assert cycle_row['worker_state']['false_zero_recovery']['recovered_pending_count'] == 0
    assert cycle_row['decision_group_state']['zero_pending_unverified'] is True
    assert cycle_row['truth_state']['status'] == 'empty_unverified'
    assert build_success_notifications(cycle) == []


def test_run_cycle_account_binding_zero_pending_fresh_probe_failure_marks_unverified(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-zero-recheck-2'
    invite_link = 'RG'

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/api/ops/whatsapp-approval-accounts/registration-runtime-directory'):
            return {
                'rows': [
                    {
                        'account_key': 'wa-admin-demo-1',
                        'account_name': 'WA Admin',
                        'responsible_type': 'registration_group',
                        'enabled': True,
                        'area': 'Indonesia',
                        'runtime_state': {
                            'active': True,
                            'base_url': 'http://worker-1',
                        },
                        'group_link_bindings': [
                            {
                                'link': invite_link,
                                'group_name': 'RG',
                                'enabled': True,
                                'area': 'Indonesia',
                                'schedule_runtime': {'configured': True, 'active_now': True},
                            }
                        ],
                    }
                ]
            }
        if url == 'http://worker-1/group-state':
            return {
                'group_id': 'g',
                'group_name': 'RG',
                'pending_count': 0,
                'member_count': 100,
                'requesters': [],
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon.time.sleep', lambda *_args, **_kwargs: None)

    cycle = run_cycle(args, {})
    cycle_row = cycle['registration_group_cycles'][0]
    notifications = build_success_notifications(cycle)

    assert cycle_row['fresh_probe']['skipped'] is True
    assert cycle_row['fresh_probe']['reason'] == 'group_state_is_authoritative_source'
    assert cycle_row['fresh_probe']['zero_pending_recheck'] is True
    assert cycle_row['fresh_probe']['recheck_source'] == 'group_state'
    assert cycle_row['decision_group_state']['source'] == 'group_state'
    assert cycle_row['decision_group_state']['pending_zero_confidence'] == 'unverified'
    assert cycle_row['decision_group_state']['zero_pending_unverified'] is True
    assert cycle_row['truth_state']['status'] == 'empty_unverified'
    assert notifications == []


def test_run_cycle_fallback_target_without_explicit_independent_truth_probe_does_not_auto_build_default(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-no-default-independent-probe'

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/api/ops/whatsapp-approval-accounts/registration-runtime-directory'):
            return {'rows': []}
        if url == 'http://127.0.0.1:8787/group-state':
            return {
                'group_id': 'g',
                'group_name': 'RG',
                'pending_count': 0,
                'member_count': 100,
                'requesters': [],
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon.time.sleep', lambda *_args, **_kwargs: None)

    cycle = run_cycle(args, {})
    cycle_row = cycle['registration_group_cycles'][0]

    assert cycle_row['monitor_target']['source'] == 'fallback_config'
    assert cycle_row['monitor_target']['independent_truth_probe_cmd'] == ''
    assert cycle_row['independent_truth_probe']['skipped'] is True
    assert cycle_row['independent_truth_probe']['reason'] == 'async_reconcile_only_not_authoritative'
    assert cycle_row['decision_group_state']['pending_zero_confidence'] == 'unverified'


def test_run_cycle_startup_initial_batch_rechecks_and_stops_when_queue_clears(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-2'

    group_states = iter([
        {
            'group_id': 'g',
            'group_name': 'RG',
            'pending_count': 2,
            'member_count': 100,
            'requesters': [{'requesterId': 'u1', 'requestedAtUnix': 100}, {'requesterId': 'u2', 'requestedAtUnix': 101}],
        },
        {
            'group_id': 'g',
            'group_name': 'RG',
            'pending_count': 0,
            'member_count': 102,
            'requesters': [],
        },
    ])

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/group-state'):
            return next(group_states)
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    fresh_states = iter([
        {
            'group_id': 'g',
            'group_name': 'RG',
            'pending_count': 2,
            'member_count': 100,
            'requesters': [{'requesterId': 'u1', 'requestedAtUnix': 100}, {'requesterId': 'u2', 'requestedAtUnix': 101}],
        },
        {
            'group_id': 'g',
            'group_name': 'RG',
            'pending_count': 0,
            'member_count': 102,
            'requesters': [],
        },
    ])
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: next(fresh_states))
    monkeypatch.setattr('scripts.production_ops_daemon.run_formal_approval_command', lambda command, timeout: {
        'returncode': 0,
        'result': {'formal_run': {'approval_run_id': 'startup-run-fail', 'result': {'verified': False, 'crm_recorded': False}}},
    })

    state = {}
    cycle = run_cycle(args, state)

    assert cycle['startup_initial_batch']['triggered'] is True
    assert cycle['startup_initial_batch']['ok'] is False
    assert cycle['startup_initial_batch']['pending_count'] == 0
    assert cycle['startup_initial_batch']['final_pending_count'] == 0
    assert cycle['startup_initial_batch']['attempt_results'][0]['recheck_error'] == ''
    assert cycle['startup_initial_batch']['startup_probe_rechecks'][0]['pending_count'] == 0
    assert 'error' in cycle['startup_initial_batch']['startup_probe_rechecks'][1]
    assert cycle['startup_initial_batch']['attempts'] == 1
    assert state['monitoring_session']['startup_initial_batch_done'] is True
    assert state['monitoring_session']['startup_initial_batch_attempts'] == 1


def test_run_cycle_startup_initial_batch_retries_at_most_twice_then_exits_startup(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-3'

    group_state = {
        'group_id': 'g',
        'group_name': 'RG',
        'pending_count': 2,
        'member_count': 100,
        'requesters': [{'requesterId': 'u1', 'requestedAtUnix': 100}, {'requesterId': 'u2', 'requestedAtUnix': 101}],
    }

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/group-state'):
            return dict(group_state)
        if url.endswith('/api/ops/approval-batches/evaluate'):
            return {
                'approval_type': 'registration_group',
                'registration_group': 'RG',
                'pending_count': 2,
                'oldest_pending_at': '2026-04-28T00:00:00+00:00',
                'ready': False,
                'release_count': 0,
                'reason_code': 'waiting_for_batch',
                'batch_size': 30,
                'timeout_minutes': 30,
                'elapsed_minutes': 1,
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: dict(group_state))

    calls = {'n': 0}
    def fake_run_formal_approval_command(command, timeout):
        calls['n'] += 1
        return {
            'returncode': 0,
            'result': {'formal_run': {'approval_run_id': f'startup-run-{calls["n"]}', 'result': {'verified': False, 'crm_recorded': False}}},
        }

    monkeypatch.setattr('scripts.production_ops_daemon.run_formal_approval_command', fake_run_formal_approval_command)

    state = {}
    cycle = run_cycle(args, state)

    assert calls['n'] == 3
    assert cycle['startup_initial_batch']['triggered'] is True
    assert cycle['startup_initial_batch']['ok'] is False
    assert cycle['startup_initial_batch']['attempts'] == 3
    assert cycle['startup_initial_batch']['max_retries'] == 2
    assert cycle['startup_initial_batch']['retries_exhausted'] is True
    assert state['monitoring_session']['startup_initial_batch_done'] is True
    assert state['monitoring_session']['startup_initial_batch_attempts'] == 3

    second_cycle = run_cycle(args, state)
    assert second_cycle['startup_initial_batch']['startup_initial_batch_done'] is True
    assert second_cycle['startup_initial_batch']['attempts'] == 3


def test_run_cycle_timeout_flush_failure_sets_30_minute_cooldown(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/group-state'):
            return {
                'group_id': 'g',
                'group_name': 'RG',
                'pending_count': 1,
                'member_count': 100,
                'requesters': [
                    {'requesterId': 'u1', 'requestedAtUnix': 100},
                ],
            }
        if url.endswith('/api/ops/approval-batches/evaluate'):
            return {
                'approval_type': 'registration_group',
                'registration_group': 'RG',
                'pending_count': 1,
                'oldest_pending_at': '2026-04-28T00:00:00+00:00',
                'ready': True,
                'release_count': 1,
                'reason_code': 'timeout_flush',
                'batch_size': 30,
                'timeout_minutes': 30,
                'elapsed_minutes': 31,
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: {
        'group_id': 'g',
        'group_name': 'RG',
        'pending_count': 1,
        'member_count': 100,
        'requesters': [
            {'requesterId': 'u1', 'requestedAtUnix': 100},
        ],
    })

    calls = {'n': 0}

    def fake_run_formal_approval_command(command, timeout):
        calls['n'] += 1
        return {
            'returncode': 0,
            'result': {
                'formal_run': {
                    'approval_run_id': f'run-{calls["n"]}',
                    'result': {
                        'verified': False,
                        'crm_recorded': False,
                    },
                },
            },
        }

    monkeypatch.setattr('scripts.production_ops_daemon.run_formal_approval_command', fake_run_formal_approval_command)

    state = {}
    first_cycle = run_cycle(args, state)
    assert first_cycle['formal_approval']['triggered'] is True
    assert first_cycle['formal_approval']['trigger_cooldown_seconds'] == 1800
    assert calls['n'] == 1

    second_cycle = run_cycle(args, state)
    assert second_cycle['formal_approval']['triggered'] is False
    assert second_cycle['formal_approval']['cooldown_skip'] is True
    assert second_cycle['formal_approval']['trigger_cooldown_seconds'] == 1800
    assert calls['n'] == 1


def test_run_cycle_accepts_successful_formal_run_from_final_status_shape(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/group-state'):
            return {
                'group_id': 'g',
                'group_name': 'RG',
                'pending_count': 2,
                'member_count': 4,
                'requesters': [
                    {'requesterId': 'u1', 'requestedAtUnix': 100},
                    {'requesterId': 'u2', 'requestedAtUnix': 101},
                ],
            }
        if url.endswith('/api/ops/approval-batches/evaluate'):
            return {
                'approval_type': 'registration_group',
                'registration_group': 'RG',
                'pending_count': 2,
                'oldest_pending_at': '2026-04-28T00:00:00+00:00',
                'ready': True,
                'release_count': 2,
                'reason_code': 'timeout_flush',
                'batch_size': 30,
                'timeout_minutes': 30,
                'elapsed_minutes': 31,
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: {
        'group_id': 'g',
        'group_name': 'RG',
        'pending_count': 2,
        'member_count': 4,
        'requesters': [
            {'requesterId': 'u1', 'requestedAtUnix': 100},
            {'requesterId': 'u2', 'requestedAtUnix': 101},
        ],
    })
    monkeypatch.setattr('scripts.production_ops_daemon.run_formal_approval_command', lambda command, timeout: {
        'returncode': 0,
        'result': {
            'formal_run': {
                'approval_run_id': 'run-success',
                'final_status': {
                    'result': {
                        'verified': True,
                        'crm_recorded': True,
                        'result_code': 'approved',
                    }
                },
            },
        },
    })

    cycle = run_cycle(args, {})

    assert cycle['formal_approval']['triggered'] is True
    assert cycle['formal_approval']['ok'] is True
    assert cycle['formal_approval']['result']['formal_run']['final_status']['result']['verified'] is True



def test_run_cycle_drains_newly_surfaced_registration_pending_into_same_success_notice(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    worker_states = [
        {
            'group_id': 'g',
            'group_name': 'RG',
            'pending_count': 4,
            'member_count': 436,
            'requesters': [
                {'requesterId': 'u1', 'requestedAtUnix': 100},
                {'requesterId': 'u2', 'requestedAtUnix': 101},
                {'requesterId': 'u3', 'requestedAtUnix': 102},
                {'requesterId': 'u4', 'requestedAtUnix': 103},
            ],
        },
        {
            'group_id': 'g',
            'group_name': 'RG',
            'pending_count': 4,
            'member_count': 440,
            'requesters': [
                {'requesterId': 'u5', 'requestedAtUnix': 104},
                {'requesterId': 'u6', 'requestedAtUnix': 105},
                {'requesterId': 'u7', 'requestedAtUnix': 106},
                {'requesterId': 'u8', 'requestedAtUnix': 107},
            ],
        },
        {
            'group_id': 'g',
            'group_name': 'RG',
            'pending_count': 0,
            'member_count': 444,
            'requesters': [],
        },
    ]
    fresh_states = list(worker_states)
    evaluate_responses = [
        {
            'approval_type': 'registration_group',
            'registration_group': 'RG',
            'pending_count': 4,
            'oldest_pending_at': '2026-04-28T00:00:00+00:00',
            'ready': True,
            'release_count': 4,
            'reason_code': 'timeout_flush',
            'batch_size': 30,
            'timeout_minutes': 30,
            'elapsed_minutes': 31,
        },
        {
            'approval_type': 'registration_group',
            'registration_group': 'RG',
            'pending_count': 4,
            'oldest_pending_at': '2026-04-28T00:00:05+00:00',
            'ready': True,
            'release_count': 4,
            'reason_code': 'timeout_flush',
            'batch_size': 30,
            'timeout_minutes': 30,
            'elapsed_minutes': 31,
        },
        {
            'approval_type': 'registration_group',
            'registration_group': 'RG',
            'pending_count': 0,
            'oldest_pending_at': None,
            'ready': False,
            'release_count': 0,
            'reason_code': 'waiting_next_cycle',
            'batch_size': 30,
            'timeout_minutes': 30,
            'elapsed_minutes': 0,
            'remaining_minutes': 30,
            'remaining_seconds': 1800,
            'cycle_started_at': '2026-04-28T00:00:00+00:00',
            'cycle_ends_at': '2026-04-28T00:30:00+00:00',
        },
    ]
    approval_calls = {'n': 0}

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/group-state'):
            return worker_states.pop(0)
        if url.endswith('/api/ops/approval-batches/evaluate'):
            return evaluate_responses.pop(0)
        raise AssertionError(url)

    def fake_run_fresh_probe(*args, **kwargs):
        return fresh_states.pop(0)

    def fake_run_formal_approval_command(command, timeout):
        approval_calls['n'] += 1
        run_no = approval_calls['n']
        return {
            'returncode': 0,
            'result': {
                'formal_run': {
                    'approval_run_id': f'run-{run_no}',
                    'final_status': {
                        'result': {
                            'verified': True,
                            'crm_recorded': True,
                            'result_code': 'approved',

                        },
                    },
                },
            },
        }
