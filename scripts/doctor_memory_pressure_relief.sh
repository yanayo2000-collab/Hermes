#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATUS_SCRIPT="$ROOT_DIR/scripts/status_memory_pressure_relief.sh"
status_json="$(bash "$STATUS_SCRIPT")"
top_output="$(set +o pipefail; top -l 1 -o cpu | head -n 18)"
vm_stat_output="$(vm_stat)"
heavy_ps_output="$(set +o pipefail; ps -Ao pid,ppid,%cpu,%mem,etime,command | sort -k3 -nr | head -n 12)"

python3 - <<'PY' "$status_json" "$top_output" "$vm_stat_output" "$heavy_ps_output"
import json, re, sys
status_json, top_output, vm_stat_output, heavy_ps_output = sys.argv[1:5]
status = json.loads(status_json)

def extract(pattern, text, cast=float):
    m = re.search(pattern, text, flags=re.MULTILINE)
    if not m:
        return None
    return cast(m.group(1))

cpu_user = extract(r'^CPU usage:\s*([0-9.]+)% user', top_output)
cpu_sys = extract(r'^CPU usage:\s*[0-9.]+% user,\s*([0-9.]+)% sys', top_output)
cpu_idle = extract(r'^CPU usage:\s*[0-9.]+% user,\s*[0-9.]+% sys,\s*([0-9.]+)% idle', top_output)
load_avg = re.search(r'^Load Avg:\s*([^\n]+)$', top_output, flags=re.MULTILINE)
free_pages = extract(r'^Pages free:\s+(\d+)\.', vm_stat_output, int)
spec_pages = extract(r'^Pages speculative:\s+(\d+)\.', vm_stat_output, int)
compressor_pages = extract(r'^Pages occupied by compressor:\s+(\d+)\.', vm_stat_output, int)
stored_pages = extract(r'^Pages stored in compressor:\s+(\d+)\.', vm_stat_output, int)
page_size = extract(r'page size of\s+(\d+)\s+bytes', vm_stat_output, int) or 4096

def pages_to_mb(pages):
    if pages is None:
        return None
    return round((pages * page_size) / (1024 * 1024), 2)

out = {
    'service': 'memory_pressure_relief_doctor',
    'launchd': status.get('launchd'),
    'recent_run': status.get('recent_run'),
    'current_system': {
        'cpu': {
            'user_percent': cpu_user,
            'sys_percent': cpu_sys,
            'idle_percent': cpu_idle,
            'load_avg': load_avg.group(1) if load_avg else None,
        },
        'memory': {
            'page_size_bytes': page_size,
            'free_mb': pages_to_mb((free_pages or 0) + (spec_pages or 0)),
            'compressor_mb': pages_to_mb(compressor_pages),
            'compressed_payload_mb': pages_to_mb(stored_pages),
        },
        'top_cpu_processes': heavy_ps_output.splitlines(),
    },
}
print(json.dumps(out, ensure_ascii=False))
PY
