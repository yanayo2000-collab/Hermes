#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
memory_json="$(bash "$ROOT_DIR/scripts/doctor_memory_pressure_relief.sh")"
gateway_json="$(bash "$ROOT_DIR/scripts/doctor_default_hermes_gateway.sh")"
python3 - <<'PY' "$memory_json" "$gateway_json"
import json, sys
memory_json, gateway_json = sys.argv[1:3]
memory = json.loads(memory_json)
gateway = json.loads(gateway_json)
out = {
    'service': 'mcn_local_ops_doctor',
    'memory_pressure_relief': memory,
    'default_hermes_gateway': gateway,
    'summary': {
        'memory_triggered': (((memory.get('recent_run') or {}).get('pressure') or {}).get('triggered')),
        'gateway_running': (((gateway.get('gateway') or {}).get('launchd') or {}).get('state') == 'running'),
        'gateway_recent_consecutive_failures': ((((gateway.get('gateway') or {}).get('recent_consecutive_failures')) or {}).get('count')),
        'system_idle_percent': (((gateway.get('current_system') or {}).get('cpu') or {}).get('idle_percent')),
        'system_free_mb': (((gateway.get('current_system') or {}).get('memory') or {}).get('free_mb')),
        'system_compressor_mb': (((gateway.get('current_system') or {}).get('memory') or {}).get('compressor_mb')),
    },
}
print(json.dumps(out, ensure_ascii=False))
PY
