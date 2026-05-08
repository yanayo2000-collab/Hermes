#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AUTH_DATA_PATH_DEFAULT="$ROOT_DIR/webjs-approval-worker/.wwebjs_auth_dedicated"
AUTH_DATA_PATH="${REGISTRATION_GROUP_APPROVAL_WEBJS_AUTH_DATA_PATH:-$AUTH_DATA_PATH_DEFAULT}"
CLIENT_ID="${REGISTRATION_GROUP_APPROVAL_WEBJS_CLIENT_ID:-registration-group-approval}"
LOG_FILE="$ROOT_DIR/logs/registration_group_webjs_worker.log"
BASE_URL="${REGISTRATION_GROUP_APPROVAL_WEBJS_BASE_URL:-http://127.0.0.1:8787}"
HEALTH_URL="${REGISTRATION_GROUP_APPROVAL_WEBJS_HEALTH_URL:-${BASE_URL%/}/health}"

mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$AUTH_DATA_PATH"

export REGISTRATION_GROUP_APPROVAL_WEBJS_AUTH_MODE=dedicated_localauth
export REGISTRATION_GROUP_APPROVAL_WEBJS_AUTH_DATA_PATH="$AUTH_DATA_PATH"
export REGISTRATION_GROUP_APPROVAL_WEBJS_CLIENT_ID="$CLIENT_ID"

"$ROOT_DIR/scripts/restart_registration_group_webjs_worker.sh"

python3 - <<'PY'
import json
import os
import sys
import urllib.request

health_url = os.environ.get('REGISTRATION_GROUP_APPROVAL_WEBJS_HEALTH_URL') or os.environ.get('HEALTH_URL') or 'http://127.0.0.1:8787/health'
log_file = os.environ.get('REGISTRATION_GROUP_APPROVAL_WEBJS_LOG_FILE')
with urllib.request.urlopen(health_url, timeout=15) as resp:
    body = json.loads(resp.read().decode('utf-8'))

approval = body.get('approval_client') if isinstance(body.get('approval_client'), dict) else {}
auth_strategy = str(approval.get('auth_strategy') or body.get('auth_strategy') or '').strip()
ready = bool(approval.get('ready')) and bool(approval.get('authenticated'))
status = {
    'auth_strategy': auth_strategy,
    'ready': ready,
    'worker_status': body.get('status'),
    'approval_status': approval.get('status'),
    'auth_path': approval.get('auth_path') or body.get('auth_path'),
    'client_id': approval.get('client_id') or body.get('client_id'),
    'last_qr_at': approval.get('last_qr_at') or body.get('last_qr_at'),
    'log_file': log_file,
}
print(json.dumps(status, ensure_ascii=False, indent=2))
if auth_strategy != 'LocalAuth':
    sys.exit(2)
if not ready:
    sys.exit(3)
PY
