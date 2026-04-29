#!/usr/bin/env bash
set -euo pipefail

HEALTH_URL="${REGISTRATION_GROUP_APPROVAL_WEBJS_HEALTH_URL:-http://127.0.0.1:8787/health}"
LOG_FILE_DEFAULT="$(cd "$(dirname "$0")/.." && pwd)/logs/registration_group_webjs_worker.log"
LOG_FILE="${REGISTRATION_GROUP_APPROVAL_WEBJS_LOG_FILE:-$LOG_FILE_DEFAULT}"

python3 - <<'PY'
import json
import os
import urllib.request

health_url = os.environ.get('REGISTRATION_GROUP_APPROVAL_WEBJS_HEALTH_URL', 'http://127.0.0.1:8787/health')
log_file = os.environ.get('REGISTRATION_GROUP_APPROVAL_WEBJS_LOG_FILE')
with urllib.request.urlopen(health_url, timeout=15) as resp:
    body = json.loads(resp.read().decode('utf-8'))

approval = body.get('approval_client') if isinstance(body.get('approval_client'), dict) else {}
summary = {
    'worker_auth_strategy': body.get('auth_strategy'),
    'worker_ready': bool(body.get('ready')),
    'worker_authenticated': bool(body.get('authenticated')),
    'worker_status': body.get('status'),
    'approval_auth_strategy': approval.get('auth_strategy'),
    'approval_ready': bool(approval.get('ready')),
    'approval_authenticated': bool(approval.get('authenticated')),
    'approval_status': approval.get('status'),
    'approval_client_id': approval.get('client_id'),
    'approval_auth_path': approval.get('auth_path'),
    'approval_last_qr_at': approval.get('last_qr_at'),
    'log_file': log_file,
}
print(json.dumps(summary, ensure_ascii=False, indent=2))
PY