import io
import json
import re
import sys
import time
import types

from openpyxl import Workbook

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import (
    create_app,
    GoogleTranslateCandidateTranslator,
    GroupAtmosphereAiCandidateRequest,
    GroupAtmosphereChatRecord,
    GroupAtmosphereConfigRequest,
    GroupAtmosphereDispatchRequest,
    GroupAtmosphereImportChatRecordsRequest,
    Service,
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




def test_group_atmosphere_dispatch_excludes_disabled_manual_customized_templates():
    templates = Service._enabled_group_atmosphere_templates({
        'template_pool': [
            {
                'text': 'disabled manual upload should not send',
                'source_type': 'manual_upload',
                'customized': True,
                'safe_to_send': True,
                'enabled': False,
            },
            {
                'text': 'enabled manual upload can send',
                'source_type': 'manual_upload',
                'customized': True,
                'safe_to_send': True,
                'enabled': True,
            },
            {
                'text': 'pending learning candidate should not send',
                'source_type': 'learning_account',
                'safe_to_send': False,
                'enabled': True,
            },
        ]
    })

    assert [item['text'] for item in templates] == ['enabled manual upload can send']


def seed_role_and_account(client):
    role = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-id-faq_helper',
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
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-id-faq_helper',
        'auto_speaking_enabled': False,
        'daily_max_messages': 7,
    })
    assert edited.status_code == 200
    assert edited.json()['binding']['binding_id'] == binding_id
    assert edited.json()['binding']['role_key'] == 'role-id-faq_helper'

    listed = client.get('/api/ops/group-atmosphere/role-bindings').json()
    assert listed['count'] == 1
    assert len(listed['relationships']) == 1
    assert listed['relationships'][0]['role_key'] == 'role-id-faq_helper'
    assert listed['relationships'][0]['groups'][0]['binding_id'] == binding_id
    assert listed['relationships'][0]['daily_max_messages'] == 7
    assert listed['relationships'][0]['auto_speaking_enabled'] is False


def test_bridge_relationship_has_independent_trigger_speaking_switch_and_rules_are_scoped_to_relationship():
    client = make_client()
    seed_role_and_account(client)
    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'role-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'enabled': True,
        'auto_speaking_enabled': False,
        'trigger_speaking_enabled': True,
        'group_send_permission_enabled': True,
    })
    assert created.status_code == 200
    binding_id = created.json()['bindings'][0]['binding_id']

    rule = client.post('/api/ops/group-atmosphere/trigger-rules', json={
        'relationship_key': 'role-id-community_seed',
        'rule_name': 'kode 关键词回复',
        'trigger_type': 'keyword_match',
        'enabled': True,
        'conditions': {'keywords': ['kode'], 'match_type': 'contains', 'case_sensitive': False},
        'message_sequence': [{'type': 'text', 'text': 'Jangan kirim ID/Code di grup ya kak.'}],
        'delay_min_seconds': 2,
        'delay_max_seconds': 5,
        'cooldown_seconds': 60,
    })
    assert rule.status_code == 200
    assert rule.json()['rule']['relationship_key'] == 'role-id-community_seed'

    listed = client.get('/api/ops/group-atmosphere/role-bindings').json()
    rel = listed['relationships'][0]
    assert rel['auto_speaking_enabled'] is False
    assert rel['trigger_speaking_enabled'] is True
    assert rel['trigger_rule_count'] == 1
    assert rel['trigger_rule_enabled_count'] == 1
    assert rel['trigger_rule_types'] == ['keyword_match']

    updated = client.post(f'/api/ops/group-atmosphere/role-bindings/{binding_id}', json={'trigger_speaking_enabled': False})
    assert updated.status_code == 200
    assert updated.json()['binding']['trigger_speaking_enabled'] is False


def test_keyword_trigger_can_fire_when_auto_speaking_is_off_but_trigger_speaking_is_on():
    client = make_client()
    seed_role_and_account(client)
    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'role-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'enabled': True,
        'auto_speaking_enabled': False,
        'trigger_speaking_enabled': True,
        'group_send_permission_enabled': True,
    })
    assert created.status_code == 200
    client.post('/api/ops/group-atmosphere/trigger-rules', json={
        'relationship_key': 'role-id-community_seed',
        'rule_name': 'kode 关键词回复',
        'trigger_type': 'keyword_match',
        'enabled': True,
        'conditions': {'keywords': ['kode'], 'match_type': 'contains'},
        'message_sequence': [{'type': 'text', 'text': 'Kode 请走私聊客服提交。'}],
        'delay_min_seconds': 2,
        'delay_max_seconds': 5,
    })

    hit = client.post('/api/ops/group-atmosphere/inbound-message', json={
        'account_key': 'atmosphere-indo-01',
        'target_group': 'group-a@g.us',
        'sender_id': 'user-1',
        'text': 'apa kode kak',
    })
    assert hit.status_code == 200
    body = hit.json()
    assert body['should_respond'] is True
    assert body['result_code'] == 'trigger_rule_matched'
    assert body['matched_rule']['trigger_type'] == 'keyword_match'
    assert body['reply_sequence'][0]['text'] == 'Kode 请走私聊客服提交。'

    binding_id = created.json()['bindings'][0]['binding_id']
    client.post(f'/api/ops/group-atmosphere/role-bindings/{binding_id}', json={'trigger_speaking_enabled': False})
    skipped = client.post('/api/ops/group-atmosphere/inbound-message', json={
        'account_key': 'atmosphere-indo-01',
        'target_group': 'group-a@g.us',
        'sender_id': 'user-1',
        'text': 'apa kode kak',
    })
    assert skipped.status_code == 200
    assert skipped.json()['should_respond'] is False
    assert skipped.json()['result_code'] == 'trigger_speaking_disabled'


def test_trigger_rules_simple_operator_modal_markers_priority_and_image_segments():
    client = make_client()
    seed_role_and_account(client)
    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'role-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'enabled': True,
        'auto_speaking_enabled': False,
        'trigger_speaking_enabled': True,
        'group_send_permission_enabled': True,
    })
    assert created.status_code == 200
    binding_id = created.json()['bindings'][0]['binding_id']

    via_binding = client.post(f'/api/ops/group-atmosphere/role-bindings/{binding_id}/trigger-rules', json={
        'rule_name': '入群欢迎',
        'trigger_type': 'member_join',
        'enabled': True,
        'priority': 1,
        'message_sequence': [
            {'type': 'text', 'text': 'Halo kak, selamat datang ya 😊', 'delay_seconds': 3},
            {'type': 'image_text', 'text': 'Kalau ada pertanyaan boleh tanya di grup.', 'media_id': 'media-welcome', 'delay_seconds': 8},
        ],
        'cooldown_seconds': 60,
    })
    assert via_binding.status_code == 200
    join_rule = via_binding.json()['rule']
    join_rule_id = join_rule['rule_id']
    assert len(join_rule['message_sequence']) == 2
    assert join_rule['message_sequence'][1]['media_id'] == 'media-welcome'
    assert join_rule['message_sequence'][1]['delay_seconds'] == 8

    silence = client.post('/api/ops/group-atmosphere/trigger-rules', json={
        'relationship_key': 'role-id-community_seed',
        'rule_name': '群沉默破冰',
        'trigger_type': 'group_silence',
        'enabled': True,
        'priority': 2,
        'conditions': {'silence_seconds': 300},
        'message_sequence': [{'type': 'text', 'text': 'Sepi ya kak, ada yang mau ditanyakan?', 'delay_seconds': 5}],
        'cooldown_seconds': 1800,
    })
    assert silence.status_code == 200

    too_many = client.post('/api/ops/group-atmosphere/trigger-rules', json={
        'relationship_key': 'role-id-community_seed',
        'rule_name': '超过三条',
        'trigger_type': 'keyword_match',
        'conditions': {'keywords': ['kode']},
        'message_sequence': [
            {'type': 'text', 'text': '1'},
            {'type': 'text', 'text': '2'},
            {'type': 'text', 'text': '3'},
            {'type': 'text', 'text': '4'},
        ],
    })
    assert too_many.status_code == 400
    assert too_many.json()['detail'] == 'trigger_message_sequence_max_3'

    listed = client.get('/api/ops/group-atmosphere/trigger-rules?relationship_key=role-id-community_seed')
    assert listed.status_code == 200
    assert listed.json()['count'] == 2
    priorities = [row['priority'] for row in listed.json()['rows']]
    assert priorities == [1, 2]

    deleted = client.delete(f'/api/ops/group-atmosphere/trigger-rules/{join_rule_id}')
    assert deleted.status_code == 200
    assert client.get('/api/ops/group-atmosphere/trigger-rules?relationship_key=role-id-community_seed').json()['count'] == 1

    html = client.get('/ops/group-atmosphere').text
    for marker in [
        'ga_trigger_rules_modal',
        'saveTriggerRuleFromModal',
        'deleteTriggerRuleFromModal',
        'addTriggerMessageSegment',
        "flex:0 0 auto!important;min-height:0!important;overflow:visible!important;",
        "window.__gaTriggerSegments.forEach((_,idx)=>syncTriggerSegmentFromDom(idx))",
        'renderTriggerMessageSegments',
        'onTriggerSegmentPaste',
        'openTriggerSegmentMediaPreview',
        'removeTriggerSegmentMedia',
        'replaceTriggerSegmentMedia',
        'renderTriggerPriorityOptions',
        'data-ga-trigger-media-icon',
        'ga-trigger-form-grid',
        '<span>状态</span><select id="ga_trigger_rule_enabled">',
        'ga-trigger-rule-row',
        'ga-trigger-rule-actions',
        'data-ga-trigger-rule-card',
        'data-ga-trigger-rule-toggle',
        'toggleTriggerRuleEnabled',
        '新人入群',
        '关键词',
        '群冷场',
    ]:
        assert marker in html
    assert 'testTriggerRuleFromModal' not in html
    assert '<div class="mini-note ga-trigger-rule-item" onclick=' not in html
    assert '<input type="checkbox" id="ga_trigger_rule_enabled"' not in html
    assert '测试只预览，不会真实发 WhatsApp' not in html
    assert '规则只作用于当前桥接关系' not in html
    assert '手写话术，最多 3 条；可直接粘贴图片' not in html
    assert '多个关键词用逗号隔开' not in html
    assert '数字越大，优先级越低' not in html
    assert '/api/ops/group-atmosphere/trigger-rules/test' not in html


def test_internal_trigger_event_endpoint_matches_member_join_and_group_silence():
    client = make_client()
    seed_role_and_account(client)
    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'role-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'enabled': True,
        'auto_speaking_enabled': False,
        'trigger_speaking_enabled': True,
        'group_send_permission_enabled': True,
    })
    assert created.status_code == 200
    client.post('/api/ops/group-atmosphere/trigger-rules', json={
        'relationship_key': 'role-id-community_seed',
        'rule_name': '入群欢迎',
        'trigger_type': 'member_join',
        'enabled': True,
        'priority': 1,
        'message_sequence': [{'type': 'text', 'text': 'Halo kak, selamat datang ya'}],
        'cooldown_seconds': 0,
    })
    client.post('/api/ops/group-atmosphere/trigger-rules', json={
        'relationship_key': 'role-id-community_seed',
        'rule_name': '群沉默破冰',
        'trigger_type': 'group_silence',
        'enabled': True,
        'priority': 2,
        'conditions': {'silence_seconds': 120},
        'message_sequence': [{'type': 'text', 'text': 'Sepi ya kak, ada yang mau ditanyakan?'}],
        'cooldown_seconds': 0,
    })

    join = client.post('/api/internal/group-atmosphere/trigger-event', json={
        'account_key': 'atmosphere-indo-01',
        'target_group': 'group-a@g.us',
        'trigger_type': 'member_join',
        'sender_id': '62812@c.us',
        'event_payload': {'recipientIds': ['62812@c.us']},
    })
    assert join.status_code == 200
    assert join.json()['should_respond'] is True
    assert join.json()['trigger_type'] == 'member_join'
    assert join.json()['reply_sequence'][0]['text'] == 'Halo kak, selamat datang ya'

    too_early = client.post('/api/internal/group-atmosphere/trigger-event', json={
        'account_key': 'atmosphere-indo-01',
        'target_group': 'group-a@g.us',
        'trigger_type': 'group_silence',
        'event_payload': {'silence_seconds': 30},
    })
    assert too_early.status_code == 200
    assert too_early.json()['result_code'] == 'silence_threshold_not_reached'

    silence = client.post('/api/internal/group-atmosphere/trigger-event', json={
        'account_key': 'atmosphere-indo-01',
        'target_group': 'group-a@g.us',
        'trigger_type': 'group_silence',
        'event_payload': {'silence_seconds': 180},
    })
    assert silence.status_code == 200
    assert silence.json()['should_respond'] is True
    assert silence.json()['trigger_type'] == 'group_silence'
    assert silence.json()['reply_sequence'][0]['text'] == 'Sepi ya kak, ada yang mau ditanyakan?'


def test_silence_event_counts_any_normal_group_message_but_not_system_messages():
    assert Service.is_group_atmosphere_regular_group_message({'message_type': 'text', 'from_me': False}) is True
    assert Service.is_group_atmosphere_regular_group_message({'message_type': 'image', 'from_me': False}) is True
    assert Service.is_group_atmosphere_regular_group_message({'message_type': 'sticker', 'from_me': False}) is True
    assert Service.is_group_atmosphere_regular_group_message({'message_type': 'text', 'from_me': True, 'trigger_type': 'scheduled_auto'}) is True
    assert Service.is_group_atmosphere_regular_group_message({'message_type': 'system', 'event_type': 'member_join'}) is False
    assert Service.is_group_atmosphere_regular_group_message({'message_type': 'group_update'}) is False


