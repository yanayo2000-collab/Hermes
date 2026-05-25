from fastapi.testclient import TestClient

from app.main import create_app


def make_client(settings=None):
    cfg = {"DB_PATH": ":memory:", "AUTO_LARK_REPLY": False}
    if settings:
        cfg.update(settings)
    return TestClient(create_app(cfg))


def test_group_atmosphere_config_upsert_and_list():
    client = make_client()

    response = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'indo-reg-01',
        'enabled': True,
        'account_key': 'wa-seed-01',
        'target_group': '120363000000000000@g.us',
        'group_name': 'ID Registration Group 01',
        'language': 'id',
        'timezone': 'Asia/Jakarta',
        'worker_base_url': 'http://127.0.0.1:9010',
        'daily_max_messages': 4,
        'min_interval_minutes': 60,
        'template_pool': [
            {'template_id': 'welcome-1', 'category': 'newcomer', 'text': 'Selamat datang. Siapkan ID dan kode undangan ya.'}
        ],
        'mention_reply_enabled': True,
        'faq_rules': [
            {'keyword': 'daftar', 'reply': 'Silakan kirim Phone / ID / Group / Code ke admin.'}
        ],
    })

    assert response.status_code == 200
    body = response.json()
    assert body['ok'] is True
    assert body['config']['config_name'] == 'indo-reg-01'
    assert body['config']['status'] == 'enabled'
    assert body['config']['template_count'] == 1

    rows = client.get('/api/ops/group-atmosphere/configs').json()['rows']
    assert len(rows) == 1
    assert rows[0]['account_key'] == 'wa-seed-01'
    assert rows[0]['target_group'] == '120363000000000000@g.us'


def test_group_atmosphere_dispatch_once_uses_worker_and_logs(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        text = '{"status":"success"}'
        def json(self):
            return {'status': 'success', 'message_id': 'msg-1', 'group_name': 'ID Group'}

    def fake_post(url, json, timeout):
        calls.append({'url': url, 'json': json, 'timeout': timeout})
        return FakeResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)
    client = make_client()
    client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'indo-reg-01',
        'enabled': True,
        'account_key': 'wa-seed-01',
        'target_group': '120363000000000000@g.us',
        'worker_base_url': 'http://127.0.0.1:9010',
        'template_pool': [{'template_id': 't1', 'text': 'Hari ini jangan lupa cek profil.'}],
    })

    response = client.post('/api/ops/group-atmosphere/dispatch-once', json={
        'config_name': 'indo-reg-01',
        'trigger_type': 'manual_test',
    })

    assert response.status_code == 200
    body = response.json()
    assert body['sent'] is True
    assert body['message_text'] == 'Hari ini jangan lupa cek profil.'
    assert calls == [{
        'url': 'http://127.0.0.1:9010/send-group-message',
        'json': {
            'target_group': '120363000000000000@g.us',
            'message_text': 'Hari ini jangan lupa cek profil.',
            'metadata': {'config_name': 'indo-reg-01', 'trigger_type': 'manual_test'},
        },
        'timeout': 30,
    }]
    logs = client.get('/api/ops/group-atmosphere/logs').json()['rows']
    assert logs[0]['config_name'] == 'indo-reg-01'
    assert logs[0]['direction'] == 'outbound'
    assert logs[0]['status'] == 'success'


def test_group_atmosphere_dispatch_respects_rate_limit():
    client = make_client()
    client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'indo-reg-01',
        'enabled': True,
        'account_key': 'wa-seed-01',
        'target_group': 'group-a@g.us',
        'worker_base_url': '',
        'daily_max_messages': 1,
        'min_interval_minutes': 60,
        'template_pool': [{'template_id': 't1', 'text': 'First message'}],
    })

    first = client.post('/api/ops/group-atmosphere/dispatch-once', json={'config_name': 'indo-reg-01'})
    assert first.status_code == 200
    assert first.json()['sent'] is False
    assert first.json()['dry_run'] is True
    assert first.json()['result_code'] == 'dry_run'

    second = client.post('/api/ops/group-atmosphere/dispatch-once', json={'config_name': 'indo-reg-01'})
    assert second.status_code == 200
    assert second.json()['sent'] is False
    assert second.json()['result_code'] == 'dry_run'


def test_group_atmosphere_mention_reply_only_when_mentioned():
    client = make_client()
    client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'indo-reg-01',
        'enabled': True,
        'account_key': 'wa-seed-01',
        'target_group': 'group-a@g.us',
        'mention_reply_enabled': True,
        'faq_rules': [{'keyword': 'code', 'reply': 'Send your 6-character personal invitation code to admin.'}],
    })

    ignored = client.post('/api/ops/group-atmosphere/inbound-message', json={
        'account_key': 'wa-seed-01',
        'target_group': 'group-a@g.us',
        'sender_id': 'user-1',
        'text': 'what is code?',
        'mentioned': False,
    })
    assert ignored.status_code == 200
    assert ignored.json()['should_respond'] is False
    assert ignored.json()['result_code'] == 'not_mentioned'

    mentioned = client.post('/api/ops/group-atmosphere/inbound-message', json={
        'account_key': 'wa-seed-01',
        'target_group': 'group-a@g.us',
        'sender_id': 'user-1',
        'text': '@bot what is code?',
        'mentioned': True,
    })
    assert mentioned.status_code == 200
    assert mentioned.json()['should_respond'] is True
    assert mentioned.json()['reply_text'] == 'Send your 6-character personal invitation code to admin.'

def test_group_atmosphere_dispatch_does_not_count_worker_success_without_message_id(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = '{"status":"success","result_code":"sent"}'

        def json(self):
            return {'status': 'success', 'result_code': 'sent'}

    monkeypatch.setattr('app.main.requests.post', lambda url, json, timeout: FakeResponse())
    client = make_client()
    client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'indo-reg-01',
        'enabled': True,
        'account_key': 'wa-seed-01',
        'target_group': 'group-a@g.us',
        'worker_base_url': 'http://127.0.0.1:9010',
        'daily_max_messages': 1,
        'min_interval_minutes': 60,
        'template_pool': [{'template_id': 't1', 'text': 'First message'}],
    })

    first = client.post('/api/ops/group-atmosphere/dispatch-once', json={'config_name': 'indo-reg-01'})
    assert first.status_code == 200
    assert first.json()['result_code'] == 'sent'
    assert first.json()['sent'] is False

    second = client.post('/api/ops/group-atmosphere/dispatch-once', json={'config_name': 'indo-reg-01'})
    assert second.status_code == 200
    assert second.json()['result_code'] == 'sent'
    assert second.json()['sent'] is False

