#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/launchd_service.sh"
PLIST_SOURCE="$ROOT_DIR/scripts/launchd/com.chauncey.mcn.registration-group-backend.plist"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.chauncey.mcn.registration-group-backend.plist"
LABEL="com.chauncey.mcn.registration-group-backend"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT_DIR/logs"
cp "$PLIST_SOURCE" "$PLIST_TARGET"
chmod 644 "$PLIST_TARGET"
chmod +x "$ROOT_DIR/scripts/run_registration_group_backend.sh" "$ROOT_DIR/scripts/restart_intake_backend_with_bind.sh"

launchd_bootstrap_service "$LABEL" "$PLIST_TARGET"
if wait_for_launchd_http_service "$LABEL" 'http://127.0.0.1:8011/health' 30 2; then
  echo '{"status":"ok","service":"com.chauncey.mcn.registration-group-backend"}'
  exit 0
fi

echo "backend launch agent installed but verification timed out" >&2
exit 1
