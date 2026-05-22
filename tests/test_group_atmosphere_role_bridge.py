import io
import json
import re
import time

from openpyxl import Workbook

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import (
    create_app,
    GoogleTranslateCandidateTranslator,
    GroupAtmosphereAiCandidateRequest,
    GroupAtmosphereChatRecord,
    GroupAtmosphereConfigRequest,
    GroupAtmosphereImportChatRecordsRequest,
)


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


def seed_second_role(client):
    role = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-faq_helper',
        'role_name': '印尼答疑号',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'faq_helper',
        'phrases': ['Kak, kalau bingung boleh tanya admin ya.'],
        'enabled': True,
    })
    assert role.status_code == 200


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


def test_role_binding_edit_can_change_role_without_creating_duplicate_relationship():
    client = make_client()
    seed_role_and_account(client)
    seed_second_role(client)

    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'auto-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'enabled': True,
        'auto_speaking_enabled': True,
        'group_send_permission_enabled': True,
        'daily_max_messages': 5,
        'min_interval_minutes': 0,
    })
    assert created.status_code == 200
    binding_id = created.json()['bindings'][0]['binding_id']

    edited = client.post(f'/api/ops/group-atmosphere/role-bindings/{binding_id}', json={
        'role_key': 'auto-id-faq_helper',
        'auto_speaking_enabled': False,
        'daily_max_messages': 7,
    })
    assert edited.status_code == 200
    assert edited.json()['binding']['binding_id'] == binding_id
    assert edited.json()['binding']['role_key'] == 'auto-id-faq_helper'

    listed = client.get('/api/ops/group-atmosphere/role-bindings').json()
    assert listed['count'] == 1
    assert len(listed['relationships']) == 1
    assert listed['relationships'][0]['role_key'] == 'auto-id-faq_helper'
    assert listed['relationships'][0]['groups'][0]['binding_id'] == binding_id
    assert listed['relationships'][0]['daily_max_messages'] == 7
    assert listed['relationships'][0]['auto_speaking_enabled'] is False


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


def test_deleted_role_binding_cannot_continue_from_generated_binding_config(monkeypatch):
    client = make_client()
    seed_role_and_account(client)

    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'auto-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'enabled': True,
        'auto_speaking_enabled': True,
        'group_send_permission_enabled': True,
        'daily_max_messages': 5,
        'min_interval_minutes': 0,
        'worker_base_url': 'http://worker.local',
    })
    assert created.status_code == 200
    binding_id = created.json()['bindings'][0]['binding_id']
    generated_config_name = f'binding-{binding_id}'

    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append({'url': url, 'json': json, 'timeout': timeout})
        return FakeSendResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)
    first_run = client.post('/api/ops/group-atmosphere/scheduler/run-due', json={})
    assert first_run.status_code == 200
    assert first_run.json()['sent_count'] == 1
    assert client.get('/api/ops/group-atmosphere/configs').json()['rows'][0]['config_name'] == generated_config_name

    deleted = client.delete(f'/api/ops/group-atmosphere/role-bindings/{binding_id}')
    assert deleted.status_code == 200
    configs = client.get('/api/ops/group-atmosphere/configs').json()['rows']
    generated = next(row for row in configs if row['config_name'] == generated_config_name)
    assert generated['enabled'] is False
    assert generated['next_due_at'] in {'', None}

    sent.clear()
    second_run = client.post('/api/ops/group-atmosphere/scheduler/run-due', json={})
    assert second_run.status_code == 200
    assert second_run.json()['sent_count'] == 0
    assert sent == []


def test_scheduler_never_falls_back_to_stale_generated_binding_configs(monkeypatch):
    client = make_client()
    seed_role_and_account(client)

    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'auto-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'enabled': True,
        'auto_speaking_enabled': True,
        'group_send_permission_enabled': True,
        'daily_max_messages': 5,
        'min_interval_minutes': 0,
        'worker_base_url': 'http://worker.local',
    })
    assert created.status_code == 200
    binding_id = created.json()['bindings'][0]['binding_id']
    generated_config_name = f'binding-{binding_id}'

    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append({'url': url, 'json': json, 'timeout': timeout})
        return FakeSendResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)
    first_run = client.post('/api/ops/group-atmosphere/scheduler/run-due', json={})
    assert first_run.status_code == 200
    assert first_run.json()['sent_count'] == 1

    deleted = client.delete(f'/api/ops/group-atmosphere/role-bindings/{binding_id}')
    assert deleted.status_code == 200
    resurrected = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': generated_config_name,
        'enabled': True,
        'account_key': 'atmosphere-indo-01',
        'target_group': 'group-a@g.us',
        'group_name': '印尼A群',
        'language': 'id',
        'timezone': 'UTC',
        'worker_base_url': 'http://worker.local',
        'daily_max_messages': 5,
        'min_interval_minutes': 0,
        'max_interval_minutes': 0,
        'template_pool': [{
            'template_id': 'tpl-1',
            'category': 'community_seed',
            'text': 'Halo kak, stale config should not send.',
            'enabled': True,
            'safe_to_send': True,
        }],
        'status': 'enabled',
    })
    assert resurrected.status_code == 200

    sent.clear()
    second_run = client.post('/api/ops/group-atmosphere/scheduler/run-due', json={})
    assert second_run.status_code == 200
    assert second_run.json()['sent_count'] == 0
    assert sent == []


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


def test_manual_phrases_rejects_empty_role_key_and_empty_phrase_payload():
    client = make_client()

    response = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': '',
        'role_name': '',
        'phrases': [],
    })

    assert response.status_code == 400
    assert response.json()['detail'] == 'role_key_or_phrases_required'
    assert client.get('/api/ops/group-atmosphere/roles').json()['rows'] == []


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
    roles_after = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    role_summary = next(row for row in roles_after if row['role_key'] == 'auto-id-faq_helper')
    assert [c['text'] for c in role_summary['candidates']] == texts


def test_role_editor_frontend_uses_replace_save_and_dedupes_pool_rows():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    assert 'replace_role_phrases:true' in html
    assert "source_type:'role_save'" in html
    assert 'const seen=new Set()' in html
    assert 'seen.has(key)' in html


def test_role_editor_phrase_pool_has_select_all_toggle():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text

    assert 'data-ga-role-pool-select-all="1"' in html
    assert '全选当前话术' in html
    assert 'function setRolePhrasePoolSelection' in html
    assert 'function updateRolePhraseSelectAllState' in html
    assert 'setRolePhrasePoolSelection(this.checked)' in html
    assert 'selectAll.indeterminate' in html
    assert 'ga-role-pool-toolbar' in html
    assert 'ga-role-pool-select-all' in html


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


def test_auto_learn_upload_rejects_single_file_over_30mb():
    client = make_client()
    oversized = 'A' * (30 * 1024 * 1024 + 1)
    try:
        client.app.state.service.auto_learn_group_atmosphere_chat_records(
            files=[{'filename': 'too-large.txt', 'content': oversized}]
        )
    except HTTPException as exc:
        assert exc.status_code == 413
        assert exc.detail == 'upload_file_too_large_30mb'
    else:
        raise AssertionError('expected 30MB upload limit rejection')


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
    assert 'gmn' not in joined
    assert 'istilah grup yang sering muncul' not in joined


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


def test_candidate_pool_image_phrase_exposes_media_fields_and_icon_marker():
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
            {
                'candidate_id': 'image-1',
                'text': 'Halo kak lihat gambar ini ya',
                'source_role': 'community_seed',
                'source_type': 'manual_upload',
                'safe_to_send': True,
                'enabled': True,
                'customized': True,
                'asset_type': 'image_caption',
                'media_id': 'gamedia_test_1',
                'media_path': '/tmp/test-image.jpg',
                'media_mime_type': 'image/jpeg',
                'media_filename': 'promo.jpg',
            }
        ],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'candidate_pool',
    })
    assert created.status_code == 200

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    candidate = next(row for row in pool if row['config_name'] == 'auto-id-community_seed')['candidates'][0]
    assert candidate['asset_type'] == 'image_caption'
    assert candidate['media_id'] == 'gamedia_test_1'
    assert candidate['media_filename'] == 'promo.jpg'
    assert candidate['media_preview_url'].endswith('/api/ops/group-atmosphere/media-assets/gamedia_test_1/preview')

    html = client.get('/ops/group-atmosphere').text
    assert 'function candidateHasMedia' in html
    assert 'function candidateMediaIcon' in html
    assert 'data-ga-candidate-media-icon="1"' in html
    assert "ga-candidate-text-wrap ${hasMedia?'has-media':''}" in html
    assert 'aria-label="带图片"' in html


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


