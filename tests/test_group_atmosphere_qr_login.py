import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import create_app


def make_client(settings=None):
    cfg = {"DB_PATH": ":memory:", "AUTO_LARK_REPLY": False}
    if settings:
        cfg.update(settings)
    return TestClient(create_app(cfg))


def test_group_atmosphere_page_has_unique_button_safe_dom_ids_and_feedback_guard():
    client = make_client()
    response = client.get('/ops/group-atmosphere')
    assert response.status_code == 200
    html = response.text

    assert html.count('id="ga_role_positioning"') == 1
    assert 'id="ga_account_role_positioning"' in html
    assert 'ga_account_role_positioning.value' in html
    assert 'Promise.resolve(handler()).catch' in html
    assert 'setFeedback(`操作失败：${err.message||err}`' in html


def test_group_atmosphere_login_refresh_does_not_auto_select_learning_or_simulation_selectors():
    client = make_client()
    response = client.get('/ops/group-atmosphere')
    assert response.status_code == 200
    html = response.text

    assert 'function selectedOperationalAccountKey()' in html
    assert "function setSelectedAtmosphereAccountKey(key){ga_account_key_login.value=key||'';ga_account_key.value=key||''}" in html
    assert "if(uploadSelect)uploadSelect.value=key||''" not in html
    assert "if(simSelect)simSelect.value=key||''" not in html
    assert "if(candidateSelect)candidateSelect.value=key||''" not in html
    assert "const uploadCurrent=uploadSelect?uploadSelect.value:''" not in html
    assert 'ga_tool_account_select' not in html
    assert "const simCurrent=simSelect?simSelect.value:''" not in html
    assert "const candidateCurrent=candidateSelect?candidateSelect.value:''" not in html
    assert "const key=document.getElementById('ga_sim_account_select')?.value.trim()||''" not in html


def test_group_atmosphere_candidate_pool_exposes_own_account_selector_and_action_feedback():
    client = make_client()
    response = client.get('/ops/group-atmosphere')
    assert response.status_code == 200
    html = response.text

    assert 'ga_candidate_language_filter' in html
    assert 'ga_candidate_role_filter' in html
    assert 'ga_candidate_account_select' not in html
    assert 'ga_candidate_group_select' not in html
    assert '<select id="ga_candidate_language_filter"><option value="id" selected>印尼</option>' in html
    assert '<option value="">语言/地区</option>' not in html
    assert '角色' in html
    assert '选择投放账号' not in html
    assert '选择发言群</option>' not in html
    assert '群聊天助手' in html
    assert '发言桥接区' in html
    assert '新增桥接' in html
    assert 'data-layout="ops-workbench-redesign"' in html
    assert 'data-layout-zone="role-group-bridge"' in html
    assert 'data-layout-zone="whatsapp-resource-pool"' in html
    assert 'data-layout-zone="speech-roles"' in html
    assert 'data-layout-zone="phrase-generation"' in html
    assert '发言机器人配置' in html
    assert 'ga_bridge_role_select' in html
    assert 'ga_bridge_group_choices' in html
    assert 'ga_mount_role_btn' in html
    assert 'ga_bridge_account_select' not in html
    assert '选择WhatsApp账号' not in html
    assert '选择WhatsApp号码' not in html
    assert '已找到可用发言账号' not in html
    assert '国家一致' not in html
    assert '无角色类型冲突' not in html
    assert '话术角色' in html
    assert '学习话术号' not in html
    assert '新增话术角色' in html
    assert '手动新增话术' not in html
    assert '/api/ops/group-atmosphere/role-bindings' in html
    assert '/api/ops/group-atmosphere/roles/manual-phrases' in html
    assert '逐群装载' not in html
    assert '请选择话术方案' not in html
    assert 'ga_group_1_plan' not in html
    assert '话术方案库' not in html
    assert '删除话术包' not in html
    assert 'ga_candidate_result' in html
    assert 'filterCandidateRows' in html
    assert 'speechPlanRows' in html
    assert "data-ga-enable-candidate" not in html
    assert "candidateButton.dataset.configName" not in html
    assert "saveSelectedCandidatesToRole" in html
    assert "ga_candidate_target_role_select" in html
    assert "ga_batch_add_candidates_to_role_btn" in html
    assert "新增话术" in html
    assert "新增图片" in html
    assert 'ga_open_image_candidate_modal_btn' in html
    assert 'data-ga-image-candidate-entry="image"' in html
    assert "ga-candidate-manual-draft" in html
    assert "openImageCandidateModal" in html
    assert "saveImageCandidate" in html
    assert "saveCustomCandidate" in html
    assert "/api/ops/group-atmosphere/candidate-pool/custom" in html
    assert "人工写入" in html
    assert "candidateSourceLabel(item)" in html
    assert "保存自定义" not in html
    assert ">编辑</button>" in html
    assert "图片可选" in html
    assert "未真正发送" in html


def test_group_atmosphere_speech_plan_library_filters_delivery_configs_and_supports_rename_delete():
    client = make_client()
    source = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'auto-id-community_seed',
        'enabled': True,
        'account_key': '',
        'target_group': '',
        'group_name': '印尼欢迎话术包',
        'language': 'id',
        'template_pool': [{'candidate_id': 'c1', 'text': 'Halo kak', 'enabled': True, 'safe_to_send': True, 'source_role': 'community_seed'}],
        'status': 'plan_ready',
    })
    assert source.status_code == 200
    delivery = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'deliver-auto-id-community_seed-atmosphere-indo-01-group-1',
        'enabled': True,
        'account_key': 'atmosphere-indo-01',
        'target_group': 'group-1@g.us',
        'group_name': 'Group 1',
        'language': 'id',
        'template_pool': [{'candidate_id': 'c1', 'text': 'Halo kak', 'enabled': True, 'safe_to_send': True, 'source_role': 'community_seed'}],
        'status': 'enabled',
    })
    assert delivery.status_code == 200
    account = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-indo-01',
        'account_name': '印尼群活跃01',
        'region': '印尼',
        'groups': [{'target_group': 'group-1@g.us', 'group_name': 'Group 1', 'enabled': True, 'speech_plan_config_name': 'auto-id-community_seed'}],
        'enabled': True,
    })
    assert account.status_code == 200

    pool = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    assert [row['config_name'] for row in pool] == ['auto-id-community_seed']
    assert pool[0]['plan_display_name'] == '印尼欢迎话术包'
    assert pool[0]['usage_count'] == 1
    assert pool[0]['usage'][0]['account_name'] == '印尼群活跃01'
    assert pool[0]['usage'][0]['group_name'] == 'Group 1'

    renamed = client.post('/api/ops/group-atmosphere/speech-plans/auto-id-community_seed/rename', json={
        'plan_display_name': '印尼新人欢迎包',
    })
    assert renamed.status_code == 200
    assert renamed.json()['config']['group_name'] == '印尼新人欢迎包'
    pool_after_rename = client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows']
    assert pool_after_rename[0]['plan_display_name'] == '印尼新人欢迎包'

    deleted = client.delete('/api/ops/group-atmosphere/speech-plans/auto-id-community_seed')
    assert deleted.status_code == 200
    assert deleted.json()['cleared_reference_count'] == 1
    assert client.get('/api/ops/group-atmosphere/candidate-pool').json()['rows'] == []
    rows = client.get('/api/ops/group-atmosphere/accounts').json()['rows']
    assert rows[0]['groups'][0]['speech_plan_config_name'] == ''


