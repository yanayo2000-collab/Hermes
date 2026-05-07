#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/launchd_service.sh"
PLIST_SOURCE="$ROOT_DIR/scripts/launchd/com.chauncey.mcn.production-ops-daemon.plist"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.chauncey.mcn.production-ops-daemon.plist"
LABEL="com.chauncey.mcn.production-ops-daemon"
ENV_PATH="$ROOT_DIR/data/production_ops_daemon.env"
SESSION_ID="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
TMP_ENV="$ENV_PATH.tmp.$$"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT_DIR/logs" "$ROOT_DIR/data"
cp "$PLIST_SOURCE" "$PLIST_TARGET"
chmod 644 "$PLIST_TARGET"
chmod +x "$ROOT_DIR/scripts/start_production_ops_daemon.sh" "$ROOT_DIR/scripts/run_production_ops_daemon.sh" "$ROOT_DIR/scripts/production_ops_daemon.py"

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

launchd_bootstrap_service "$LABEL" "$PLIST_TARGET"
if wait_for_launchd_process_service "$LABEL" 'production_ops_daemon.py' 30 2; then
  echo '{"status":"ok","service":"com.chauncey.mcn.production-ops-daemon"}'
  exit 0
fi

echo "production ops daemon launch agent installed but verification timed out" >&2
exit 1
