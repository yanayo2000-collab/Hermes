#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.chauncey.mcn.cms-keepalive"
STATE_PATH="$ROOT_DIR/data/cms_keepalive_status.json"

launchctl print "gui/$(id -u)/$LABEL" >/tmp/mcn_cms_keepalive_launchd_status.$$ 2>/tmp/mcn_cms_keepalive_launchd_error.$$ || true
python3 - "$STATE_PATH" /tmp/mcn_cms_keepalive_launchd_status.$$ /tmp/mcn_cms_keepalive_launchd_error.$$ <<'PY'
import json, sys
from pathlib import Path
state_path=Path(sys.argv[1])
launchd_status=Path(sys.argv[2]).read_text(errors='ignore')
launchd_error=Path(sys.argv[3]).read_text(errors='ignore')
state=None
if state_path.exists():
    try:
        state=json.loads(state_path.read_text())
    except Exception as exc:
        state={'error': f'invalid state json: {exc}'}
print(json.dumps({
    'service': 'com.chauncey.mcn.cms-keepalive',
    'launchd_loaded': 'state = running' in launchd_status or 'last exit code' in launchd_status,
    'launchd_error': launchd_error.strip()[:200] or None,
    'state': state,
}, ensure_ascii=False, indent=2))
PY
rm -f /tmp/mcn_cms_keepalive_launchd_status.$$ /tmp/mcn_cms_keepalive_launchd_error.$$