def test_group_atmosphere_candidate_pool_reorder_keeps_combined_pool_order_across_storage_configs():
    client = make_client()
    for config_name, candidate_id, text in [
        ('auto-id-community_seed', 'upload-a', '第一条跨来源话术'),
        ('auto-id-community_seed-learn', 'learn-b', '第二条跨来源话术'),
        ('auto-id-community_seed-manual', 'manual-c', '第三条跨来源话术'),
    ]:
        created = client.post('/api/ops/group-atmosphere/configs', json={
            'config_name': config_name,
            'enabled': False,
            'account_key': config_name,
            'target_group': config_name,
            'group_name': '自动学习素材库-印尼',
            'language': 'id',
            'daily_max_messages': 0,
            'min_interval_minutes': 120,
            'template_pool': [
                {
                    'candidate_id': candidate_id,
                    'text': text,
                    'role_positioning': 'community_seed',
                    'source_role': 'community_seed',
                    'source_type': 'role_save' if candidate_id == 'manual-c' else 'upload_file',
                    'safe_to_send': True,
                    'enabled': True,
                }
            ],
            'faq_rules': [],
            'worker_base_url': '',
            'status': 'candidate_pool',
        })
        assert created.status_code == 200

    reordered_auto = client.post('/api/ops/group-atmosphere/candidate-pool/reorder', json={
        'config_name': 'auto-id-community_seed',
        'candidate_ids': ['upload-a'],
        'candidate_orders': {'learn-b': 0, 'upload-a': 1, 'manual-c': 2},
    })
    reordered_learn = client.post('/api/ops/group-atmosphere/candidate-pool/reorder', json={
        'config_name': 'auto-id-community_seed-learn',
        'candidate_ids': ['learn-b'],
        'candidate_orders': {'learn-b': 0, 'upload-a': 1, 'manual-c': 2},
    })
    reordered_manual = client.post('/api/ops/group-atmosphere/candidate-pool/reorder', json={
        'config_name': 'auto-id-community_seed-manual',
        'candidate_ids': ['manual-c'],
        'candidate_orders': {'learn-b': 0, 'upload-a': 1, 'manual-c': 2},
    })
    assert reordered_auto.status_code == 200
    assert reordered_learn.status_code == 200
    assert reordered_manual.status_code == 200

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    candidates = next(row for row in pool if row['role_positioning'] == 'community_seed')['candidates']
    ids = [item['candidate_id'] for item in candidates if item['candidate_id'] in {'upload-a', 'learn-b', 'manual-c'}]
    assert ids == ['learn-b', 'upload-a', 'manual-c']
    manual_candidate = next(item for item in candidates if item['candidate_id'] == 'manual-c')
    assert manual_candidate['source_type'] == 'role_save'
    assert manual_candidate['source_label'] == '人工写入'


def test_group_atmosphere_candidate_pool_move_to_other_phrase_type_preserves_phrase_and_source_config():
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
        'template_pool': [{
            'candidate_id': 'move-a',
            'text': '这条话术要移动到答疑类型',
            'role_positioning': 'community_seed',
            'source_role': 'community_seed',
            'source_type': 'manual_upload',
            'safe_to_send': True,
            'enabled': True,
            'sort_order': 3,
        }],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'candidate_pool',
    })
    assert created.status_code == 200

    moved = client.post('/api/ops/group-atmosphere/candidate-pool/move-type', json={
        'config_name': 'auto-id-community_seed',
        'candidate_ids': ['move-a'],
        'target_role_positioning': 'faq_helper',
    })
    assert moved.status_code == 200
    body = moved.json()
    assert body['moved_count'] == 1
    assert body['source_config_name'] == 'auto-id-community_seed'
    assert body['target_role_positioning'] == 'faq_helper'
    assert body['target_config_name'] == 'auto-id-faq_helper'

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    community = next((row for row in pool if row['role_positioning'] == 'community_seed'), None)
    assert not community or all(item['candidate_id'] != 'move-a' for item in community['candidates'])
    faq = next(row for row in pool if row['role_positioning'] == 'faq_helper')
    moved_item = next(item for item in faq['candidates'] if item['candidate_id'] == 'move-a')
    assert moved_item['text'] == '这条话术要移动到答疑类型'
    assert moved_item['role_positioning'] == 'faq_helper'
    assert moved_item['source_role'] == 'faq_helper'
    assert moved_item['category'] == 'faq_helper'
    assert moved_item['source_config_name'] == 'auto-id-faq_helper'


def test_group_atmosphere_candidate_delete_falls_back_to_source_config_from_combined_pool():
    client = make_client()
    for config_name, candidate_id, text in [
        ('auto-id-community_seed', 'upload-a', '第一条展示配置话术'),
        ('auto-id-community_seed-manual', 'manual-c', '第二条真实来源话术'),
    ]:
        created = client.post('/api/ops/group-atmosphere/configs', json={
            'config_name': config_name,
            'enabled': False,
            'account_key': config_name,
            'target_group': config_name,
            'group_name': '自动学习素材库-印尼',
            'language': 'id',
            'daily_max_messages': 0,
            'min_interval_minutes': 120,
            'template_pool': [{
                'candidate_id': candidate_id,
                'text': text,
                'role_positioning': 'community_seed',
                'source_role': 'community_seed',
                'source_type': 'manual_upload' if candidate_id == 'manual-c' else 'upload_file',
                'safe_to_send': True,
                'enabled': True,
            }],
            'faq_rules': [],
            'worker_base_url': '',
            'status': 'candidate_pool',
        })
        assert created.status_code == 200

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    row = next(item for item in pool if item['role_positioning'] == 'community_seed')
    assert row['config_name'] == 'auto-id-community_seed'
    manual_candidate = next(item for item in row['candidates'] if item['candidate_id'] == 'manual-c')
    assert manual_candidate['source_config_name'] == 'auto-id-community_seed-manual'
    assert manual_candidate['source_label'] == '人工写入'

    deleted = client.delete('/api/ops/group-atmosphere/candidate-pool/auto-id-community_seed/manual-c')
    assert deleted.status_code == 200
    assert deleted.json()['config_name'] == 'auto-id-community_seed-manual'

    remaining = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    remaining_ids = [item['candidate_id'] for row in remaining for item in row.get('candidates', [])]
    assert 'manual-c' not in remaining_ids
    assert 'upload-a' in remaining_ids


def test_group_atmosphere_candidate_pool_page_deletes_by_source_config_name():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    assert "deleteCandidateFromPool('${esc(c.source_config_name||c.config_name||r.config_name)}','${esc(c.candidate_id)}')" in html


def test_group_atmosphere_candidate_pool_page_exposes_drag_sort_controls():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text

    assert 'draggable="true"' in html
    assert 'data-ga-candidate-drag-handle' in html
    assert '拖动排序' in html
    assert 'function onCandidateDragStart' in html
    assert 'function onCandidateDrop' in html
    assert 'function moveSelectedCandidatePriority' in html
    assert 'selectedUsableCandidateIdsForConfig(configName)' in html
    assert 'moveCandidatePriority' not in html
    assert '/api/ops/group-atmosphere/candidate-pool/reorder' in html
    assert '排序已保存' in html
    assert 'candidate_orders:globalOrder' in html
    assert 'source_config_name||c.config_name||configName' in html
    assert 'data-source-config-name' in html
    assert 'moveSelectedCandidatePriority(\'${esc(r.config_name)}\',-1)' in html
    assert 'moveSelectedCandidatePriority(\'${esc(r.config_name)}\',1)' in html
    assert '${candidateDraftRow(r,role)}${usableBlock}${more}${pendingBlock}</div>' in html


def test_group_atmosphere_candidate_pool_usable_list_has_select_all_checkbox():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text

    assert 'data-ga-usable-select-all="1"' in html
    assert 'function setUsableCandidateSelectionForConfig' in html
    assert 'function selectedUsableCandidateIdsForConfig' in html
    assert 'function moveSelectedCandidatePriority' in html
    assert 'function restoreUsableCandidateSelectionForConfig' in html
    assert 'const preservedSelection=selectedUsableCandidateIdsForConfig(configName)' in html
    assert 'restoreUsableCandidateSelectionForConfig(configName,preservedSelection)' in html
    assert 'usableSelectAll.indeterminate' in html
    assert '[data-ga-candidate-select][data-config-name=' in html
    assert 'setUsableCandidateSelectionForConfig(\'${esc(r.config_name)}\',this.checked)' in html
    assert '<span>可用话术</span>' in html
    assert 'ga-candidate-bulk-move-actions' in html
    assert 'data-ga-move-type-select' in html
    assert '移动到类型' in html
    assert 'function moveSelectedCandidatesToType' in html
    assert '/api/ops/group-atmosphere/candidate-pool/move-type' in html


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


