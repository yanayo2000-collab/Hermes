#!/bin/bash

terminate_listener() {
  local port="$1"
  local graceful_attempts="${2:-40}"
  local force_attempts="${3:-20}"
  local sleep_seconds="${4:-0.25}"
  local pids=""

  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -z "$pids" ]]; then
    return 0
  fi

  kill $pids >/dev/null 2>&1 || true
  for _ in $(seq 1 "$graceful_attempts"); do
    pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -z "$pids" ]]; then
      return 0
    fi
    sleep "$sleep_seconds"
  done

  kill -9 $pids >/dev/null 2>&1 || true
  for _ in $(seq 1 "$force_attempts"); do
    pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -z "$pids" ]]; then
      return 0
    fi
    sleep "$sleep_seconds"
  done

  echo "failed to clear listener on port $port" >&2
  return 1
}
