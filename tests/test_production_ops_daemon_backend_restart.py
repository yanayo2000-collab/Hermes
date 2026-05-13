from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.production_ops import build_success_notifications, format_lark_alert
from scripts.production_ops_daemon import SUCCESS_NOTIFICATION_CODES, _build_formal_approval_command, _build_recovery_notifications, _evaluate_release, _notification_delivery_summary, _notify_incidents, _run_registration_group_cycle, _session_state, _target_session_key, run_cycle


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
    monitoring_session_id = ''


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



def test_success_notification_codes_exact_match_guardrail():
    assert SUCCESS_NOTIFICATION_CODES == {
        'formal_approval_succeeded',
        'formal_approval_recovered',
        'registration_cycle_noop',
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



def test_registration_group_cycle_restarts_account_runtime_before_reporting_worker_state_failed(monkeypatch):
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
        run_cycle(args, state)
    assert calls['stop'] == 0
    assert calls['start'] == 0
    assert calls['session'] == 0

    tenth_cycle = run_cycle(args, state)
    cycle_row = tenth_cycle['registration_group_cycles'][0]

    assert calls['stop'] == 1
    assert calls['start'] == 1
    assert calls['session'] == 1
    assert cycle_row['worker_state']['recovery']['trigger_reason'] == 'healthy_false_zero_stale_session'
    assert cycle_row['worker_state']['recovery']['mode'] == 'account_runtime_rebuild'



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
                            'approved_count': 4,
                            'pending_after': 0 if run_no == 2 else 4,
                            'member_count_after': 440 if run_no == 1 else 444,
                        }
                    },
                }
            },
        }

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', fake_run_fresh_probe)
    monkeypatch.setattr('scripts.production_ops_daemon.run_formal_approval_command', fake_run_formal_approval_command)

    cycle = run_cycle(args, {})
    notifications = build_success_notifications(cycle)

    assert approval_calls['n'] == 2
    assert cycle['formal_approval']['triggered'] is True
    assert cycle['formal_approval']['ok'] is True
    assert cycle['formal_approval']['drain_rounds'] == 2
    assert cycle['formal_approval']['aggregate_approved_count'] == 8
    assert cycle['formal_approval']['approval_run_ids'] == ['run-1', 'run-2']
    assert cycle['formal_approval']['final_pending_count'] == 0
    assert len(notifications) == 2
    assert [item['code'] for item in notifications] == ['formal_approval_succeeded', 'formal_approval_succeeded']
    assert [item['dedupe_key'] for item in notifications] == [
        'formal_approval_succeeded:run-1',
        'formal_approval_succeeded:run-2',
    ]
    assert [item['details']['approval_run_ids'] for item in notifications] == [['run-1'], ['run-2']]
    assert [item['details']['approved_count'] for item in notifications] == [4, 4]
    assert [item['details']['pending_after'] for item in notifications] == [4, 0]



