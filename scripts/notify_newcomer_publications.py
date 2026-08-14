#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.newcomer_publication import dispatch_pending_newcomer_events, load_secret


def main() -> int:
    parser = argparse.ArgumentParser(description='Deliver MCN newcomer outbox events.')
    parser.add_argument('--db-path', default=os.getenv('DB_PATH', '/data/mcn-data/automation.db'))
    parser.add_argument(
        '--url',
        default=os.getenv(
            'NEWCOMER_WEBHOOK_URL',
            'http://127.0.0.1:3000/api/internal/mcn/newcomers/events',
        ),
    )
    parser.add_argument(
        '--secret-file',
        default=os.getenv(
            'NEWCOMER_WEBHOOK_SECRET_FILE',
            '/etc/mcn-ai-automation/newcomer-webhook.secret',
        ),
    )
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()

    secret = load_secret(args.secret_file)
    conn = sqlite3.connect(args.db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=5000')
    try:
        result = dispatch_pending_newcomer_events(
            conn,
            url=args.url,
            secret=secret,
            limit=args.limit,
        )
    finally:
        conn.close()
    print(
        'newcomer_events '
        f"processed={result['processed_count']} "
        f"delivered={result['delivered_count']} failed={result['failed_count']}"
    )
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
