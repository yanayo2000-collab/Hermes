#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="/Users/chauncey/work/mcn-ai-automation"
cd "$ROOT_DIR"

REG_GROUP='🇮🇩3️⃣7️⃣Grup Registrasi Resmi Linky 💎'
STAMP="$(date '+%Y%m%d-%H%M%S')"
OUT_DIR="$ROOT_DIR/logs"
mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/resume_prod_ops_${STAMP}.json"

PENDING_COUNT="$((python3 - <<'PY'
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8011/api/ops/registration-group-approval-executor-group-state?registration_group=%F0%9F%87%AE%F0%9F%87%A93%EF%B8%8F%E2%83%A37%EF%B8%8F%E2%83%A3Grup%20Registrasi%20Resmi%20Linky%20%F0%9F%92%8E', timeout=90) as r:
    body=json.loads(r.read().decode())
print(max(int(body.get('pending_count') or 0), 0))
PY
) 2>/dev/null || echo 0)"

if [[ "$PENDING_COUNT" -gt 0 ]]; then
  ./.venv/bin/python ./scripts/run_registration_group_formal_approval.py \
    --api-base-url http://127.0.0.1:8011 \
    --registration-group "$REG_GROUP" \
    --fresh-probe-cmd ". .venv/bin/activate && node scripts/fresh_webjs_group_state.js '$REG_GROUP'" \
    --restart-cmd ./scripts/restart_registration_group_webjs_worker.sh \
    --auto-recover \
    --backend-restart-cmd ./scripts/ensure_registration_group_backend.sh \
    --restart-wait-seconds 8 \
    --area Indonesia \
    --remark 'scheduled_resume_1230_release_then_resume_monitoring' \
    --approved-count "$PENDING_COUNT" \
    --poll-interval-seconds 0.1 \
    --poll-timeout-seconds 120 \
    --decided-by Hermes \
    --decided-by-name 'Song Yuqi' \
    > "$OUT_FILE"
else
  printf '{"skipped":true,"reason":"no_pending_request","checked_at":"%s"}\n' "$(date -Iseconds)" > "$OUT_FILE"
fi

bash ./scripts/install_production_ops_daemon_launch_agent.sh
