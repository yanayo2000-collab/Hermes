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

if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
  echo "[backup] no changes to back up"
  exit 0
fi

git add -A

STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
MSG="backup: auto snapshot ${STAMP}"

if git commit -m "$MSG" >/dev/null 2>&1; then
  echo "[backup] committed: $MSG"
else
  echo "[backup] nothing to commit"
fi

git push origin "$(git branch --show-current)"
echo "[backup] pushed successfully"
