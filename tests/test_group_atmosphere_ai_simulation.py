from fastapi.testclient import TestClient

from app.main import create_app


def make_client(settings=None):
    cfg = {"DB_PATH": ":memory:", "AUTO_LARK_REPLY": False}
    if settings:
        cfg.update(settings)
    return TestClient(create_app(cfg))


def seed_config(client):
    response = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'indo-reg-01',
        'enabled': True,
        'account_key': 'wa-seed-01',
        'target_group': 'group-a@g.us',
        'group_name': 'ID Group A',
        'language': 'id',
        'worker_base_url': '',
        'daily_max_messages': 3,
        'min_interval_minutes': 0,
        'template_pool': [
            {'template_id': 'welcome', 'category': 'newcomer', 'text': 'Halo kak, selamat datang. Kalau sudah siap, kirim data ke admin ya.'},
            {'template_id': 'reminder', 'category': 'task', 'text': 'Reminder kak, pastikan ID dan kode undangan sudah benar.'},
        ],
        'mention_reply_enabled': True,
        'faq_rules': [{'keyword': 'kode', 'reply': 'Kode pribadi 6 karakter, kirim ke admin ya kak.'}],
    })
    assert response.status_code == 200


def test_group_atmosphere_auto_learn_upload_routes_records_to_role_profiles():
    client = make_client()

    response = client.post('/api/ops/group-atmosphere/chat-records/auto-learn', json={
        'filename': 'wa-export.txt',
        'content': '\n'.join([
            '[12/05/26, 09.12.33] Admin: Halo kak, kirim ID dan kode ke admin ya.',
            '[12/05/26, 09.13.00] User: kak kode dimana?',
            '[12/05/26, 09.14.00] Admin: Selamat datang kak, semangat mulai pelan-pelan.',
            '[12/05/26, 09.15.00] Admin: Kalau sudah siap, join agency dan lanjut ke grup resmi ya kak.',
        ]),
    })

    assert response.status_code == 200
    body = response.json()
    assert body['ok'] is True
    assert body['detected_language'] == 'id'
    assert body['detected_region'] == '印尼'
    assert body['imported_count'] == 4
    roles = {item['role_positioning']: item for item in body['role_assignments']}
    assert {'newcomer_guide', 'faq_helper', 'community_seed', 'motivation_admin'} <= set(roles)
    assert roles['newcomer_guide']['imported_count'] >= 1
    assert roles['faq_helper']['imported_count'] >= 1
    assert roles['community_seed']['profile']['tone_markers']['uses_kak'] is True
    assert all(candidate['safe_to_send'] is False for item in roles.values() for candidate in item['candidates'])

    configs = client.get('/api/ops/group-atmosphere/configs').json()['rows']
    config_names = {row['config_name'] for row in configs}
    assert 'auto-id-newcomer_guide' in config_names
    assert 'auto-id-faq_helper' in config_names


class FakeSendResponse:
    status_code = 200
    text = '{"status":"sent","message_id":"msg-1"}'

    def json(self):
        return {'status': 'sent', 'message_id': 'msg-1', 'result_code': 'sent'}


class FakeDetachedFrameResponse:
    status_code = 500
    text = '{"status":"failed","result_code":"bridge_internal_error","result_reason":"Attempted to use detached Frame"}'

    def json(self):
        return {
            'status': 'failed',
            'result_code': 'bridge_internal_error',
            'result_reason': "Attempted to use detached Frame '9B01E6EB155369725A1337D31841FE52'.",
        }



def test_group_atmosphere_auto_learn_accepts_multiple_files_and_reuses_local_abbreviations():
    client = make_client()

    response = client.post('/api/ops/group-atmosphere/chat-records/auto-learn', json={
        'files': [
            {
                'filename': 'group-1.txt',
                'content': '[12/05/26, 09.12.33] Admin: Halo kak, jgn lupa krm ID ya.\n[12/05/26, 09.13.00] User: kak gmn caranya?',
            },
            {
                'filename': 'group-2.txt',
                'content': '[12/05/26, 09.14.00] Admin: Yg baru join, ttp semangat ya kak.\n[12/05/26, 09.15.00] User: kode dmn kak?',
            },
        ]
    })

    assert response.status_code == 200
    body = response.json()
    assert body['file_count'] == 2
    assert body['imported_count'] == 4
    roles = {item['role_positioning']: item for item in body['role_assignments']}
    profile = roles['newcomer_guide']['profile']
    assert {'jgn', 'krm', 'gmn'} & set(profile['tone_markers']['local_abbreviations'])
    all_candidates = [candidate['text'].lower() for item in roles.values() for candidate in item['candidates']]
    assert any(any(token in text for token in ['jgn', 'krm', 'gmn', 'dmn', 'yg', 'ttp']) for text in all_candidates)



