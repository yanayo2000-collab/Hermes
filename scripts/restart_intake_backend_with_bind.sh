#!/bin/bash
set -euo pipefail
cd /Users/chauncey/work/mcn-ai-automation

source /Users/chauncey/work/mcn-ai-automation/scripts/lib/launchd_service.sh
LABEL="com.chauncey.mcn.registration-group-backend"
PLIST_SOURCE="/Users/chauncey/work/mcn-ai-automation/scripts/launchd/$LABEL.plist"
PLIST_TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ -f "$PLIST_TARGET" ]]; then
  cp "$PLIST_SOURCE" "$PLIST_TARGET"
  chmod 644 "$PLIST_TARGET"
  chmod +x /Users/chauncey/work/mcn-ai-automation/scripts/run_registration_group_backend.sh /Users/chauncey/work/mcn-ai-automation/scripts/restart_intake_backend_with_bind.sh
  launchd_bootstrap_service "$LABEL" "$PLIST_TARGET"
  if wait_for_launchd_http_service "$LABEL" 'http://127.0.0.1:8011/health' 30 2; then
    exit 0
  fi
  echo "backend launch agent restart timed out" >&2
  exit 1
fi

exec /Users/chauncey/work/mcn-ai-automation/scripts/run_registration_group_backend.sh
