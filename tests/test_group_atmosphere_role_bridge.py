import json
import re

from fastapi.testclient import TestClient

from app.main import create_app, GoogleTranslateCandidateTranslator


def make_client(settings=None):
    cfg = {"DB_PATH": ":memory:", "AUTO_LARK_REPLY": False}
    if settings:
        cfg.update(settings)
    return TestClient(create_app(cfg))


class FakeCandidateTranslator:
    def __init__(self):
        self.calls = []

    def translate(self, text, *, role='', language='', region=''):
        self.calls.append({'text': text, 'role': role, 'language': language, 'region': region})
        return {
            'text_zh': '请把 ID 发给管理员。',
            'status': 'ok',
            'source': 'ai',
        }


class FakeLibreTranslateResponse:
    status_code = 200

    def json(self):
        return {'translatedText': '请把 ID 发给管理员。'}


class FakeSendResponse:
    status_code = 200
    text = '{"status":"sent","message_id":"msg-1"}'

    def json(self):
        return {'status': 'sent', 'message_id': 'msg-1', 'result_code': 'sent'}


def seed_role_and_account(client):
    role = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '印尼活跃气氛号',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['Halo kak, jgn lupa cek info grup ya.'],
        'enabled': True,
    })
    assert role.status_code == 200
    account = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-indo-01',
        'account_name': '印尼发言号01',
        'region': '印尼',
        'role_positioning': 'community_seed',
        'daily_max_messages': 5,
        'min_interval_minutes': 0,
        'groups': [
            {'target_group': 'group-a@g.us', 'group_name': '印尼A群', 'enabled': True},
            {'target_group': 'group-b@g.us', 'group_name': '印尼B群', 'enabled': True},
        ],
        'enabled': True,
    })
    assert account.status_code == 200


def test_role_binding_is_primary_distribution_surface_and_group_permission_gates_send(monkeypatch):
    client = make_client()
    seed_role_and_account(client)

    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'auto-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0, 1],
        'enabled': True,
        'auto_speaking_enabled': True,
        'group_send_permission_enabled': True,
        'daily_max_messages': 5,
        'min_interval_minutes': 0,
        'worker_base_url': 'http://worker.local',
    })
    assert created.status_code == 200
    body = created.json()
    assert body['created_count'] == 2
    binding_ids = [row['binding_id'] for row in body['bindings']]
    assert len(binding_ids) == 2

    disabled = client.post(f'/api/ops/group-atmosphere/role-bindings/{binding_ids[1]}', json={
        'group_send_permission_enabled': False,
    })
    assert disabled.status_code == 200

    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append({'url': url, 'json': json, 'timeout': timeout})
        return FakeSendResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)
    run = client.post('/api/ops/group-atmosphere/scheduler/run-due', json={})

    assert run.status_code == 200
    payload = run.json()
    assert payload['attempted_count'] == 2
    assert payload['sent_count'] == 1
    result_by_group = {item['target_group']: item for item in payload['results']}
    assert result_by_group['group-a@g.us']['sent'] is True
    assert result_by_group['group-b@g.us']['sent'] is False
    assert result_by_group['group-b@g.us']['result_code'] == 'group_send_permission_disabled'
    assert [item['json']['target_group'] for item in sent] == ['group-a@g.us']

    listed = client.get('/api/ops/group-atmosphere/role-bindings').json()
    assert listed['count'] == 2
    assert listed['rows'][0]['role_name'] == '印尼活跃气氛号'
    assert listed['rows'][0]['distribution_status'] in {'可发送', '群权限关闭'}


def test_role_binding_delete_removes_bridge_from_listing_and_relationships():
    client = make_client()
    seed_role_and_account(client)

    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'auto-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0, 1],
        'enabled': True,
        'auto_speaking_enabled': True,
        'group_send_permission_enabled': True,
    })
    assert created.status_code == 200
    binding_ids = [row['binding_id'] for row in created.json()['bindings']]

    deleted = client.delete(f'/api/ops/group-atmosphere/role-bindings/{binding_ids[0]}')
    assert deleted.status_code == 200
    assert deleted.json()['deleted'] is True

    listed = client.get('/api/ops/group-atmosphere/role-bindings').json()
    assert listed['count'] == 1
    assert [row['binding_id'] for row in listed['rows']] == [binding_ids[1]]
    assert listed['relationship_count'] == 1
    assert [group['binding_id'] for group in listed['relationships'][0]['groups']] == [binding_ids[1]]

    deleted_again = client.delete(f'/api/ops/group-atmosphere/role-bindings/{binding_ids[0]}')
    assert deleted_again.status_code == 404


def test_group_atmosphere_page_delete_bridge_uses_delete_endpoint_instead_of_soft_disable():
    client = make_client()
    page = client.get('/ops/group-atmosphere')
    assert page.status_code == 200
    html = page.text

    delete_script = html.split('async function deleteBridgeRelationship', 1)[1].split('async function deleteGroupAtmosphereRole', 1)[0]
    assert "if(!confirm('确认删除这条桥接关系？'))" in delete_script
    assert "method:'DELETE'" in delete_script
    assert 'enabled:false' not in delete_script


def test_role_binding_allows_disabled_group_but_keeps_group_permission_off():
    client = make_client()
    role = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '印尼活跃气氛号',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['Halo kak'],
        'enabled': True,
    })
    assert role.status_code == 200
    account = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-indo-01',
        'account_name': '印尼发言号01',
        'region': '印尼',
        'role_positioning': 'community_seed',
        'groups': [
            {'target_group': 'disabled-group@g.us', 'group_name': '关闭群', 'enabled': False},
        ],
        'enabled': True,
    })
    assert account.status_code == 200

    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'auto-id-community_seed',
        'group_targets': ['disabled-group@g.us'],
        'enabled': True,
        'auto_speaking_enabled': True,
        'group_send_permission_enabled': True,
    })
    assert created.status_code == 200
    row = created.json()['bindings'][0]
    assert row['target_group'] == 'disabled-group@g.us'
    assert row['group_send_permission_enabled'] is False
    assert row['distribution_status'] == '群权限关闭'


def test_learning_account_delete_removes_learning_and_shadow_whatsapp_account():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-delete-01',
        'account_name': '删除验证学习号',
        'region': '印尼',
        'language': 'id',
        'group_links': [{'target_group': 'https://chat.whatsapp.com/delete-check'}],
    })
    assert created.status_code == 200
    assert client.get('/api/ops/group-atmosphere/learning-accounts').json()['count'] == 1

    deleted = client.delete('/api/ops/group-atmosphere/learning-accounts/learn-delete-01')
    assert deleted.status_code == 200
    assert deleted.json()['deleted'] is True
    assert client.get('/api/ops/group-atmosphere/learning-accounts').json()['count'] == 0

    missing = client.delete('/api/ops/group-atmosphere/learning-accounts/learn-delete-01')
    assert missing.status_code == 404


def test_learning_account_auto_key_does_not_overwrite_existing_learning_bot():
    client = make_client()
    first = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'account_name': '学习bot01',
        'region': '印尼',
        'language': 'id',
        'group_links': [{'target_group': 'group-a@g.us'}],
    })
    second = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'account_name': '学习bot02',
        'region': '印尼',
        'language': 'id',
        'group_links': [{'target_group': 'group-b@g.us'}],
    })
    assert first.status_code == 200
    assert second.status_code == 200
    rows = client.get('/api/ops/group-atmosphere/learning-accounts').json()['rows']
    assert {row['account_name'] for row in rows} == {'学习bot01', '学习bot02'}
    assert len({row['learning_account_key'] for row in rows}) == 2
    assert sorted(row['learning_account_key'] for row in rows) == ['learn-indo-01', 'learn-indo-02']


def test_manual_written_phrases_bypass_cleaning_filtering_polishing_and_dedupe():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '人工写入规则测试',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': [
            '2026/05/18 12:31 - Alice: <Media omitted>',
            '421324123',
            'ok',
            'ok',
            '13/05/26 07.45 - 雪碧-2新中-',
        ],
        'source_type': 'manual',
        'safe_to_send': True,
        'enabled': True,
    })
    assert created.status_code == 200
    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    role = next(row for row in pool if row['config_name'] == 'auto-id-community_seed')
    texts = [candidate['text'] for candidate in role['candidates']]
    assert '2026/05/18 12:31 - Alice: <Media omitted>' in texts
    assert '421324123' in texts
    assert texts.count('ok') == 2
    assert '13/05/26 07.45 - 雪碧-2新中-' in texts

    custom = client.post('/api/ops/group-atmosphere/candidate-pool/custom', json={
        'config_name': 'auto-id-community_seed',
        'text': '12/05/26 21.34 - +62 821: jgn krm <Media omitted>',
        'role_positioning': 'community_seed',
    })
    assert custom.status_code == 200
    assert custom.json()['candidate']['text'] == '12/05/26 21.34 - +62 821: jgn krm <Media omitted>'


def test_deleting_last_candidate_keeps_loaded_role_container_visible():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '删除候选保留角色',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['Halo kak, jgn lupa cek info grup ya.'],
        'source_type': 'manual',
        'safe_to_send': True,
        'enabled': True,
    })
    assert created.status_code == 200
    role_before = next(row for row in client.get('/api/ops/group-atmosphere/roles').json()['rows'] if row['role_key'] == 'auto-id-community_seed')
    assert role_before['phrase_count'] == 1

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    candidate_id = next(row for row in pool if row['config_name'] == 'auto-id-community_seed')['candidates'][0]['candidate_id']
    deleted = client.delete(f'/api/ops/group-atmosphere/candidate-pool/auto-id-community_seed/{candidate_id}')

    assert deleted.status_code == 200
    roles_after = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    role_after = next(row for row in roles_after if row['role_key'] == 'auto-id-community_seed')
    assert role_after['role_name'] == '删除候选保留角色'
    assert role_after['phrase_count'] == 0
    assert role_after['status'] == 'role_container'


def test_role_editor_save_replaces_checked_pool_phrases_without_duplicate_append():
    client = make_client()
    first = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-faq_helper',
        'role_name': '测试答疑角色',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'faq_helper',
        'phrases': ['Halo kak, kalau bingung tanya admin ya', 'hahhh yup', 'hahhh yup'],
        'source_type': 'manual',
        'safe_to_send': True,
        'enabled': True,
    })
    assert first.status_code == 200
    before = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    role_before = next(row for row in before if row['config_name'] == 'auto-id-faq_helper')
    assert [c['text'] for c in role_before['candidates']].count('hahhh yup') == 2

    save = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-faq_helper',
        'role_name': '测试答疑角色',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'faq_helper',
        'phrases': [
            'Halo kak, kalau bingung tanya admin ya',
            'hahhh yup',
            'hahhh yup',
        ],
        'source_type': 'role_save',
        'replace_role_phrases': True,
        'safe_to_send': True,
        'enabled': True,
    })
    assert save.status_code == 200
    after = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    role_after = next(row for row in after if row['config_name'] == 'auto-id-faq_helper')
    texts = [c['text'] for c in role_after['candidates']]
    assert texts.count('Halo kak, kalau bingung tanya admin ya') == 1
    assert texts.count('hahhh yup') == 1
    assert len(texts) == 2


def test_role_editor_frontend_uses_replace_save_and_dedupes_pool_rows():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    assert 'replace_role_phrases:true' in html
    assert "source_type:'role_save'" in html
    assert 'const seen=new Set()' in html
    assert 'seen.has(key)' in html