def test_notify_incidents_sends_success_notification_once_per_approval_run(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.notify_cooldown_seconds = 900
    state = {}
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
    success_notifications = [{
        'severity': 'info',
        'code': 'formal_approval_succeeded',
        'summary': '注册群审批成功',
        'details': {'approved_count': 2, 'pending_after': 0, 'member_count_after': 6},
        'dedupe_key': 'formal_approval_succeeded:run-success',
    }]

    sent_texts = []

    class DummyNotifier:
        def send_text(self, text):
            sent_texts.append(text)
            return {'code': 0}

    monkeypatch.setattr('scripts.production_ops_daemon._build_notifier_from_args', lambda args, cycle=None: DummyNotifier())

    first = _notify_incidents(args, state, cycle, success_notifications)
    second = _notify_incidents(args, state, cycle, success_notifications)
    later_cycle = {**cycle, 'checked_at': '2026-04-29T06:20:00+00:00'}
    third = _notify_incidents(args, state, later_cycle, success_notifications)

    assert len(first) == 1
    assert first[0]['status'] == 'sent'
    assert len(sent_texts) == 1
    assert '✅ 生产守护通知｜注册群审批成功' in sent_texts[0]
    assert '审批类型: 常规轮次' in sent_texts[0]
    assert '本次通过人数: 2' in sent_texts[0]
    assert '剩余待审批人数: 0' in sent_texts[0]
    assert second == []
    assert third == []



def test_notify_incidents_sends_registration_cycle_noop_only_once_per_cycle(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.notify_cooldown_seconds = 900
    state = {}
    cycle = {
        'checked_at': '2026-05-08T01:00:40+00:00',
        'registration_group': 'RG',
        'monitor_target': {'group_name': '注册测试1'},
    }
    success_notifications = [{
        'severity': 'info',
        'code': 'registration_cycle_noop',
        'summary': '注册群本轮无审批',
        'details': {
            'group_name': '注册测试1',
            'pending_count': 0,
            'cycle_started_at': '2026-05-08T01:00:00+00:00',
            'cycle_ends_at': '2026-05-08T01:30:00+00:00',
            'reason_code': 'waiting_next_cycle',
        },
        'dedupe_key': 'registration_cycle_noop:注册测试1|2026-05-08T01:00:00+00:00',
    }]

    sent_texts = []

    class DummyNotifier:
        def send_text(self, text):
            sent_texts.append(text)
            return {'code': 0}

    monkeypatch.setattr('scripts.production_ops_daemon._build_notifier_from_args', lambda args, cycle=None: DummyNotifier())

    first = _notify_incidents(args, state, cycle, success_notifications)
    later_cycle = {**cycle, 'checked_at': '2026-05-08T01:15:47+00:00'}
    second = _notify_incidents(args, state, later_cycle, success_notifications)

    assert len(first) == 1
    assert first[0]['status'] == 'sent'
    assert len(sent_texts) == 1
    assert '✅ 生产守护通知｜注册群本轮无审批' in sent_texts[0]
    assert second == []



def test_notify_incidents_requires_three_consecutive_worker_state_failures_before_alert(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.notify_cooldown_seconds = 900
    state = {}
    cycle = {
        'checked_at': '2026-05-06T06:12:21+00:00',
        'registration_group': 'RG',
    }
    incidents = [{
        'severity': 'critical',
        'code': 'worker_state_failed',
        'summary': '群状态探测失败',
        'details': {'error': '<urlopen error [Errno 61] Connection refused>'},
        'dedupe_key': 'worker_state_failed',
    }]
    sent_texts = []

    class DummyNotifier:
        def send_text(self, text):
            sent_texts.append(text)
            return {'code': 0}

    monkeypatch.setattr('scripts.production_ops_daemon._build_notifier_from_args', lambda args, cycle=None: DummyNotifier())

    first = _notify_incidents(args, state, cycle, incidents)
    second = _notify_incidents(args, state, cycle, incidents)
    third = _notify_incidents(args, state, cycle, incidents)

    assert first == []
    assert second == []
    assert len(third) == 1
    assert third[0]['status'] == 'sent'
    assert third[0]['streak_count'] == 3
    assert third[0]['threshold'] == 3
    assert len(sent_texts) == 1
    assert '🚨 生产守护告警｜群状态探测失败' in sent_texts[0]



def test_notify_incidents_resets_worker_state_failed_streak_after_recovery(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.notify_cooldown_seconds = 900
    state = {}
    cycle = {
        'checked_at': '2026-05-06T06:12:21+00:00',
        'registration_group': 'RG',
    }
    incidents = [{
        'severity': 'critical',
        'code': 'worker_state_failed',
        'summary': '群状态探测失败',
        'details': {'error': '<urlopen error [Errno 61] Connection refused>'},
        'dedupe_key': 'worker_state_failed',
    }]

    class DummyNotifier:
        def send_text(self, text):
            return {'code': 0}

    monkeypatch.setattr('scripts.production_ops_daemon._build_notifier_from_args', lambda args, cycle=None: DummyNotifier())

    assert _notify_incidents(args, state, cycle, incidents) == []
    assert state['incident_streaks']['worker_state_failed']['count'] == 1
    assert _notify_incidents(args, state, cycle, incidents) == []
    assert state['incident_streaks']['worker_state_failed']['count'] == 2
    assert _notify_incidents(args, state, cycle, []) == []
    assert 'worker_state_failed' not in state.get('incident_streaks', {})
    assert _notify_incidents(args, state, cycle, incidents) == []



def test_notify_incidents_skips_registration_zero_pending_unverified_alert(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.notify_cooldown_seconds = 900
    state = {}
    cycle = {
        'checked_at': '2026-05-09T09:07:49+00:00',
        'registration_group': 'RG',
        'monitor_target': {'group_name': '注册测试1'},
    }
    incidents = [{
        'severity': 'critical',
        'code': 'registration_zero_pending_unverified',
        'summary': '注册群零待审批未核实',
        'notify_disabled': True,
        'details': {
            'group_name': '注册测试1',
            'pending_count': 0,
            'reason': 'same_runtime_family_zero_pending',
        },
        'dedupe_key': 'registration_zero_pending_unverified:注册测试1|2026-05-09T09:07:17+00:00',
    }]
    sent_texts = []

    class DummyNotifier:
        def send_text(self, text):
            sent_texts.append(text)
            return {'code': 0}

    monkeypatch.setattr('scripts.production_ops_daemon._build_notifier_from_args', lambda args, cycle=None: DummyNotifier())

    result = _notify_incidents(args, state, cycle, incidents)

    assert result == []
    assert sent_texts == []
    assert state.get('incident_streaks', {}) == {}
    assert state.get('notifications', {}) == {}



def test_build_success_notifications_includes_startup_initial_batch_success():
    cycle = {
        'checked_at': '2026-04-30T02:48:58+00:00',
        'registration_group': 'https://chat.whatsapp.com/EoHAaKPML7p3BG7LNEbOl1',
        'startup_initial_batch': {
            'triggered': True,
            'ok': True,
            'session_id': 'session-123',
            'pending_count': 2,
            'attempt_results': [
                {
                    'result': {
                        'formal_run': {
                            'approval_run_id': 'startup-success-1',
                            'final_status': {
                                'result': {
                                    'verified': True,
                                    'crm_recorded': True,
                                    'approved_count': 2,
                                    'pending_after': 0,
                                    'member_count_after': 5,
                                    'result_code': 'approved',
                                }
                            },
                        }
                    }
                }
            ],
        },
    }

    notifications = build_success_notifications(cycle)

    assert len(notifications) == 1
    assert notifications[0]['code'] == 'startup_initial_batch_succeeded'
    assert notifications[0]['details']['approved_count'] == 2
    assert notifications[0]['details']['pending_after'] == 0
    assert notifications[0]['details']['member_count_after'] == 5
    assert notifications[0]['dedupe_key'] == 'startup_initial_batch_succeeded:startup-success-1'



def test_notify_incidents_sends_startup_initial_batch_success_once(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.notify_cooldown_seconds = 900
    state = {}
    cycle = {
        'checked_at': '2026-04-30T02:48:58+00:00',
        'registration_group': 'https://chat.whatsapp.com/EoHAaKPML7p3BG7LNEbOl1',
        'startup_initial_batch': {
            'triggered': True,
            'ok': True,
            'session_id': 'session-123',
            'pending_count': 2,
            'attempt_results': [
                {
                    'result': {
                        'formal_run': {
                            'approval_run_id': 'startup-success-1',
                            'final_status': {
                                'result': {
                                    'verified': True,
                                    'crm_recorded': True,
                                    'approved_count': 2,
                                    'pending_after': 0,
                                    'member_count_after': 5,
                                    'result_code': 'approved',
                                }
                            },
                        }
                    }
                }
            ],
        },
    }
    success_notifications = build_success_notifications(cycle)
    sent_texts = []

    class DummyNotifier:
        def send_text(self, text):
            sent_texts.append(text)
            return {'code': 0}

    monkeypatch.setattr('scripts.production_ops_daemon._build_notifier_from_args', lambda args, cycle=None: DummyNotifier())

    first = _notify_incidents(args, state, cycle, success_notifications)
    second = _notify_incidents(args, state, cycle, success_notifications)

    assert len(first) == 1
    assert first[0]['status'] == 'sent'
    assert '✅ 生产守护通知｜启动首批审批成功' in sent_texts[0]
    assert '审批类型: 启动首批' in sent_texts[0]
    assert '本次通过人数: 2' in sent_texts[0]
    assert '剩余待审批人数: 0' in sent_texts[0]
    assert second == []



def test_notify_incidents_retries_success_notification_after_all_deliveries_fail(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.notify_cooldown_seconds = 900
    state = {}
    cycle = {
        'checked_at': '2026-05-08T02:40:00+00:00',
        'registration_group': 'RG',
        'monitor_target': {
            'notify_profile_name': 'wa-approval-broadcast',
            'notify_robot_name': '审批bot01',
        },
    }
    incidents = [{
        'severity': 'info',
        'code': 'formal_approval_succeeded',
        'summary': '注册群审批成功',
        'details': {
            'approved_count': 3,
            'pending_after': 0,
        },
        'dedupe_key': 'formal_approval_succeeded:test-retry-all-failed',
    }]
    calls = {'count': 0}

    class FailingThenHealthyNotifier:
        def send_text(self, text):
            calls['count'] += 1
            if calls['count'] <= 1:
                raise RuntimeError('temporary send failure')
            return {'code': 0}

    monkeypatch.setattr('scripts.production_ops_daemon._build_notifier_from_args', lambda args, cycle=None: FailingThenHealthyNotifier())

    first = _notify_incidents(args, state, cycle, incidents)

    assert len(first) == 1
    assert first[0]['status'] == 'failed'
    assert state['notifications']['formal_approval_succeeded:test-retry-all-failed']['last_status'] == 'failed'
    assert 'last_sent_at' not in state['notifications']['formal_approval_succeeded:test-retry-all-failed']

    second = _notify_incidents(args, state, cycle, incidents)

    assert len(second) == 1
    assert second[0]['status'] == 'sent'
    assert state['notifications']['formal_approval_succeeded:test-retry-all-failed']['last_status'] == 'sent'
    assert state['notifications']['formal_approval_succeeded:test-retry-all-failed']['last_sent_at'] == '2026-05-08T02:40:00+00:00'



def test_notify_incidents_retries_success_notification_after_failed_delivery(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.notify_cooldown_seconds = 900
    state = {}
    cycle = {
        'checked_at': '2026-05-08T02:40:00+00:00',
        'registration_group': 'RG',
        'monitor_target': {
            'notify_profile_name': 'wa-approval-broadcast',
            'notify_robot_name': '审批bot01',
        },
    }
    incidents = [{
        'severity': 'info',
        'code': 'formal_approval_succeeded',
        'summary': '注册群审批成功',
        'details': {
            'approved_count': 3,
            'pending_after': 0,
        },
        'dedupe_key': 'formal_approval_succeeded:test-retry',
    }]
    calls = {'count': 0}

    class FlakyNotifier:
        def send_text(self, text):
            calls['count'] += 1
            if calls['count'] == 1:
                raise RuntimeError('temporary send failure')
            return {'code': 0}

    monkeypatch.setattr('scripts.production_ops_daemon._build_notifier_from_args', lambda args, cycle=None: FlakyNotifier())

    first = _notify_incidents(args, state, cycle, incidents)

    assert len(first) == 1
    assert first[0]['status'] == 'failed'
    assert state['notifications']['formal_approval_succeeded:test-retry']['last_status'] == 'failed'
    assert 'last_sent_at' not in state['notifications']['formal_approval_succeeded:test-retry']

    second = _notify_incidents(args, state, cycle, incidents)

    assert len(second) == 1
    assert second[0]['status'] == 'sent'
    assert state['notifications']['formal_approval_succeeded:test-retry']['last_status'] == 'sent'
    assert state['notifications']['formal_approval_succeeded:test-retry']['last_sent_at'] == '2026-05-08T02:40:00+00:00'



def test_notify_incidents_retries_success_notification_after_skipped_no_notifier(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.notify_cooldown_seconds = 900
    state = {}
    cycle = {
        'checked_at': '2026-05-08T02:50:00+00:00',
        'registration_group': 'RG',
        'monitor_target': {
            'notify_profile_name': 'wa-approval-broadcast',
            'notify_robot_name': '审批bot01',
        },
    }
    incidents = [{
        'severity': 'info',
        'code': 'startup_initial_batch_succeeded',
        'summary': '启动首批审批成功',
        'details': {
            'approved_count': 1,
            'pending_after': 0,
        },
        'dedupe_key': 'startup_initial_batch_succeeded:test-skipped',
    }]
    calls = {'count': 0}

    class HealthyNotifier:
        def send_text(self, text):
            calls['count'] += 1
            return {'code': 0}

    monkeypatch.setattr('scripts.production_ops_daemon._build_notifier_from_args', lambda args, cycle=None: None if calls['count'] == 0 else HealthyNotifier())

    first = _notify_incidents(args, state, cycle, incidents)
    assert len(first) == 1
    assert first[0]['status'] == 'skipped_no_notifier'
    assert state['notifications']['startup_initial_batch_succeeded:test-skipped']['last_status'] == 'skipped_no_notifier'
    assert 'last_sent_at' not in state['notifications']['startup_initial_batch_succeeded:test-skipped']

    calls['count'] = 1
    second = _notify_incidents(args, state, cycle, incidents)
    assert len(second) == 1
    assert second[0]['status'] == 'sent'
    assert state['notifications']['startup_initial_batch_succeeded:test-skipped']['last_status'] == 'sent'
    assert state['notifications']['startup_initial_batch_succeeded:test-skipped']['last_sent_at'] == '2026-05-08T02:50:00+00:00'



def test_notify_incidents_sends_official_group_manual_review_required_only_once(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.notify_cooldown_seconds = 900
    state = {}
    cycle = {
        'checked_at': '2026-05-08T03:00:00+00:00',
        'registration_group': 'RG',
        'monitor_target': {
            'notify_profile_name': 'wa-approval-broadcast',
            'notify_robot_name': '审批bot01',
        },
    }
    incidents = [{
        'severity': 'info',
        'code': 'official_group_manual_review_required',
        'summary': '官方群审批需人工复核',
        'details': {
            'group_name': '官方测试1',
            'reason_code': 'crm_customer_not_found',
            'remaining_pending_count': 1,
        },
        'dedupe_key': 'official_group_manual_review_required:official-group-1:lead-1:crm_customer_not_found:1',
    }]
    sent_texts = []

    class DummyNotifier:
        def send_text(self, text):
            sent_texts.append(text)
            return {'code': 0}

    monkeypatch.setattr('scripts.production_ops_daemon._build_notifier_from_args', lambda args, cycle=None: DummyNotifier())

    first = _notify_incidents(args, state, cycle, incidents)
    second = _notify_incidents(args, state, cycle, incidents)

    assert len(first) == 1
    assert first[0]['status'] == 'sent'
    assert len(first[0]['deliveries']) == 1
    assert len(sent_texts) == 1
    assert second == []


def test_notify_incidents_sends_distinct_official_group_manual_review_notifications_when_remaining_count_changes(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.notify_cooldown_seconds = 900
    state = {}
    cycle = {
        'checked_at': '2026-05-08T03:05:00+00:00',
        'registration_group': 'RG',
        'monitor_target': {
            'notify_profile_name': 'wa-approval-broadcast',
            'notify_robot_name': '审批bot01',
        },
    }
    sent_texts = []

    class DummyNotifier:
        def send_text(self, text):
            sent_texts.append(text)
            return {'code': 0}

    monkeypatch.setattr('scripts.production_ops_daemon._build_notifier_from_args', lambda args, cycle=None: DummyNotifier())

    first = _notify_incidents(args, state, cycle, [{
        'severity': 'warning',
        'code': 'official_group_manual_review_required',
        'summary': '官方群审批需人工复核',
        'details': {
            'group_name': '官方测试1',
            'mobile': '+852****3942',
            'reason_code': 'official_group_requester_unmatched',
            'remaining_pending_count': 2,
        },
        'dedupe_key': 'official_group_manual_review_required:official-group-permata:150749711495258@lid:official_group_requester_unmatched:2',
    }])
    second = _notify_incidents(args, state, cycle, [{
        'severity': 'warning',
        'code': 'official_group_manual_review_required',
        'summary': '官方群审批需人工复核',
        'details': {
            'group_name': '官方测试1',
            'mobile': '+852****3942',
            'reason_code': 'official_group_requester_unmatched',
            'remaining_pending_count': 1,
        },
        'dedupe_key': 'official_group_manual_review_required:official-group-permata:150749711495258@lid:official_group_requester_unmatched:1',
    }])

    assert len(first) == 1
    assert len(second) == 1
    assert first[0]['status'] == 'sent'
    assert second[0]['status'] == 'sent'
    assert len(sent_texts) == 2



def test_notify_incidents_prefers_binding_notify_profile_credentials(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.notify_enabled = True
    args.feishu_app_id = 'cli_wrong_default'
    args.feishu_app_secret = 'wrong-secret'
    args.notify_chat_id = 'oc_wrong_default'
    args.feishu_domain = 'lark'
    args.notify_cooldown_seconds = 900
    state = {}
    cycle = {
        'checked_at': '2026-04-30T02:48:58+00:00',
        'registration_group': 'RG',
        'monitor_target': {
            'notify_profile_name': 'wa-approval-broadcast',
            'notify_robot_name': '审批bot01',
        },
    }
    incidents = [{
        'severity': 'info',
        'code': 'startup_initial_batch_succeeded',
        'summary': '启动首批审批成功',
        'details': {'approved_count': 2, 'pending_after': 0, 'member_count_after': 5},
        'dedupe_key': 'startup_initial_batch_succeeded:test-routing',
    }]
    captured = {}

    class DummyNotifier:
        def __init__(self, *, app_id, app_secret, chat_id, domain='lark'):
            captured['init'] = {
                'app_id': app_id,
                'app_secret': app_secret,
                'chat_id': chat_id,
                'domain': domain,
            }

        def send_text(self, text):
            captured['text'] = text
            return {'code': 0}

    monkeypatch.setattr('scripts.production_ops_daemon._load_notify_profile_env', lambda profile_name: {
        'FEISHU_APP_ID': 'cli_bot01',
        'FEISHU_APP_SECRET': 'bot01-secret',
        'FEISHU_HOME_CHANNEL': 'oc_bot01',
        'FEISHU_DOMAIN': 'lark',
    })
    monkeypatch.setattr('scripts.production_ops_daemon.FeishuNotifier', DummyNotifier)

    sent = _notify_incidents(args, state, cycle, incidents)

    assert len(sent) == 1
    assert sent[0]['status'] == 'sent'
    assert captured['init']['app_id'] == 'cli_bot01'
    assert captured['init']['chat_id'] == 'oc_bot01'
    assert captured['init']['app_secret'] == 'bot01-secret'



def test_notify_incidents_prefers_incident_notify_profile_credentials(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.notify_enabled = True
    args.feishu_app_id = 'cli_wrong_default'
    args.feishu_app_secret = 'wrong-secret'
    args.notify_chat_id = 'oc_wrong_default'
    args.feishu_domain = 'lark'
    args.notify_cooldown_seconds = 900
    state = {}
    cycle = {
        'checked_at': '2026-05-06T07:21:10+00:00',
        'registration_group': 'RG',
        'monitor_target': {
            'notify_profile_name': 'wrong-profile',
            'notify_robot_name': '错误机器人',
        },
    }
    incidents = [{
        'severity': 'info',
        'code': 'official_group_approval_succeeded',
        'summary': '官方群审批成功',
        'details': {
            'group_name': '官方测试1',
            'approved_count': 1,
            'pending_after': 0,
            'member_count_after': 5,
        },
        'notify_profile_name': 'wa-approval-broadcast',
        'notify_robot_name': '审批bot01',
        'dedupe_key': 'official_group_approval_succeeded:test-routing',
    }]
    captured = {'inits': [], 'texts': []}

    def fake_load_notify_profile_env(profile_name):
        if profile_name == 'wa-approval-broadcast':
            return {
                'FEISHU_APP_ID': 'cli_bot01',
                'FEISHU_APP_SECRET': 'bot01-secret',
                'FEISHU_HOME_CHANNEL': 'oc_bot01',
                'FEISHU_DOMAIN': 'lark',
            }
        if profile_name == 'wa-approval-broadcast-02':
            return {
                'FEISHU_APP_ID': 'cli_bot02',
                'FEISHU_APP_SECRET': 'bot02-secret',
                'FEISHU_HOME_CHANNEL': 'oc_bot02',
                'FEISHU_DOMAIN': 'lark',
            }
        if profile_name == 'wrong-profile':
            return {
                'FEISHU_APP_ID': 'wrong_bot',
                'FEISHU_APP_SECRET': 'wrong-secret-2',
                'FEISHU_HOME_CHANNEL': 'oc_wrong_2',
                'FEISHU_DOMAIN': 'lark',
            }
        return {}

    class DummyNotifier:
        def __init__(self, *, app_id, app_secret, chat_id, domain='lark'):
            captured['inits'].append({
                'app_id': app_id,
                'app_secret': app_secret,
                'chat_id': chat_id,
                'domain': domain,
            })

        def send_text(self, text):
            captured['texts'].append(text)
            return {'code': 0}

    monkeypatch.setattr('scripts.production_ops_daemon._load_notify_profile_env', fake_load_notify_profile_env)
    monkeypatch.setattr('scripts.production_ops_daemon.FeishuNotifier', DummyNotifier)

    sent = _notify_incidents(args, state, cycle, incidents)

    assert len(sent) == 1
    assert sent[0]['status'] == 'sent'
    assert sent[0]['notify_profile_name'] == 'wa-approval-broadcast'
    assert len(sent[0]['deliveries']) == 1
    assert sent[0]['deliveries'][0]['notify_profile_name'] == 'wa-approval-broadcast'
    assert len(captured['inits']) == 1
    assert captured['inits'][0]['app_id'] == 'cli_bot01'
    assert captured['inits'][0]['chat_id'] == 'oc_bot01'
    assert captured['inits'][0]['app_secret'] == 'bot01-secret'
    assert all('官方群审批成功' in text for text in captured['texts'])
    assert all('本次通过人数: 1' in text for text in captured['texts'])
    assert all('剩余待审批人数: 0' in text for text in captured['texts'])
    assert all('原因: 已审批通过 1 人' in text for text in captured['texts'])



def test_notify_incidents_backend_unhealthy_fanouts_to_bot01_and_bot02(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.notify_enabled = True
    args.feishu_app_id = 'cli_wrong_default'
    args.feishu_app_secret = 'wrong-secret'
    args.notify_chat_id = 'oc_wrong_default'
    args.feishu_domain = 'lark'
    args.notify_cooldown_seconds = 900
    state = {}
    cycle = {
        'checked_at': '2026-05-07T10:33:18+00:00',
        'registration_group': '120363425215002840@g.us',
        'monitor_target': {
            'notify_profile_name': 'wa-approval-broadcast',
            'notify_robot_name': '审批bot01',
        },
    }
    incidents = [{
        'severity': 'critical',
        'code': 'backend_unhealthy',
        'summary': '后端健康检查失败',
        'details': {
            'error': '<urlopen error [Errno 61] Connection refused>',
        },
        'dedupe_key': 'backend_unhealthy',
    }]
    captured = {'inits': [], 'texts': []}

    def fake_load_notify_profile_env(profile_name):
        if profile_name == 'wa-approval-broadcast':
            return {
                'FEISHU_APP_ID': 'cli_bot01',
                'FEISHU_APP_SECRET': 'bot01-secret',
                'FEISHU_HOME_CHANNEL': 'oc_bot01',
                'FEISHU_DOMAIN': 'lark',
            }
        if profile_name == 'wa-approval-broadcast-02':
            return {
                'FEISHU_APP_ID': 'cli_bot02',
                'FEISHU_APP_SECRET': 'bot02-secret',
                'FEISHU_HOME_CHANNEL': 'oc_bot02',
                'FEISHU_DOMAIN': 'lark',
            }
        return {}

    class DummyNotifier:
        def __init__(self, *, app_id, app_secret, chat_id, domain='lark'):
            captured['inits'].append({
                'app_id': app_id,
                'app_secret': app_secret,
                'chat_id': chat_id,
                'domain': domain,
            })

        def send_text(self, text):
            captured['texts'].append(text)
            return {'code': 0}

    monkeypatch.setattr('scripts.production_ops_daemon._load_notify_profile_env', fake_load_notify_profile_env)
    monkeypatch.setattr('scripts.production_ops_daemon.FeishuNotifier', DummyNotifier)

    sent = _notify_incidents(args, state, cycle, incidents)

    assert len(sent) == 1
    assert sent[0]['code'] == 'backend_unhealthy'
    assert sent[0]['status'] == 'sent'
    assert sent[0]['notify_profile_name'] == 'wa-approval-broadcast'
    assert len(sent[0]['deliveries']) == 1
    assert sent[0]['deliveries'][0]['notify_profile_name'] == 'wa-approval-broadcast'
    assert sent[0]['deliveries'][0]['status'] == 'sent'
    assert len(captured['inits']) == 1
    assert captured['inits'][0]['app_id'] == 'cli_bot01'
    assert captured['inits'][0]['chat_id'] == 'oc_bot01'
    assert all('后端健康检查失败' in text for text in captured['texts'])
    assert all('Connection refused' in text for text in captured['texts'])



def test_run_cycle_uses_fresh_probe_as_authoritative_source(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/group-state'):
            return {
                'group_id': 'g',
                'group_name': 'RG',
                'pending_count': 10,
                'member_count': 339,
                'requesters': [{'requesterId': 'old1', 'requestedAtUnix': 100}],
            }
        if url.endswith('/api/ops/approval-batches/evaluate'):
            assert payload['pending_count'] == 10
            return {
                'approval_type': 'registration_group',
                'registration_group': 'RG',
                'pending_count': 10,
                'oldest_pending_at': payload['oldest_pending_at'],
                'ready': True,
                'release_count': 10,
                'reason_code': 'timeout_flush',
                'batch_size': 30,
                'timeout_minutes': 30,
                'elapsed_minutes': 40,
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: {
        'group_id': 'g',
        'group_name': 'RG',
        'pending_count': 6,
        'member_count': 376,
        'requesters': [
            {'requesterId': 'fresh1', 'requestedAtUnix': 100},
            {'requesterId': 'fresh2', 'requestedAtUnix': 101},
            {'requesterId': 'fresh3', 'requestedAtUnix': 102},
            {'requesterId': 'fresh4', 'requestedAtUnix': 103},
            {'requesterId': 'fresh5', 'requestedAtUnix': 104},
            {'requesterId': 'fresh6', 'requestedAtUnix': 105},
        ],
    })

    captured = {}
    def fake_run_formal_approval_command(command, timeout):
        captured['command'] = command
        return {
            'returncode': 0,
            'result': {'formal_run': {'approval_run_id': 'run-fresh', 'result': {'verified': False, 'crm_recorded': False}}},
        }

    monkeypatch.setattr('scripts.production_ops_daemon.run_formal_approval_command', fake_run_formal_approval_command)

    cycle = run_cycle(args, {})
    assert cycle['decision_group_state']['source'] == 'group_state'
    assert cycle['decision_group_state']['mismatch'] is False
    assert cycle['fresh_probe']['skipped'] is True
    assert cycle['fresh_probe']['reason'] == 'group_state_is_authoritative_source'
    assert cycle['formal_approval']['release_count'] == 10
    assert captured['command'][captured['command'].index('--approved-count') + 1] == '10'


def test_run_cycle_prefers_active_monitored_binding_target_from_accounts_api(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    invite_link = 'https://chat.whatsapp.com/Bp1WKsmpcbC2RkAyIACeRv'
    calls = {'group_state': [], 'fresh_probe_called': False}

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
                            'base_url': 'http://127.0.0.1:52681',
                        },
                        'group_link_bindings': [
                            {
                                'link': 'https://chat.whatsapp.com/JfgI1v2OyayJPR9JJaMGDm',
                                'group_name': '印尼37群',
                                'enabled': False,
                                'area': 'Indonesia',
                                'schedule_runtime': {'configured': True, 'active_now': True},
                            },
                            {
                                'link': invite_link,
                                'group_name': '测试85',
                                'enabled': True,
                                'area': 'Indonesia',
                                'schedule_runtime': {'configured': True, 'active_now': True},
                            },
                        ],
                    }
                ]
            }
        if url == 'http://127.0.0.1:52681/group-state':
            calls['group_state'].append({'method': method, 'payload': payload})
            assert payload['registration_group'] == invite_link
            return {
                'group_id': 'g-test85',
                'group_name': '测试85',
                'pending_count': 2,
                'member_count': 5,
                'requesters': [
                    {'requesterId': 'u1', 'requestedAtUnix': 101},
                    {'requesterId': 'u2', 'requestedAtUnix': 102},
                ],
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)

    def fake_run_fresh_probe(command, *, timeout):
        calls['fresh_probe_called'] = True
        raise AssertionError('fresh probe should be skipped for dedicated runtime binding target')

    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', fake_run_fresh_probe)

    cycle = run_cycle(args, {})

    assert cycle['registration_group'] == invite_link
    assert cycle['monitor_target']['source'] == 'account_binding'
    assert cycle['monitor_target']['worker_base_url'] == 'http://127.0.0.1:52681'
    assert cycle['monitor_target']['group_name'] == '测试85'
    assert cycle['fresh_probe']['ok'] is False
    assert cycle['fresh_probe']['skipped'] is True
    assert cycle['fresh_probe']['reason'] == 'group_state_is_authoritative_source'
    assert cycle['decision_group_state']['source'] == 'group_state'
    assert calls['fresh_probe_called'] is False
    assert len(calls['group_state']) == 1


def test_run_cycle_does_not_fallback_to_shared_worker_when_binding_runtime_is_unavailable(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    invite_link = 'https://chat.whatsapp.com/EoHAaKPML7p3BG7LNEbOl1'
    calls = {'group_state': []}

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
                            'active': False,
                            'base_url': '',
                        },
                        'group_link_bindings': [
                            {
                                'link': invite_link,
                                'group_name': '注册01',
                                'enabled': True,
                                'area': 'Indonesia',
                                'schedule_runtime': {'configured': True, 'active_now': True},
                            }
                        ],
                    }
                ]
            }
        if url.endswith('/group-state'):
            calls['group_state'].append({'url': url, 'payload': payload})
            raise AssertionError('shared worker must not be used when selected binding runtime is unavailable')
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('fresh probe must not run when selected binding runtime is unavailable')))

    cycle = run_cycle(args, {})

    assert cycle['registration_group'] == invite_link
    assert cycle['monitor_target']['source'] == 'account_binding'
    assert cycle['monitor_target']['worker_base_url'] == ''
    assert cycle['monitor_targets']['selection_reason'] == 'configured_binding_runtime_unavailable'
    assert cycle['monitor_targets']['allow_fallback'] is False
    assert cycle['monitor_targets']['active_count'] == 1
    assert len(cycle['registration_group_cycles']) == 1
    assert cycle['registration_group_cycles'][0]['worker_state']['ok'] is False
    assert cycle['registration_group_cycles'][0]['worker_state']['error'] == 'worker_base_url_missing_for_selected_binding'
    assert cycle['registration_group_cycles'][0]['decision_group_state']['source'] == 'fail_closed'
    assert calls['group_state'] == []



def test_run_cycle_recent_binding_runtime_startup_grace_skips_rebuild_when_health_not_ready(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    invite_link = 'https://chat.whatsapp.com/EoHAaKPML7p3BG7LNEbOl1'
    now = datetime.now(timezone.utc)
    calls = {'group_state': [], 'runtime_stop': 0, 'runtime_start': 0, 'session_start': 0}

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
                            'active': False,
                            'pid': 12345,
                            'port': 58354,
                            'base_url': 'http://127.0.0.1:58354',
                            'status': 'stopped',
                            'started_at': now.isoformat(),
                            'stopped_at': None,
                        },
                        'group_link_bindings': [
                            {
                                'link': invite_link,
                                'group_name': '注册01',
                                'enabled': True,
                                'area': 'Indonesia',
                                'schedule_runtime': {'configured': True, 'active_now': True},
                            }
                        ],
                    }
                ]
            }
        if url.endswith('/runtime/internal/stop'):
            calls['runtime_stop'] += 1
            raise AssertionError('startup grace should skip runtime stop')
        if url.endswith('/runtime/internal/start'):
            calls['runtime_start'] += 1
            raise AssertionError('startup grace should skip runtime restart')
        if url.endswith('/session/internal/start'):
            calls['session_start'] += 1
            raise AssertionError('startup grace should skip session start')
        if url.endswith('/group-state'):
            calls['group_state'].append({'url': url, 'payload': payload})
            raise AssertionError('shared worker fetch should not run during startup grace')
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('fresh probe must not run during startup grace')))

    cycle = run_cycle(args, {})

    registration_cycle = cycle['registration_group_cycles'][0]
    assert registration_cycle['worker_state']['ok'] is False
    assert registration_cycle['worker_state']['error'] == 'worker_base_url_missing_for_selected_binding'
    assert registration_cycle['worker_state']['startup_grace']['active'] is True
    assert registration_cycle['worker_state']['startup_grace']['reason'] == 'runtime_startup_grace_skip_auto_rebuild'
    assert calls['runtime_stop'] == 0
    assert calls['runtime_start'] == 0
    assert calls['session_start'] == 0
    assert calls['group_state'] == []



def test_run_cycle_outside_schedule_does_not_fallback_to_stale_shared_worker(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    invite_link = 'https://chat.whatsapp.com/EoHAaKPML7p3BG7LNEbOl1'
    calls = {'group_state': []}

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
                            'base_url': 'http://127.0.0.1:60057',
                        },
                        'group_link_bindings': [
                            {
                                'link': invite_link,
                                'group_name': '注册01',
                                'enabled': True,
                                'area': 'Indonesia',
                                'schedule_runtime': {'configured': True, 'active_now': False},
                            }
                        ],
                    }
                ]
            }
        if url.endswith('/group-state'):
            calls['group_state'].append({'url': url, 'payload': payload})
            raise AssertionError('shared worker must stay idle when the only configured binding is outside schedule')
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('fresh probe must not run outside configured schedule')))

    cycle = run_cycle(args, {})

    assert cycle['registration_group'] == invite_link
    assert cycle['monitor_target']['source'] == 'account_binding'
    assert cycle['monitor_targets']['selection_reason'] == 'configured_binding_outside_schedule'
    assert cycle['monitor_targets']['allow_fallback'] is False
    assert cycle['monitor_targets']['active_count'] == 0
    assert cycle['registration_group_cycles'] == []
    assert 'worker_state' not in cycle
    assert calls['group_state'] == []



