#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/launchd_service.sh"
LABEL="com.chauncey.mcn.memory-pressure-relief"
LOG_PATH="$ROOT_DIR/logs/memory_pressure_relief.log"
LAUNCHD_LOG_PATH="$ROOT_DIR/logs/memory_pressure_relief.launchd.log"
LAUNCHCTL_PRINT="$(launchctl print "$(launchd_gui_domain)/$LABEL" 2>/dev/null || true)"
launch_state="$(launchd_state "$LABEL")"
last_exit="$(launchd_last_exit_code "$LABEL")"

python3 - <<'PY' "$LABEL" "$launch_state" "$last_exit" "$LOG_PATH" "$LAUNCHD_LOG_PATH" "$LAUNCHCTL_PRINT"
import json, re, sys
from pathlib import Path

label, launch_state, last_exit, log_path, launchd_log_path, launchctl_print = sys.argv[1:7]


def parse_last_json_object(text: str):
    decoder = json.JSONDecoder()
    idx = 0
    last = None
    n = len(text)
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        last = obj
        idx = end
    return last


def extract_field(pattern: str, text: str):
    match = re.search(pattern, text, flags=re.MULTILINE)
    return match.group(1) if match else None

log_text = Path(log_path).read_text(encoding='utf-8') if Path(log_path).exists() else ''
recent = parse_last_json_object(log_text) if log_text else None
runs = extract_field(r'^\s*runs =\s*(\d+)', launchctl_print)
pid = extract_field(r'^\s*pid =\s*(\d+)', launchctl_print)

out = {
    'service': 'memory_pressure_relief',
    'launchd': {
        'label': label,
        'state': launch_state or None,
        'last_exit_code': last_exit or None,
        'runs': int(runs) if runs else None,
        'pid': int(pid) if pid else None,
    },
    'logs': {
        'log_path': log_path,
        'launchd_log_path': launchd_log_path,
        'log_exists': Path(log_path).exists(),
        'launchd_log_exists': Path(launchd_log_path).exists(),
    },
    'recent_run': recent,
}
print(json.dumps(out, ensure_ascii=False))
PY
