#!/usr/bin/env bash
set -euo pipefail

LOCK_PATH="${MCN_DEPLOY_LOCK_PATH:-/var/lock/mcn-deploy.lock}"
META_PATH="${MCN_DEPLOY_LOCK_META_PATH:-${LOCK_PATH}.meta}"
WAIT_SECONDS="${MCN_DEPLOY_LOCK_WAIT_SECONDS:-0}"
RESOURCE_WAIT_SECONDS="${MCN_DEPLOY_RESOURCE_LOCK_WAIT_SECONDS:-90}"
EXTRA_LOCK_PATHS="${MCN_DEPLOY_EXTRA_LOCK_PATHS:-}"
LOCK_FD=9
RESOURCE_LOCK_FDS=()

usage() {
  cat >&2 <<'EOF'
Usage: scripts/mcn_deploy_lock.sh <command> [args...]
       scripts/mcn_deploy_lock.sh --check

Runs a deployment or restart command under the global MCN deployment lock.
The default is non-blocking. Set MCN_DEPLOY_LOCK_WAIT_SECONDS=N to wait.
EOF
}

lock_is_held() {
  [[ "${MCN_DEPLOY_LOCK_ACTIVE:-}" == "1" ]] || return 1
  [[ "${MCN_DEPLOY_LOCK_FD:-}" =~ ^[0-9]+$ ]] || return 1
  [[ -n "${MCN_DEPLOY_LOCK_PATH:-}" ]] || return 1
  if [[ "$(uname -s)" != "Linux" ]]; then
    return 0
  fi
  command -v flock >/dev/null 2>&1 || return 1
  local actual expected actual_inode expected_inode
  actual="$(readlink -f "/proc/$$/fd/${MCN_DEPLOY_LOCK_FD}" 2>/dev/null || true)"
  expected="$(readlink -f "${MCN_DEPLOY_LOCK_PATH}" 2>/dev/null || true)"
  [[ -n "$actual" && "$actual" == "$expected" ]] || return 1
  actual_inode="$(stat -Lc '%d:%i' "/proc/$$/fd/${MCN_DEPLOY_LOCK_FD}" 2>/dev/null || true)"
  expected_inode="$(stat -Lc '%d:%i' "${MCN_DEPLOY_LOCK_PATH}" 2>/dev/null || true)"
  [[ -n "$actual_inode" && "$actual_inode" == "$expected_inode" ]] || return 1
  flock -n "${MCN_DEPLOY_LOCK_FD}"
}

if [[ "${1:-}" == "--check" ]]; then
  lock_is_held
  exit $?
fi

if (( $# == 0 )); then
  usage
  exit 64
fi
if [[ ! "$WAIT_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "[mcn-deploy-lock] MCN_DEPLOY_LOCK_WAIT_SECONDS must be a non-negative integer" >&2
  exit 64
fi
if [[ ! "$RESOURCE_WAIT_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "[mcn-deploy-lock] MCN_DEPLOY_RESOURCE_LOCK_WAIT_SECONDS must be a non-negative integer" >&2
  exit 64
fi
if ! command -v flock >/dev/null 2>&1; then
  echo "[mcn-deploy-lock] flock is required; refusing an unlocked deployment" >&2
  exit 69
fi

umask 077
mkdir -p "$(dirname "$LOCK_PATH")"
exec 9>"$LOCK_PATH"
chmod 600 "$LOCK_PATH"

if (( WAIT_SECONDS > 0 )); then
  if ! flock -w "$WAIT_SECONDS" "$LOCK_FD"; then
    echo "[mcn-deploy-lock] another deployment/restart is running; timeout=${WAIT_SECONDS}s" >&2
    [[ -f "$META_PATH" ]] && sed 's/^/  /' "$META_PATH" >&2 || true
    exit 75
  fi
else
  if ! flock -n "$LOCK_FD"; then
    echo "[mcn-deploy-lock] another deployment/restart is running; refusing concurrency" >&2
    [[ -f "$META_PATH" ]] && sed 's/^/  /' "$META_PATH" >&2 || true
    exit 75
  fi
fi

acquire_resource_locks() {
  [[ -n "$EXTRA_LOCK_PATHS" ]] || return 0
  local path fd
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    mkdir -p "$(dirname "$path")"
    exec {fd}>"$path"
    chmod 600 "$path"
    if (( RESOURCE_WAIT_SECONDS > 0 )); then
      if ! flock -w "$RESOURCE_WAIT_SECONDS" "$fd"; then
        echo "[mcn-deploy-lock] conflicting production resource is busy: ${path}" >&2
        return 75
      fi
    elif ! flock -n "$fd"; then
      echo "[mcn-deploy-lock] conflicting production resource is busy: ${path}" >&2
      return 75
    fi
    RESOURCE_LOCK_FDS+=("$fd")
  done < <(
    printf '%s' "$EXTRA_LOCK_PATHS" |
      tr ':' '\n' |
      sed '/^[[:space:]]*$/d' |
      LC_ALL=C sort -u
  )
}

check_queue_failed_units() {
  [[ "${MCN_DEPLOY_QUEUE_ACTIVE:-}" == "1" ]] || return 0
  command -v systemctl >/dev/null 2>&1 || {
    echo "[mcn-deploy-lock] systemctl is required for queue failed-unit admission" >&2
    return 69
  }
  local failed_text allowed_dependencies
  allowed_dependencies=":${MCN_DEPLOY_DEPENDENCY_UNITS:-}:"
  failed_text="$({
    systemctl --failed --no-legend --plain 2>/dev/null |
      awk -v restart_policy="${MCN_DEPLOY_RESTART_POLICY:-backend}" \
          -v dependencies="$allowed_dependencies" '
        NF && $1 != "mcn-deploy-queue.service" {
          if (restart_policy != "none" || index(dependencies, ":" $1 ":") > 0) print $1
        }
      ' |
      LC_ALL=C sort -u
  } || true)"
  if [[ -n "$failed_text" ]]; then
    echo "[mcn-deploy-lock] global failed-unit gate closed under deploy lock: ${failed_text//$'\n'/ }" >&2
    return 75
  fi
}

set +e
acquire_resource_locks
resource_lock_status=$?
set -e
if (( resource_lock_status != 0 )); then
  exit "$resource_lock_status"
fi

set +e
check_queue_failed_units
failed_unit_status=$?
set -e
if (( failed_unit_status != 0 )); then
  exit "$failed_unit_status"
fi

cleanup() {
  rm -f "${META_PATH}.$$"
  rm -f "$META_PATH"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

{
  printf 'started_at=%s\n' "$(date -Is)"
  printf 'host=%s\n' "$(hostname 2>/dev/null || echo unknown)"
  printf 'user=%s\n' "${USER:-unknown}"
  printf 'pid=%s\n' "$$"
  printf 'cwd=%s\n' "$(pwd)"
  printf 'command_executable=%q\n' "$1"
  printf 'command_argument_count=%s\n' "$(( $# - 1 ))"
  printf 'extra_lock_paths=%s\n' "$EXTRA_LOCK_PATHS"
} > "${META_PATH}.$$"
chmod 600 "${META_PATH}.$$"
mv -f "${META_PATH}.$$" "$META_PATH"

export MCN_DEPLOY_LOCK_ACTIVE=1
export MCN_DEPLOY_LOCK_FD="$LOCK_FD"
export MCN_DEPLOY_LOCK_PATH="$LOCK_PATH"
"$@"
