from fastapi.testclient import TestClient

from app.main import create_app


def make_client(settings=None):
    cfg = {"DB_PATH": ":memory:", "AUTO_LARK_REPLY": False, "AUTH_ENABLED": True}
    if settings:
        cfg.update(settings)
    return TestClient(create_app(cfg))


def test_login_page_preserves_operator_account_settings_next_target():
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
    assert "'/ops/group-atmosphere'" not in html
    assert 'safeNextUrlForRole(data.user && data.user.role)' in html
    assert '初始化管理员' not in html
    assert 'bootstrapTab' not in html


def test_operator_login_api_succeeds_and_account_settings_is_self_service_only():
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
    assert client.get('/ops').status_code == 200
    assert client.get('/ops/production-ops').status_code == 200
    ops_html = client.get('/ops').text
    assert '/ops/intake-bot-presets' in ops_html
    assert '/ops/production-ops' in ops_html
    assert '/ops/registration-group-approval-batch-members' in ops_html
    assert '/ops/accounts' in ops_html
    assert '账号设置' in ops_html
    assert '/ops/group-atmosphere' not in ops_html
    accounts_page = client.get('/ops/accounts')
    assert accounts_page.status_code == 200
    html = accounts_page.text
    assert '账号设置' in html
    assert '修改我的密码' in html
    assert '/api/ops/auth/password' in html
    assert '显示密码' in html
    assert 'togglePasswordVisibility' in html
    assert 'showToast' in html
    assert '密码修改成功' in html
    assert '新增账号' not in html
    assert '账号列表' not in html
    assert '管理员重置密码' not in html
    assert '/api/ops/accounts' not in html
    accounts_api = client.get('/api/ops/accounts')
    assert accounts_api.status_code == 403
    assert accounts_api.json()['detail'] == 'ops_admin_required'
