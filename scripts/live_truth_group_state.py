#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.registration_group_executor import LiveWarmWhatsAppRegistrationGroupApprovalExecutor


def _env(name: str, default: str = '') -> str:
    return str(os.getenv(name) or default).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description='Independent live truth probe for WhatsApp registration-group pending approvals')
    parser.add_argument('--group-name', required=True)
    parser.add_argument('--chrome-user-data-root', default=_env('TRUTH_PROBE_CHROME_USER_DATA_ROOT') or _env('WHATSAPP_CHROME_USER_DATA_ROOT') or _env('CHROME_USER_DATA_ROOT') or str(Path('~/Library/Application Support/Google/Chrome').expanduser()))
    parser.add_argument('--profile-dir', default=_env('TRUTH_PROBE_WHATSAPP_PROFILE_DIR') or _env('WHATSAPP_PROFILE_DIR', 'Profile 25'))
    parser.add_argument('--temp-user-data-dir', default=_env('TRUTH_PROBE_TEMP_USER_DATA_DIR') or _env('WHATSAPP_REGISTRATION_APPROVAL_TEMP_DIR', '/tmp/chrome-whatsapp-registration-group-approval'))
    parser.add_argument('--registration-list-item-index', type=int, default=int(_env('TRUTH_PROBE_REGISTRATION_LIST_ITEM_INDEX') or _env('WHATSAPP_REGISTRATION_LIST_ITEM_INDEX', '0') or 0))
    parser.add_argument('--initial-wait-ms', type=int, default=int(_env('WHATSAPP_INITIAL_WAIT_MS', '500') or 500))
    parser.add_argument('--navigation-wait-ms', type=int, default=int(_env('WHATSAPP_NAVIGATION_WAIT_MS', '120') or 120))
    parser.add_argument('--post-click-wait-ms', type=int, default=int(_env('WHATSAPP_POST_CLICK_WAIT_MS', '80') or 80))
    parser.add_argument('--verify-timeout-ms', type=int, default=int(_env('WHATSAPP_VERIFY_TIMEOUT_MS', '1200') or 1200))
    parser.add_argument('--verify-poll-ms', type=int, default=int(_env('WHATSAPP_VERIFY_POLL_MS', '80') or 80))
    parser.add_argument('--strict-reload-verify', action='store_true', default=_env('WHATSAPP_STRICT_RELOAD_VERIFY', '').lower() in {'1', 'true', 'yes', 'on'})
    args = parser.parse_args()

    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(
        chrome_user_data_root=args.chrome_user_data_root,
        profile_dir=args.profile_dir,
        registration_list_item_index=args.registration_list_item_index,
        registration_group_name=args.group_name,
        temp_user_data_dir=args.temp_user_data_dir,
        initial_wait_ms=args.initial_wait_ms,
        navigation_wait_ms=args.navigation_wait_ms,
        post_click_wait_ms=args.post_click_wait_ms,
        verify_timeout_ms=args.verify_timeout_ms,
        verify_poll_ms=args.verify_poll_ms,
        strict_reload_verify=args.strict_reload_verify,
    )
    payload = executor.group_state(args.group_name)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