def test_learning_account_learn_once_falls_back_to_group_id_when_invite_link_resolution_fails(monkeypatch):
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-link-fallback',
        'account_name': '学习链接兜底号',
        'region': '印尼',
        'language': 'id',
        'enabled': True,
        'worker_base_url': 'http://learning-worker.local',
        'group_links': [{
            'target_group': 'https://chat.whatsapp.com/IJI3J0yUDkS4salNC19T5a',
            'group_id': '120363425401663814@g.us',
            'group_name': '6️⃣🥇Grup Elite Linky kelompok 2',
            'enabled': True,
        }],
        'target_role_keys': ['auto-id-community_seed'],
    })
    assert created.status_code == 200

    fetch_calls = []

    class FetchResponse:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body

        def json(self):
            return self._body

    def fake_post(url, json=None, timeout=None):
        fetch_calls.append({'url': url, 'json': json, 'timeout': timeout})
        if json['target_group'].startswith('https://chat.whatsapp.com/'):
            return FetchResponse(500, {'result_reason': f"group not found: {json['target_group']}"})
        return FetchResponse(200, {
            'status': 'success',
            'result_code': 'messages_fetched',
            'group_id': '120363425401663814@g.us',
            'group_name': '6️⃣🥇Grup Elite Linky kelompok 2',
            'records': [
                {'sender': 'user1', 'text': 'Halo kak, grup rame banget hari ini', 'created_at': '2026-05-21T06:20:22Z', 'message_id': 'msg-new-fallback'},
            ],
            'next_cursor': {'last_message_id': 'msg-new-fallback', 'last_message_at': '2026-05-21T06:20:22Z'},
        })

    monkeypatch.setattr('app.main.requests.post', fake_post)
    learned = client.post('/api/ops/group-atmosphere/learning-accounts/learn-link-fallback/learn-once', json={})

    assert learned.status_code == 200
    assert [call['json']['target_group'] for call in fetch_calls] == [
        '120363425401663814@g.us',
    ]
    refreshed = client.get('/api/ops/group-atmosphere/learning-accounts').json()['rows'][0]
    assert refreshed['group_links'][0]['group_id'] == '120363425401663814@g.us'
    assert refreshed['group_links'][0]['group_name'] == '6️⃣🥇Grup Elite Linky kelompok 2'
    assert refreshed['group_links'][0]['last_learned_message_id'] == 'msg-new-fallback'

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    role = next(row for row in pool if row['config_name'] == 'auto-id-community_seed')
    learned_candidates = [candidate for candidate in role['candidates'] if candidate['source_type'] == 'learning_account']
    assert learned_candidates
    assert role['source_types'] == ['learning_account']
    assert role['enabled_candidate_count'] == 0
    assert all(candidate['safe_to_send'] is False for candidate in learned_candidates)
    assert all(candidate['enabled'] is False for candidate in learned_candidates)
    assert all(candidate['quality_status'] == 'pending_review' for candidate in learned_candidates)

    enabled = client.post('/api/ops/group-atmosphere/candidate-pool/enable', json={
        'config_name': 'auto-id-community_seed',
        'candidate_ids': [learned_candidates[0]['candidate_id']],
    })
    assert enabled.status_code == 200
    pool_after_confirm = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    role_after_confirm = next(row for row in pool_after_confirm if row['config_name'] == 'auto-id-community_seed')
    confirmed = next(candidate for candidate in role_after_confirm['candidates'] if candidate['candidate_id'] == learned_candidates[0]['candidate_id'])
    assert confirmed['safe_to_send'] is True
    assert confirmed['enabled'] is True
    assert confirmed['quality_status'] == 'manual_approved'


def test_learning_account_existing_unconfirmed_candidates_remain_pending_in_candidate_pool():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '印尼 · 气氛活跃型',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['Kak, jangan malu ngobrol di grup ya. Saling sapa biar suasana makin hidup.'],
        'source_type': 'learning_account',
        'safe_to_send': True,
        'enabled': True,
    })
    assert created.status_code == 200

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    role = next(row for row in pool if row['config_name'] == 'auto-id-community_seed')
    candidate = role['candidates'][0]

    assert candidate['source_type'] == 'learning_account'
    assert candidate['safe_to_send'] is False
    assert candidate['enabled'] is False
    assert candidate['quality_status'] == 'pending_review'
    assert role['enabled_candidate_count'] == 0


def test_learning_account_learn_once_with_no_new_worker_records_returns_empty_success(monkeypatch):
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-empty-01',
        'account_name': '学习空结果号',
        'region': '印尼',
        'language': 'id',
        'enabled': True,
        'worker_base_url': 'http://learning-worker.local',
        'group_links': [{
            'target_group': 'group-a@g.us',
            'group_name': '印尼A群',
            'enabled': True,
            'last_learned_message_id': 'msg-last',
            'last_learned_message_at': '2026-05-15T02:59:00Z',
        }],
        'target_role_keys': ['auto-id-community_seed'],
    })
    assert created.status_code == 200

    fetch_calls = []

    class EmptyFetchResponse:
        status_code = 200

        def json(self):
            return {
                'status': 'success',
                'result_code': 'messages_fetched',
                'records': [],
            }

    def fake_post(url, json=None, timeout=None):
        fetch_calls.append({'url': url, 'json': json, 'timeout': timeout})
        return EmptyFetchResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)
    learned = client.post('/api/ops/group-atmosphere/learning-accounts/learn-empty-01/learn-once', json={})

    assert learned.status_code == 200
    body = learned.json()
    assert body['ok'] is True
    assert body['result_code'] == 'no_new_records'
    assert body['read_count'] == 0
    assert body['candidate_count'] == 0
    assert body['last_result_summary']['result_code'] == 'no_new_records'
    assert fetch_calls == [
        {
            'url': 'http://learning-worker.local/fetch-group-messages',
            'json': {
                'target_group': 'group-a@g.us',
                'limit': 300,
                'after_message_id': 'msg-last',
                'after_timestamp': '2026-05-15T02:59:00Z',
            },
            'timeout': 30,
        },
        {
            'url': 'http://learning-worker.local/fetch-group-messages',
            'json': {
                'target_group': 'group-a@g.us',
                'limit': 300,
            },
            'timeout': 30,
        },
    ]


def test_learning_account_learn_once_retries_without_cursor_when_worker_misses_new_records(monkeypatch):
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-cursor-gap',
        'account_name': '学习游标兜底号',
        'region': '印尼',
        'language': 'id',
        'enabled': True,
        'worker_base_url': 'http://learning-worker.local',
        'group_links': [{
            'target_group': 'group-a@g.us',
            'group_name': '印尼A群',
            'enabled': True,
            'last_learned_message_id': 'msg-old-not-in-window',
            'last_learned_message_at': '2026-05-15T02:59:00Z',
        }],
        'target_role_keys': ['auto-id-community_seed'],
    })
    assert created.status_code == 200

    fetch_calls = []

    class FetchResponse:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(url, json=None, timeout=None):
        fetch_calls.append({'url': url, 'json': json, 'timeout': timeout})
        if json and json.get('after_message_id'):
            return FetchResponse({'status': 'success', 'result_code': 'messages_fetched', 'records': [], 'next_cursor': None})
        return FetchResponse({
            'status': 'success',
            'result_code': 'messages_fetched',
            'records': [
                {'sender': 'admin', 'text': 'Jgn lupa krm ID dan kode ke admin ya kak.', 'created_at': '2026-05-15T03:01:00Z', 'message_id': 'msg-new-1'},
                {'sender': 'user', 'text': 'Makasih kak', 'created_at': '2026-05-15T02:40:00Z', 'message_id': 'msg-old-ignored'},
            ],
            'next_cursor': {'last_message_id': 'msg-new-1', 'last_message_at': '2026-05-15T03:01:00Z'},
        })

    monkeypatch.setattr('app.main.requests.post', fake_post)
    learned = client.post('/api/ops/group-atmosphere/learning-accounts/learn-cursor-gap/learn-once', json={})

    assert learned.status_code == 200
    body = learned.json()
    assert body['read_count'] == 1
    assert body['candidate_count'] >= 1
    assert [call['json'] for call in fetch_calls] == [
        {
            'target_group': 'group-a@g.us',
            'limit': 300,
            'after_message_id': 'msg-old-not-in-window',
            'after_timestamp': '2026-05-15T02:59:00Z',
        },
        {
            'target_group': 'group-a@g.us',
            'limit': 300,
        },
    ]
    refreshed = client.get('/api/ops/group-atmosphere/learning-accounts').json()['rows'][0]
    assert refreshed['group_links'][0]['last_learned_message_id'] == 'msg-new-1'
    assert refreshed['group_links'][0]['last_learned_message_at'] == '2026-05-15T03:01:00Z'


