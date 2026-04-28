#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.chauncey.mcn.production-ops-daemon"
STATUS_FILE="$ROOT_DIR/data/production_ops_daemon_status.json"
GUI_DOMAIN="gui/$(id -u)"

launchctl print "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1 || true
if [[ -f "$STATUS_FILE" ]]; then
  cat "$STATUS_FILE"
else
  echo '{"status":"missing_status_file"}'
fi
