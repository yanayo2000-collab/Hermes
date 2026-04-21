# MCN AI Automation

P0 automation service for lead ingestion, event collection, task orchestration, CRM sync, and daily summary.

## Web service

```bash
. .venv/bin/activate
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8011
```

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
