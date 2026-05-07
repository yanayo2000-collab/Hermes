#!/usr/bin/env bash

launchd_gui_domain() {
  echo "gui/$(id -u)"
}

launchd_state() {
  local label="$1"
  launchctl print "$(launchd_gui_domain)/$label" 2>/dev/null | grep -E 'state = ' | head -n 1 | sed 's/^[[:space:]]*state = //' || true
}

launchd_last_exit_code() {
  local label="$1"
  launchctl print "$(launchd_gui_domain)/$label" 2>/dev/null | grep -E 'last exit code = ' | head -n 1 | sed 's/^[[:space:]]*last exit code = //' || true
}

launchd_bootstrap_service() {
  local label="$1"
  local plist_target="$2"
  local gui_domain
  local state
  local attempts="${3:-5}"
  local sleep_seconds="${4:-1}"
  gui_domain="$(launchd_gui_domain)"
  launchctl bootout "$gui_domain/$label" >/dev/null 2>&1 || true
  for _ in $(seq 1 "$attempts"); do
    launchctl bootstrap "$gui_domain" "$plist_target" >/dev/null 2>&1 || true
    launchctl enable "$gui_domain/$label" >/dev/null 2>&1 || true
    launchctl kickstart -k "$gui_domain/$label" >/dev/null 2>&1 || true
    state="$(launchd_state "$label")"
    if [[ -n "$state" ]]; then
      return 0
    fi
    sleep "$sleep_seconds"
  done
  return 1
}

launchd_uninstall_service() {
  local label="$1"
  local plist_target="$2"
  local gui_domain
  gui_domain="$(launchd_gui_domain)"
  launchctl bootout "$gui_domain/$label" >/dev/null 2>&1 || true
  launchctl disable "$gui_domain/$label" >/dev/null 2>&1 || true
  rm -f "$plist_target"
}

wait_for_launchd_http_service() {
  local label="$1"
  local url="$2"
  local attempts="${3:-30}"
  local sleep_seconds="${4:-2}"
  local state
  for _ in $(seq 1 "$attempts"); do
    sleep "$sleep_seconds"
    state="$(launchd_state "$label")"
    if [[ "$state" == "running" ]] && curl -sf "$url" >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

wait_for_launchd_process_service() {
  local label="$1"
  local pattern="$2"
  local attempts="${3:-30}"
  local sleep_seconds="${4:-2}"
  local state
  for _ in $(seq 1 "$attempts"); do
    sleep "$sleep_seconds"
    state="$(launchd_state "$label")"
    if [[ "$state" == "running" ]] && pgrep -f "$pattern" >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}
