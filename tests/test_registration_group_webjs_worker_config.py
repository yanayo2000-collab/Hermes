from pathlib import Path


def _extract_default(script_text: str, env_name: str) -> str:
    needle = f': "${{{env_name}:='
    for line in script_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(needle) and stripped.endswith('}"'):
            return stripped[len(needle):-2]
    raise AssertionError(f'missing default for {env_name}')


def test_webjs_worker_restart_script_uses_higher_timeout_defaults_for_real_group_batches():
    script_text = Path('scripts/restart_registration_group_webjs_worker.sh').read_text()

    assert _extract_default(script_text, 'REGISTRATION_GROUP_APPROVAL_WEBJS_APPROVE_CALL_TIMEOUT_MS') == '15000'
    assert _extract_default(script_text, 'REGISTRATION_GROUP_APPROVAL_WEBJS_PER_REQUESTER_TIMEOUT_MS') == '5000'
    assert _extract_default(script_text, 'REGISTRATION_GROUP_APPROVAL_WEBJS_VERIFY_WAIT_MS') == '1200'
    assert _extract_default(script_text, 'REGISTRATION_GROUP_APPROVAL_WEBJS_VERIFY_RETRIES') == '4'


def test_webjs_worker_group_state_uses_same_approval_client_as_real_approval_path():
    server_text = Path('webjs-approval-worker/src/server.js').read_text()

    assert 'const SHARED_APPROVAL_CLIENT = !REUSE_CHROME_PROFILE;' in server_text
    assert 'function syncApprovalStateFromPrimary() {' in server_text
    assert 'if (SHARED_APPROVAL_CLIENT) {' in server_text
    assert "const group = await resolveApprovalGroup(context.registration_group);" in server_text
    assert "const requests = await getApprovalRequestEnriched(group);" in server_text
    assert "await ensureApprovalClientStarted();" in server_text
    assert "await waitForApprovalReady(QR_TIMEOUT_MS).catch(() => {" in server_text
    assert "auth_strategy: approvalState.auth_strategy," in server_text


def test_webjs_worker_restart_script_supports_dedicated_localauth_mode():
    script_text = Path('scripts/restart_registration_group_webjs_worker.sh').read_text()

    assert 'REGISTRATION_GROUP_APPROVAL_WEBJS_AUTH_MODE' in script_text
    assert 'dedicated_localauth' in script_text
    assert 'REGISTRATION_GROUP_APPROVAL_WEBJS_AUTH_DATA_PATH' in script_text
    assert 'REGISTRATION_GROUP_APPROVAL_WEBJS_CLIENT_ID' in script_text


def test_webjs_worker_localauth_switch_helper_exports_dedicated_mode_and_reuses_restart_script():
    script_text = Path('scripts/switch_registration_group_webjs_worker_to_localauth.sh').read_text()

    assert 'REGISTRATION_GROUP_APPROVAL_WEBJS_AUTH_MODE=dedicated_localauth' in script_text
    assert 'REGISTRATION_GROUP_APPROVAL_WEBJS_AUTH_DATA_PATH' in script_text
    assert 'restart_registration_group_webjs_worker.sh' in script_text
    assert "auth_strategy != 'LocalAuth'" in script_text


def test_webjs_worker_status_helper_reads_health_endpoint():
    script_text = Path('scripts/registration_group_webjs_worker_status.sh').read_text()

    assert 'http://127.0.0.1:8787/health' in script_text
    assert 'approval_auth_strategy' in script_text
    assert 'approval_auth_path' in script_text
