#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$ROOT_DIR/logs"
mkdir -p "$LOG_DIR" "$ROOT_DIR/data"

if [[ -f "$HOME/.hermes/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$HOME/.hermes/.env"
  set +a
fi

if [[ -f "$ROOT_DIR/data/production_ops_daemon.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/data/production_ops_daemon.env"
  set +a
fi

exec python3 "$ROOT_DIR/scripts/production_ops_daemon.py" \
  --api-base-url "${PRODUCTION_OPS_API_BASE_URL:-http://127.0.0.1:8011}" \
  --worker-base-url "${PRODUCTION_OPS_WORKER_BASE_URL:-http://127.0.0.1:8787}" \
  --registration-group "${PRODUCTION_OPS_REGISTRATION_GROUP:-🇮🇩3️⃣7️⃣Grup Registrasi Resmi Linky 💎}" \
  --fresh-probe-cmd "${PRODUCTION_OPS_FRESH_PROBE_CMD:-node $ROOT_DIR/scripts/fresh_webjs_group_state.js $(printf '%q' "${PRODUCTION_OPS_REGISTRATION_GROUP:-🇮🇩3️⃣7️⃣Grup Registrasi Resmi Linky 💎}")}" \
  --worker-restart-cmd "${PRODUCTION_OPS_WORKER_RESTART_CMD:-$ROOT_DIR/scripts/restart_registration_group_webjs_worker.sh}" \
  --backend-restart-cmd "${PRODUCTION_OPS_BACKEND_RESTART_CMD:-$ROOT_DIR/scripts/ensure_registration_group_backend.sh}" \
  --state-path "${PRODUCTION_OPS_STATE_PATH:-$ROOT_DIR/data/production_ops_daemon_state.json}" \
  --status-path "${PRODUCTION_OPS_STATUS_PATH:-$ROOT_DIR/data/production_ops_daemon_status.json}" \
  --interval-seconds "${PRODUCTION_OPS_INTERVAL_SECONDS:-20}" \
  --monitoring-session-id "${PRODUCTION_OPS_MONITORING_SESSION_ID:-}" \
  --notify-chat-id "${PRODUCTION_OPS_NOTIFY_CHAT_ID:-${FEISHU_HOME_CHANNEL:-}}" \
  --feishu-app-id "${PRODUCTION_OPS_FEISHU_APP_ID:-${FEISHU_APP_ID:-}}" \
  --feishu-app-secret "${PRODUCTION_OPS_FEISHU_APP_SECRET:-${FEISHU_APP_SECRET:-}}" \
  --feishu-domain "${PRODUCTION_OPS_FEISHU_DOMAIN:-${FEISHU_DOMAIN:-lark}}" \
  >> "$LOG_DIR/production_ops_daemon.log" 2>> "$LOG_DIR/production_ops_daemon.error.log"
