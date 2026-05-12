#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_PATH="$ROOT_DIR/logs/default_hermes_gateway_self_heal.log"
mkdir -p "$ROOT_DIR/logs" "$ROOT_DIR/data"
exec python3 "$ROOT_DIR/scripts/hermes_gateway_health.py" self-heal --apply >> "$LOG_PATH" 2>&1
