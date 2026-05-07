#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/launchd_service.sh"
LABEL="com.chauncey.mcn.production-ops-daemon"
STATUS_FILE="$ROOT_DIR/data/production_ops_daemon_status.json"
daemon_pid="$(pgrep -f 'production_ops_daemon.py' | head -n 1 || true)"
launch_state="$(launchd_state "$LABEL")"
last_exit="$(launchd_last_exit_code "$LABEL")"
status_json="$(cat "$STATUS_FILE" 2>/dev/null || true)"
python3 - <<'PY' "$daemon_pid" "$launch_state" "$last_exit" "$status_json"
import json, sys
pid, launch_state, last_exit, status_json = sys.argv[1:5]
out = {
    'daemon': {
        'pid': pid or None,
        'running': bool(pid),
        'status': json.loads(status_json) if status_json else None,
    },
    'launchd': {
        'label': 'com.chauncey.mcn.production-ops-daemon',
        'state': launch_state or None,
        'last_exit_code': last_exit or None,
    },
}
print(json.dumps(out, ensure_ascii=False))
PY