def test_run_cycle_outside_schedule_flushes_pending_batch_once_at_window_end(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.approval_poll_interval_seconds = 20
    invite_link = 'https://chat.whatsapp.com/EoHAaKPML7p3BG7LNEbOl1'
    calls = {'group_state': [], 'formal': []}

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
                            'base_url': 'http://127.0.0.1:60057',
                        },
                        'group_link_bindings': [
                            {
                                'link': invite_link,
                                'group_name': '注册01',
                                'enabled': True,
                                'area': 'Indonesia',
                                'approval_count_threshold': 30,
                                'approval_timeout_minutes': 20,
                                'schedule_runtime': {'configured': True, 'active_now': False},
                                'schedule_windows': [{'start': '09:00', 'end': '10:00'}],
                            }
                        ],
                    }
                ]
            }
        if url == 'http://127.0.0.1:60057/group-state':
            calls['group_state'].append({'url': url, 'payload': payload})
            return {
                'group_id': '120363422719530134@g.us',
                'group_name': '注册01',
                'pending_count': 3,
                'member_count': 12,
                'requesters': [
                    {'requesterId': 'u1', 'requestedAtUnix': 100},
                    {'requesterId': 'u2', 'requestedAtUnix': 101},
                    {'requesterId': 'u3', 'requestedAtUnix': 102},
                ],
            }
        raise AssertionError(url)

    def fake_run_formal_approval_command(command, timeout):
        calls['formal'].append(command)
        return {
            'returncode': 0,
            'result': {
                'formal_run': {
                    'approval_run_id': 'window-end-run-1',
                    'result': {
                        'verified': True,
                        'crm_recorded': True,
                    },
                },
            },
        }

    end_boundary_now = datetime(2026, 1, 1, 2, 1, 5, tzinfo=timezone.utc)
    monkeypatch.setattr('scripts.production_ops_daemon.utc_now', lambda: end_boundary_now)
    monkeypatch.setattr('scripts.production_ops_daemon.utc_now_iso', lambda: end_boundary_now.isoformat().replace('+00:00', 'Z'))
    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon.run_formal_approval_command', fake_run_formal_approval_command)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('fresh probe should be skipped for dedicated runtime binding target')))

    cycle = run_cycle(args, {})

    assert cycle['registration_group'] == invite_link
    assert cycle['monitor_targets']['selection_reason'] == 'configured_binding_outside_schedule'
    assert len(cycle['registration_group_cycles']) == 1
    assert cycle['formal_approval']['triggered'] is True
    assert cycle['formal_approval']['ok'] is True
    assert cycle['formal_approval']['reason_code'] == 'schedule_window_end_flush'
    assert cycle['formal_approval']['release_count'] == 3
    assert len(calls['group_state']) >= 1
    assert len(calls['formal']) == 1
    assert calls['formal'][0][calls['formal'][0].index('--registration-group') + 1] == invite_link
    assert calls['formal'][0][calls['formal'][0].index('--approved-count') + 1] == '3'



