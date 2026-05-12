from fastapi.testclient import TestClient

from app.main import create_app


def make_client(settings=None):
    cfg = {"DB_PATH": ":memory:", "AUTO_LARK_REPLY": False, "AUTH_ENABLED": True}
    if settings:
        cfg.update(settings)
    return TestClient(create_app(cfg))


def test_login_page_does_not_redirect_operator_to_admin_only_next_target():
    client = make_client()

    html = client.get('/login?next=/ops/accounts').text

    assert 'safeNextUrlForRole' in html
    assert "'/ops/accounts'" in html or '"/ops/accounts"' in html
    assert "return '/ops';" in html
    assert 'safeNextUrlForRole(data.user && data.user.role)' in html


def test_operator_login_api_succeeds_but_accounts_page_is_admin_only():
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
    assert client.get('/ops/accounts').status_code == 403