def test_group_atmosphere_candidate_pool_can_be_enabled_and_run_by_schedule(monkeypatch):
    client = make_client()
    client.post('/api/ops/group-atmosphere/chat-records/auto-learn', json={
        'filename': 'wa-export.txt',
        'content': '\n'.join([
            '[12/05/26, 09.12.33] Admin: Halo kak, kirim ID dan kode ke admin ya.',
            '[12/05/26, 09.13.00] User: kak kode dimana?',
            '[12/05/26, 09.14.00] Admin: Selamat datang kak, semangat mulai pelan-pelan.',
        ]),
    })

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    newcomer = next(item for item in pool if item['config_name'] == 'auto-id-newcomer_guide')
    assert newcomer['enabled_candidate_count'] == 0
    candidate_id = newcomer['candidates'][0]['candidate_id']

    enabled = client.post('/api/ops/group-atmosphere/candidate-pool/enable', json={
        'config_name': 'auto-id-newcomer_guide',
        'candidate_ids': [candidate_id],
        'target_group': '120363400336474261@g.us',
        'group_name': 'ID Group',
        'account_key': 'atmosphere-indo-01',
        'worker_base_url': 'http://worker.local',
        'daily_max_messages': 1,
        'min_interval_minutes': 60,
    })
    assert enabled.status_code == 200
    enabled_body = enabled.json()
    assert enabled_body['enabled_count'] == 1
    assert enabled_body['config']['enabled'] is True
    assert enabled_body['config']['status'] == 'enabled'
    assert enabled_body['config']['template_pool'][0]['safe_to_send'] is True

    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append({'url': url, 'json': json, 'timeout': timeout})
        return FakeSendResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)

    run = client.post('/api/ops/group-atmosphere/scheduler/run-due', json={})
    assert run.status_code == 200
    payload = run.json()
    assert payload['attempted_count'] == 1
    assert payload['sent_count'] == 1
    assert sent[0]['url'] == 'http://worker.local/send-group-message'
    assert sent[0]['json']['target_group'] == '120363400336474261@g.us'
    assert 'Halo kak' in sent[0]['json']['message_text']

    second = client.post('/api/ops/group-atmosphere/scheduler/run-due', json={}).json()
    assert second['sent_count'] == 0
    assert second['results'][0]['result_code'] == 'daily_limit_reached'



def test_group_atmosphere_dispatch_retries_detached_frame_once(monkeypatch):
    client = make_client()
    seed_config(client)
    client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'indo-reg-01',
        'enabled': True,
        'account_key': 'wa-seed-01',
        'target_group': 'group-a@g.us',
        'group_name': 'ID Group A',
        'language': 'id',
        'worker_base_url': 'http://worker.local',
        'daily_max_messages': 3,
        'min_interval_minutes': 0,
        'template_pool': [
            {'template_id': 'welcome', 'category': 'newcomer', 'text': 'Halo kak, selamat datang. Kalau sudah siap, kirim data ke admin ya.'},
        ],
        'mention_reply_enabled': True,
        'faq_rules': [{'keyword': 'kode', 'reply': 'Kode pribadi 6 karakter, kirim ke admin ya kak.'}],
    })
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append({'url': url, 'json': json, 'timeout': timeout})
        if len(calls) == 1:
            return FakeDetachedFrameResponse()
        return FakeSendResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)

    response = client.post('/api/ops/group-atmosphere/dispatch-once', json={
        'config_name': 'indo-reg-01',
        'trigger_type': 'scheduled_auto',
    })

    assert response.status_code == 200
    body = response.json()
    assert body['sent'] is True
    assert body['result_code'] == 'sent'
    assert body['raw_result']['retry_after_recoverable_error'] is True
    assert body['raw_result']['first_error']['result_code'] == 'bridge_internal_error'
    assert len(calls) == 2


