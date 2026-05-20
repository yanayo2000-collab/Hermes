from fastapi.testclient import TestClient

from app.main import create_app


def make_auth_client(tmp_path, *, role='admin', internal_token='dev-internal-token'):
    app = create_app({
        'DB_PATH': str(tmp_path / 'automation.db'),
        'AUTH_ENABLED': True,
        'AUTH_INTERNAL_TOKEN': internal_token,
        'AUTO_LARK_REPLY': False,
        'GROUP_ATMOSPHERE_SCHEDULER_ENABLED': False,
    })
    password = 'StrongPass123!'
    app.state.auth_manager.create_user(
        username=f'{role}user',
        password=password,
        role=role,
        display_name=f'{role} user',
    )
    client = TestClient(app, client=('127.0.0.1', 49152))
    login = client.post('/api/ops/auth/login', json={'username': f'{role}user', 'password': password})
    assert login.status_code == 200
    return client, internal_token


def test_group_atmosphere_loopback_requires_session_or_internal_token(tmp_path):
    app = create_app({
        'DB_PATH': str(tmp_path / 'automation.db'),
        'AUTH_ENABLED': True,
        'AUTH_INTERNAL_TOKEN': 'dev-internal-token',
        'AUTO_LARK_REPLY': False,
        'GROUP_ATMOSPHERE_SCHEDULER_ENABLED': False,
    })
    client = TestClient(app, client=('127.0.0.1', 49152))

    unauthenticated = client.get('/api/ops/group-atmosphere/roles')
    assert unauthenticated.status_code == 401

    internal = client.get(
        '/api/ops/group-atmosphere/roles',
        headers={'x-ops-internal-token': 'dev-internal-token'},
    )
    assert internal.status_code == 200


def test_group_atmosphere_account_mutations_require_admin_role(tmp_path):
    operator_client, _ = make_auth_client(tmp_path / 'operator', role='operator')
    denied = operator_client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-indo-01',
        'account_name': 'Atmosphere Indo 01',
        'groups': [],
        'enabled': True,
    })
    assert denied.status_code == 403

    admin_client, _ = make_auth_client(tmp_path / 'admin', role='admin')
    allowed = admin_client.post('/api/ops/group-atmosphere/accounts', json={
        'account_key': 'atmosphere-indo-01',
        'account_name': 'Atmosphere Indo 01',
        'groups': [],
        'enabled': True,
    })
    assert allowed.status_code == 200


def test_group_atmosphere_rejects_untrusted_worker_base_url(tmp_path):
    client, _ = make_auth_client(tmp_path, role='admin')

    response = client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'indo-reg-01',
        'enabled': True,
        'account_key': 'wa-seed-01',
        'target_group': '120363000000000000@g.us',
        'worker_base_url': 'http://169.254.169.254/latest',
        'template_pool': [{'template_id': 't1', 'text': 'Halo kk'}],
    })
    assert response.status_code == 400
    assert response.json()['detail'] == 'invalid_worker_base_url'


def test_group_atmosphere_mutations_reject_cross_site_origin(tmp_path):
    client, _ = make_auth_client(tmp_path, role='admin')

    response = client.post(
        '/api/ops/group-atmosphere/candidate-pool/reorder',
        headers={'Origin': 'https://evil.example'},
        json={'config_name': 'missing', 'candidate_ids': ['c1']},
    )
    assert response.status_code == 403
    assert response.json()['detail'] == 'ops_csrf_origin_forbidden'


def test_group_atmosphere_scheduler_run_due_requires_internal_token(tmp_path):
    client, internal_token = make_auth_client(tmp_path, role='admin')

    denied = client.post('/api/ops/group-atmosphere/scheduler/run-due', json={})
    assert denied.status_code == 403

    allowed = client.post(
        '/api/ops/group-atmosphere/scheduler/run-due',
        headers={'x-ops-internal-token': internal_token},
        json={},
    )
    assert allowed.status_code == 200


def test_group_atmosphere_reorder_and_delete_write_audit_log(tmp_path):
    client, _ = make_auth_client(tmp_path, role='admin')
    client.post('/api/ops/group-atmosphere/configs', json={
        'config_name': 'role-community',
        'enabled': True,
        'account_key': 'wa-seed-01',
        'target_group': '120363000000000000@g.us',
        'template_pool': [
            {'template_id': 'c1', 'candidate_id': 'c1', 'text': 'Halo kk', 'safe_to_send': True, 'enabled': True},
            {'template_id': 'c2', 'candidate_id': 'c2', 'text': 'Jgn lupa kirim kode ya', 'safe_to_send': True, 'enabled': True},
        ],
        'status': 'library_only',
    })

    reorder = client.post('/api/ops/group-atmosphere/candidate-pool/reorder', json={
        'config_name': 'role-community',
        'candidate_ids': ['c2', 'c1'],
    })
    assert reorder.status_code == 200
    delete = client.delete('/api/ops/group-atmosphere/candidate-pool/role-community/c1')
    assert delete.status_code == 200

    rows = client.get('/api/ops/operator-audit-log').json()['rows']
    event_types = {row['event_type'] for row in rows}
    assert 'group_atmosphere_candidate_reordered' in event_types
    assert 'group_atmosphere_candidate_deleted' in event_types
