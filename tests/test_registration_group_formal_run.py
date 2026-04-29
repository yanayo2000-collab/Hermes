from app.registration_group_formal_run import execute_formal_registration_group_approval
from scripts.run_registration_group_formal_approval import _ensure_backend_healthy


class StubTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, *, method='GET', payload=None, timeout=30.0):
        self.calls.append({
            'url': url,
            'method': method,
            'payload': payload,
            'timeout': timeout,
        })
        if not self.responses:
            raise AssertionError('unexpected extra transport call')
        return self.responses.pop(0)


def test_execute_formal_registration_group_approval_posts_then_polls_until_done():
    transport = StubTransport([
        {
            'accepted': True,
            'queued': True,
            'approval_run_id': 'registration_group_approval_test_1',
        },
        {
            'status': 'processing',
            'result': None,
        },
        {
            'status': 'done',
            'result': {
                'status': 'success',
                'result_code': 'approved',
                'crm_recorded': True,
            },
        },
    ])

    result = execute_formal_registration_group_approval(
        api_base_url='http://127.0.0.1:8011',
        registration_group='8️⃣5️⃣',
        area='Indonesia',
        remark='formal test',
        fetch_json=transport,
        sleep_fn=lambda _seconds: None,
        decided_by='Hermes',
        decided_by_name='Song Yuqi',
        approved_count=1,
        expected_group_state={
            'pending_count': 2,
            'member_count': 4,
            'requester_ids': ['aaa@lid', 'bbb@lid'],
            'requesters': [
                {'requesterId': 'aaa@lid', 'requestedAtUnix': 100},
                {'requesterId': 'bbb@lid', 'requestedAtUnix': 200},
            ],
        },
        poll_interval_seconds=0.01,
        poll_timeout_seconds=5.0,
    )

    assert result['accepted_response']['approval_run_id'] == 'registration_group_approval_test_1'
    assert result['final_status']['status'] == 'done'
    assert result['final_status']['result']['result_code'] == 'approved'
    assert transport.calls[0]['method'] == 'POST'
    assert transport.calls[0]['url'] == 'http://127.0.0.1:8011/api/registration-groups/approval-decisions'
    assert transport.calls[0]['payload']['registration_group'] == '8️⃣5️⃣'
    assert transport.calls[0]['payload']['expected_pending_count'] == 2
    assert transport.calls[0]['payload']['expected_member_count'] == 4
    assert transport.calls[0]['payload']['expected_requester_ids'] == ['aaa@lid', 'bbb@lid']
    assert transport.calls[0]['payload']['expected_requesters'][0]['requesterId'] == 'aaa@lid'
    assert transport.calls[1]['url'].endswith('/api/registration-groups/approval-decisions/registration_group_approval_test_1')


def test_execute_formal_registration_group_approval_raises_on_poll_timeout():
    transport = StubTransport([
        {
            'accepted': True,
            'queued': True,
            'approval_run_id': 'registration_group_approval_test_2',
        },
        {
            'status': 'processing',
            'result': None,
        },
        {
            'status': 'processing',
            'result': None,
        },
    ])

    try:
        execute_formal_registration_group_approval(
            api_base_url='http://127.0.0.1:8011',
            registration_group='8️⃣5️⃣',
            area='Indonesia',
            remark='formal test timeout',
            fetch_json=transport,
            sleep_fn=lambda _seconds: None,
            now_fn=iter([0.0, 1.2]).__next__,
            poll_interval_seconds=0.01,
            poll_timeout_seconds=1.0,
        )
    except TimeoutError as exc:
        assert 'registration_group_approval_test_2' in str(exc)
    else:
        raise AssertionError('expected TimeoutError')


def test_ensure_backend_healthy_restarts_when_initial_health_fails(monkeypatch):
    calls = []

    def fake_fetch(url, *, timeout=30.0, **_kwargs):
        calls.append({'url': url, 'timeout': timeout})
        if len(calls) == 1:
            raise RuntimeError('connection refused')
        return {'status': 'ok'}

    class Result:
        returncode = 0
        stdout = 'started'
        stderr = ''

    monkeypatch.setattr('scripts.run_registration_group_formal_approval.subprocess.run', lambda *args, **kwargs: Result())

    result = _ensure_backend_healthy(
        api_base_url='http://127.0.0.1:8011',
        fetch_json=fake_fetch,
        restart_cmd='./scripts/ensure_registration_group_backend.sh',
        sleep_fn=lambda _seconds: None,
        restart_wait_seconds=1.0,
    )

    assert result['ok'] is True
    assert result['restarted'] is True
    assert result['health'] == {'status': 'ok'}
    assert result['attempts'][0]['stage'] == 'initial'
    assert result['attempts'][0]['ok'] is False
    assert result['attempts'][1]['stage'] == 'after_restart'
    assert result['attempts'][1]['ok'] is True


def test_ensure_backend_healthy_returns_failure_when_restart_cmd_missing():
    def fake_fetch(_url, **_kwargs):
        raise RuntimeError('connection refused')

    result = _ensure_backend_healthy(
        api_base_url='http://127.0.0.1:8011',
        fetch_json=fake_fetch,
        restart_cmd=None,
        sleep_fn=lambda _seconds: None,
    )

    assert result['ok'] is False
    assert result['restarted'] is False
    assert result['reason'] == 'backend_unhealthy_and_restart_cmd_missing'
