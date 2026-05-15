from app.main import create_app


def make_client(config=None):
    from fastapi.testclient import TestClient
    cfg = {'AUTH_ENABLED': True, 'DB_PATH': ':memory:', 'AUTO_LARK_REPLY': False}
    if config:
        cfg.update(config)
    app = create_app(cfg)
    return TestClient(app)


def test_login_page_redirects_operator_to_group_atmosphere():
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
    assert "'/ops/accounts'" not in html
    assert "'/ops/production-ops'" not in html
    assert "normalizedRole === 'operator'" in html
    assert "'/ops/group-atmosphere'" in html
    assert 'safeNextUrlForRole(data.user && data.user.role)' in html
    assert '初始化管理员' not in html
    assert 'bootstrapTab' not in html


def test_operator_role_is_limited_to_group_atmosphere_only():
    client = make_client()
    admin = client.post('/api/ops/auth/bootstrap', json={
        'username': 'admin01',
        'password': 'secret123',
        'display_name': 'Admin',
    })
    assert admin.status_code == 200
    created = client.post('/api/ops/accounts', json={
        'username': 'operator01',
        'password': 'secret123',
        'role': 'operator',
        'display_name': 'Operator',
    })
    assert created.status_code == 200
    client.post('/api/ops/auth/logout')

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
    assert '/ops/accounts' not in html

    assert client.get('/api/ops/group-atmosphere/accounts').status_code == 200
    runtime = client.get('/api/ops/runtime-health')
    assert runtime.status_code == 403
    assert runtime.json()['detail'] == 'ops_customer_service_required'
    approval_accounts = client.get('/api/ops/whatsapp-approval-accounts')
    assert approval_accounts.status_code == 403
    assert approval_accounts.json()['detail'] == 'ops_customer_service_required'
    assert client.get('/ops/production-ops', follow_redirects=False).status_code == 303
    assert client.get('/ops/intake-bot-presets', follow_redirects=False).status_code == 303
    assert client.get('/ops/accounts', follow_redirects=False).status_code == 303
