# webjs-approval-worker

Purpose
- Parallel Node worker for registration-group approval.
- Intended to replace only the unstable Playwright approval executor layer.
- Python backend remains the system of record for approval_run_id, verified semantics, and CRM writeback.

Current state
- This is a scaffold only.
- `/health` and `/warmup` are live.
- `/approve` currently returns a structured not-implemented failure so the Python side can integrate before real WhatsApp logic lands.

Routes
- `GET /health`
- `POST /warmup`
- `POST /approve`

Environment
- `REGISTRATION_GROUP_APPROVAL_WEBJS_HOST` default: `127.0.0.1`
- `REGISTRATION_GROUP_APPROVAL_WEBJS_PORT` default: `8787`

Startup
- `cd webjs-approval-worker`
- `npm install`
- `npm start`

Design notes
- Real implementation will use whatsapp-web.js with persistent auth.
- Python should call this worker through `WebjsBridgeRegistrationGroupApprovalExecutor`.
- Final worker output must preserve the backend contract fields used by the current pipeline:
  - `verified`
  - `result_code`
  - `result_reason`
  - `approved_count`
  - `target_member`
  - `raw_result.approval_run_id`

Next implementation step
- Replace scaffold `/approve` with real membership-request fetch + approval flow after exact library API verification against installed whatsapp-web.js version.
