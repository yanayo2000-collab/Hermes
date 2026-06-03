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
    assert 'function extractInviteCode(targetValue) {' in server_text
    assert 'const inviteInfo = await activeClient.getInviteInfo(inviteCode);' in server_text
    assert 'const groupId = inviteInfoGroupId(inviteInfo);' in server_text
    assert "let group = await resolveApprovalGroup(context.registration_group);" in server_text
    assert "let requestsBefore = await getApprovalRequestEnriched(group);" in server_text
    assert "await ensureApprovalClientStarted();" in server_text
    assert "await waitForApprovalReady(QR_TIMEOUT_MS).catch(() => {" in server_text
    assert "auth_strategy: approvalState.auth_strategy," in server_text
    assert "function isExecutionContextDestroyedError(error) {" in server_text
    assert "if (isExecutionContextDestroyedError(error)) {" in server_text
    assert "await sleep(400);" in server_text
    assert "return await groupStateWithRecovery(payload);" in server_text


def test_webjs_worker_exposes_probe_group_state_route_for_runtime_probe_client():
    server_text = Path('webjs-approval-worker/src/server.js').read_text()

    assert "async function probeGroupState(context, options = {}) {" in server_text
    assert "async function probeGroupStateWithRecovery(context) {" in server_text
    assert "if (req.method === 'POST' && req.url === '/probe-group-state') {" in server_text
    assert "await ensureClientStarted();" in server_text
    assert "await waitForReady(QR_TIMEOUT_MS).catch(() => {" in server_text
    assert "return await probeGroupStateWithRecovery(payload);" in server_text
    assert "auth_strategy: state.auth_strategy," in server_text
    assert "function isExecutionContextDestroyedError(error) {" in server_text
    assert "if (isExecutionContextDestroyedError(error)) {" in server_text


def test_webjs_worker_recovers_approval_client_from_navigation_crashes_marker():
    server_text = Path('webjs-approval-worker/src/server.js').read_text()

    assert "let approvalRefreshPromise = null;" in server_text
    assert "function scheduleApprovalClientRefresh(reason) {" in server_text
    assert "await resetApprovalClientSession(reason || 'approval_runtime_recoverable_error');" in server_text
    assert "await waitForApprovalQrOrReady(QR_TIMEOUT_MS).catch(() => {" in server_text
    assert "function installRecoverableApprovalErrorHandlers() {" in server_text
    assert "process.on('unhandledRejection', recoverableHandler('unhandled_rejection'));" in server_text
    assert "process.on('uncaughtException', recoverableHandler('uncaught_exception'));" in server_text
    assert "status: 'recovering_execution_context'," in server_text
    assert "last_qr: null," in server_text
    assert "last_qr_at: null," in server_text
    assert "installRecoverableApprovalErrorHandlers();" in server_text


def test_webjs_worker_fetch_group_messages_preserves_runtime_observation_fields_and_message_id_only_records():
    server_text = Path('webjs-approval-worker/src/server.js').read_text()

    assert "chat_id: safeString(group && group.id) || targetGroup," in server_text
    assert "from_me: Boolean(message && message.fromMe)," in server_text
    assert "message_type: safeString(message && message.type)," in server_text
    assert "if (!record.text && !record.message_id) return false;" in server_text


def test_webjs_worker_empty_queue_recheck_keeps_live_refresh_and_authoritative_full_verify_path():
    server_text = Path('webjs-approval-worker/src/server.js').read_text()

    assert "group = await reloadApprovalGroupFromFreshSession(context, 'empty_start_snapshot_recheck');" in server_text
    assert "await forceRefreshApprovalGroupBeforeRead(context, group, {" in server_text
    assert "stage: 'empty_queue_live_refresh_failed'," in server_text
    assert "const refreshedState = await groupStateWithRecovery({" in server_text
    assert "mode: 'full_verify'," in server_text
    assert "stage: 'empty_queue_authoritative_recheck_failed'," in server_text
    assert "stage: 'empty_queue_recheck_finished'," in server_text
    assert "error: emptyQueueRecheckError ? String(emptyQueueRecheckError && emptyQueueRecheckError.message ? emptyQueueRecheckError.message : emptyQueueRecheckError) : null," in server_text


def test_webjs_worker_marks_review_surface_positive_without_pending_section_and_without_requester_ids_as_suspected_residue():
    server_text = Path('webjs-approval-worker/src/server.js').read_text()

    assert "review_surface_ready: Boolean(surface.page_ready)," in server_text
    assert "source: 'approval_review_surface'," in server_text
    assert "pending_zero_confidence: requesterRows.length <= 0 ? 'unverified' : null," in server_text


def test_fresh_group_state_script_uses_runtime_probe_endpoint_instead_of_temp_browser_copy():
    script_text = Path('scripts/fresh_webjs_group_state.js').read_text()

    assert "usage: fresh_webjs_group_state.js <registration_group> [worker_base_url]" in script_text
    assert "[deprecated] fresh_webjs_group_state.js now acts as a debug wrapper" in script_text
    assert "async function resolveWorkerBaseUrl(targetValue) {" in script_text
    assert '/api/ops/production-ops-daemon' in script_text
    assert '/api/ops/whatsapp-approval-accounts' in script_text
    assert '/group-state' in script_text
    assert "authoritative_source: 'group_state'" in script_text
    assert "JSON.stringify({ registration_group: registrationGroup })" in script_text
    assert "cp', ['-R'" not in script_text
    assert "new NoAuth()" not in script_text
    assert "new Client({" not in script_text


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

    assert 'REGISTRATION_GROUP_APPROVAL_WEBJS_BASE_URL' in script_text
    assert 'REGISTRATION_GROUP_APPROVAL_WEBJS_HEALTH_URL' in script_text
    assert 'approval_auth_strategy' in script_text
    assert 'approval_auth_path' in script_text
