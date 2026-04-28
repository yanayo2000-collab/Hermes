#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$ROOT_DIR/logs"
BACKEND_LOG="$LOG_DIR/registration_group_backend.log"
PLIST_SOURCE="$ROOT_DIR/scripts/launchd/com.chauncey.mcn.registration-group-backend.plist"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.chauncey.mcn.registration-group-backend.plist"
LABEL="com.chauncey.mcn.registration-group-backend"

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"

if curl -sf http://127.0.0.1:8011/health >/dev/null 2>&1; then
  echo '{"status":"ok","action":"already_healthy"}'
  exit 0
fi

if [[ -f "$PLIST_TARGET" ]]; then
  launchctl bootstrap "gui/$(id -u)" "$PLIST_TARGET" >/dev/null 2>&1 || true
  launchctl enable "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
else
  if [[ -f "$PLIST_SOURCE" ]]; then
    cp "$PLIST_SOURCE" "$PLIST_TARGET"
    launchctl bootstrap "gui/$(id -u)" "$PLIST_TARGET" >/dev/null 2>&1 || true
    launchctl enable "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  else
    nohup "$ROOT_DIR/scripts/restart_intake_backend_with_bind.sh" </dev/null >> "$BACKEND_LOG" 2>&1 &
  fi
fi

for _ in {1..30}; do
  sleep 2
  if curl -sf http://127.0.0.1:8011/health >/dev/null 2>&1; then
    echo '{"status":"ok","action":"recovered"}'
    exit 0
  fi
done

echo "backend failed to become healthy; check $BACKEND_LOG" >&2
exit 1
