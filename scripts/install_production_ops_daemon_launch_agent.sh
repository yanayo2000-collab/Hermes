#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SOURCE="$ROOT_DIR/scripts/launchd/com.chauncey.mcn.production-ops-daemon.plist"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.chauncey.mcn.production-ops-daemon.plist"
LABEL="com.chauncey.mcn.production-ops-daemon"
GUI_DOMAIN="gui/$(id -u)"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT_DIR/logs" "$ROOT_DIR/data"
cp "$PLIST_SOURCE" "$PLIST_TARGET"
chmod 644 "$PLIST_TARGET"
chmod +x "$ROOT_DIR/scripts/start_production_ops_daemon.sh" "$ROOT_DIR/scripts/production_ops_daemon.py"

ENV_PATH="$ROOT_DIR/data/production_ops_daemon.env"
SESSION_ID="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
TMP_ENV="$ENV_PATH.tmp.$$"
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

launchctl bootout "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "$GUI_DOMAIN" "$PLIST_TARGET"
launchctl enable "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1 || true
launchctl kickstart -k "$GUI_DOMAIN/$LABEL"

echo '{"status":"ok","service":"com.chauncey.mcn.production-ops-daemon"}'
