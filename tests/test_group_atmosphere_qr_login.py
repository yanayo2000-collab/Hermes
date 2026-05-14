from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import create_app


def make_client(settings=None):
    cfg = {"DB_PATH": ":memory:", "AUTO_LARK_REPLY": False}
    if settings:
        cfg.update(settings)
    return TestClient(create_app(cfg))


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
    assert '上传 WhatsApp 聊天记录' in html
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
    assert 'groupReadiness' in html
    assert '群名称' in html
    assert '入群链接' in html
    assert '状态' in html
    assert '可投产' in html
    assert '待登录' in html
    assert '未开启' in html
    assert '生成二维码' in html
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
    assert rows[0]['responsible_type'] == 'group_atmosphere'
    assert health_calls['count'] == 0

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
