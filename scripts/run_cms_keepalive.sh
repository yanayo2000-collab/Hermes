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

if [[ -f "$ROOT_DIR/data/cms_keepalive.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/data/cms_keepalive.env"
  set +a
fi

exec python3 "$ROOT_DIR/scripts/cms_keepalive.py" \
  --env-path "${CMS_KEEPALIVE_ENV_PATH:-$ROOT_DIR/data/cms_keepalive.env}" \
  --state-path "${CMS_KEEPALIVE_STATE_PATH:-$ROOT_DIR/data/cms_keepalive_status.json}" \
  --timeout-seconds "${CMS_KEEPALIVE_TIMEOUT_SECONDS:-30}" \
  >> "$LOG_DIR/cms_keepalive.log" 2>> "$LOG_DIR/cms_keepalive.error.log"
