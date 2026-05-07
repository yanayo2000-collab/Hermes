#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/launchd_service.sh"
LABEL="com.chauncey.mcn.registration-group-backend"
backend_pid="$(lsof -tiTCP:8011 -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
backend_health="$(curl -s http://127.0.0.1:8011/health 2>/dev/null || true)"
launch_state="$(launchd_state "$LABEL")"
last_exit="$(launchd_last_exit_code "$LABEL")"
python3 - <<'PY' "$backend_pid" "$backend_health" "$launch_state" "$last_exit"
import json, sys
backend_pid, backend_health, launch_state, last_exit = sys.argv[1:5]
out = {
    'backend': {
        'pid': backend_pid or None,
        'listening': bool(backend_pid),
        'health': json.loads(backend_health) if backend_health else None,
    },
    'launchd': {
        'label': 'com.chauncey.mcn.registration-group-backend',
        'state': launch_state or None,
        'last_exit_code': last_exit or None,
    },
}
print(json.dumps(out, ensure_ascii=False))
PY
