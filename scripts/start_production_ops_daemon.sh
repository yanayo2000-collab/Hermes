#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/launchd_service.sh"
LABEL="com.chauncey.mcn.production-ops-daemon"
PLIST_SOURCE="$ROOT_DIR/scripts/launchd/$LABEL.plist"
PLIST_TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
ENV_PATH="$ROOT_DIR/data/production_ops_daemon.env"
SESSION_ID="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
TMP_ENV="$ENV_PATH.tmp.$$"

mkdir -p "$ROOT_DIR/data"

if [[ -f "$PLIST_TARGET" ]]; then
  cp "$PLIST_SOURCE" "$PLIST_TARGET"
fi

if [[ -f "$ENV_PATH" ]]; then
  : > "$TMP_ENV"
  updated=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == PRODUCTION_OPS_MONITORING_SESSION_ID=* ]]; then
      printf 'PRODUCTION_OPS_MONITORING_SESSION_ID=%s\n' "$SESSION_ID" >> "$TMP_ENV"
      updated=1
    else
      printf '%s\n' "$line" >> "$TMP_ENV"
    fi
  done < "$ENV_PATH"
  if [[ $updated -eq 0 ]]; then
    printf 'PRODUCTION_OPS_MONITORING_SESSION_ID=%s\n' "$SESSION_ID" >> "$TMP_ENV"
  fi
  mv "$TMP_ENV" "$ENV_PATH"
else
  printf 'PRODUCTION_OPS_MONITORING_SESSION_ID=%s\n' "$SESSION_ID" > "$ENV_PATH"
fi

if [[ -f "$PLIST_TARGET" ]]; then
  launchd_bootstrap_service "$LABEL" "$PLIST_TARGET"
  if wait_for_launchd_process_service "$LABEL" 'production_ops_daemon.py' 30 2; then
    echo '{"status":"ok","service":"com.chauncey.mcn.production-ops-daemon"}'
    exit 0
  fi
  echo "production ops daemon launch agent restart timed out" >&2
  exit 1
fi

exec "$ROOT_DIR/scripts/run_production_ops_daemon.sh"