def test_learning_account_learn_once_starts_runtime_when_snapshot_is_stopped(monkeypatch):
    client = make_client()
    service = client.app.state.service
    created = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-start-runtime',
        'account_name': '学习启动Runtime',
        'region': '印尼',
        'language': 'id',
        'enabled': True,
        'group_links': [{'target_group': 'group-a@g.us', 'group_name': '印尼A群', 'enabled': True}],
        'target_role_keys': ['auto-id-community_seed'],
    })
    assert created.status_code == 200

    starts = []
    monkeypatch.setattr(service, '_build_whatsapp_approval_runtime_state', lambda key, **kwargs: {
        'account_key': key,
        'active': False,
        'base_url': '',
        'source': 'dedicated',
    })

    def fake_start(account_key, reset=False):
        starts.append({'account_key': account_key, 'reset': reset})
        return {
            'runtime': {'account_key': account_key, 'active': True, 'base_url': 'http://learning-worker.local', 'source': 'dedicated'},
            'session': {'login_verified': True, 'login_check_status': 'passed'},
        }

    class FetchResponse:
        status_code = 200

        def json(self):
            return {
                'status': 'success',
                'result_code': 'messages_fetched',
                'records': [
                    {'sender': 'admin', 'text': 'Jgn lupa krm ID dan kode ke admin ya kak.', 'created_at': '2026-05-15T03:01:00Z', 'message_id': 'msg-new-1'},
                ],
                'next_cursor': {'last_message_id': 'msg-new-1', 'last_message_at': '2026-05-15T03:01:00Z'},
            }

    fetch_calls = []
    cached_sessions = []

    def fake_post(url, json=None, timeout=None):
        fetch_calls.append({'url': url, 'json': json, 'timeout': timeout})
        return FetchResponse()

    monkeypatch.setattr(service, 'start_whatsapp_approval_account_session', fake_start)
    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', lambda base_url: {
        'status': 'warm',
        'ready': True,
        'authenticated': True,
        'approval_client': {'status': 'warm', 'ready': True, 'authenticated': True, 'client_id': service._whatsapp_approval_session_client_id('learn-start-runtime'), 'auth_path': str(service._whatsapp_approval_session_auth_path('learn-start-runtime'))},
    })
    monkeypatch.setattr(service, '_cache_whatsapp_approval_session_snapshot', lambda account_key, session_state, worker_health: cached_sessions.append({'account_key': account_key, 'session': dict(session_state), 'health': dict(worker_health)}))
    monkeypatch.setattr('app.main.requests.post', fake_post)

    learned = client.post('/api/ops/group-atmosphere/learning-accounts/learn-start-runtime/learn-once', json={})

    assert learned.status_code == 200
    assert starts == [{'account_key': 'learn-start-runtime', 'reset': False}]
    assert fetch_calls and fetch_calls[0]['url'] == 'http://learning-worker.local/fetch-group-messages'
    assert cached_sessions and cached_sessions[0]['account_key'] == 'learn-start-runtime'
    assert cached_sessions[0]['session']['login_verified'] is True
    assert learned.json()['candidate_count'] >= 1


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
    assert body['candidate_count'] == 2
    assert body['last_result_summary']['candidate_count'] == 2
    learned_items = body['last_result_summary']['items']
    assert {item['role_positioning'] for item in learned_items} == {'newcomer_guide', 'motivation_admin'}
    assert all(item['text'] for item in learned_items)

    rows = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    by_config = {row['config_name']: row for row in rows}
    all_text = '\n'.join(candidate['text'] for row in rows for candidate in row['candidates']).lower()
    original_case_text = '\n'.join(candidate['text'] for row in rows for candidate in row['candidates'])
    assert 'wkwk' not in all_text
    assert 'pada kerja mungkin' not in all_text
    assert 'jgn' in all_text
    assert 'krm' in all_text
    assert 'dmn' not in all_text
    assert 'aku bingung' not in all_text
    assert 'kirim ID dan kode' not in original_case_text
    assert 'krm ID dan kode' in original_case_text
    assert 'auto-id-faq_helper' not in by_config
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


def test_learning_account_list_uses_authenticated_cached_worker_health_over_stale_pending_session(monkeypatch):
    client = make_client()
    service = client.app.state.service
    created = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-stale-cache',
        'account_name': '学习bot缓存登录',
        'region': '印尼',
        'enabled': True,
        'group_links': [{'target_group': 'cache-group@g.us', 'group_name': '缓存学习群'}],
    })
    assert created.status_code == 200
    client_id = service._whatsapp_approval_session_client_id('learn-stale-cache')
    auth_path = str(service._whatsapp_approval_session_auth_path('learn-stale-cache'))
    meta = {
        'pid': 43211,
        'port': 59998,
        'base_url': 'http://127.0.0.1:59998',
        'auth_path': auth_path,
        'client_id': client_id,
        'last_session_checked_ts': time.time(),
        'last_session_state': {
            'account_key': 'learn-stale-cache',
            'status': 'idle',
            'ready': False,
            'authenticated': False,
            'login_verified': False,
            'login_check_status': 'pending_runtime',
            'login_check_message': '正在准备登录会话，请稍候。',
        },
        'last_worker_health': {
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
        },
    }
    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: meta if key == 'learn-stale-cache' else {})
    monkeypatch.setattr(service, '_write_whatsapp_approval_runtime_meta', lambda key, payload: payload)
    monkeypatch.setattr(service, '_pid_running', lambda pid: True)
    monkeypatch.setattr(service, '_group_atmosphere_allow_test_worker_urls', False)
    health_calls = []
    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', lambda base_url: health_calls.append(base_url) or {})

    rows = client.get('/api/ops/group-atmosphere/learning-accounts').json()['rows']

    assert health_calls == []
    row = rows[0]
    assert row['session']['login_verified'] is True
    assert row['login_verified'] is True
    assert row['login_check_message'] == '账号已登录，可以正常使用。'


def test_learning_account_list_does_not_treat_cached_login_as_active_when_runtime_stopped(monkeypatch):
    client = make_client()
    service = client.app.state.service
    created = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-stopped-cache',
        'account_name': '学习bot进程已停',
        'region': '印尼',
        'enabled': True,
        'group_links': [{'target_group': 'cache-group@g.us', 'group_name': '缓存学习群'}],
    })
    assert created.status_code == 200
    client_id = service._whatsapp_approval_session_client_id('learn-stopped-cache')
    auth_path = str(service._whatsapp_approval_session_auth_path('learn-stopped-cache'))
    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: {
        'pid': 49999,
        'port': 59998,
        'base_url': 'http://127.0.0.1:59998',
        'auth_path': auth_path,
        'client_id': client_id,
        'last_session_checked_ts': time.time(),
        'last_session_state': {
            'account_key': 'learn-stopped-cache',
            'status': 'warm',
            'ready': True,
            'authenticated': True,
            'login_verified': True,
            'login_check_status': 'passed',
            'login_check_message': '账号已登录，可以正常使用。',
        },
        'last_worker_health': {
            'status': 'warm',
            'ready': True,
            'authenticated': True,
            'approval_client': {'status': 'warm', 'ready': True, 'authenticated': True, 'client_id': client_id, 'auth_path': auth_path},
        },
    } if key == 'learn-stopped-cache' else {})
    monkeypatch.setattr(service, '_pid_running', lambda pid: False)
    monkeypatch.setattr(service, '_whatsapp_approval_has_local_auth_session', lambda key: key == 'learn-stopped-cache')
    monkeypatch.setattr(service, '_group_atmosphere_allow_test_worker_urls', False)

    rows = client.get('/api/ops/group-atmosphere/learning-accounts').json()['rows']
    row = rows[0]

    assert row['runtime']['active'] is False
    assert row['session']['login_verified'] is False
    assert row['login_verified'] is False
    assert row['session']['login_state'] == 'recoverable'
    assert row['session']['login_check_status'] == 'runtime_recoverable'
    assert row['session']['qr_available'] is False
    assert row['session']['can_show_qr'] is False
    assert '待扫码' not in row['session']['login_check_message']
    assert row['session']['login_check_message'] == '登录态可恢复，点击实时学习恢复。'


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
    assert phrases[2] not in translations
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


