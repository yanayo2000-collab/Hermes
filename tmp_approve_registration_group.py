#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

SRC_ROOT = Path('~/Library/Application Support/Google/Chrome').expanduser()
PROFILE = 'Profile 25'
REG_GROUP_INDEX = 0
REG_GROUP_NAME = '8️⃣5️⃣'


def normalize_phone(text: str) -> str:
    digits = re.sub(r'\D+', '', text or '')
    if not digits:
        return ''
    if text.strip().startswith('+'):
        return '+' + digits
    return digits


def extract_pending_count(text: str) -> int:
    m = re.search(r'待处理请求\s*(\d+)', text or '')
    return int(m.group(1)) if m else 0


def extract_member_count(text: str) -> int | None:
    m = re.search(r'群组\s*[·•]\s*(\d+)位成员', text or '')
    return int(m.group(1)) if m else None


async def open_reg_group(context) -> dict[str, Any]:
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto('https://web.whatsapp.com/', wait_until='domcontentloaded', timeout=60000)
    await page.wait_for_timeout(8000)
    await page.get_by_role('tab', name='群组').click(timeout=5000)
    await page.wait_for_timeout(1500)
    await page.locator(f'[data-testid="chat-list"] [data-testid="list-item-{REG_GROUP_INDEX}"]').click()
    await page.wait_for_timeout(2500)
    await page.locator('[data-testid="conversation-header"]').click()
    await page.wait_for_timeout(3000)
    body = await page.locator('body').inner_text()
    return {'page': page, 'body': body}


async def launch_with_copy(temp_dir: str):
    dst_root = Path(temp_dir).expanduser()
    if dst_root.exists():
        shutil.rmtree(dst_root)
    dst_root.mkdir(parents=True)
    for name in ['Local State', PROFILE]:
        src = SRC_ROOT / name
        dst = dst_root / name
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)
    p = await async_playwright().start()
    context = await p.chromium.launch_persistent_context(
        str(dst_root),
        channel='chrome',
        headless=True,
        args=[f'--profile-directory={PROFILE}'],
    )
    return p, context


async def inspect_before() -> dict[str, Any]:
    p, context = await launch_with_copy('/tmp/chrome-whatsapp-approve-before')
    try:
        opened = await open_reg_group(context)
        page = opened['page']
        body = opened['body']
        before = {
            'pending_count': extract_pending_count(body),
            'member_count': extract_member_count(body),
            'body_excerpt': body[:4000],
        }
        if before['pending_count'] > 0:
            await page.get_by_text(f'审核{before["pending_count"]}请求加入').click(timeout=5000)
            await page.wait_for_timeout(2500)
            row = page.locator('[data-testid="row"]').first
            before['row_count'] = await page.locator('[data-testid="row"]').count()
            before['target_text'] = (await row.inner_text()).strip()
            before['target_phone_raw'] = (await row.locator('[data-testid="name"] span').first.inner_text()).strip()
            before['target_phone'] = normalize_phone(before['target_phone_raw'])
            pushname = page.locator('[data-testid="pushname"]')
            before['target_name'] = (await pushname.first.inner_text()).strip() if await pushname.count() else ''
            await row.locator('[aria-label="批准"]').click(timeout=5000)
            await page.wait_for_timeout(6000)
            before['clicked_approve'] = True
        else:
            before['clicked_approve'] = False
        return before
    finally:
        await context.close()
        await p.stop()


async def inspect_after() -> dict[str, Any]:
    p, context = await launch_with_copy('/tmp/chrome-whatsapp-approve-after')
    try:
        opened = await open_reg_group(context)
        body = opened['body']
        after = {
            'pending_count': extract_pending_count(body),
            'member_count': extract_member_count(body),
            'body_excerpt': body[:5000],
            'all_phones_normalized': sorted({normalize_phone(x) for x in re.findall(r'\+\d[\d\s-]{6,}', body)}),
        }
        return after
    finally:
        await context.close()
        await p.stop()


async def main() -> None:
    before = await inspect_before()
    result: dict[str, Any] = {'registration_group': REG_GROUP_NAME, 'before': before}
    if before.get('pending_count', 0) <= 0:
        result['action'] = 'no_pending'
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    after = await inspect_after()
    result['after'] = after
    target_phone = before.get('target_phone') or ''
    queue_delta = after.get('pending_count', 0) < before.get('pending_count', 0)
    member_confirmed = bool(target_phone and target_phone in after.get('all_phones_normalized', []))
    result['verification'] = {
        'target_phone': target_phone,
        'target_name': before.get('target_name'),
        'queue_delta': queue_delta,
        'member_confirmed': member_confirmed,
        'approved_success': bool(queue_delta and member_confirmed),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
