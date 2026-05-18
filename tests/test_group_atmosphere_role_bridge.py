from fastapi.testclient import TestClient

from app.main import create_app


def make_client(settings=None):
    cfg = {"DB_PATH": ":memory:", "AUTO_LARK_REPLY": False}
    if settings:
        cfg.update(settings)
    return TestClient(create_app(cfg))


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


def test_learning_account_is_silent_and_updates_candidate_pool_from_chat_records(monkeypatch):
    client = make_client()
    created = client.post('/api/ops/group-atmosphere/learning-accounts', json={
        'learning_account_key': 'learn-indo-01',
        'account_name': '印尼学习话术号01',
        'region': '印尼',
        'enabled': True,
        'worker_base_url': 'http://learning-worker.local',
        'group_links': [{'target_group': 'group-a@g.us', 'group_name': '印尼A群'}],
        'target_role_keys': ['auto-id-community_seed'],
        'daily_learning_time': '03:00',
    })
    assert created.status_code == 200
    account = created.json()['account']
    assert account['responsible_type'] == 'group_atmosphere_learning'
    assert account['language'] == 'id'
    assert account['silent_learning_only'] is True

    fetch_calls = []

    class FakeFetchResponse:
        status_code = 200
        text = '{"status":"success"}'

        def json(self):
            return {
                'status': 'success',
                'result_code': 'messages_fetched',
                'records': [
                    {'sender': 'user1', 'text': 'Halo kak, gmn cara mulai?', 'created_at': '2026-05-15T03:00:00Z'},
                    {'sender': 'admin', 'text': 'Jgn lupa krm ID dan kode ke admin ya kak.', 'created_at': '2026-05-15T03:01:00Z'},
                ],
            }

    def fake_post(url, json=None, timeout=None):
        fetch_calls.append({'url': url, 'json': json, 'timeout': timeout})
        return FakeFetchResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)
    learned = client.post('/api/ops/group-atmosphere/learning-accounts/learn-indo-01/learn-once', json={})
    assert fetch_calls == [{
        'url': 'http://learning-worker.local/fetch-group-messages',
        'json': {'target_group': 'group-a@g.us', 'limit': 300},
        'timeout': 30,
    }]
    assert learned.status_code == 200
    body = learned.json()
    assert body['ok'] is True
    assert body['silent_learning_only'] is True
    assert body['imported_count'] == 2
    assert body['candidate_count'] >= 1

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    role = next(row for row in pool if row['config_name'] == 'auto-id-community_seed')
    assert role['source_types'] == ['learning_account']
    assert role['enabled_candidate_count'] == 0
    assert all(candidate['safe_to_send'] is False for candidate in role['candidates'])


def test_group_atmosphere_page_exposes_role_bridge_and_custom_phrase_entry():
    client = make_client()
    page = client.get('/ops/group-atmosphere')
    assert page.status_code == 200
    html = page.text
    assert '角色挂载' in html
    assert '话术角色' in html
    assert '学习话术号' not in html
    assert '静默学习' not in html
    assert '手动新增话术' in html
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


def test_group_atmosphere_page_matches_role_modal_and_bridge_card_ux():
    client = make_client()
    page = client.get('/ops/group-atmosphere')
    assert page.status_code == 200
    html = page.text
    assert 'id="ga_new_role_btn"' in html
    assert 'id="ga_role_editor_modal"' in html
    assert html.index('id="ga_accounts_card"') < html.index('id="ga_new_account_btn"')
    assert html.index('id="ga_new_account_btn"') < html.index('id="ga_accounts"')
    assert 'WhatsApp账号' in html
    assert 'openRoleEditor' in html
    assert '从话术池选择话术' in html
    assert 'ga_role_phrase_pool' in html
    assert 'ga-bridge-layout' in html
    assert 'renderBridgeRelationships' in html
    assert 'toggleBridgeGroupPermission' in html
    assert '最多10群/关系' not in html
    assert '最大间隔分钟' in html
    assert '开启自动发言' in html
    assert '立即一键发言' in html
    assert '<h2>话术角色</h2>' in html


def test_group_atmosphere_page_removes_teaching_copy_and_prioritizes_account_operations():
    client = make_client()
    page = client.get('/ops/group-atmosphere')
    assert page.status_code == 200
    html = page.text

    assert html.index('id="ga_role_bridge_card"') < html.index('id="ga_accounts_card"')
    assert html.index('id="ga_accounts_card"') < html.index('id="ga_role_library_card"')
    assert '<h1>群聊天助手</h1>' in html
    assert '群聊天助手 · 话术角色分发控制台' not in html
    assert '结构重点：话术角色是容器' not in html
    assert '发送判定优先级' not in html
    assert '角色挂载' in html
    assert '话术角色</h3>' in html
    assert 'WhatsApp 群组</h3>' in html
    assert '桥接关系</h3>' in html
    assert html.index('id="ga_speech_plan_library_card"') < html.index('候选话术')
    assert 'id="ga_learning_upload_card"' not in html
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

    assert '<h1>群聊天助手</h1>' in html
    assert '桥接区优先' not in html
    assert '最多10群/关系' not in html
