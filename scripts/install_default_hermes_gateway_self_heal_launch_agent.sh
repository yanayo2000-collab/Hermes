#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/launchd_service.sh"
PLIST_SOURCE="$ROOT_DIR/scripts/launchd/com.chauncey.default-hermes-gateway-self-heal.plist"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.chauncey.default-hermes-gateway-self-heal.plist"
LABEL="com.chauncey.default-hermes-gateway-self-heal"
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT_DIR/logs" "$ROOT_DIR/data"
cp "$PLIST_SOURCE" "$PLIST_TARGET"
chmod 644 "$PLIST_TARGET"
chmod +x "$ROOT_DIR/scripts/run_default_hermes_gateway_self_heal.sh" "$ROOT_DIR/scripts/hermes_gateway_health.py" "$ROOT_DIR/scripts/status_default_hermes_gateway.sh" "$ROOT_DIR/scripts/doctor_default_hermes_gateway.sh"
launchd_bootstrap_service "$LABEL" "$PLIST_TARGET"
echo '{"status":"ok","service":"com.chauncey.default-hermes-gateway-self-heal"}'