def test_group_atmosphere_page_exposes_qr_login_console():
    client = make_client()
    response = client.get('/ops/group-atmosphere')
    assert response.status_code == 200
    html = response.text
    assert '群聊天助手' in html
    assert '账号 key' not in html
    assert 'ga_account_key_login' in html
    assert 'ga_region' in html
    assert '墨西哥' in html
    assert '巴西' in html
    assert 'ga_role_positioning' in html
    account_modal = html.split('id="ga_editor_modal"', 1)[1].split('id="ga_role_editor_modal"', 1)[0]
    assert 'ga_randomness_level' not in account_modal
    assert 'ga_group_rows' in html
    assert 'ga_add_group_btn' in html
    assert '+ 增加发言群' in html
    assert 'ga_account_enabled' in html
    assert 'ga_action_feedback' in html
    assert 'ga_chat_file' in html
    assert 'ga_clear_chat_files_btn' in html
    assert 'ga_tool_account_select' not in html
    assert 'ga_sim_account_select' not in html
    assert '<h2>话术学习</h2>' not in html
    assert '上传并学习' in html
    assert '学习机器人区' in html
    assert '话术文件学习' in html
    assert '自动分配到话术库' not in html
    assert '话术方案库' not in html
    assert 'humanizeGaUploadError' in html
    assert '删除话术包' not in html
    assert '<h2>话术库管理</h2>' not in html
    assert 'id="ga_phrase_library_card"' not in html
    assert '话术生成区' in html
    assert '话术备选区' in html
    assert '自动发言' in html
    assert 'data-layout="ops-workbench-redesign"' in html
    assert '发言机器人配置' in html
    assert '已启用账号' not in html
    assert '检查可发送' in html
    assert '后台会按每日上限与间隔自动随机发送' not in html
    assert 'ga_max_interval_minutes' not in html
    assert 'ga_bridge_max_interval' in html
    assert 'stopAtmosphereSchedulerLoop' not in html
    assert 'toggleAtmosphereSchedulerLoop' not in html
    assert 'ga_scheduler_running' not in html
    assert '/api/ops/group-atmosphere/candidate-pool' in html
    assert '/api/ops/group-atmosphere/scheduler/run-due' in html
    assert '请先在聊天记录区域选择账号' not in html
    assert '请先在发送前预览区域选择账号' not in html
    assert 'id="ga_start_qr_btn"' not in html
    assert 'id="ga_refresh_session_btn"' not in html
    assert 'class="status-grid"' not in html
    assert 'ga_status_account' not in html
    assert 'ga_status_runtime' not in html
    assert 'ga_status_login' not in html
    assert 'removeGroupRow' in html
    assert '删除群组' in html
    assert 'toggleAtmosphereAccountEnabled' in html
    assert 'toggleAtmosphereGroupEnabled' in html
    assert 'account-card' in html
    assert 'account-status-grid' in html
    assert '账号用途' not in html
    assert '账号已启用' not in html
    assert '账号启用中' not in html
    assert '运行状态' not in html
    assert '登录状态' not in html
    assert 'account-title-row' in html
    assert '已登录·' in html
    assert "?'运行中':'未运行'" in html
    assert '登录中…' in html
    assert '#ga_accounts .group-card{padding:8px 10px!important' in html
    assert '#ga_accounts .group-card-title{display:grid!important;grid-template-columns:minmax(0,1fr) auto!important' in html
    assert '${health}' in html
    assert '当前账号' not in html
    assert 'Runtime' not in html
    assert 'startAtmosphereQrForAccount' in html
    assert 'gaQrModal' in html
    assert 'qr-modal-card' in html
    assert 'openAtmosphereQrModal' in html
    assert 'closeAtmosphereQrModal' in html
    assert 'retryAtmosphereQrModal' in html
    assert 'refreshAtmosphereQrModal' in html
    assert '二维码会显示在这里' not in html
    assert 'group-card-grid' in html
    assert 'group-card' in html
    assert 'groupAutoSpeakState' in html
    account_renderer = html.split('function renderAccounts(rows)', 1)[1].split('function openManualSendModal', 1)[0]
    assert '真实群名' not in account_renderer
    assert '群链接' not in account_renderer
    assert '生效状态' not in account_renderer
    assert 'group-card-link' in account_renderer
    assert 'g.target_group' in account_renderer
    assert '群名待探测' in html
    assert '自动发言已开启' in html
    assert '开启自动发言' in html
    assert '待登录生效' not in html
    assert '允许发言' not in html
    assert '生成二维码' in html
    assert '手动发言' in html
    assert 'gaSendModal' in html
    assert '确认发送' in html
    assert '可直接粘贴图片，发送时会作为图文消息。' in html
    assert 'id="ga_manual_media_preview"' in html
    assert 'function handleManualMessagePaste' in html
    assert "payload.media_id=media.media_id" in html
    assert '/api/ops/group-atmosphere/media-assets' in html
    assert '/groups/${ctx.groupIndex}/send' in html
    assert '扫码成功 · 已登录' in html
    assert '扫码登录成功，账号已可用' in html
    assert '扫码登录成功，账号已可用于群聊天助手。' in html
    assert '删除' in html
    assert '/api/ops/group-atmosphere/accounts' in html
    assert '/api/ops/group-atmosphere/accounts/${encodeURIComponent(key)}/session/' in html
    assert '群审批控制台' in html
    nav_registration = html.index('/ops/registration-group-approval-batch-members')
    nav_group_chat = html.index('/ops/group-atmosphere')
    nav_accounts = html.index('/ops/accounts')
    assert nav_registration < nav_group_chat < nav_accounts


