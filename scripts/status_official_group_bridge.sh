#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/launchd_service.sh"
LABEL="com.chauncey.mcn.official-group-bridge"
bridge_pid="$(lsof -tiTCP:55801 -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
bridge_health="$(curl -s http://127.0.0.1:55801/healthz 2>/dev/null || true)"
launch_state="$(launchd_state "$LABEL")"
last_exit="$(launchd_last_exit_code "$LABEL")"
python3 - <<'PY' "$bridge_pid" "$bridge_health" "$launch_state" "$last_exit"
import json, sys
bridge_pid, bridge_health, launch_state, last_exit = sys.argv[1:5]
out = {
    'bridge': {
        'pid': bridge_pid or None,
        'listening': bool(bridge_pid),
        'health': json.loads(bridge_health) if bridge_health else None,
    },
    'launchd': {
        'label': 'com.chauncey.mcn.official-group-bridge',
        'state': launch_state or None,
        'last_exit_code': last_exit or None,
    },
}
print(json.dumps(out, ensure_ascii=False))
PY
