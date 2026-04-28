#!/usr/bin/env bash
set -euo pipefail

LABEL="com.chauncey.mcn.registration-group-backend"
GUI_DOMAIN="gui/$(id -u)"
PLIST_TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1 || true
launchctl disable "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1 || true
rm -f "$PLIST_TARGET"

if lsof -tiTCP:8011 -sTCP:LISTEN >/dev/null 2>&1; then
  kill $(lsof -tiTCP:8011 -sTCP:LISTEN) || true
fi

echo '{"status":"stopped","service":"com.chauncey.mcn.registration-group-backend"}'