def test_group_atmosphere_role_bridge_auto_assigns_whatsapp_account_by_role_and_group():
    client = make_client()
    role = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-pt-community_seed',
        'role_name': '巴西活跃BOT',
        'region': '巴西',
        'language': 'pt',
        'role_positioning': 'community_seed',
        'phrases': ['Oi gente'],
        'enabled': True,
    })
    assert role.status_code == 200
    role_key = role.json()['role']['role_key']
    assert role_key.startswith('role-')
    account = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-br-01',
        'account_name': '+55 11 90000 2233',
        'region': '巴西',
        'language': 'pt',
        'role_positioning': 'community_seed',
        'daily_max_messages': 20,
        'min_interval_minutes': 30,
        'max_interval_minutes': 90,
        'groups': [{'target_group': 'br-group-1@g.us', 'group_name': '巴西群01', 'enabled': True}],
        'enabled': True,
    })
    assert account.status_code == 200

    response = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': role_key,
        'group_targets': ['br-group-1@g.us'],
        'auto_speaking_enabled': True,
    })

    assert response.status_code == 200
    body = response.json()
    assert body['created_count'] == 1
    binding = body['bindings'][0]
    assert binding['role_key'] == role_key
    assert binding['target_group'] == 'br-group-1@g.us'
    assert binding['account_key'] == 'atmosphere-br-01'
    assert binding['assigned_account_label'] == '+55 11 90000 2233'
    assert binding['daily_max_messages'] == 20
    assert binding['min_interval_minutes'] == 30
    assert binding['max_interval_minutes'] == 90
    rel = body['relationship']
    assert rel['role_key'] == role_key
    assert rel['groups'][0]['assigned_account_label'] == '+55 11 90000 2233'


def test_group_atmosphere_role_bridge_rejects_country_mismatch_without_checklist_noise():
    client = make_client()
    role = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-pt-community_seed',
        'role_name': '巴西活跃BOT',
        'region': '巴西',
        'language': 'pt',
        'role_positioning': 'community_seed',
        'phrases': ['Oi gente'],
        'enabled': True,
    })
    assert role.status_code == 200
    role_key = role.json()['role']['role_key']
    client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-indo-01',
        'account_name': '+62 812 0000 7788',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'community_seed',
        'groups': [{'target_group': 'id-group-1@g.us', 'group_name': '印尼群01', 'enabled': True}],
        'enabled': True,
    })

    response = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': role_key,
        'group_targets': ['id-group-1@g.us'],
    })

    assert response.status_code == 400
    assert '国家/地区不一致' in response.json()['detail']


def test_group_atmosphere_account_upsert_and_list_without_approval_requirements(monkeypatch):
    client = make_client()
    service = client.app.state.service
    health_calls = {'count': 0}

    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda account_key: {
        'pid': 43210,
        'port': 59999,
        'base_url': 'http://127.0.0.1:59999',
        'auth_path': str(service._whatsapp_approval_session_auth_path(account_key)),
        'client_id': service._whatsapp_approval_session_client_id(account_key),
    } if str(account_key or '').startswith('atmosphere-') else {})
    monkeypatch.setattr(service, '_pid_running', lambda pid: True)

    def fail_health(_base_url):
        health_calls['count'] += 1
        raise AssertionError('group atmosphere save/list must not block on live worker health')

    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', fail_health)

    response = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_name': '印尼群活跃01',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'newcomer_guide',
        'randomness_level': 'medium',
        'daily_max_messages': 3,
        'min_interval_minutes': 120,
        'groups': [
            {'target_group': '120363400336474261@g.us', 'group_name': 'ID Group', 'enabled': True},
            {'target_group': 'https://chat.whatsapp.com/ABCDEFG', 'group_name': 'Backup Group', 'enabled': False},
            {'target_group': '120363400336474262@g.us', 'group_name': 'Third Group', 'enabled': True},
        ],
        'enabled': False,
    })
    assert response.status_code == 200
    body = response.json()
    assert body['ok'] is True
    assert body['account_key'].startswith('atmosphere-indo-')
    assert body['account']['account_key'] == body['account_key']
    assert body['account']['responsible_type'] == 'group_atmosphere'
    assert body['account']['region'] == '印尼'
    assert body['account']['role_positioning'] == 'newcomer_guide'
    assert body['account']['randomness_level'] == 'medium'
    assert body['account']['group_count'] == 3
    assert body['account']['enabled'] is False
    assert body['runtime']['mode'] == 'dedicated_runtime'

    rows = client.get('/api/ops/group-atmosphere/accounts').json()['rows']
    assert len(rows) == 1
    assert rows[0]['account_key'] == body['account_key']
    assert rows[0]['target_group'] == '120363400336474261@g.us'
    assert rows[0]['groups'][0]['enabled'] is False
    assert rows[0]['groups'][1]['enabled'] is False
    assert rows[0]['groups'][2]['enabled'] is False
    assert rows[0]['enabled'] is False
    assert health_calls['count'] == 0

    planned = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': body['account_key'],
        'account_name': '印尼群活跃01',
        'region': '印尼',
        'language': 'id',
        'role_positioning': 'newcomer_guide',
        'daily_max_messages': 3,
        'min_interval_minutes': 120,
        'groups': [
            {'target_group': '120363400336474261@g.us', 'group_name': 'ID Group', 'enabled': True, 'speech_plan_config_name': 'plan-id-newcomer'},
            {'target_group': 'https://chat.whatsapp.com/ABCDEFG', 'group_name': 'Backup Group', 'enabled': True, 'speech_plan_config_name': 'plan-id-faq'},
        ],
        'enabled': True,
    })
    assert planned.status_code == 200
    planned_rows = client.get('/api/ops/group-atmosphere/accounts').json()['rows']
    assert planned_rows[0]['groups'][0]['speech_plan_config_name'] == 'plan-id-newcomer'
    assert planned_rows[0]['groups'][1]['speech_plan_config_name'] == 'plan-id-faq'

    too_many = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_name': 'too many',
        'region': '印尼',
        'groups': [{'target_group': f'g{i}@g.us'} for i in range(11)],
    })
    assert too_many.status_code == 400
    assert 'at most 5 groups' in too_many.json()['detail']

    deleted = client.delete(f"/api/ops/group-atmosphere/accounts/{body['account_key']}")
    assert deleted.status_code == 200
    assert deleted.json()['deleted'] is True


