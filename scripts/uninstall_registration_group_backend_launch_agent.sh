#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/launchd_service.sh"
LABEL="com.chauncey.mcn.registration-group-backend"
PLIST_TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

launchd_uninstall_service "$LABEL" "$PLIST_TARGET"

if lsof -tiTCP:8011 -sTCP:LISTEN >/dev/null 2>&1; then
  kill $(lsof -tiTCP:8011 -sTCP:LISTEN) || true
fi

echo '{"status":"stopped","service":"com.chauncey.mcn.registration-group-backend"}'