def test_run_cycle_startup_batch_command_uses_selected_binding_runtime_and_target(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-binding-1'
    invite_link = 'https://chat.whatsapp.com/Bp1WKsmpcbC2RkAyIACeRv'
    captured = {}

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
                            'base_url': 'http://127.0.0.1:52681',
                        },
                        'group_link_bindings': [
                            {
                                'link': invite_link,
                                'group_name': '测试85',
                                'enabled': True,
                                'area': 'Indonesia',
                                'schedule_runtime': {'configured': True, 'active_now': True},
                            }
                        ],
                    }
                ]
            }
        if url == 'http://127.0.0.1:52681/group-state':
            return {
                'group_id': 'g-test85',
                'group_name': '测试85',
                'pending_count': 3,
                'member_count': 5,
                'requesters': [
                    {'requesterId': 'u1', 'requestedAtUnix': 100},
                    {'requesterId': 'u2', 'requestedAtUnix': 101},
                    {'requesterId': 'u3', 'requestedAtUnix': 102},
                ],
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: {
        'group_id': 'g-test85',
        'group_name': '测试85',
        'pending_count': 3,
        'member_count': 5,
        'requesters': [
            {'requesterId': 'u1', 'requestedAtUnix': 100},
            {'requesterId': 'u2', 'requestedAtUnix': 101},
            {'requesterId': 'u3', 'requestedAtUnix': 102},
        ],
    })

    def fake_run_formal_approval_command(command, timeout):
        captured['command'] = command
        return {
            'returncode': 0,
            'result': {
                'formal_run': {
                    'approval_run_id': 'startup-binding-1',
                    'result': {
                        'verified': True,
                        'crm_recorded': True,
                    },
                },
            },
        }

    monkeypatch.setattr('scripts.production_ops_daemon.run_formal_approval_command', fake_run_formal_approval_command)

    cycle = run_cycle(args, {})

    assert cycle['startup_initial_batch']['triggered'] is True
    assert captured['command'][captured['command'].index('--worker-base-url') + 1] == 'http://127.0.0.1:52681'
    assert captured['command'][captured['command'].index('--registration-group') + 1] == invite_link
    assert captured['command'][captured['command'].index('--approved-count') + 1] == '3'


