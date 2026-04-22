#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import re
import shutil
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

SRC_ROOT = Path('~/Library/Application Support/Google/Chrome').expanduser()
PROFILE = 'Profile 25'
REG_GROUP_INDEX = 0
REG_GROUP_NAME = '8️⃣5️⃣'
API_URL = 'http://127.0.0.1:8011/api/registration-groups/approval-batches'


def normalize_phone(text: str) -> str:
    digits = re.sub(r'\D+', '', text or '')
    if not digits:
        return ''
    if str(text).strip().startswith('+'):
        return '+' + digits
    return digits


def extract_pending_count(text: str) -> int:
    m = re.search(r'待处理请求\s*(\d+)', text or '')
    return int(m.group(1)) if m else 0


def extract_member_count(text: str) -> int | None:
    m = re.search(r'群组\s*[·•]\s*(\d+)位成员', text or '')
    return int(m.group(1)) if m else None


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


async def enter_groups_tab(page):
    await page.goto('https://web.whatsapp.com/', wait_until='domcontentloaded', timeout=60000)
    await page.wait_for_timeout(12000)
    groups_locators = [
        page.get_by_role('tab', name='群组'),
        page.get_by_text('群组', exact=True),
        page.locator('[data-testid="chat-list-search"]'),
    ]
    for locator in groups_locators[:2]:
        try:
            if await locator.count():
                await locator.first.click(timeout=10000)
                await page.wait_for_timeout(2000)
                return
        except Exception:
            pass
    # fallback: if group list filter already active or not needed, continue
    await page.wait_for_timeout(2000)


async def open_reg_group(context):
    page = context.pages[0] if context.pages else await context.new_page()
    await enter_groups_tab(page)
    await page.locator(f'[data-testid="chat-list"] [data-testid="list-item-{REG_GROUP_INDEX}"]').click(timeout=10000)
    await page.wait_for_timeout(2500)
    await page.locator('[data-testid="conversation-header"]').click(timeout=10000)
    await page.wait_for_timeout(3000)
    return page


async def inspect_and_click() -> dict[str, Any]:
    p, context = await launch_with_copy('/tmp/chrome-whatsapp-force-approve-before')
    try:
        page = await open_reg_group(context)
        body_before = await page.locator('body').inner_text()
        pending_count = extract_pending_count(body_before)
        result: dict[str, Any] = {
            'pending_count': pending_count,
            'member_count': extract_member_count(body_before),
            'body_excerpt': body_before[-3000:],
        }
        if pending_count <= 0:
            result['clicked_approve'] = False
            return result
        review_locator = page.get_by_text(f'审核{pending_count}请求加入')
        if await review_locator.count():
            await review_locator.first.click(timeout=10000)
        else:
            await page.locator('[data-testid="conversation-subheader"]').click(timeout=10000)
        await page.wait_for_timeout(2500)
        row = page.locator('[data-testid="row"]').first
        result['row_count'] = await page.locator('[data-testid="row"]').count()
        result['target_text'] = (await row.inner_text()).strip()
        result['target_phone_raw'] = (await row.locator('[data-testid="name"] span').first.inner_text()).strip()
        result['target_phone'] = normalize_phone(result['target_phone_raw'])
        pushname = page.locator('[data-testid="pushname"]')
        result['target_name'] = (await pushname.first.inner_text()).strip() if await pushname.count() else ''
        approve = row.locator('[aria-label="批准"]')
        await approve.click(timeout=10000, force=True)
        await page.wait_for_timeout(3000)
        body_after_click = await page.locator('body').inner_text()
        result['clicked_approve'] = True
        result['body_after_click_excerpt'] = body_after_click[-2000:]
        return result
    finally:
        await context.close()
        await p.stop()


async def verify_after() -> dict[str, Any]:
    p, context = await launch_with_copy('/tmp/chrome-whatsapp-force-approve-after')
    try:
        page = await open_reg_group(context)
        body = await page.locator('body').inner_text()
        phones = sorted({normalize_phone(x) for x in re.findall(r'\+\d[\d\s-]{6,}', body)})
        return {
            'pending_count': extract_pending_count(body),
            'member_count': extract_member_count(body),
            'all_phones_normalized': phones,
            'body_excerpt': body[-3500:],
        }
    finally:
        await context.close()
        await p.stop()


def write_crm_batch(source_ad: str, approved_at: str) -> dict[str, Any]:
    payload = {
        'registration_group': REG_GROUP_NAME,
        'approved_count': 1,
        'approved_by': 'system:whatsapp_live_monitor',
        'approved_by_name': 'Hermes WhatsApp Auto Approver',
        'source_platform': 'whatsapp',
        'source_campaign': 'registration_group_live_prod_test_force_approve',
        'source_adset': REG_GROUP_NAME,
        'source_ad': source_ad,
        'approved_at': approved_at,
        'area': 'Indonesia',
        'remark': 'forced approval by operator instruction; verified by queue delta and member list confirmation',
    }
    req = urllib.request.Request(API_URL, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode('utf-8')
    return {'request': payload, 'response': json.loads(body)}


async def main() -> None:
    started_at = datetime.now(timezone.utc)
    before = await inspect_and_click()
    result: dict[str, Any] = {
        'started_at': started_at.isoformat(),
        'registration_group': REG_GROUP_NAME,
        'before': before,
    }
    if not before.get('clicked_approve'):
        result['status'] = 'no_pending_request'
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    await asyncio.sleep(8)
    after = await verify_after()
    finished_at = datetime.now(timezone.utc)
    queue_delta = after.get('pending_count', 0) < before.get('pending_count', 0)
    member_confirmed = bool(before.get('target_phone') and before['target_phone'] in after.get('all_phones_normalized', []))
    approved_success = bool(queue_delta and member_confirmed)
    result['after'] = after
    result['verification'] = {
        'queue_delta': queue_delta,
        'member_confirmed': member_confirmed,
        'approved_success': approved_success,
    }
    result['finished_at'] = finished_at.isoformat()
    result['elapsed_seconds'] = round((finished_at - started_at).total_seconds(), 2)
    if approved_success:
        crm = write_crm_batch(
            source_ad=f"{before.get('target_name','').strip()} {before.get('target_phone_raw','').strip()}".strip(),
            approved_at=finished_at.isoformat(),
        )
        result['crm_batch'] = crm
        result['status'] = 'approved_and_recorded'
    else:
        result['status'] = 'approval_not_verified'
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
