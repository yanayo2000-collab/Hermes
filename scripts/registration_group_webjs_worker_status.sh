#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${REGISTRATION_GROUP_APPROVAL_WEBJS_BASE_URL:-}"
HEALTH_URL="${REGISTRATION_GROUP_APPROVAL_WEBJS_HEALTH_URL:-${BASE_URL:+${BASE_URL%/}/health}}"
LOG_FILE_DEFAULT="$(cd "$(dirname "$0")/.." && pwd)/logs/registration_group_webjs_worker.log"
LOG_FILE="${REGISTRATION_GROUP_APPROVAL_WEBJS_LOG_FILE:-$LOG_FILE_DEFAULT}"

if [[ -z "$HEALTH_URL" ]]; then
  echo '{"configured":false,"error":"REGISTRATION_GROUP_APPROVAL_WEBJS_HEALTH_URL or REGISTRATION_GROUP_APPROVAL_WEBJS_BASE_URL is required"}'
  exit 2
fi

python3 - <<'PY'
import json
import os
import urllib.request

health_url = os.environ.get('REGISTRATION_GROUP_APPROVAL_WEBJS_HEALTH_URL') or os.environ.get('HEALTH_URL')
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