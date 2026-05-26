#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$ROOT_DIR/logs"
mkdir -p "$LOG_DIR" "$ROOT_DIR/data"

if [[ -f "$HOME/.hermes/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$HOME/.hermes/.env"
  set +a
fi

if [[ -f "$ROOT_DIR/data/production_ops_daemon.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/data/production_ops_daemon.env"
  set +a
fi

if [[ -f "$ROOT_DIR/data/internal_auth.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/data/internal_auth.env"
  set +a
fi

resolved_worker_base_url="${PRODUCTION_OPS_WORKER_BASE_URL-}"
resolved_registration_group="${PRODUCTION_OPS_REGISTRATION_GROUP-}"
resolved_fresh_probe_cmd="${PRODUCTION_OPS_FRESH_PROBE_CMD-}"
resolved_independent_truth_probe_cmd="${PRODUCTION_OPS_INDEPENDENT_TRUTH_PROBE_CMD-}"
notify_flag="--notify-enabled"
case "${PRODUCTION_OPS_NOTIFY_ENABLED:-true}" in
  0|false|False|FALSE|no|No|NO)
    notify_flag="--notify-disabled"
    ;;
esac

# Registration-group auto approval is archived by product decision.
# The daemon may still contain historical auto-approval code paths, but production
# startup must always run in monitor-only mode. Manual one-click approval remains available.
registration_auto_approval_flag="--registration-auto-approval-disabled"
if [[ -n "${PRODUCTION_OPS_REGISTRATION_AUTO_APPROVAL_ENABLED:-}" ]]; then
  echo "[production-ops] ignored PRODUCTION_OPS_REGISTRATION_AUTO_APPROVAL_ENABLED because registration auto approval is archived; running monitor-only" >&2
fi
if [[ -z "$resolved_fresh_probe_cmd" && -n "$resolved_registration_group" ]]; then
  resolved_fresh_probe_cmd="node $ROOT_DIR/scripts/fresh_webjs_group_state.js $(printf '%q' "$resolved_registration_group")"
fi

exec python3 "$ROOT_DIR/scripts/production_ops_daemon.py" \
  "$notify_flag" \
  "$registration_auto_approval_flag" \
  --api-base-url "${PRODUCTION_OPS_API_BASE_URL:-http://127.0.0.1:8011}" \
  --worker-base-url "$resolved_worker_base_url" \
  --registration-group "$resolved_registration_group" \
  --fresh-probe-cmd "$resolved_fresh_probe_cmd" \
  --independent-truth-probe-cmd "$resolved_independent_truth_probe_cmd" \
  --independent-truth-probe-interval-seconds "${PRODUCTION_OPS_INDEPENDENT_TRUTH_PROBE_INTERVAL_SECONDS:-1800}" \
  --worker-restart-cmd "${PRODUCTION_OPS_WORKER_RESTART_CMD:-$ROOT_DIR/scripts/restart_registration_group_webjs_worker.sh}" \
  --backend-restart-cmd "${PRODUCTION_OPS_BACKEND_RESTART_CMD:-$ROOT_DIR/scripts/ensure_registration_group_backend.sh}" \
  --state-path "${PRODUCTION_OPS_STATE_PATH:-$ROOT_DIR/data/production_ops_daemon_state.json}" \
  --status-path "${PRODUCTION_OPS_STATUS_PATH:-$ROOT_DIR/data/production_ops_daemon_status.json}" \
  --interval-seconds "${PRODUCTION_OPS_INTERVAL_SECONDS:-20}" \
  --worker-timeout-seconds "${PRODUCTION_OPS_WORKER_TIMEOUT_SECONDS:-90}" \
  --temp-cleanup-interval-seconds "${PRODUCTION_OPS_TEMP_CLEANUP_INTERVAL_SECONDS:-600}" \
  --temp-cleanup-min-age-hours "${PRODUCTION_OPS_TEMP_CLEANUP_MIN_AGE_HOURS:-1}" \
  --monitoring-session-id "${PRODUCTION_OPS_MONITORING_SESSION_ID:-}" \
  >> "$LOG_DIR/production_ops_daemon.log" 2>> "$LOG_DIR/production_ops_daemon.error.log"
