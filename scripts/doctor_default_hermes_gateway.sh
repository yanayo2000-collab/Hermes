#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
status_json="$(python3 "$ROOT_DIR/scripts/hermes_gateway_health.py" status)"
top_output="$(set +o pipefail; top -l 1 -o cpu | head -n 18)"
vm_stat_output="$(vm_stat)"
heavy_ps_output="$(set +o pipefail; ps -Ao pid,ppid,%cpu,%mem,etime,command | sort -k3 -nr | head -n 12)"
python3 - <<'PY' "$status_json" "$top_output" "$vm_stat_output" "$heavy_ps_output"
import json, re, sys
status_json, top_output, vm_stat_output, heavy_ps_output = sys.argv[1:5]
status = json.loads(status_json)
def extract(pattern, text, cast=float):
    m = re.search(pattern, text, flags=re.MULTILINE)
    return cast(m.group(1)) if m else None
page_size = extract(r'page size of\s+(\d+)\s+bytes', vm_stat_output, int) or 4096
free_pages = extract(r'^Pages free:\s+(\d+)\.', vm_stat_output, int) or 0
spec_pages = extract(r'^Pages speculative:\s+(\d+)\.', vm_stat_output, int) or 0
compressor_pages = extract(r'^Pages occupied by compressor:\s+(\d+)\.', vm_stat_output, int) or 0
stored_pages = extract(r'^Pages stored in compressor:\s+(\d+)\.', vm_stat_output, int) or 0
load_avg = re.search(r'^Load Avg:\s*([^\n]+)$', top_output, flags=re.MULTILINE)
out = {
  'service': 'default_hermes_gateway_doctor',
  'gateway': status,
  'current_system': {
    'cpu': {
      'user_percent': extract(r'^CPU usage:\s*([0-9.]+)% user', top_output),
      'sys_percent': extract(r'^CPU usage:\s*[0-9.]+% user,\s*([0-9.]+)% sys', top_output),
      'idle_percent': extract(r'^CPU usage:\s*[0-9.]+% user,\s*[0-9.]+% sys,\s*([0-9.]+)% idle', top_output),
      'load_avg': load_avg.group(1) if load_avg else None,
    },
    'memory': {
      'page_size_bytes': page_size,
      'free_mb': round(((free_pages + spec_pages) * page_size) / (1024 * 1024), 2),
      'compressor_mb': round((compressor_pages * page_size) / (1024 * 1024), 2),
      'compressed_payload_mb': round((stored_pages * page_size) / (1024 * 1024), 2),
    },
    'top_cpu_processes': heavy_ps_output.splitlines(),
  },
}
print(json.dumps(out, ensure_ascii=False))
PY