def test_whatsapp_account_group_limits_match_resource_plan(monkeypatch):
    client = make_client()
    service = client.app.state.service
    monkeypatch.setattr(service, 'list_whatsapp_approval_area_options', lambda: {'options': [{'value': '印尼', 'label': '印尼'}], 'source_options': []})
    monkeypatch.setattr(service, '_list_notify_robot_options', lambda: [{'profile_name': 'approval-bot', 'label': '审批bot'}])
    monkeypatch.setattr(service, '_build_whatsapp_approval_runtime_state', lambda *args, **kwargs: {'active': False})

    approval_groups = [
        {
            'link': f'https://chat.whatsapp.com/approval{i:02d}ABCDEFG1234567890',
            'group_name': f'审批群{i}',
            'area': '印尼',
            'notify_profile_name': 'approval-bot',
            'enabled': True,
        }
        for i in range(10)
    ]
    approval = client.post('/api/ops/whatsapp-approval-accounts/wa-approval-limit', json={
        'account_name': '审批账号10群',
        'responsible_type': 'registration_group',
        'group_link_bindings': approval_groups,
        'enabled': True,
    })
    assert approval.status_code == 200
    assert approval.json()['account']['group_count'] == 10
    approval_over_limit = client.post('/api/ops/whatsapp-approval-accounts/wa-approval-limit-over', json={
        'account_name': '审批账号11群',
        'responsible_type': 'registration_group',
        'group_link_bindings': approval_groups + [{**approval_groups[-1], 'link': 'https://chat.whatsapp.com/approval11'}],
        'enabled': True,
    })
    assert approval_over_limit.status_code == 400
    assert 'at most 10 groups' in approval_over_limit.text

    learning_groups = [{'target_group': f'learn-{i}@g.us', 'group_name': f'学习群{i}', 'enabled': True} for i in range(10)]
    learning = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-limit-01',
        'account_name': '学习账号10群',
        'region': '印尼',
        'groups': learning_groups,
        'enabled': True,
    })
    assert learning.status_code == 200
    assert len(learning.json()['account']['group_links']) == 10
    learning_over_limit = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-limit-02',
        'account_name': '学习账号11群',
        'region': '印尼',
        'groups': learning_groups + [{'target_group': 'learn-11@g.us'}],
        'enabled': True,
    })
    assert learning_over_limit.status_code == 400
    assert 'at most 10 groups' in learning_over_limit.text

    speaking_groups = [{'target_group': f'speak-{i}@g.us', 'group_name': f'发言群{i}', 'enabled': True} for i in range(5)]
    speaking = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-limit-01',
        'account_name': '发言账号5群',
        'region': '印尼',
        'groups': speaking_groups,
        'enabled': True,
    })
    assert speaking.status_code == 200
    assert speaking.json()['account']['group_count'] == 5
    speaking_over_limit = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-limit-02',
        'account_name': '发言账号6群',
        'region': '印尼',
        'groups': speaking_groups + [{'target_group': 'speak-6@g.us'}],
        'enabled': True,
    })
    assert speaking_over_limit.status_code == 400
    assert 'at most 5 groups' in speaking_over_limit.text

    production_ops = client.get('/ops/production-ops')
    assert production_ops.status_code == 200
    production_html = production_ops.text
    assert 'APPROVAL_BINDING_MAX_COUNT = 10' in production_html
    assert '最多配置10个群组' in production_html


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
    assert '__gaLatestLearningResultByKey' in html
    assert '已放入下方话术备选区的“待人工确认”' in html
    assert '没有新增文案；可能与现有话术重复或被质检过滤' in html
    assert 'openLearningResultModal(key)' in html
    assert 'Number(r.candidate_count||0)' in html
    assert '已读取${readCount}条群消息,有效消息${usefulCount}条,新增${candidateCount}条待确认文案' in html
    assert '已读取${readCount}条群消息,有效素材${usefulCount}条,生成${candidateCount}条文案' not in html
    assert '已读取 ${readCount} 条群消息，有效素材 ${usefulCount} 条，生成 ${candidateCount} 条文案' not in html
    assert '待人工确认' in html
    assert 'data-ga-learning-pending-list="1"' in html
    assert '学习机器人/上传生成的话术会出现在这里，确认后才进入可用话术。' not in html
    assert '可用话术' in html
    assert '<strong>${esc(candidateTypeLabel(r))}</strong><span class="muted" style="margin-left:8px;white-space:nowrap;">${esc(r.region||\'-\')} · 可用 ${usable.length}/${all.length} 条${pendingHint}</span>' in html
    assert '<strong>可用话术</strong><span class="pill gray">${usable.length} 条</span>' not in html
    assert '#ga_candidate_pool .group-card-title{align-items:center!important;margin-bottom:4px!important;}' in html
    assert '#ga_candidate_pool .group-card-title>.inline-actions{margin-top:0!important;align-self:flex-start!important;transform:translateY(-2px);}' in html
    assert 'style="margin:2px 0 4px;align-items:center;justify-content:space-between;gap:8px;"' in html
    assert 'style="margin-top:8px;border-top:1px solid #e5e7eb;padding-top:8px;"' in html
    assert "manual_approved'?'人工确认" not in html
    assert "status==='manual_approved'||status==='approved_manual'||status==='pending_review')return ''" in html
    assert "?'待质检'" not in html
    assert 'const GA_MAX_GROUPS=5;' in html
    assert 'const GA_LEARNING_MAX_GROUPS=10;' in html
    assert '<select id="ga_candidate_language_filter"><option value="">语言/地区</option><option value="id" selected>印尼</option>' in html
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
    assert 'saveEditedBridgeRelationship' in html
    assert "window.__gaBridgeEditingRelationship||null" in html
    assert "role-bindings/${encodeURIComponent(existing.binding_id)}" in html
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


def test_group_atmosphere_role_bridge_create_survives_stale_binding_id_after_role_switch():
    client = make_client()
    seed_role_and_account(client)
    role_b = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-community_seed-0112131',
        'role_name': '盖伦0112131',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['第二个同类型角色'],
        'enabled': True,
    })
    assert role_b.status_code == 200

    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'auto-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'auto_speaking_enabled': True,
        'min_interval_minutes': 0,
    })
    assert created.status_code == 200
    binding_id = created.json()['bindings'][0]['binding_id']

    switched = client.post(f'/api/ops/group-atmosphere/role-bindings/{binding_id}', json={
        'role_key': 'role-id-community_seed-0112131',
    })
    assert switched.status_code == 200

    recreated = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'auto-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'auto_speaking_enabled': True,
        'min_interval_minutes': 0,
    })
    assert recreated.status_code == 409
    assert recreated.json()['detail'] == 'role_binding_account_group_already_used'


def test_group_atmosphere_role_bridge_rejects_same_account_group_in_another_relationship():
    client = make_client()
    seed_role_and_account(client)
    role_b = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-community_seed-duplicate',
        'role_name': '另一个气氛角色',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['第二个角色话术'],
        'enabled': True,
    })
    assert role_b.status_code == 200

    first = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'auto-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'auto_speaking_enabled': True,
    })
    assert first.status_code == 200

    duplicate = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'role-id-community_seed-duplicate',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'auto_speaking_enabled': True,
    })
    assert duplicate.status_code == 409
    assert duplicate.json()['detail'] == 'role_binding_account_group_already_used'


def test_group_atmosphere_role_bridge_relationship_labels_keep_created_order_when_new_role_added():
    client = make_client()
    seed_role_and_account(client)
    role_b = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-community_seed-0112131',
        'role_name': '盖伦0112131',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['第二个同类型角色'],
        'enabled': True,
    })
    assert role_b.status_code == 200

    first = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'role-id-community_seed-0112131',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'auto_speaking_enabled': True,
        'min_interval_minutes': 0,
    })
    assert first.status_code == 200
    before = client.get('/api/ops/group-atmosphere/role-bindings').json()['relationships']
    assert before[0]['role_key'] == 'role-id-community_seed-0112131'
    assert before[0]['relationship_label'] == '桥接关系1'

    second = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'auto-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [1],
        'auto_speaking_enabled': True,
        'min_interval_minutes': 0,
    })
    assert second.status_code == 200
    after = client.get('/api/ops/group-atmosphere/role-bindings').json()['relationships']
    labels = {rel['role_key']: rel['relationship_label'] for rel in after}
    assert labels['role-id-community_seed-0112131'] == '桥接关系1'
    assert labels['auto-id-community_seed'] == '桥接关系2'


def test_group_atmosphere_phrase_type_manual_upload_and_move_workflow():
    client = make_client()
    created_type = client.post('/api/ops/group-atmosphere/phrase-types', json={
        'type_key': 'retention_recall',
        'type_name': '留存召回型',
        'description': '召回沉默用户',
        'enabled': True,
        'region_scope': ['印尼'],
    })
    assert created_type.status_code == 200
    types = client.get('/api/ops/group-atmosphere/phrase-types').json()['rows']
    assert any(row['type_key'] == 'retention_recall' and row['type_name'] == '留存召回型' for row in types)

    uploaded = client.post('/api/ops/group-atmosphere/phrases/manual-upload', json={
        'role_key': 'role-id-retention_recall',
        'role_name': '印尼留存召回',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'retention_recall',
        'content': 'Halo kak, kembali aktif ya.\nBonus masih tersedia.',
    })
    assert uploaded.status_code == 200
    upload_body = uploaded.json()
    assert upload_body['imported_count'] == 2
    assert upload_body['review_required'] is False

    role = client.get('/api/ops/group-atmosphere/roles').json()['rows'][0]
    phrases = role['template_pool']
    assert all(item['source_type'] == 'manual_upload' for item in phrases)
    assert all(item['safe_to_send'] and item['enabled'] for item in phrases)
    assert all(item.get('quality_status') == 'approved_manual' for item in phrases)
    pool_rows = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    uploaded_candidates = [item for row in pool_rows for item in row.get('candidates', []) if item.get('source_type') == 'manual_upload']
    assert uploaded_candidates
    assert all(item['source_label'] == '人工写入' for item in uploaded_candidates)
    assert all(item['safe_to_send'] and item['enabled'] for item in uploaded_candidates)

    target = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-community_seed',
        'role_name': '印尼气氛',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['原有气氛话术'],
        'enabled': True,
    })
    assert target.status_code == 200
    moved = client.post('/api/ops/group-atmosphere/phrases/move', json={
        'source_role_key': 'role-id-retention_recall',
        'target_role_key': 'role-id-community_seed',
        'template_ids': [phrases[0]['template_id']],
        'mode': 'move',
    })
    assert moved.status_code == 200
    assert moved.json()['moved_count'] == 1
    roles = {row['config_name']: row for row in client.get('/api/ops/group-atmosphere/roles').json()['rows']}
    assert all(item.get('template_id') != phrases[0]['template_id'] or item.get('enabled') is False for item in roles['role-id-retention_recall']['template_pool'])
    assert any(item.get('moved_from_role_key') == 'role-id-retention_recall' for item in roles['role-id-community_seed']['template_pool'])


