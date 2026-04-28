#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SOURCE="$ROOT_DIR/scripts/launchd/com.chauncey.mcn.registration-group-backend.plist"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.chauncey.mcn.registration-group-backend.plist"
LABEL="com.chauncey.mcn.registration-group-backend"
GUI_DOMAIN="gui/$(id -u)"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT_DIR/logs"
cp "$PLIST_SOURCE" "$PLIST_TARGET"

launchctl bootout "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "$GUI_DOMAIN" "$PLIST_TARGET"
launchctl enable "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1 || true
launchctl kickstart -k "$GUI_DOMAIN/$LABEL"

for _ in {1..30}; do
  sleep 2
  if curl -sf http://127.0.0.1:8011/health >/dev/null 2>&1; then
    echo '{"status":"ok","service":"com.chauncey.mcn.registration-group-backend"}'
    exit 0
  fi
done

echo "backend launch agent installed but health check timed out" >&2
exit 1