def test_run_cycle_selected_binding_passes_custom_thresholds_to_release_evaluation(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    invite_link = 'https://chat.whatsapp.com/EoHAaKPML7p3BG7LNEbOl1'
    evaluate_payloads = []

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
                            'base_url': 'http://127.0.0.1:60057',
                        },
                        'group_link_bindings': [
                            {
                                'link': invite_link,
                                'group_name': '注册测试1',
                                'enabled': True,
                                'area': 'Indonesia',
                                'approval_count_threshold': 30,
                                'approval_timeout_minutes': 5,
                                'schedule_runtime': {'configured': True, 'active_now': True},
                            }
                        ],
                    }
                ]
            }
        if url == 'http://127.0.0.1:60057/group-state':
            return {
                'group_id': '120363422719530134@g.us',
                'group_name': '注册测试1',
                'pending_count': 2,
                'member_count': 3,
                'requesters': [
                    {'requesterId': 'u1', 'requestedAtUnix': 100},
                    {'requesterId': 'u2', 'requestedAtUnix': 101},
                ],
            }
        if url.endswith('/api/ops/approval-batches/evaluate'):
            evaluate_payloads.append(payload)
            return {
                'approval_type': 'registration_group',
                'registration_group': invite_link,
                'pending_count': 2,
                'oldest_pending_at': payload['oldest_pending_at'],
                'ready': False,
                'release_count': 0,
                'reason_code': 'waiting_for_batch',
                'batch_size': payload.get('batch_size') or 30,
                'timeout_minutes': payload.get('timeout_minutes') or 30,
                'elapsed_minutes': 0,
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('fresh probe should be skipped for dedicated runtime binding target')))

    cycle = run_cycle(args, {})

    assert cycle['monitor_target']['group_name'] == '注册测试1'
    assert len(evaluate_payloads) == 1
    assert evaluate_payloads[0]['batch_size'] == 30
    assert evaluate_payloads[0]['timeout_minutes'] == 5
    assert cycle['release_evaluation']['payload']['timeout_minutes'] == 5



