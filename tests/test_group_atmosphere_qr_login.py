from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import create_app


def make_client(settings=None):
    cfg = {"DB_PATH": ":memory:", "AUTO_LARK_REPLY": False}
    if settings:
        cfg.update(settings)
    return TestClient(create_app(cfg))


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
    assert "const uploadCurrent=uploadSelect?uploadSelect.value:''" in html
    assert "const simCurrent=simSelect?simSelect.value:''" in html
    assert "const candidateCurrent=candidateSelect?candidateSelect.value:''" not in html
    assert "const key=document.getElementById('ga_sim_account_select')?.value.trim()||''" in html


def test_group_atmosphere_candidate_pool_exposes_own_account_selector_and_action_feedback():
    client = make_client()
    response = client.get('/ops/group-atmosphere')
    assert response.status_code == 200
    html = response.text

    assert 'ga_candidate_language_filter' in html
    assert 'ga_candidate_role_filter' in html
    assert 'ga_candidate_account_select' not in html
    assert 'ga_candidate_group_select' not in html
    assert '选择语言/地区' in html
    assert '选择角色' in html
    assert '选择投放账号' not in html
    assert '选择发言群</option>' not in html
    assert '请选择话术方案' in html
    assert 'ga_group_1_plan' in html
    assert 'speech_plan_config_name' in html
    assert '话术方案库' in html
    assert '逐群装载' in html
    assert '已装载方案' in html
    assert '话术包' in html
    assert '保存名称' in html
    assert '删除话术包' in html
    assert '使用位置：' in html
    assert 'renameSpeechPlan' in html
    assert 'deleteSpeechPlan' in html
    assert 'deliver-' not in html
    assert 'openGroupPlanLoader' in html
    assert 'loadPlanIntoGroup' in html
    assert 'groupPlanLabel' in html
    assert 'setGroupPlanSelectValue' in html
    assert '当前方案：' in html
    assert 'ga_candidate_result' in html
    assert 'filterCandidateRows' in html
    assert 'speechPlanRows' in html
    assert "data-ga-enable-candidate" in html
    assert "candidateButton.dataset.configName" in html
    assert "正在加入话术方案" in html
    assert "请到 WhatsApp 账号配置里选择这个话术方案" in html
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
    assert 'ga_randomness_level' in html
    assert 'ga_group_rows' in html
    assert 'ga_add_group_btn' in html
    assert '+ 增加发言群' in html
    assert 'ga_account_enabled' in html
    assert 'ga_action_feedback' in html
    assert 'ga_chat_file' in html
    assert 'multiple' in html
    assert 'ga_clear_chat_files_btn' in html
    assert '清空文件' in html
    assert 'ga_tool_account_select' in html
    assert 'ga_sim_account_select' in html
    assert '请选择账号' in html
    assert '话术学习' in html
    assert '上传并学习' in html
    assert '自动分配到话术库' in html
    assert '/api/ops/group-atmosphere/chat-records/auto-learn' in html
    assert '候选话术池' in html
    assert '自动发言' in html
    assert '全部已启用账号' in html
    assert '立即检查可发送话术' in html
    assert '后台会按每日上限与间隔自动随机发送' in html
    assert 'ga_max_interval_minutes' in html
    assert 'stopAtmosphereSchedulerLoop' not in html
    assert 'toggleAtmosphereSchedulerLoop' not in html
    assert 'ga_scheduler_running' not in html
    assert '/api/ops/group-atmosphere/candidate-pool' in html
    assert '/api/ops/group-atmosphere/scheduler/run-due' in html
    assert '请先在聊天记录区域选择账号' not in html
    assert '请先在发送前预览区域选择账号' in html
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
    assert '账号用途' in html
    assert '运行状态' in html
    assert '登录状态' in html
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
    assert '群名称' in html
    assert '入群链接' in html
    assert '自动发言已开启' in html
    assert '开启自动发言' in html
    assert '待登录后生效' in html
    assert '允许发言' not in html
    assert '生成二维码' in html
    assert '手动发言' in html
    assert 'gaSendModal' in html
    assert '确认发送' in html
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
        'groups': [
            {'target_group': 'g1@g.us'}, {'target_group': 'g2@g.us'},
            {'target_group': 'g3@g.us'}, {'target_group': 'g4@g.us'},
        ],
    })
    assert too_many.status_code == 400
    assert 'at most 3 groups' in too_many.json()['detail']

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


def test_group_atmosphere_account_list_auto_recovers_stopped_dedicated_runtime(monkeypatch):
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

    rows = client.get('/api/ops/group-atmosphere/accounts').json()['rows']

    assert starts == [{'key': account_key, 'reset': False}]
    assert rows[0]['runtime']['status'] == 'warm'
    assert rows[0]['runtime']['authenticated'] is True
    assert rows[0]['session']['login_verified'] is True
    assert rows[0]['session']['login_check_status'] == 'authenticated'


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

    response = client.post(f'/api/ops/group-atmosphere/accounts/{account_key}/groups/0/send', json={
        'message_text': 'Halo kak, test manual',
    })

    assert response.status_code == 200
    body = response.json()
    assert body['sent'] is True
    assert body['group_name'] == 'ID Group'
    assert sent[0]['url'] == 'http://127.0.0.1:59999/send-group-message'
    assert sent[0]['json']['target_group'] == '120363400336474261@g.us'
    assert sent[0]['json']['message_text'] == 'Halo kak, test manual'
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
