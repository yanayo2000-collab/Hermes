from __future__ import annotations

import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any, Dict, Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


PHONE_PATTERN = re.compile(r'\+\d[\d\s\-*]{6,}')
PENDING_COUNT_PATTERN = re.compile(r'待处理请求\s*(\d+)')
REQUEST_JOIN_ROW_PATTERN = re.compile(r'请求加入。点击以审核。')
MEMBER_COUNT_PATTERN = re.compile(r'群组\s*[·•]\s*(\d+)位成员')


class LiveWarmWhatsAppRegistrationGroupApprovalExecutor:
    def __init__(
        self,
        *,
        chrome_user_data_root: Optional[str] = None,
        profile_dir: str = 'Profile 25',
        registration_list_item_index: int = 0,
        registration_group_name: str = '8️⃣5️⃣',
        temp_user_data_dir: str = '/tmp/chrome-whatsapp-registration-group-approval',
        chrome_channel: str = 'chrome',
        initial_wait_ms: int = 800,
        navigation_wait_ms: int = 350,
        post_click_wait_ms: int = 250,
        verify_timeout_ms: int = 2200,
        verify_poll_ms: int = 150,
        strict_reload_verify: bool = False,
    ) -> None:
        self.chrome_user_data_root = str(Path(chrome_user_data_root or '~/Library/Application Support/Google/Chrome').expanduser())
        self.profile_dir = profile_dir
        self.registration_list_item_index = int(registration_list_item_index)
        self.registration_group_name = registration_group_name
        self.temp_user_data_dir = str(Path(temp_user_data_dir).expanduser())
        self.chrome_channel = chrome_channel
        self.initial_wait_ms = max(0, int(initial_wait_ms or 0))
        self.navigation_wait_ms = max(0, int(navigation_wait_ms or 0))
        self.post_click_wait_ms = max(0, int(post_click_wait_ms or 0))
        self.verify_timeout_ms = max(300, int(verify_timeout_ms or 300))
        self.verify_poll_ms = max(50, int(verify_poll_ms or 50))
        self.strict_reload_verify = bool(strict_reload_verify)
        self._playwright = None
        self._context = None
        self._page = None
        self._warm = False
        self._last_error = None
        self._last_started_at = None
        self._last_action_at = None
        self._group_info_ready = False
        self._approval_lock = threading.Lock()

    def warmup(self) -> Dict[str, Any]:
        with self._approval_lock:
            try:
                self._ensure_browser()
                return self.health()
            except Exception as exc:
                self._reset_browser(f'warmup_error:{exc}')
                return self.health()

    def health(self) -> Dict[str, Any]:
        return {
            'configured': True,
            'status': 'warm' if self._warm and self._context is not None else 'idle',
            'provider': 'whatsapp_web_live_warm',
            'profile_dir': self.profile_dir,
            'group_name': self.registration_group_name,
            'temp_user_data_dir': self.temp_user_data_dir,
            'schema_version': 'registration-group-live-warm-v1',
            'supports': ['approve', 'strict_queue_and_member_verify', 'crm_batch_writeback_ready'],
            'timing_profile': {
                'initial_wait_ms': self.initial_wait_ms,
                'navigation_wait_ms': self.navigation_wait_ms,
                'post_click_wait_ms': self.post_click_wait_ms,
                'verify_timeout_ms': self.verify_timeout_ms,
                'verify_poll_ms': self.verify_poll_ms,
                'strict_reload_verify': self.strict_reload_verify,
            },
            'last_error': self._last_error,
            'last_started_at': self._last_started_at,
            'last_action_at': self._last_action_at,
        }

    def _clone_profile_once(self) -> None:
        src_root = Path(self.chrome_user_data_root)
        dst_root = Path(self.temp_user_data_dir)
        if not dst_root.exists():
            dst_root.mkdir(parents=True)
        for name in ['Local State', self.profile_dir]:
            src = src_root / name
            dst = dst_root / name
            if dst.exists():
                continue
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)

    def _close_browser(self) -> None:
        if self._page is not None:
            try:
                self._page.close()
            except Exception:
                pass
        self._page = None
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        self._context = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = None
        self._group_info_ready = False
        self._warm = False

    def _reset_browser(self, reason: str) -> None:
        self._last_error = reason
        self._close_browser()
        try:
            shutil.rmtree(self.temp_user_data_dir)
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def _ensure_browser(self) -> None:
        if self._context is not None and self._page is not None:
            return
        try:
            self._clone_profile_once()
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                self.temp_user_data_dir,
                channel=self.chrome_channel,
                headless=True,
                args=[f'--profile-directory={self.profile_dir}'],
            )
        except Exception as exc:
            if 'ProcessSingleton' not in str(exc):
                raise
            self._reset_browser(f'stale_profile_dir:{exc}')
            self._clone_profile_once()
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                self.temp_user_data_dir,
                channel=self.chrome_channel,
                headless=True,
                args=[f'--profile-directory={self.profile_dir}'],
            )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.goto('https://web.whatsapp.com/', wait_until='domcontentloaded', timeout=60000)
        self._page.wait_for_timeout(max(self.initial_wait_ms, 200))
        self._last_error = None
        self._last_started_at = datetime.now(timezone.utc).isoformat()
        self._warm = True

    def _enter_groups_tab(self) -> None:
        assert self._page is not None
        locators = [
            self._page.get_by_role('tab', name='群组'),
            self._page.get_by_text('群组', exact=True),
        ]
        deadline = time.perf_counter() + 3.0
        while True:
            for locator in locators:
                try:
                    if locator.count():
                        locator.first.click(timeout=3000)
                        self._page.wait_for_timeout(max(self.navigation_wait_ms, 100))
                        return
                except Exception:
                    continue
            if time.perf_counter() >= deadline:
                return
            try:
                self._page.wait_for_timeout(120)
            except Exception:
                return

    def _page_ready_for_approval(self) -> bool:
        assert self._page is not None
        quick_checks = [
            self._page.get_by_text('群组信息', exact=True),
            self._page.get_by_text('待处理请求', exact=True),
            self._page.get_by_text('请求加入。点击以审核。'),
        ]
        for locator in quick_checks:
            try:
                if locator.count():
                    return True
            except Exception:
                continue
        return False

    def _open_group_info(self) -> None:
        assert self._page is not None
        if self._group_info_ready and self._page_ready_for_approval():
            return
        self._enter_groups_tab()
        self._page.locator(f'[data-testid="chat-list"] [data-testid="list-item-{self.registration_list_item_index}"]').click(timeout=10000)
        self._page.wait_for_timeout(max(self.navigation_wait_ms, 100))
        self._page.locator('[data-testid="conversation-header"]').click(timeout=10000)
        self._page.wait_for_timeout(max(self.navigation_wait_ms, 100))
        self._group_info_ready = self._page_ready_for_approval()

    def _normalize_phone(self, text: str) -> str:
        digits = re.sub(r'\D+', '', str(text or ''))
        if not digits:
            return ''
        if str(text).strip().startswith('+'):
            return '+' + digits
        return digits

    def _extract_pending_count(self, text: str) -> int:
        body = str(text or '')
        m = PENDING_COUNT_PATTERN.search(body)
        if m:
            return int(m.group(1))
        return len(REQUEST_JOIN_ROW_PATTERN.findall(body))

    def _extract_member_count(self, text: str) -> Optional[int]:
        m = MEMBER_COUNT_PATTERN.search(str(text or ''))
        return int(m.group(1)) if m else None

    def _extract_all_phones(self, text: str) -> list[str]:
        values = []
        for phone in PHONE_PATTERN.findall(str(text or '')):
            normalized = self._normalize_phone(phone)
            if normalized and normalized not in values:
                values.append(normalized)
        return values

    def _snapshot_group_state(self) -> Dict[str, Any]:
        assert self._page is not None
        body = self._page.locator('body').inner_text()
        phones = self._extract_all_phones(body)
        pending_after = self._extract_pending_count(body)
        return {
            'pending_count': pending_after,
            'member_count': self._extract_member_count(body),
            'all_phones_normalized': phones,
            'body_excerpt': body[-2500:],
        }

    def _same_session_verify(self, *, target_phone: str, pending_before: int) -> Dict[str, Any]:
        assert self._page is not None
        if self.strict_reload_verify:
            self._page.reload(wait_until='domcontentloaded', timeout=60000)
            self._page.wait_for_timeout(max(self.initial_wait_ms, 200))
            self._open_group_info()
        deadline = time.perf_counter() + (self.verify_timeout_ms / 1000.0)
        latest = self._snapshot_group_state()
        while True:
            latest['queue_delta'] = latest['pending_count'] < pending_before
            latest['member_confirmed'] = bool(target_phone and target_phone in latest['all_phones_normalized'])
            if latest['queue_delta'] and latest['member_confirmed']:
                return latest
            if time.perf_counter() >= deadline:
                return latest
            self._page.wait_for_timeout(self.verify_poll_ms)
            latest = self._snapshot_group_state()

    def _open_pending_review(self, pending_before: int) -> None:
        assert self._page is not None
        review_text = f'审核{pending_before}请求加入'
        review = self._page.get_by_text(review_text)
        try:
            review.first.click(timeout=2000)
            self._page.wait_for_timeout(max(self.navigation_wait_ms, 500))
            return
        except Exception:
            pass
        try:
            approve_buttons = self._page.locator('[aria-label="批准"]')
            if approve_buttons.count():
                return
        except Exception:
            pass
        try:
            subheader = self._page.locator('[data-testid="conversation-subheader"]')
            if subheader.count():
                subheader.click(timeout=2000)
                self._page.wait_for_timeout(max(self.navigation_wait_ms, 500))
        except Exception:
            return

    def _wait_for_review_row(self):
        assert self._page is not None
        row = self._page.locator('[data-testid="row"]').first
        deadline = time.perf_counter() + 1.2
        while True:
            try:
                if self._page.locator('[data-testid="row"]').count():
                    return row
            except Exception:
                pass
            if time.perf_counter() >= deadline:
                return row
            self._page.wait_for_timeout(120)

    def _click_approve_action(self, row) -> None:
        assert self._page is not None
        try:
            row.locator('[aria-label="批准"]').click(timeout=1200, force=True)
            return
        except Exception:
            pass
        approve_buttons = self._page.locator('[aria-label="批准"]')
        if approve_buttons.count():
            approve_buttons.first.click(timeout=1200, force=True)
            return
        row.click(timeout=1200, force=True)
        approve_buttons = self._page.locator('[aria-label="批准"]')
        if approve_buttons.count():
            approve_buttons.first.click(timeout=1200, force=True)
            return
        raise PlaywrightTimeoutError('approve action unavailable after review row opened')

    def approve(self, context: Dict[str, Any]) -> Dict[str, Any]:
        started = time.perf_counter()
        with self._approval_lock:
            try:
                self._ensure_browser()
                self._open_group_info()
                assert self._page is not None
                body_before = self._page.locator('body').inner_text()
                pending_before = self._extract_pending_count(body_before)
                member_before = self._extract_member_count(body_before)
                self._group_info_ready = True
                if pending_before <= 0:
                    finished_at = datetime.now(timezone.utc).isoformat()
                    return {
                        'status': 'failed',
                        'verified': False,
                        'result_code': 'no_pending_request',
                        'result_reason': 'no pending request in registration group',
                        'finished_at': finished_at,
                        'elapsed_seconds': round(time.perf_counter() - started, 2),
                        'raw_result': {
                            'pending_before': pending_before,
                            'member_count_before': member_before,
                            'body_excerpt': body_before[-2000:],
                        },
                    }
                self._open_pending_review(pending_before)
                row = self._wait_for_review_row()
                row_text = row.inner_text().strip()
                phone_matches = self._extract_all_phones(row_text)
                target_phone = phone_matches[0] if phone_matches else ''
                target_phone_raw = target_phone or row_text.splitlines()[0].strip()
                pushname = self._page.locator('[data-testid="pushname"]')
                target_name = pushname.first.inner_text().strip() if pushname.count() else (row_text.splitlines()[0].strip() if row_text else '')
                self._click_approve_action(row)
                self._page.wait_for_timeout(max(self.post_click_wait_ms, 100))
                verification = self._same_session_verify(target_phone=target_phone, pending_before=pending_before)
                finished_at = datetime.now(timezone.utc).isoformat()
                verified = bool(verification['queue_delta'] and verification['member_confirmed'])
                self._last_action_at = finished_at
                result = {
                    'status': 'success' if verified else 'failed',
                    'verified': verified,
                    'result_code': 'approved' if verified else 'approval_not_verified',
                    'result_reason': 'queue delta and member confirmation verified' if verified else 'strict verification failed after approve click',
                    'finished_at': finished_at,
                    'approved_at': finished_at,
                    'approved_count': int(context.get('approved_count') or 1),
                    'elapsed_seconds': round(time.perf_counter() - started, 2),
                    'queue_delta': verification['queue_delta'],
                    'member_confirmed': verification['member_confirmed'],
                    'target_member': {
                        'name': target_name,
                        'phone_raw': target_phone_raw,
                        'phone_normalized': target_phone,
                    },
                    'raw_result': {
                        'pending_before': pending_before,
                        'member_count_before': member_before,
                        'pending_after': verification['pending_count'],
                        'member_count_after': verification['member_count'],
                        'all_phones_normalized': verification['all_phones_normalized'],
                        'verification_excerpt': verification['body_excerpt'],
                    },
                }
                return result
            except PlaywrightTimeoutError as exc:
                self._reset_browser(f'playwright_timeout:{exc}')
                finished_at = datetime.now(timezone.utc).isoformat()
                return {
                    'status': 'failed',
                    'verified': False,
                    'result_code': 'playwright_timeout',
                    'result_reason': str(exc),
                    'finished_at': finished_at,
                    'elapsed_seconds': round(time.perf_counter() - started, 2),
                    'raw_result': {},
                }
            except Exception as exc:
                self._reset_browser(f'playwright_error:{exc}')
                finished_at = datetime.now(timezone.utc).isoformat()
                return {
                    'status': 'failed',
                    'verified': False,
                    'result_code': 'playwright_error',
                    'result_reason': str(exc),
                    'finished_at': finished_at,
                    'elapsed_seconds': round(time.perf_counter() - started, 2),
                    'raw_result': {},
                }


