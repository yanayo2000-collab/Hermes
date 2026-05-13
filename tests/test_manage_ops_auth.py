import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'manage_ops_auth.py'


def run_cli(*args):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result


def test_manage_ops_auth_bootstrap_and_create_user(tmp_path):
    db_path = tmp_path / 'automation.db'

    status_before = run_cli('--db-path', str(db_path), 'status')
    assert status_before.returncode == 0
    payload_before = json.loads(status_before.stdout)
    assert payload_before['bootstrap_open'] is True
    assert payload_before['user_count'] == 0

    bootstrap = run_cli(
        '--db-path', str(db_path),
        'bootstrap-admin',
        '--username', 'admin01',
        '--password', 'secret123',
        '--display-name', 'Admin',
    )
    assert bootstrap.returncode == 0
    bootstrap_payload = json.loads(bootstrap.stdout)
    assert bootstrap_payload['user']['role'] == 'super_admin'

    create_user = run_cli(
        '--db-path', str(db_path),
        'create-user',
        '--username', 'ops01',
        '--password', 'operator123',
        '--display-name', '运营1',
        '--role', 'operator',
    )
    assert create_user.returncode == 0
    create_payload = json.loads(create_user.stdout)
    assert create_payload['user']['role'] == 'operator'

    status_after = run_cli('--db-path', str(db_path), 'status')
    assert status_after.returncode == 0
    payload_after = json.loads(status_after.stdout)
    assert payload_after['bootstrap_open'] is False
    assert payload_after['user_count'] == 2
    assert [row['username'] for row in payload_after['users']] == ['admin01', 'ops01']


def test_manage_ops_auth_update_user(tmp_path):
    db_path = tmp_path / 'automation.db'
    bootstrap = run_cli(
        '--db-path', str(db_path),
        'bootstrap-admin',
        '--username', 'admin01',
        '--password', 'secret123',
    )
    assert bootstrap.returncode == 0
    user_id = json.loads(bootstrap.stdout)['user']['user_id']

    update = run_cli(
        '--db-path', str(db_path),
        'update-user',
        '--user-id', user_id,
        '--display-name', '新管理员',
        '--disabled',
    )
    assert update.returncode == 0
    payload = json.loads(update.stdout)
    assert payload['user']['display_name'] == '新管理员'
    assert payload['user']['enabled'] is False


def test_manage_ops_auth_ensure_internal_token(tmp_path):
    env_path = tmp_path / 'internal_auth.env'

    created = run_cli(
        'ensure-internal-token',
        '--env-path', str(env_path),
        '--token', 'token-123',
    )
    assert created.returncode == 0
    created_payload = json.loads(created.stdout)
    assert created_payload['changed'] is True
    assert env_path.read_text(encoding='utf-8') == "export AUTH_INTERNAL_TOKEN='token-123'\n"

    preserved = run_cli('ensure-internal-token', '--env-path', str(env_path))
    assert preserved.returncode == 0
    preserved_payload = json.loads(preserved.stdout)
    assert preserved_payload['changed'] is False
    assert preserved_payload['reason'] == 'already_present'

    forced = run_cli(
        'ensure-internal-token',
        '--env-path', str(env_path),
        '--token', 'token-456',
        '--force',
    )
    assert forced.returncode == 0
    forced_payload = json.loads(forced.stdout)
    assert forced_payload['changed'] is True
    assert env_path.read_text(encoding='utf-8') == "export AUTH_INTERNAL_TOKEN='token-456'\n"
