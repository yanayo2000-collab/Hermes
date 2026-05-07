#!/bin/bash
set -euo pipefail
cd /Users/chauncey/work/mcn-ai-automation
source /Users/chauncey/work/mcn-ai-automation/scripts/lib/launchd_service.sh
source /Users/chauncey/work/mcn-ai-automation/scripts/lib/port_utils.sh

LABEL="com.chauncey.mcn.official-group-bridge"
PLIST_SOURCE="/Users/chauncey/work/mcn-ai-automation/scripts/launchd/$LABEL.plist"
PLIST_TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ -f "$PLIST_TARGET" ]]; then
  cp "$PLIST_SOURCE" "$PLIST_TARGET"
  chmod 644 "$PLIST_TARGET"
  chmod +x /Users/chauncey/work/mcn-ai-automation/scripts/run_official_group_bridge.sh /Users/chauncey/work/mcn-ai-automation/scripts/start_official_group_bridge.sh
  terminate_listener 55801
  launchd_bootstrap_service "$LABEL" "$PLIST_TARGET"
  if wait_for_launchd_http_service "$LABEL" 'http://127.0.0.1:55801/healthz' 30 2; then
    exit 0
  fi
  echo "official group bridge launch agent restart timed out" >&2
  exit 1
fi

terminate_listener 55801
exec /Users/chauncey/work/mcn-ai-automation/scripts/run_official_group_bridge.sh