def test_candidate_pool_cleans_dedupes_and_sorts_learned_phrases():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-faq_helper',
        'role_name': '印尼答疑话术',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'faq_helper',
        'phrases': [
            '2026/05/18 12:31 - Alice: Halo kak, kirim ID ke admin ya',
            'Alice: Halo kak, kirim ID ke admin ya',
            '2026/05/18 12:32 - Bob: <Media omitted>',
            '[12:33, 18/05/2026] Cindy: Kode dmn kak?',
            '12/05/26 21.34 - +62 821-7236-5470: Admin yg barusan KK kirim',
            '13/05/26 07.45 - 雪碧-2新中-',
        ],
        'source_type': 'learning_account',
        'safe_to_send': False,
        'enabled': False,
    })
    assert created.status_code == 200
    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    role = next(row for row in pool if row['config_name'] == 'auto-id-faq_helper')
    texts = [candidate['text'] for candidate in role['candidates']]
    assert set(texts) == {'Halo kak, kirim ID ke admin ya.', 'Kode dmn kak?'}
    assert role['candidates'][0]['frequency'] == 2
    assert all(candidate['text_zh'] for candidate in role['candidates'])
    assert all(candidate['text_zh_source'] == 'rule' for candidate in role['candidates'])
    assert all(candidate['text_zh_status'] in {'ok', 'needs_review'} for candidate in role['candidates'])
    joined = '\n'.join(texts)
    assert all(token not in joined for token in ['Alice:', '2026/', 'Media omitted', '12/05/26', '+62', '雪碧', '新中', 'Admin yg barusan'])


def test_auto_learn_upload_merges_with_existing_candidate_pool_instead_of_overwriting():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'auto-id-community_seed',
        'enabled': False,
        'account_key': 'auto-id-community_seed',
        'target_group': 'auto-id-community_seed',
        'group_name': '自动学习素材库-印尼',
        'language': 'id',
        'daily_max_messages': 0,
        'min_interval_minutes': 120,
        'template_pool': [
            {'candidate_id': 'manual-keep-1', 'text': 'Kak, ini话术不要被上传学习覆盖。', 'source_role': 'community_seed', 'source_type': 'manual', 'safe_to_send': False, 'enabled': False},
            {'candidate_id': 'upload-existing-1', 'text': 'Halo kak, selamat datang. Jangan malu ngobrol di grup ya.', 'source_role': 'community_seed', 'source_type': 'upload_file', 'safe_to_send': False, 'enabled': False, 'frequency': 1},
        ],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'candidate_pool',
    })
    assert created.status_code == 200

    payload = {
        'filename': 'community.txt',
        'content': '\n'.join([
            'Kak, jangan malu ngobrol di grup ya. Saling sapa biar suasana makin hidup.',
            'Kak, tetap semangat ya. Pelan-pelan saja, yang penting terus ikut arahan grup.',
            'Halo kak, selamat datang. Jangan malu ngobrol di grup ya.',
        ]),
    }
    first = client.post('/api/ops/group-atmosphere/chat-records/auto-learn', json=payload)
    assert first.status_code == 200
    second = client.post('/api/ops/group-atmosphere/chat-records/auto-learn', json=payload)
    assert second.status_code == 200

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    row = next(row for row in pool if row['config_name'] == 'auto-id-community_seed')
    texts = [candidate['text'] for candidate in row['candidates']]
    assert 'Kak, ini话术不要被上传学习覆盖。' in texts
    assert 'Halo kak, selamat datang. Jangan malu ngobrol di grup ya.' in texts
    normalized = [client.app.state.service._normalize_group_atmosphere_phrase_key(text) for text in texts]
    assert len(normalized) == len(set(normalized))
    assert any(candidate['candidate_id'] == 'upload-existing-1' for candidate in row['candidates'])


def test_auto_learn_candidates_never_include_chat_export_metadata_or_duplicate_suffixes():
    client = make_client()
    response = client.post('/api/ops/group-atmosphere/chat-records/auto-learn', json={
        'filename': 'dirty.txt',
        'content': '\n'.join([
            '12/05/26 21.34 - +62 821-7236-5470: Admin yg barusan KK kirim',
            '13/05/26 07.45 - 雪碧-2新中-',
            '13/05/26 07.10 - +62 812-1749-8215: izin nanyak kak iti harus ada foto profil?',
            '[12/05/26, 09.12.33] Admin: Halo kak, jgn lupa krm ID ya.',
            '[12/05/26, 09.13.00] User: kak gmn caranya?',
        ]),
    })
    assert response.status_code == 200
    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    texts = [candidate['text'] for row in pool for candidate in row['candidates']]
    joined = '\n'.join(texts)
    assert texts
    assert all(token not in joined for token in ['Catatan grup:', '12/05/26', '13/05/26', '+62', '雪碧', '新中'])
    assert not any(re.search(r'\(\d+\)$', text) for text in texts)
    assert len(texts) == len(set(texts))


def test_auto_learn_candidates_do_not_reuse_untrusted_sender_names_as_local_terms():
    client = make_client()
    response = client.post('/api/ops/group-atmosphere/chat-records/auto-learn', json={
        'filename': 'sender-name.txt',
        'content': '\n'.join([
            '13/05/26 07.45 - 雪碧-2新中-sena: kamu udah verifikasi belum?',
            '13/05/26 07.46 - +62 812-1111-2222: kak gmn caranya?',
            '13/05/26 07.47 - Admin: Halo kak, jgn lupa krm ID ya.',
        ]),
    })
    assert response.status_code == 200
    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    texts = [candidate['text'] for row in pool for candidate in row['candidates']]
    joined = '\n'.join(texts).lower()
    assert 'sena' not in joined
    assert '雪碧' not in joined
    assert '+62' not in joined
    assert 'jgn' in joined or 'krm' in joined or 'gmn' in joined


