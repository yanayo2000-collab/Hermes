#!/bin/bash
set -euo pipefail
cd /Users/chauncey/work/mcn-ai-automation
export CRM_BASE_URL=http://47.236.9.71:8310/enterprise-admin
export CRM_USERNAME=Hermes
export CRM_PASSWORD=@Hermes123
export FEISHU_APP_ID=cli_a956dd7994b81ed0
export FEISHU_APP_SECRET=Bhmt1p9P8lSdGtr8hTEm5dWvOOF2j7K6
export FEISHU_DOMAIN=lark
export FEISHU_CONNECTION_MODE=websocket
export FEISHU_HOME_CHANNEL=oc_45e1a564b0106d0302fb5cb3e45b0a18
export FEISHU_ALLOWED_USERS=ou_f7423c1af89a8ae988a27964e1def9a5,1f1gff95
export REGISTRATION_GROUP_APPROVAL_EXECUTOR_KIND=webjs_bridge
export REGISTRATION_GROUP_APPROVAL_WEBJS_BASE_URL=http://127.0.0.1:8787
export REGISTRATION_GROUP_APPROVAL_WEBJS_TIMEOUT_SECONDS=35
export OFFICIAL_GROUP_APPROVAL_EXECUTOR_KIND=${OFFICIAL_GROUP_APPROVAL_EXECUTOR_KIND:-webhook}
export OFFICIAL_GROUP_APPROVAL_WEBHOOK_URL=${OFFICIAL_GROUP_APPROVAL_WEBHOOK_URL:-http://127.0.0.1:55801/official-group/approve}
export OFFICIAL_GROUP_APPROVAL_WEBHOOK_TOKEN=${OFFICIAL_GROUP_APPROVAL_WEBHOOK_TOKEN:-official-group-local-token}
export OFFICIAL_GROUP_TARGET_MAP=${OFFICIAL_GROUP_TARGET_MAP:-'{"registration_group_prefix:piso":"official-group-piso","registration_group_prefix:permata":"official-group-permata","registration_group_prefix:sampanye":"official-group-sampanye","registration_group_prefix:nova":"official-group-nova","dept_name:piso":"official-group-piso","dept_name:permata":"official-group-permata","dept_name:sampanye":"official-group-sampanye","dept_name:nova":"official-group-nova"}'}
export INGRESS_WORKER_POLL_INTERVAL=0.1
export WHATSAPP_PROFILE_DIR='Profile 25'
export WHATSAPP_REGISTRATION_GROUP_NAME='🇮🇩3️⃣7️⃣Grup Registrasi Resmi Linky 💎'
export PATH='/Users/chauncey/work/mcn-ai-automation/.venv/bin:/Users/chauncey/.local/bin:/usr/local/bin:/System/Cryptexes/App/usr/bin:/usr/bin:/bin:/usr/sbin:/sbin:/var/run/com.apple.security.cryptexd/codex.system/bootstrap/usr/local/bin:/var/run/com.apple.security.cryptexd/codex.system/bootstrap/usr/bin:/var/run/com.apple.security.cryptexd/codex.system/bootstrap/usr/appleinternal/bin:/opt/homebrew/bin:/Users/chauncey/Library/Python/3.9/bin:/Users/chauncey/.bun/bin:/Users/chauncey/.antigravity/antigravity/bin:/Users/chauncey/.local/bin:/Users/chauncey/.nvm/versions/node/v22.22.2/bin:/opt/local/bin:/opt/local/sbin LaunchInstanceID=C1642984-3AC2-415E-B70B-68903234EA9A'
export VIRTUAL_ENV=/Users/chauncey/work/mcn-ai-automation/.venv
export HOME=/Users/chauncey
export USER=chauncey
export TMPDIR=/var/folders/1m/92wdcs6d711302s0y3x81p9m0000gn/T/
export LC_CTYPE=UTF-8
export TERM=xterm-256color
export SHELL=/bin/zsh
export PWD=/Users/chauncey/work/mcn-ai-automation
export PYTHONUNBUFFERED=1
export HERMES_QUIET=1
export HERMES_MAX_ITERATIONS=90
export HERMES_INTERACTIVE=0
export ENABLE_CHROME_BIND_EXECUTOR=true
export BIND_CHROME_PROFILE_MAP='{"permata-profile":"Profile 5"}'
export CHROME_USER_DATA_ROOT='/Users/chauncey/Library/Application Support/Google/Chrome'
if lsof -tiTCP:8011 -sTCP:LISTEN >/dev/null 2>&1; then
  kill $(lsof -tiTCP:8011 -sTCP:LISTEN) || true
  for _ in $(seq 1 20); do
    if ! lsof -tiTCP:8011 -sTCP:LISTEN >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done
fi
exec /Users/chauncey/work/mcn-ai-automation/.venv/bin/python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8011