def test_role_binding_delete_removes_bridge_from_listing_and_relationships():
    client = make_client()
    seed_role_and_account(client)

    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-id-community_seed',
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
    role = next(row for row in pool if row.get('phrase_type') == 'community_seed')
    texts = [candidate['text'] for candidate in role['candidates']]
    assert '2026/05/18 12:31 - Alice: <Media omitted>' in texts
    assert '421324123' in texts
    assert texts.count('ok') == 1
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
        'role_key': 'role-id-community_seed',
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
    role_before = next(row for row in client.get('/api/ops/group-atmosphere/roles').json()['rows'] if row['role_key'] == 'role-id-community_seed')
    assert role_before['phrase_count'] == 1

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    candidate = next(row for row in pool if row.get('phrase_type') == 'community_seed')['candidates'][0]
    deleted = client.delete(f"/api/ops/group-atmosphere/candidate-pool/{candidate['source_config_name']}/{candidate['candidate_id']}")

    assert deleted.status_code == 200
    roles_after = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    role_after = next(row for row in roles_after if row['role_key'] == 'role-id-community_seed')
    assert role_after['role_name'] == '删除候选保留角色'
    assert role_after['phrase_count'] == 0
    assert role_after['status'] == 'role_container'


def test_role_editor_save_replaces_checked_pool_phrases_without_duplicate_append():
    client = make_client()
    first = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-faq_helper',
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
    role_before = next(row for row in before if row.get('phrase_type') == 'faq_helper')
    assert [c['text'] for c in role_before['candidates']].count('hahhh yup') == 1

    save = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-faq_helper',
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
    role_after = next(row for row in after if row.get('phrase_type') == 'faq_helper')
    texts = [c['text'] for c in role_after['candidates']]
    assert texts.count('Halo kak, kalau bingung tanya admin ya') == 1
    assert texts.count('hahhh yup') == 1
    assert len(texts) == 2
    roles_after = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    role_summary = next(row for row in roles_after if row['role_key'] == 'role-id-faq_helper')
    assert set(c['text'] for c in role_summary['candidates']) == set(texts)



def test_role_editor_selection_preserves_media_candidate_when_selected_from_phrase_pool():
    client = make_client()
    source = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'auto-id-000001',
        'enabled': False,
        'account_key': 'auto-id-000001',
        'target_group': 'auto-id-000001',
        'group_name': '自动学习素材库-印尼',
        'language': 'id',
        'daily_max_messages': 0,
        'min_interval_minutes': 120,
        'template_pool': [{
            'candidate_id': 'img-lets-go',
            'template_id': 'tpl-img-lets-go',
            'text': 'lets go！kak',
            'asset_type': 'image_caption',
            'media_id': 'media-song',
            'media_path': '/tmp/song.png',
            'media_mime_type': 'image/png',
            'media_filename': 'song.png',
            'source_role': '000001',
            'role_positioning': '000001',
            'category': '000001',
            'source_type': 'manual_upload',
            'safe_to_send': True,
            'enabled': True,
        }],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'candidate_pool',
    })
    assert source.status_code == 200

    role = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-000001',
        'role_name': '盖伦000001',
        'region': '印尼',
        'language': 'id',
        'role_positioning': '000001',
        'phrases': ['lets go！kak'],
        'source_type': 'role_save',
        'replace_role_phrases': True,
        'safe_to_send': True,
        'enabled': True,
    })
    assert role.status_code == 200
    rows = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    saved = next(row for row in rows if row['role_key'] == 'role-id-000001')
    item = next(c for c in saved['candidates'] if c['text'] == 'lets go！kak')
    assert item['asset_type'] == 'image_caption'
    assert item['media_id'] == 'media-song'
    assert item['media_path'] == '/tmp/song.png'
    assert item['media_filename'] == 'song.png'


def test_candidate_image_edit_cascades_to_role_and_binding_dispatch_snapshots():
    client = make_client()
    service = client.app.state.service
    old_media = service.create_group_atmosphere_media_asset('song.png', b'old-image', 'image/png', created_by='test')['media']
    new_media = service.create_group_atmosphere_media_asset('new.png', b'new-image', 'image/png', created_by='test')['media']
    source = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'auto-id-000001',
        'enabled': False,
        'account_key': 'auto-id-000001',
        'target_group': 'auto-id-000001',
        'group_name': '自动学习素材库-印尼',
        'language': 'id',
        'daily_max_messages': 0,
        'min_interval_minutes': 120,
        'template_pool': [{
            'candidate_id': 'img-lets-go',
            'template_id': 'tpl-img-lets-go',
            'text': 'lets go！kak',
            'asset_type': 'image_caption',
            'media_id': old_media['media_id'],
            'media_path': old_media['media_path'],
            'media_mime_type': old_media['mime_type'],
            'media_filename': old_media['filename'],
            'source_role': '000001',
            'role_positioning': '000001',
            'category': '000001',
            'source_type': 'manual_upload',
            'safe_to_send': True,
            'enabled': True,
        }],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'candidate_pool',
    })
    assert source.status_code == 200
    role = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-000001',
        'role_name': '盖伦000001',
        'region': '印尼',
        'language': 'id',
        'role_positioning': '000001',
        'phrases': ['lets go！kak'],
        'source_type': 'role_save',
        'replace_role_phrases': True,
        'safe_to_send': True,
        'enabled': True,
    })
    assert role.status_code == 200
    account = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_name': '印尼群活跃01',
        'region': '印尼',
        'groups': [{'target_group': '120363400336474261@g.us', 'group_name': 'ID Group', 'enabled': True}],
        'enabled': True,
    }).json()
    binding = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'role-id-000001',
        'account_key': account['account_key'],
        'group_indexes': [0],
        'enabled': True,
        'auto_speaking_enabled': True,
        'group_send_permission_enabled': True,
    })
    assert binding.status_code == 200
    binding_id = binding.json()['bindings'][0]['binding_id']
    trigger = client.post(f'/api/ops/group-atmosphere/role-bindings/{binding_id}/trigger')
    assert trigger.status_code == 200
    stale_binding_config = service._get_group_atmosphere_config(f'binding-{binding_id}')
    assert stale_binding_config['template_pool'][0]['media_id'] == old_media['media_id']

    edited = client.post('/api/ops/group-atmosphere/candidate-pool/custom', json={
        'config_name': 'auto-id-000001',
        'candidate_id': 'img-lets-go',
        'role_positioning': '000001',
        'text': 'lets go！kak',
        'media_id': new_media['media_id'],
    })
    assert edited.status_code == 200
    assert edited.json()['candidate']['media_id'] == new_media['media_id']

    role_after = service._get_group_atmosphere_config('role-id-000001')
    binding_after = service._get_group_atmosphere_config(f'binding-{binding_id}')
    assert role_after['template_pool'][0]['media_id'] == new_media['media_id']
    assert role_after['template_pool'][0]['media_filename'] == 'new.png'
    assert binding_after['template_pool'][0]['media_id'] == new_media['media_id']
    assert binding_after['template_pool'][0]['media_filename'] == 'new.png'


def test_dispatch_resolves_latest_candidate_media_before_sending(monkeypatch):
    client = make_client()
    service = client.app.state.service
    old_media = service.create_group_atmosphere_media_asset('song.png', b'old-image', 'image/png', created_by='test')['media']
    new_media = service.create_group_atmosphere_media_asset('new.png', b'new-image', 'image/png', created_by='test')['media']
    client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'auto-id-000001',
        'enabled': False,
        'account_key': 'auto-id-000001',
        'target_group': 'auto-id-000001',
        'group_name': '自动学习素材库-印尼',
        'language': 'id',
        'daily_max_messages': 0,
        'min_interval_minutes': 120,
        'template_pool': [{
            'candidate_id': 'img-lets-go',
            'template_id': 'tpl-img-lets-go',
            'text': 'lets go！kak',
            'source_role': '000001',
            'role_positioning': '000001',
            'category': '000001',
            'source_type': 'manual_upload',
            'safe_to_send': True,
            'enabled': True,
            'asset_type': 'image_caption',
            'media_id': new_media['media_id'],
            'media_path': new_media['media_path'],
            'media_mime_type': new_media['mime_type'],
            'media_filename': new_media['filename'],
        }],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'candidate_pool',
    })
    client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'binding-gabind_stale_media',
        'enabled': True,
        'account_key': 'atmosphere-test-01',
        'target_group': '120363400336474261@g.us',
        'group_name': 'ID Group',
        'language': 'id',
        'daily_max_messages': 10,
        'min_interval_minutes': 0,
        'template_pool': [{
            'candidate_id': 'img-lets-go',
            'template_id': 'tpl-img-lets-go',
            'text': 'lets go！kak',
            'source_role': '000001',
            'role_positioning': '000001',
            'category': '000001',
            'source_type': 'manual_upload',
            'safe_to_send': True,
            'enabled': True,
            'asset_type': 'image_caption',
            'media_id': old_media['media_id'],
            'media_path': old_media['media_path'],
            'media_mime_type': old_media['mime_type'],
            'media_filename': old_media['filename'],
        }],
        'faq_rules': [],
        'worker_base_url': 'http://127.0.0.1:59999',
        'status': 'enabled',
    })
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append({'url': url, 'json': json, 'timeout': timeout})
        return FakeSendResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)
    result = service.dispatch_group_atmosphere_once(GroupAtmosphereDispatchRequest(config_name='binding-gabind_stale_media', trigger_type='scheduled_auto'))
    assert result['sent'] is True
    assert sent[0]['json']['message_text'] == 'lets go！kak'
    assert sent[0]['json']['media_id'] == new_media['media_id']
    assert sent[0]['json']['media_filename'] == 'new.png'


def test_role_editor_replace_save_only_changes_role_send_selection_without_deleting_phrase_library():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-stale_guard',
        'role_name': '防旧页面覆盖角色',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'stale_guard',
        'phrases': ['第一条', '第二条', '第三条', '第四条'],
        'source_type': 'manual',
        'safe_to_send': True,
        'enabled': True,
    })
    assert created.status_code == 200
    save_selection = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-stale_guard',
        'role_name': '防旧页面覆盖角色',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'stale_guard',
        'phrases': ['第一条'],
        'source_type': 'role_save',
        'replace_role_phrases': True,
        'safe_to_send': True,
        'enabled': True,
    })
    assert save_selection.status_code == 200
    after = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    role_after = next(row for row in after if row['role_key'] == 'role-id-stale_guard')
    assert role_after['phrase_count'] == 1
    assert role_after['available_phrase_count'] == 4
    assert role_after['enabled_phrase_count'] == 1
    candidates = role_after['candidates']
    assert [c['text'] for c in candidates] == ['第一条']
    assert candidates[0]['enabled'] is True
    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    pool_role = next(row for row in pool if row['config_name'] == 'role-id-stale_guard')
    assert pool_role['candidate_count'] == 4
    by_text = {c['text']: c for c in pool_role['candidates']}
    assert set(by_text) == {'第一条', '第二条', '第三条', '第四条'}
    assert by_text['第一条']['enabled'] is True
    # 候选池表示话术库可用性，不表示某个角色是否选中发送；未选中话术仍应留在可用池里。
    assert by_text['第二条']['enabled'] is True
    assert by_text['第三条']['enabled'] is True
    assert by_text['第四条']['enabled'] is True


def test_role_editor_frontend_uses_replace_save_and_dedupes_pool_rows():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    assert 'replace_role_phrases:true' in html
    assert "source_type:'role_save'" in html
    assert 'const seen=new Map()' in html
    assert 'mergeRolePhraseMedia' in html
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
    assert 'ga-role-pool-text-wrap' in html
    assert '${candidateMediaIcon(p)}' in html
    assert '#ga_role_editor_card .ga-role-pool-text-wrap .ga-candidate-media-icon{position:static!important' in html
    assert 'function mergeRolePhraseMedia' in html