def test_ai_candidates_ignore_dirty_terms_from_existing_language_profile():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'dirty-profile',
        'enabled': False,
        'account_key': 'dirty-profile',
        'target_group': 'dirty-profile',
        'group_name': 'dirty-profile',
        'language': 'id',
        'daily_max_messages': 0,
        'min_interval_minutes': 120,
        'template_pool': [],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'candidate_pool',
    })
    assert created.status_code == 200
    conn = client.app.state.service.db.connect()
    conn.execute(
        """
        INSERT INTO whatsapp_group_atmosphere_language_profiles
        (config_name, language, sample_count, frequent_terms, phrase_samples, tone_markers, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            'dirty-profile',
            'id',
            3,
            '["852", "5122", "sena", "kak", "kode"]',
            '["13/05/26 08.13 - 雪碧-2新中-sena: 2500 diamond itu 8rb yaa"]',
            '{"uses_kak": true, "local_abbreviations": ["sena", "kak", "gmn"]}',
            '2026-05-19T00:00:00Z',
        ),
    )
    conn.commit()
    response = client.post('/api/ops/group-atmosphere/ai-candidates', json={
        'config_name': 'dirty-profile',
        'topic': 'community_seed',
        'count': 8,
    })
    assert response.status_code == 200
    joined = '\n'.join(candidate['text'] for candidate in response.json()['candidates']).lower()
    assert 'sena' not in joined
    assert '雪碧' not in joined
    assert '13/05/26' not in joined
    assert '852' not in joined
    assert 'gmn' in joined


def test_candidate_pool_custom_phrase_is_persisted_and_prioritized():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'auto-id-community_seed',
        'enabled': False,
        'account_key': 'auto-id-community_seed',
        'target_group': 'auto-id-community_seed',
        'group_name': '自动学习素材库-印尼',
        'language': 'id',
        'daily_max_messages': 0,
        'min_interval_minutes': 120,
        'template_pool': [
            {'candidate_id': 'upload-1', 'text': 'Halo kak dari upload lama', 'source_role': 'community_seed', 'source_type': 'upload_file', 'safe_to_send': False, 'enabled': False},
            {'candidate_id': 'upload-2', 'text': 'Halo kak dari upload kedua', 'source_role': 'community_seed', 'source_type': 'upload_file', 'safe_to_send': False, 'enabled': False},
            {'candidate_id': 'learn-1', 'text': 'Halo kak dari learning bot', 'source_role': 'community_seed', 'source_type': 'learning_account', 'safe_to_send': False, 'enabled': False},
        ],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'candidate_pool',
    })
    assert created.status_code == 200

    manual = client.post('/api/ops/group-atmosphere/candidate-pool/custom', json={
        'config_name': 'auto-id-community_seed',
        'text': 'Halo kak ini tulisan manual operator',
        'role_positioning': 'community_seed',
    })
    assert manual.status_code == 200
    edited = client.post('/api/ops/group-atmosphere/candidate-pool/custom', json={
        'config_name': 'auto-id-community_seed',
        'candidate_id': 'upload-2',
        'text': 'Halo kak upload kedua sudah diedit operator',
    })
    assert edited.status_code == 200

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    candidates = pool[0]['candidates']
    assert candidates[0]['text'] == 'Halo kak ini tulisan manual operator'
    assert candidates[0]['source_type'] == 'manual'
    assert candidates[0]['source_label'] == '人工写入'
    upload_candidates = [item for item in candidates if item['source_type'] == 'upload_file']
    assert [item['candidate_id'] for item in upload_candidates[:2]] == ['upload-2', 'upload-1']
    assert upload_candidates[0]['text'] == 'Halo kak upload kedua sudah diedit operator'
    assert upload_candidates[0]['customized'] is True


def test_candidate_pool_reorder_persists_manual_priority_order():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'auto-id-community_seed',
        'enabled': False,
        'account_key': 'auto-id-community_seed',
        'target_group': 'auto-id-community_seed',
        'group_name': '自动学习素材库-印尼',
        'language': 'id',
        'daily_max_messages': 0,
        'min_interval_minutes': 120,
        'template_pool': [
            {'candidate_id': 'upload-1', 'text': '第一条低优先级', 'source_role': 'community_seed', 'source_type': 'upload_file', 'score': 10, 'safe_to_send': False, 'enabled': False},
            {'candidate_id': 'upload-2', 'text': '第二条提升优先级', 'source_role': 'community_seed', 'source_type': 'upload_file', 'score': 5, 'safe_to_send': False, 'enabled': False},
            {'candidate_id': 'upload-3', 'text': '第三条中间优先级', 'source_role': 'community_seed', 'source_type': 'upload_file', 'score': 1, 'safe_to_send': False, 'enabled': False},
        ],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'candidate_pool',
    })
    assert created.status_code == 200

    reordered = client.post('/api/ops/group-atmosphere/candidate-pool/reorder', json={
        'config_name': 'auto-id-community_seed',
        'candidate_ids': ['upload-2', 'upload-3', 'upload-1'],
    })
    assert reordered.status_code == 200
    assert reordered.json()['ordered_count'] == 3

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    candidates = next(row for row in pool if row['config_name'] == 'auto-id-community_seed')['candidates']
    assert [item['candidate_id'] for item in candidates] == ['upload-2', 'upload-3', 'upload-1']
    assert [item['sort_order'] for item in candidates] == [0, 1, 2]


def test_group_atmosphere_candidate_pool_page_exposes_drag_sort_controls():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text

    assert 'draggable="true"' in html
    assert 'data-ga-candidate-drag-handle' in html
    assert '拖动排序' in html
    assert 'function onCandidateDragStart' in html
    assert 'function onCandidateDrop' in html
    assert 'function moveCandidatePriority' in html
    assert '/api/ops/group-atmosphere/candidate-pool/reorder' in html
    assert '排序已保存' in html
    assert '上移' in html
    assert '下移' in html


def test_learning_account_is_silent_and_updates_candidate_pool_from_chat_records(monkeypatch):
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-indo-01',
        'account_name': '印尼学习话术号01',
        'region': '印尼',
        'enabled': True,
        'worker_base_url': 'http://learning-worker.local',
        'group_links': [
            {
                'target_group': 'group-a@g.us',
                'group_name': '印尼A群',
                'enabled': True,
                'last_learned_message_id': 'msg-old-9',
                'last_learned_message_at': '2026-05-15T02:59:00Z',
            },
            {'target_group': 'group-b@g.us', 'group_name': '印尼B群', 'enabled': False},
        ],
        'target_role_keys': ['auto-id-community_seed'],
        'daily_learning_time': '03:00',
    })
    assert created.status_code == 200
    account = created.json()['account']
    assert account['responsible_type'] == 'group_atmosphere_learning'
    assert account['language'] == 'id'
    assert account['silent_learning_only'] is True
    assert account['group_links'][0]['enabled'] is True
    assert account['group_links'][1]['enabled'] is False

    fetch_calls = []

    class FakeFetchResponse:
        status_code = 200
        text = '{"status":"success"}'

        def json(self):
            return {
                'status': 'success',
                'result_code': 'messages_fetched',
                'records': [
                    {'sender': 'user1', 'text': 'Halo kak, gmn cara mulai?', 'created_at': '2026-05-15T03:00:00Z', 'message_id': 'msg-new-1'},
                    {'sender': 'admin', 'text': 'Jgn lupa krm ID dan kode ke admin ya kak.', 'created_at': '2026-05-15T03:01:00Z', 'message_id': 'msg-new-2'},
                ],
                'next_cursor': {'last_message_id': 'msg-new-2', 'last_message_at': '2026-05-15T03:01:00Z'},
            }

    def fake_post(url, json=None, timeout=None):
        fetch_calls.append({'url': url, 'json': json, 'timeout': timeout})
        return FakeFetchResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)
    learned = client.post('/api/ops/group-atmosphere/learning-accounts/learn-indo-01/learn-once', json={})
    assert fetch_calls == [{
        'url': 'http://learning-worker.local/fetch-group-messages',
        'json': {
            'target_group': 'group-a@g.us',
            'limit': 300,
            'after_message_id': 'msg-old-9',
            'after_timestamp': '2026-05-15T02:59:00Z',
        },
        'timeout': 30,
    }]
    assert all(call['json']['target_group'] != 'group-b@g.us' for call in fetch_calls)
    assert learned.status_code == 200
    body = learned.json()
    assert body['ok'] is True
    assert body['silent_learning_only'] is True
    assert body['imported_count'] == 2
    assert body['candidate_count'] >= 1
    refreshed = client.get('/api/ops/group-atmosphere/learning-accounts').json()['rows'][0]
    assert refreshed['group_links'][0]['last_learned_message_id'] == 'msg-new-2'
    assert refreshed['group_links'][0]['last_learned_message_at'] == '2026-05-15T03:01:00Z'
    assert 'last_learned_cursor_at' in refreshed['group_links'][0]
    assert refreshed['group_links'][1].get('last_learned_message_id') is None

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    role = next(row for row in pool if row['config_name'] == 'auto-id-community_seed')
    assert role['source_types'] == ['learning_account']
    assert role['enabled_candidate_count'] == 0
    assert all(candidate['safe_to_send'] is False for candidate in role['candidates'])


def test_learning_account_filters_polishes_and_routes_useful_phrases_by_role():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-route-01',
        'account_name': '学习话术路由号',
        'region': '印尼',
        'language': 'id',
        'enabled': True,
        'group_links': [{'target_group': 'route-group@g.us'}],
        'target_role_keys': [
            'auto-id-community_seed',
            'auto-id-faq_helper',
            'auto-id-newcomer_guide',
            'auto-id-motivation_admin',
        ],
    })
    assert created.status_code == 200

    learned = client.post('/api/ops/group-atmosphere/learning-accounts/learn-route-01/learn-once', json={
        'records': [
            {'sender': 'user1', 'text': 'wkwkwk 😂😂'},
            {'sender': 'user2', 'text': 'Pada kerja mungkin kak'},
            {'sender': 'user3', 'text': 'Kak, kode dmn ya? aku bingung cara mulai'},
            {'sender': 'admin', 'text': 'Jgn lupa krm ID dan kode ke admin ya kak.'},
            {'sender': 'admin', 'text': 'Semangat kak konsisten aktif aja pasti bisa kok'},
        ],
    })
    assert learned.status_code == 200
    body = learned.json()
    assert body['candidate_count'] == 3
    assert body['last_result_summary']['candidate_count'] == 3
    learned_items = body['last_result_summary']['items']
    assert {item['role_positioning'] for item in learned_items} == {'faq_helper', 'newcomer_guide', 'motivation_admin'}
    assert all(item['text'] for item in learned_items)

    rows = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    by_config = {row['config_name']: row for row in rows}
    all_text = '\n'.join(candidate['text'] for row in rows for candidate in row['candidates']).lower()
    original_case_text = '\n'.join(candidate['text'] for row in rows for candidate in row['candidates'])
    assert 'wkwk' not in all_text
    assert 'pada kerja mungkin' not in all_text
    assert 'jgn' in all_text
    assert 'krm' in all_text
    assert 'dmn' in all_text
    assert 'kirim ID dan kode' not in original_case_text
    assert 'krm ID dan kode' in original_case_text
    assert by_config['auto-id-faq_helper']['candidate_count'] == 1
    assert by_config['auto-id-newcomer_guide']['candidate_count'] == 1
    assert by_config['auto-id-motivation_admin']['candidate_count'] == 1
    assert 'auto-id-community_seed' not in by_config


def test_learning_account_understands_soft_motivation_and_atmosphere_without_business_keywords():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-semantic-01',
        'account_name': '语义学习话术号',
        'region': '印尼',
        'language': 'id',
        'enabled': True,
        'group_links': [{'target_group': 'semantic-group@g.us'}],
        'target_role_keys': [
            'auto-id-community_seed',
            'auto-id-motivation_admin',
        ],
    })
    assert created.status_code == 200

    learned = client.post('/api/ops/group-atmosphere/learning-accounts/learn-semantic-01/learn-once', json={
        'records': [
            {'sender': 'member1', 'text': 'Pelan pelan aja, yang penting tetap jalan sedikit demi sedikit'},
            {'sender': 'member2', 'text': 'Jangan malu ngobrol di sini, saling sapa biar suasana hidup'},
        ],
    })
    assert learned.status_code == 200
    body = learned.json()
    assert body['candidate_count'] == 2
    assert body['last_result_summary']['useful_count'] == 2
    assert body['last_result_summary']['semantic_candidate_count'] == 2
    learned_items = body['last_result_summary']['items']
    assert {item['role_positioning'] for item in learned_items} == {'motivation_admin', 'community_seed'}
    assert all(item['safe_to_send'] is False for item in learned_items)
    learned_text = '\n'.join(item['text'] for item in learned_items).lower()
    assert 'pelan pelan aja' not in learned_text
    assert 'saling sapa biar suasana hidup' not in learned_text


def test_learning_account_list_reflects_authenticated_runtime_even_when_learning_disabled(monkeypatch):
    client = make_client()
    service = client.app.state.service
    created = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-indo-disabled',
        'account_name': '学习bot禁用但已登录',
        'region': '印尼',
        'enabled': False,
        'group_links': [{'target_group': 'disabled-group@g.us', 'group_name': '禁用学习群'}],
    })
    assert created.status_code == 200
    client_id = service._whatsapp_approval_session_client_id('learn-indo-disabled')
    auth_path = str(service._whatsapp_approval_session_auth_path('learn-indo-disabled'))
    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: {
        'pid': 43210,
        'port': 59997,
        'base_url': 'http://127.0.0.1:59997',
        'auth_path': auth_path,
        'client_id': client_id,
    } if key == 'learn-indo-disabled' else {})
    monkeypatch.setattr(service, '_pid_running', lambda pid: True)
    health_calls = []
    def fake_health(base_url):
        health_calls.append(base_url)
        return {
            'status': 'warm',
            'ready': True,
            'authenticated': True,
            'approval_client': {
                'status': 'warm',
                'ready': True,
                'authenticated': True,
                'client_id': client_id,
                'auth_path': auth_path,
                'auth_strategy': 'LocalAuth',
            },
        }
    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', fake_health)

    rows = client.get('/api/ops/group-atmosphere/learning-accounts').json()['rows']

    assert health_calls == ['http://127.0.0.1:59997']
    row = rows[0]
    assert row['enabled'] is False
    assert row['runtime']['authenticated'] is True
    assert row['session']['login_verified'] is True
    assert row['login_verified'] is True
    assert row['login_check_message'] == '账号已登录，可以正常使用。'



def test_learning_account_list_probes_and_persists_actual_group_name_when_logged_in(monkeypatch):
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-probe-01',
        'account_name': '学习bot真实群名',
        'region': '印尼',
        'enabled': True,
        'group_links': [{'target_group': 'https://chat.whatsapp.com/probeInvite', 'enabled': True}],
    })
    assert created.status_code == 200
    service = client.app.state.service
    client_id = service._whatsapp_approval_session_client_id('learn-probe-01')
    auth_path = str(service._whatsapp_approval_session_auth_path('learn-probe-01'))
    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: {
        'pid': 43111,
        'port': 59991,
        'base_url': 'http://127.0.0.1:59991',
        'auth_path': auth_path,
        'client_id': client_id,
    } if key == 'learn-probe-01' else {})
    monkeypatch.setattr(service, '_pid_running', lambda pid: True)
    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', lambda base_url: {
        'status': 'warm',
        'ready': True,
        'authenticated': True,
        'approval_client': {
            'status': 'warm',
            'ready': True,
            'authenticated': True,
            'client_id': client_id,
            'auth_path': auth_path,
            'auth_strategy': 'LocalAuth',
        },
    })
    probe_calls = []

    class FakeProbeResponse:
        status_code = 200
        text = '{"group_name":"GROUP01 印尼学习群","group_id":"120363111@g.us"}'

        def raise_for_status(self):
            return None

        def json(self):
            return {'group_name': 'GROUP01 印尼学习群', 'group_id': '120363111@g.us'}

    def fake_post(url, json=None, timeout=None):
        probe_calls.append({'url': url, 'json': json, 'timeout': timeout})
        return FakeProbeResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)

    listed = client.get('/api/ops/group-atmosphere/learning-accounts')

    assert listed.status_code == 200
    assert probe_calls == [{
        'url': 'http://127.0.0.1:59991/probe-group-state',
        'json': {'registration_group': 'https://chat.whatsapp.com/probeInvite'},
        'timeout': 8.0,
    }]
    row = listed.json()['rows'][0]
    assert row['login_verified'] is True
    assert row['group_links'][0]['group_name'] == 'GROUP01 印尼学习群'
    assert row['group_links'][0]['group_id'] == '120363111@g.us'

    listed_again = client.get('/api/ops/group-atmosphere/learning-accounts').json()['rows'][0]
    assert listed_again['group_links'][0]['group_name'] == 'GROUP01 印尼学习群'

def test_learning_once_auto_uses_active_runtime_base_url_when_worker_url_not_saved(monkeypatch):
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-indo-runtime',
        'account_name': '印尼学习话术号runtime',
        'region': '印尼',
        'enabled': True,
        'group_links': [{'target_group': 'runtime-group@g.us', 'group_name': 'Runtime群'}],
        'target_role_keys': ['auto-id-community_seed'],
    })
    assert created.status_code == 200
    service = client.app.state.service
    monkeypatch.setattr(
        service,
        '_build_whatsapp_approval_runtime_state',
        lambda account_key, **kwargs: {'active': True, 'base_url': 'http://runtime-worker.local'} if account_key == 'learn-indo-runtime' else {},
    )
    fetch_calls = []

    class FakeFetchResponse:
        status_code = 200
        text = '{"status":"success"}'

        def json(self):
            return {'records': [{'sender': 'u1', 'text': 'Halo kak, krm ID ya', 'created_at': '2026-05-19T00:00:00Z'}]}

    def fake_post(url, json=None, timeout=None):
        fetch_calls.append({'url': url, 'json': json, 'timeout': timeout})
        return FakeFetchResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)
    learned = client.post('/api/ops/group-atmosphere/learning-accounts/learn-indo-runtime/learn-once', json={})
    assert learned.status_code == 200
    assert fetch_calls == [{
        'url': 'http://runtime-worker.local/fetch-group-messages',
        'json': {'target_group': 'runtime-group@g.us', 'limit': 300},
        'timeout': 30,
    }]
    assert learned.json()['imported_count'] == 1


def test_enabled_learning_account_runs_every_six_hours_from_scheduler(monkeypatch):
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-scheduled-01',
        'account_name': '定时学习号',
        'region': '印尼',
        'language': 'id',
        'enabled': True,
        'daily_learning_time': '00:00',
        'worker_base_url': 'http://learning-worker.local',
        'group_links': [{'target_group': 'scheduled-group@g.us', 'group_name': '定时群'}],
        'target_role_keys': ['auto-id-newcomer_guide'],
    })
    assert created.status_code == 200
    fetch_calls = []

    class FakeFetchResponse:
        status_code = 200
        text = '{"status":"success"}'

        def json(self):
            return {'records': [{'sender': 'admin', 'text': 'Jgn lupa krm ID dan kode ke admin ya kak.', 'created_at': '2026-05-19T00:00:00Z'}]}

    def fake_post(url, json=None, timeout=None):
        fetch_calls.append({'url': url, 'json': json, 'timeout': timeout})
        return FakeFetchResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)
    service = client.app.state.service
    result = service.run_due_group_atmosphere_learning_scheduler(limit=10)
    assert result['learned_count'] == 1
    assert fetch_calls == [{
        'url': 'http://learning-worker.local/fetch-group-messages',
        'json': {'target_group': 'scheduled-group@g.us', 'limit': 300},
        'timeout': 30,
    }]

    second = service.run_due_group_atmosphere_learning_scheduler(limit=10)
    assert second['learned_count'] == 0

    conn = service.db.connect()
    old_time = '2026-05-18T00:00:00+00:00'
    conn.execute("UPDATE whatsapp_group_atmosphere_learning_accounts SET last_learned_at=? WHERE learning_account_key=?", (old_time, 'learn-scheduled-01'))
    conn.commit()
    third = service.run_due_group_atmosphere_learning_scheduler(limit=10)
    assert third['learned_count'] == 1
    assert len(fetch_calls) == 2


def test_candidate_pool_keeps_only_latest_100_review_candidates_per_role():
    client = make_client()
    phrases = [f'Halo kak, krm ID dan kode ke admin ya batch {idx}' for idx in range(105)]
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-newcomer_guide',
        'role_name': '印尼新人引导',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'newcomer_guide',
        'phrases': phrases,
        'source_type': 'learning_account',
        'safe_to_send': False,
        'enabled': False,
    })
    assert created.status_code == 200
    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    role = next(row for row in pool if row['config_name'] == 'auto-id-newcomer_guide')
    assert role['candidate_count'] == 100
    texts = [item['text'] for item in role['candidates']]
    assert all(item['safe_to_send'] is False and item['enabled'] is False for item in role['candidates'])
    assert not any('batch 0.' in text for text in texts)
    assert any('batch 104.' in text for text in texts)



def test_learning_group_card_uses_three_line_layout_with_actual_name_switch_link_and_progress():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    renderer = html.split('function renderLearningAccounts', 1)[1].split('function renderLearningGroupLinks', 1)[0]

    assert 'ga-learning-group-card' in renderer
    assert 'ga-learning-group-main' in renderer
    assert 'ga-learning-group-name' in renderer
    assert 'ga-learning-group-link-line' in renderer
    assert 'ga-learning-group-progress' in renderer
    assert '当前已学习生成 ${learnedCount} 条文案' in renderer
    assert 'openLearningResultModal' in renderer
    assert '<div class="ga-learning-summary"' not in renderer

def test_indonesian_learning_candidates_get_readable_chinese_meanings():
    class IndonesianSentenceTranslator:
        def translate(self, text, *, role='', language='', region=''):
            meanings = {
                'Kak, jangan malu ngobrol di grup ya. Saling sapa biar suasana makin hidup.': '请不要不好意思在群里聊天，大家可以互相打招呼，让群里的气氛更活跃。',
                'Kak, tetap semangat ya. Pelan-pelan saja, yang penting terus ikut arahan grup.': '请继续保持积极，可以慢慢来，重要的是持续跟着群里的指引走。',
                'Kok bisa ada user yang nyariin kak boleh tau caranya supaya usernya inget terus sama kita gmna?': '为什么会有用户主动来找你？可以了解一下怎样做，才能让用户一直记得我们吗？',
            }
            return {'text_zh': meanings[text], 'status': 'ok', 'source': 'ai'}

    client = make_client({'GROUP_ATMOSPHERE_CANDIDATE_TRANSLATOR': IndonesianSentenceTranslator()})
    phrases = [
        'Kak, jangan malu ngobrol di grup ya. Saling sapa biar suasana makin hidup.',
        'Kak, tetap semangat ya. Pelan-pelan saja, yang penting terus ikut arahan grup.',
        'Kok bisa ada user yang nyariin kak boleh tau caranya supaya usernya inget terus sama kita gmna?',
    ]
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '印尼气氛活跃',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': phrases,
        'source_type': 'learning_account',
        'safe_to_send': False,
        'enabled': False,
    })
    assert created.status_code == 200

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    row = next(item for item in pool if item['config_name'] == 'auto-id-community_seed')
    translations = {candidate['text']: candidate['text_zh'] for candidate in row['candidates']}

    assert translations[phrases[0]] == '请不要不好意思在群里聊天，大家可以互相打招呼，让群里的气氛更活跃。'
    assert translations[phrases[1]] == '请继续保持积极，可以慢慢来，重要的是持续跟着群里的指引走。'
    assert translations[phrases[2]] == '为什么会有用户主动来找你？可以了解一下怎样做，才能让用户一直记得我们吗？'
    assert all('大意：' not in text_zh for text_zh in translations.values())
    assert all(' gmna' not in text_zh and 'pelan pelan' not in text_zh and 'malu ngobrol' not in text_zh for text_zh in translations.values())


def test_learning_candidate_ingestion_uses_sentence_level_translator_instead_of_rule_fallback():
    class SentenceTranslator:
        def __init__(self):
            self.calls = []

        def translate(self, text, *, role='', language='', region=''):
            self.calls.append({'text': text, 'role': role, 'language': language, 'region': region})
            return {
                'text_zh': '准确中文：请在群里自然聊天，让用户更容易记住我们。',
                'status': 'ok',
                'source': 'ai',
            }

    translator = SentenceTranslator()
    client = make_client({'GROUP_ATMOSPHERE_CANDIDATE_TRANSLATOR': translator})
    phrase = 'Kak, jangan malu ngobrol di grup ya, saling cerita supaya user makin ingat sama kita.'
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '印尼气氛活跃',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': [phrase],
        'source_type': 'learning_account',
        'safe_to_send': False,
        'enabled': False,
    })
    assert created.status_code == 200

    candidate = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows'][0]['candidates'][0]
    assert candidate['text_zh'] == '准确中文：请在群里自然聊天，让用户更容易记住我们。'
    assert candidate['text_zh_source'] == 'ai'
    assert candidate['text_zh_status'] == 'ok'
    assert translator.calls == [{'text': phrase, 'role': 'community_seed', 'language': 'id', 'region': '印尼'}]


def test_translation_fallback_never_displays_mixed_indonesian_chinese_as_meaning():
    client = make_client()
    phrase = 'Halo kak, kalau ada yang belum jelas, boleh tanya di grup ya.'
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '印尼气氛活跃',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': [phrase],
        'source_type': 'learning_account',
        'safe_to_send': False,
        'enabled': False,
    })
    assert created.status_code == 200

    candidate = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows'][0]['candidates'][0]
    assert candidate['text_zh'] == ''
    assert candidate['text_zh_source'] == 'unavailable'
    assert candidate['text_zh_status'] == 'needs_translation'


def test_candidate_translation_can_use_google_public_endpoint_without_api_key(monkeypatch):
    calls = []

    class FakeGoogleTranslateResponse:
        status_code = 200
        text = '[[["请不要不好意思在群里聊天，大家可以互相打招呼，让群里的气氛更活跃。","src",null,null,3]],null,"id"]'

        def json(self):
            return [[['请不要不好意思在群里聊天，大家可以互相打招呼，让群里的气氛更活跃。', 'src', None, None, 3]], None, 'id']

    def fake_get(url, params=None, timeout=None):
        calls.append({'url': url, 'params': params, 'timeout': timeout})
        return FakeGoogleTranslateResponse()

    monkeypatch.setattr('app.main.requests.get', fake_get)
    client = make_client({'GROUP_ATMOSPHERE_TRANSLATOR_PROVIDER': 'google'})
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '印尼气氛活跃',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['Kak, jangan malu ngobrol di grup ya. Saling sapa biar suasana makin hidup.'],
        'source_type': 'learning_account',
        'safe_to_send': False,
        'enabled': False,
    })
    assert created.status_code == 200
    candidate = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows'][0]['candidates'][0]

    assert candidate['text_zh'] == '请不要不好意思在群里聊天，大家可以互相打招呼，让群里的气氛更活跃。'
    assert candidate['text_zh_source'] == 'google'
    assert candidate['text_zh_status'] == 'ok'
    assert calls == [{
        'url': 'https://translate.googleapis.com/translate_a/single',
        'params': {
            'client': 'gtx',
            'sl': 'id',
            'tl': 'zh-CN',
            'dt': 't',
            'q': 'Kak, jangan malu ngobrol di grup ya. Saling sapa biar suasana makin hidup.',
        },
        'timeout': 20.0,
    }]


def test_google_translation_source_language_follows_region_when_language_missing(monkeypatch):
    calls = []

    class FakeGoogleTranslateResponse:
        status_code = 200

        def json(self):
            return [[['请发送你的 ID 给管理员。', 'src', None, None, 3]], None, 'pt']

    def fake_get(url, params=None, timeout=None):
        calls.append({'url': url, 'params': params, 'timeout': timeout})
        return FakeGoogleTranslateResponse()

    monkeypatch.setattr('app.main.requests.get', fake_get)
    translator = GoogleTranslateCandidateTranslator(timeout_seconds=7)

    result = translator.translate('Por favor, envie seu ID para o admin.', region='巴西')

    assert result['text_zh'] == '请发送你的 ID 给管理员。'
    assert result['source'] == 'google'
    assert calls[0]['params']['sl'] == 'pt'
    assert calls[0]['params']['tl'] == 'zh-CN'
    assert calls[0]['timeout'] == 7


def test_google_translation_source_language_auto_detects_unknown_region(monkeypatch):
    calls = []

    class FakeGoogleTranslateResponse:
        status_code = 200

        def json(self):
            return [[['你好。', 'src', None, None, 3]], None, 'auto']

    def fake_get(url, params=None, timeout=None):
        calls.append({'url': url, 'params': params, 'timeout': timeout})
        return FakeGoogleTranslateResponse()

    monkeypatch.setattr('app.main.requests.get', fake_get)
    translator = GoogleTranslateCandidateTranslator()

    translator.translate('Bonjour.', region='未知地区')

    assert calls[0]['params']['sl'] == 'auto'


def test_candidate_translation_can_use_free_self_hosted_libretranslate(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append({'url': url, 'json': json, 'timeout': timeout})
        return FakeLibreTranslateResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)
    client = make_client({
        'GROUP_ATMOSPHERE_TRANSLATOR_PROVIDER': 'libretranslate',
        'LIBRETRANSLATE_BASE_URL': 'http://127.0.0.1:5000',
    })
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-faq_helper',
        'role_name': '印尼答疑话术',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'faq_helper',
        'phrases': ['Halo kak, kirim ID ke admin ya'],
        'source_type': 'learning_account',
        'safe_to_send': False,
        'enabled': False,
    })
    assert created.status_code == 200
    candidate = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows'][0]['candidates'][0]

    translated = client.post('/api/ops/group-atmosphere/candidate-pool/translate', json={
        'config_name': 'auto-id-faq_helper',
        'candidate_id': candidate['candidate_id'],
    })
    assert translated.status_code == 200
    body = translated.json()
    assert body['candidate']['text_zh'] == '请把 ID 发给管理员。'
    assert body['candidate']['text_zh_source'] == 'libretranslate'
    assert calls == [{
        'url': 'http://127.0.0.1:5000/translate',
        'json': {'q': 'Halo kak, kirim ID ke admin ya.', 'source': 'id', 'target': 'zh', 'format': 'text'},
        'timeout': 20.0,
    }]


def test_candidate_translation_endpoint_uses_ai_once_then_cache_and_preserves_source_type():
    translator = FakeCandidateTranslator()
    client = make_client({'GROUP_ATMOSPHERE_CANDIDATE_TRANSLATOR': translator})
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-faq_helper',
        'role_name': '印尼答疑话术',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'faq_helper',
        'phrases': ['Halo kak, kirim ID ke admin ya'],
        'source_type': 'learning_account',
        'safe_to_send': False,
        'enabled': False,
    })
    assert created.status_code == 200
    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    role = next(row for row in pool if row['config_name'] == 'auto-id-faq_helper')
    candidate = role['candidates'][0]

    translated = client.post('/api/ops/group-atmosphere/candidate-pool/translate', json={
        'config_name': 'auto-id-faq_helper',
        'candidate_id': candidate['candidate_id'],
    })
    assert translated.status_code == 200
    body = translated.json()
    assert body['candidate']['text_zh'] == '请把 ID 发给管理员。'
    assert body['candidate']['text_zh_source'] == 'ai'
    assert body['candidate']['text_zh_status'] == 'ok'
    assert body['candidate']['source_type'] == 'learning_account'
    assert len(translator.calls) == 1

    cached = client.post('/api/ops/group-atmosphere/candidate-pool/translate', json={
        'config_name': 'auto-id-faq_helper',
        'candidate_id': candidate['candidate_id'],
    })
    assert cached.status_code == 200
    assert cached.json()['candidate']['text_zh'] == '请把 ID 发给管理员。'
    assert len(translator.calls) == 1


def test_candidate_manual_translation_override_is_not_replaced_by_ai():
    translator = FakeCandidateTranslator()
    client = make_client({'GROUP_ATMOSPHERE_CANDIDATE_TRANSLATOR': translator})
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-faq_helper',
        'role_name': '印尼答疑话术',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'faq_helper',
        'phrases': ['Kode dmn kak?'],
        'source_type': 'learning_account',
        'safe_to_send': False,
        'enabled': False,
    })
    assert created.status_code == 200
    candidate = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows'][0]['candidates'][0]
    translator.calls.clear()

    manual = client.post('/api/ops/group-atmosphere/candidate-pool/translate', json={
        'config_name': 'auto-id-faq_helper',
        'candidate_id': candidate['candidate_id'],
        'text_zh': '代码在哪里？',
    })
    assert manual.status_code == 200
    assert manual.json()['candidate']['text_zh_source'] == 'manual'

    translated = client.post('/api/ops/group-atmosphere/candidate-pool/translate', json={
        'config_name': 'auto-id-faq_helper',
        'candidate_id': candidate['candidate_id'],
    })
    assert translated.status_code == 200
    assert translated.json()['candidate']['text_zh'] == '代码在哪里？'
    assert translated.json()['candidate']['text_zh_source'] == 'manual'
    assert translator.calls == []


def test_group_atmosphere_page_translation_button_requests_backend_endpoint():
    client = make_client()
    page = client.get('/ops/group-atmosphere')
    assert page.status_code == 200
    html = page.text
    assert '/api/ops/group-atmosphere/candidate-pool/translate' in html
    assert 'async function toggleCandidateTranslation(configName,candidateId)' in html
    assert '正在翻译' in html
    assert 'text_zh_source' in html


def test_group_atmosphere_page_keeps_learning_control_single_entry():
    client = make_client()
    page = client.get('/ops/group-atmosphere')
    assert page.status_code == 200
    html = page.text
    assert '自动学习已开启' not in html
    assert '自动学习已关闭' not in html
    assert 'ga-learning-switch' not in html
    assert '学习生效中' not in html
    assert 'openLearningResultModal' in html
    assert 'ga_learning_result_modal' in html
    assert 'function toggleCandidateTranslation(configName,candidateId)' in html
    assert 'data-zh-text' in html
    assert '<span>当前已学习生成 ${learnedCount} 条文案</span><a href="javascript:void(0)" class="ga-learning-detail-link"' in html
    assert '<button type="button" class="ga-learning-summary"' not in html
    assert '实时学习</button>' in html
    assert 'learnOnceLearningBot' in html
    assert '立即学习一次</button>' not in html
    assert '立即学习</button>' not in html

def test_group_atmosphere_page_exposes_role_bridge_and_custom_phrase_entry():
    client = make_client()
    page = client.get('/ops/group-atmosphere')
    assert page.status_code == 200
    html = page.text
    assert '发言桥接区' in html
    assert '话术角色' in html
    assert '学习话术号' not in html
    assert '静默学习' not in html
    assert '新增话术角色' in html
    assert '手动新增话术' not in html
    assert '暂无学习机器人' not in html
    assert '学习机器人已删除' not in html
    assert '#ga_learning_accounts:empty{display:none!important;}' in html
    assert '#ga_tip_toast' in html
    assert "box.classList.add('is-empty');box.innerHTML=''" in html
    assert "showTip('学习机器人已保存','success')" in html
    assert '/api/ops/group-atmosphere/role-bindings' in html
    assert '/api/ops/group-atmosphere/learning-accounts' in html
    assert '/api/ops/group-atmosphere/roles/manual-phrases' in html


def test_role_bridge_relationship_groups_rows_into_one_card_and_caps_ten_groups():
    client = make_client()
    role = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '印尼活跃气氛号',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['Halo kak, info grup ya.'],
        'enabled': True,
    })
    assert role.status_code == 200
    groups = [
        {'target_group': f'group-{i}@g.us', 'group_name': f'印尼{i}群', 'enabled': True}
        for i in range(10)
    ]
    account = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-indo-10',
        'account_name': '印尼发言号10',
        'region': '印尼',
        'groups': groups,
        'enabled': True,
    })
    assert account.status_code == 200

    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'auto-id-community_seed',
        'account_key': 'atmosphere-indo-10',
        'group_indexes': list(range(10)),
        'daily_max_messages': 12,
        'min_interval_minutes': 7,
        'max_interval_minutes': 19,
        'auto_speaking_enabled': True,
        'group_send_permission_enabled': True,
    })
    assert created.status_code == 200
    body = created.json()
    assert body['created_count'] == 10
    assert len(body['relationship']['groups']) == 10
    assert body['relationship']['relationship_label'] == '桥接关系1'
    assert body['relationship']['daily_max_messages'] == 12
    assert body['relationship']['min_interval_minutes'] == 7
    assert body['relationship']['max_interval_minutes'] == 19
    assert body['relationship']['randomness_level'] == 'medium'
    assert body['relationship']['phrase_send_order'] == 'random'

    listed = client.get('/api/ops/group-atmosphere/role-bindings').json()
    assert listed['count'] == 10
    assert listed['relationship_count'] == 1
    relationship = listed['relationships'][0]
    assert relationship['role_key'] == 'auto-id-community_seed'
    assert relationship['account_key'] == 'atmosphere-indo-10'
    assert len(relationship['groups']) == 10
    assert relationship['groups'][0]['group_send_permission_enabled'] is True

    too_many = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'auto-id-community_seed',
        'account_key': 'atmosphere-indo-10',
        'group_indexes': list(range(11)),
    })
    assert too_many.status_code == 400
    assert too_many.json()['detail'] == 'role_binding_groups_limit_10'



def test_group_atmosphere_role_bridge_persists_randomness_and_sorted_send_order(monkeypatch):
    client = make_client()
    client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '印尼活跃气氛号',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['第一条', '第二条', '第三条'],
        'enabled': True,
    })
    account = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-indo-order',
        'account_name': '印尼排序测试号',
        'region': '印尼',
        'groups': [{'target_group': 'order-group@g.us', 'group_name': '排序群', 'enabled': True}],
        'enabled': True,
    })
    assert account.status_code == 200

    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'auto-id-community_seed',
        'account_key': 'atmosphere-indo-order',
        'group_indexes': [0],
        'randomness_level': 'high',
        'phrase_send_order': 'sorted',
        'daily_max_messages': 10,
        'min_interval_minutes': 0,
        'worker_base_url': 'http://worker.local',
    })
    assert created.status_code == 200
    relationship = created.json()['relationship']
    assert relationship['randomness_level'] == 'high'
    assert relationship['phrase_send_order'] == 'sorted'

    listed = client.get('/api/ops/group-atmosphere/role-bindings').json()
    row = listed['rows'][0]
    assert row['randomness_level'] == 'high'
    assert row['phrase_send_order'] == 'sorted'
    assert listed['relationships'][0]['groups'][0]['phrase_send_order'] == 'sorted'

    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        return FakeSendResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)
    binding_id = row['binding_id']
    first = client.post(f'/api/ops/group-atmosphere/role-bindings/{binding_id}/trigger')
    second = client.post(f'/api/ops/group-atmosphere/role-bindings/{binding_id}/trigger')
    assert first.status_code == 200
    assert second.status_code == 200
    sent_texts = [item['message_text'] for item in sent[:2]]
    assert len(sent_texts) == 2
    assert len(set(sent_texts)) == 2
    assert set(sent_texts).issubset({'第一条', '第二条', '第三条'})


def test_manual_role_binding_trigger_persists_binding_config_even_without_worker_base_url(monkeypatch):
    client = make_client()
    seed_role_and_account(client)
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append({'url': url, 'json': json})
        return FakeSendResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)

    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'auto-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'enabled': True,
        'auto_speaking_enabled': True,
        'group_send_permission_enabled': True,
        'daily_max_messages': 5,
        'min_interval_minutes': 0,
        'worker_base_url': '',
    })
    assert created.status_code == 200
    binding_id = created.json()['bindings'][0]['binding_id']

    triggered = client.post(f'/api/ops/group-atmosphere/role-bindings/{binding_id}/trigger')
    assert triggered.status_code == 200
    body = triggered.json()
    assert body['binding_id'] == binding_id
    assert body['result_code'] != 'group_atmosphere_config_not_found'
    assert body['result_code'] in {'sent', 'dry_run'}
    assert body['sent'] is True

    config_name = f'binding-{binding_id}'
    dispatched_again = client.post('/api/ops/group-atmosphere/dispatch-once', json={
        'config_name': config_name,
        'trigger_type': 'manual_role_bridge_regression',
    })
    assert dispatched_again.status_code == 200
    again_body = dispatched_again.json()
    assert again_body['result_code'] != 'group_atmosphere_config_not_found'
    assert again_body['result_code'] in {'sent', 'dry_run'}
    if body['result_code'] == 'sent' or again_body['result_code'] == 'sent':
        assert sent


def test_manual_role_phrase_append_returns_newest_phrase_first_in_role_pool():
    client = make_client()
    first = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '印尼活跃气氛号',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['Kak grup aktif info admin semangat ngobrol bareng semua.'],
        'enabled': True,
    })
    assert first.status_code == 200
    second = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '印尼活跃气氛号',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['zzz terbaru manual'],
        'enabled': True,
    })
    assert second.status_code == 200

    pool = client.get('/api/ops/group-atmosphere/candidate-pool')
    assert pool.status_code == 200
    rows = pool.json()['rows']
    role_row = next(row for row in rows if row['config_name'] == 'auto-id-community_seed')

    assert [item['text'] for item in role_row['candidates'][:2]] == [
        'zzz terbaru manual',
        'Kak grup aktif info admin semangat ngobrol bareng semua.',
    ]


def test_group_atmosphere_page_matches_role_modal_and_bridge_card_ux():
    client = make_client()
    page = client.get('/ops/group-atmosphere')
    assert page.status_code == 200
    html = page.text
    # 话术角色弹窗里的角色类型下拉必须和备选话术区分类标签同名，避免一个叫活跃气氛号、另一个叫气氛活跃型。
    role_modal = html.split('id="ga_role_editor_modal"', 1)[1].split('id="ga_learning_account_modal"', 1)[0]
    assert '<button type="button" id="ga_new_role_btn">新增话术角色</button>' in html
    assert '手动新增话术' not in html
    account_modal = html.split('id="ga_editor_modal"', 1)[1].split('id="ga_role_editor_modal"', 1)[0]
    for option in [
        '<option value="community_seed">气氛活跃型</option>',
        '<option value="faq_helper">解惑答疑型</option>',
        '<option value="newcomer_guide">教程引导型</option>',
        '<option value="motivation_admin">激励运营型</option>',
    ]:
        assert option in role_modal
        assert option in account_modal
    assert '活跃气氛号' not in account_modal
    assert '新人引导号' not in account_modal
    assert 'FAQ答疑号' not in account_modal
    assert '激励运营号' not in account_modal
    assert '已装载话术' not in role_modal
    assert 'ga-role-pool-row' in html
    assert 'ga-role-pool-source' in html
    assert 'ga-role-pool-type' not in html
    assert '${esc(rolePhraseSourceLabel(p))}' in html
    assert '${esc(roleLabel(p.role_positioning||\'\'))}' not in html
    assert 'function rolePhraseSourceLabel' in html
    assert 'ga-role-manual-phrases' in html
    assert 'rolePhraseMatchesSelectedType' in html
    assert "ga_role_positioning.addEventListener('change'" in html
    assert 'id="ga_role_randomness_level"' not in role_modal
    assert 'id="ga_role_daily_max"' not in role_modal
    assert 'id="ga_role_min_interval"' not in role_modal
    assert 'id="ga_role_max_interval"' not in role_modal
    assert 'id="ga_role_effective_time"' not in role_modal
    assert '话术随机性' not in role_modal
    assert '每日上限' not in role_modal
    assert '生效时间' not in role_modal
    assert 'ga_bridge_daily_max' in html
    assert 'ga_bridge_min_interval' in html
    assert 'ga_bridge_max_interval' in html
    assert 'id="ga_save_manual_role_phrases_btn"' in role_modal
    assert '保存文案' in role_modal
    assert 'saveRoleManualPhrases' in html
    assert 'collectManualRolePhrases' in html
    assert 'function optimisticInsertManualRolePhrases' in html
    assert 'optimisticInsertManualRolePhrases(' in html
    assert 'window.__gaRoleEditorSelectedTexts=new Set([...previousSelected,...savedPhrases])' in html
    assert "document.getElementById('ga_role_phrases').value=''" in html
    assert 'renderRolePhrasePool(savedPhrases);renderCandidatePool(window.__gaCandidateRows||[])' in html
    assert 'Promise.all([loadCandidatePool(),loadRoleBridge()])' in html
    assert "document.querySelector('#ga_role_phrase_pool .ga-role-pool-row.is-newly-saved')" in html
    assert "savedRow.scrollIntoView({block:'nearest',inline:'nearest'})" in html
    role_manual_script = html.split('async function saveRoleManualPhrases()', 1)[1].split('async function saveRoleEditor()', 1)[0]
    assert 'await reloadAll()' not in role_manual_script
    assert 'closeRoleEditor()' not in role_manual_script
    role_editor_script = html.split('async function saveRoleEditor()', 1)[1].split('function renderBridgeFormOptions()', 1)[0]
    assert 'ga_role_phrases' not in role_editor_script
    assert 'closeRoleEditor()' in role_editor_script
    assert role_editor_script.index('await reloadAll()') < role_editor_script.index('closeRoleEditor()')

    assert 'id="ga_new_role_btn"' in html
    assert 'id="ga_role_editor_modal"' in html
    assert html.index('id="ga_accounts_card"') < html.index('id="ga_new_account_btn"')
    assert html.index('id="ga_new_account_btn"') < html.index('id="ga_accounts"')
    assert '发言机器人配置' in html
    assert 'openRoleEditor' in html
    assert '从话术备选区选择话术' in html or '话术备选区' in html
    assert 'ga_role_phrase_pool' in html
    assert 'ga-bridge-layout' in html
    assert 'renderBridgeRelationships' in html
    assert 'toggleBridgeGroupPermission' in html
    assert '最多10群/关系' not in html
    assert '最大间隔秒' in html
    bridge_modal = html.split('id="ga_bridge_modal"', 1)[1].split('id="ga_editor_modal"', 1)[0]
    assert '开启自动发言' in html
    assert '立即一键发言' in html
    assert '<h2>话术角色</h2>' in html
    assert '检查可发送' not in bridge_modal
    assert 'id="ga_run_scheduler_btn"' not in bridge_modal
    assert '发言时间段' in bridge_modal
    assert 'type="time" id="ga_bridge_window_start"' in bridge_modal
    assert 'type="time" id="ga_bridge_window_end"' in bridge_modal
    assert 'value="00:00"' in bridge_modal
    assert 'value="23:59"' in bridge_modal
    assert 'collectBridgeAllowedWindows' in html
    assert 'allowed_windows:collectBridgeAllowedWindows()' in html


def test_group_atmosphere_candidate_pool_uses_config_role_when_template_role_missing():
    client = make_client()
    service = client.app.state.service
    now = '2026-05-19T00:00:00+00:00'
    template_pool = [
        {
            'template_id': 'tpl-faq-1',
            'candidate_id': 'cand-faq-1',
            'text': 'Halo kak, kirim ID ke admin ya',
            'role_positioning': 'community_seed',
            'source_type': 'upload_file',
            'safe_to_send': True,
            'enabled': True,
        }
    ]
    with service.db.connect() as conn:
        conn.execute(
            """
            INSERT INTO whatsapp_group_atmosphere_configs (
                config_name, enabled, account_key, target_group, group_name, language, timezone,
                worker_base_url, daily_max_messages, min_interval_minutes, max_interval_minutes,
                allowed_windows, template_pool, mention_reply_enabled, faq_rules, status, updated_at
            ) VALUES (?, 0, ?, ?, ?, 'id', 'UTC', '', 4, 60, 240, '[]', ?, 1, '[]', 'candidate_pool', ?)
            """,
            ('auto-id-faq_helper', 'auto-id-faq_helper', 'auto-id-faq_helper', '印尼 · 解惑答疑型', json.dumps(template_pool, ensure_ascii=False), now),
        )
        conn.commit()

    rows = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    assert len(rows) == 1
    assert rows[0]['role_positioning'] == 'faq_helper'
    assert rows[0]['candidates'][0]['role_positioning'] == 'faq_helper'


def test_group_atmosphere_bridge_edit_modal_prefills_existing_relationship_options():
    client = make_client()
    page = client.get('/ops/group-atmosphere')
    assert page.status_code == 200
    html = page.text

    bridge_script = html.split('function renderBridgeGroupChoices()', 1)[1].split('async function mountSelectedRole()', 1)[0]
    edit_script = html.split('function clearBridgeModalForm()', 1)[1].split('async function deleteBridgeRelationship', 1)[0]
    bridge_renderer = html.split('function renderBridgeRelationships(bindings)', 1)[1].split('async function toggleBridgeRelationshipAuto', 1)[0]

    assert 'window.__gaBridgeEditingRelationship' in bridge_script
    assert 'editingTargets' in bridge_script
    assert 'editingTargets.has(target)' in bridge_script
    assert '${checked}' in bridge_script
    assert 'function applyBridgeRelationshipToForm(rel)' in html
    assert "renderUnifiedRegionOptions('ga_bridge_region',rel?.region||'')" in edit_script
    assert "ga_bridge_auto_speaking" in edit_script
    assert "ga_bridge_daily_max" in edit_script
    assert "ga_bridge_min_interval" in edit_script
    assert "ga_bridge_max_interval" in edit_script
    assert "ga_bridge_randomness_level" in edit_script
    assert "ga_bridge_phrase_send_order" in edit_script
    assert "applyBridgeAllowedWindows(rel?.allowed_windows||[])" in edit_script
    assert "applyBridgeAllowedWindows([])" in edit_script
    assert 'window.__gaRoleRelationships' in edit_script
    assert 'openBridgeModal(rel||null)' in edit_script
    assert "editBridgeRelationship('${esc(rel.relationship_key||rel.role_key||'')}')" in bridge_renderer


def test_group_atmosphere_page_removes_teaching_copy_and_prioritizes_account_operations():
    client = make_client()
    page = client.get('/ops/group-atmosphere')
    assert page.status_code == 200
    html = page.text

    assert html.index('id="ga_role_bridge_card"') < html.index('id="ga_accounts_card"')
    assert html.index('id="ga_accounts_card"') < html.index('id="ga_role_library_card"')
    assert '群聊天助手' in html
    assert '群聊天助手 · 话术角色分发控制台' not in html
    assert '结构重点：话术角色是容器' not in html
    assert '发送判定优先级' not in html
    assert '发言桥接区' in html
    assert '选择话术角色' in html
    assert '目标群组' in html
    assert '发言机器人配置' in html
    assert '话术生成区' in html
    assert 'id="ga_learning_upload_card"' in html
    assert '<h2>话术学习</h2>' not in html
    assert '<h2>发送日志</h2>' not in html
    assert '/api/ops/group-atmosphere/logs' not in html
    assert 'id="ga_editor_modal"' in html
    assert 'ga-bridge-layout' in html

    removed_copy = [
        '按“话术角色 → WhatsApp群组”的桥接关系分发，群权限关闭时不会发送。',
        '桥接关系保存后，下方生成“桥接关系1/2/3...”卡片',
        '配置生产群内的静默学习号，每天读取聊天内容，学习结果只进入候选话术池。',
        '接口：/api/ops/group-atmosphere/learning-accounts',
        '开启账号和群后，后台会按每日上限与间隔自动随机发送。',
        '话术角色由上方分发桥接挂载到群组',
        '暂无话术角色，点击“新增话术角色”。',
        '话术池暂无候选，可先用话术学习导入，或右侧手动输入。',
        '上传话术文件学习',
        '入口常驻',
        '上传群聊天记录或话术文件，系统生成候选话术；默认进入话术库。',
        '发送判定优先级',
        '角色承载语种、定位、语气和默认策略',
        '后台会按间隔持续检查',
        '发送前预览',
        '预览</button>',
        '生成预览',
    ]
    for copy in removed_copy:
        assert copy not in html

    assert '群聊天助手' in html
    assert '桥接区优先' not in html
    assert '最多10群/关系' not in html


def test_group_atmosphere_page_matches_next_product_iteration_requirements():
    client = make_client()
    page = client.get('/ops/group-atmosphere')
    assert page.status_code == 200
    html = page.text

    assert 'id="ga_bridge_modal"' in html
    assert 'id="ga_bridge_region"' in html
    assert 'id="ga_close_bridge_modal_btn"' in html
    assert '新增桥接' in html
    assert '编辑桥接' in html
    assert '删除桥接' in html
    assert '一键发言' in html
    assert '国家/地区' in html
    assert '话术角色' in html
    assert '自动发言' in html
    assert 'ga-bridge-field-grid' in html
    assert 'ga-bridge-frequency-grid' in html
    assert 'ga-bridge-footer' in html
    assert 'ga-bridge-footer-actions' in html
    assert '状态：<span id="ga_scheduler_status"' in html
    assert '#ga_bridge_modal_card .modal-close{height:36px!important' in html
    assert '检查可发送</button><button type="button" id="ga_mount_role_btn">保存桥接' in html
    assert '每日上限' in html
    assert '最小间隔秒' in html
    assert '最大间隔秒' in html
    assert '最小间隔分钟' not in html
    assert '最大间隔分钟' not in html
    assert '暂无同地区可用发言机器人' in html
    assert '当前群发言关闭，保存后桥接默认关闭' in html
    assert 'data-ga-bridge-group-permission-off' in html
    assert 'display:flex;align-items:center;gap:8px;min-height:38px' in html
    assert 'width:16px!important;height:16px!important' in html
    assert 'toggleBridgeRelationshipAuto' in html
    assert '自动发言：开' in html
    assert '自动发言：关' in html
    assert 'white-space:nowrap;">${esc(bridgeTodayText(g,rel))}</span>' in html
    assert 'white-space:nowrap;flex:0 0 auto;">${statusText}</span>' in html

    assert '账号用途' not in html
    assert '运行状态' not in html
    assert '登录状态' not in html
    assert 'account-title-row' in html
    assert '已登录·生效中' in html
    assert '#ga_accounts .group-card{padding:8px 10px!important' in html
    assert '${health}' in html
    assert 'Robot card responsive 3-column layout' in html
    assert '#ga_accounts .account-card-grid,' in html
    assert '#ga_learning_accounts.ga-learning-card-list' in html
    assert '#ga_role_library_card #ga_role_library' in html
    assert 'grid-template-columns:repeat(3,minmax(0,1fr))!important' in html
    assert 'grid-template-columns:repeat(2,minmax(0,1fr))!important' in html
    account_renderer = html.split('function renderAccounts(rows)', 1)[1].split('function openManualSendModal', 1)[0]
    assert '真实群名' not in account_renderer
    assert '群链接' not in account_renderer
    assert '生效状态' not in account_renderer
    assert 'group-card-link' in account_renderer
    assert 'g.target_group' in account_renderer
    assert '群名待探测' in html

    assert '角色定位' in html
    assert '国家' in html
    assert '装载话术' in html
    assert 'deleteGroupAtmosphereRole' in html

    assert '<h2>话术生成区</h2>' in html
    assert '话术上传区' in html
    assert '学习机器人区' in html
    assert 'id="ga_chat_file"' in html
    assert 'id="ga_upload_chat_btn"' in html
    assert '/api/ops/group-atmosphere/chat-records/auto-learn' in html
    assert 'await file.text()' in html
    assert 'showTip(`已解析 ${data.file_count||files.length} 个文件，入库 ${data.imported_count||0} 条，生成 ${candidateCount} 条备选话术`' in html
    assert "setLocalFeedback('ga_upload_result'" not in html
    assert '下一步接入解析入库' not in html
    assert '后端学习接口待接入文件解析' not in html
    assert '新增学习机器人' in html
    assert 'id="ga_learning_account_modal"' in html
    assert 'id="ga_open_learning_bot_modal_btn"' in html
    assert 'id="ga_learning_group_links"' in html
    assert 'data-ga-learning-group-enabled' in html
    assert '学习：开' in html
    assert '学习：关' in html
    assert 'toggleLearningGroupEnabled' in html
    assert '群学习：开' in html
    assert '群学习：关' in html
    assert '>学习已开启<' not in html
    assert 'id="ga_add_learning_group_link_btn"' in html
    assert 'id="ga_save_learning_bot_btn"' in html
    assert '自动学习已开启' not in html
    assert '自动学习已关闭' not in html
    assert 'ga-learning-switch' not in html
    assert '学习生效中' not in html
    assert 'openLearningResultModal' in html
    assert 'groupLearningResultItemsByRole' in html
    assert 'ga-learning-result-group' in html
    assert 'ga_learning_result_modal' in html
    assert '实时学习</button>' in html
    assert 'learnOnceLearningBot' in html
    assert '立即学习一次</button>' not in html
    assert '立即学习</button>' not in html

    assert '正在生成学习机器人二维码…' not in html

    assert '<h2>话术备选区</h2>' in html
    assert '气氛活跃型' in html
    assert '解惑答疑型' in html
    assert '教程引导型' in html
    assert 'data-ga-candidate-select' in html
    assert 'function toggleCandidateTranslation(configName,candidateId)' in html
    assert 'data-zh-text' in html
    assert '候选翻译' in html
    assert '保存自定义' in html
    assert '加入角色' in html
    assert '保存至此话术角色' not in html
    assert '保存至话术角色' not in html
    assert 'data-ga-candidate-role-select' not in html
    assert 'saveSelectedCandidatesToRole' in html
    assert 'id="ga_candidate_target_role_select"' in html
    assert 'id="ga_batch_add_candidates_to_role_btn"' in html


def test_group_atmosphere_iteration_fixes_feedback_learning_isolation_and_candidate_save_to_role():
    client = make_client()
    page = client.get('/ops/group-atmosphere')
    assert page.status_code == 200
    html = page.text

    # 学习机器人必须与发言 WhatsApp 资源池隔离，不从 __gaAccounts 里拿发言账号。
    assert 'ga_learning_account_name' in html
    assert '每次最多读取消息数' not in html
    assert '每天最多读取消息数' not in html
    assert '不是每天发送条数' not in html
    assert 'ga_learning_daily_time' not in html
    assert 'ga_learning_max_messages' not in html
    assert "function openLearningBotModal" in html
    assert "function addLearningGroupLinkRow" in html
    assert "function deleteLearningBot" in html
    assert 'const rows=window.__gaLearningAccounts||[]' in html
    assert 'const rows=window.__gaAccounts||[];sel.innerHTML' not in html
    assert 'ga-learning-head-actions' in html
    assert 'ga-learning-edit-btn' in html
    assert '<span class=\"pill ${loginCls}\">${esc(loginText)}</span><button type=\"button\" class=\"secondary ga-learning-edit-btn\"' in html
    assert '<div class=\"inline-actions\"><button type=\"button\" class=\"secondary\" onclick=\"openLearningBotModal' not in html
    assert '<span>当前已学习生成 ${learnedCount} 条文案</span><a href="javascript:void(0)" class="ga-learning-detail-link"' in html
    assert '<button type="button" class="ga-learning-summary"' not in html

    # 话术角色保存后要刷新角色/桥接/备选下拉，而不是只关弹窗。
    assert 'await loadRoleBridge();renderCandidatePool(window.__gaCandidateRows||[])' in html
    assert '话术角色已保存，列表已更新' in html

    # 话术备选区改成低高度单行编辑；表头选择目标角色后批量加入，单行按钮改为删除。
    assert 'id="ga_candidate_target_role_select"' in html
    assert 'id="ga_batch_add_candidates_to_role_btn"' in html
    assert '#ga_candidate_list_card>.ga-proto-head{align-items:center!important;gap:12px!important;min-height:36px!important;margin-bottom:12px!important;}' in html
    assert '.ga-pool-filter-row select{width:auto!important;max-width:180px!important;height:34px!important;min-height:34px!important;' in html
    assert '.ga-pool-filter-row button{height:34px!important;min-height:34px!important;' in html
    assert '#ga_candidate_list_card>.ga-proto-head{flex-direction:row!important;align-items:center!important;justify-content:space-between!important}' in html
    assert 'saveSelectedCandidatesToRole' in html
    assert '请先选择话术角色' in html
    assert 'candidate-row-compact' in html
    assert 'data-ga-candidate-text' in html
    assert '<textarea data-ga-candidate-text' not in html
    assert 'deleteCandidateFromPool' in html
    delete_candidate_script = html.split('async function deleteCandidateFromPool', 1)[1].split('async function enableCandidate', 1)[0]
    assert 'await loadCandidatePool();await loadRoleBridge();renderCandidatePool(window.__gaCandidateRows||[])' in delete_candidate_script
    assert 'data-ga-enable-candidate' not in html
    assert 'id="ga_batch_add_candidates_to_role_btn">加入角色</button>' in html

    # 所有新按钮/区域都要有可见反馈，不只依赖顶部 ga_action_feedback。
    assert 'ga_learning_result' in html
    assert 'ga_candidate_result' in html
    assert 'setLocalFeedback' in html


def test_group_atmosphere_role_delete_keeps_related_bindings_as_invalid_relationships():
    client = make_client()
    client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'delete-role-id-community_seed',
        'role_name': '待删除角色',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['Halo kak'],
        'enabled': True,
    })
    client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'delete-role-account',
        'account_name': '删除角色测试号',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'groups': [{'target_group': 'delete-role-group@g.us', 'group_name': '删除测试群', 'enabled': True}],
        'enabled': True,
    })
    bind = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'delete-role-id-community_seed',
        'group_targets': ['delete-role-group@g.us'],
    })
    assert bind.status_code == 200
    before = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    assert any(r['role_key'] == 'delete-role-id-community_seed' for r in before)

    deleted = client.delete('/api/ops/group-atmosphere/roles/delete-role-id-community_seed')

    assert deleted.status_code == 200
    assert deleted.json()['deleted'] is True
    assert deleted.json()['kept_bindings'] is True
    assert deleted.json()['affected_bindings'] == 1
    after = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    assert all(r['role_key'] != 'delete-role-id-community_seed' for r in after)
    binding_payload = client.get('/api/ops/group-atmosphere/role-bindings').json()
    bindings = binding_payload['rows']
    assert any(b['role_key'] == 'delete-role-id-community_seed' for b in bindings)
    kept = next(b for b in bindings if b['role_key'] == 'delete-role-id-community_seed')
    assert kept['role_deleted'] is True
    assert kept['distribution_status'] == '角色被删除'
    relationships = binding_payload['relationships']
    rel = next(r for r in relationships if r['role_key'] == 'delete-role-id-community_seed')
    assert rel['role_deleted'] is True
    assert rel['distribution_status'] == '角色被删除'


def test_group_atmosphere_role_delete_keeps_candidate_pool_phrases():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '临时角色容器',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['Halo kak, jangan lupa kirim ID ya'],
        'source_type': 'manual',
        'safe_to_send': True,
        'enabled': True,
    })
    assert created.status_code == 200
    assert any(r['role_key'] == 'auto-id-community_seed' for r in client.get('/api/ops/group-atmosphere/roles').json()['rows'])

    deleted = client.delete('/api/ops/group-atmosphere/roles/auto-id-community_seed')

    assert deleted.status_code == 200
    assert deleted.json()['deleted'] is True
    assert deleted.json()['kept_candidate_pool'] is True
    assert all(r['role_key'] != 'auto-id-community_seed' for r in client.get('/api/ops/group-atmosphere/roles').json()['rows'])
    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    texts = [c['text'] for row in pool for c in row['candidates']]
    assert 'Halo kak, jangan lupa kirim ID ya' in texts


def test_candidate_pool_selected_phrases_can_be_added_to_chosen_matching_role_and_reused():
    client = make_client()
    source = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '候选来源',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['Halo kak, kirim ID ke admin ya'],
        'source_type': 'upload_file',
        'safe_to_send': False,
        'enabled': False,
    })
    assert source.status_code == 200
    empty_role = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-empty-id-community_seed',
        'role_name': '空角色容器',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': [],
        'enabled': True,
    })
    assert empty_role.status_code == 200
    assert empty_role.json()['role']['phrase_count'] == 0
    pool_row = next(row for row in client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows'] if row['config_name'] == 'auto-id-community_seed')
    candidate_id = pool_row['candidates'][0]['candidate_id']

    added = client.post('/api/ops/group-atmosphere/candidate-pool/add-to-role', json={
        'role_key': 'role-empty-id-community_seed',
        'source_config_name': 'auto-id-community_seed',
        'candidate_ids': [candidate_id],
    })

    assert added.status_code == 200
    assert added.json()['added_count'] == 1
    role = next(r for r in client.get('/api/ops/group-atmosphere/roles').json()['rows'] if r['role_key'] == 'role-empty-id-community_seed')
    assert role['phrase_count'] == 1
    pool_again = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    assert any(c['text'].startswith('Halo kak, kirim ID ke admin ya') for row in pool_again for c in row['candidates'])

    mismatch_role = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-faq-id-faq_helper',
        'role_name': '答疑角色',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'faq_helper',
        'phrases': [],
    })
    assert mismatch_role.status_code == 200
    mismatch = client.post('/api/ops/group-atmosphere/candidate-pool/add-to-role', json={
        'role_key': 'role-faq-id-faq_helper',
        'source_config_name': 'auto-id-community_seed',
        'candidate_ids': [candidate_id],
    })
    assert mismatch.status_code == 400


def test_group_atmosphere_page_deletes_role_through_delete_api_not_only_unbind():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    assert "DELETE" in html
    assert "/api/ops/group-atmosphere/roles/${encodeURIComponent(roleKey)}" in html
    assert "话术角色已删除" in html
    assert "角色被删除" in html
    assert "请编辑桥接更换角色" in html


def test_group_atmosphere_buttons_have_local_feedback_targets_for_modals_and_sections():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    assert 'const gaActionLocalTargets=' in html
    expected_pairs = {
        "'保存桥接关系':'ga_role_bridge_result'",
        "'删除桥接':'ga_role_bridge_result'",
        "'保存话术角色':'ga_role_editor_result'",
        "'保存学习机器人':'ga_learning_result'",
        "'加入话术角色':'ga_candidate_result'",
        "'删除备选话术':'ga_candidate_result'",
        "'自动发言检查':'ga_scheduler_result'",
        "'发送群消息':'ga_send_result'",
        "'保存':'ga_session_status'",
    }
    for marker in expected_pairs:
        assert marker in html
    assert "'上传学习':'ga_upload_result'" not in html
    assert 'setLocalFeedback(localTarget,`${label}失败：${err.message||err}`' in html
    assert 'setLocalFeedback(localTarget,`${label}成功`' in html


def test_group_atmosphere_load_json_surfaces_plain_text_errors_as_feedback():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    assert 'try{data=text?JSON.parse(text):{}}catch(_){data={detail:text}}' in html
    assert "throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||data))" in html


def test_group_atmosphere_run_action_does_not_clobber_specific_local_result_and_manual_send_throws_on_failed_send():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    assert "const currentLocalText=localEl?localEl.textContent:''" in html
    assert "if(localEl&&(!currentLocalText||currentLocalText===`${label}中…`))setLocalFeedback(localTarget,`${label}成功`" in html
    assert "if(!data.sent)throw new Error(data.result_reason||data.result_code||'发送失败')" in html




def test_group_atmosphere_bridge_interval_values_are_seconds(monkeypatch):
    client = make_client()
    client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '印尼活跃气氛号',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['秒级间隔测试'],
        'enabled': True,
    })
    client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-indo-seconds',
        'account_name': '印尼秒级测试号',
        'region': '印尼',
        'groups': [{'target_group': 'seconds-group@g.us', 'group_name': '秒级群', 'enabled': True}],
        'enabled': True,
    })
    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'auto-id-community_seed',
        'account_key': 'atmosphere-indo-seconds',
        'group_indexes': [0],
        'min_interval_minutes': 2,
        'max_interval_minutes': 2,
        'worker_base_url': 'http://worker.local',
    })
    assert created.status_code == 200
    binding_id = created.json()['bindings'][0]['binding_id']

    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        return FakeSendResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)
    first = client.post(f'/api/ops/group-atmosphere/role-bindings/{binding_id}/trigger')
    second = client.post('/api/ops/group-atmosphere/scheduler/run-due', json={})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()['sent'] is True
    result = second.json()['results'][0]
    assert result['sent'] is False
    assert result['result_code'] == 'not_due_yet'
    assert 'seconds' in result['result_reason']
    assert len(sent) == 1


def test_group_atmosphere_account_cards_remove_group_speaking_switch_and_bridge_group_cards_keep_it():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    account_renderer = html.split('function renderAccounts(rows)', 1)[1].split('function openManualSendModal', 1)[0]
    bridge_renderer = html.split('function renderBridgeRelationships(bindings)', 1)[1].split('async function toggleBridgeRelationshipAuto', 1)[0]

    assert 'toggleAtmosphereGroupEnabled' not in account_renderer
    assert '开启发言' not in account_renderer
    assert '关闭发言' not in account_renderer
    assert 'switchClass' not in account_renderer
    assert 'toggleBridgeGroupPermission' in bridge_renderer
    assert '群发言：开' in bridge_renderer
    assert '群发言：关' in bridge_renderer


def test_group_atmosphere_bridge_relationships_low_frequency_refreshes_counts_without_full_reload():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    script = html.split('<script>', 1)[1].split('</script>', 1)[0]

    assert 'const GA_BRIDGE_REFRESH_INTERVAL_MS=30000' in script
    assert 'function shouldSkipBridgeRelationshipRefresh()' in script
    assert 'async function refreshBridgeRelationshipsQuietly()' in script
    assert "loadJson('/api/ops/group-atmosphere/role-bindings')" in script
    assert 'renderBridgeRelationships(data)' in script
    assert 'setInterval(refreshBridgeRelationshipsQuietly,GA_BRIDGE_REFRESH_INTERVAL_MS)' in script
    assert "document.visibilityState==='hidden'" in script
    assert 'document.querySelector(\'.modal.is-open\')' in script
    assert 'window.__gaActionInFlight' in script
    assert 'reloadAll()' not in script.split('async function refreshBridgeRelationshipsQuietly()', 1)[1].split('async function addManualPhrases', 1)[0]


def test_group_atmosphere_account_modal_does_not_expose_frequency_fields_bridge_keeps_them():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    account_modal = html.split('<div class="modal" id="ga_editor_modal"', 1)[1].split('<div class="modal" id="ga_role_editor_modal"', 1)[0]
    bridge_modal = html.split('<div class="modal" id="ga_bridge_modal"', 1)[1].split('<div class="modal" id="ga_editor_modal"', 1)[0]
    assert 'ga_daily_max_messages' not in account_modal
    assert 'ga_min_interval_minutes' not in account_modal
    assert 'ga_max_interval_minutes' not in account_modal
    assert '每日上限' not in account_modal
    assert '最小间隔分钟' not in account_modal
    assert '最大间隔分钟' not in account_modal
    assert 'ga_bridge_daily_max' in bridge_modal
    assert 'ga_bridge_min_interval' in bridge_modal
    assert 'ga_bridge_max_interval' in bridge_modal
    assert '最小间隔秒' in bridge_modal
    assert '最大间隔秒' in bridge_modal
    script = html.split('<script>', 1)[1].split('</script>', 1)[0]
    assert 'ga_daily_max_messages.value' not in script
    assert 'ga_min_interval_minutes.value' not in script
    assert 'ga_max_interval_minutes.value' not in script


def test_group_atmosphere_account_modal_uses_clear_phrase_variation_copy_and_refined_clear_button():
    client = make_client()
    response = client.get('/ops/group-atmosphere')
    assert response.status_code == 200
    html = response.text

    account_modal = html.split('id="ga_editor_modal"', 1)[1].split('id="ga_role_editor_modal"', 1)[0]
    bridge_modal = html.split('id="ga_bridge_modal"', 1)[1].split('id="ga_editor_modal"', 1)[0]
    assert 'id="ga_randomness_level"' not in account_modal
    assert '话术变化：稳定' not in account_modal
    assert '话术变化：适中' not in account_modal
    assert '话术变化：灵活' not in account_modal
    assert '话术随机性' in bridge_modal
    assert 'id="ga_bridge_randomness_level"' in bridge_modal
    assert '随机性低' not in html
    assert '随机性中' not in html
    assert '随机性高' not in html

    assert 'id="ga_clear_form_btn"' in html
    assert 'class="clear-form-button" id="ga_clear_form_btn"' in html
    assert '>清空表单<' in html
    assert '.clear-form-button' in html
    assert 'clear-form-button:hover' in html


def test_group_atmosphere_account_group_input_and_switch_are_same_row():
    client = make_client()
    response = client.get('/ops/group-atmosphere')
    assert response.status_code == 200
    html = response.text

    assert 'class="ga-account-group-row" data-ga-group-row="1" style="display:grid"' in html
    assert 'id="ga_group_1_target"' in html
    assert 'id="ga_group_1_enabled"' in html
    assert '.ga-account-group-row{display:grid' in html
    assert 'grid-template-columns:minmax(0,1fr) 150px' in html
    assert "row.style.display='grid'" in html
    assert "row.style.display=idx<count?'grid':'none'" in html


def test_group_atmosphere_phrase_generation_sections_stack_and_upload_actions_inline():
    client = make_client()
    response = client.get('/ops/group-atmosphere')
    assert response.status_code == 200
    html = response.text

    assert 'ga-generation-stack' in html
    assert 'class="ga-upload-action-row"' in html
    assert html.index('id="ga_chat_file"') < html.index('id="ga_upload_chat_btn"') < html.index('id="ga_clear_chat_files_btn"')
    assert '#ga_candidate_card .ga-generation-grid{grid-template-columns:1fr!important' in html
    assert '.ga-generation-stack{display:grid!important;grid-template-columns:1fr!important' in html
    assert '.ga-upload-action-row{display:grid!important;grid-template-columns:minmax(0,1fr) auto auto!important' in html
    assert '#ga_learning_bot_card{display:grid!important' in html



def test_group_atmosphere_candidate_pool_hides_runtime_bindings_and_manual_needs_no_confirmation():
    client = make_client()
    seed_role_and_account(client)

    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'auto-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'enabled': True,
        'auto_speaking_enabled': True,
        'group_send_permission_enabled': True,
    })
    assert created.status_code == 200

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    assert all(not row['config_name'].startswith('binding-') for row in pool)
    community_rows = [row for row in pool if row['role_positioning'] == 'community_seed']
    assert [row['config_name'] for row in community_rows] == ['auto-id-community_seed']

    manual = client.post('/api/ops/group-atmosphere/candidate-pool/custom', json={
        'config_name': 'auto-id-community_seed',
        'text': 'Manual operator phrase should be ready immediately.',
        'role_positioning': 'community_seed',
    })
    assert manual.status_code == 200
    candidate = manual.json()['candidate']
    assert candidate['source_type'] == 'manual'
    assert candidate['safe_to_send'] is True
    assert candidate['enabled'] is True

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    row = next(item for item in pool if item['config_name'] == 'auto-id-community_seed')
    manual_rows = [item for item in row['candidates'] if item['source_type'] == 'manual']
    assert manual_rows
    assert all(item['safe_to_send'] and item['enabled'] for item in manual_rows)


def test_group_atmosphere_candidate_pool_has_batch_pending_confirmation_ui():
    client = make_client()
    response = client.get('/ops/group-atmosphere')
    assert response.status_code == 200
    html = response.text

    assert 'data-ga-pending-candidate-select' in html
    assert '全选待确认' in html
    assert '一键确认' in html
    assert '一键删除' in html
    assert 'candidateIsManual' in html
    assert 'confirmSelectedPendingCandidates' in html
    assert 'deleteSelectedPendingCandidates' in html
