#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/launchd_service.sh"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.chauncey.mcn.memory-pressure-relief.plist"
LABEL="com.chauncey.mcn.memory-pressure-relief"

launchd_uninstall_service "$LABEL" "$PLIST_TARGET"
echo '{"status":"ok","service":"com.chauncey.mcn.memory-pressure-relief","action":"uninstalled"}'