def test_candidate_pool_cleans_dedupes_and_sorts_learned_phrases():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-faq_helper',
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
    role = next(row for row in pool if row.get('phrase_type') == 'faq_helper')
    texts = [candidate['text'] for candidate in role['candidates']]
    assert set(texts) == {'Halo kak, kirim ID ke admin ya.', 'Kode dmn kak?'}
    assert role['candidates'][0]['frequency'] == 2
    assert all(candidate['text_zh'] for candidate in role['candidates'])
    assert all(candidate['text_zh_source'] in {'rule', 'google', 'ai', 'libretranslate'} for candidate in role['candidates'])
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
    row = next(row for row in pool if row.get('phrase_type') == 'community_seed')
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
    manual_candidate = next(item for item in candidates if item['text'] == 'Halo kak ini tulisan manual operator')
    assert manual_candidate['source_type'] == 'manual'
    assert manual_candidate['source_label'] == '人工写入'
    upload_candidates = [item for item in candidates if item['source_type'] == 'upload_file']
    assert upload_candidates[0]['candidate_id'] == 'upload-2'
    assert {item['candidate_id'] for item in upload_candidates[:2]} == {'upload-2', 'upload-1'}
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
    candidate = next(row for row in pool if row.get('phrase_type') == 'community_seed')['candidates'][0]
    assert candidate['asset_type'] == 'image_caption'
    assert candidate['media_id'] == 'gamedia_test_1'
    assert candidate['media_filename'] == 'promo.jpg'
    assert candidate['media_preview_url'].endswith('/api/ops/group-atmosphere/media-assets/gamedia_test_1/preview')

    html = client.get('/ops/group-atmosphere').text
    assert 'function candidateHasMedia' in html
    assert 'function candidateMediaIcon' in html
    assert 'data-ga-candidate-media-icon="1"' in html
    assert '<button type="button" class="ga-candidate-media-icon"' in html
    assert 'onclick="openCandidateMediaPreview' in html
    assert 'function openCandidateMediaPreview' in html
    assert 'id="ga_candidate_media_preview_modal"' in html
    assert 'id="ga_candidate_media_preview_image"' in html
    assert '更换图片' in html
    assert '删除图片' in html
    assert 'padding:0!important' in html
    assert 'margin:0!important' in html
    assert 'box-sizing:border-box!important' in html
    assert 'cursor:pointer!important' in html
    icon_css = re.search(r'\.ga-candidate-media-icon\{([^}]*)\}', html).group(1)
    assert 'background:transparent!important' in icon_css
    assert 'border:0!important' in icon_css
    assert 'border-radius:0!important' in icon_css
    assert 'background:#eff6ff' not in icon_css
    assert 'border:1px solid' not in icon_css
    assert 'button.ga-candidate-media-icon,button.ga-candidate-media-icon:hover,button.ga-candidate-media-icon:focus,button.ga-candidate-media-icon:active' in html
    assert '#ga_candidate_pool button.ga-candidate-media-icon' in html
    assert '#ga_role_editor_card button.ga-candidate-media-icon' in html
    override_match = re.search(r'button\.ga-candidate-media-icon[^\{]*\{([^}]*)\}', html)
    assert override_match
    override_css = override_match.group(1)
    assert 'background:transparent!important' in override_css
    assert 'border:0!important' in override_css
    assert "ga-candidate-text-wrap ${hasMedia?'has-media':''}" in html
    assert 'aria-label="预览图片"' in html


def test_candidate_pool_custom_edit_can_replace_and_remove_candidate_media():
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
                'media_id': 'old_media',
                'media_path': '/tmp/old-image.jpg',
                'media_mime_type': 'image/jpeg',
                'media_filename': 'old.jpg',
            }
        ],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'candidate_pool',
    })
    assert created.status_code == 200
    uploaded = client.post(
        '/api/ops/group-atmosphere/media-assets',
        files={'file': ('new.jpg', b'fake image bytes', 'image/jpeg')},
    )
    assert uploaded.status_code == 200
    media = uploaded.json()['media']

    replaced = client.post('/api/ops/group-atmosphere/candidate-pool/custom', json={
        'config_name': 'auto-id-community_seed',
        'candidate_id': 'image-1',
        'text': 'Halo kak lihat gambar ini ya',
        'media_id': media['media_id'],
    })
    assert replaced.status_code == 200
    replaced_candidate = replaced.json()['candidate']
    assert replaced_candidate['asset_type'] == 'image_caption'
    assert replaced_candidate['media_id'] == media['media_id']
    assert replaced_candidate['media_filename'] == 'new.jpg'

    removed = client.post('/api/ops/group-atmosphere/candidate-pool/custom', json={
        'config_name': 'auto-id-community_seed',
        'candidate_id': 'image-1',
        'text': 'Halo kak lihat gambar ini ya',
        'remove_media': True,
    })
    assert removed.status_code == 200
    removed_candidate = removed.json()['candidate']
    assert removed_candidate['asset_type'] == 'text'
    assert removed_candidate.get('media_id') in (None, '')
    assert removed_candidate.get('media_path') in (None, '')
    assert removed_candidate.get('media_filename') in (None, '')


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
    candidates = next(row for row in pool if row.get('phrase_type') == 'community_seed')['candidates']
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


def test_group_atmosphere_candidate_pool_respects_custom_type_inside_auto_storage_config():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'auto-id-000001',
        'enabled': False,
        'account_key': 'auto-id-000001',
        'target_group': 'auto-id-000001',
        'group_name': '自建类型素材库-印尼',
        'language': 'id',
        'daily_max_messages': 0,
        'min_interval_minutes': 120,
        'template_pool': [{
            'candidate_id': 'custom-type-a',
            'text': '自建类型下未选中的话术也要出现在角色编辑池',
            'role_positioning': '000001',
            'source_role': '000001',
            'category': '000001',
            'source_type': 'manual_upload',
            'safe_to_send': True,
            'enabled': True,
        }],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'candidate_pool',
    })
    assert created.status_code == 200

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    custom = next(row for row in pool if row['role_positioning'] == '000001')
    assert custom['config_name'] == 'auto-id-000001'
    assert [item['candidate_id'] for item in custom['candidates']] == ['custom-type-a']
    assert all(row['role_positioning'] != 'community_seed' or all(item['candidate_id'] != 'custom-type-a' for item in row['candidates']) for row in pool)


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

    same_type = client.post('/api/ops/group-atmosphere/candidate-pool/move-type', json={
        'config_name': 'auto-id-community_seed',
        'candidate_ids': ['move-a'],
        'target_role_positioning': 'community_seed',
    })
    assert same_type.status_code == 400
    assert same_type.json()['detail'] == 'target_type_same_as_source'

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


def test_role_editor_save_only_updates_role_selection_without_shrinking_phrase_type_pool():
    client = make_client()
    phrases = [f'盖伦0001候选话术 {idx}' for idx in range(1, 13)]
    created = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'auto-id-000001',
        'enabled': False,
        'account_key': 'auto-id-000001',
        'target_group': 'auto-id-000001',
        'group_name': '盖伦000001',
        'language': 'id',
        'daily_max_messages': 0,
        'min_interval_minutes': 120,
        'template_pool': [{
            'candidate_id': f'garen-{idx}',
            'text': text,
            'role_positioning': '000001',
            'source_role': '000001',
            'category': '000001',
            'source_type': 'manual_upload',
            'safe_to_send': True,
            'enabled': True,
        } for idx, text in enumerate(phrases, start=1)],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'candidate_pool',
    })
    assert created.status_code == 200

    selected = [phrases[1], phrases[4], phrases[9]]
    saved = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-000001',
        'role_name': '盖伦000001',
        'region': '印尼',
        'language': 'id',
        'role_positioning': '000001',
        'phrases': selected,
        'enabled': True,
        'replace_role_phrases': True,
        'source_type': 'role_save',
    })
    assert saved.status_code == 200

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    row = next(item for item in pool if item['role_positioning'] == '000001')
    assert len(row['candidates']) == len(phrases)
    assert {item['text'] for item in row['candidates']} == set(phrases)

    roles = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    role = next(item for item in roles if item['role_key'] == 'role-id-000001')
    assert role['phrase_count'] == len(selected)
    assert role['available_phrase_count'] == len(phrases)
    assert [item['text'] for item in role['candidates']] == selected

    html = client.get('/ops/group-atmosphere').text
    assert 'function roleAvailablePhraseCount' in html
    assert 'available_phrase_count' in html
    assert "装载话术：${esc(loaded)}/${esc(available)} 条" in html








def test_group_atmosphere_auto_learn_creates_only_candidate_pool_configs_not_roles():
    client = make_client()
    content = """
[01/01/2026 10:00] Admin: Halo kak jangan malu ngobrol di grup ya biar suasana ramai.
[01/01/2026 10:01] Admin: Kak kirim data dan ID ke admin dulu ya supaya bisa lanjut.
[01/01/2026 10:02] Admin: Kalau bingung soal kode, tanya singkat di grup ya.
[01/01/2026 10:03] Admin: Semangat kak pelan-pelan saja yang penting konsisten.
"""
    resp = client.post('/api/ops/group-atmosphere/chat-records/auto-learn', json={'filename': 'sample.txt', 'content': content})
    assert resp.status_code == 200, resp.text
    assignments = resp.json()['role_assignments']
    assert assignments
    configs = client.get('/api/ops/group-atmosphere/configs').json()['rows']
    auto_configs = [row for row in configs if row['config_name'].startswith('auto-id-')]
    assert auto_configs
    assert all(row['status'] == 'candidate_pool' for row in auto_configs)
    assert all(row['config_kind'] == 'candidate_pool' for row in auto_configs)
    assert all(row['enabled'] is False for row in auto_configs)
    roles = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    assert roles == []


def test_group_atmosphere_custom_candidate_creation_has_phrase_type_alias_and_never_role():
    client = make_client()
    resp = client.post('/api/ops/group-atmosphere/candidate-pool/custom', json={
        'config_name': 'auto-id-00002',
        'role_positioning': '00002',
        'text': 'Kak kirim data ke admin ya.',
    })
    assert resp.status_code == 200, resp.text
    configs = client.get('/api/ops/group-atmosphere/configs').json()['rows']
    cfg = next(row for row in configs if row['config_name'] == 'auto-id-00002')
    assert cfg['status'] == 'candidate_pool'
    assert cfg['config_kind'] == 'candidate_pool'
    assert cfg['phrase_type'] == '00002'
    candidate = resp.json()['candidate']
    # Candidate payload can be fed back through APIs without losing canonical type semantics.
    assert candidate.get('phrase_type') in {'00002', None}
    pool_row = next(row for row in client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows'] if row['phrase_type'] == '00002')
    assert pool_row['candidates'][0]['phrase_type'] == '00002'
    assert client.get('/api/ops/group-atmosphere/roles').json()['rows'] == []


def test_group_atmosphere_role_config_cannot_be_demoted_to_candidate_pool_by_buggy_payload():
    client = make_client()
    resp = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'role-id-00002',
        'enabled': True,
        'account_key': 'role-id-00002',
        'target_group': 'role-id-00002',
        'group_name': '人工角色00002',
        'language': 'id',
        'daily_max_messages': 4,
        'min_interval_minutes': 60,
        'template_pool': [{'text': 'Kak kirim data ke admin ya.', 'candidate_id': 'cand-role-1', 'source_role': '00002', 'source_type': 'role_save', 'role_selected': True, 'role_send_enabled': True}],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'candidate_pool',
    })
    assert resp.status_code == 200, resp.text
    cfg = resp.json()['config']
    assert cfg['status'] == 'role_container'
    assert cfg['enabled'] is False
    assert cfg['config_kind'] == 'speech_role'
    assert cfg['phrase_type'] == '00002'


def test_group_atmosphere_config_semantic_barrier_enforces_auto_and_role_kinds():
    client = make_client()
    # Even if an old/buggy caller tries to write auto-* as plan_ready, service must normalize it
    # into a pure candidate pool. This is the root guard, not just UI filtering.
    resp = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'auto-id-00002',
        'enabled': True,
        'account_key': 'auto-id-00002',
        'target_group': 'auto-id-00002',
        'group_name': '印尼 · 话术包',
        'language': 'id',
        'daily_max_messages': 4,
        'min_interval_minutes': 60,
        'template_pool': [{'text': 'Kak kirim data ke admin ya.', 'candidate_id': 'cand-auto-1', 'source_role': '00002', 'source_type': 'manual'}],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'plan_ready',
    })
    assert resp.status_code == 200, resp.text
    cfg = resp.json()['config']
    assert cfg['status'] == 'candidate_pool'
    assert cfg['enabled'] is False
    assert cfg['config_kind'] == 'candidate_pool'
    assert cfg['phrase_type'] == '00002'
    roles = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    assert all(row['role_key'] != 'role-id-00002' for row in roles)


def test_group_atmosphere_role_endpoint_never_uses_auto_key_as_role_identity():
    client = make_client()
    # Backward/buggy payload passes an auto-* key to the role endpoint. The service must not
    # create or upgrade auto-* as a role; it should mint/use a role-* identity.
    resp = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-00002',
        'role_name': '人工角色00002',
        'region': '印尼',
        'language': 'id',
        'role_positioning': '00002',
        'phrases': ['Kak kirim data ke admin ya.'],
        'enabled': True,
        'replace_role_phrases': True,
        'source_type': 'role_save',
    })
    assert resp.status_code == 200, resp.text
    role = resp.json()['role']
    assert role['role_key'].startswith('role-id-00002')
    assert role['phrase_type'] == '00002'
    configs = client.get('/api/ops/group-atmosphere/configs').json()['rows']
    auto = next((row for row in configs if row['config_name'] == 'auto-id-00002'), None)
    assert auto is None or (auto['status'] == 'candidate_pool' and auto['config_kind'] == 'candidate_pool')
    roles = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    assert any(row['role_key'].startswith('role-id-00002') for row in roles)
    assert all(not str(row['role_key']).startswith('auto-') for row in roles)


def test_group_atmosphere_phrase_type_alias_is_present_across_candidate_and_role_apis():
    client = make_client()
    upload = client.post('/api/ops/group-atmosphere/phrases/manual-upload', json={
        'region': '印尼',
        'language': 'id',
        'role_positioning': '00002',
        'role_name': '印尼 · 话术包',
        'content': 'Kak kirim data ke admin ya.',
    })
    assert upload.status_code == 200, upload.text
    pool_rows = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    pool_row = next(row for row in pool_rows if row['role_positioning'] == '00002')
    assert pool_row['phrase_type'] == '00002'
    assert pool_row['candidates'][0]['phrase_type'] == '00002'

    role_resp = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': None,
        'role_name': '人工角色00002',
        'region': '印尼',
        'language': 'id',
        'role_positioning': '00002',
        'phrases': ['Kak kirim data ke admin ya.'],
        'enabled': True,
        'replace_role_phrases': True,
        'source_type': 'role_save',
    })
    assert role_resp.status_code == 200, role_resp.text
    role = role_resp.json()['role']
    assert role['phrase_type'] == '00002'
    roles = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    assert next(row for row in roles if row['role_key'] == role['role_key'])['phrase_type'] == '00002'