def test_run_cycle_startup_recheck_uses_worker_state_for_dedicated_binding(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-binding-recheck'
    invite_link = 'https://chat.whatsapp.com/Bp1WKsmpcbC2RkAyIACeRv'
    calls = {'group_state': 0, 'fresh_probe_called': False}

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/api/ops/whatsapp-approval-accounts/registration-runtime-directory'):
            return {
                'rows': [{
                    'account_key': 'wa-admin-demo-1',
                    'account_name': 'WA Admin',
                    'responsible_type': 'registration_group',
                    'enabled': True,
                    'area': 'Indonesia',
                    'runtime_state': {'active': True, 'base_url': 'http://127.0.0.1:52681'},
                    'group_link_bindings': [{
                        'link': invite_link,
                        'group_name': '测试85',
                        'enabled': True,
                        'area': 'Indonesia',
                        'schedule_runtime': {'configured': True, 'active_now': True},
                    }],
                }]
            }
        if url == 'http://127.0.0.1:52681/group-state':
            calls['group_state'] += 1
            if calls['group_state'] == 1:
                return {
                    'group_id': 'g-test85',
                    'group_name': '测试85',
                    'pending_count': 3,
                    'member_count': 5,
                    'requesters': [
                        {'requesterId': 'u1', 'requestedAtUnix': 100},
                        {'requesterId': 'u2', 'requestedAtUnix': 101},
                        {'requesterId': 'u3', 'requestedAtUnix': 102},
                    ],
                }
            return {
                'group_id': 'g-test85',
                'group_name': '测试85',
                'pending_count': 0,
                'member_count': 5,
                'requesters': [],
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon.run_formal_approval_command', lambda *args, **kwargs: {
        'returncode': 99,
        'result': {'formal_run': {'result': {'verified': False, 'crm_recorded': False}}},
    })

    def fake_run_fresh_probe(*args, **kwargs):
        calls['fresh_probe_called'] = True
        raise AssertionError('fresh probe should not be used in dedicated binding startup recheck')

    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', fake_run_fresh_probe)

    cycle = run_cycle(args, {})

    assert cycle['startup_initial_batch']['cleared_after_recheck'] is True
    assert cycle['startup_initial_batch']['attempt_results'][0]['recheck_source'] == 'worker_state'
    assert len(cycle['startup_initial_batch']['startup_probe_rechecks']) == 2
    assert [item['pending_count'] for item in cycle['startup_initial_batch']['startup_probe_rechecks']] == [0, 0]
    assert calls['group_state'] == 4
    assert calls['fresh_probe_called'] is False


