#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$ROOT_DIR/logs"
BACKEND_LOG="$LOG_DIR/registration_group_backend.log"
mkdir -p "$LOG_DIR"

cd "$ROOT_DIR"
./scripts/restart_registration_group_webjs_worker.sh >/dev/null

if ! "$ROOT_DIR/scripts/ensure_registration_group_backend.sh" >/dev/null; then
  echo "backend start timed out; check $BACKEND_LOG" >&2
  exit 1
fi

echo '{"backend":"ok","worker":"ok"}'