def test_group_atmosphere_page_humanizes_common_error_codes():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    assert 'function humanizeGaUploadError' in html
    assert "phrases_required:'请先填写或上传话术内容'" in html
    assert "candidate_not_found:'话术不存在或已被删除，请刷新后重试'" in html
    assert "target_type_same_as_source:'目标话术类型和当前类型相同，无需移动'" in html
    assert "if(/^[a-z]+[a-z0-9_]*$/.test(text)&&text.includes('_'))return '操作失败，请检查填写内容后重试'" in html


def test_manual_upload_writes_candidate_pool_not_role_container():
    client = make_client()
    payload = {
        'region': '印尼',
        'language': 'id',
        'role_positioning': '00002',
        'role_name': '印尼 · 话术包',
        'content': '213123',
    }
    resp = client.post('/api/ops/group-atmosphere/phrases/manual-upload', json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['config_name'] == 'auto-id-00002'
    assert 'role' not in data
    configs = client.get('/api/ops/group-atmosphere/configs').json()['rows']
    cfg = next(row for row in configs if row['config_name'] == 'auto-id-00002')
    assert cfg['status'] == 'candidate_pool'
    roles = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    assert all(row['role_key'] != 'role-id-00002' for row in roles)
    assert all(row['role_positioning'] != '00002' for row in roles)
    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    assert any(row['role_positioning'] == '00002' and row['candidate_count'] == 1 for row in pool)


def test_manual_upload_to_existing_auto_pool_does_not_upgrade_to_role():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'auto-id-00002',
        'enabled': False,
        'account_key': 'auto-id-00002',
        'target_group': 'auto-id-00002',
        'group_name': '印尼 · 话术包',
        'language': 'id',
        'daily_max_messages': 0,
        'min_interval_minutes': 120,
        'template_pool': [],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'candidate_pool',
    })
    assert created.status_code == 200
    resp = client.post('/api/ops/group-atmosphere/phrases/manual-upload', json={
        'role_key': 'role-id-00002',
        'region': '印尼',
        'language': 'id',
        'role_positioning': '00002',
        'role_name': '印尼 · 话术包',
        'content': 'lets go kak',
    })
    assert resp.status_code == 200, resp.text
    configs = client.get('/api/ops/group-atmosphere/configs').json()['rows']
    cfg = next(row for row in configs if row['config_name'] == 'auto-id-00002')
    assert cfg['status'] == 'candidate_pool'
    assert len(cfg['template_pool']) == 1
    roles = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    assert all(row['role_key'] != 'role-id-00002' for row in roles)


def test_candidate_pool_manual_config_does_not_auto_create_visible_role_and_label_says_phrase_type():
    client = make_client()
    created_type = client.post('/api/ops/group-atmosphere/phrase-types', json={
        'type_key': '00002',
        'type_name': '盖伦西语00002',
        'enabled': True,
    })
    assert created_type.status_code == 200
    created = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'auto-id-00002',
        'enabled': False,
        'account_key': 'auto-id-00002',
        'target_group': 'auto-id-00002',
        'group_name': '印尼 · 话术包',
        'language': 'id',
        'daily_max_messages': 0,
        'min_interval_minutes': 120,
        'template_pool': [{
            'candidate_id': 'manual-only-00002',
            'text': '213123',
            'source_type': 'manual',
            'source_role': '00002',
            'category': '00002',
            'customized': True,
            'safe_to_send': True,
            'enabled': True,
        }],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'candidate_pool',
    })
    assert created.status_code == 200
    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    assert any(row['role_positioning'] == '00002' for row in pool)
    roles = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    assert all(row['role_key'] != 'role-id-00002' for row in roles)
    html = client.get('/ops/group-atmosphere').text
    assert '话术类型：${esc(roleLabel(r.role_positioning))}' in html
    assert '角色定位：${esc(roleLabel(r.role_positioning))}' not in html


def test_role_loaded_count_uses_all_available_candidates_in_same_role_positioning():
    client = make_client()
    role_phrases = ['角色已选话术 A', '角色已选话术 B']
    role = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-count_scope',
        'role_name': '计数口径角色',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'count_scope',
        'phrases': role_phrases,
        'enabled': True,
        'source_type': 'manual',
    })
    assert role.status_code == 200
    extra = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'auto-id-count_scope',
        'enabled': False,
        'account_key': 'auto-id-count_scope',
        'target_group': 'auto-id-count_scope',
        'group_name': '计数口径素材池',
        'language': 'id',
        'daily_max_messages': 0,
        'min_interval_minutes': 120,
        'template_pool': [{
            'candidate_id': 'extra-count-scope-1',
            'text': '额外可选话术 C',
            'role_positioning': 'count_scope',
            'source_role': 'count_scope',
            'category': 'count_scope',
            'source_type': 'manual_upload',
            'safe_to_send': True,
            'enabled': True,
        }],
        'faq_rules': [],
        'worker_base_url': '',
        'status': 'candidate_pool',
    })
    assert extra.status_code == 200
    rows = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    pool_row = next(row for row in rows if row['role_positioning'] == 'count_scope')
    assert pool_row['candidate_count'] == 3
    roles = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    role_row = next(row for row in roles if row['role_key'] == 'role-id-count_scope')
    assert role_row['phrase_count'] == 2
    assert role_row['available_phrase_count'] == 3


def test_candidate_move_type_merges_same_text_image_candidate_without_creating_text_duplicate():
    client = make_client()
    for config_name, candidates in [
        ('auto-id-community_seed', [{
            'candidate_id': 'image-lets-go',
            'text': 'lets go！kak',
            'role_positioning': 'community_seed',
            'source_role': 'community_seed',
            'category': 'community_seed',
            'source_type': 'manual_upload',
            'asset_type': 'image_caption',
            'media_id': 'media-lets-go',
            'media_filename': 'lets-go.png',
            'media_mime_type': 'image/png',
            'media_preview_url': '/media/lets-go.png',
            'safe_to_send': True,
            'enabled': True,
        }]),
        ('auto-id-000001', [{
            'candidate_id': 'text-lets-go',
            'text': 'lets go！kak',
            'role_positioning': '000001',
            'source_role': '000001',
            'category': '000001',
            'source_type': 'role_save',
            'safe_to_send': True,
            'enabled': True,
        }]),
    ]:
        created = client.post('/api/ops/group-atmosphere/configs', json={
            'config_name': config_name,
            'enabled': False,
            'account_key': config_name,
            'target_group': config_name,
            'group_name': config_name,
            'language': 'id',
            'daily_max_messages': 0,
            'min_interval_minutes': 120,
            'template_pool': candidates,
            'faq_rules': [],
            'worker_base_url': '',
            'status': 'candidate_pool',
        })
        assert created.status_code == 200

    moved = client.post('/api/ops/group-atmosphere/candidate-pool/move-type', json={
        'config_name': 'auto-id-community_seed',
        'candidate_ids': ['image-lets-go'],
        'target_role_positioning': '000001',
    })
    assert moved.status_code == 200

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    target = next(row for row in pool if row['role_positioning'] == '000001')
    same_text = [item for item in target['candidates'] if item['text'] == 'lets go！kak']
    assert len(same_text) == 1
    assert same_text[0]['media_id'] == 'media-lets-go'
    assert same_text[0]['asset_type'] == 'image_caption'


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


def test_group_atmosphere_candidate_pool_page_deletes_selected_by_source_config_name():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    assert 'function deleteSelectedUsableCandidates' in html
    assert 'c.source_config_name||c.config_name||configName' in html
    assert "deleteCandidateFromPool('${esc(c.source_config_name||c.config_name||r.config_name)}','${esc(c.candidate_id)}')" not in html


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
    assert 'ga-candidate-card-head' in html
    assert 'ga-candidate-card-actions' in html
    assert 'ga-candidate-usable-toolbar' in html
    assert 'ga-candidate-drop-cursor' in html
    assert 'function showCandidateDropCursor' in html
    assert 'function clearCandidateDropCursor' in html
    assert 'function onCandidatePointerDragStart' in html
    assert 'function onCandidatePointerDragMove' in html
    assert 'function onCandidatePointerDragEnd' in html
    assert 'onpointerdown="${usable?`onCandidatePointerDragStart' in html
    assert 'document.addEventListener(\'pointermove\',onCandidatePointerDragMove' in html
    assert 'saveCandidateOrder(drag.configName,ids)' in html
    assert '拖动到这里放手' in html
    assert 'data-ga-drop-position' in html
    assert '/api/ops/group-atmosphere/candidate-pool/reorder' in html

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
    assert '<span>可用话术</span><span class="pill gray">${usable.length} 条</span>' in html
    assert 'ga-candidate-usable-title' in html
    assert '#ga_candidate_pool .ga-candidate-bulk-move-actions{display:flex!important;align-items:center!important;justify-content:flex-end!important;gap:6px!important;flex-wrap:nowrap!important' in html
    assert '#ga_candidate_pool .ga-candidate-bulk-move-actions select{width:156px!important' in html
    assert 'ga-candidate-bulk-move-actions' in html
    assert 'data-ga-move-type-select' in html
    assert '/api/ops/group-atmosphere/candidate-pool/move-type' in html
    assert 'window.__gaCandidatePoolMinHeight' in html
    assert 'window.scrollBy({top:delta,left:0,behavior:\'auto\'})' in html
    assert '/api/ops/group-atmosphere/candidate-pool/move-type' in html
    assert 'data-ga-candidate-drag-handle="1"' in html
    assert 'draggable="true" data-ga-candidate-drag-handle="1"' in html or 'data-ga-candidate-drag-handle="1" title="拖动排序" aria-label="拖动排序" ondragstart=' in html
    assert 'draggable="true" data-ga-candidate-row="1"' not in html


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
        'target_role_keys': ['role-id-community_seed'],
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


def test_learning_account_summary_items_match_actual_new_pending_candidates(monkeypatch):
    client = make_client()
    service = client.app.state.service
    monkeypatch.setattr(service, '_clean_group_atmosphere_message_text', lambda text: str(text or '').strip())
    monkeypatch.setattr(service, '_rewrite_group_atmosphere_semantic_candidate', lambda text, role='': str(text or '').strip())
    monkeypatch.setattr(service, '_polish_group_atmosphere_candidate_text', lambda text, role='': str(text or '').strip())
    monkeypatch.setattr(service, '_is_group_atmosphere_useful_candidate', lambda text, role='': bool(str(text or '').strip()))
    monkeypatch.setattr(service, '_classify_group_atmosphere_record_role', lambda text: 'community_seed')
    monkeypatch.setattr(service, '_group_atmosphere_semantic_intent', lambda text: 'chat')
    monkeypatch.setattr(service, '_evaluate_group_atmosphere_candidate_quality', lambda text, role='', source_type='': {
        'decision': 'accept',
        'quality_status': 'pending_review',
        'quality_score': 88,
        'reasons': [],
        'normalized_key': service._normalize_group_atmosphere_phrase_key(text),
        'semantic_key': service._normalize_group_atmosphere_semantic_phrase_key(text),
    })

    created = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-summary-match',
        'account_name': '摘要一致性学习号',
        'region': '印尼',
        'language': 'id',
        'enabled': True,
        'group_links': [{'target_group': 'group-a@g.us', 'group_name': '印尼A群', 'enabled': True}],
        'target_role_keys': ['role-id-community_seed'],
    })
    assert created.status_code == 200
    seeded = service.upsert_group_atmosphere_manual_phrases({
        'role_key': 'role-id-community_seed',
        'role_name': '气氛活跃型',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['Kak, tetap semangat ya. Pelan-pelan saja, yang penting terus ikut arahan grup.'],
        'source_type': 'learning_account',
        'safe_to_send': False,
        'enabled': False,
    })
    assert seeded['added_count'] == 1

    learned = client.post('/api/ops/group-atmosphere/learning-accounts/learn-summary-match/learn-once', json={
        'records': [
            {'sender': 'u1', 'text': 'Kak, tetap semangat ya. Pelan-pelan saja, yang penting terus ikut arahan grup.', 'created_at': '2026-05-22T03:36:00Z', 'message_id': 'm1'},
            {'sender': 'u2', 'text': 'Maksudnya gmn kak?', 'created_at': '2026-05-22T03:36:14Z', 'message_id': 'm2'},
        ],
    })
    assert learned.status_code == 200
    body = learned.json()
    assert body['candidate_count'] == 1
    items = body['last_result_summary']['items']
    assert [item['text'] for item in items] == ['Maksudnya gmn kak?']
    assert all('tetap semangat' not in item['text'] for item in items)

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    role = next(row for row in pool if row.get('phrase_type') == 'community_seed')
    pending = [c['text'] for c in role['candidates'] if c['source_type'] == 'learning_account' and c['safe_to_send'] is False]
    assert 'Maksudnya gmn kak?' in pending


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
        'target_role_keys': ['role-id-community_seed'],
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
    role = next(row for row in pool if row.get('phrase_type') == 'community_seed')
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
    role_after_confirm = next(row for row in pool_after_confirm if row.get('phrase_type') == 'community_seed')
    confirmed = next(candidate for candidate in role_after_confirm['candidates'] if candidate['candidate_id'] == learned_candidates[0]['candidate_id'])
    assert confirmed['safe_to_send'] is True
    assert confirmed['enabled'] is True
    assert confirmed['quality_status'] == 'manual_approved'


