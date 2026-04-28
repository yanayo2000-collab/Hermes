#!/usr/bin/env bash
set -euo pipefail

LABEL="com.chauncey.mcn.production-ops-daemon"
PLIST_TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
GUI_DOMAIN="gui/$(id -u)"

launchctl bootout "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1 || true
rm -f "$PLIST_TARGET"

echo '{"status":"ok","service":"com.chauncey.mcn.production-ops-daemon","removed":true}'
