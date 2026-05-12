from fastapi.testclient import TestClient

from app.main import create_app


def make_client(settings=None):
    cfg = {"DB_PATH": ":memory:", "AUTO_LARK_REPLY": False, "AUTH_ENABLED": True}
    if settings:
        cfg.update(settings)
    return TestClient(create_app(cfg))


def test_login_page_redirects_operator_away_from_account_management_only():
    client = make_client()

    html = client.get('/login?next=/ops/accounts').text

    assert 'safeNextUrlForRole' in html
    assert "'/ops/accounts'" in html or '"/ops/accounts"' in html
    assert "'/ops/production-ops'" not in html
    assert "'/ops/group-atmosphere'" not in html
    assert "return '/ops';" in html
    assert 'safeNextUrlForRole(data.user && data.user.role)' in html


def test_operator_login_api_succeeds_and_only_account_management_stays_admin_only():
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
    assert client.get('/ops/group-atmosphere').status_code == 200
    accounts_page = client.get('/ops/accounts', follow_redirects=False)
    assert accounts_page.status_code == 303
    assert accounts_page.headers['location'] == '/ops'
    accounts_api = client.get('/api/ops/accounts')
    assert accounts_api.status_code == 403
    assert accounts_api.json()['detail'] == 'ops_admin_required'
