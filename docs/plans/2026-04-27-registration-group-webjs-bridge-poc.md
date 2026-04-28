# Registration Group whatsapp-web.js Bridge POC Plan

> For Hermes: Use subagent-driven-development skill to implement this plan task-by-task.

Goal: Replace the unstable Playwright DOM approval executor with a parallel whatsapp-web.js-based registration-group approval worker, while preserving the existing Python backend, approval_run_id, verification semantics, and CRM gating.

Architecture: Keep the current FastAPI/Python service as the system of record. Add a separate Node worker that owns the WhatsApp Web session and exposes a local HTTP bridge for health, warmup, and approve actions. The Python backend swaps only the registration-group approval executor implementation, so ingress queue, evidence handling, verified semantics, and CRM writeback stay unchanged.

Tech Stack: Python/FastAPI, requests, Node.js, whatsapp-web.js, LocalAuth/persistent session, local HTTP bridge.

---

### Task 1: Add Python bridge executor

Objective: Create a Python-side executor that talks to a local Node whatsapp-web.js worker over HTTP and returns the same dict contract expected by the current approval pipeline.

Files:
- Create: `app/registration_group_webjs_executor.py`
- Modify: `app/main.py`
- Test: `tests/test_api.py`

Steps:
1. Write failing tests for:
   - bridge executor `health()` shape
   - bridge executor `approve(context)` POST passthrough + normalization
   - `create_app()` wiring for `REGISTRATION_GROUP_APPROVAL_EXECUTOR_KIND=webjs_bridge`
2. Run targeted pytest and verify failure.
3. Implement minimal bridge executor using `requests.Session`.
4. Wire new executor kind in `create_app()`.
5. Re-run targeted pytest until pass.

### Task 2: Add Node worker scaffold

Objective: Create a runnable Node worker skeleton with local health/warmup/approve endpoints and session-ready state.

Files:
- Create: `webjs-approval-worker/package.json`
- Create: `webjs-approval-worker/src/server.js`
- Create: `webjs-approval-worker/README.md`

Steps:
1. Add minimal package.json with `whatsapp-web.js` dependency and start script.
2. Add HTTP server skeleton exposing:
   - `GET /health`
   - `POST /warmup`
   - `POST /approve`
3. Return stable JSON schema compatible with Python bridge.
4. Document env vars and startup commands.

### Task 3: Preserve backend contract

Objective: Ensure bridge executor outputs preserve current approval pipeline fields so existing evidence/CRM code remains unchanged.

Files:
- Modify: `app/registration_group_webjs_executor.py`
- Test: `tests/test_api.py`

Steps:
1. Add tests for invalid JSON / non-200 / timeout normalization.
2. Confirm bridge `raw_result.approval_run_id` and main pipeline status mapping still behave correctly.
3. Run focused API tests.

### Task 4: Install Node worker dependencies and smoke-test locally

Objective: Make the Node worker start locally and answer health without replacing the live Playwright path yet.

Files:
- Use: `webjs-approval-worker/package.json`

Steps:
1. Run `npm install` inside `webjs-approval-worker`.
2. Start the worker locally.
3. Verify `GET /health` responds.
4. Keep worker isolated from the current live production executor until approval endpoints are implemented.

### Task 5: Implement whatsapp-web.js approval path

Objective: Replace Node scaffold stub logic with real WhatsApp Web session ownership and membership-request approval handling.

Files:
- Modify: `webjs-approval-worker/src/server.js`
- Test: add Node-side unit/integration coverage if practical

Steps:
1. Confirm exact current library APIs for membership-request fetch/approve in the installed version.
2. Implement LocalAuth-backed client lifecycle.
3. Implement `warmup` to initialize and authenticate the client.
4. Implement `approve` to:
   - resolve target group
   - fetch membership requests
   - select target by hints when available
   - approve request(s)
   - return evidence payload compatible with Python backend
5. Only after local smoke pass, consider adding this executor kind to a non-production runtime.

### Task 6: Switch runtime behind config

Objective: Make Node worker selectable via config without deleting the Playwright fallback.

Files:
- Modify: `app/main.py`
- Modify: deployment/run scripts if needed

Steps:
1. Add config/env knobs for worker URL/token/timeouts.
2. Keep `live_whatsapp` as fallback kind.
3. Add runtime health fields that show the active provider clearly.
4. Verify no regression in existing API behavior when the bridge is not enabled.

### Verification

Python tests:
- `pytest -q tests/test_api.py -k 'registration_group_approval and webjs'`
- `pytest -q tests/test_api.py tests/test_registration_group_executor.py tests/test_live_monitor.py`

Node smoke:
- `cd webjs-approval-worker && npm install`
- `npm start`
- `curl http://127.0.0.1:<port>/health`

Acceptance for POC phase:
- Python backend can construct and call a Node bridge executor.
- Bridge executor health/approve contract is stable.
- Existing approval_run_id / verified / crm_recorded pipeline remains unchanged.
- Playwright executor remains available as fallback.