def test_group_atmosphere_account_list_uses_lightweight_snapshot_without_live_probe(monkeypatch):
    client = make_client()
    service = client.app.state.service
    response = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_name': '印尼群活跃01',
        'region': '印尼',
        'groups': [{'target_group': 'https://chat.whatsapp.com/ABCDEFG', 'enabled': True}],
        'enabled': True,
    })
    account_key = response.json()['account_key']

    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: {
        'pid': 43210,
        'port': 59999,
        'base_url': 'http://127.0.0.1:59999',
        'auth_path': str(service._whatsapp_approval_session_auth_path(key)),
        'client_id': service._whatsapp_approval_session_client_id(key),
        'started_at': datetime.now(timezone.utc).isoformat(),
    } if key == account_key else {})
    monkeypatch.setattr(service, '_pid_running', lambda pid: True)
    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', lambda base_url: (_ for _ in ()).throw(AssertionError('list must not call worker health')))

    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append({'url': url, 'json': json, 'timeout': timeout})
        raise AssertionError('list must not probe group state')

    monkeypatch.setattr('app.main.requests.post', fake_post)

    rows = client.get('/api/ops/group-atmosphere/accounts').json()['rows']

    assert rows[0]['groups'][0]['group_name'] == 'https://chat.whatsapp.com/ABCDEFG'
    assert rows[0]['groups'][0]['group_id'] == ''
    assert calls == []

    rows_again = client.get('/api/ops/group-atmosphere/accounts').json()['rows']
    assert rows_again[0]['groups'][0]['group_name'] == 'https://chat.whatsapp.com/ABCDEFG'


def test_group_atmosphere_new_account_without_runtime_shows_not_logged_in(monkeypatch):
    client = make_client()
    service = client.app.state.service
    response = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-new-no-runtime',
        'account_name': '新发言号未登录',
        'region': '印尼',
        'groups': [{'target_group': 'https://chat.whatsapp.com/NEWLOGIN', 'enabled': True}],
        'enabled': True,
    })
    assert response.status_code == 200
    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: {})
    monkeypatch.setattr(service, '_pid_running', lambda pid: False)
    monkeypatch.setattr(service, '_whatsapp_approval_has_local_auth_session', lambda key: False)
    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', lambda base_url: (_ for _ in ()).throw(AssertionError('list must not call worker health')))

    row = client.get('/api/ops/group-atmosphere/accounts').json()['rows'][0]

    assert row['runtime']['active'] is False
    assert row['session']['login_verified'] is False
    assert row['session']['login_check_status'] == 'not_logged_in'
    assert row['session']['login_check_message'] == '未登录，请点击二维码登录。'
    assert row['session']['qr_available'] is False


def test_group_atmosphere_account_list_reflects_authenticated_runtime_even_when_account_disabled(monkeypatch):
    client = make_client()
    service = client.app.state.service
    response = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-indo-disabled',
        'account_name': '印尼发言号禁用但已登录',
        'region': '印尼',
        'groups': [{'target_group': 'https://chat.whatsapp.com/DISABLED', 'enabled': True}],
        'enabled': False,
    })
    assert response.status_code == 200
    account_key = response.json()['account_key']
    auth_path = str(service._whatsapp_approval_session_auth_path(account_key))
    client_id = service._whatsapp_approval_session_client_id(account_key)
    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: {
        'pid': 43211,
        'port': 59996,
        'base_url': 'http://127.0.0.1:59996',
        'auth_path': auth_path,
        'client_id': client_id,
        'started_at': datetime.now(timezone.utc).isoformat(),
    } if key == account_key else {})
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

    rows = client.get('/api/ops/group-atmosphere/accounts').json()['rows']

    assert health_calls == ['http://127.0.0.1:59996']
    assert rows[0]['enabled'] is False
    assert rows[0]['runtime']['authenticated'] is True
    assert rows[0]['session']['login_verified'] is True
    assert rows[0]['session']['login_check_message'] == '账号已登录，可以正常使用。'


def test_group_atmosphere_account_list_reflects_authenticated_runtime(monkeypatch):
    client = make_client()
    service = client.app.state.service
    response = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_name': '印尼群活跃01',
        'region': '印尼',
        'groups': [{'target_group': 'https://chat.whatsapp.com/ABCDEFG', 'enabled': True}],
        'enabled': True,
    })
    account_key = response.json()['account_key']
    auth_path = str(service._whatsapp_approval_session_auth_path(account_key))
    client_id = service._whatsapp_approval_session_client_id(account_key)

    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: {
        'pid': 43210,
        'port': 59999,
        'base_url': 'http://127.0.0.1:59999',
        'auth_path': auth_path,
        'client_id': client_id,
        'started_at': datetime.now(timezone.utc).isoformat(),
    } if key == account_key else {})
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

    rows = client.get('/api/ops/group-atmosphere/accounts').json()['rows']

    assert rows[0]['runtime']['status'] == 'warm'
    assert rows[0]['runtime']['ready'] is True
    assert rows[0]['runtime']['authenticated'] is True
    assert rows[0]['session']['login_verified'] is True
    assert rows[0]['session']['login_check_message'] == '账号已登录，可以正常使用。'


