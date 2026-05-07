import json
import os
import subprocess
from pathlib import Path


def test_launchd_bootstrap_service_retries_until_service_is_registered(tmp_path):
    state_path = tmp_path / 'state.json'
    state_path.write_text(json.dumps({'bootstrap_calls': 0, 'registered': False}))

    fakebin = tmp_path / 'fakebin'
    fakebin.mkdir()

    (fakebin / 'id').write_text('#!/bin/sh\necho 501\n')
    os.chmod(fakebin / 'id', 0o755)

    (fakebin / 'sleep').write_text('#!/bin/sh\nexit 0\n')
    os.chmod(fakebin / 'sleep', 0o755)

    launchctl_script = f'''#!/usr/bin/env python3
import json
import sys
from pathlib import Path

state_path = Path({str(state_path)!r})
state = json.loads(state_path.read_text())
args = sys.argv[1:]
cmd = args[0]

if cmd == "bootout":
    pass
elif cmd == "bootstrap":
    state["bootstrap_calls"] += 1
    if state["bootstrap_calls"] >= 2:
        state["registered"] = True
elif cmd == "enable":
    pass
elif cmd == "kickstart":
    pass
elif cmd == "print":
    if state.get("registered"):
        sys.stdout.write("state = waiting\\n")
        state_path.write_text(json.dumps(state))
        raise SystemExit(0)
    raise SystemExit(1)
else:
    raise SystemExit(2)

state_path.write_text(json.dumps(state))
'''
    (fakebin / 'launchctl').write_text(launchctl_script)
    os.chmod(fakebin / 'launchctl', 0o755)

    helper_path = Path('scripts/lib/launchd_service.sh').resolve()
    command = f'''
set -euo pipefail
export PATH={str(fakebin)!r}:$PATH
source {str(helper_path)!r}
launchd_bootstrap_service "com.example.demo" "/tmp/demo.plist"
python3 - <<'PY'
import json
from pathlib import Path
state = json.loads(Path({str(state_path)!r}).read_text())
print(json.dumps(state))
PY
'''
    result = subprocess.run(['bash', '-lc', command], capture_output=True, text=True, check=True)
    state = json.loads(result.stdout.strip())

    assert state['registered'] is True
    assert state['bootstrap_calls'] == 2


def test_launchd_bootstrap_service_returns_nonzero_when_service_never_registers(tmp_path):
    state_path = tmp_path / 'state.json'
    state_path.write_text(json.dumps({'bootstrap_calls': 0, 'registered': False}))

    fakebin = tmp_path / 'fakebin'
    fakebin.mkdir()

    (fakebin / 'id').write_text('#!/bin/sh\necho 501\n')
    os.chmod(fakebin / 'id', 0o755)

    (fakebin / 'sleep').write_text('#!/bin/sh\nexit 0\n')
    os.chmod(fakebin / 'sleep', 0o755)

    launchctl_script = f'''#!/usr/bin/env python3
import json
import sys
from pathlib import Path

state_path = Path({str(state_path)!r})
state = json.loads(state_path.read_text())
args = sys.argv[1:]
cmd = args[0]

if cmd == "bootstrap":
    state["bootstrap_calls"] += 1
elif cmd in {"bootout", "enable", "kickstart"}:
    pass
elif cmd == "print":
    raise SystemExit(1)
else:
    raise SystemExit(2)

state_path.write_text(json.dumps(state))
'''
    (fakebin / 'launchctl').write_text(launchctl_script)
    os.chmod(fakebin / 'launchctl', 0o755)

    helper_path = Path('scripts/lib/launchd_service.sh').resolve()
    command = f'''
set -euo pipefail
export PATH={str(fakebin)!r}:$PATH
source {str(helper_path)!r}
launchd_bootstrap_service "com.example.demo" "/tmp/demo.plist" 3 0
'''
    result = subprocess.run(['bash', '-lc', command], capture_output=True, text=True)
    state = json.loads(state_path.read_text())

    assert result.returncode != 0
    assert state['registered'] is False
    assert state['bootstrap_calls'] == 3
