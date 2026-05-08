#!/usr/bin/env bash
set -euo pipefail

backend_pid="$(lsof -tiTCP:8011 -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
backend_health="$(curl -s http://127.0.0.1:8011/health 2>/dev/null || true)"
backend_launchd_state="$(./scripts/status_registration_group_backend.sh 2>/dev/null || true)"
group_state="$(python3 - <<'PY'
import json, urllib.parse, urllib.request
url = 'http://127.0.0.1:8011/api/ops/registration-group-approval-executor-group-state?' + urllib.parse.urlencode({'registration_group': '🇮🇩3️⃣7️⃣Grup Registrasi Resmi Linky 💎'})
try:
    with urllib.request.urlopen(url, timeout=15) as resp:
        print(resp.read().decode('utf-8'))
except Exception:
    pass
PY
)"

python3 - <<'PY' "$backend_pid" "$backend_health" "$backend_launchd_state" "$group_state"
import json, sys
backend_pid, backend_health, backend_launchd_state, group_state = sys.argv[1:5]
out = {
    'backend': {
        'pid': backend_pid or None,
        'listening': bool(backend_pid),
        'health': json.loads(backend_health) if backend_health else None,
        'launchd': (json.loads(backend_launchd_state).get('launchd') if backend_launchd_state else None),
    },
    'registration_runtime': json.loads(group_state) if group_state else None,
}
print(json.dumps(out, ensure_ascii=False))
PY
