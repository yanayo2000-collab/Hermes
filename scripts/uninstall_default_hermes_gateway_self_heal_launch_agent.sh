#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/launchd_service.sh"
LABEL="com.chauncey.default-hermes-gateway-self-heal"
PLIST_TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
launchd_uninstall_service "$LABEL" "$PLIST_TARGET"
echo '{"status":"ok","service":"com.chauncey.default-hermes-gateway-self-heal","action":"uninstalled"}'
