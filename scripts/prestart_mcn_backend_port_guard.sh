#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${MCN_BACKEND_SYSTEMD_SERVICE:-mcn-backend.service}"
PORT="${MCN_BACKEND_PORT:-8011}"
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

mapfile -t pids < <(listener_pids | awk 'NF' | sort -u)
if (( ${#pids[@]} == 0 )); then
  exit 0
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
  echo "${SERVICE_NAME} prestart port guard failed on ${PORT}: ${final_pids[*]}" >&2
  exit 1
fi
