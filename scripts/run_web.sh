#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
. .venv/bin/activate
exec gunicorn app.main:create_app --factory -c deploy/gunicorn_conf.py