def test_group_atmosphere_account_list_uses_authenticated_cached_worker_health_over_stale_pending_session(monkeypatch):
    client = make_client()
    service = client.app.state.service
    response = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-stale-cache',
        'account_name': '发言号缓存登录',
        'region': '印尼',
        'groups': [{'target_group': 'cache-group@g.us', 'enabled': True}],
        'enabled': True,
    })
    assert response.status_code == 200
    account_key = response.json()['account_key']
    auth_path = str(service._whatsapp_approval_session_auth_path(account_key))
    client_id = service._whatsapp_approval_session_client_id(account_key)
    meta = {
        'pid': 43212,
        'port': 59995,
        'base_url': 'http://127.0.0.1:59995',
        'auth_path': auth_path,
        'client_id': client_id,
        'started_at': datetime.now(timezone.utc).isoformat(),
        'last_session_checked_ts': time.time(),
        'last_session_state': {
            'account_key': account_key,
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
    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: meta if key == account_key else {})
    monkeypatch.setattr(service, '_write_whatsapp_approval_runtime_meta', lambda key, payload: payload)
    monkeypatch.setattr(service, '_pid_running', lambda pid: True)
    monkeypatch.setattr(service, '_group_atmosphere_allow_test_worker_urls', False)
    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', lambda base_url: (_ for _ in ()).throw(AssertionError('list must not call worker health')))

    rows = client.get('/api/ops/group-atmosphere/accounts').json()['rows']

    assert rows[0]['runtime']['active'] is True
    assert rows[0]['session']['login_verified'] is True
    assert rows[0]['session']['login_check_message'] == '账号已登录，可以正常使用。'


def test_group_atmosphere_account_list_shows_recoverable_instead_of_scan_when_runtime_stopped(monkeypatch):
    client = make_client()
    service = client.app.state.service
    response = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-stopped-cache',
        'account_name': '发言号进程已停',
        'region': '印尼',
        'groups': [{'target_group': 'cache-group@g.us', 'enabled': True}],
        'enabled': True,
    })
    assert response.status_code == 200
    account_key = response.json()['account_key']
    auth_path = str(service._whatsapp_approval_session_auth_path(account_key))
    client_id = service._whatsapp_approval_session_client_id(account_key)
    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: {
        'pid': 43213,
        'port': 59994,
        'base_url': 'http://127.0.0.1:59994',
        'auth_path': auth_path,
        'client_id': client_id,
        'started_at': datetime.now(timezone.utc).isoformat(),
        'last_session_checked_ts': time.time(),
        'last_session_state': {
            'account_key': account_key,
            'login_verified': True,
            'login_check_status': 'passed',
            'login_check_message': '账号已登录，可以正常使用。',
        },
    } if key == account_key else {})
    monkeypatch.setattr(service, '_pid_running', lambda pid: False)
    monkeypatch.setattr(service, '_whatsapp_approval_has_local_auth_session', lambda key: key == account_key)
    monkeypatch.setattr(service, '_group_atmosphere_allow_test_worker_urls', False)

    rows = client.get('/api/ops/group-atmosphere/accounts').json()['rows']
    session = rows[0]['session']

    assert rows[0]['runtime']['active'] is False
    assert session['login_verified'] is False
    assert session['login_state'] == 'recoverable'
    assert session['login_check_status'] == 'runtime_recoverable'
    assert session['qr_available'] is False
    assert session['can_show_qr'] is False
    assert '待扫码' not in session['login_check_message']
    assert session['login_check_message'] == '登录态可恢复，点击实时学习恢复。'


def test_group_atmosphere_account_list_persists_actual_group_name_after_login(monkeypatch):
    client = make_client()
    service = client.app.state.service
    response = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_name': '+852 4456 8277',
        'region': '香港',
        'groups': [
            {'target_group': 'https://chat.whatsapp.com/EoHAaKPML7p3BG7LNEbOl1', 'enabled': True},
        ],
        'enabled': True,
    })
    account_key = response.json()['account_key']
    auth_path = str(service._whatsapp_approval_session_auth_path(account_key))
    client_id = service._whatsapp_approval_session_client_id(account_key)

    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: {
        'pid': 12345,
        'port': 59998,
        'base_url': 'http://127.0.0.1:59998',
        'auth_path': auth_path,
        'client_id': client_id,
        'started_at': datetime.now(timezone.utc).isoformat(),
    } if key == account_key else {})
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

    class FakeProbeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {'group_name': 'Hong Kong Creator Group', 'group_id': '120363000000000000@g.us'}

    probe_calls = []

    def fake_post(url, json=None, timeout=None):
        probe_calls.append({'url': url, 'json': json, 'timeout': timeout})
        return FakeProbeResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)

    rows = client.get('/api/ops/group-atmosphere/accounts').json()['rows']

    assert probe_calls == []
    assert rows[0]['session']['login_verified'] is True
    assert rows[0]['groups'][0]['target_group'] == 'https://chat.whatsapp.com/EoHAaKPML7p3BG7LNEbOl1'


