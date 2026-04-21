#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
. .venv/bin/activate
exec python - <<'PY'
import time
from app.main import create_app

app = create_app({})
service = app.state.service
print('worker started')
while True:
    processed = service.process_next_ingress_job()
    if not processed:
        time.sleep(service.ingress_worker_poll_interval)
PY
