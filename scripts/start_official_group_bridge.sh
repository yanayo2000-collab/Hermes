#!/bin/bash
set -euo pipefail
cd /Users/chauncey/work/mcn-ai-automation
export PATH='/Users/chauncey/work/mcn-ai-automation/.venv/bin:/Users/chauncey/.local/bin:/usr/local/bin:/System/Cryptexes/App/usr/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/opt/local/bin:/opt/local/sbin'
export VIRTUAL_ENV=/Users/chauncey/work/mcn-ai-automation/.venv
export HOME=/Users/chauncey
export USER=chauncey
export PYTHONUNBUFFERED=1
export OFFICIAL_GROUP_BRIDGE_MODE=${OFFICIAL_GROUP_BRIDGE_MODE:-manual_queue}
export OFFICIAL_GROUP_BRIDGE_TOKEN=${OFFICIAL_GROUP_BRIDGE_TOKEN:-official-group-local-token}
if lsof -tiTCP:55801 -sTCP:LISTEN >/dev/null 2>&1; then kill $(lsof -tiTCP:55801 -sTCP:LISTEN) || true; sleep 1; fi
exec /Users/chauncey/work/mcn-ai-automation/.venv/bin/python -m uvicorn app.official_webhook_bridge_app:create_app --factory --host 127.0.0.1 --port 55801
