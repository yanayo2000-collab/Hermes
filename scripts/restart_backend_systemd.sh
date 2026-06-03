#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${MCN_BACKEND_SYSTEMD_SERVICE:-mcn-backend.service}"
PORT="${MCN_BACKEND_PORT:-8011}"
HOST="${MCN_BACKEND_HOST:-127.0.0.1}"
HEALTH_URL="${MCN_BACKEND_HEALTH_URL:-http://${HOST}:${PORT}/health}"
STOP_WAIT_SECONDS="${MCN_BACKEND_STOP_WAIT_SECONDS:-15}"
START_WAIT_SECONDS="${MCN_BACKEND_START_WAIT_SECONDS:-45}"
TERM_WAIT_SECONDS="${MCN_BACKEND_TERM_WAIT_SECONDS:-5}"
KILL_WAIT_SECONDS="${MCN_BACKEND_KILL_WAIT_SECONDS:-3}"

listener_pids() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true
    return
  fi
  if command -v ss >/dev/null 2>&1; then
    ss -lntp 2>/dev/null | awk -v port=":${PORT}" '
      index($4, port) {
        while (match($0, /pid=[0-9]+/)) {
          print substr($0, RSTART + 4, RLENGTH - 4)
          $0 = substr($0, RSTART + RLENGTH)
        }
      }
    ' | sort -u || true
    return
  fi
}

wait_for_port_release() {
  local deadline=$((SECONDS + STOP_WAIT_SECONDS))
  while (( SECONDS < deadline )); do
    if [[ -z "$(listener_pids | tr -d '[:space:]')" ]]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

kill_stale_listeners() {
  mapfile -t pids < <(listener_pids | awk 'NF' | sort -u)
  if (( ${#pids[@]} == 0 )); then
    return 0
  fi

  kill -TERM "${pids[@]}" 2>/dev/null || true
  sleep "${TERM_WAIT_SECONDS}"

  mapfile -t remaining < <(listener_pids | awk 'NF' | sort -u)
  if (( ${#remaining[@]} > 0 )); then
    kill -KILL "${remaining[@]}" 2>/dev/null || true
    sleep "${KILL_WAIT_SECONDS}"
  fi

  mapfile -t final_pids < <(listener_pids | awk 'NF' | sort -u)
  if (( ${#final_pids[@]} > 0 )); then
    echo "stale listeners still occupy port ${PORT}: ${final_pids[*]}" >&2
    return 1
  fi
  return 0
}

wait_for_health() {
  local deadline=$((SECONDS + START_WAIT_SECONDS))
  while (( SECONDS < deadline )); do
    if curl --noproxy '*' -fsS --max-time 3 "${HEALTH_URL}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

systemctl stop "${SERVICE_NAME}" >/dev/null 2>&1 || true
systemctl kill --kill-who=all "${SERVICE_NAME}" >/dev/null 2>&1 || true
wait_for_port_release || true
kill_stale_listeners
systemctl reset-failed "${SERVICE_NAME}" >/dev/null 2>&1 || true
systemctl start "${SERVICE_NAME}"

if ! wait_for_health; then
  echo "${SERVICE_NAME} failed health check after restart: ${HEALTH_URL}" >&2
  systemctl --no-pager --full status "${SERVICE_NAME}" || true
  exit 1
fi

echo "{\"status\":\"ok\",\"service\":\"${SERVICE_NAME}\",\"port\":${PORT},\"health_url\":\"${HEALTH_URL}\"}"