def test_group_atmosphere_phrase_type_delete_hides_only_custom_types_and_page_has_delete_entry():
    client = make_client()
    created_type = client.post('/api/ops/group-atmosphere/phrase-types', json={
        'type_key': 'retention_recall',
        'type_name': '留存召回型',
        'enabled': True,
    })
    assert created_type.status_code == 200
    assert created_type.json()['phrase_type']['is_system'] is False

    html = client.get('/ops/group-atmosphere').text
    assert 'deleteInlinePhraseType' in html
    assert 'data-ga-delete-phrase-type' in html
    assert '系统默认话术类型不能删除' in html
    assert '/api/ops/group-atmosphere/phrase-types/${encodeURIComponent(typeKey)}' in html
    assert 'ga-candidate-tab-delete-inside' in html
    assert 'return `<button type="button" class="ga-candidate-tab ${activeCandidateRole()===role?' in html
    assert '${deleteBtn}</button>`' in html
    assert 'ga-candidate-tab-wrap' not in html

    deleted = client.delete('/api/ops/group-atmosphere/phrase-types/retention_recall')
    assert deleted.status_code == 200
    assert deleted.json()['deleted'] is True
    assert deleted.json()['phrase_type']['enabled'] is False
    assert deleted.json()['phrase_type']['type_key'] == 'retention_recall'

    visible_types = client.get('/api/ops/group-atmosphere/phrase-types').json()['rows']
    assert all(row['type_key'] != 'retention_recall' for row in visible_types)
    all_types = client.get('/api/ops/group-atmosphere/phrase-types?include_disabled=true').json()['rows']
    hidden = next(row for row in all_types if row['type_key'] == 'retention_recall')
    assert hidden['enabled'] is False

    system_delete = client.delete('/api/ops/group-atmosphere/phrase-types/community_seed')
    assert system_delete.status_code == 400
    assert system_delete.json()['detail'] == 'system_phrase_type_cannot_delete'


def test_group_atmosphere_phrase_type_rename_custom_only_and_page_has_inline_edit():
    client = make_client()
    created_type = client.post('/api/ops/group-atmosphere/phrase-types', json={
        'type_key': 'retention_recall',
        'type_name': '留存召回型',
        'enabled': True,
    })
    assert created_type.status_code == 200

    renamed = client.post('/api/ops/group-atmosphere/phrase-types/retention_recall', json={
        'type_name': '沉默召回型',
    })
    assert renamed.status_code == 200
    assert renamed.json()['phrase_type']['type_key'] == 'retention_recall'
    assert renamed.json()['phrase_type']['type_name'] == '沉默召回型'

    types = client.get('/api/ops/group-atmosphere/phrase-types').json()['rows']
    assert any(row['type_key'] == 'retention_recall' and row['type_name'] == '沉默召回型' for row in types)

    system_rename = client.post('/api/ops/group-atmosphere/phrase-types/community_seed', json={
        'type_name': '不能改名',
    })
    assert system_rename.status_code == 400
    assert system_rename.json()['detail'] == 'system_phrase_type_cannot_rename'

    html = client.get('/ops/group-atmosphere').text
    assert 'startPhraseTypeRename' in html
    assert 'handlePhraseTypeRenameKey' in html
    assert 'savePhraseTypeRename' in html
    assert 'data-ga-phrase-type-rename' in html
    assert 'ga-phrase-type-rename-input' in html
    assert '/api/ops/group-atmosphere/phrase-types/${encodeURIComponent(typeKey)}' in html
    assert "ev.key==='Enter'" in html


def test_group_atmosphere_media_asset_upload_preview_and_bind_to_phrase(tmp_path):
    media_dir = tmp_path / 'ga-media'
    client = make_client({'GROUP_ATMOSPHERE_MEDIA_DIR': str(media_dir)})
    png_bytes = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32

    uploaded = client.post('/api/ops/group-atmosphere/media-assets', files={
        'file': ('poster.png', png_bytes, 'image/png'),
    })
    assert uploaded.status_code == 200
    media = uploaded.json()['media']
    assert media['filename'] == 'poster.png'
    assert media['mime_type'] == 'image/png'
    assert media['file_size'] == len(png_bytes)
    assert media['media_path'].startswith(str(media_dir))

    duplicate = client.post('/api/ops/group-atmosphere/media-assets', files={
        'file': ('poster-copy.png', png_bytes, 'image/png'),
    })
    assert duplicate.status_code == 200
    assert duplicate.json()['media']['media_id'] == media['media_id']
    assert duplicate.json()['deduped'] is True

    listed = client.get('/api/ops/group-atmosphere/media-assets')
    assert listed.status_code == 200
    assert len(listed.json()['rows']) == 1

    preview = client.get(f"/api/ops/group-atmosphere/media-assets/{media['media_id']}/preview")
    assert preview.status_code == 200
    assert preview.headers['content-type'].startswith('image/png')
    assert preview.content == png_bytes

    uploaded_phrase = client.post('/api/ops/group-atmosphere/phrases/manual-upload', json={
        'role_key': 'role-id-community_seed',
        'role_name': '印尼气氛图文',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'content': 'Halo kak, lihat poster ini.',
        'media_id': media['media_id'],
    })
    assert uploaded_phrase.status_code == 200
    role = client.get('/api/ops/group-atmosphere/roles').json()['rows'][0]
    tpl = role['template_pool'][0]
    assert tpl['asset_type'] == 'image_caption'
    assert tpl['media_id'] == media['media_id']
    assert tpl['media_path'] == media['media_path']
    assert tpl['media_mime_type'] == 'image/png'
    assert tpl['media_filename'] == 'poster.png'


