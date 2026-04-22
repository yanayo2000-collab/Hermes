#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import errno
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.whatsapp_live_monitor import (
    load_monitor_state,
    summarize_monitor_result,
    update_first_seen_at,
)
from playwright.async_api import async_playwright


def _safe_rmtree(path: Path, *, attempts: int = 5, delay_seconds: float = 0.2) -> None:
    for attempt in range(max(1, attempts)):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno not in {errno.ENOTEMPTY, errno.EBUSY, errno.EPERM} or attempt == attempts - 1:
                raise
            time.sleep(delay_seconds)


def _allocate_run_temp_user_data_dir(base_dir: Path) -> Path:
    base_dir = Path(base_dir).expanduser()
    base_dir.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f'{base_dir.name}-', dir=str(base_dir.parent)))


async def inspect_group(page, *, list_item_index: int) -> dict:
    await page.locator('[data-testid="chat-list"] [data-testid="list-item-%d"]' % list_item_index).click()
    await page.wait_for_timeout(2500)
    await page.locator('[data-testid="conversation-header"]').click()
    await page.wait_for_timeout(3000)
    body = await page.locator('body').inner_text()
    title = ''
    try:
        title = (await page.locator('[data-testid="conversation-info-header-chat-title"]').inner_text()).strip()
    except Exception:
        pass
    subtitle = ''
    try:
        subtitle = (await page.locator('[data-testid="chat-subtitle"]').inner_text()).strip()
    except Exception:
        pass
    return {
        'title': title,
        'subtitle': subtitle,
        'body_text': body,
    }


async def run(args) -> int:
    src_root = Path(args.chrome_user_data_root).expanduser()
    dst_root = _allocate_run_temp_user_data_dir(Path(args.temp_user_data_dir))
    state_path = Path(args.state_path).expanduser()
    state = load_monitor_state(state_path)
    try:
        for name in ['Local State', args.profile_dir]:
            src = src_root / name
            dst = dst_root / name
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                str(dst_root),
                channel='chrome',
                headless=args.headless,
                args=[f'--profile-directory={args.profile_dir}'],
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto('https://web.whatsapp.com/', wait_until='domcontentloaded', timeout=60000)
                await page.wait_for_timeout(args.initial_wait_ms)
                await page.get_by_role('tab', name='群组').click(timeout=5000)
                await page.wait_for_timeout(1500)

                reg = await inspect_group(page, list_item_index=args.registration_list_item_index)
                reg_now = datetime.now(timezone.utc)
                reg_probe = summarize_monitor_result(args.registration_group_name, reg['body_text'], first_seen_at=None, now=reg_now)
                reg_first_seen = update_first_seen_at(
                    state,
                    group_name=args.registration_group_name,
                    pending_count=reg_probe['pending']['pending_count'],
                    now=reg_now,
                    state_path=state_path,
                )
                reg_result = summarize_monitor_result(
                    args.registration_group_name,
                    reg['body_text'],
                    first_seen_at=reg_first_seen,
                    now=reg_now,
                )
                reg_result['title'] = reg['title']
                reg_result['subtitle'] = reg['subtitle']
                reg_result['has_pending_request_row'] = '请求加入。点击以审核。' in reg['body_text']

                await page.get_by_role('tab', name='群组').click(timeout=5000)
                await page.wait_for_timeout(1200)
                off = await inspect_group(page, list_item_index=args.official_list_item_index)
                off_now = datetime.now(timezone.utc)
                off_probe = summarize_monitor_result(args.official_group_name, off['body_text'], first_seen_at=None, now=off_now)
                off_first_seen = update_first_seen_at(
                    state,
                    group_name=args.official_group_name,
                    pending_count=off_probe['pending']['pending_count'],
                    now=off_now,
                    state_path=state_path,
                )
                off_result = summarize_monitor_result(
                    args.official_group_name,
                    off['body_text'],
                    first_seen_at=off_first_seen,
                    now=off_now,
                )
                off_result['title'] = off['title']
                off_result['subtitle'] = off['subtitle']
                off_result['has_pending_request_row'] = '请求加入。点击以审核。' in off['body_text']

                output = {
                    'checked_at': datetime.now(timezone.utc).isoformat(),
                    'profile_dir': args.profile_dir,
                    'state_path': str(state_path),
                    'temp_user_data_dir': str(dst_root),
                    'registration_group': reg_result,
                    'official_group': off_result,
                }
                print(json.dumps(output, ensure_ascii=False, indent=2))
            finally:
                await context.close()
    finally:
        _safe_rmtree(dst_root)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='Live WhatsApp production-test monitor v1.')
    parser.add_argument('--chrome-user-data-root', default='~/Library/Application Support/Google/Chrome')
    parser.add_argument('--profile-dir', default='Profile 25')
    parser.add_argument('--temp-user-data-dir', default='/tmp/chrome-whatsapp-live-monitor')
    parser.add_argument('--state-path', default='./data/whatsapp_live_monitor_state.json')
    parser.add_argument('--registration-list-item-index', type=int, default=0)
    parser.add_argument('--official-list-item-index', type=int, default=1)
    parser.add_argument('--registration-group-name', default='8️⃣5️⃣')
    parser.add_argument('--official-group-name', default='8️⃣8️⃣')
    parser.add_argument('--initial-wait-ms', type=int, default=8000)
    parser.add_argument('--headless', action='store_true', default=True)
    return asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    raise SystemExit(main())
