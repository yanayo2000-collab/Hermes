#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/launchd_service.sh"
LABEL="com.chauncey.mcn.cms-keepalive"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.chauncey.mcn.cms-keepalive.plist"

launchd_bootout_service "$LABEL" || true
rm -f "$PLIST_TARGET"
echo '{"status":"ok","service":"com.chauncey.mcn.cms-keepalive","removed":true}'
