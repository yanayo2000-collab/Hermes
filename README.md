# MCN AI Automation

P0 automation service for Lead ingestion, event collection, task orchestration, CRM sync, and daily summary.

## Run

```bash
. .venv/bin/activate
uvicorn app.main:app --reload
```

## Test

```bash
. .venv/bin/activate
pytest -q
```
