# MCN AI Automation

P0 automation service for lead ingestion, event collection, task orchestration, CRM sync, and daily summary.

## Web service

```bash
. .venv/bin/activate
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8011
```

## Minimal WhatsApp webhook service + official-group bridge service

Use this if you need:
- a real callback URL for Meta/WhatsApp webhook verification
- and/or a deployable official-group approval bridge service body (A2)

```bash
export WHATSAPP_WEBHOOK_VERIFY_TOKEN=replac...oken
export OFFICIAL_GROUP_BRIDGE_TOKEN=replace-with-bridge-token
export OFFICIAL_GROUP_BRIDGE_MODE=mock_success
uvicorn app.official_webhook_bridge_app:create_app --factory --host 0.0.0.0 --port 8091
```

After deployment, your webhook URL looks like:
- `https://your-domain.com/webhooks/whatsapp`

Verification endpoint:
- `GET /webhooks/whatsapp`

Event receiver endpoint:
- `POST /webhooks/whatsapp`

Latest received event (for ops/debug):
- `GET /ops/whatsapp-webhook/latest`

Recent webhook history:
- `GET /ops/whatsapp-webhook/recent`

Layered webhook stats:
- `GET /ops/whatsapp-webhook/stats`

Official-group bridge health:
- `GET /ops/official-group-bridge/health`

Official-group bridge request history:
- `GET /ops/official-group-bridge/requests`

Official-group approve endpoint for Hermes executor:
- `POST /official-group/approve`

Bridge modes:
- `mock_success`
- `mock_retryable_failed`
- `manual_queue`
- `passthrough_webhook`

## Multi-worker web service

```bash
. .venv/bin/activate
pip install gunicorn
./scripts/run_web.sh
```

## Async ingress worker

```bash
. .venv/bin/activate
./scripts/run_worker.sh
```

## Reverse proxy

- Nginx config: `deploy/nginx/mcn-intake.conf`
- Caddy config: `deploy/caddy/Caddyfile`

## Infra bootstrap

```bash
docker compose -f deploy/docker-compose.infra.yml up -d
```

## PostgreSQL schema bootstrap

```bash
psql postgresql://mcn:mcn@127.0.0.1:5432/mcn_automation -f sql/postgres_schema.sql
```

## Test

```bash
. .venv/bin/activate
pytest tests/test_api.py tests/test_crm_adapter.py tests/test_native_ocr.py -q
```

## Real success chain verifier

Use this to verify one submission across four gates: parse -> bind -> CRM create -> CRM verify.

```bash
. .venv/bin/activate
python scripts/verify_real_success_chain.py \
  --mobile '+62 857-4748-2100' \
  --account-id 51717067 \
  --invite-code BKDYCS \
  --registration-group PERMATA-909
```

Output summary:
- `REAL_SUCCESS_CONFIRMED` = parse/bind/CRM create/CRM verify all passed
- `REAL_SUCCESS_NOT_CONFIRMED` = at least one stage failed
- `LEAD_NOT_FOUND` = no matching lead resolved from the query

## Official-group bridge readiness verifier

Use this before real-scene testing to verify that the official-group approval executor is configured and exposed in runtime health.

```bash
. .venv/bin/activate
python scripts/verify_official_group_bridge_ready.py
```

Expected summary values:
- `OFFICIAL_GROUP_BRIDGE_READY`
- `OFFICIAL_GROUP_BRIDGE_NOT_READY`
- `OFFICIAL_GROUP_BRIDGE_UNREACHABLE`

Key readiness checks:
- runtime health exposes `official_group_approval`
- CRM is enabled and healthy
- executor is configured
- executor status is `healthy`
- schema version is present
- official-group summary endpoint is available

## Official-group local smoke test

You can use the mock bridge first, then run a full approval smoke test against a local service instance wired to that bridge.

Start mock bridge:
```bash
python scripts/mock_official_group_bridge.py --port 55801 --mode success
```

Start service with webhook executor config:
```bash
export OFFICIAL_GROUP_APPROVAL_EXECUTOR_KIND=webhook
export OFFICIAL_GROUP_APPROVAL_WEBHOOK_URL=http://127.0.0.1:55801
export OFFICIAL_GROUP_APPROVAL_WEBHOOK_TOKEN=test-token
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8011
```

Run smoke verifier:
```bash
python scripts/verify_official_group_smoke.py --base-url http://127.0.0.1:8011
```

Expected summary values:
- `OFFICIAL_GROUP_SMOKE_SUCCESS`
- `OFFICIAL_GROUP_SMOKE_FAILED`

If it fails, inspect:
- `failure_reason_code`
- `failure_next_action`

A common pre-prod failure is:
- `crm_verification_missing` -> means the current service was started without a healthy live CRM adapter, so the official-group gate correctly blocks execution.

## New async ingress endpoints

- `POST /api/intake/lark/events` (file-backed/live DB defaults to queued async ingress)
- `POST /api/intake/manual-cs-submissions` (file-backed/live DB can queue async ingress)
- `GET /api/ops/ingress-queue`
- `POST /api/ops/ingress-queue/run-next`
- `GET /api/ops/operator-audit-log`

## Current migration stance

- SQLite remains the default execution store for the existing MVP chain.
- High-concurrency ingress now has queue/idempotency/audit primitives inside the service.
- PostgreSQL + Redis bootstrap files are included for the next migration step away from single-node SQLite.
