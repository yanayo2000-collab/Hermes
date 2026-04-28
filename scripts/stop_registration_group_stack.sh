#!/usr/bin/env bash
set -euo pipefail

if lsof -tiTCP:8011 -sTCP:LISTEN >/dev/null 2>&1; then
  kill $(lsof -tiTCP:8011 -sTCP:LISTEN) || true
fi
if lsof -tiTCP:8787 -sTCP:LISTEN >/dev/null 2>&1; then
  kill $(lsof -tiTCP:8787 -sTCP:LISTEN) || true
fi
sleep 1

echo '{"stopped":true}'