def test_learning_account_existing_unconfirmed_candidates_remain_pending_in_candidate_pool():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-community_seed',
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
    role = next(row for row in pool if row.get('phrase_type') == 'community_seed')
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
        'target_role_keys': ['role-id-community_seed'],
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
        'target_role_keys': ['role-id-community_seed'],
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
        'target_role_keys': ['role-id-community_seed'],
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
        'target_role_keys': ['role-id-community_seed'],
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
        'target_role_keys': ['role-id-newcomer_guide'],
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
        'role_key': 'role-id-newcomer_guide',
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
    role = next(row for row in pool if row.get('phrase_type') == 'newcomer_guide')
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
        'role_key': 'role-id-community_seed',
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
    row = next(item for item in pool if item.get('phrase_type') == 'community_seed')
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
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-id-community_seed',
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
    assert candidate['text_zh']
    assert candidate['text_zh_source'] in {'unavailable', 'rule', 'google'}
    assert candidate['text_zh_status'] in {'needs_translation', 'needs_review', 'ok'}
    assert not any(token in candidate['text_zh'].lower() for token in ['kak', 'boleh', 'tanya', 'grup'])


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
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-id-faq_helper',
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
        'role_key': 'role-id-faq_helper',
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
    role = next(row for row in pool if row.get('phrase_type') == 'faq_helper')
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
        'role_key': 'role-id-faq_helper',
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
    monkeypatch.setattr(service, 'list_whatsapp_approval_area_options', lambda: {'options': [{'value': 'Indonesia', 'label': '印尼'}], 'source_options': []})
    monkeypatch.setattr(service, '_list_notify_robot_options', lambda: [{'profile_name': 'approval-bot', 'label': '审批bot'}])
    monkeypatch.setattr(service, '_build_whatsapp_approval_runtime_state', lambda *args, **kwargs: {'active': False})

    approval_groups = [
        {
            'link': f'https://chat.whatsapp.com/approval{i:02d}ABCDEFG1234567890',
            'group_name': f'审批群{i}',
            'area': 'Indonesia',
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
    assert '<strong>可用话术</strong><span class="pill gray">${usable.length} 条</span>' not in html
    assert '#ga_candidate_pool .group-card-title{align-items:center!important;margin-bottom:4px!important;}' in html
    assert '#ga_candidate_pool .group-card-title>.inline-actions{margin-top:0!important;align-self:flex-start!important;transform:translateY(-2px);}' in html
    assert 'style="margin-top:8px;border-top:1px solid #e5e7eb;padding-top:8px;"' in html
    assert "manual_approved'?'人工确认" not in html
    assert "status==='manual_approved'||status==='approved_manual'||status==='pending_review')return ''" in html
    assert "?'待质检'" not in html
    assert 'const GA_MAX_GROUPS=5;' in html
    assert 'const GA_LEARNING_MAX_GROUPS=10;' in html
    assert '<select id="ga_candidate_language_filter"><option value="id" selected>印尼</option>' in html
    assert '<option value="">语言/地区</option>' not in html
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
        'role_key': 'role-id-community_seed',
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
        for i in range(5)
    ]
    account = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-indo-05',
        'account_name': '印尼发言号05',
        'region': '印尼',
        'groups': groups,
        'enabled': True,
    })
    assert account.status_code == 200

    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'role-id-community_seed',
        'account_key': 'atmosphere-indo-05',
        'group_indexes': list(range(5)),
        'daily_max_messages': 12,
        'min_interval_minutes': 7,
        'max_interval_minutes': 19,
        'auto_speaking_enabled': True,
        'group_send_permission_enabled': True,
    })
    assert created.status_code == 200
    body = created.json()
    assert body['created_count'] == 5
    assert len(body['relationship']['groups']) == 5
    assert body['relationship']['relationship_label'] == '桥接关系1'
    assert body['relationship']['daily_max_messages'] == 12
    assert body['relationship']['min_interval_minutes'] == 7
    assert body['relationship']['max_interval_minutes'] == 19
    assert body['relationship']['randomness_level'] == 'medium'
    assert body['relationship']['phrase_send_order'] == 'random'

    listed = client.get('/api/ops/group-atmosphere/role-bindings').json()
    assert listed['count'] == 5
    assert listed['relationship_count'] == 1
    relationship = listed['relationships'][0]
    assert relationship['role_key'] == 'role-id-community_seed'
    assert relationship['account_key'] == 'atmosphere-indo-05'
    assert len(relationship['groups']) == 5
    assert relationship['groups'][0]['group_send_permission_enabled'] is True

    too_many = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'role-id-community_seed',
        'account_key': 'atmosphere-indo-05',
        'group_indexes': list(range(6)),
    })
    assert too_many.status_code in {400, 409}
    assert too_many.json()['detail'] in {'role_binding_groups_limit_5','role_binding_groups_limit_10','role_binding_already_exists','role_binding_account_group_already_used'}



def test_group_atmosphere_role_bridge_persists_randomness_and_sorted_send_order(monkeypatch):
    client = make_client()
    client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [1],
        'auto_speaking_enabled': True,
        'min_interval_minutes': 0,
    })
    assert second.status_code == 200
    after = client.get('/api/ops/group-atmosphere/role-bindings').json()['relationships']
    labels = {rel['role_key']: rel['relationship_label'] for rel in after}
    assert labels['role-id-community_seed-0112131'] == '桥接关系1'
    assert labels['role-id-community_seed'] == '桥接关系2'


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
    keys = [row['type_key'] for row in types]
    assert any(row['type_key'] == 'retention_recall' and row['type_name'] == '留存召回型' for row in types)
    assert keys.index('retention_recall') > keys.index('motivation_admin')
    assert types[keys.index('retention_recall')]['sort_order'] > types[keys.index('motivation_admin')]['sort_order']

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

    assert client.get('/api/ops/group-atmosphere/roles').json()['rows'] == []
    pool_rows = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    uploaded_candidates = [item for row in pool_rows for item in row.get('candidates', []) if item.get('source_type') == 'manual_upload']
    assert uploaded_candidates
    assert all(item['source_label'] == '人工写入' for item in uploaded_candidates)
    assert all(item['safe_to_send'] and item['enabled'] for item in uploaded_candidates)
    source_role = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-retention_recall',
        'role_name': '印尼留存召回',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'retention_recall',
        'phrases': [uploaded_candidates[0]['text']],
        'source_type': 'role_save',
        'replace_role_phrases': True,
        'enabled': True,
    })
    assert source_role.status_code == 200
    phrases = source_role.json()['role']['template_pool']

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


def test_new_phrase_type_empty_tab_can_create_first_manual_candidate():
    client = make_client()
    created_type = client.post('/api/ops/group-atmosphere/phrase-types', json={
        'type_key': 'garen_0001',
        'type_name': '盖伦0001',
        'enabled': True,
    })
    assert created_type.status_code == 200

    created = client.post('/api/ops/group-atmosphere/candidate-pool/custom', json={
        'config_name': 'auto-id-garen_0001',
        'role_positioning': 'garen_0001',
        'text': 'Halo kak ini话术 pertama',
    })
    assert created.status_code == 200
    assert created.json()['config_name'] == 'auto-id-garen_0001'

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    row = next(item for item in pool if item['role_positioning'] == 'garen_0001')
    assert row['config_name'] == 'auto-id-garen_0001'
    assert row['candidate_count'] == 1
    assert row['candidates'][0]['text'] == 'Halo kak ini话术 pertama'

    html = client.get('/ops/group-atmosphere').text
    assert "const configName=`auto-${document.getElementById('ga_candidate_language_filter')?.value||'id'}-${role}`" in html
    assert 'function saveManualCandidate' in html


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
    assert '#ga_candidate_type_bar{display:flex!important;align-items:flex-start!important;gap:8px!important' in html
    assert '#ga_candidate_role_tabs{display:flex!important;gap:8px!important;flex-wrap:nowrap!important;overflow-x:scroll!important' in html
    assert 'padding:0 0 12px!important;scrollbar-width:auto!important;scrollbar-color:#94a3b8 #e2e8f0!important;' in html
    assert '#ga_candidate_role_tabs::-webkit-scrollbar{height:10px!important;}' in html
    assert '#ga_candidate_role_tabs::-webkit-scrollbar-track{background:#e2e8f0!important;border-radius:999px!important;}' in html
    assert '#ga_candidate_role_tabs::-webkit-scrollbar-thumb{background:#94a3b8!important;border-radius:999px!important;border:2px solid #e2e8f0!important;}' in html
    assert '#ga_candidate_role_tabs .ga-candidate-tab{flex:0 0 auto!important;}' in html
    assert '#ga_add_phrase_type_btn{flex:0 0 auto!important;margin:0!important;}' in html
    assert '<div class="ga-candidate-type-label">话术类型</div><div id="ga_candidate_type_bar"><div id="ga_candidate_role_tabs" class="ga-candidate-tabs"></div><button type="button" class="ga-candidate-tab ga-add-phrase-type-tab" id="ga_add_phrase_type_btn" title="新增话术类型" aria-label="新增话术类型">+</button></div>' in html

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
    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    tpl = next(c for row in pool for c in row['candidates'] if c['text'] == 'Halo kak, lihat poster ini.')
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
    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    row = next(item for item in pool if item.get('phrase_type') == 'community_seed')
    texts = [item['text'] for item in row['candidates'] if item.get('source_type') == 'manual_upload']
    assert 'Kak, jangan lupa aktif di grup ya.' in texts
    assert 'Kalau ada pertanyaan boleh tanya admin.' in texts


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
    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    row = next(item for item in pool if item.get('phrase_type') == 'community_seed')
    texts = [item['text'] for item in row['candidates'] if item.get('source_type') == 'manual_upload']
    assert set(texts) == {
        'Halo kak, share pengalaman kamu di grup ya.',
        'Kak, jangan sepi-sepi, ngobrol santai di sini.',
        'Kalau sudah daftar, boleh cerita prosesnya lancar atau tidak.',
    }
    assert not any(text.isdigit() for text in texts)


def test_group_atmosphere_manual_upload_preview_requires_review_groups_languages_and_does_not_write_pool(tmp_path):
    client = make_client({'GROUP_ATMOSPHERE_MEDIA_DIR': str(tmp_path / 'ga-media')})
    preview = client.post('/api/ops/group-atmosphere/phrases/manual-upload-preview', json={
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'content': 'Halo kak!\nhalo kak\nOi amiga, tudo bem?\n123\nHola amiga, comparte tu experiencia.',
    })
    assert preview.status_code == 200
    data = preview.json()
    assert data['review_required'] is True
    assert data['summary']['total'] == 5
    assert data['summary']['new_count'] == 3
    assert data['summary']['duplicate_count'] == 1
    assert data['summary']['invalid_count'] == 1
    assert data['summary']['language_groups']['id'] == 1
    assert data['summary']['language_groups']['pt'] == 1
    assert data['summary']['language_groups']['es'] == 1
    assert [item['text'] for item in data['items']] == [
        'Halo kak!',
        'Oi amiga, tudo bem?',
        'Hola amiga, comparte tu experiencia.',
    ]
    assert data['items'][0]['selected'] is True
    assert data['items'][0]['duplicate_status'] == 'new'
    assert data['items'][1]['language'] == 'pt'
    assert data['items'][1]['region'] == '巴西'
    assert data['items'][2]['language'] == 'es'
    assert data['items'][2]['region'] == '墨西哥'
    assert client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows'] == []


def test_group_atmosphere_manual_upload_preview_classifies_long_spanish_copy_as_mexico_without_default_region(tmp_path):
    client = make_client({'GROUP_ATMOSPHERE_MEDIA_DIR': str(tmp_path / 'ga-media')})
    spanish_lines = [
        'Si el código aparece como inválido, no sigas intentando muchas veces. Envíanos una captura de la pantalla donde te sale el error y revisamos si estás en la sección correcta o si hay una incidencia temporal del sistema.',
        'Para completar tu perfil, revisa estos puntos: foto clara, nombre/nickname, descripción llamativa, datos básicos y publicaciones/fotos. Cuando termines, manda captura para que una administradora revise qué falta.',
        'Hoy vamos a refrescar el perfil. Cambien la foto principal por una imagen clara, bonita y llamativa. Un perfil actualizado ayuda a que más usuarios se interesen y respondan los mensajes.',
        'Chicas, ¿quién tiene dudas en este momento? Pueden preguntar sin pena: descarga, perfil, código de agencia, mensajes, diamantes o retiro. Estamos aquí para guiarlas paso a paso.',
    ]
    preview = client.post('/api/ops/group-atmosphere/phrases/manual-upload-preview', json={
        'role_positioning': 'newcomer_guide',
        'content': '\n'.join(spanish_lines),
    })
    assert preview.status_code == 200
    data = preview.json()
    assert data['summary']['new_count'] == len(spanish_lines)
    assert data['summary']['language_groups'] == {'es': len(spanish_lines)}
    assert {item['language'] for item in data['items']} == {'es'}
    assert {item['region'] for item in data['items']} == {'墨西哥'}
    assert all(item['selected'] is True for item in data['items'])


