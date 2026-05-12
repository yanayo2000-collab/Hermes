#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_PATH="$ROOT_DIR/logs/memory_pressure_relief.log"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

mkdir -p "$ROOT_DIR/logs"

FREE_MB_THRESHOLD="${MEMORY_RELIEF_FREE_MB_THRESHOLD:-512}"
COMPRESSOR_MB_THRESHOLD="${MEMORY_RELIEF_COMPRESSOR_MB_THRESHOLD:-2048}"
MIN_AGE_HOURS="${MEMORY_RELIEF_MIN_AGE_HOURS:-0.5}"

exec "$PYTHON_BIN" "$ROOT_DIR/scripts/memory_pressure_relief.py" \
  --apply \
  --free-mb-threshold "$FREE_MB_THRESHOLD" \
  --compressor-mb-threshold "$COMPRESSOR_MB_THRESHOLD" \
  --min-age-hours "$MIN_AGE_HOURS" \
  >> "$LOG_PATH" 2>&1
