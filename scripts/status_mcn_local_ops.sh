#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
memory_json="$(bash "$ROOT_DIR/scripts/status_memory_pressure_relief.sh")"
gateway_json="$(bash "$ROOT_DIR/scripts/status_default_hermes_gateway.sh")"
python3 - <<'PY' "$memory_json" "$gateway_json"
import json, sys
memory_json, gateway_json = sys.argv[1:3]
out = {
    'service': 'mcn_local_ops_status',
    'memory_pressure_relief': json.loads(memory_json),
    'default_hermes_gateway': json.loads(gateway_json),
}
print(json.dumps(out, ensure_ascii=False))
PY