def test_group_atmosphere_manual_upload_confirm_writes_only_reviewed_items_to_candidate_pool(tmp_path):
    client = make_client({'GROUP_ATMOSPHERE_MEDIA_DIR': str(tmp_path / 'ga-media')})
    preview = client.post('/api/ops/group-atmosphere/phrases/manual-upload-preview', json={
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'content': 'Halo kak!\nOi amiga, tudo bem?',
    }).json()
    items = preview['items']
    items[1]['selected'] = False
    confirm = client.post('/api/ops/group-atmosphere/phrases/manual-upload-confirm', json={'items': items})
    assert confirm.status_code == 200
    assert confirm.json()['imported_count'] == 1
    rows = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    candidates = [c for row in rows for c in row['candidates']]
    assert [c['text'] for c in candidates] == ['Halo kak!']
    assert candidates[0]['safe_to_send'] is True
    assert candidates[0]['enabled'] is True
    assert candidates[0]['quality_status'] == 'manual_approved'
    assert candidates[0]['source_type'] == 'manual_upload'


def test_group_atmosphere_manual_upload_confirm_preserves_original_language_after_translation_preview(tmp_path):
    client = make_client({'GROUP_ATMOSPHERE_MEDIA_DIR': str(tmp_path / 'ga-media')})
    preview = client.post('/api/ops/group-atmosphere/phrases/manual-upload-preview', json={
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'content': 'Oi amiga, tudo bem?',
    })
    assert preview.status_code == 200
    item = preview.json()['items'][0]
    assert item['language'] == 'pt'
    assert item['region'] == '巴西'
    confirm = client.post('/api/ops/group-atmosphere/phrases/manual-upload-confirm', json={'items': [{
        **item,
        'selected': True,
        'text': '嗨朋友，一切都好吗？',
        'original_text': 'Oi amiga, tudo bem?',
        'translated_text': '嗨朋友，一切都好吗？',
    }]})
    assert confirm.status_code == 200, confirm.text
    config = confirm.json()['configs'][0]
    assert config['language'] == 'pt'
    assert config['region'] == '巴西'
    rows = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    assert len(rows) == 1
    assert rows[0]['language'] == 'pt'
    assert rows[0]['region'] == '巴西'
    candidates = [c for row in rows for c in row['candidates']]
    assert [c['text'] for c in candidates] == ['Oi amiga, tudo bem?']
    assert all('嗨朋友' not in c['text'] for c in candidates)


def test_group_atmosphere_manual_upload_confirm_inferrs_region_from_original_text_when_translation_preview_drops_metadata(tmp_path):
    client = make_client({'GROUP_ATMOSPHERE_MEDIA_DIR': str(tmp_path / 'ga-media')})
    confirm = client.post('/api/ops/group-atmosphere/phrases/manual-upload-confirm', json={'items': [{
        'selected': True,
        'text': '嗨朋友，一切都好吗？',
        'original_text': 'Oi amiga, tudo bem?',
        'translated_text': '嗨朋友，一切都好吗？',
        'role_positioning': 'community_seed',
    }]})
    assert confirm.status_code == 200, confirm.text
    config = confirm.json()['configs'][0]
    assert config['language'] == 'pt'
    assert config['region'] == '巴西'
    rows = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    assert rows[0]['language'] == 'pt'
    assert rows[0]['region'] == '巴西'
    assert rows[0]['candidates'][0]['text'] == 'Oi amiga, tudo bem?'


def test_group_atmosphere_manual_upload_preview_filters_chinese_headers_and_offers_translate(tmp_path):
    client = make_client({'GROUP_ATMOSPHERE_MEDIA_DIR': str(tmp_path / 'ga-media')})
    preview = client.post('/api/ops/group-atmosphere/phrases/manual-upload-preview', json={
        'region': '墨西哥',
        'language': 'es',
        'role_positioning': 'community_seed',
        'content': '优化后可复制话术\nHola amiga, comparte tu experiencia.\nChicas, pueden preguntar sin pena.',
    })
    assert preview.status_code == 200
    data = preview.json()
    assert data['summary']['total'] == 3
    assert data['summary']['new_count'] == 2
    assert data['summary']['invalid_count'] == 1
    assert data['invalid_items'][0]['reason'] == 'cjk_non_target_language'
    assert [item['text'] for item in data['items']] == [
        'Hola amiga, comparte tu experiencia.',
        'Chicas, pueden preguntar sin pena.',
    ]

    translated = client.post('/api/ops/group-atmosphere/phrases/manual-upload-translate', json={
        'text': 'Hola amiga, comparte tu experiencia.',
        'language': 'es',
        'region': '墨西哥',
        'role_positioning': 'community_seed',
    })
    assert translated.status_code == 200
    assert translated.json()['text_zh']


def test_group_atmosphere_manual_upload_preview_detects_existing_and_csv_phrase_column(tmp_path):
    client = make_client({'GROUP_ATMOSPHERE_MEDIA_DIR': str(tmp_path / 'ga-media')})
    seeded = client.post('/api/ops/group-atmosphere/phrases/manual-upload-confirm', json={'items': [{
        'text': 'Halo kak!',
        'language': 'id',
        'region': '印尼',
        'role_positioning': 'community_seed',
        'selected': True,
    }]})
    assert seeded.status_code == 200
    csv_bytes = '序号,备注,话术内容\n001,ignore,Halo kak!!\n002,ignore,Kak admin siap bantu.\n'.encode('utf-8-sig')
    preview = client.post('/api/ops/group-atmosphere/phrases/manual-upload-preview-file', data={
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
    }, files={'file': ('phrases.csv', csv_bytes, 'text/csv')})
    assert preview.status_code == 200
    data = preview.json()
    assert data['summary']['existing_duplicate_count'] == 1
    assert [item['text'] for item in data['items']] == ['Kak admin siap bantu.']
    duplicates = data['duplicates']
    assert duplicates[0]['text'] == 'Halo kak!!'
    assert duplicates[0]['duplicate_status'] == 'existing'


def test_group_atmosphere_manual_upload_preview_file_supports_real_xls_via_xlrd(monkeypatch, tmp_path):
    class FakeCell:
        def __init__(self, value):
            self.value = value

    class FakeSheet:
        nrows = 3
        ncols = 3
        _rows = [
            ['序号', '备注', '话术内容'],
            ['001', 'ignore', 'Kak, tetap aktif di grup ya.'],
            ['002', 'ignore', 'Kalau ada pertanyaan tanya admin.'],
        ]
        def row(self, idx):
            return [FakeCell(value) for value in self._rows[idx]]

    fake_xlrd = types.SimpleNamespace(open_workbook=lambda file_contents=None: types.SimpleNamespace(sheet_by_index=lambda index: FakeSheet()))
    monkeypatch.setitem(sys.modules, 'xlrd', fake_xlrd)
    client = make_client({'GROUP_ATMOSPHERE_MEDIA_DIR': str(tmp_path / 'ga-media')})
    preview = client.post('/api/ops/group-atmosphere/phrases/manual-upload-preview-file', data={
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
    }, files={'file': ('phrases.xls', b'\xd0\xcf\x11\xe0real-xls', 'application/vnd.ms-excel')})
    assert preview.status_code == 200
    assert [item['text'] for item in preview.json()['items']] == [
        'Kak, tetap aktif di grup ya.',
        'Kalau ada pertanyaan tanya admin.',
    ]


def test_group_atmosphere_manual_upload_page_exposes_review_modal_and_preview_flow():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    assert 'ga_manual_upload_review_modal' in html
    assert '审核导入话术' in html
    assert 'function renderManualUploadReviewModal' in html
    assert 'function confirmManualUploadReview' in html
    assert '/api/ops/group-atmosphere/phrases/manual-upload-preview' in html
    assert '/api/ops/group-atmosphere/phrases/manual-upload-preview-file' in html
    assert '/api/ops/group-atmosphere/phrases/manual-upload-confirm' in html
    assert 'id="ga_manual_upload_review_card"' in html
    assert 'aria-labelledby="gaManualUploadReviewTitle"' in html
    assert 'id="gaManualUploadReviewTitle">审核导入话术' in html
    assert '只会导入已勾选的话术；可在确认前修改文案。' in html
    assert 'ga-review-summary-grid' in html
    assert 'ga-review-toolbar' in html
    assert 'data-ga-review-select-all="1"' in html
    assert 'function setManualUploadReviewSelection' in html
    assert 'function updateManualUploadReviewSelectionState' in html
    assert 'ga-review-row-main' in html
    assert 'data-ga-review-textarea="1"' in html
    assert 'function manualUploadReviewTextareaRows' in html
    assert 'function autoResizeManualUploadReviewTextarea' in html
    assert "rows=\"${manualUploadReviewTextareaRows(item.text||'')}\"" in html
    assert 'max-height:240px' in html
    assert '#ga_manual_upload_review_card .ga-review-row textarea{min-height:38px!important;height:auto!important' not in html
    assert 'height:var(--ga-review-textarea-height,auto)!important' in html
    assert 'resize:none!important' in html
    assert 'box-sizing:border-box!important' in html
    assert "split(new RegExp('\\r\\n|\\r|\\n'))" in html
    assert "el.setAttribute('rows',String(rows))" in html
    assert "el.style.setProperty('height',`${next}px`,'important')" in html
    assert "el.style.setProperty('overflow-y',scrollHeight>maxHeight?'auto':'hidden','important')" in html
    assert 'data-original-text="${esc(item.original_text||item.text||\'\')}"' in html
    assert "const originalText=String(el?.dataset?.originalText||item.original_text||item.text||'').trim()" in html
    assert "const showingZh=el?.dataset?.showingZh==='1'" in html
    assert 'const text=showingZh&&originalText?originalText:rawValue' in html
    assert 'translated_text:showingZh?rawValue' in html
    assert 'function toggleManualUploadReviewTranslation' in html
    assert 'ga-review-translate-btn' in html
    assert '/api/ops/group-atmosphere/phrases/manual-upload-translate' in html
    assert 'data-ga-review-region' in html
    assert 'data-ga-review-role' in html
    assert 'ga-review-bulk-controls' in html
    assert 'ga_manual_review_bulk_region' in html
    assert 'ga_manual_review_bulk_role' in html
    assert '按语言自动分配' in html
    assert '__auto_by_language__' in html
    assert "manualUploadReviewRegionOptionsHtml('__auto_by_language__',true)" in html
    assert 'function manualUploadReviewRegionByLanguage' in html
    assert 'function manualUploadReviewAutoRegionForItem' in html
    assert 'manualUploadReviewAutoRegionForItem(item):region' in html
    assert 'let applied=0' in html
    assert '请先勾选要应用的话术' in html
    assert '确认无误后点击“确认导入已选话术”' in html
    assert 'showTip(msg,\'success\')' in html
    assert 'function applyManualUploadReviewBulk' in html
    assert 'function setManualUploadReviewRegion' in html
    assert 'function setManualUploadReviewRole' in html
    assert 'ga-review-row-meta' not in html
    assert '<span class="pill green">新话术</span>' not in html
    assert "item.source_label||item.source||'人工导入'" not in html
    assert 'ga-review-footer' in html
    assert '确认导入已选话术' in html
    assert '无需二次审核' not in html



def test_group_atmosphere_keyword_trigger_matches_inbound_group_id_from_worker(tmp_path):
    client = make_client({'GROUP_ATMOSPHERE_MEDIA_DIR': str(tmp_path / 'ga-media')})
    account = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-indo-01',
        'account_name': '测试发言号',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'groups': [{
            'target_group': 'https://chat.whatsapp.com/invite-1',
            'group_name': '注册测试1',
            'enabled': True,
        }],
        'enabled': True,
    })
    assert account.status_code == 200
    with client.app.state.service.db.connect() as conn:
        rows = json.loads(conn.execute("SELECT group_links FROM whatsapp_approval_accounts WHERE account_key='atmosphere-indo-01'").fetchone()[0])
        rows[0]['group_id'] = '120363422719530134@g.us'
        conn.execute("UPDATE whatsapp_approval_accounts SET group_links=? WHERE account_key='atmosphere-indo-01'", (json.dumps(rows, ensure_ascii=False),))
        conn.commit()
    role = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-000001',
        'role_name': '盖伦000001',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['Halo kak, admin bantu ya.'],
        'enabled': True,
    })
    assert role.status_code == 200
    binding = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'role-id-000001',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'enabled': True,
        'auto_speaking_enabled': False,
        'trigger_speaking_enabled': True,
        'group_send_permission_enabled': True,
    })
    assert binding.status_code == 200
    rule = client.post('/api/ops/group-atmosphere/trigger-rules', json={
        'relationship_key': 'role-id-000001',
        'rule_name': 'apa keyword',
        'trigger_type': 'keyword_match',
        'enabled': True,
        'priority': 1,
        'conditions': {'keywords': ['apa'], 'match_type': 'contains'},
        'message_sequence': [{'type': 'text', 'text': 'Balasan otomatis.', 'delay_seconds': 0}],
    })
    assert rule.status_code == 200

    inbound = client.post('/api/internal/group-atmosphere/inbound-message', json={
        'account_key': 'atmosphere-indo-01',
        'target_group': '120363422719530134@g.us',
        'sender_id': '62812@c.us',
        'text': 'apa',
    })
    assert inbound.status_code == 200
    body = inbound.json()
    assert body['should_respond'] is True
    assert body['result_code'] == 'trigger_rule_matched'
    assert body['binding_id']
    assert body['matched_keyword'] == 'apa'




