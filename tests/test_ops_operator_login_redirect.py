from app.main import create_app


def make_client(config=None):
    from fastapi.testclient import TestClient
    cfg = {'AUTH_ENABLED': True, 'DB_PATH': ':memory:', 'AUTO_LARK_REPLY': False}
    if config:
        cfg.update(config)
    app = create_app(cfg)
    return TestClient(app)


def _bootstrap_admin_and_create_user(client, *, username, role, display_name):
    admin = client.post('/api/ops/auth/bootstrap', json={
        'username': 'admin01',
        'password': 'secret123',
        'display_name': 'Admin',
    })
    assert admin.status_code == 200
    created = client.post('/api/ops/accounts', json={
        'username': username,
        'password': 'secret123',
        'role': role,
        'display_name': display_name,
    })
    assert created.status_code == 200
    client.post('/api/ops/auth/logout')


def _assert_limited_self_password_page(html):
    assert '账号设置' in html
    assert '修改我的密码' in html
    assert '退出登录' in html
    assert 'onclick="logoutCurrentAccount()"' in html
    assert "async function logoutCurrentAccount()" in html
    assert "fetch('/api/ops/auth/logout'" in html
    assert "window.location.replace('/login')" in html
    assert '账号列表' not in html
    assert '创建账号' not in html
    assert '角色' not in html
    assert '/api/ops/auth/password' in html

def _assert_admin_account_settings_logout_action(html):
    assert '<div class="accounts-hero-actions"><button class="secondary" type="button" onclick="openChangeOwnPassword()">修改我的密码</button><button class="ghost" type="button" onclick="logoutCurrentAccount()">退出登录</button></div>' in html
    assert "async function logoutCurrentAccount()" in html
    assert "fetchJson('/api/ops/auth/logout', {method:'POST'})" in html
    assert "window.location.replace('/login')" in html


def test_admin_account_settings_shows_logout_next_to_change_password():
    client = make_client()
    admin = client.post('/api/ops/auth/bootstrap', json={
        'username': 'admin01',
        'password': 'secret123',
        'display_name': 'Admin',
    })
    assert admin.status_code == 200

    accounts_page = client.get('/ops/accounts')

    assert accounts_page.status_code == 200
    _assert_admin_account_settings_logout_action(accounts_page.text)


def test_login_page_allows_operator_next_to_accounts_or_group_atmosphere():
    client = make_client()
    admin = client.post('/api/ops/auth/bootstrap', json={
        'username': 'admin01',
        'password': 'secret123',
        'display_name': 'Admin',
    })
    assert admin.status_code == 200
    client.post('/api/ops/auth/logout')

    html = client.get('/login?next=/ops/accounts').text

    assert 'safeNextUrlForRole' in html
    assert 'const adminOnlyNextTargets = [];' in html
    assert "'/ops/production-ops'" not in html
    assert "normalizedRole === 'operator'" in html
    assert "target === '/ops/accounts'" in html
    assert "'/ops/group-atmosphere'" in html
    assert 'safeNextUrlForRole(data.user && data.user.role)' in html
    assert '初始化管理员' not in html
    assert 'bootstrapTab' not in html


def test_operator_role_can_use_group_atmosphere_and_limited_account_settings():
    client = make_client()
    _bootstrap_admin_and_create_user(
        client,
        username='operator01',
        role='operator',
        display_name='Operator',
    )

    login = client.post('/api/ops/auth/login', json={
        'username': 'operator01',
        'password': 'secret123',
    })

    assert login.status_code == 200
    assert login.json()['user']['role'] == 'operator'

    ops_home = client.get('/ops', follow_redirects=False)
    assert ops_home.status_code == 303
    assert ops_home.headers['location'] == '/ops/group-atmosphere'

    group_page = client.get('/ops/group-atmosphere')
    assert group_page.status_code == 200
    html = group_page.text
    assert '群聊天助手' in html
    assert '/api/ops/group-atmosphere/accounts' in html
    assert '/ops/intake-bot-presets' not in html
    assert '/ops/production-ops' not in html
    assert '/ops/registration-group-approval-batch-members' not in html
    assert '/ops/accounts' in html
    assert '账号设置' in html

    assert client.get('/api/ops/group-atmosphere/accounts').status_code == 200
    runtime = client.get('/api/ops/runtime-health')
    assert runtime.status_code == 403
    assert runtime.json()['detail'] == 'ops_customer_service_required'
    approval_accounts = client.get('/api/ops/whatsapp-approval-accounts')
    assert approval_accounts.status_code == 403
    assert approval_accounts.json()['detail'] == 'ops_customer_service_required'
    assert client.get('/ops/production-ops', follow_redirects=False).status_code == 303
    assert client.get('/ops/intake-bot-presets', follow_redirects=False).status_code == 303

    accounts_page = client.get('/ops/accounts', follow_redirects=False)
    assert accounts_page.status_code == 200
    _assert_limited_self_password_page(accounts_page.text)


def test_customer_service_role_can_open_limited_account_settings():
    client = make_client()
    _bootstrap_admin_and_create_user(
        client,
        username='kefu01',
        role='customer_service',
        display_name='客服',
    )

    login = client.post('/api/ops/auth/login', json={
        'username': 'kefu01',
        'password': 'secret123',
    })

    assert login.status_code == 200
    assert login.json()['user']['role'] == 'customer_service'

    ops_home = client.get('/ops', follow_redirects=False)
    assert ops_home.status_code == 303
    assert ops_home.headers['location'] == '/ops/intake-submit'

    accounts_page = client.get('/ops/accounts', follow_redirects=False)
    assert accounts_page.status_code == 200
    _assert_limited_self_password_page(accounts_page.text)


def test_customer_service_can_manage_intake_presets_but_only_read_guild_executors():
    client = make_client()
    _bootstrap_admin_and_create_user(
        client,
        username='kefu_presets',
        role='customer_service',
        display_name='客服收口',
    )

    login = client.post('/api/ops/auth/login', json={
        'username': 'kefu_presets',
        'password': 'secret123',
    })
    assert login.status_code == 200

    page = client.get('/ops/intake-bot-presets', follow_redirects=False)
    assert page.status_code == 200
    html = page.text
    assert '收口配置中心' in html
    assert '＋ 新增机器人配置' in html
    assert '保存配置' in html
    assert '<button type="button" class="admin-only" onclick="openExecutorModal(null)">＋ 新增公会执行器</button>' in html
    assert 'body[data-ops-role="operator"] .admin-only, body[data-ops-role="customer_service"] .admin-only { display: none !important; }' in html

    assert client.get('/api/ops/intake-bot-presets').status_code == 200
    save_preset = client.post('/api/ops/intake-bot-presets/intake-cs-test', json={
        'robot_name': '客服测试机器人',
        'app_id': 'cli_test_customer_service',
        'app_secret': 'secret-for-test',
        'default_app': 'Tugao',
        'default_guild': 'Carote',
    })
    assert save_preset.status_code != 403
    assert save_preset.json()['detail'] != 'ops_admin_required'

    assert client.get('/api/ops/guild-executors').status_code == 200
    assert client.get('/api/ops/guild-executors/health').status_code == 200
    write_executor = client.post('/api/ops/guild-executors/Carote', json={'enabled': True})
    assert write_executor.status_code == 403
    assert write_executor.json()['detail'] == 'ops_admin_required'
    delete_executor = client.delete('/api/ops/guild-executors/Carote')
    assert delete_executor.status_code == 403
    assert delete_executor.json()['detail'] == 'ops_admin_required'