def test_group_atmosphere_manual_file_upload_supports_txt_csv_and_xlsx_template(tmp_path):
    client = make_client({'GROUP_ATMOSPHERE_MEDIA_DIR': str(tmp_path / 'ga-media')})
    uploaded = client.post('/api/ops/group-atmosphere/phrases/manual-upload-file', data={
        'role_key': 'role-id-community_seed',
        'role_name': '印尼气氛',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
    }, files={'file': ('phrases.csv', b'Halo kak\nSelamat pagi', 'text/csv')})
    assert uploaded.status_code == 200
    assert uploaded.json()['imported_count'] == 2

    wb = Workbook()
    ws = wb.active
    ws.append(['序号', '类型', '话术内容'])
    ws.append(['001', '气氛', 'Kak, jangan lupa aktif di grup ya.'])
    ws.append(['002', '答疑', 'Kalau ada pertanyaan boleh tanya admin.'])
    buf = io.BytesIO()
    wb.save(buf)

    xlsx = client.post('/api/ops/group-atmosphere/phrases/manual-upload-file', data={
        'role_key': 'role-id-xlsx-community_seed',
        'role_name': '印尼xlsx气氛',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
    }, files={'file': ('phrases.xlsx', buf.getvalue(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
    assert xlsx.status_code == 200
    assert xlsx.json()['imported_count'] == 2
    roles = {row['config_name']: row for row in client.get('/api/ops/group-atmosphere/roles').json()['rows']}
    texts = [item['text'] for item in roles['role-id-xlsx-community_seed']['template_pool']]
    assert texts == ['Kak, jangan lupa aktif di grup ya.', 'Kalau ada pertanyaan boleh tanya admin.']


def test_group_atmosphere_manual_xlsx_without_matching_header_chooses_text_rich_column(tmp_path):
    client = make_client({'GROUP_ATMOSPHERE_MEDIA_DIR': str(tmp_path / 'ga-media')})
    wb = Workbook()
    ws = wb.active
    ws.append(['001', '气氛', 'Halo kak, share pengalaman kamu di grup ya.'])
    ws.append(['002', '气氛', 'Kak, jangan sepi-sepi, ngobrol santai di sini.'])
    ws.append(['003', '气氛', 'Kalau sudah daftar, boleh cerita prosesnya lancar atau tidak.'])
    buf = io.BytesIO()
    wb.save(buf)

    xlsx = client.post('/api/ops/group-atmosphere/phrases/manual-upload-file', data={
        'role_key': 'role-id-xlsx-no-header',
        'role_name': '印尼无表头话术',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
    }, files={'file': ('phrases.xlsx', buf.getvalue(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
    assert xlsx.status_code == 200
    assert xlsx.json()['imported_count'] == 3
    role = client.get('/api/ops/group-atmosphere/roles').json()['rows'][0]
    texts = [item['text'] for item in role['template_pool']]
    assert texts == [
        'Halo kak, share pengalaman kamu di grup ya.',
        'Kak, jangan sepi-sepi, ngobrol santai di sini.',
        'Kalau sudah daftar, boleh cerita prosesnya lancar atau tidak.',
    ]
    assert not any(text.isdigit() for text in texts)


def test_group_atmosphere_dispatch_sends_image_with_caption(monkeypatch):
    client = make_client()
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        return FakeSendResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)
    config = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'image-role',
        'account_key': 'atmosphere-indo-01',
        'target_group': 'group-a@g.us',
        'group_name': '印尼A群',
        'language': 'id',
        'worker_base_url': 'http://worker.local',
        'min_interval_minutes': 0,
        'template_pool': [{
            'template_id': 'tpl-image-1',
            'text': 'Halo kak, lihat info ini.',
            'source_type': 'manual_upload',
            'safe_to_send': True,
            'enabled': True,
            'media_path': '/tmp/poster.jpg',
            'media_mime_type': 'image/jpeg',
        }],
    })
    assert config.status_code == 200
    dispatched = client.post('/api/ops/group-atmosphere/dispatch-once', json={'config_name': 'image-role'})
    assert dispatched.status_code == 200
    assert sent[0]['message_text'] == 'Halo kak, lihat info ini.'
    assert sent[0]['media_path'] == '/tmp/poster.jpg'
    assert sent[0]['media_mime_type'] == 'image/jpeg'


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
    assert "manual_upload:'人工写入'" in html
    assert "'manual_upload','custom'" in html
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
    assert 'function loginLabel(session,runtime)' in html
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
    assert '话术文件学习' in html
    assert '学习机器人区' in html
    assert 'id="ga_chat_file"' in html
    assert 'id="ga_upload_chat_btn"' in html
    assert '/api/ops/group-atmosphere/chat-records/auto-learn' in html
    assert 'await file.text()' in html
    assert 'const rejectedCount=Number(data.rejected_count||0)' in html
    assert '过滤 ${rejectedCount} 条' in html
    assert "setUploadResult(successText,'success')" in html
    assert "setUploadResult(errorText,'error')" in html
    assert "humanizeGaUploadError" in html
    assert '上传内容太大，请减少文件数量或压缩后再上传' in html
    assert '单个文件太大，最大30MB，请压缩后再上传' in html
    assert 'const GA_UPLOAD_MAX_FILE_BYTES=30*1024*1024;' in html
    assert "const GA_UPLOAD_MAX_FILE_LABEL='30MB';" in html
    assert 'Number(file.size||0)>GA_UPLOAD_MAX_FILE_BYTES' in html
    assert 'upload_file_too_large_30mb' in html
    assert '<html>' not in html.split('function humanizeGaUploadError', 1)[1].split('async function uploadChatFiles', 1)[0]
    assert "setUploadResult(loadingText)" in html
    assert "showTip(loadingText,'info',{sticky:true})" in html
    assert "if(!text||options.sticky)return" in html
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
    # 质检/待确认徽标不能挤占动作列，按钮必须作为独立动作组横向排布，避免线上出现“删除”竖排/错位。
    assert 'ga-candidate-meta-badges' in html
    assert 'ga-candidate-actions' in html
    assert '#ga_candidate_pool .ga-candidate-meta-badges{grid-column:4 / 6!important;' in html
    assert '#ga_candidate_pool .ga-candidate-actions{grid-column:5!important;display:flex!important;align-items:center!important;justify-content:flex-end!important;gap:6px!important;flex-wrap:wrap!important;' in html
    assert '#ga_candidate_pool .ga-candidate-actions>button{white-space:nowrap!important;' in html
    assert 'deleteCandidateFromPool' in html
    delete_candidate_script = html.split('async function deleteCandidateFromPool', 1)[1].split('async function enableCandidate', 1)[0]
    assert 'await loadCandidatePool();await loadRoleBridge();renderCandidatePool(window.__gaCandidateRows||[])' in delete_candidate_script
    assert 'data-ga-enable-candidate' not in html
    assert 'id="ga_batch_add_candidates_to_role_btn">加入角色</button>' in html
    # 点击“新增话术”后的人工写入草稿行必须是统一的表单卡片，不复用候选行五列布局，避免输入框和按钮挤成一团。
    assert 'ga-candidate-manual-draft-card' in html
    assert 'ga-candidate-manual-draft-main' in html
    assert 'ga-candidate-manual-draft-actions' in html
    assert '#ga_candidate_pool .ga-candidate-manual-draft-card{display:grid!important;grid-template-columns:1fr!important;' in html
    assert '#ga_candidate_pool .ga-candidate-manual-draft-main{display:grid!important;grid-template-columns:minmax(0,1fr) auto!important;' in html
    assert '#ga_candidate_pool .ga-candidate-manual-draft-card input[type="checkbox"]' not in html

    # 所有新按钮/区域都要有可见反馈，不只依赖顶部 ga_action_feedback。
    assert 'ga_learning_result' in html
    assert 'ga_candidate_result' in html
    assert 'setLocalFeedback' in html

    # 话术库入口收敛：人工上传不再选择话术角色；类型管理和图片上传并入话术备选区。
    assert 'id="ga_manual_upload_role"' not in html
    assert '新建/自动角色' not in html
    assert 'id="ga_phrase_type_key"' not in html
    assert 'id="ga_phrase_type_desc"' not in html
    assert '话术类型管理' not in html
    assert '图片素材上传' not in html
    assert 'id="ga_media_asset_list"' not in html
    assert 'ga_add_phrase_type_btn' in html
    assert 'ga_phrase_type_inline_form' in html
    assert '#ga_phrase_type_inline_form.is-open{display:flex!important;align-items:center!important;gap:10px!important;' in html
    assert '#ga_phrase_type_inline_form:not(.is-open){display:none!important;}' in html
    assert '#ga_phrase_type_inline_form button{min-width:88px!important;width:88px!important;white-space:nowrap!important;' in html
    assert "form.classList.add('is-open')" in html
    assert "form.classList.remove('is-open')" in html
    assert 'saveInlinePhraseType' in html
    assert 'ga_open_image_candidate_modal_btn' in html
    assert 'ga_image_candidate_modal' in html
    assert 'saveImageCandidate' in html
    assert '新增图片' in html


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
    assert "throw new Error(humanizeGaUploadError" in html
    assert 'function humanizeGaUploadError' in html
    assert '上传内容太大，请减少文件数量或压缩后再上传' in html
    assert '单个文件太大，最大30MB，请压缩后再上传' in html
    helper = html.split('function humanizeGaUploadError', 1)[1].split('function esc', 1)[0]
    assert '<html>' not in helper
    assert '<html' not in helper
    assert '<body' not in helper
    assert 'nginx/' not in helper


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
    assert 'bridgeGroupProductionState' in html
    assert "text:'已就绪'" in html
    assert "text:'未就绪'" in html
    assert '可投产' not in bridge_renderer
    assert '探针待确认' not in bridge_renderer
    assert '等待自动发言' not in bridge_renderer
    assert '自动发言关闭' not in bridge_renderer


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

    for index in range(1, 6):
        expected_display = 'grid' if index == 1 else 'none'
        assert f'class="ga-account-group-row" data-ga-group-row="{index}" style="display:{expected_display}"' in html
        assert f'id="ga_group_{index}_target"' in html
        assert f'id="ga_group_{index}_enabled"' in html
    assert '.ga-account-group-row{display:grid' in html
    assert 'grid-template-columns:minmax(0,1fr) 150px' in html
    assert "row.style.display='grid'" in html
    assert "row.style.display=idx<count?'grid':'none'" in html


def test_approval_and_learning_extra_group_rows_keep_consistent_template():
    client = make_client()
    group_page = client.get('/ops/group-atmosphere')
    assert group_page.status_code == 200
    group_html = group_page.text
    learning_renderer = group_html.split('function renderLearningGroupLinks', 1)[1].split('function collectLearningGroups', 1)[0]
    assert 'GA_LEARNING_MAX_GROUPS' in learning_renderer
    assert 'class="ga-learning-group-row" data-learning-group-row="${idx}"' in learning_renderer
    assert 'data-ga-learning-group-link="1"' in learning_renderer
    assert 'data-ga-learning-group-enabled="1"' in learning_renderer

    approval_page = client.get('/ops/production-ops')
    assert approval_page.status_code == 200
    approval_html = approval_page.text
    assert 'const APPROVAL_BINDING_MAX_COUNT = 10;' in approval_html
    assert "const template = document.getElementById('wa_binding_card_3');" in approval_html
    assert "card.id = `wa_binding_card_${i}`" in approval_html
    assert ".replace(/_3/g, `_${i}`)" in approval_html
    assert 'class="binding-card" id="wa_binding_card_1"' in approval_html
    assert 'class="binding-card" id="wa_binding_card_2"' in approval_html
    assert 'class="binding-card" id="wa_binding_card_3"' in approval_html


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

    roles_after_binding = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    assert all(not row['role_key'].startswith('binding-') for row in roles_after_binding)

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
    assert 'ga-pending-batch-bar' in html
    assert '待确认批量操作' in html
    assert '全选待确认' in html
    assert '全选本列表' not in html
    assert '暂无待确认' in html
    assert '一键确认' in html
    assert '删除已选话术' in html
    assert '一键删除' not in html
    assert 'candidateIsManual' in html
    assert 'selectedCandidateIdsForConfig' in html
    assert 'setCandidateSelectionForConfig' in html
    assert 'confirmSelectedPendingCandidates' in html
    assert 'deleteSelectedPendingCandidates' in html

def test_confirming_candidate_pool_does_not_create_or_expose_role_container():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'auto-id-faq_helper',
        'enabled': False,
        'account_key': 'auto-id-faq_helper',
        'target_group': 'auto-id-faq_helper',
        'group_name': '印尼 · 解惑答疑话术包',
        'language': 'id',
        'timezone': 'UTC',
        'daily_max_messages': 4,
        'min_interval_minutes': 60,
        'template_pool': [
            {'candidate_id': 'upload-1', 'text': 'Halo kak, kirim ID ke admin ya.', 'source_role': 'faq_helper', 'source_type': 'upload_file', 'safe_to_send': False, 'enabled': False},
        ],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'candidate_pool',
    })
    assert created.status_code == 200

    before_roles = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    assert all(row['role_key'] != 'auto-id-faq_helper' for row in before_roles)

    confirmed = client.post('/api/ops/group-atmosphere/candidate-pool/enable', json={
        'config_name': 'auto-id-faq_helper',
        'candidate_ids': ['upload-1'],
    })
    assert confirmed.status_code == 200
    assert confirmed.json()['plan_only'] is True

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    candidate_row = next(row for row in pool if row['config_name'] == 'auto-id-faq_helper')
    assert candidate_row['enabled_candidate_count'] == 1

    after_roles = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    assert all(row['role_key'] != 'auto-id-faq_helper' for row in after_roles)



def test_auto_learn_dedupes_same_generated_phrase_across_role_types(monkeypatch):
    client = make_client()
    service = client.app.state.service

    from app.main import GroupAtmosphereChatRecord

    records = [
        GroupAtmosphereChatRecord(sender='u1', text='halo kak tanya kode ya'),
        GroupAtmosphereChatRecord(sender='u2', text='halo kak semangat ya'),
    ]
    monkeypatch.setattr(service, '_parse_group_atmosphere_chat_export', lambda content: records)
    monkeypatch.setattr(service, '_detect_group_atmosphere_language_and_region', lambda recs: ('id', '印尼'))
    roles = iter(['faq_helper', 'community_seed'])
    monkeypatch.setattr(service, '_classify_group_atmosphere_record_role', lambda text: next(roles))
    monkeypatch.setattr(service, 'generate_group_atmosphere_ai_candidates', lambda payload: {
        'candidates': [{'text': 'Halo kak, istilah grup yang sering muncul: kak, ya, yg. Kalau bingung, tanya admin ya.'}]
    })

    result = client.post('/api/ops/group-atmosphere/chat-records/auto-learn', json={'filename': 'chat.txt', 'content': 'dummy'})
    assert result.status_code == 200

    payload = result.json()
    assert payload['rejected_count'] == 2
    assert 'meta_summary' in payload['rejected_reasons']
    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    occurrences = []
    for row in pool:
        for candidate in row['candidates']:
            if candidate['text'] == 'Halo kak, istilah grup yang sering muncul: kak, ya, yg. Kalau bingung, tanya admin ya.':
                occurrences.append((row['role_positioning'], row['config_name']))
    assert len(occurrences) == 0


def test_candidate_pool_listing_hides_existing_duplicate_phrase_across_role_types():
    client = make_client()
    duplicate_text = 'Halo kak, kalau ada yang bingung soal kode, tanya di grup ya. Admin bantu cek.'
    for config_name, role in [('auto-id-community_seed', 'community_seed'), ('auto-id-faq_helper', 'faq_helper')]:
        response = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
            'role_key': config_name,
            'role_name': role,
            'region': '印尼',
            'language': 'id',
            'role_positioning': role,
            'phrases': [duplicate_text],
            'source_type': 'upload_file',
            'safe_to_send': False,
            'enabled': False,
        })
        assert response.status_code == 200

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    occurrences = [
        (row['role_positioning'], candidate['config_name'])
        for row in pool
        for candidate in row['candidates']
        if candidate['text'] == duplicate_text
    ]
    assert len(occurrences) == 1


def test_enabling_upload_candidate_does_not_create_role():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '上传候选池',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['Halo kak, jangan malu ngobrol di grup ya.'],
        'source_type': 'upload_file',
        'safe_to_send': False,
        'enabled': False,
    })
    assert created.status_code == 200
    candidate_id = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows'][0]['candidates'][0]['candidate_id']

    enabled = client.post('/api/ops/group-atmosphere/candidate-pool/enable', json={
        'config_name': 'auto-id-community_seed',
        'candidate_ids': [candidate_id],
    })
    assert enabled.status_code == 200

    roles = client.get('/api/ops/group-atmosphere/roles').json()
    assert roles['count'] == 0
    assert roles['rows'] == []