def test_group_atmosphere_keyword_trigger_default_user_cooldown_is_shorter():
    client = make_client()
    seed_role_and_account(client)
    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'role-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'enabled': True,
        'auto_speaking_enabled': False,
        'trigger_speaking_enabled': True,
        'group_send_permission_enabled': True,
    })
    assert created.status_code == 200
    rule = client.post('/api/ops/group-atmosphere/trigger-rules', json={
        'relationship_key': 'role-id-community_seed',
        'rule_name': 'kode keyword',
        'trigger_type': 'keyword_match',
        'enabled': True,
        'conditions': {'keywords': ['kode'], 'match_type': 'contains'},
        'message_sequence': [{'type': 'text', 'text': 'Balasan otomatis.'}],
    })
    assert rule.status_code == 200
    assert rule.json()['rule']['cooldown_seconds'] == 0
    assert rule.json()['rule']['per_user_cooldown_seconds'] == 10


def test_group_atmosphere_keyword_cooldown_is_scoped_to_same_group(tmp_path):
    client = make_client({'GROUP_ATMOSPHERE_MEDIA_DIR': str(tmp_path / 'ga-media')})
    account = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-indo-01',
        'account_name': '测试发言号',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'groups': [
            {'target_group': 'https://chat.whatsapp.com/invite-1', 'group_name': '注册测试1', 'enabled': True},
            {'target_group': 'https://chat.whatsapp.com/invite-2', 'group_name': '官方测试1', 'enabled': True},
        ],
        'enabled': True,
    })
    assert account.status_code == 200
    with client.app.state.service.db.connect() as conn:
        rows = json.loads(conn.execute("SELECT group_links FROM whatsapp_approval_accounts WHERE account_key='atmosphere-indo-01'").fetchone()[0])
        rows[0]['group_id'] = '120363111111111111@g.us'
        rows[1]['group_id'] = '120363222222222222@g.us'
        conn.execute("UPDATE whatsapp_approval_accounts SET group_links=? WHERE account_key='atmosphere-indo-01'", (json.dumps(rows, ensure_ascii=False),))
        conn.commit()
    role = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-000001',
        'role_name': '盖伦000001',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['Halo kak.'],
        'enabled': True,
    })
    assert role.status_code == 200
    binding = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'role-id-000001',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0, 1],
        'enabled': True,
        'auto_speaking_enabled': False,
        'trigger_speaking_enabled': True,
        'group_send_permission_enabled': True,
    })
    assert binding.status_code == 200
    rule = client.post('/api/ops/group-atmosphere/trigger-rules', json={
        'relationship_key': 'role-id-000001',
        'rule_name': 'apa keyword',
        'trigger_type': 'keyword_match',
        'enabled': True,
        'priority': 1,
        'conditions': {'keywords': ['apa'], 'match_type': 'contains'},
        'message_sequence': [{'type': 'text', 'text': 'Balasan otomatis.', 'delay_seconds': 0}],
        'cooldown_seconds': 60,
        'per_user_cooldown_seconds': 300,
    })
    assert rule.status_code == 200

    first = client.post('/api/internal/group-atmosphere/inbound-message', json={
        'account_key': 'atmosphere-indo-01',
        'target_group': '120363111111111111@g.us',
        'sender_id': '62812@c.us',
        'text': 'apa',
    }).json()
    assert first['should_respond'] is True

    same_group = client.post('/api/internal/group-atmosphere/inbound-message', json={
        'account_key': 'atmosphere-indo-01',
        'target_group': '120363111111111111@g.us',
        'sender_id': '62813@c.us',
        'text': 'apa',
    }).json()
    assert same_group['should_respond'] is False
    assert same_group['result_code'] == 'rule_cooldown_active'
    assert same_group['cooldown_scope'] == 'group'

    other_group = client.post('/api/internal/group-atmosphere/inbound-message', json={
        'account_key': 'atmosphere-indo-01',
        'target_group': '120363222222222222@g.us',
        'sender_id': '62812@c.us',
        'text': 'apa',
    }).json()
    assert other_group['should_respond'] is True
    assert other_group['binding_id'] != first['binding_id']


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
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-id-community_seed',
        'role_name': '印尼活跃气氛号',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': ['Kak grup aktif info admin semangat ngobrol bareng semua.'],
        'enabled': True,
    })
    assert first.status_code == 200
    second = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-community_seed',
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
    role_row = next(row for row in rows if row.get('phrase_type') == 'community_seed')

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
    assert 'ga-role-manual-phrases' not in html
    assert '手动补充话术' not in role_modal
    assert 'id="ga_role_phrases"' not in role_modal
    assert 'id="ga_save_manual_role_phrases_btn"' not in role_modal
    assert 'saveRoleManualPhrases' not in html
    assert 'collectManualRolePhrases' not in html
    assert 'function optimisticInsertManualRolePhrases' not in html
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
    assert 'function collectRolePhrases(){const selected=syncRoleEditorSelectedTextsFromDom()||window.__gaRoleEditorSelectedTexts||new Set()' in html
    assert '#ga_role_phrase_pool{display:grid!important;gap:8px!important;max-height:560px!important;overflow:auto!important' in html
    assert 'const visiblePhrases=phrases;' in html
    assert '列表高度约10条，更多向下滚动' in html
    assert 'phrases.slice(0,10)' not in html
    assert 'phrases.slice(0,80)' not in html
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
    assert '触发发言' in html
    assert 'toggleBridgeRelationshipTrigger' in html
    assert '触发规则' in html
    assert 'openTriggerRulesModal' in html
    assert 'trigger_rule_enabled_count' in html
    assert '发言测试' in html
    assert '一键发言' not in html
    assert '立即一键发言' not in html
    assert 'openBridgeManualSendModal' in html
    assert 'sendBridgeRelationshipManualMessage' in html
    assert "group_send_permission_enabled!==false" in html
    assert "trigger_type:'manual_role_bridge_batch'" in html
    assert 'class="primary" ${roleDeleted?\'disabled\':\'\'} onclick="openBridgeManualSendModal' in html
    assert 'class="secondary" ${roleDeleted?\'disabled\':\'\'} onclick="triggerRelationship' in html
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
    assert '/api/ops/group-atmosphere/logs?limit=8' in html
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


def test_group_atmosphere_manual_upload_is_first_generation_card_without_library_section():
    client = make_client()
    page = client.get('/ops/group-atmosphere')
    assert page.status_code == 200
    html = page.text

    assert '<h2>话术库管理</h2>' not in html
    assert 'id="ga_phrase_library_card"' not in html
    candidate_card = html.split('id="ga_candidate_card"', 1)[1].split('id="ga_candidate_list_card"', 1)[0]
    assert 'id="ga_manual_upload_card"' in candidate_card
    assert 'id="ga_learning_upload_card"' in candidate_card
    assert candidate_card.index('id="ga_manual_upload_card"') < candidate_card.index('id="ga_learning_upload_card"')
    assert '人工上传话术' in candidate_card
    assert 'id="ga_manual_upload_btn"' in candidate_card
    assert 'id="ga_clear_manual_phrase_file_btn"' in candidate_card
    assert candidate_card.index('id="ga_manual_upload_btn"') < candidate_card.index('id="ga_clear_manual_phrase_file_btn"')
    assert 'id="ga_phrase_library_result"' in candidate_card
    assert 'ga-manual-upload-compact' in candidate_card
    assert 'ga-manual-upload-head' in candidate_card
    assert 'ga-manual-upload-actions' in candidate_card
    assert 'ga-manual-upload-row' in candidate_card
    assert 'id="ga_manual_upload_region"' not in candidate_card
    assert 'id="ga_manual_upload_type"' not in candidate_card
    assert '每行一条；或选择 txt/csv/xlsx 文件导入' in candidate_card
    assert '#ga_manual_upload_card.ga-manual-upload-compact{width:100%!important;max-width:none!important;padding:16px!important;margin:0!important;display:grid!important;gap:10px!important;}' in html
    assert '#ga_manual_upload_card .ga-manual-upload-row{display:grid!important;grid-template-columns:minmax(280px,520px)!important' in html
    assert '#ga_manual_upload_card textarea#ga_manual_phrase_text{min-height:72px!important;margin:0!important;resize:vertical!important;}' in html


def test_group_atmosphere_ui_contract_normalizes_spacing_typography_and_controls():
    client = make_client()
    page = client.get('/ops/group-atmosphere')
    assert page.status_code == 200
    html = page.text

    # One compact UI contract for the group-atmosphere page: parent gaps own spacing,
    # same-level headings/empty states share a visual system, and controls use a small
    # fixed set of heights rather than ad-hoc per-card sizes.
    expected_markers = [
        ':root{--ga-section-gap:16px;--ga-card-padding:20px;--ga-control-height:38px;--ga-filter-height:34px;--ga-list-control-height:30px;--ga-radius:18px;}',
        '.ga-proto-stack{display:grid!important;gap:var(--ga-section-gap)!important;}',
        '.ga-generation-grid{display:grid!important;gap:16px!important;}',
        '.ga-generation-grid>.ga-upload-panel{margin:0!important;}',
        '#ga_candidate_card .ga-upload-panel h3{font-size:16px!important;line-height:22px!important;margin:0!important;}',
        '.ga-empty-state{min-height:42px!important;padding:11px 12px!important;border-radius:12px!important;background:#f8fafc!important;border:1px solid #e2e8f0!important;color:#64748b!important;font-size:14px!important;line-height:20px!important;display:flex!important;align-items:center!important;}',
        '#ga_accounts.is-empty,#ga_role_library.is-empty{min-height:42px!important;padding:11px 12px!important;border-radius:12px!important;background:#f8fafc!important;border:1px solid #e2e8f0!important;color:#64748b!important;}',
        '#ga_candidate_card button,#ga_role_bridge_card button,#ga_accounts_card button,#ga_role_library_card button{height:var(--ga-control-height)!important;min-height:var(--ga-control-height)!important;display:inline-flex!important;align-items:center!important;justify-content:center!important;margin:0!important;}',
        '#ga_candidate_list_card .ga-candidate-card-actions button,#ga_candidate_list_card .ga-candidate-bulk-move-actions button{height:var(--ga-list-control-height)!important;min-height:var(--ga-list-control-height)!important;}',
        '#ga_candidate_list_card .ga-candidate-bulk-move-actions select{height:var(--ga-list-control-height)!important;min-height:var(--ga-list-control-height)!important;}',
        '#ga_candidate_list_card .ga-candidate-row button{height:var(--ga-list-control-height)!important;min-height:var(--ga-list-control-height)!important;}',
        '#ga_candidate_list_card .ga-candidate-row{margin-top:6px!important;}',
        '#ga_candidate_list_card .ga-candidate-usable-toolbar{min-height:38px!important;}',
        '#ga_candidate_list_card h2{font-size:19px!important;line-height:24px!important;}',
        '#ga_candidate_list_card>.ga-proto-head h2{font-size:19px!important;line-height:24px!important;}',
        '#ga_manual_upload_card.ga-manual-upload-compact{width:100%!important;max-width:none!important;padding:16px!important;margin:0!important;display:grid!important;gap:10px!important;}',
    ]
    for marker in expected_markers:
        assert marker in html

    assert 'margin:14px 0 18px!important' not in html


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

    assert '话术类型' in html
    assert '国家' in html
    assert '装载话术' in html
    assert 'deleteGroupAtmosphereRole' in html

    assert '<h2>话术生成区</h2>' in html
    assert '<h2>话术库管理</h2>' not in html
    assert 'id="ga_phrase_library_card"' not in html
    candidate_card = html.split('id="ga_candidate_card"', 1)[1].split('id="ga_candidate_list_card"', 1)[0]
    assert 'id="ga_manual_upload_card"' in candidate_card
    assert candidate_card.index('id="ga_manual_upload_card"') < candidate_card.index('id="ga_learning_upload_card"')
    assert '人工上传话术' in candidate_card
    assert 'id="ga_manual_upload_btn"' in candidate_card
    assert 'id="ga_clear_manual_phrase_file_btn"' in candidate_card
    assert candidate_card.index('id="ga_manual_upload_btn"') < candidate_card.index('id="ga_clear_manual_phrase_file_btn"')
    assert 'id="ga_phrase_library_result"' in candidate_card
    assert 'ga-manual-upload-compact' in candidate_card
    assert 'ga-manual-upload-head' in candidate_card
    assert 'ga-manual-upload-actions' in candidate_card
    assert 'ga-manual-upload-row' in candidate_card
    assert 'id="ga_manual_upload_region"' not in candidate_card
    assert 'id="ga_manual_upload_type"' not in candidate_card
    assert '每行一条；或选择 txt/csv/xlsx 文件导入' in candidate_card
    assert '#ga_manual_upload_card.ga-manual-upload-compact{width:100%!important;max-width:none!important;padding:16px!important;margin:0!important;display:grid!important;gap:10px!important;}' in html
    assert '#ga_manual_upload_card .ga-manual-upload-row{display:grid!important;grid-template-columns:minmax(280px,520px)!important' in html
    assert '#ga_manual_upload_card textarea#ga_manual_phrase_text{min-height:72px!important;margin:0!important;resize:vertical!important;}' in html
    assert 'id="ga_candidate_count"' not in candidate_card
    assert '<span class="pill gray" id="ga_candidate_count">' not in candidate_card
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
    assert '保存自定义' not in html
    assert '>编辑</button>' in html
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
    assert 'data-ga-image-candidate-entry="text"' in html
    assert 'data-ga-image-candidate-entry="image"' in html
    assert 'ga_image_candidate_modal' in html
    assert 'saveImageCandidate' in html
    assert '新增图片' in html
    assert '图片可选，不选择图片则保存为纯文本话术。' in html
    assert 'openImageCandidateModal' in html