def test_run_cycle_fresh_probe_failure_fails_closed(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', lambda *args, **kwargs: {'group_id': 'g', 'group_name': 'RG', 'pending_count': 10, 'member_count': 339, 'requesters': [{'requesterId': 'old1', 'requestedAtUnix': 100}]})
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('fresh probe unavailable')))

    cycle = run_cycle(args, {})
    assert cycle['worker_state']['ok'] is True
    assert cycle['fresh_probe']['ok'] is False
    assert cycle['fresh_probe']['skipped'] is True
    assert cycle['fresh_probe']['reason'] == 'group_state_is_authoritative_source'
    assert cycle['decision_group_state']['source'] == 'group_state'
    assert cycle['formal_approval']['triggered'] is False
    assert cycle['formal_approval']['ready'] is False



def test_registration_group_cycle_retries_release_evaluation_after_backend_refusal(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.restart_wait_seconds = 0.0
    target = {
        'registration_group': 'RG',
        'group_name': '测试群',
        'worker_base_url': 'http://worker-1',
        'source': 'fallback_config',
        'approval_count_threshold': 30,
        'approval_timeout_minutes': 5,
        'runtime_state': {'active': True, 'ready': True, 'authenticated': True},
        'session_state': {'session_target_match': True, 'login_verified': True},
    }
    evaluate_attempts = {'n': 0}
    restart_calls = []
    health_checks = []

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url == 'http://worker-1/group-state':
            return {
                'group_id': 'g',
                'group_name': '测试群',
                'pending_count': 2,
                'member_count': 5,
                'requesters': [
                    {'requesterId': 'u1', 'requestedAtUnix': 100},
                    {'requesterId': 'u2', 'requestedAtUnix': 101},
                ],
            }
        if url.endswith('/api/ops/approval-batches/evaluate'):
            evaluate_attempts['n'] += 1
            if evaluate_attempts['n'] == 1:
                raise RuntimeError('<urlopen error [Errno 61] Connection refused>')
            return {
                'approval_type': 'registration_group',
                'registration_group': 'RG',
                'pending_count': 2,
                'oldest_pending_at': payload['oldest_pending_at'],
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

    def fake_check_backend_health(api_base_url, *, timeout):
        health_checks.append((api_base_url, timeout))
        return {'ok': True, 'payload': {'status': 'ok'}}

    def fake_restart(command, *, timeout):
        restart_calls.append((command, timeout))
        return {'attempted': True, 'ok': True, 'command': command}

    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', fake_check_backend_health)
    monkeypatch.setattr('scripts.production_ops_daemon.maybe_restart', fake_restart)
    monkeypatch.setattr('scripts.production_ops_daemon.time.sleep', lambda seconds: None)

    cycle = _run_registration_group_cycle(args, {}, target, now=datetime(2026, 5, 9, 8, 31, 6, tzinfo=timezone.utc))

    assert cycle['worker_state']['ok'] is True
    assert cycle['release_evaluation']['ok'] is True
    assert cycle['release_evaluation']['payload']['reason_code'] == 'waiting_for_batch'
    assert cycle['release_evaluation']['recovered_after_retry'] is True
    assert evaluate_attempts['n'] == 2
    assert restart_calls == []
    assert len(health_checks) == 1


def test_run_cycle_dispatches_ready_official_group_batches(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    calls = []

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        calls.append({'url': url, 'method': method, 'payload': payload})
        if url.endswith('/api/ops/whatsapp-approval-accounts/registration-runtime-directory'):
            return {'rows': []}
        if url.endswith('/group-state'):
            return {'group_id': 'g', 'group_name': 'RG', 'pending_count': 0, 'member_count': 339, 'requesters': []}
        if url.endswith('/api/ops/approval-batches/evaluate'):
            return {
                'approval_type': 'registration_group',
                'registration_group': 'RG',
                'pending_count': 0,
                'oldest_pending_at': None,
                'ready': False,
                'release_count': 0,
                'reason_code': 'waiting_for_batch',
                'batch_size': 30,
                'timeout_minutes': 30,
                'elapsed_minutes': 0,
            }
        if url.endswith('/api/ops/approval-batch-queue'):
            return {
                'registration_groups': [],
                'official_groups': [
                    {
                        'approval_type': 'official_group',
                        'registration_group': 'Piso-5',
                        'pending_count': 6,
                        'oldest_pending_at': '2026-04-16T04:36:47.469921+00:00',
                        'ready': True,
                        'release_count': 6,
                        'reason_code': 'timeout_flush',
                        'batch_size': 10,
                        'timeout_minutes': 30,
                        'elapsed_minutes': 18807,
                    }
                ],
            }
        if url.endswith('/api/ops/official-group-approval-executor-health'):
            return {'configured': True, 'status': 'healthy', 'provider': 'webhook', 'supports': ['approve']}
        if url.endswith('/api/ops/official-group-approval-batches/run-ready'):
            return {
                'executed': True,
                'ready_group_count': 1,
                'executed_count': 1,
                'skipped_count': 0,
                'unresolved_count': 0,
                'results': [{'lead_id': 'lead_1', 'target_group': 'official-group-piso', 'executed': True}],
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: {'group_id': 'g', 'group_name': 'RG', 'pending_count': 0, 'member_count': 339, 'requesters': []})

    cycle = run_cycle(args, {})

    official_calls = [call for call in calls if call['url'].endswith('/api/ops/official-group-approval-batches/run-ready')]
    assert len(official_calls) == 1
    assert official_calls[0]['method'] == 'POST'
    assert official_calls[0]['payload']['decided_by'] == 'Hermes'
    assert cycle['official_group_dispatch']['triggered'] is True
    assert cycle['official_group_dispatch']['ok'] is True
    assert cycle['official_group_dispatch']['result']['executed_count'] == 1


def test_run_cycle_official_group_dispatch_uses_binding_timeout_for_cooldown(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    calls = []

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        calls.append({'url': url, 'method': method, 'payload': payload})
        if url.endswith('/api/ops/whatsapp-approval-accounts/registration-runtime-directory'):
            return {'rows': []}
        if url.endswith('/group-state'):
            return {'group_id': 'g', 'group_name': 'RG', 'pending_count': 0, 'member_count': 339, 'requesters': []}
        if url.endswith('/api/ops/approval-batches/evaluate'):
            return {
                'approval_type': 'registration_group',
                'registration_group': 'RG',
                'pending_count': 0,
                'oldest_pending_at': None,
                'ready': False,
                'release_count': 0,
                'reason_code': 'waiting_for_batch',
                'batch_size': 30,
                'timeout_minutes': 30,
                'elapsed_minutes': 0,
            }
        if url.endswith('/api/ops/approval-batch-queue'):
            return {
                'registration_groups': [],
                'official_groups': [
                    {
                        'approval_type': 'official_group',
                        'registration_group': '官方测试1',
                        'target_group': 'official-group-permata',
                        'pending_count': 2,
                        'oldest_pending_at': '2026-05-06T05:46:46.000Z',
                        'ready': True,
                        'release_count': 2,
                        'reason_code': 'timeout_flush',
                        'batch_size': 10,
                        'timeout_minutes': 10,
                        'elapsed_minutes': 76,
                    }
                ],
            }
        if url.endswith('/api/ops/official-group-approval-executor-health'):
            return {'configured': True, 'status': 'healthy', 'provider': 'webhook', 'supports': ['approve']}
        if url.endswith('/api/ops/official-group-approval-batches/run-ready'):
            return {
                'executed': True,
                'ready_group_count': 1,
                'executed_count': 2,
                'skipped_count': 0,
                'unresolved_count': 0,
                'results': [
                    {'lead_id': 'lead_1', 'target_group': 'official-group-permata', 'executed': True},
                    {'lead_id': 'lead_2', 'target_group': 'official-group-permata', 'executed': True},
                ],
            }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: {'group_id': 'g', 'group_name': 'RG', 'pending_count': 0, 'member_count': 339, 'requesters': []})

    state = {}
    first_cycle = run_cycle(args, state)
    second_cycle = run_cycle(args, state)

    first_dispatch = first_cycle['official_group_dispatch']
    second_dispatch = second_cycle['official_group_dispatch']
    assert first_dispatch['triggered'] is True
    assert first_dispatch['trigger_cooldown_seconds'] == 600
    assert first_dispatch['result']['executed_count'] == 2
    assert second_dispatch['triggered'] is False
    assert second_dispatch['trigger_cooldown_seconds'] == 600
    assert second_dispatch['cooldown_skip'] is True
    official_calls = [call for call in calls if call['url'].endswith('/api/ops/official-group-approval-batches/run-ready')]
    assert len(official_calls) == 1


def test_run_cycle_adding_new_binding_starts_monitoring_only_for_new_group(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-multi-add'
    group_a = 'https://chat.whatsapp.com/group-a'
    group_b = 'https://chat.whatsapp.com/group-b'
    call_state = {'accounts_calls': 0, 'approval_commands': []}

    def accounts_payload(include_group_b: bool):
        bindings = [
            {
                'link': group_a,
                'group_name': 'Group A',
                'enabled': True,
                'area': 'Indonesia',
                'approval_count_threshold': 30,
                'approval_timeout_minutes': 30,
                'schedule_runtime': {'configured': True, 'active_now': True},
            },
        ]
        if include_group_b:
            bindings.append({
                'link': group_b,
                'group_name': 'Group B',
                'enabled': True,
                'area': 'Indonesia',
                'approval_count_threshold': 30,
                'approval_timeout_minutes': 30,
                'schedule_runtime': {'configured': True, 'active_now': True},
            })
        return {
            'rows': [{
                'account_key': 'wa-admin-demo-1',
                'account_name': 'WA Admin',
                'responsible_type': 'registration_group',
                'enabled': True,
                'area': 'Indonesia',
                'runtime_state': {'active': True, 'base_url': 'http://127.0.0.1:52681'},
                'group_link_bindings': bindings,
            }]
        }

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/api/ops/whatsapp-approval-accounts/registration-runtime-directory'):
            call_state['accounts_calls'] += 1
            return accounts_payload(include_group_b=call_state['accounts_calls'] >= 2)
        if url == 'http://127.0.0.1:52681/group-state':
            registration_group = payload['registration_group']
            if registration_group == group_a:
                return {
                    'group_id': 'ga',
                    'group_name': 'Group A',
                    'pending_count': 0,
                    'member_count': 10,
                    'requesters': [],
                }
            if registration_group == group_b:
                return {
                    'group_id': 'gb',
                    'group_name': 'Group B',
                    'pending_count': 3,
                    'member_count': 12,
                    'requesters': [
                        {'requesterId': 'u1', 'requestedAtUnix': 100},
                        {'requesterId': 'u2', 'requestedAtUnix': 101},
                        {'requesterId': 'u3', 'requestedAtUnix': 102},
                    ],
                }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('fresh probe should be skipped for dedicated runtime binding target')))

    def fake_run_formal_approval_command(command, timeout):
        call_state['approval_commands'].append(command)
        return {
            'returncode': 0,
            'result': {
                'formal_run': {
                    'approval_run_id': f"run-{len(call_state['approval_commands'])}",
                    'result': {'verified': True, 'crm_recorded': True},
                },
            },
        }

    monkeypatch.setattr('scripts.production_ops_daemon.run_formal_approval_command', fake_run_formal_approval_command)

    state = {}
    first_cycle = run_cycle(args, state)
    assert len(first_cycle['registration_group_cycles']) == 1

    second_cycle = run_cycle(args, state)

    assert len(second_cycle['registration_group_cycles']) == 2
    by_group = {item['registration_group']: item for item in second_cycle['registration_group_cycles']}
    assert by_group[group_a]['startup_initial_batch'].get('triggered') is not True
    assert by_group[group_b]['startup_initial_batch']['triggered'] is True
    assert by_group[group_b]['startup_initial_batch']['pending_count'] == 3
    assert len(call_state['approval_commands']) == 1
    assert call_state['approval_commands'][0][call_state['approval_commands'][0].index('--registration-group') + 1] == group_b


def test_run_cycle_binding_config_change_restarts_only_changed_group_session(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-multi-update'
    group_a = 'https://chat.whatsapp.com/group-a'
    group_b = 'https://chat.whatsapp.com/group-b'
    call_state = {'accounts_calls': 0, 'approval_commands': []}

    def accounts_payload(group_b_threshold: int):
        return {
            'rows': [{
                'account_key': 'wa-admin-demo-1',
                'account_name': 'WA Admin',
                'responsible_type': 'registration_group',
                'enabled': True,
                'area': 'Indonesia',
                'runtime_state': {'active': True, 'base_url': 'http://127.0.0.1:52681'},
                'group_link_bindings': [
                    {
                        'link': group_a,
                        'group_name': 'Group A',
                        'enabled': True,
                        'area': 'Indonesia',
                        'approval_count_threshold': 30,
                        'approval_timeout_minutes': 30,
                        'schedule_runtime': {'configured': True, 'active_now': True},
                    },
                    {
                        'link': group_b,
                        'group_name': 'Group B',
                        'enabled': True,
                        'area': 'Indonesia',
                        'approval_count_threshold': group_b_threshold,
                        'approval_timeout_minutes': 30,
                        'schedule_runtime': {'configured': True, 'active_now': True},
                    },
                ],
            }]
        }

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/api/ops/whatsapp-approval-accounts/registration-runtime-directory'):
            call_state['accounts_calls'] += 1
            return accounts_payload(group_b_threshold=30 if call_state['accounts_calls'] == 1 else 10)
        if url == 'http://127.0.0.1:52681/group-state':
            registration_group = payload['registration_group']
            if registration_group == group_a:
                return {
                    'group_id': 'ga',
                    'group_name': 'Group A',
                    'pending_count': 0,
                    'member_count': 10,
                    'requesters': [],
                }
            if registration_group == group_b:
                pending_count = 0 if call_state['accounts_calls'] == 1 else 2
                requesters = [] if pending_count == 0 else [
                    {'requesterId': 'u1', 'requestedAtUnix': 100},
                    {'requesterId': 'u2', 'requestedAtUnix': 101},
                ]
                return {
                    'group_id': 'gb',
                    'group_name': 'Group B',
                    'pending_count': pending_count,
                    'member_count': 12,
                    'requesters': requesters,
                }
        raise AssertionError(url)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', fake_fetch_json)
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('fresh probe should be skipped for dedicated runtime binding target')))

    def fake_run_formal_approval_command(command, timeout):
        call_state['approval_commands'].append(command)
        return {
            'returncode': 0,
            'result': {
                'formal_run': {
                    'approval_run_id': f"run-{len(call_state['approval_commands'])}",
                    'result': {'verified': True, 'crm_recorded': True},
                },
            },
        }

    monkeypatch.setattr('scripts.production_ops_daemon.run_formal_approval_command', fake_run_formal_approval_command)

    state = {}
    first_cycle = run_cycle(args, state)
    assert len(first_cycle['registration_group_cycles']) == 2

    second_cycle = run_cycle(args, state)

    by_group = {item['registration_group']: item for item in second_cycle['registration_group_cycles']}
    assert by_group[group_a]['startup_initial_batch'].get('triggered') is not True
    assert by_group[group_b]['startup_initial_batch']['triggered'] is True
    assert by_group[group_b]['startup_initial_batch']['pending_count'] == 2
    assert len(call_state['approval_commands']) == 1
    assert call_state['approval_commands'][0][call_state['approval_commands'][0].index('--registration-group') + 1] == group_b