class MultiRegistrationGroupApprovalExecutor:
    def __init__(self, *, group_configs: Dict[str, Dict[str, Any]], default_kwargs: Optional[Dict[str, Any]] = None) -> None:
        self.group_configs = {str(k).strip(): dict(v or {}) for k, v in (group_configs or {}).items() if str(k).strip()}
        self.default_kwargs = dict(default_kwargs or {})
        self._executors: Dict[str, LiveWarmWhatsAppRegistrationGroupApprovalExecutor] = {}
        self._lock = threading.Lock()

    def _build_executor(self, registration_group: str) -> LiveWarmWhatsAppRegistrationGroupApprovalExecutor:
        config = dict(self.default_kwargs)
        config.update(self.group_configs.get(registration_group, {}))
        config.setdefault('registration_group_name', registration_group)
        if config.get('temp_user_data_dir'):
            temp_root = Path(str(config['temp_user_data_dir']).expanduser())
            suffix = re.sub(r'[^A-Za-z0-9._-]+', '-', registration_group).strip('-') or 'default'
            config['temp_user_data_dir'] = str(temp_root / suffix)
        return LiveWarmWhatsAppRegistrationGroupApprovalExecutor(**config)

    def _get_executor(self, registration_group: str) -> LiveWarmWhatsAppRegistrationGroupApprovalExecutor:
        with self._lock:
            executor = self._executors.get(registration_group)
            if executor is None:
                executor = self._build_executor(registration_group)
                self._executors[registration_group] = executor
            return executor

    def approve(self, context: Dict[str, Any]) -> Dict[str, Any]:
        registration_group = str(context.get('registration_group') or '').strip()
        if not registration_group:
            return {
                'status': 'failed',
                'verified': False,
                'result_code': 'registration_group_missing',
                'result_reason': 'registration_group is required',
                'raw_result': {},
            }
        executor = self._get_executor(registration_group)
        result = executor.approve(context)
        if isinstance(result, dict):
            raw_result = result.setdefault('raw_result', {})
            if isinstance(raw_result, dict):
                raw_result.setdefault('registration_group', registration_group)
        return result

    def health(self) -> Dict[str, Any]:
        rows = []
        configured_groups = sorted(self.group_configs.keys())
        for registration_group in configured_groups:
            executor = self._executors.get(registration_group)
            if executor is not None:
                health = executor.health() or {}
            else:
                health = {
                    'configured': True,
                    'status': 'idle',
                    'provider': 'whatsapp_web_live_warm',
                    'group_name': registration_group,
                    'timing_profile': dict(self.default_kwargs),
                }
            rows.append({'registration_group': registration_group, **health})
        return {
            'configured': bool(configured_groups),
            'status': 'warm' if any(row.get('status') == 'warm' for row in rows) else ('configured' if rows else 'unconfigured'),
            'provider': 'registration_group_executor_pool',
            'supports': ['approve', 'per_group_executor_pool', 'crm_batch_writeback_ready'],
            'pool_size': len(rows),
            'rows': rows,
        }
