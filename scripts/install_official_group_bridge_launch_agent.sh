#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/launchd_service.sh"
source "$ROOT_DIR/scripts/lib/port_utils.sh"
PLIST_SOURCE="$ROOT_DIR/scripts/launchd/com.chauncey.mcn.official-group-bridge.plist"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.chauncey.mcn.official-group-bridge.plist"
LABEL="com.chauncey.mcn.official-group-bridge"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT_DIR/logs"
cp "$PLIST_SOURCE" "$PLIST_TARGET"
chmod 644 "$PLIST_TARGET"
chmod +x "$ROOT_DIR/scripts/run_official_group_bridge.sh" "$ROOT_DIR/scripts/start_official_group_bridge.sh"

terminate_listener 55801
launchd_bootstrap_service "$LABEL" "$PLIST_TARGET"
if wait_for_launchd_http_service "$LABEL" 'http://127.0.0.1:55801/healthz' 30 2; then
  echo '{"status":"ok","service":"com.chauncey.mcn.official-group-bridge"}'
  exit 0
fi

echo "official group bridge launch agent installed but verification timed out" >&2
exit 1