def test_group_atmosphere_candidate_pool_can_enable_one_candidate_for_all_enabled_groups(monkeypatch):
    client = make_client()
    client.post('/api/ops/group-atmosphere/chat-records/auto-learn', json={
        'filename': 'wa-export.txt',
        'content': '\n'.join([
            '[12/05/26, 09.12.33] Admin: Halo kak, kirim ID dan kode ke admin ya.',
            '[12/05/26, 09.13.00] User: kak kode dimana?',
        ]),
    })
    client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-indo-01',
        'account_name': '印尼账号01',
        'region': '印尼',
        'role_positioning': 'newcomer_guide',
        'daily_max_messages': 5,
        'min_interval_minutes': 0,
        'groups': [
            {'target_group': 'group-a@g.us', 'group_name': '印尼A群', 'enabled': True},
            {'target_group': 'group-b@g.us', 'group_name': '印尼B群', 'enabled': True},
            {'target_group': 'group-c@g.us', 'group_name': '印尼C群', 'enabled': False},
        ],
        'enabled': True,
    })
    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    newcomer = next(item for item in pool if item['config_name'] == 'auto-id-newcomer_guide')
    candidate_id = newcomer['candidates'][0]['candidate_id']

    enabled = client.post('/api/ops/group-atmosphere/candidate-pool/enable', json={
        'config_name': newcomer['config_name'],
        'candidate_ids': [candidate_id],
        'account_key': 'atmosphere-indo-01',
        'target_group': '__all_enabled_groups__',
        'daily_max_messages': 5,
        'min_interval_minutes': 0,
    })

    assert enabled.status_code == 200
    body = enabled.json()
    assert body['target_group_count'] == 2
    configs = {item['target_group']: item for item in body['configs']}
    assert set(configs) == {'group-a@g.us', 'group-b@g.us'}
    assert all(item['enabled'] is True for item in configs.values())

    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append({'url': url, 'json': json, 'timeout': timeout})
        return FakeSendResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)
    run = client.post('/api/ops/group-atmosphere/scheduler/run-due', json={})

    assert run.status_code == 200
    payload = run.json()
    assert payload['attempted_count'] == 2
    assert payload['sent_count'] == 2
    assert {item['json']['target_group'] for item in sent} == {'group-a@g.us', 'group-b@g.us'}



def test_group_atmosphere_scheduler_ignores_stale_delivery_configs_after_group_plan_switch(monkeypatch):
    client = make_client()
    client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'auto-id-community_seed',
        'enabled': False,
        'account_key': 'auto-id-community_seed',
        'target_group': 'auto-id-community_seed',
        'group_name': '活跃气氛包',
        'language': 'id',
        'status': 'plan_ready',
        'template_pool': [{'template_id': 'seed-1', 'text': 'Halo kak, cek info grup ya.', 'safe_to_send': True, 'enabled': True}],
    })
    client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'auto-id-motivation_admin',
        'enabled': False,
        'account_key': 'auto-id-motivation_admin',
        'target_group': 'auto-id-motivation_admin',
        'group_name': '激励话术包',
        'language': 'id',
        'status': 'plan_ready',
        'template_pool': [{'template_id': 'mot-1', 'text': 'Semangat kak, pelan-pelan pasti bisa.', 'safe_to_send': True, 'enabled': True}],
    })
    client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-indo-01',
        'account_name': '印尼账号01',
        'region': '印尼',
        'role_positioning': 'community_seed',
        'daily_max_messages': 5,
        'min_interval_minutes': 0,
        'groups': [
            {'target_group': 'group-a@g.us', 'group_name': '印尼A群', 'enabled': True, 'speech_plan_config_name': 'auto-id-community_seed'},
            {'target_group': 'group-b@g.us', 'group_name': '印尼B群', 'enabled': True, 'speech_plan_config_name': 'auto-id-motivation_admin'},
        ],
        'enabled': True,
    })
    stale = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'deliver-auto-id-community_seed-atmosphere-indo-01-group-b-2',
        'enabled': True,
        'account_key': 'atmosphere-indo-01',
        'target_group': 'group-b@g.us',
        'group_name': '印尼B群',
        'language': 'id',
        'worker_base_url': 'http://worker.local',
        'daily_max_messages': 5,
        'min_interval_minutes': 0,
        'status': 'enabled',
        'template_pool': [{'template_id': 'stale-seed', 'text': 'STALE should not send', 'safe_to_send': True, 'enabled': True}],
    })
    assert stale.status_code == 200

    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append({'url': url, 'json': json, 'timeout': timeout})
        return FakeSendResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)
    run = client.post('/api/ops/group-atmosphere/scheduler/run-due', json={})

    assert run.status_code == 200
    payload = run.json()
    assert payload['sent_count'] == 2
    assert {(item['json']['target_group'], item['json']['message_text']) for item in sent} == {
        ('group-a@g.us', 'Halo kak, cek info grup ya.'),
        ('group-b@g.us', 'Semangat kak, pelan-pelan pasti bisa.'),
    }
    assert all(item['json']['message_text'] != 'STALE should not send' for item in sent)
    disabled_configs = client.get('/api/ops/group-atmosphere/configs').json()['rows']
    stale_config = next(item for item in disabled_configs if item['config_name'] == 'deliver-auto-id-community_seed-atmosphere-indo-01-group-b-2')
    assert stale_config['enabled'] is False
    assert stale_config['status'] == 'disabled_stale_plan'



