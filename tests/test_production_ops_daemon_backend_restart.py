from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.production_ops import build_success_notifications
from scripts.production_ops_daemon import _notify_incidents, _run_registration_group_cycle, run_cycle


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


def test_run_cycle_backend_recovery_does_not_raise_after_restart(monkeypatch):
    calls = {'n': 0}

    def fake_check_backend_health(api_base_url, *, timeout):
        calls['n'] += 1
        if calls['n'] == 1:
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
    assert cycle['fresh_probe']['ok'] is True


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
        if url == 'http://127.0.0.1:8011/api/ops/whatsapp-approval-accounts/wa-admin-demo-1/runtime/start':
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
    assert cycle['monitor_target']['worker_base_url'] == 'http://127.0.0.1:62000'
    assert [entry[0] for entry in calls] == [
        'http://127.0.0.1:61150/group-state',
        'http://127.0.0.1:8011/api/ops/whatsapp-approval-accounts/wa-admin-demo-1/runtime/start',
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
    assert cycle['startup_initial_batch']['ok'] is True
    assert cycle['startup_initial_batch']['cleared_after_recheck'] is True
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

    assert len(first) == 1
    assert first[0]['status'] == 'sent'
    assert len(sent_texts) == 1
    assert 'INFO formal_approval_succeeded' in sent_texts[0]
    assert second == []



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
    assert 'INFO startup_initial_batch_succeeded' in sent_texts[0]
    assert '启动首批审批成功' in sent_texts[0]
    assert '通过人数: 2' in sent_texts[0]
    assert second == []



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
            assert payload['pending_count'] == 6
            return {
                'approval_type': 'registration_group',
                'registration_group': 'RG',
                'pending_count': 6,
                'oldest_pending_at': payload['oldest_pending_at'],
                'ready': True,
                'release_count': 6,
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
    assert cycle['decision_group_state']['source'] == 'fresh_probe'
    assert cycle['decision_group_state']['mismatch'] is True
    assert 'pending_count' in cycle['decision_group_state']['mismatch_reasons']
    assert cycle['formal_approval']['release_count'] == 6
    assert captured['command'][captured['command'].index('--approved-count') + 1] == '6'


def test_run_cycle_prefers_active_monitored_binding_target_from_accounts_api(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    invite_link = 'https://chat.whatsapp.com/Bp1WKsmpcbC2RkAyIACeRv'
    calls = {'group_state': [], 'fresh_probe_called': False}

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/api/ops/whatsapp-approval-accounts'):
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
                'pending_count': 0,
                'member_count': 5,
                'requesters': [],
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
    assert cycle['fresh_probe']['ok'] is True
    assert cycle['fresh_probe']['skipped'] is True
    assert cycle['fresh_probe']['reason'] == 'dedicated_runtime_worker_state'
    assert cycle['decision_group_state']['source'] == 'worker_state'
    assert calls['fresh_probe_called'] is False
    assert len(calls['group_state']) == 1


def test_run_cycle_does_not_fallback_to_shared_worker_when_binding_runtime_is_unavailable(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    invite_link = 'https://chat.whatsapp.com/EoHAaKPML7p3BG7LNEbOl1'
    calls = {'group_state': []}

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/api/ops/whatsapp-approval-accounts'):
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



def test_run_cycle_startup_batch_command_uses_selected_binding_runtime_and_target(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-binding-1'
    invite_link = 'https://chat.whatsapp.com/Bp1WKsmpcbC2RkAyIACeRv'
    captured = {}

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/api/ops/whatsapp-approval-accounts'):
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


def test_run_cycle_startup_recheck_uses_worker_state_for_dedicated_binding(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    args.monitoring_session_id = 'session-binding-recheck'
    invite_link = 'https://chat.whatsapp.com/Bp1WKsmpcbC2RkAyIACeRv'
    calls = {'group_state': 0, 'fresh_probe_called': False}

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        if url.endswith('/api/ops/whatsapp-approval-accounts'):
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
    assert calls['group_state'] == 2
    assert calls['fresh_probe_called'] is False


def test_run_cycle_fresh_probe_failure_fails_closed(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)

    monkeypatch.setattr('scripts.production_ops_daemon.check_backend_health', lambda *args, **kwargs: {'ok': True, 'payload': {'status': 'ok'}})
    monkeypatch.setattr('scripts.production_ops_daemon.fetch_json', lambda *args, **kwargs: {'group_id': 'g', 'group_name': 'RG', 'pending_count': 10, 'member_count': 339, 'requesters': [{'requesterId': 'old1', 'requestedAtUnix': 100}]})
    monkeypatch.setattr('scripts.production_ops_daemon._run_fresh_probe', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('fresh probe unavailable')))

    cycle = run_cycle(args, {})
    assert cycle['worker_state']['ok'] is True
    assert cycle['fresh_probe']['ok'] is False
    assert cycle['decision_group_state']['source'] == 'fail_closed'
    assert cycle.get('formal_approval') is None


def test_run_cycle_dispatches_ready_official_group_batches(monkeypatch):
    args = SimpleNamespace(**Args.__dict__)
    calls = []

    def fake_fetch_json(url, *, method='GET', payload=None, timeout=30.0):
        calls.append({'url': url, 'method': method, 'payload': payload})
        if url.endswith('/api/ops/whatsapp-approval-accounts'):
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
        if url.endswith('/api/ops/whatsapp-approval-accounts'):
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
        if url.endswith('/api/ops/whatsapp-approval-accounts'):
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
