#!/usr/bin/env bash
set -euo pipefail

backend_pid="$(lsof -tiTCP:8011 -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
worker_pid="$(lsof -tiTCP:8787 -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
backend_health="$(curl -s http://127.0.0.1:8011/health 2>/dev/null || true)"
worker_health="$(curl -s http://127.0.0.1:8787/health 2>/dev/null || true)"
backend_launchd_state="$(./scripts/status_registration_group_backend.sh 2>/dev/null || true)"

python3 - <<'PY' "$backend_pid" "$worker_pid" "$backend_health" "$worker_health" "$backend_launchd_state"
import json, sys
backend_pid, worker_pid, backend_health, worker_health, backend_launchd_state = sys.argv[1:6]
out = {
    'backend': {
        'pid': backend_pid or None,
        'listening': bool(backend_pid),
        'health': json.loads(backend_health) if backend_health else None,
        'launchd': (json.loads(backend_launchd_state).get('launchd') if backend_launchd_state else None),
    },
    'worker': {
        'pid': worker_pid or None,
        'listening': bool(worker_pid),
        'health': json.loads(worker_health) if worker_health else None,
    },
}
print(json.dumps(out, ensure_ascii=False))
PY
