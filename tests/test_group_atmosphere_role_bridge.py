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
    assert '桥接操作区' in html
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
    assert 'WhatsApp 账号与群组' in html
    assert 'openRoleEditor' in html
    assert '从话术备选区选择话术' in html or '话术备选区' in html
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
    assert '群聊天助手' in html
    assert '群聊天助手 · 话术角色分发控制台' not in html
    assert '结构重点：话术角色是容器' not in html
    assert '发送判定优先级' not in html
    assert '桥接操作区' in html
    assert '选择话术角色' in html
    assert '目标群组' in html
    assert 'WhatsApp 账号与群组' in html
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
    assert '每日上限' in html
    assert '最小间隔分钟' in html
    assert '最大间隔分钟' in html

    assert '账号用途' not in html
    assert '运行状态' not in html
    assert '登录状态' in html
    assert '真实群名' in html
    assert '群链接' in html
    assert '生效状态' in html

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
    assert '已生成' in html
    assert '下一步接入解析入库' not in html
    assert '后端学习接口待接入文件解析' not in html
    assert '新增学习机器人WhatsApp账号' in html
    assert 'id="ga_learning_account_modal"' in html
    assert 'id="ga_open_learning_bot_modal_btn"' in html
    assert 'id="ga_learning_group_links"' in html
    assert 'id="ga_add_learning_group_link_btn"' in html
    assert 'id="ga_save_learning_bot_btn"' in html
    assert 'id="ga_close_learning_bot_modal_btn"' in html
    assert '学习机器人只读取群消息生成话术，不参与桥接发言。' not in html
    assert '正在生成学习机器人二维码…' not in html

    assert '<h2>话术备选区</h2>' in html
    assert '气氛活跃型' in html
    assert '解惑答疑型' in html
    assert '教程引导型' in html
    assert 'data-ga-candidate-select' in html
    assert '保存自定义' in html
    assert '加入角色' in html
    assert '保存至此话术角色' not in html
    assert '保存至话术角色' not in html
    assert 'data-ga-candidate-role-select' not in html
    assert 'saveSelectedCandidatesToRole' not in html
    assert 'id="ga_batch_save_candidates_btn"' not in html
    assert 'batchSaveCandidatesToRole' not in html


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

    # 话术角色保存后要刷新角色/桥接/备选下拉，而不是只关弹窗。
    assert 'await loadRoleBridge();renderCandidatePool(window.__gaCandidateRows||[])' in html
    assert '话术角色已保存，列表已更新' in html

    # 话术备选区改成低高度单行编辑，保留单条保存自定义/加入角色；移除分组级角色下拉和“保存至此话术角色”。
    assert 'data-ga-candidate-role-select' not in html
    assert '保存至此话术角色' not in html
    assert '保存至话术角色' not in html
    assert 'id="ga_batch_save_candidates_btn"' not in html
    assert 'candidate-row-compact' in html
    assert 'data-ga-candidate-text' in html
    assert '<textarea data-ga-candidate-text' not in html
    assert 'saveSelectedCandidatesToRole' not in html

    # 所有新按钮/区域都要有可见反馈，不只依赖顶部 ga_action_feedback。
    assert 'ga_learning_result' in html
    assert 'ga_candidate_result' in html
    assert 'setLocalFeedback' in html


def test_group_atmosphere_role_delete_removes_role_and_related_bindings():
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
    after = client.get('/api/ops/group-atmosphere/roles').json()['rows']
    assert all(r['role_key'] != 'delete-role-id-community_seed' for r in after)
    bindings = client.get('/api/ops/group-atmosphere/role-bindings').json()['rows']
    assert all(b['role_key'] != 'delete-role-id-community_seed' for b in bindings)


def test_group_atmosphere_page_deletes_role_through_delete_api_not_only_unbind():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    assert "DELETE" in html
    assert "/api/ops/group-atmosphere/roles/${encodeURIComponent(roleKey)}" in html
    assert "话术角色已删除" in html


def test_group_atmosphere_buttons_have_local_feedback_targets_for_modals_and_sections():
    client = make_client()
    html = client.get('/ops/group-atmosphere').text
    assert 'const gaActionLocalTargets=' in html
    expected_pairs = {
        "'保存桥接关系':'ga_role_bridge_result'",
        "'删除桥接':'ga_role_bridge_result'",
        "'保存话术角色':'ga_role_editor_result'",
        "'保存学习机器人':'ga_learning_result'",
        "'上传学习':'ga_upload_result'",
        "'加入话术方案':'ga_candidate_result'",
        "'自动发言检查':'ga_scheduler_result'",
        "'发送群消息':'ga_send_result'",
        "'保存':'ga_session_status'",
    }
    for marker in expected_pairs:
        assert marker in html
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
    script = html.split('<script>', 1)[1].split('</script>', 1)[0]
    assert 'ga_daily_max_messages.value' not in script
    assert 'ga_min_interval_minutes.value' not in script
    assert 'ga_max_interval_minutes.value' not in script


def test_group_atmosphere_account_modal_uses_clear_phrase_variation_copy_and_refined_clear_button():
    client = make_client()
    response = client.get('/ops/group-atmosphere')
    assert response.status_code == 200
    html = response.text

    assert 'id="ga_randomness_level"' in html
    assert '话术变化：稳定' in html
    assert '话术变化：适中' in html
    assert '话术变化：灵活' in html
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
