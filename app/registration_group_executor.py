from __future__ import annotations

import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any, Dict, Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


PHONE_PATTERN = re.compile(r'\+\d[\d\s\-*]{6,}')
PENDING_COUNT_PATTERN = re.compile(r'待处理请求\s*(\d+)')
REVIEW_CTA_PATTERN = re.compile(r'审核\s*(\d+)\s*请求加入')
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
        self._active_temp_user_data_dir: Optional[str] = None
        self._owner_thread_id: Optional[int] = None
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
            'temp_user_data_dir': self._active_temp_user_data_dir or self.temp_user_data_dir,
            'temp_user_data_dir_base': self.temp_user_data_dir,
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

    def _allocate_run_temp_user_data_dir(self) -> Path:
        base_dir = Path(self.temp_user_data_dir).expanduser()
        base_dir.parent.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=f'{base_dir.name}-', dir=str(base_dir.parent)))

    def _clone_profile_once(self) -> None:
        src_root = Path(self.chrome_user_data_root)
        if not self._active_temp_user_data_dir:
            self._active_temp_user_data_dir = str(self._allocate_run_temp_user_data_dir())
        dst_root = Path(self._active_temp_user_data_dir)
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
        self._owner_thread_id = None
        self._warm = False

    def _reset_browser(self, reason: str) -> None:
        self._last_error = reason
        active_temp_user_data_dir = self._active_temp_user_data_dir
        self._close_browser()
        self._active_temp_user_data_dir = None
        if not active_temp_user_data_dir:
            return
        try:
            shutil.rmtree(active_temp_user_data_dir)
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def _page_has_logged_out_gate(self) -> bool:
        assert self._page is not None
        login_markers = [
            ('扫描登录', False),
            ('使用电话号码登录', False),
            ('开始使用', False),
        ]
        for text, exact in login_markers:
            try:
                if self._page.get_by_text(text, exact=exact).count():
                    return True
            except Exception:
                continue
        return False

    def _ensure_browser(self) -> None:
        current_thread_id = threading.get_ident()
        if self._context is not None and self._page is not None:
            if self._owner_thread_id is not None and self._owner_thread_id != current_thread_id:
                self._reset_browser(f'thread_mismatch:owner={self._owner_thread_id},current={current_thread_id}')
            else:
                if self._page_has_logged_out_gate():
                    self._reset_browser('logged_out:whatsapp_session_not_authenticated')
                    raise RuntimeError('logged_out:whatsapp_session_not_authenticated')
                return
        try:
            self._clone_profile_once()
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                self._active_temp_user_data_dir,
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
                self._active_temp_user_data_dir,
                channel=self.chrome_channel,
                headless=True,
                args=[f'--profile-directory={self.profile_dir}'],
            )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.goto('https://web.whatsapp.com/', wait_until='domcontentloaded', timeout=60000)
        self._page.wait_for_timeout(max(self.initial_wait_ms, 200))
        if self._page_has_logged_out_gate():
            self._reset_browser('logged_out:whatsapp_session_not_authenticated')
            raise RuntimeError('logged_out:whatsapp_session_not_authenticated')
        self._last_error = None
        self._last_started_at = datetime.now(timezone.utc).isoformat()
        self._owner_thread_id = current_thread_id
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
        try:
            group_info_visible = bool(self._page.get_by_text('群组信息', exact=True).count())
        except Exception:
            group_info_visible = False
        try:
            pending_section_visible = bool(self._page.get_by_text('待处理请求', exact=True).count())
        except Exception:
            pending_section_visible = False
        try:
            empty_queue_visible = bool(self._page.get_by_text('没有要审核的成员', exact=True).count())
        except Exception:
            empty_queue_visible = False
        try:
            contact_info_visible = bool(self._page.get_by_text('联系人信息', exact=True).count())
        except Exception:
            contact_info_visible = False
        try:
            membership_request_visible = bool(self._page.locator('[data-testid="subtype-membership_approval_request"]').count())
        except Exception:
            membership_request_visible = False
        if contact_info_visible and not group_info_visible and not pending_section_visible and not empty_queue_visible:
            return False
        return bool(group_info_visible or pending_section_visible or empty_queue_visible or membership_request_visible)

    def _open_group_info(self) -> None:
        assert self._page is not None
        if self._group_info_ready and self._page_ready_for_approval():
            return
        self._enter_groups_tab()
        self._page.locator(f'[data-testid="chat-list"] [data-testid="list-item-{self.registration_list_item_index}"]').click(timeout=10000)
        self._page.wait_for_timeout(max(self.navigation_wait_ms, 100))
        self._group_info_ready = self._page_ready_for_approval()
        if self._group_info_ready:
            return
        self._page.locator('[data-testid="conversation-header"]').click(timeout=10000)
        self._page.wait_for_timeout(max(self.navigation_wait_ms, 100))
        self._group_info_ready = self._page_ready_for_approval()
        if self._group_info_ready:
            return
        try:
            self._page.locator('[data-testid="conversation-subheader"]').click(timeout=2000)
            self._page.wait_for_timeout(max(self.navigation_wait_ms, 100))
        except Exception:
            pass
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
        if '待处理请求' in body:
            relevant = body.split('待处理请求', 1)[1]
            relevant_phones = []
            for phone in PHONE_PATTERN.findall(relevant):
                normalized = self._normalize_phone(phone)
                if normalized and normalized not in relevant_phones:
                    relevant_phones.append(normalized)
            if relevant_phones:
                return len(relevant_phones)
            relevant_request_join_count = len(REQUEST_JOIN_ROW_PATTERN.findall(relevant))
            if relevant_request_join_count:
                return relevant_request_join_count
            review_match = REVIEW_CTA_PATTERN.search(relevant)
            if review_match:
                return int(review_match.group(1))
            return 0
        if '联系人信息' in body and '群组信息' not in body:
            return 0
        review_match = REVIEW_CTA_PATTERN.search(body)
        if review_match:
            return int(review_match.group(1))
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

    def _review_surface_state(self) -> Dict[str, Any]:
        assert self._page is not None
        row_count = 0
        approve_count = 0
        membership_request_button_count = 0
        empty_queue_detected = False
        body_excerpt = ''
        try:
            row_count = self._page.locator('[data-testid="row"]').count()
        except Exception:
            row_count = 0
        try:
            approve_count = self._page.locator('[aria-label="批准"]').count()
        except Exception:
            approve_count = 0
        try:
            membership_request_button_count = self._page.locator('[data-testid="subtype-membership_approval_request"]').count()
        except Exception:
            membership_request_button_count = 0
        try:
            empty_queue_detected = bool(self._page.get_by_text('没有要审核的成员').count())
        except Exception:
            empty_queue_detected = False
        try:
            body_excerpt = self._page.locator('body').inner_text(timeout=1200)[-1200:]
        except Exception:
            body_excerpt = ''
        return {
            'row_count': row_count,
            'approve_count': approve_count,
            'membership_request_button_count': membership_request_button_count,
            'empty_queue_detected': empty_queue_detected,
            'body_excerpt': body_excerpt,
        }

    def _wait_for_review_surface(self, *, timeout_seconds: float = 3.0) -> Dict[str, Any]:
        assert self._page is not None
        deadline = time.perf_counter() + max(0.3, timeout_seconds)
        latest = self._review_surface_state()
        while True:
            if latest['row_count'] > 0 or latest['approve_count'] > 0 or latest['empty_queue_detected']:
                return latest
            if time.perf_counter() >= deadline:
                return latest
            self._page.wait_for_timeout(120)
            latest = self._review_surface_state()

    def _open_pending_review(self, pending_before: int) -> Dict[str, Any]:
        assert self._page is not None

        def _await_surface(opened_via: str) -> Dict[str, Any]:
            state = self._wait_for_review_surface(timeout_seconds=2.0)
            state['opened_via'] = opened_via
            return state

        review_text = f'审核{pending_before}请求加入'
        review = self._page.get_by_text(review_text)
        try:
            review.first.click(timeout=2000)
            self._page.wait_for_timeout(max(self.navigation_wait_ms, 500))
            state = _await_surface('review_text')
            if state['row_count'] > 0 or state['approve_count'] > 0 or state['empty_queue_detected']:
                return state
        except Exception:
            pass
        try:
            approve_buttons = self._page.locator('[aria-label="批准"]')
            if approve_buttons.count():
                state = self._review_surface_state()
                state['opened_via'] = 'approve_button_already_visible'
                return state
        except Exception:
            pass
        try:
            membership_requests = self._page.locator('[data-testid="subtype-membership_approval_request"]')
            membership_count = membership_requests.count()
        except Exception:
            membership_requests = None
            membership_count = 0
        last_membership_state = None
        empty_membership_state = None
        for index in range(membership_count - 1, -1, -1):
            try:
                membership_requests.nth(index).click(timeout=2000, force=True)
                self._page.wait_for_timeout(max(self.navigation_wait_ms, 300))
                state = _await_surface(f'membership_request_button_{index}')
                last_membership_state = state
                if state['row_count'] > 0 or state['approve_count'] > 0:
                    return state
                if state['empty_queue_detected']:
                    if membership_count <= 1:
                        return state
                    if empty_membership_state is None:
                        empty_membership_state = state
            except Exception:
                continue
        if empty_membership_state is not None:
            return empty_membership_state
        if last_membership_state is not None:
            return last_membership_state
        try:
            subheader = self._page.locator('[data-testid="conversation-subheader"]')
            if subheader.count():
                subheader.click(timeout=2000)
                self._page.wait_for_timeout(max(self.navigation_wait_ms, 500))
                state = _await_surface('conversation_subheader')
                if state['row_count'] > 0 or state['approve_count'] > 0 or state['empty_queue_detected']:
                    return state
        except Exception:
            pass
        state = self._wait_for_review_surface(timeout_seconds=2.0)
        state['opened_via'] = 'surface_poll_timeout'
        return state

    def _wait_for_review_row(self):
        assert self._page is not None
        deadline = time.perf_counter() + 2.0
        while True:
            try:
                rows = self._page.locator('[data-testid="row"]')
                if rows.count():
                    row = rows.first
                    try:
                        row.inner_text(timeout=300)
                        return row
                    except Exception:
                        pass
            except Exception:
                pass
            if time.perf_counter() >= deadline:
                snapshot = self._review_surface_state()
                raise PlaywrightTimeoutError(
                    f'review row unavailable after opening pending review; '
                    f'row_count={snapshot.get("row_count", 0)} approve_count={snapshot.get("approve_count", 0)}'
                )
            self._page.wait_for_timeout(120)

    def _click_approve_action(self, row) -> None:
        assert self._page is not None

        def _submission_confirmed(timeout_seconds: float = 0.8) -> bool:
            deadline = time.perf_counter() + max(0.2, timeout_seconds)
            while True:
                snapshot = self._review_surface_state()
                if snapshot.get('empty_queue_detected'):
                    return True
                if snapshot.get('row_count', 0) == 0 and snapshot.get('approve_count', 0) == 0:
                    return True
                if time.perf_counter() >= deadline:
                    return False
                self._page.wait_for_timeout(120)

        try:
            row.locator('[aria-label="批准"]').click(timeout=1200, force=True)
            if _submission_confirmed(timeout_seconds=0.8):
                return
        except Exception:
            pass

        def _click_global_approve(timeout_seconds: float = 1.5) -> bool:
            deadline = time.perf_counter() + max(0.3, timeout_seconds)
            while True:
                try:
                    approve_buttons = self._page.locator('[aria-label="批准"]')
                    if approve_buttons.count():
                        approve_buttons.first.click(timeout=1200, force=True)
                        return True
                except Exception:
                    pass
                if time.perf_counter() >= deadline:
                    return False
                self._page.wait_for_timeout(120)

        if _click_global_approve(timeout_seconds=0.8):
            return
        row.click(timeout=1200, force=True)
        if _click_global_approve(timeout_seconds=2.0):
            return
        snapshot = self._review_surface_state()
        raise PlaywrightTimeoutError(
            f'approve action unavailable after review row opened; '
            f'row_count={snapshot.get("row_count", 0)} approve_count={snapshot.get("approve_count", 0)}'
        )

    def approve(self, context: Dict[str, Any]) -> Dict[str, Any]:
        started = time.perf_counter()
        stage_marks: Dict[str, float] = {}
        with self._approval_lock:
            try:
                self._ensure_browser()
                stage_marks['browser_ready_seconds'] = round(time.perf_counter() - started, 3)
                self._open_group_info()
                stage_marks['group_info_ready_seconds'] = round(time.perf_counter() - started, 3)
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
                            'stage_timings': dict(stage_marks),
                        },
                    }
                review_surface = self._open_pending_review(pending_before)
                stage_marks['review_surface_ready_seconds'] = round(time.perf_counter() - started, 3)
                if review_surface.get('empty_queue_detected'):
                    finished_at = datetime.now(timezone.utc).isoformat()
                    return {
                        'status': 'failed',
                        'verified': False,
                        'result_code': 'no_pending_request',
                        'result_reason': 'review surface opened but no actionable pending member remained',
                        'finished_at': finished_at,
                        'elapsed_seconds': round(time.perf_counter() - started, 2),
                        'raw_result': {
                            'pending_before': pending_before,
                            'member_count_before': member_before,
                            'review_surface': review_surface,
                            'body_excerpt': body_before[-2000:],
                            'stage_timings': dict(stage_marks),
                        },
                    }
                row = self._wait_for_review_row()
                stage_marks['review_row_ready_seconds'] = round(time.perf_counter() - started, 3)
                row_text = row.inner_text().strip()
                phone_matches = self._extract_all_phones(row_text)
                target_phone = phone_matches[0] if phone_matches else ''
                target_phone_raw = target_phone or row_text.splitlines()[0].strip()
                pushname = self._page.locator('[data-testid="pushname"]')
                target_name = pushname.first.inner_text().strip() if pushname.count() else (row_text.splitlines()[0].strip() if row_text else '')
                self._click_approve_action(row)
                stage_marks['approve_clicked_seconds'] = round(time.perf_counter() - started, 3)
                self._page.wait_for_timeout(max(self.post_click_wait_ms, 100))
                verification = self._same_session_verify(target_phone=target_phone, pending_before=pending_before)
                retry_attempted = False
                retry_succeeded = False
                retry_snapshot = None
                if not verification['queue_delta']:
                    try:
                        retry_snapshot = self._review_surface_state()
                    except Exception:
                        retry_snapshot = None
                    if retry_snapshot and (retry_snapshot.get('row_count', 0) > 0 or retry_snapshot.get('approve_count', 0) > 0):
                        retry_attempted = True
                        self._click_approve_action(row)
                        self._page.wait_for_timeout(max(self.post_click_wait_ms, 100))
                        verification = self._same_session_verify(target_phone=target_phone, pending_before=pending_before)
                        retry_succeeded = bool(verification.get('queue_delta'))
                stage_marks['verification_complete_seconds'] = round(time.perf_counter() - started, 3)
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
                        'row_text_excerpt': row_text[-800:],
                        'review_surface': review_surface,
                        'retry_attempted': retry_attempted,
                        'retry_succeeded': retry_succeeded,
                        'retry_snapshot': retry_snapshot,
                        'stage_timings': dict(stage_marks),
                    },
                }
                return result
            except PlaywrightTimeoutError as exc:
                timeout_snapshot = {}
                try:
                    timeout_snapshot = self._review_surface_state()
                except Exception:
                    timeout_snapshot = {}
                self._reset_browser(f'playwright_timeout:{exc}')
                finished_at = datetime.now(timezone.utc).isoformat()
                return {
                    'status': 'failed',
                    'verified': False,
                    'result_code': 'playwright_timeout',
                    'result_reason': str(exc),
                    'finished_at': finished_at,
                    'elapsed_seconds': round(time.perf_counter() - started, 2),
                    'raw_result': {
                        'timeout_snapshot': timeout_snapshot,
                        'stage_timings': dict(stage_marks),
                    },
                }
            except Exception as exc:
                error_snapshot = {}
                try:
                    error_snapshot = self._review_surface_state()
                except Exception:
                    error_snapshot = {}
                self._reset_browser(f'playwright_error:{exc}')
                finished_at = datetime.now(timezone.utc).isoformat()
                return {
                    'status': 'failed',
                    'verified': False,
                    'result_code': 'playwright_error',
                    'result_reason': str(exc),
                    'finished_at': finished_at,
                    'elapsed_seconds': round(time.perf_counter() - started, 2),
                    'raw_result': {
                        'error_snapshot': error_snapshot,
                        'stage_timings': dict(stage_marks),
                    },
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