def test_group_atmosphere_candidate_pool_exposes_language_role_and_binds_selected_group():
    client = make_client()
    client.post('/api/ops/group-atmosphere/chat-records/auto-learn', json={
        'filename': 'wa-export.txt',
        'content': '\n'.join([
            '[12/05/26, 09.12.33] Admin: Halo kak, kirim ID dan kode ke admin ya.',
            '[12/05/26, 09.13.00] User: kak kode dimana?',
        ]),
    })
    client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-indo-01',
        'account_name': '印尼账号01',
        'region': '印尼',
        'role_positioning': 'newcomer_guide',
        'groups': [
            {'target_group': 'group-a@g.us', 'group_name': '印尼A群', 'enabled': True},
            {'target_group': 'group-b@g.us', 'group_name': '印尼B群', 'enabled': True},
        ],
        'enabled': True,
    })

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    newcomer = next(item for item in pool if item['config_name'] == 'auto-id-newcomer_guide')
    assert newcomer['language'] == 'id'
    assert newcomer['region'] == '印尼'
    assert newcomer['role_positioning'] == 'newcomer_guide'
    assert newcomer['bound_account_key'] in {'', None}
    assert newcomer['candidates'][0]['language'] == 'id'
    assert newcomer['candidates'][0]['role_positioning'] == 'newcomer_guide'

    enabled = client.post('/api/ops/group-atmosphere/candidate-pool/enable', json={
        'config_name': newcomer['config_name'],
        'candidate_ids': [newcomer['candidates'][0]['candidate_id']],
        'account_key': 'atmosphere-indo-01',
        'target_group': 'group-b@g.us',
        'group_name': '印尼B群',
        'daily_max_messages': 1231,
        'min_interval_minutes': 0,
    })
    assert enabled.status_code == 200
    config = enabled.json()['config']
    assert config['account_key'] == 'atmosphere-indo-01'
    assert config['target_group'] == 'group-b@g.us'
    assert config['group_name'] == '印尼B群'
    assert config['daily_max_messages'] == 1231
    assert config['language'] == 'id'


def test_group_atmosphere_import_chat_records_builds_language_profile_and_ai_candidates():
    client = make_client()
    seed_config(client)

    imported = client.post('/api/ops/group-atmosphere/import-chat-records', json={
        'config_name': 'indo-reg-01',
        'records': [
            {'sender': 'admin', 'text': 'Halo kak, jangan lupa cek panduan dulu ya.'},
            {'sender': 'user', 'text': 'Kak cara mulai gimana?'},
            {'sender': 'admin', 'text': 'Kalau sudah siap, kirim ID dan kode undangan ke admin ya kak.'},
            {'sender': 'user', 'text': 'Kode undangan yang mana kak?'},
        ],
    })

    assert imported.status_code == 200
    body = imported.json()
    assert body['imported_count'] == 4
    profile = body['language_profile']
    assert profile['language'] == 'id'
    assert 'kak' in profile['frequent_terms']
    assert profile['tone_markers']['uses_kak'] is True

    candidates = client.post('/api/ops/group-atmosphere/ai-candidates', json={
        'config_name': 'indo-reg-01',
        'topic': 'newcomer_start',
        'count': 3,
    })
    assert candidates.status_code == 200
    payload = candidates.json()
    assert payload['source'] == 'local_language_profile'
    assert len(payload['candidates']) == 3
    assert all('kak' in item['text'].lower() for item in payload['candidates'])
    assert all(item['safe_to_send'] is False for item in payload['candidates'])


def test_group_atmosphere_simulation_runs_schedule_faq_and_ai_without_real_send():
    client = make_client()
    seed_config(client)
    client.post('/api/ops/group-atmosphere/import-chat-records', json={
        'config_name': 'indo-reg-01',
        'records': [
            {'sender': 'admin', 'text': 'Halo kak, cek panduan dan kirim ID ke admin ya.'},
            {'sender': 'admin', 'text': 'Semangat kak, mulai pelan-pelan dulu.'},
        ],
    })

    response = client.post('/api/ops/group-atmosphere/simulate', json={
        'config_name': 'indo-reg-01',
        'scenario': 'full_stage_4',
        'inbound_messages': [
            {'sender_id': 'user-1', 'text': '@bot kode itu apa kak?', 'mentioned': True},
            {'sender_id': 'user-2', 'text': '@bot belum paham mulai', 'mentioned': True},
            {'sender_id': 'user-3', 'text': 'random chat tanpa mention', 'mentioned': False},
        ],
    })

    assert response.status_code == 200
    data = response.json()
    assert data['dry_run'] is True
    assert data['config_name'] == 'indo-reg-01'
    assert len(data['scheduled_messages']) >= 1
    assert data['scheduled_messages'][0]['would_send'] is True
    replies = data['inbound_replies']
    assert replies[0]['should_respond'] is True
    assert replies[0]['result_code'] == 'faq_reply_matched'
    assert replies[1]['should_respond'] is True
    assert replies[1]['result_code'] == 'ai_candidate_reply'
    assert replies[1]['safe_to_send'] is False
    assert replies[2]['should_respond'] is False
    assert data['real_send_performed'] is False
