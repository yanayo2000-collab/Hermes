#!/usr/bin/env bash
set -euo pipefail

LABEL="com.chauncey.mcn.registration-group-backend"
GUI_DOMAIN="gui/$(id -u)"
backend_pid="$(lsof -tiTCP:8011 -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
backend_health="$(curl -s http://127.0.0.1:8011/health 2>/dev/null || true)"
launchctl print "$GUI_DOMAIN/$LABEL" >/tmp/registration_group_backend_launchctl_status.txt 2>/dev/null || true
launch_state="$(grep -E 'state = ' /tmp/registration_group_backend_launchctl_status.txt 2>/dev/null | head -n 1 | sed 's/^[[:space:]]*state = //' || true)"
last_exit="$(grep -E 'last exit code = ' /tmp/registration_group_backend_launchctl_status.txt 2>/dev/null | head -n 1 | sed 's/^[[:space:]]*last exit code = //' || true)"
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
rm -f /tmp/registration_group_backend_launchctl_status.txt