def test_group_atmosphere_session_start_persists_probed_group_name_to_account_and_bridge(monkeypatch, tmp_path):
    client = make_client({'DB_PATH': str(tmp_path / 'automation.db'), 'AUTH_INTERNAL_TOKEN': 'dev-internal-token'})
    service = client.app.state.service
    headers = {'x-ops-internal-token': 'dev-internal-token'}
    role = client.post('/api/ops/group-atmosphere/roles/manual-phrases', json={
        'role_key': 'auto-hk-community_seed',
        'role_name': '香港活跃BOT',
        'region': '香港',
        'language': 'zh',
        'role_positioning': 'community_seed',
        'phrases': ['大家可以多交流。'],
        'enabled': True,
    }, headers=headers)
    assert role.status_code == 200
    role_key = role.json()['role']['role_key']
    account = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-hk-01',
        'account_name': '+852 4456 8277',
        'region': '香港',
        'language': 'zh',
        'role_positioning': 'community_seed',
        'groups': [{'target_group': 'https://chat.whatsapp.com/probeInvite', 'enabled': True}],
        'enabled': True,
    }, headers=headers)
    assert account.status_code == 200
    binding = client.post('/api/ops/group-atmosphere/role-bindings', json={
        'role_key': role_key,
        'account_key': 'atmosphere-hk-01',
        'group_indexes': [0],
        'auto_speaking_enabled': True,
    }, headers=headers)
    assert binding.status_code == 200
    auth_path = str(service._whatsapp_approval_session_auth_path('atmosphere-hk-01'))
    client_id = service._whatsapp_approval_session_client_id('atmosphere-hk-01')
    meta = {
        'pid': 4456,
        'port': 59992,
        'base_url': 'http://127.0.0.1:59992',
        'auth_path': auth_path,
        'client_id': client_id,
        'started_at': datetime.now(timezone.utc).isoformat(),
    }
    monkeypatch.setattr(service, 'start_whatsapp_approval_account_runtime', lambda key, reset=False: {'runtime': meta})
    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: meta if key == 'atmosphere-hk-01' else {})
    monkeypatch.setattr(service, '_pid_running', lambda pid: True)
    probe_calls = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_post(url, json=None, timeout=None):
        if url.endswith('/warmup'):
            return FakeResponse({
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
        probe_calls.append({'url': url, 'json': json, 'timeout': timeout})
        return FakeResponse({'group_name': '香港创作者群', 'group_id': '12036344568277@g.us'})

    monkeypatch.setattr('app.main.requests.post', fake_post)

    started = client.post('/api/ops/group-atmosphere/accounts/atmosphere-hk-01/session/start', headers=headers)

    assert started.status_code == 200
    assert probe_calls == [{
        'url': 'http://127.0.0.1:59992/probe-group-state',
        'json': {'registration_group': 'https://chat.whatsapp.com/probeInvite'},
        'timeout': 8.0,
    }]
    listed = client.get('/api/ops/group-atmosphere/accounts', headers=headers).json()['rows'][0]
    assert listed['groups'][0]['group_name'] == '香港创作者群'
    assert listed['groups'][0]['group_id'] == '12036344568277@g.us'
    rels = client.get('/api/ops/group-atmosphere/role-bindings', headers=headers).json()
    bridge_group = rels['relationships'][0]['groups'][0]
    assert bridge_group['group_name'] == '香港创作者群'
    assert bridge_group['target_group'] == 'https://chat.whatsapp.com/probeInvite'


def test_group_atmosphere_account_list_does_not_auto_recover_stopped_dedicated_runtime(monkeypatch):
    client = make_client()
    service = client.app.state.service
    response = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_name': '印尼群活跃01',
        'region': '印尼',
        'groups': [{'target_group': 'https://chat.whatsapp.com/ABCDEFG', 'enabled': True}],
        'enabled': True,
    })
    account_key = response.json()['account_key']
    auth_path = str(service._whatsapp_approval_session_auth_path(account_key))
    client_id = service._whatsapp_approval_session_client_id(account_key)
    starts = []
    probes = []

    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: {
        'pid': 43210,
        'port': 59999,
        'base_url': 'http://127.0.0.1:59999',
        'auth_path': auth_path,
        'client_id': client_id,
        'started_at': datetime.now(timezone.utc).isoformat(),
    } if key == account_key else {})
    monkeypatch.setattr(service, '_pid_running', lambda pid: False)

    def fake_start(key, *, reset=False):
        starts.append({'key': key, 'reset': reset})
        return {
            'started': True,
            'runtime': {'account_key': key, 'status': 'warm', 'active': True, 'ready': True, 'authenticated': True, 'base_url': 'http://127.0.0.1:59999'},
            'session': {'account_key': key, 'login_verified': True, 'login_check_status': 'authenticated', 'login_check_message': '账号已登录，可以正常使用。'},
        }

    monkeypatch.setattr(service, 'start_group_atmosphere_whatsapp_account_session', fake_start)
    monkeypatch.setattr(service, '_probe_group_atmosphere_actual_group_names', lambda **kwargs: probes.append(kwargs) or [])

    rows = client.get('/api/ops/group-atmosphere/accounts').json()['rows']

    assert starts == []
    assert probes == []
    assert rows[0]['runtime']['status'] in {'recovering', 'stopped', 'warm'}
    assert rows[0]['runtime']['authenticated'] is False
    assert rows[0]['session']['login_verified'] is False
    assert rows[0]['session']['login_check_status'] in {'runtime_recovering', 'runtime_recoverable', 'not_logged_in'}


def test_group_atmosphere_chat_record_file_upload_updates_language_profile():
    client = make_client()
    account = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_name': '印尼群聊天01',
        'region': '印尼',
        'role_positioning': 'community_seed',
        'randomness_level': 'medium',
        'groups': [{'target_group': '120363400336474261@g.us', 'group_name': 'ID Group', 'enabled': True}],
    }).json()
    key = account['account_key']
    response = client.post(f'/api/ops/group-atmosphere/accounts/{key}/chat-records/upload', json={
        'filename': 'whatsapp-chat.txt',
        'content': '[12/05/26, 09.12.33] Admin: Halo kak, kirim ID ke admin ya.\n[12/05/26, 09.14.02] User: kak kode dimana?'
    })
    assert response.status_code == 200
    body = response.json()
    assert body['ok'] is True
    assert body['imported_count'] == 2
    assert body['language_profile']['language'] == 'id'
    assert body['language_profile']['tone_markers']['uses_kak'] is True


def test_group_atmosphere_manual_group_send_api_uses_dedicated_runtime(monkeypatch):
    client = make_client()
    service = client.app.state.service
    account = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_name': '印尼群活跃01',
        'region': '印尼',
        'groups': [{'target_group': '120363400336474261@g.us', 'group_name': 'ID Group', 'enabled': True}],
        'enabled': True,
    }).json()
    account_key = account['account_key']
    auth_path = str(service._whatsapp_approval_session_auth_path(account_key))
    client_id = service._whatsapp_approval_session_client_id(account_key)
    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: {
        'pid': 43210,
        'port': 59999,
        'base_url': 'http://127.0.0.1:59999',
        'auth_path': auth_path,
        'client_id': client_id,
    } if key == account_key else {})
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
    sent = []

    class FakeSendResponse:
        status_code = 200
        text = '{"status":"sent"}'
        def json(self):
            return {'status': 'sent', 'message_id': 'msg-1', 'result_reason': 'ok'}

    def fake_post(url, json=None, timeout=None):
        sent.append({'url': url, 'json': json, 'timeout': timeout})
        return FakeSendResponse()

    monkeypatch.setattr('app.main.requests.post', fake_post)

    media = service.create_group_atmosphere_media_asset('manual-send.jpg', b'fake-image-bytes', 'image/jpeg', created_by='test')
    response = client.post(f'/api/ops/group-atmosphere/accounts/{account_key}/groups/0/send', json={
        'message_text': 'Halo kak, test manual',
        'media_id': media['media']['media_id'],
    })

    assert response.status_code == 200
    body = response.json()
    assert body['sent'] is True
    assert body['group_name'] == 'ID Group'
    assert sent[0]['url'] == 'http://127.0.0.1:59999/send-group-message'
    assert sent[0]['json']['target_group'] == '120363400336474261@g.us'
    assert sent[0]['json']['message_text'] == 'Halo kak, test manual'
    assert sent[0]['json']['media_id'] == media['media']['media_id']
    assert sent[0]['json']['media_filename'] == 'manual-send.jpg'
    assert sent[0]['json']['media_mime_type'] == 'image/jpeg'
    assert body['media_id'] == media['media']['media_id']
    logs = client.get('/api/ops/group-atmosphere/logs').json()['rows']
    assert logs[0]['status'] == 'success'
    assert logs[0]['message_text'] == 'Halo kak, test manual'


