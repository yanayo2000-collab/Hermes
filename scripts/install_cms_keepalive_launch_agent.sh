#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/launchd_service.sh"
PLIST_SOURCE="$ROOT_DIR/scripts/launchd/com.chauncey.mcn.cms-keepalive.plist"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.chauncey.mcn.cms-keepalive.plist"
LABEL="com.chauncey.mcn.cms-keepalive"
ENV_PATH="$ROOT_DIR/data/cms_keepalive.env"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT_DIR/logs" "$ROOT_DIR/data"
cp "$PLIST_SOURCE" "$PLIST_TARGET"
chmod 644 "$PLIST_TARGET"
chmod +x "$ROOT_DIR/scripts/cms_keepalive.py" "$ROOT_DIR/scripts/run_cms_keepalive.sh"

if [[ ! -f "$ENV_PATH" ]]; then
  cat > "$ENV_PATH" <<'EOF'
# Linky CMS keepalive accounts. Do not commit this file.
# Single account mode:
# LINKE_CMS_ACCOUNT_NAME=Permata
# LINKE_CMS_GUILD_ID=413
# LINKE_CMS_GUILD_SID=25400979
# LINKE_CMS_AUTHORIZATION=replace_with_cms_authorization_jwt
#
# Multi-account mode:
# LINKE_CMS_ACCOUNTS_JSON=[{"name":"Permata","guild_id":"413","guild_sid":"25400979","authorization":"replace_with_cms_authorization_jwt"}]
CMS_KEEPALIVE_TIMEOUT_SECONDS=30
EOF
  chmod 600 "$ENV_PATH"
fi

launchd_bootstrap_service "$LABEL" "$PLIST_TARGET"
echo '{"status":"ok","service":"com.chauncey.mcn.cms-keepalive","interval_seconds":21600}'