def test_group_atmosphere_p2_ui_clarifies_bridge_generation_and_role_boundaries():
    client = make_client()
    page = client.get('/ops/group-atmosphere')
    assert page.status_code == 200
    html = page.text

    # 桥接操作层级：卡片级批量发送、单群发送、发言测试必须有明确语义。
    assert 'title="向该桥接关系下所有可发群发送"' in html
    assert 'title="仅向本群发送"' in html
    assert 'title="测试该桥接关系首个群的发言链路"' in html
    assert 'openBridgeManualSendModal' in html
    assert 'sendBridgeRelationshipManualMessage' in html
    assert "trigger_type:'manual_role_bridge_batch'" in html

    # 话术生成区：运营端不展示低价值说明文案，保留实际操作入口。
    assert 'ga-generation-help' not in html
    assert '人工上传：先进入审核弹窗，确认后进入备选区' not in html
    assert '文件学习：AI 从聊天记录里提取候选话术' not in html
    assert '确认后才可加入话术角色' not in html
    assert 'id="ga_manual_upload_card"' in html
    assert 'id="ga_learning_upload_card"' in html
    assert 'id="ga_learning_bot_card"' in html

    # 话术类型与话术角色：视觉上分离，类型负责筛选，角色负责装载。
    assert 'ga-candidate-type-label' in html
    assert '话术类型' in html
    assert 'ga-candidate-role-mount-panel' in html
    assert '装载到话术角色' in html
    assert '<option value="">选择要装载的话术角色</option>' in html


def test_group_atmosphere_p3_ui_adds_tooltips_upload_polish_and_runtime_summary():
    client = make_client()
    page = client.get('/ops/group-atmosphere')
    assert page.status_code == 200
    html = page.text

    # 关键短按钮要有 tooltip/aria，避免运营不知道 + 和 译 的含义。
    assert 'id="ga_add_phrase_type_btn" title="新增话术类型" aria-label="新增话术类型"' in html
    assert 'title="候选翻译" aria-label="候选翻译"' in html
    assert 'title="刷新自动调度状态" aria-label="刷新自动调度状态"' not in html

    # 文件上传控件统一成清晰的轻量样式。
    assert 'ga-file-upload-shell' in html
    assert 'ga_manual_phrase_file_label' in html
    assert 'ga_chat_file_label' in html
    assert '选择 txt/csv/xlsx 文件' in html
    assert '选择聊天记录文件' in html
    assert '#ga_candidate_card .ga-file-upload-shell{display:flex!important;align-items:center!important;' in html
    assert 'width:min(520px,100%)!important;max-width:520px!important' in html

    # 运营端不再展示最近发送/调度摘要卡片，避免页面噪音。
    assert 'id="ga_runtime_summary_card"' not in html
    assert '最近发送/调度摘要' not in html
    assert 'id="ga_recent_runtime_logs"' not in html
    assert '暂无发送或调度记录' not in html


def test_group_atmosphere_scheduler_status_api_remains_but_page_card_is_hidden():
    client = make_client({'GROUP_ATMOSPHERE_SCHEDULER_ENABLED': False})
    page = client.get('/ops/group-atmosphere')
    assert page.status_code == 200
    html = page.text

    assert 'id="ga_scheduler_overview_card"' not in html
    assert 'id="ga_scheduler_runtime_status"' not in html
    assert '自动调度状态' not in html
    assert '桥接自动发言开关只代表配置允许' not in html
    assert '最近跳过原因' not in html

    resp = client.get('/api/ops/group-atmosphere/scheduler/status')
    assert resp.status_code == 200
    data = resp.json()
    assert data['scheduler_enabled'] is False
    assert data['scheduler_running'] is False
    assert data['status_label'] == '调度器未启用'
    assert data['auto_enabled_binding_count'] == 0
    assert data['group_send_enabled_count'] == 0
    assert data['last_skip_reason'] == 'scheduler_disabled'


def test_group_atmosphere_role_delete_keeps_related_bindings_as_invalid_relationships():
    client = make_client()
    client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-delete-community_seed',
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
        'role_key': 'role-delete-community_seed',
        'group_targets': ['delete-role-group@g.us'],
    })
    assert bind.status_code == 200
    before = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    assert any(r['role_key'] == 'role-delete-community_seed' for r in before)

    deleted = client.delete('/api/ops/group-atmosphere/roles/role-delete-community_seed')

    assert deleted.status_code == 200
    assert deleted.json()['deleted'] is True
    assert deleted.json()['kept_bindings'] is True
    assert deleted.json()['affected_bindings'] == 1
    after = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    assert all(r['role_key'] != 'role-delete-community_seed' for r in after)
    binding_payload = client.get('/api/ops/group-atmosphere/role-bindings').json()
    bindings = binding_payload['rows']
    assert any(b['role_key'] == 'role-delete-community_seed' for b in bindings)
    kept = next(b for b in bindings if b['role_key'] == 'role-delete-community_seed')
    assert kept['role_deleted'] is True
    assert kept['distribution_status'] == '角色被删除'
    relationships = binding_payload['relationships']
    rel = next(r for r in relationships if r['role_key'] == 'role-delete-community_seed')
    assert rel['role_deleted'] is True
    assert rel['distribution_status'] == '角色被删除'


def test_group_atmosphere_role_delete_keeps_candidate_pool_phrases():
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-community_seed',
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
    assert any(r['role_key'] == 'role-id-community_seed' for r in client.get('/api/ops/group-atmosphere/roles').json()['rows'])

    deleted = client.delete('/api/ops/group-atmosphere/roles/role-id-community_seed')

    assert deleted.status_code == 200
    assert deleted.json()['deleted'] is True
    assert deleted.json()['kept_candidate_pool'] is True
    assert all(r['role_key'] != 'role-id-community_seed' for r in client.get('/api/ops/group-atmosphere/roles').json()['rows'])
    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    texts = [c['text'] for row in pool for c in row['candidates']]
    assert 'Halo kak, jangan lupa kirim ID ya' in texts


def test_candidate_pool_selected_phrases_can_be_added_to_chosen_matching_role_and_reused():
    client = make_client()
    source = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-empty-community_seed',
        'role_name': '空角色容器',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'phrases': [],
        'enabled': True,
    })
    assert empty_role.status_code == 200
    assert empty_role.json()['role']['phrase_count'] == 0
    pool_row = next(row for row in client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows'] if row.get('phrase_type') == 'community_seed')
    candidate_id = pool_row['candidates'][0]['candidate_id']

    added = client.post('/api/ops/group-atmosphere/candidate-pool/add-to-role', json={
        'role_key': 'role-empty-community_seed',
        'source_config_name': pool_row['candidates'][0]['source_config_name'],
        'candidate_ids': [candidate_id],
    })

    assert added.status_code == 200
    assert added.json()['added_count'] == 1
    role = next(r for r in client.get('/api/ops/group-atmosphere/roles').json()['rows'] if r['role_key'] == 'role-empty-community_seed')
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
    assert mismatch.status_code in {400, 404}


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
        'role_key': 'role-id-community_seed',
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
        'role_key': 'role-id-community_seed',
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
    assert '.ga-upload-action-row{display:grid!important;grid-template-columns:minmax(280px,520px) auto auto!important' in html
    assert '#ga_learning_bot_card{display:grid!important' in html



def test_group_atmosphere_candidate_pool_hides_runtime_bindings_and_manual_needs_no_confirmation():
    client = make_client()
    seed_role_and_account(client)

    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'role-id-community_seed',
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
    assert [row['config_name'] for row in community_rows] == ['role-id-community_seed']

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
    row = next(item for item in pool if item.get('phrase_type') == 'community_seed')
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
    assert 'selectedCandidateIdsForConfig(configName){return selectedUsableCandidateIdsForConfig(configName)}' in html
    assert 'setPendingCandidateSelectionForConfig' in html
    assert 'onchange="setPendingCandidateSelectionForConfig' in html
    assert 'onchange="setCandidateSelectionForConfig' not in html
    assert 'querySelectorAll(`[data-ga-pending-candidate-select][data-config-name=' in html
    assert 'const ids=pendingCandidateIdsForConfig(configName);if(!ids.length)throw new Error(\'请先勾选要删除的话术\')' in html
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
    assert all(row['role_key'] != 'role-id-faq_helper' for row in before_roles)

    confirmed = client.post('/api/ops/group-atmosphere/candidate-pool/enable', json={
        'config_name': 'auto-id-faq_helper',
        'candidate_ids': ['upload-1'],
    })
    assert confirmed.status_code == 200
    assert confirmed.json()['plan_only'] is True

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    candidate_row = next(row for row in pool if row.get('phrase_type') == 'faq_helper')
    assert candidate_row['enabled_candidate_count'] == 1

    after_roles = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    assert all(row['role_key'] != 'role-id-faq_helper' for row in after_roles)



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
    created = client.post('/api/ops/group-atmosphere/phrases/manual-upload', json={
        'config_name': 'auto-id-community_seed',
        'role_name': '上传候选池',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'content': 'Halo kak, jangan malu ngobrol di grup ya.',
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
        'role_key': 'role-id-community_seed',
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
    row = next(item for item in pool if item.get('phrase_type') == 'community_seed')
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

def test_role_binding_today_count_uses_worker_message_id_logs_not_cache():
    client = make_client()
    seed_role_and_account(client)
    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'role-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'enabled': True,
        'auto_speaking_enabled': True,
        'group_send_permission_enabled': True,
        'daily_max_messages': 999,
        'min_interval_minutes': 0,
        'worker_base_url': '',
    })
    assert created.status_code == 200
    binding_id = created.json()['bindings'][0]['binding_id']
    service = client.app.state.service
    today = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).date().isoformat()
    with service.db.connect() as conn:
        conn.execute(
            "UPDATE whatsapp_group_atmosphere_role_bindings SET sent_count_today=81, sent_count_date=? WHERE binding_id=?",
            (today, binding_id),
        )
        conn.execute(
            "INSERT INTO whatsapp_group_atmosphere_logs (log_id, config_name, account_key, target_group, direction, trigger_type, message_text, status, result_code, result_reason, raw_result, created_at) VALUES (?, ?, ?, ?, 'outbound', 'scheduled_auto', 'dry run only', 'success', 'dry_run', '', ?, ?)",
            ('walog-fake-dry', f'binding-{binding_id}', 'atmosphere-indo-01', 'group-a@g.us', '{"dry_run": true}', today + 'T00:00:00+00:00'),
        )
        conn.commit()

    row = service.get_group_atmosphere_role_binding(binding_id)
    assert row['sent_count_today'] == 0

    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO whatsapp_group_atmosphere_logs (log_id, config_name, account_key, target_group, direction, trigger_type, message_text, status, result_code, result_reason, raw_result, created_at) VALUES (?, ?, ?, ?, 'outbound', 'scheduled_auto', 'real send', 'success', 'sent', '', ?, ?)",
            ('walog-real-send', f'binding-{binding_id}', 'atmosphere-indo-01', 'group-a@g.us', '{"message_id": "msg-real-1"}', today + 'T00:01:00+00:00'),
        )
        conn.commit()

    row = service.get_group_atmosphere_role_binding(binding_id)
    assert row['sent_count_today'] == 1

def test_role_binding_successful_trigger_writes_binding_event_ledger_and_count(monkeypatch):
    client = make_client()
    seed_role_and_account(client)

    class FakeResponseWithMessageId:
        status_code = 200
        text = '{"status":"sent","message_id":"msg-binding-ledger-1"}'

        def json(self):
            return {'status': 'sent', 'message_id': 'msg-binding-ledger-1', 'result_code': 'sent'}

    monkeypatch.setattr('app.main.requests.post', lambda url, json=None, timeout=None: FakeResponseWithMessageId())
    created = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': 'role-id-community_seed',
        'account_key': 'atmosphere-indo-01',
        'group_indexes': [0],
        'enabled': True,
        'auto_speaking_enabled': True,
        'group_send_permission_enabled': True,
        'daily_max_messages': 999,
        'min_interval_minutes': 0,
        'worker_base_url': 'http://worker.local',
    })
    assert created.status_code == 200
    binding_id = created.json()['bindings'][0]['binding_id']

    triggered = client.post(f'/api/ops/group-atmosphere/role-bindings/{binding_id}/trigger')
    assert triggered.status_code == 200
    assert triggered.json()['sent'] is True

    service = client.app.state.service
    rows = service.db.connect().execute(
        "SELECT * FROM mcn_event_ledger WHERE object_type='group_atmosphere_binding' AND object_key=?",
        (binding_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]['event_type'] == 'group_message_sent'
    assert rows[0]['evidence_level'] == 'whatsapp_message_id'
    assert rows[0]['external_id'] == 'msg-binding-ledger-1'

    binding = service.get_group_atmosphere_role_binding(binding_id)
    assert binding['sent_count_today'] == 1