def test_group_atmosphere_qr_session_state_survives_missing_qrcode_renderer(monkeypatch):
    client = make_client()
    service = client.app.state.service

    def fail_render(qr_text):
        raise RuntimeError("Cannot find module 'qrcode'")

    monkeypatch.setattr(service, '_render_whatsapp_approval_qr_image_data_url', fail_render)
    session = service._build_whatsapp_approval_session_state(
        'atmosphere-indo-01',
        worker_health={
            'status': 'awaiting_qr',
            'authenticated': False,
            'ready': False,
            'client_id': service._whatsapp_approval_session_client_id('atmosphere-indo-01'),
            'auth_path': str(service._whatsapp_approval_session_auth_path('atmosphere-indo-01')),
            'last_qr': 'QR_TEXT_SAMPLE',
        },
        include_qr_ascii=True,
    )

    assert session['qr_available'] is True
    assert session['login_check_status'] == 'waiting_for_scan'
    assert session['qr_image_data_url'] in {None, ''}
    assert "Cannot find module 'qrcode'" in session['qr_render_error']


def test_group_atmosphere_stale_qr_is_detected_for_regeneration():
    client = make_client()
    service = client.app.state.service
    stale_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    fresh_at = datetime.now(timezone.utc).isoformat()

    assert service._whatsapp_approval_session_has_stale_qr({
        'qr_available': True,
        'authenticated': False,
        'login_verified': False,
        'last_qr_at': stale_at,
    }) is True
    assert service._whatsapp_approval_session_has_stale_qr({
        'qr_available': True,
        'authenticated': False,
        'login_verified': False,
        'last_qr_at': fresh_at,
    }) is False
    assert service._whatsapp_approval_session_has_stale_qr({
        'qr_available': True,
        'authenticated': True,
        'login_verified': True,
        'last_qr_at': stale_at,
    }) is False


def test_group_atmosphere_session_start_auto_recovers_unstable_runtime(monkeypatch):
    client = make_client()
    service = client.app.state.service
    account = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_name': '印尼群活跃01',
        'region': '印尼',
        'groups': [{'target_group': '120363400336474261@g.us', 'group_name': 'ID Group', 'enabled': True}],
        'enabled': True,
    }).json()
    account_key = account['account_key']
    auth_path = str(service._whatsapp_approval_session_auth_path(account_key))
    client_id = service._whatsapp_approval_session_client_id(account_key)
    starts = []
    stops = []
    health_calls = []

    def fake_start_runtime(key, *, reset=False):
        starts.append({'key': key, 'reset': reset})
        port = 61000 + len(starts)
        return {'runtime': {
            'account_key': key,
            'source': 'dedicated',
            'active': True,
            'status': 'running',
            'base_url': f'http://127.0.0.1:{port}',
            'auth_path': auth_path,
            'client_id': client_id,
        }}

    def fake_stop_runtime(key):
        stops.append(key)
        return {'stopped': True}

    def fake_warmup(*args, **kwargs):
        raise RuntimeError('warmup connection reset')

    def fake_health(base_url):
        health_calls.append(base_url)
        if len(health_calls) == 1:
            raise RuntimeError('connection refused')
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

    monkeypatch.setattr(service, 'start_whatsapp_approval_account_runtime', fake_start_runtime)
    monkeypatch.setattr(service, 'stop_whatsapp_approval_account_runtime', fake_stop_runtime)
    monkeypatch.setattr('app.main.requests.post', fake_warmup)
    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', fake_health)

    result = service.start_group_atmosphere_whatsapp_account_session(account_key)

    assert result['started'] is True
    assert result['auto_recover_attempts'] == 2
    assert result['session']['login_verified'] is True
    assert result['runtime']['status'] == 'warm'
    assert len(starts) == 2
    assert stops == [account_key]
    assert len(health_calls) == 2


def test_group_atmosphere_session_start_returns_json_after_auto_recover_exhausted(monkeypatch):
    client = make_client()
    service = client.app.state.service
    account = client.post('/api/ops/group-atmosphere/accounts', json={
        'account_name': '印尼群活跃01',
        'region': '印尼',
        'groups': [{'target_group': '120363400336474261@g.us', 'group_name': 'ID Group', 'enabled': True}],
        'enabled': True,
    }).json()
    account_key = account['account_key']
    starts = []
    stops = []

    def fake_start_runtime(key, *, reset=False):
        starts.append({'key': key, 'reset': reset})
        return {'runtime': {'account_key': key, 'source': 'dedicated', 'active': True, 'status': 'running', 'base_url': 'http://127.0.0.1:61001'}}

    monkeypatch.setattr(service, 'start_whatsapp_approval_account_runtime', fake_start_runtime)
    monkeypatch.setattr(service, 'stop_whatsapp_approval_account_runtime', lambda key: stops.append(key) or {'stopped': True})
    monkeypatch.setattr('app.main.requests.post', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('warmup failed')))
    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', lambda base_url: (_ for _ in ()).throw(RuntimeError('health failed')))

    result = service.start_group_atmosphere_whatsapp_account_session(account_key)

    assert result['started'] is False
    assert result['auto_recover_attempts'] == 3
    assert result['runtime']['status'] == 'unavailable'
    assert result['session']['login_check_status'] == 'runtime_unstable'
    assert '多次自愈仍未稳定' in result['session']['login_check_message']
    assert len(starts) == 3
    assert len(stops) == 3