def test_group_atmosphere_filters_meta_term_variants_as_semantic_duplicates():
    client = make_client()
    service = client.app.state.service

    variants = [
        'Halo kak, istilah grup yang sering muncul: kak, wa, ya. Kalau bingung, tanya admin ya.',
        'Halo kak, istilah grup yang sering muncul: kak, ya, yg. Kalau bingung, tanya admin ya.',
        'Halo kak, istilah grup yang sering muncul: kak, ya, aja. Kalau bingung, tanya admin ya.',
    ]

    semantic_keys = {service._normalize_group_atmosphere_semantic_phrase_key(text) for text in variants}
    assert len(semantic_keys) == 1
    assert all(not service._is_group_atmosphere_useful_candidate(text, role='community_seed') for text in variants)


def test_group_atmosphere_ai_candidates_do_not_generate_meta_term_summary_copy():
    client = make_client()
    service = client.app.state.service
    service.upsert_group_atmosphere_config(GroupAtmosphereConfigRequest(
        config_name='auto-id-community_seed',
        enabled=False,
        account_key='acct',
        target_group='group',
        group_name='group',
        language='id',
        template_pool=[],
        faq_rules=[],
        worker_base_url='',
        status='candidate_pool',
    ))
    service.import_group_atmosphere_chat_records(GroupAtmosphereImportChatRecordsRequest(
        config_name='auto-id-community_seed',
        records=[
            GroupAtmosphereChatRecord(sender='u1', text='kak wa ya yg aja'),
            GroupAtmosphereChatRecord(sender='u2', text='kak wa ya admin grup'),
        ],
    ))

    data = service.generate_group_atmosphere_ai_candidates(GroupAtmosphereAiCandidateRequest(
        config_name='auto-id-community_seed',
        topic='community_seed',
        count=10,
    ))
    texts = [row['text'] for row in data['candidates']]
    assert texts
    assert all('istilah grup yang sering muncul' not in text.lower() for text in texts)


def test_group_atmosphere_candidate_quality_gate_classifies_rejects_and_semantic_keys():
    client = make_client()
    service = client.app.state.service

    meta_a = 'Halo kak, istilah grup yang sering muncul: kak, wa, ya. Kalau bingung, tanya admin ya.'
    meta_b = 'Halo kak, istilah grup yang sering muncul: kak, ya, aja. Kalau bingung, tanya admin ya.'
    user_question = 'Kok bisa ada user yang nyariin kak boleh tau caranya supaya usernya inget terus sama kita gmna?'
    good = 'Kak, jangan malu ngobrol di grup ya. Saling sapa biar suasana makin hidup.'

    meta_quality = service._evaluate_group_atmosphere_candidate_quality(meta_a, role='community_seed', source_type='upload_file')
    question_quality = service._evaluate_group_atmosphere_candidate_quality(user_question, role='community_seed', source_type='learning_account')
    good_quality = service._evaluate_group_atmosphere_candidate_quality(good, role='community_seed', source_type='upload_file')

    assert meta_quality['decision'] == 'reject'
    assert 'meta_summary' in meta_quality['reasons']
    assert meta_quality['semantic_key'] == service._evaluate_group_atmosphere_candidate_quality(meta_b, role='community_seed', source_type='upload_file')['semantic_key']
    assert question_quality['decision'] == 'reject'
    assert 'question_like' in question_quality['reasons']
    assert good_quality['decision'] == 'accept'
    assert good_quality['quality_score'] >= 60


def test_upload_learning_source_uses_quality_gate_and_keeps_candidates_pending_review():
    client = make_client()
    good = 'Kak, jangan malu ngobrol di grup ya. Saling sapa biar suasana makin hidup.'
    rejected_meta = 'Halo kak, istilah grup yang sering muncul: kak, wa, ya. Kalau bingung, tanya admin ya.'
    rejected_question = 'Kok bisa ada user yang nyariin kak boleh tau caranya supaya usernya inget terus sama kita gmna?'

    response = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-id-community_seed',
        'role_name': '印尼气氛活跃',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': [good, rejected_meta, rejected_question],
        'source_type': 'upload_file',
        'safe_to_send': True,
        'enabled': True,
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload['added_count'] == 1
    assert payload['rejected_count'] == 2
    assert {'meta_summary', 'question_like'} <= set(payload['rejected_reasons'])

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    row = next(item for item in pool if item['config_name'] == 'auto-id-community_seed')
    assert len(row['candidates']) == 1
    candidate = row['candidates'][0]
    assert candidate['text'] == good
    assert candidate['enabled'] is False
    assert candidate['safe_to_send'] is False
    assert candidate['quality_decision'] == 'accept'
    assert candidate['quality_status'] == 'pending_review'
    assert candidate['quality_reasons'] == []
    assert candidate['semantic_key']


def test_group_atmosphere_candidate_cards_show_quality_reason_and_upload_filtered_summary():
    client = make_client()
    response = client.get('/ops/group-atmosphere')
    assert response.status_code == 200
    html = response.text

    assert 'candidateQualityReasonText' in html
    assert 'candidateQualityBadge' in html
    assert '质量原因' in html
    assert '疑似用户问题' in html
    assert '系统分析产物' in html
    assert '过滤 ${rejectedCount} 条' in html
    assert 'const rejectedCount=Number(data.rejected_count||0)' in html
