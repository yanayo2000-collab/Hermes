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