def test_group_atmosphere_qr_session_endpoint_returns_qr_payload(monkeypatch):
    def fake_start(self, account_key, *, reset=False):
        return {
            'started': True,
            'reset': reset,
            'runtime': {'account_key': account_key, 'status': 'running', 'base_url': 'http://127.0.0.1:60001'},
            'session': {
                'account_key': account_key,
                'login_verified': False,
                'login_check_status': 'waiting_for_scan',
                'login_check_message': '已生成二维码，等待扫码完成登录。',
                'qr_available': True,
                'qr_image_data_url': 'data:image/png;base64,TESTQR',
            },
        }

    monkeypatch.setattr('app.main.Service.start_group_atmosphere_whatsapp_account_session', fake_start)
    client = make_client()
    response = client.post('/api/ops/group-atmosphere/accounts/atmosphere-indo-01/session/start')
    assert response.status_code == 200
    body = response.json()
    assert body['started'] is True
    assert body['session']['qr_image_data_url'].startswith('data:image/png;base64,')
    assert body['session']['login_check_status'] == 'waiting_for_scan'


def test_whatsapp_approval_runtime_supervisor_env_selects_systemd_or_popen(monkeypatch):
    client = make_client()
    service = client.app.state.service

    monkeypatch.setenv('WHATSAPP_APPROVAL_RUNTIME_SUPERVISOR', 'popen')
    assert service._should_use_systemd_whatsapp_runtime() is False

    monkeypatch.setenv('WHATSAPP_APPROVAL_RUNTIME_SUPERVISOR', 'systemd')
    assert service._should_use_systemd_whatsapp_runtime() is True


def test_whatsapp_approval_runtime_state_uses_systemd_main_pid(monkeypatch):
    client = make_client()
    service = client.app.state.service
    account_key = 'registration-639974974871'
    meta = {
        'account_key': account_key,
        'pid': 111,
        'port': 59987,
        'base_url': 'http://127.0.0.1:59987',
        'auth_path': str(service._whatsapp_approval_session_auth_path(account_key)),
        'client_id': service._whatsapp_approval_session_client_id(account_key),
        'systemd_unit': 'mcn-wa-runtime-registration-639974974871.service',
        'supervisor': 'systemd',
    }
    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: meta if key == account_key else {})
    monkeypatch.setattr(service, '_systemd_whatsapp_runtime_main_pid', lambda unit: 222 if unit == meta['systemd_unit'] else None)
    monkeypatch.setattr(service, '_pid_running', lambda pid: False)
    monkeypatch.setattr(service, '_request_whatsapp_approval_worker_health', lambda base_url: (_ for _ in ()).throw(AssertionError('lightweight runtime state must not call worker health')))

    state = service._build_whatsapp_approval_runtime_state(account_key, skip_health_check=True, allow_shared_fallback=False)

    assert state['active'] is True
    assert state['pid'] == 222
    assert state['source'] == 'dedicated'


def test_stop_whatsapp_approval_systemd_runtime_stops_unit_before_killing_pids(monkeypatch):
    client = make_client()
    service = client.app.state.service
    account_key = 'registration-639974974871'
    unit = 'mcn-wa-runtime-registration-639974974871.service'
    meta = {
        'account_key': account_key,
        'pid': 111,
        'auth_path': str(service._whatsapp_approval_session_auth_path(account_key)),
        'systemd_unit': unit,
        'supervisor': 'systemd',
    }
    calls = []
    written = []

    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: dict(meta) if key == account_key else {})
    monkeypatch.setattr(service, '_write_whatsapp_approval_runtime_meta', lambda key, payload: written.append(payload) or payload)
    monkeypatch.setattr(service, '_list_whatsapp_approval_runtime_processes', lambda auth_path: [222])
    monkeypatch.setattr(service, '_terminate_whatsapp_approval_runtime_processes', lambda pids: calls.append(('terminate', list(pids))))
    monkeypatch.setattr(service, '_systemd_whatsapp_runtime_main_pid', lambda systemd_unit: None)

    def fake_run(cmd, **kwargs):
        calls.append(tuple(cmd))
        class Result:
            returncode = 0
            stdout = ''
            stderr = ''
        return Result()

    monkeypatch.setattr('app.main.subprocess.run', fake_run)

    result = service.stop_whatsapp_approval_account_runtime(account_key)

    assert ('systemctl', 'stop', unit) in calls
    assert ('terminate', [111, 222]) in calls
    assert written and written[-1]['stopped_at']
    assert result['stopped'] is True


def test_whatsapp_runtime_identity_is_isolated_for_all_account_types():
    client = make_client()
    service = client.app.state.service

    keys = [
        'registration-639974974871',
        'learn-indo-01',
        'group-atmosphere-indo-01',
    ]
    identities = [service._whatsapp_approval_runtime_identity(key) for key in keys]

    assert [item['account_key'] for item in identities] == keys
    assert len({item['slug'] for item in identities}) == len(keys)
    assert len({item['systemd_unit'] for item in identities}) == len(keys)
    assert len({str(item['auth_path']) for item in identities}) == len(keys)
    assert len({str(item['state_path']) for item in identities}) == len(keys)
    assert len({item['port'] for item in identities}) == len(keys)
    for key, identity in zip(keys, identities):
        slug = service._whatsapp_approval_session_account_key(key)
        assert identity['slug'] == slug
        assert identity['client_id'] == f'wa-approval-{slug}'
        assert identity['systemd_unit'] == f'mcn-wa-runtime-{slug}.service'
        assert str(identity['auth_path']).endswith(f'.wwebjs_auth_accounts/{slug}')
        assert str(identity['state_path']).endswith(f'data/whatsapp_approval_worker_runtimes/{slug}.json')
        assert str(identity['log_path']).endswith(f'logs/whatsapp_approval_workers/{slug}.log')
        assert identity['base_url'] == f"http://127.0.0.1:{identity['port']}"


def test_whatsapp_runtime_identity_reuses_existing_persisted_port(monkeypatch):
    client = make_client()
    service = client.app.state.service
    account_key = 'learn-indo-01'
    persisted = {
        'account_key': account_key,
        'port': 61234,
        'base_url': 'http://127.0.0.1:61234',
    }

    monkeypatch.setattr(service, '_read_whatsapp_approval_runtime_meta', lambda key: dict(persisted) if key == account_key else {})

    identity = service._whatsapp_approval_runtime_identity(account_key)

    assert identity['port'] == 61234
    assert identity['base_url'] == 'http://127.0.0.1:61234'
