#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[backup] not a git repository: $REPO_DIR" >&2
  exit 1
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "[backup] missing git remote 'origin'" >&2
  echo "[backup] run: git remote add origin <github-repo-url>" >&2
  exit 2
fi

if ! git config user.name >/dev/null 2>&1; then
  echo "[backup] missing git user.name" >&2
  exit 3
fi

if ! git config user.email >/dev/null 2>&1; then
  echo "[backup] missing git user.email" >&2
  exit 4
fi

# Back up source/config only. Do not stage runtime state, browser profiles,
# logs, local databases, temp probes, node_modules, or generated caches.
CORE_PATHS=(
  app
  scripts
  tests
  docs
  deploy
  sql
  webjs-approval-worker/src
  webjs-approval-worker/test
  webjs-approval-worker/package.json
  webjs-approval-worker/package-lock.json
  webjs-approval-worker/README.md
  requirements.txt
  pytest.ini
  README.md
  .gitignore
  .env.example
  .github
  ci.yml
  render.webhook.yaml
)

# Stage only the curated source set. Missing optional paths are ignored.
for path in "${CORE_PATHS[@]}"; do
  if [ -e "$path" ] || git ls-files --error-unmatch "$path" >/dev/null 2>&1; then
    git add -A -- "$path"
  fi
done

# Defensive unstage: these paths must never be added by the backup job.
git restore --staged -- \
  data logs tmp .wwebjs_cache .venv .venv-live-truth node_modules \
  webjs-approval-worker/node_modules \
  webjs-approval-worker/logs \
  webjs-approval-worker/tmp \
  webjs-approval-worker/.wwebjs_auth \
  webjs-approval-worker/.wwebjs_auth_accounts \
  webjs-approval-worker/.wwebjs_auth_dedicated \
  webjs-approval-worker/.wwebjs_cache \
  '*.log' \
  2>/dev/null || true

# If old runtime artifacts were already tracked before .gitignore was fixed,
# stage their removal from Git index without deleting the local live files.
TRACKED_IGNORED="$(git ls-files -ci --exclude-standard)"
if [ -n "$TRACKED_IGNORED" ]; then
  echo "$TRACKED_IGNORED" | xargs git rm --cached --ignore-unmatch -r -- >/dev/null
fi

BRANCH="$(git branch --show-current)"
if git diff --cached --quiet; then
  if git rev-parse --verify "origin/${BRANCH}" >/dev/null 2>&1; then
    AHEAD_COUNT="$(git rev-list --count "origin/${BRANCH}..HEAD")"
  else
    AHEAD_COUNT=1
  fi
  if [ "${AHEAD_COUNT}" -gt 0 ]; then
    echo "[backup] no new core source changes; pushing ${AHEAD_COUNT} existing local commit(s)"
    git push origin "${BRANCH}"
    echo "[backup] pushed successfully"
    exit 0
  fi
  echo "[backup] no core source changes to back up"
  exit 0
fi

STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
MSG="backup: core source snapshot ${STAMP}"

if git commit -m "$MSG" >/dev/null 2>&1; then
  echo "[backup] committed: $MSG"
else
  echo "[backup] nothing to commit"
fi

git push origin "$(git branch --show-current)"
echo "[backup] pushed successfully"
