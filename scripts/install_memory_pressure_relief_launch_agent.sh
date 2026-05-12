#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/launchd_service.sh"
PLIST_SOURCE="$ROOT_DIR/scripts/launchd/com.chauncey.mcn.memory-pressure-relief.plist"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.chauncey.mcn.memory-pressure-relief.plist"
LABEL="com.chauncey.mcn.memory-pressure-relief"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT_DIR/logs"
cp "$PLIST_SOURCE" "$PLIST_TARGET"
chmod 644 "$PLIST_TARGET"
chmod +x "$ROOT_DIR/scripts/run_memory_pressure_relief.sh" "$ROOT_DIR/scripts/memory_pressure_relief.py"

launchd_bootstrap_service "$LABEL" "$PLIST_TARGET"
STATE="$(launchd_state "$LABEL")"
if [[ -n "$STATE" ]]; then
  echo '{"status":"ok","service":"com.chauncey.mcn.memory-pressure-relief"}'
  exit 0
fi

echo "memory pressure relief launch agent installed but launchctl state was empty" >&2
exit 1
