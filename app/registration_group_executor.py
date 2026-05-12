from __future__ import annotations

import queue
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


class ReviewSurfaceRecoveryRequired(RuntimeError):
    pass


class AmbiguousReviewTargetError(RuntimeError):
    pass


class StaleReviewSurfaceError(RuntimeError):
    pass


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
        self._owner_thread_lock = threading.Lock()
        self._owner_call_queue: "queue.Queue[tuple[Any, queue.Queue]]" = queue.Queue()
        self._owner_thread: Optional[threading.Thread] = None
        self._owner_thread_ready = threading.Event()
        self._last_review_selection: Dict[str, Any] = {}

    def warmup(self) -> Dict[str, Any]:
        return self._call_on_owner_thread(self._warmup_impl)

    def _ensure_owner_thread(self) -> None:
        worker = self._owner_thread
        if worker is not None and worker.is_alive():
            return
        with self._owner_thread_lock:
            worker = self._owner_thread
            if worker is not None and worker.is_alive():
                return
            self._owner_thread_ready.clear()
            worker = threading.Thread(
                target=self._owner_thread_main,
                name='registration-group-approval-owner',
                daemon=True,
            )
            self._owner_thread = worker
            worker.start()
        self._owner_thread_ready.wait(timeout=5)

    def _owner_thread_main(self) -> None:
        self._owner_thread_id = threading.get_ident()
        self._owner_thread_ready.set()
        while True:
            task, result_queue = self._owner_call_queue.get()
            try:
                result_queue.put((True, task()))
            except BaseException as exc:
                result_queue.put((False, exc))

    def _call_on_owner_thread(self, func):
        if threading.get_ident() == self._owner_thread_id:
            return func()
        self._ensure_owner_thread()
        result_queue: "queue.Queue[tuple[bool, Any]]" = queue.Queue(maxsize=1)
        self._owner_call_queue.put((func, result_queue))
        ok, payload = result_queue.get()
        if ok:
            return payload
        raise payload

    def _warmup_impl(self) -> Dict[str, Any]:
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

    def _page_has_loading_gate(self) -> bool:
        assert self._page is not None
        loading_markers = [
            ('请不要关闭此窗口', False),
            ('消息正在下载中', False),
            ('你的消息正在下载中', False),
        ]
        for text, exact in loading_markers:
            try:
                if self._page.get_by_text(text, exact=exact).count():
                    return True
            except Exception:
                continue
        return False

    def _wait_for_home_surface_ready(self, *, timeout_seconds: float = 8.0) -> None:
        assert self._page is not None
        deadline = time.perf_counter() + max(0.5, timeout_seconds)
        while True:
            if self._page_has_logged_out_gate():
                return
            loading_gate = self._page_has_loading_gate()
            try:
                chat_list_ready = bool(self._page.locator('[data-testid="chat-list"]').count())
            except Exception:
                chat_list_ready = False
            if chat_list_ready and not loading_gate:
                return
            if time.perf_counter() >= deadline:
                return
            self._page.wait_for_timeout(120)

    def _ensure_browser(self) -> None:
        current_thread_id = threading.get_ident()
        if self._context is not None and self._page is not None:
            if self._page_has_logged_out_gate():
                self._reset_browser('logged_out:whatsapp_session_not_authenticated')
                raise RuntimeError('logged_out:whatsapp_session_not_authenticated')
            self._wait_for_home_surface_ready(timeout_seconds=12.0)
            self._owner_thread_id = current_thread_id
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
        self._wait_for_home_surface_ready(timeout_seconds=12.0)
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
        if contact_info_visible and not group_info_visible and not pending_section_visible and not empty_queue_visible:
            return False
        return bool(group_info_visible or pending_section_visible or empty_queue_visible)

    def _search_chat_by_group_name(self, target_group_name: str) -> bool:
        assert self._page is not None
        target_name = str(target_group_name or '').strip()
        if not target_name:
            return False
        search_inputs = []
        for placeholder in ('搜索或开始新聊天', 'Search or start new chat', '搜索'):
            try:
                search_inputs.append(self._page.get_by_placeholder(placeholder))
            except Exception:
                continue
        for search_input in search_inputs:
            try:
                if not search_input.count():
                    continue
                search_input.first.click(timeout=1200)
                search_input.first.fill(target_name, timeout=1500)
                self._page.wait_for_timeout(max(self.navigation_wait_ms, 200))
                named_chat_locator = self._page.get_by_text(target_name, exact=True)
                if int(named_chat_locator.count()) > 0:
                    named_chat_locator.first.click(timeout=1500)
                    return True
                search_input.first.fill('', timeout=800)
            except Exception:
                continue
        return False

    def _open_registration_chat_row(self, target_group_name: Optional[str] = None, *, allow_index_fallback: bool = True) -> str:
        assert self._page is not None
        chat_item_selector = f'[data-testid="chat-list"] [data-testid="list-item-{self.registration_list_item_index}"]'
        target_name = str(target_group_name or self.registration_group_name or '').strip()
        named_chat_locator = None
        named_chat_count = 0
        if target_name:
            try:
                named_chat_locator = self._page.get_by_text(target_name, exact=True)
                named_chat_count = int(named_chat_locator.count())
            except Exception:
                named_chat_locator = None
                named_chat_count = 0
        if named_chat_locator is not None and named_chat_count > 0:
            named_chat_locator.first.click(timeout=1500)
            return 'named_chat_exact'
        if target_name and self._search_chat_by_group_name(target_name):
            return 'sidebar_search_exact'
        if not allow_index_fallback:
            raise RuntimeError(f'target_group_not_visible:{target_name}')
        try:
            self._page.locator(chat_item_selector).click(timeout=1500)
            return f'list_item_index_{self.registration_list_item_index}'
        except Exception as specific_error:
            generic_rows = self._page.locator('[data-testid="chat-list"] [data-testid^="list-item-"]')
            generic_count = 0
            try:
                generic_count = int(generic_rows.count())
            except Exception:
                generic_count = 0
            if generic_count > self.registration_list_item_index and hasattr(generic_rows, 'nth'):
                generic_rows.nth(self.registration_list_item_index).click(timeout=1500)
                return f'generic_list_item_index_{self.registration_list_item_index}'
            raise specific_error

    def _try_open_group_info_on_current_surface(self, *, timeout_seconds: float = 1.2) -> bool:
        assert self._page is not None
        if self._page_ready_for_approval():
            self._group_info_ready = True
            return True
        deadline = time.perf_counter() + max(0.3, timeout_seconds)
        last_error = None
        while True:
            try:
                self._page.locator('[data-testid="conversation-header"]').click(timeout=1200)
                self._page.wait_for_timeout(max(self.navigation_wait_ms, 120))
                if self._page_ready_for_approval():
                    self._group_info_ready = True
                    return True
            except Exception as exc:
                last_error = exc
            try:
                self._page.locator('[data-testid="conversation-subheader"]').click(timeout=1200)
                self._page.wait_for_timeout(max(self.navigation_wait_ms, 120))
                if self._page_ready_for_approval():
                    self._group_info_ready = True
                    return True
            except Exception as exc:
                last_error = exc
            if time.perf_counter() >= deadline:
                self._group_info_ready = self._page_ready_for_approval()
                return bool(self._group_info_ready)
            self._page.wait_for_timeout(120)

    def _open_group_info(self, target_group_name: Optional[str] = None, *, allow_index_fallback: bool = True) -> None:
        assert self._page is not None
        if self._group_info_ready and self._page_ready_for_approval():
            return
        if self._try_open_group_info_on_current_surface(timeout_seconds=1.2):
            return
        chat_deadline = time.perf_counter() + 4.0
        last_error = None
        while True:
            self._enter_groups_tab()
            self._wait_for_home_surface_ready(timeout_seconds=3.0)
            if self._page_has_loading_gate():
                chat_deadline = max(chat_deadline, time.perf_counter() + 3.0)
                self._page.wait_for_timeout(120)
                continue
            try:
                self._open_registration_chat_row(target_group_name=target_group_name, allow_index_fallback=allow_index_fallback)
                self._page.wait_for_timeout(max(self.initial_wait_ms, 200))
                break
            except Exception as exc:
                last_error = exc
                if time.perf_counter() >= chat_deadline:
                    raise RuntimeError(f'unable_to_open_registration_group_chat_row:{last_error}')
                self._page.wait_for_timeout(120)
        if self._page_ready_for_approval():
            self._group_info_ready = True
            return
        deadline = time.perf_counter() + 4.0
        last_error = None
        while True:
            try:
                self._page.locator('[data-testid="conversation-header"]').click(timeout=1200)
                self._page.wait_for_timeout(max(self.navigation_wait_ms, 120))
                if self._page_ready_for_approval():
                    self._group_info_ready = True
                    return
            except Exception as exc:
                last_error = exc
            try:
                self._page.locator('[data-testid="conversation-subheader"]').click(timeout=1200)
                self._page.wait_for_timeout(max(self.navigation_wait_ms, 120))
                if self._page_ready_for_approval():
                    self._group_info_ready = True
                    return
            except Exception as exc:
                last_error = exc
            if time.perf_counter() >= deadline:
                self._group_info_ready = self._page_ready_for_approval()
                if self._group_info_ready:
                    return
                raise RuntimeError(f'unable to open group info surface: {last_error}')
            self._page.wait_for_timeout(120)

    def _normalize_phone(self, text: str) -> str:
        digits = re.sub(r'\D+', '', str(text or ''))
        if not digits:
            return ''
        if str(text).strip().startswith('+'):
            return '+' + digits
        return digits

    def _pre_panel_live_segments(self, body: str) -> list[str]:
        text = str(body or '')
        if '群组信息' not in text:
            return []
        pre_panel_body = text.rsplit('群组信息', 1)[0]
        pre_panel_relevant = pre_panel_body.rsplit('聊天历史', 1)[-1]
        if '输入消息' not in pre_panel_relevant:
            return []
        before_input, after_input = pre_panel_relevant.rsplit('输入消息', 1)
        segments: list[str] = []
        after_segment = str(after_input or '').strip()
        if after_segment:
            segments.append(after_segment)
        before_lines = [str(line or '').strip() for line in before_input.splitlines() if str(line or '').strip()]
        shell_window = ''
        if before_lines:
            anchor_indices = [
                index
                for index, line in enumerate(before_lines)
                if any(anchor in line for anchor in ['你已通过邀请链接加入', '位成员', '群组管理员', '添加成员标记'])
            ]
            if anchor_indices:
                start_index = anchor_indices[-1]
                shell_window = '\n'.join(before_lines[start_index:]).strip()
            else:
                shell_window = '\n'.join(before_lines[-16:]).strip()
        has_active_shell = bool(
            shell_window
            and any(anchor in shell_window for anchor in ['你已通过邀请链接加入', '位成员', '群组管理员', '添加成员标记'])
            and (
                REQUEST_JOIN_ROW_PATTERN.search(shell_window)
                or '由+' in shell_window
                or '由 +' in shell_window
            )
        )
        if has_active_shell and shell_window:
            segments.append(shell_window)
        deduped = []
        seen = set()
        for segment in segments:
            if segment not in seen:
                deduped.append(segment)
                seen.add(segment)
        return deduped

    def _extract_pending_count(self, text: str) -> int:
        body = str(text or '')
        panel_body = body.rsplit('群组信息', 1)[1] if '群组信息' in body else body
        m = PENDING_COUNT_PATTERN.search(panel_body)
        if m:
            return int(m.group(1))
        if '待处理请求' in panel_body:
            relevant = panel_body.rsplit('待处理请求', 1)[1]
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
        review_match = REVIEW_CTA_PATTERN.search(panel_body)
        if review_match:
            return int(review_match.group(1))
        request_join_count = len(REQUEST_JOIN_ROW_PATTERN.findall(panel_body))
        if request_join_count:
            return request_join_count
        for live_segment in self._pre_panel_live_segments(body):
            live_phones = []
            for phone in PHONE_PATTERN.findall(live_segment):
                normalized = self._normalize_phone(phone)
                if normalized and normalized not in live_phones:
                    live_phones.append(normalized)
            pre_panel_review_match = REVIEW_CTA_PATTERN.search(live_segment)
            pre_panel_request_join_count = len(REQUEST_JOIN_ROW_PATTERN.findall(live_segment))
            inferred_counts = [len(live_phones), pre_panel_request_join_count]
            if pre_panel_review_match:
                inferred_counts.append(int(pre_panel_review_match.group(1)))
            inferred_count = max(inferred_counts or [0])
            if inferred_count > 0:
                return inferred_count
        return 0

    def _extract_pending_candidates(self, text: str) -> Dict[str, list[str]]:
        body = str(text or '')
        panel_body = body.rsplit('群组信息', 1)[1] if '群组信息' in body else body
        relevant = ''
        if '待处理请求' in panel_body:
            relevant = panel_body.rsplit('待处理请求', 1)[1]
        else:
            for live_segment in self._pre_panel_live_segments(body):
                if REVIEW_CTA_PATTERN.search(live_segment) or REQUEST_JOIN_ROW_PATTERN.search(live_segment):
                    relevant = live_segment
                    break
        if not relevant:
            return {'phones': [], 'requesters': []}
        for marker in ['\n联系人信息\n', '\n输入消息\n', '\n搜索\n', '\n群组信息\n']:
            if marker in relevant:
                relevant = relevant.split(marker, 1)[0]
        if '没有要审核的成员' in relevant:
            return {'phones': [], 'requesters': []}
        requesters = []
        ignored_requester_values = {
            '待处理请求',
            '通过邀请链接',
            '你已成为群组管理员',
            '添加成员',
            '添加成员标记',
            '菜单',
        }
        for line in relevant.splitlines():
            value = str(line or '').strip()
            if not value or value in ignored_requester_values:
                continue
            if value.startswith('审核') and '请求加入' in value:
                continue
            if PHONE_PATTERN.fullmatch(value):
                continue
            if value.startswith('由+') or value.startswith('由 +'):
                continue
            if '请求加入' in value or '点击以审核' in value:
                continue
            if value not in requesters:
                requesters.append(value)
        return {
            'phones': self._extract_all_phones(relevant),
            'requesters': requesters,
        }

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

    def _review_candidates_match_expected_pending(
        self,
        *,
        candidate_rows: list[Dict[str, Any]],
        pending_candidates: Dict[str, list[str]],
        expected_phone: str = '',
        expected_name: str = '',
    ) -> bool:
        rows = [dict(row or {}) for row in list(candidate_rows or [])]
        if not rows:
            return True
        actionable = [row for row in rows if bool(row.get('actionable'))]
        if not actionable:
            actionable = rows
        if len(actionable) <= 1:
            return True
        pending_phones = {
            self._normalize_phone(value)
            for value in list((pending_candidates or {}).get('phones') or [])
            if self._normalize_phone(value)
        }
        pending_names = {
            str(value or '').strip()
            for value in list((pending_candidates or {}).get('requesters') or [])
            if str(value or '').strip()
        }
        normalized_expected_phone = self._normalize_phone(expected_phone)
        normalized_expected_name = str(expected_name or '').strip()
        if normalized_expected_phone:
            pending_phones.add(normalized_expected_phone)
        if normalized_expected_name:
            pending_names.add(normalized_expected_name)
        if not pending_phones and not pending_names:
            return True
        for candidate in actionable:
            candidate_phones = {
                self._normalize_phone(value)
                for value in list(candidate.get('phones') or [])
                if self._normalize_phone(value)
            }
            candidate_name = str(candidate.get('display_name') or '').strip()
            if candidate_phones & pending_phones:
                return True
            if candidate_name and candidate_name in pending_names:
                return True
        return False

    def _capture_group_info_body(self, *, wait_for_pending_seconds: float = 1.2) -> str:
        assert self._page is not None
        deadline = time.perf_counter() + max(0.2, wait_for_pending_seconds)
        latest = self._page.locator('body').inner_text(timeout=1200)
        while True:
            group_info_visible = '群组信息' in latest
            if group_info_visible and self._extract_pending_count(latest) > 0:
                return latest
            if group_info_visible and '没有要审核的成员' in latest:
                return latest
            if '联系人信息' in latest and '群组信息' not in latest:
                return latest
            if time.perf_counter() >= deadline:
                return latest
            self._page.wait_for_timeout(120)
            latest = self._page.locator('body').inner_text(timeout=1200)

    def _review_row_priority(self, row_text: str, *, expected_phone: str = '', expected_name: str = '', approve_available: bool = False) -> int:
        text = str(row_text or '')
        normalized_expected_phone = self._normalize_phone(expected_phone)
        normalized_phones = self._extract_all_phones(text)
        normalized_expected_name = str(expected_name or '').strip()
        if normalized_expected_phone and normalized_expected_phone in normalized_phones:
            return 100
        if normalized_expected_name and normalized_expected_name in text:
            return 90
        if '请求加入' in text or '点击以审核' in text:
            return 80
        if '通过邀请链接' in text or '由+' in text or '由 +' in text:
            return 70
        if approve_available:
            return 60
        if normalized_phones:
            return 50
        if text.strip():
            return 10
        return 0

    def _extract_review_row_candidate(self, row, index: int, *, expected_phone: str = '', expected_name: str = '') -> Optional[Dict[str, Any]]:
        approve_available = False
        try:
            approve_available = bool(row.locator('[aria-label="批准"]').count())
        except Exception:
            approve_available = False
        try:
            row_text = row.inner_text(timeout=300).strip()
        except Exception:
            return None
        if not row_text:
            return None
        lowered = row_text.lower()
        if '联系人信息' in row_text or '1个共同群组' in row_text or '影音内容、链接和文档' in row_text or '加密' in row_text:
            return None
        phones = self._extract_all_phones(row_text)
        display_name = ''
        for line in row_text.splitlines():
            value = str(line or '').strip()
            if not value:
                continue
            if PHONE_PATTERN.fullmatch(value):
                continue
            if value.startswith('由+') or value.startswith('由 +'):
                continue
            if value in {'通过邀请链接', '请求加入。点击以审核。', '请求加入', '点击以审核'}:
                continue
            display_name = value
            break
        has_request_marker = any(marker in row_text for marker in ['请求加入', '点击以审核', '通过邀请链接', '由+', '由 +'])
        actionable = bool(approve_available or has_request_marker)
        normalized_expected_phone = self._normalize_phone(expected_phone)
        normalized_expected_name = str(expected_name or '').strip()
        exact_phone_match = bool(normalized_expected_phone and normalized_expected_phone in phones)
        exact_name_match = bool(normalized_expected_name and normalized_expected_name == display_name)
        score = self._review_row_priority(
            row_text,
            expected_phone=expected_phone,
            expected_name=expected_name,
            approve_available=approve_available,
        )
        return {
            'row': row,
            'index': index,
            'row_text': row_text,
            'phones': phones,
            'display_name': display_name,
            'approve_available': approve_available,
            'has_request_marker': has_request_marker,
            'actionable': actionable,
            'exact_phone_match': exact_phone_match,
            'exact_name_match': exact_name_match,
            'score': score,
            'looks_like_contact_info': ('contact' in lowered and 'info' in lowered),
        }

    def _candidate_snapshot(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        return {
            'index': candidate.get('index'),
            'display_name': candidate.get('display_name') or '',
            'phones': list(candidate.get('phones') or []),
            'approve_available': bool(candidate.get('approve_available')),
            'has_request_marker': bool(candidate.get('has_request_marker')),
            'actionable': bool(candidate.get('actionable')),
            'exact_phone_match': bool(candidate.get('exact_phone_match')),
            'exact_name_match': bool(candidate.get('exact_name_match')),
            'score': int(candidate.get('score') or 0),
            'row_text_excerpt': str(candidate.get('row_text') or '')[-400:],
        }

    def _select_review_row_candidate(self, *, expected_phone: str = '', expected_name: str = '') -> Dict[str, Any]:
        assert self._page is not None
        rows = self._page.locator('[data-testid="row"]')
        row_count = rows.count()
        candidates: list[Dict[str, Any]] = []
        for index in range(row_count):
            row = rows.nth(index) if hasattr(rows, 'nth') else (rows.first if index == 0 else None)
            if row is None:
                continue
            candidate = self._extract_review_row_candidate(
                row,
                index,
                expected_phone=expected_phone,
                expected_name=expected_name,
            )
            if candidate is not None:
                candidates.append(candidate)
        snapshots = [self._candidate_snapshot(candidate) for candidate in candidates]
        self._last_review_selection = {
            'candidate_rows': snapshots,
            'selected_candidate': {},
            'selection_reason': '',
        }
        phone_matches = [candidate for candidate in candidates if candidate.get('exact_phone_match')]
        if len(phone_matches) == 1:
            selected = phone_matches[0]
            self._last_review_selection = {
                'candidate_rows': snapshots,
                'selected_candidate': self._candidate_snapshot(selected),
                'selection_reason': 'exact_phone_match',
            }
            return {
                'row': selected['row'],
                'candidate_rows': snapshots,
                'selected_candidate': self._candidate_snapshot(selected),
                'selection_reason': 'exact_phone_match',
            }
        if len(phone_matches) > 1:
            raise AmbiguousReviewTargetError('multiple review rows matched expected phone')
        name_matches = [candidate for candidate in candidates if candidate.get('exact_name_match')]
        if len(name_matches) == 1:
            selected = name_matches[0]
            self._last_review_selection = {
                'candidate_rows': snapshots,
                'selected_candidate': self._candidate_snapshot(selected),
                'selection_reason': 'exact_name_match',
            }
            return {
                'row': selected['row'],
                'candidate_rows': snapshots,
                'selected_candidate': self._candidate_snapshot(selected),
                'selection_reason': 'exact_name_match',
            }
        if len(name_matches) > 1:
            raise AmbiguousReviewTargetError('multiple review rows matched expected name')
        actionable = [candidate for candidate in candidates if candidate.get('actionable')]
        if len(actionable) == 1:
            selected = actionable[0]
            self._last_review_selection = {
                'candidate_rows': snapshots,
                'selected_candidate': self._candidate_snapshot(selected),
                'selection_reason': 'single_actionable_row',
            }
            return {
                'row': selected['row'],
                'candidate_rows': snapshots,
                'selected_candidate': self._candidate_snapshot(selected),
                'selection_reason': 'single_actionable_row',
            }
        if len(actionable) > 1:
            raise AmbiguousReviewTargetError('multiple actionable review rows remained without a unique exact match')
        best = max(candidates, key=lambda item: int(item.get('score') or 0), default=None)
        if best is not None and int(best.get('score') or 0) > 0:
            self._last_review_selection = {
                'candidate_rows': snapshots,
                'selected_candidate': self._candidate_snapshot(best),
                'selection_reason': 'best_effort_highest_score',
            }
            return {
                'row': best['row'],
                'candidate_rows': snapshots,
                'selected_candidate': self._candidate_snapshot(best),
                'selection_reason': 'best_effort_highest_score',
            }
        raise PlaywrightTimeoutError('review row candidate unavailable on actionable review surface')

    def _snapshot_group_state(self) -> Dict[str, Any]:
        assert self._page is not None
        body = self._page.locator('body').inner_text(timeout=1200)
        phones = self._extract_all_phones(body)
        pending_after = self._extract_pending_count(body)
        return {
            'pending_count': pending_after,
            'member_count': self._extract_member_count(body),
            'all_phones_normalized': phones,
            'body_excerpt': body[-2500:],
            'contact_info_detected': '联系人信息' in body and '群组信息' not in body,
            'empty_queue_detected': '没有要审核的成员' in body,
        }

    def _selected_candidate_exact_phone_match(self, *, target_phone: str, target_confirmation_hint: Optional[Dict[str, Any]] = None) -> bool:
        normalized_target_phone = self._normalize_phone(target_phone)
        if not normalized_target_phone:
            return False
        hint = dict(target_confirmation_hint or {})
        selected_candidate = dict(hint.get('selected_candidate') or {})
        selection_reason = str(hint.get('selection_reason') or '')
        selected_phones = []
        for value in list(selected_candidate.get('phones') or []):
            normalized_value = self._normalize_phone(value)
            if normalized_value and normalized_value not in selected_phones:
                selected_phones.append(normalized_value)
        exact_phone_match = bool(selected_candidate.get('exact_phone_match')) or selection_reason == 'exact_phone_match'
        return bool(exact_phone_match and normalized_target_phone in selected_phones)

    def _same_session_verify(
        self,
        *,
        target_phone: str,
        pending_before: int,
        target_confirmation_hint: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        assert self._page is not None
        if self.strict_reload_verify:
            self._page.reload(wait_until='domcontentloaded', timeout=60000)
            self._page.wait_for_timeout(max(self.initial_wait_ms, 200))
            self._open_group_info()
        deadline = time.perf_counter() + (self.verify_timeout_ms / 1000.0)
        latest = self._snapshot_group_state()
        while True:
            latest['queue_delta'] = latest['pending_count'] < pending_before
            latest['member_confirmation_source'] = ''
            latest['member_confirmed'] = bool(target_phone and target_phone in latest['all_phones_normalized'])
            if latest['member_confirmed']:
                latest['member_confirmation_source'] = 'body_phone_match'
            elif latest['queue_delta'] and self._selected_candidate_exact_phone_match(
                target_phone=target_phone,
                target_confirmation_hint=target_confirmation_hint,
            ):
                latest['member_confirmed'] = True
                latest['member_confirmation_source'] = 'selected_candidate_exact_phone_match'
            if latest['queue_delta'] and latest['member_confirmed']:
                return latest
            if latest['queue_delta'] and (latest.get('contact_info_detected') or int(latest.get('pending_count') or 0) <= 0):
                return latest
            if time.perf_counter() >= deadline:
                return latest
            self._page.wait_for_timeout(self.verify_poll_ms)
            latest = self._snapshot_group_state()

    def _review_surface_state(self, *, prefer_fast_path: bool = False) -> Dict[str, Any]:
        assert self._page is not None
        row_count = 0
        approve_count = 0
        membership_request_button_count = 0
        empty_queue_detected = False
        contact_info_detected = False
        review_marker_detected = False
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
            contact_info_detected = bool(self._page.get_by_text('联系人信息', exact=True).count())
        except Exception:
            contact_info_detected = False
        if prefer_fast_path and (approve_count > 0 or empty_queue_detected or contact_info_detected):
            return {
                'row_count': row_count,
                'approve_count': approve_count,
                'membership_request_button_count': membership_request_button_count,
                'empty_queue_detected': empty_queue_detected,
                'contact_info_detected': contact_info_detected,
                'review_marker_detected': bool(approve_count > 0),
                'body_excerpt': '',
            }
        try:
            body_excerpt = self._page.locator('body').inner_text(timeout=1200)[-1200:]
        except Exception:
            body_excerpt = ''
        review_marker_detected = any(marker in body_excerpt for marker in ['待处理请求', '请求加入', '点击以审核', '通过邀请链接'])
        return {
            'row_count': row_count,
            'approve_count': approve_count,
            'membership_request_button_count': membership_request_button_count,
            'empty_queue_detected': empty_queue_detected,
            'contact_info_detected': contact_info_detected,
            'review_marker_detected': review_marker_detected,
            'body_excerpt': body_excerpt,
        }

    def _wait_for_review_surface(self, *, timeout_seconds: float = 3.0) -> Dict[str, Any]:
        assert self._page is not None
        deadline = time.perf_counter() + max(0.3, timeout_seconds)
        latest = self._review_surface_state(prefer_fast_path=True)
        ambiguous_empty_membership_seen = False
        while True:
            actionable_rows = (
                (latest['approve_count'] > 0 or (latest['row_count'] > 0 and latest.get('review_marker_detected')))
                and not latest.get('contact_info_detected')
            )
            transient_empty_membership_surface = bool(
                latest.get('empty_queue_detected')
                and int(latest.get('membership_request_button_count') or 0) > 0
                and int(latest.get('approve_count') or 0) <= 0
                and int(latest.get('row_count') or 0) <= 0
                and not latest.get('contact_info_detected')
            )
            if actionable_rows:
                return latest
            if latest['empty_queue_detected']:
                if not transient_empty_membership_surface:
                    return latest
                if ambiguous_empty_membership_seen:
                    return latest
                ambiguous_empty_membership_seen = True
                self._page.wait_for_timeout(120)
                latest = self._review_surface_state(prefer_fast_path=False)
                continue
            if time.perf_counter() >= deadline:
                return latest
            self._page.wait_for_timeout(120)
            latest = self._review_surface_state(prefer_fast_path=True)

    def _open_pending_review(self, pending_before: int) -> Dict[str, Any]:
        assert self._page is not None

        def _await_surface(opened_via: str) -> Dict[str, Any]:
            state = self._wait_for_review_surface(timeout_seconds=2.0)
            state['opened_via'] = opened_via
            return state

        def _is_actionable_surface(state: Dict[str, Any]) -> bool:
            if state.get('empty_queue_detected'):
                return True
            if state.get('contact_info_detected'):
                return False
            return bool(
                state.get('approve_count', 0) > 0
                or (state.get('row_count', 0) > 0 and state.get('review_marker_detected'))
            )

        review_text = f'审核{pending_before}请求加入'
        review_locators = [
            self._page.get_by_text(review_text),
            self._page.get_by_text(re.compile(r'审核\s*\d+\s*请求加入')),
        ]
        for review_locator, opened_via in [
            (review_locators[0], 'review_text_exact'),
            (review_locators[1], 'review_text_regex'),
        ]:
            try:
                if not review_locator.count():
                    continue
                target_locator = review_locator.last if hasattr(review_locator, 'last') else review_locator.first
                target_locator.click(timeout=2000)
                self._page.wait_for_timeout(max(self.navigation_wait_ms, 120))
                state = _await_surface(opened_via)
                if _is_actionable_surface(state):
                    return state
            except Exception:
                continue
        try:
            approve_buttons = self._page.locator('[aria-label="批准"]')
            if approve_buttons.count():
                state = self._review_surface_state()
                if not state.get('contact_info_detected'):
                    state['opened_via'] = 'approve_button_already_visible'
                    return state
        except Exception:
            pass
        def _request_join_candidate_definitions() -> list[Dict[str, Any]]:
            return [
                {
                    'source': 'notification_container',
                    'opened_via_prefix': 'request_join_row',
                    'locator_factory': lambda: self._page.locator(
                        '[data-testid="msg-notification-container"] [data-testid="subtype-membership_approval_request"]'
                    ),
                },
                {
                    'source': 'role_button',
                    'opened_via_prefix': 'request_join_role_button',
                    'locator_factory': lambda: self._page.locator(
                        '[data-testid="subtype-membership_approval_request"][role="button"]'
                    ),
                },
                {
                    'source': 'plain_text',
                    'opened_via_prefix': 'request_join_row',
                    'locator_factory': lambda: self._page.get_by_text('请求加入。点击以审核。'),
                },
            ]

        def _resolve_request_join_candidate(candidate_definition: Dict[str, Any]) -> Dict[str, Any]:
            try:
                locator = candidate_definition['locator_factory']()
                count = int(locator.count())
            except Exception:
                locator = None
                count = 0
            return {
                **candidate_definition,
                'locator': locator,
                'count': count,
            }

        def _build_request_join_indices(count: int) -> list[int]:
            preferred_request_join_window = max(1, int(pending_before or 0))
            newest_request_join_start = max(0, count - preferred_request_join_window)
            indices = list(range(count - 1, newest_request_join_start - 1, -1))
            indices.extend(index for index in range(newest_request_join_start - 1, -1, -1) if index not in indices)
            return indices

        request_join_candidate_definitions = _request_join_candidate_definitions()
        request_join_candidates = [
            _resolve_request_join_candidate(candidate_definition)
            for candidate_definition in request_join_candidate_definitions
        ]
        request_join_attempts = 0
        last_request_join_state = None
        empty_request_join_state = None
        for candidate in request_join_candidates:
            candidate_count = int(candidate.get('count') or 0)
            if candidate_count <= 0:
                continue
            for index in _build_request_join_indices(candidate_count):
                try:
                    if request_join_attempts > 0:
                        try:
                            self._group_info_ready = False
                            self._open_group_info()
                        except Exception:
                            pass
                        refreshed_candidate = _resolve_request_join_candidate(candidate)
                        candidate['locator'] = refreshed_candidate.get('locator')
                        candidate['count'] = int(refreshed_candidate.get('count') or 0)
                        candidate_count = int(candidate.get('count') or 0)
                    request_join_attempts += 1
                    request_join_rows = candidate.get('locator')
                    if request_join_rows is None or candidate_count <= 0:
                        continue
                    if index >= candidate_count:
                        continue
                    if hasattr(request_join_rows, 'nth'):
                        request_join_rows.nth(index).click(timeout=2000, force=True)
                    else:
                        target_locator = request_join_rows.last if index == candidate_count - 1 and hasattr(request_join_rows, 'last') else request_join_rows.first
                        target_locator.click(timeout=2000, force=True)
                    self._page.wait_for_timeout(max(self.navigation_wait_ms, 120))
                    state = _await_surface(f"{candidate['opened_via_prefix']}_{index}")
                    last_request_join_state = state
                    if _is_actionable_surface(state) and not state.get('empty_queue_detected'):
                        return state
                    if state.get('empty_queue_detected') and empty_request_join_state is None:
                        empty_request_join_state = state
                except Exception:
                    continue
        if last_request_join_state is not None and _is_actionable_surface(last_request_join_state) and not last_request_join_state.get('empty_queue_detected'):
            return last_request_join_state
        try:
            membership_requests = self._page.locator('[data-testid="subtype-membership_approval_request"]')
            membership_count = membership_requests.count()
        except Exception:
            membership_requests = None
            membership_count = 0
        last_membership_state = None
        empty_membership_state = None
        membership_indices = list(range(membership_count - 1, -1, -1))
        for attempt_index, index in enumerate(membership_indices):
            try:
                if attempt_index > 0:
                    try:
                        self._group_info_ready = False
                        self._open_group_info()
                    except Exception:
                        pass
                    try:
                        membership_requests = self._page.locator('[data-testid="subtype-membership_approval_request"]')
                    except Exception:
                        membership_requests = None
                if membership_requests is None:
                    continue
                membership_requests.nth(index).click(timeout=2000, force=True)
                self._page.wait_for_timeout(max(self.navigation_wait_ms, 120))
                state = _await_surface(f'membership_request_button_{index}')
                last_membership_state = state
                if _is_actionable_surface(state):
                    if state['empty_queue_detected']:
                        if membership_count <= 1:
                            return state
                        if empty_membership_state is None:
                            empty_membership_state = state
                    else:
                        return state
                elif state['empty_queue_detected']:
                    if membership_count <= 1:
                        return state
                    if empty_membership_state is None:
                        empty_membership_state = state
            except Exception:
                continue
        if empty_request_join_state is not None:
            return empty_request_join_state
        if empty_membership_state is not None:
            return empty_membership_state
        if last_membership_state is not None and _is_actionable_surface(last_membership_state):
            return last_membership_state
        try:
            subheader = self._page.locator('[data-testid="conversation-subheader"]')
            if subheader.count():
                subheader.click(timeout=2000)
                self._page.wait_for_timeout(max(self.navigation_wait_ms, 120))
                state = _await_surface('conversation_subheader')
                if _is_actionable_surface(state):
                    return state
        except Exception:
            pass
        state = self._wait_for_review_surface(timeout_seconds=2.0)
        state['opened_via'] = 'surface_poll_timeout'
        return state

    def _wait_for_review_row(self, *, expected_phone: str = '', expected_name: str = ''):
        assert self._page is not None
        deadline = time.perf_counter() + 2.0
        while True:
            try:
                snapshot = self._review_surface_state()
                if snapshot.get('contact_info_detected'):
                    raise RuntimeError('contact info panel is open')
                selection = self._select_review_row_candidate(expected_phone=expected_phone, expected_name=expected_name)
                self._last_review_selection = {
                    'candidate_rows': selection.get('candidate_rows') or [],
                    'selected_candidate': selection.get('selected_candidate') or {},
                    'selection_reason': selection.get('selection_reason') or '',
                }
                return selection['row']
            except AmbiguousReviewTargetError:
                raise
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
                snapshot = self._review_surface_state(prefer_fast_path=True)
                if snapshot.get('empty_queue_detected'):
                    return True
                if snapshot.get('row_count', 0) == 0 and snapshot.get('approve_count', 0) == 0:
                    return True
                body_excerpt = str(snapshot.get('body_excerpt') or '')
                joined_marker_detected = any(marker in body_excerpt for marker in ['已通过邀请链接加入', '通过邀请链接加入'])
                still_pending_marker_detected = any(marker in body_excerpt for marker in ['待处理请求', '请求加入', '点击以审核'])
                if (
                    joined_marker_detected
                    and snapshot.get('approve_count', 0) == 0
                    and not snapshot.get('contact_info_detected')
                    and not still_pending_marker_detected
                ):
                    return True
                if time.perf_counter() >= deadline:
                    return False
                self._page.wait_for_timeout(120)

        def _click_row_approve(timeout_seconds: float = 0.8) -> bool:
            wait_ms = 80
            deadline = time.perf_counter() + max(0.2, timeout_seconds)
            max_attempts = max(1, int((max(0.2, timeout_seconds) * 1000) // wait_ms) + 1)
            attempts = 0
            while True:
                clicked = False
                attempts += 1
                try:
                    row.locator('[aria-label="批准"]').click(timeout=1200, force=True)
                    clicked = True
                    confirm_budget = min(0.25, max(0.1, deadline - time.perf_counter()))
                    if _submission_confirmed(timeout_seconds=confirm_budget):
                        return True
                except Exception:
                    pass
                if time.perf_counter() >= deadline or attempts >= max_attempts:
                    return False
                self._page.wait_for_timeout(120 if clicked else wait_ms)

        if _click_row_approve(timeout_seconds=0.8):
            return

        def _click_global_approve(timeout_seconds: float = 1.5) -> bool:
            wait_ms = 120
            deadline = time.perf_counter() + max(0.3, timeout_seconds)
            max_attempts = max(1, int((max(0.3, timeout_seconds) * 1000) // wait_ms) + 1)
            attempts = 0
            while True:
                clicked = False
                attempts += 1
                try:
                    approve_buttons = self._page.locator('[aria-label="批准"]')
                    if approve_buttons.count():
                        approve_buttons.first.click(timeout=1200, force=True)
                        clicked = True
                        confirm_budget = min(0.25, max(0.1, deadline - time.perf_counter()))
                        if _submission_confirmed(timeout_seconds=confirm_budget):
                            return True
                except Exception:
                    pass
                if time.perf_counter() >= deadline or attempts >= max_attempts:
                    return False
                if clicked:
                    self._page.wait_for_timeout(wait_ms)
                    continue
                self._page.wait_for_timeout(wait_ms)

        if _click_global_approve(timeout_seconds=0.8):
            return
        row.click(timeout=1200, force=True)
        snapshot_after_row_click = self._review_surface_state(prefer_fast_path=True)
        if snapshot_after_row_click.get('contact_info_detected') and snapshot_after_row_click.get('approve_count', 0) <= 0:
            raise ReviewSurfaceRecoveryRequired(
                'contact info opened after row click; review surface must be reopened'
            )
        if _click_row_approve(timeout_seconds=0.6):
            return
        if _click_global_approve(timeout_seconds=2.0):
            return
        snapshot = self._review_surface_state()
        raise PlaywrightTimeoutError(
            f'approve action unavailable after review row opened; '
            f'row_count={snapshot.get("row_count", 0)} approve_count={snapshot.get("approve_count", 0)}'
        )

    def group_state(self, registration_group: Optional[str] = None) -> Dict[str, Any]:
        return self._call_on_owner_thread(lambda: self._group_state_impl(registration_group))

    def _group_state_impl(self, registration_group: Optional[str] = None) -> Dict[str, Any]:
        target_group_name = str(registration_group or self.registration_group_name or '').strip()
        started = time.perf_counter()
        with self._approval_lock:
            self._ensure_browser()
            self._open_group_info(target_group_name=target_group_name, allow_index_fallback=False)
            snapshot = self._snapshot_group_state()
            review_surface = self._review_surface_state(prefer_fast_path=True)
            pending_count = int(snapshot.get('pending_count') or 0)
            explicit_zero_confirmation = bool(review_surface.get('empty_queue_visible') or review_surface.get('zero_pending_verified_by'))
            if pending_count <= 0 and not explicit_zero_confirmation:
                try:
                    opened_review_surface = self._open_pending_review(max(pending_count, 1))
                    if isinstance(opened_review_surface, dict) and opened_review_surface:
                        review_surface = {**review_surface, **opened_review_surface}
                except Exception:
                    pass
            review_surface_ready = bool(review_surface.get('review_surface_ready'))
            has_pending_section = bool(review_surface.get('has_pending_section'))
            has_pending_request_row = bool(review_surface.get('has_pending_request_row'))
            empty_queue_visible = bool(review_surface.get('empty_queue_visible'))
            zero_pending_verified_by = review_surface.get('zero_pending_verified_by')
            zero_pending_unverified = bool(review_surface.get('zero_pending_unverified'))
            review_surface_pending_count = int(review_surface.get('pending_count') or 0) if review_surface.get('pending_count') is not None else 0
            if review_surface_pending_count > 0:
                pending_count = review_surface_pending_count
            if pending_count <= 0 and not zero_pending_unverified:
                confirmed_zero = review_surface_ready and (empty_queue_visible or bool(zero_pending_verified_by))
                if not confirmed_zero:
                    zero_pending_unverified = True
            payload = {
                'group_name': target_group_name or self.registration_group_name,
                'pending_count': pending_count,
                'member_count': snapshot.get('member_count'),
                'requester_ids': [],
                'status': 'ok',
                'result_code': 'live_review_surface_ok',
                'result_reason': '',
                'verification_source': 'live_review_surface',
                'body_excerpt': snapshot.get('body_excerpt') or '',
                'contact_info_detected': bool(snapshot.get('contact_info_detected')),
                'empty_queue_detected': bool(snapshot.get('empty_queue_detected')),
                'review_surface_ready': review_surface_ready,
                'has_pending_section': has_pending_section,
                'has_pending_request_row': has_pending_request_row,
                'empty_queue_visible': empty_queue_visible,
                'zero_pending_unverified': zero_pending_unverified,
                'zero_pending_verified_by': zero_pending_verified_by,
                'review_surface_state': review_surface,
                'checked_at': datetime.now(timezone.utc).isoformat(),
                'elapsed_seconds': round(time.perf_counter() - started, 3),
            }
            if zero_pending_unverified:
                payload['zero_pending_unverified_reason'] = 'review_surface_zero_not_confirmed'
            if review_surface.get('candidate_rows'):
                payload['requesters'] = [
                    {
                        'display_name': str(row.get('display_name') or '').strip(),
                        'phones': list(row.get('phones') or []),
                        'actionable': bool(row.get('actionable')),
                    }
                    for row in list(review_surface.get('candidate_rows') or [])
                ]
            else:
                pending_candidates = self._extract_pending_candidates(str(snapshot.get('body_excerpt') or ''))
                payload['requesters'] = [
                    {
                        'display_name': value,
                        'phones': [],
                        'actionable': True,
                    }
                    for value in list(pending_candidates.get('requesters') or [])
                ]
            return payload

    def approve(self, context: Dict[str, Any]) -> Dict[str, Any]:
        return self._call_on_owner_thread(lambda: self._approve_impl(context))

    def _approve_impl(self, context: Dict[str, Any]) -> Dict[str, Any]:
        started = time.perf_counter()
        stage_marks: Dict[str, float] = {}
        approval_run_id = str(context.get('approval_run_id') or '').strip() or f"registration_group_approval_{int(time.time())}"
        start_snapshot: Dict[str, Any] = {}
        pending_before: Optional[int] = None
        member_before: Optional[int] = None
        row_text = ''
        target_phone = ''
        target_phone_raw = ''
        target_name = ''
        candidate_rows: list[Dict[str, Any]] = []
        selected_candidate: Dict[str, Any] = {}
        selection_reason = ''
        target_confirmation_hint: Dict[str, Any] = {}
        with self._approval_lock:
            try:
                self._ensure_browser()
                stage_marks['browser_ready_seconds'] = round(time.perf_counter() - started, 3)
                self._open_group_info()
                stage_marks['group_info_ready_seconds'] = round(time.perf_counter() - started, 3)
                assert self._page is not None
                body_before = self._capture_group_info_body(wait_for_pending_seconds=1.2)
                pending_before = self._extract_pending_count(body_before)
                member_before = self._extract_member_count(body_before)
                pending_candidates = self._extract_pending_candidates(body_before)
                start_snapshot = {
                    'pending_count': pending_before,
                    'member_count': member_before,
                    'pending_candidates': pending_candidates,
                    'body_excerpt': body_before[-2500:],
                }
                expected_phone = self._normalize_phone(context.get('target_phone_hint') or '') or (pending_candidates['phones'][0] if pending_candidates.get('phones') else '')
                expected_name = str(context.get('target_name_hint') or '').strip() or (pending_candidates['requesters'][0] if pending_candidates.get('requesters') else '')
                self._group_info_ready = True
                if pending_before <= 0 and '群组信息' in body_before and '没有要审核的成员' not in body_before:
                    self._group_info_ready = False
                    self._open_group_info()
                    body_before = self._capture_group_info_body(wait_for_pending_seconds=2.5)
                    pending_before = self._extract_pending_count(body_before)
                    member_before = self._extract_member_count(body_before)
                    pending_candidates = self._extract_pending_candidates(body_before)
                    start_snapshot = {
                        'pending_count': pending_before,
                        'member_count': member_before,
                        'pending_candidates': pending_candidates,
                        'body_excerpt': body_before[-2500:],
                    }
                    expected_phone = self._normalize_phone(context.get('target_phone_hint') or '') or (pending_candidates['phones'][0] if pending_candidates.get('phones') else '')
                    expected_name = str(context.get('target_name_hint') or '').strip() or (pending_candidates['requesters'][0] if pending_candidates.get('requesters') else '')
                    self._group_info_ready = True
                review_surface = None
                if pending_before <= 0:
                    should_probe_review_surface = bool(expected_phone or expected_name)
                    if should_probe_review_surface:
                        review_surface_retry_attempted = False
                        try:
                            review_surface = self._open_pending_review(1)
                        except Exception:
                            review_surface = None
                        actionable_review_surface = bool(
                            review_surface
                            and not review_surface.get('empty_queue_detected')
                            and (
                                int(review_surface.get('approve_count', 0) or 0) > 0
                                or int(review_surface.get('row_count', 0) or 0) > 0
                            )
                        )
                        stale_empty_review_surface = bool(
                            review_surface
                            and review_surface.get('empty_queue_detected')
                            and int(review_surface.get('membership_request_button_count', 0) or 0) > 0
                            and (
                                review_surface.get('review_marker_detected')
                                or REQUEST_JOIN_ROW_PATTERN.search(body_before or '')
                                or REQUEST_JOIN_ROW_PATTERN.search(str(review_surface.get('body_excerpt') or ''))
                            )
                        )
                        if not actionable_review_surface and stale_empty_review_surface:
                            review_surface_retry_attempted = True
                            self._group_info_ready = False
                            self._open_group_info()
                            body_before = self._capture_group_info_body(wait_for_pending_seconds=2.5)
                            pending_before = self._extract_pending_count(body_before)
                            member_before = self._extract_member_count(body_before)
                            pending_candidates = self._extract_pending_candidates(body_before)
                            start_snapshot = {
                                'pending_count': pending_before,
                                'member_count': member_before,
                                'pending_candidates': pending_candidates,
                                'body_excerpt': body_before[-2500:],
                            }
                            expected_phone = self._normalize_phone(context.get('target_phone_hint') or '') or (pending_candidates['phones'][0] if pending_candidates.get('phones') else '')
                            expected_name = str(context.get('target_name_hint') or '').strip() or (pending_candidates['requesters'][0] if pending_candidates.get('requesters') else '')
                            self._group_info_ready = True
                            try:
                                review_surface = self._open_pending_review(1)
                            except Exception:
                                review_surface = None
                            actionable_review_surface = bool(
                                review_surface
                                and not review_surface.get('empty_queue_detected')
                                and (
                                    int(review_surface.get('approve_count', 0) or 0) > 0
                                    or int(review_surface.get('row_count', 0) or 0) > 0
                                )
                            )
                        if actionable_review_surface:
                            pending_before = 1
                            start_snapshot['pending_count'] = 1
                            if not start_snapshot.get('pending_candidates'):
                                start_snapshot['pending_candidates'] = {
                                    'phones': [expected_phone] if expected_phone else [],
                                    'requesters': [expected_name] if expected_name else [],
                                }
                        else:
                            finished_at = datetime.now(timezone.utc).isoformat()
                            return {
                                'status': 'failed',
                                'verified': False,
                                'result_code': 'no_pending_request',
                                'result_reason': 'no pending request in registration group',
                                'finished_at': finished_at,
                                'elapsed_seconds': round(time.perf_counter() - started, 2),
                                'raw_result': {
                                    'approval_run_id': approval_run_id,
                                    'start_snapshot': start_snapshot,
                                    'pending_before': pending_before,
                                    'member_count_before': member_before,
                                    'body_excerpt': body_before[-2000:],
                                    'review_surface': review_surface,
                                    'review_surface_retry_attempted': review_surface_retry_attempted,
                                    'stage_timings': dict(stage_marks),
                                },
                            }
                    else:
                        finished_at = datetime.now(timezone.utc).isoformat()
                        return {
                            'status': 'failed',
                            'verified': False,
                            'result_code': 'no_pending_request',
                            'result_reason': 'no pending request in registration group',
                            'finished_at': finished_at,
                            'elapsed_seconds': round(time.perf_counter() - started, 2),
                            'raw_result': {
                                'approval_run_id': approval_run_id,
                                'start_snapshot': start_snapshot,
                                'pending_before': pending_before,
                                'member_count_before': member_before,
                                'body_excerpt': body_before[-2000:],
                                'stage_timings': dict(stage_marks),
                            },
                        }
                if review_surface is None:
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
                            'approval_run_id': approval_run_id,
                            'start_snapshot': start_snapshot,
                            'pending_before': pending_before,
                            'member_count_before': member_before,
                            'review_surface': review_surface,
                            'body_excerpt': body_before[-2000:],
                            'stage_timings': dict(stage_marks),
                        },
                    }
                recovery_attempted = False
                recovery_snapshot = None
                for attempt in range(2):
                    try:
                        row = self._wait_for_review_row(expected_phone=expected_phone, expected_name=expected_name)
                        selection = dict(self._last_review_selection or {})
                        candidate_rows = list(selection.get('candidate_rows') or [])
                        selected_candidate = dict(selection.get('selected_candidate') or {})
                        selection_reason = str(selection.get('selection_reason') or '')
                        if not self._review_candidates_match_expected_pending(
                            candidate_rows=candidate_rows,
                            pending_candidates=pending_candidates,
                            expected_phone=expected_phone,
                            expected_name=expected_name,
                        ):
                            raise StaleReviewSurfaceError('review surface candidates do not match current pending candidates')
                        if attempt == 0:
                            stage_marks['review_row_ready_seconds'] = round(time.perf_counter() - started, 3)
                        else:
                            stage_marks['review_row_recovered_seconds'] = round(time.perf_counter() - started, 3)
                        row_text = row.inner_text(timeout=300).strip()
                        phone_matches = self._extract_all_phones(row_text)
                        target_phone = phone_matches[0] if phone_matches else self._normalize_phone(expected_phone)
                        target_phone_raw = target_phone or row_text.splitlines()[0].strip()
                        selected_display_name = str((selected_candidate or {}).get('display_name') or '').strip()
                        target_confirmation_hint = {
                            'selection_reason': selection_reason,
                            'selected_candidate': dict(selected_candidate or {}),
                        }
                        row_lines = [line.strip() for line in row_text.splitlines() if line.strip()]
                        derived_row_name = ''
                        for line in row_lines:
                            if line == target_phone_raw:
                                continue
                            if self._normalize_phone(line):
                                continue
                            if line.startswith('由') and ('添加' in line or 'invite' in line.lower()):
                                continue
                            derived_row_name = line
                            break
                        pushname = self._page.locator('[data-testid="pushname"]')
                        pushname_text = pushname.first.inner_text(timeout=300).strip() if pushname.count() else ''
                        target_name = selected_display_name or derived_row_name or expected_name or pushname_text or (row_lines[0] if row_lines else '')
                        self._click_approve_action(row)
                        break
                    except AmbiguousReviewTargetError:
                        raise
                    except StaleReviewSurfaceError:
                        if attempt > 0:
                            raise
                        recovery_attempted = True
                        try:
                            recovery_snapshot = self._review_surface_state()
                        except Exception:
                            recovery_snapshot = None
                    except ReviewSurfaceRecoveryRequired:
                        if attempt > 0:
                            raise
                        recovery_attempted = True
                        try:
                            recovery_snapshot = self._review_surface_state()
                        except Exception:
                            recovery_snapshot = None
                    except PlaywrightTimeoutError:
                        if attempt > 0:
                            raise
                        try:
                            recovery_snapshot = self._review_surface_state()
                        except Exception:
                            recovery_snapshot = None
                        if not (recovery_snapshot or {}).get('contact_info_detected'):
                            raise
                        recovery_attempted = True
                    self._group_info_ready = False
                    self._open_group_info()
                    review_surface = self._open_pending_review(max(pending_before, 1))
                    stage_marks['review_surface_recovered_seconds'] = round(time.perf_counter() - started, 3)
                    if review_surface.get('empty_queue_detected'):
                        raise PlaywrightTimeoutError('review surface reopened after contact info fallback but queue was empty')
                stage_marks['approve_clicked_seconds'] = round(time.perf_counter() - started, 3)
                self._page.wait_for_timeout(max(self.post_click_wait_ms, 100))
                verification = self._same_session_verify(
                    target_phone=target_phone,
                    pending_before=pending_before,
                    target_confirmation_hint=target_confirmation_hint,
                )
                retry_attempted = False
                retry_succeeded = False
                retry_snapshot = None
                delayed_verification_attempted = False
                delayed_verification_snapshot = None
                if not verification['queue_delta']:
                    try:
                        retry_snapshot = self._review_surface_state()
                    except Exception:
                        retry_snapshot = None
                    if retry_snapshot and (retry_snapshot.get('row_count', 0) > 0 or retry_snapshot.get('approve_count', 0) > 0):
                        retry_attempted = True
                        self._click_approve_action(row)
                        self._page.wait_for_timeout(max(self.post_click_wait_ms, 100))
                        verification = self._same_session_verify(
                            target_phone=target_phone,
                            pending_before=pending_before,
                            target_confirmation_hint=target_confirmation_hint,
                        )
                        retry_succeeded = bool(verification.get('queue_delta'))
                should_attempt_delayed_verification = bool(
                    (verification.get('queue_delta') and not verification.get('member_confirmed'))
                    or (verification.get('member_confirmed') and not verification.get('queue_delta'))
                )
                if should_attempt_delayed_verification:
                    delayed_verification_attempted = True
                    try:
                        reuse_current_surface = False
                        try:
                            pending_after_value = int(verification.get('pending_count') or 0)
                        except Exception:
                            pending_after_value = None
                        if not verification.get('contact_info_detected'):
                            if verification.get('empty_queue_detected') or pending_after_value == 0:
                                reuse_current_surface = True
                            elif self._group_info_ready and self._page_ready_for_approval() and verification.get('queue_delta'):
                                reuse_current_surface = True
                        if not reuse_current_surface:
                            self._group_info_ready = False
                            self._open_group_info()
                        delayed_verification_snapshot = self._same_session_verify(
                            target_phone=target_phone,
                            pending_before=pending_before,
                            target_confirmation_hint=target_confirmation_hint,
                        )
                    except Exception:
                        delayed_verification_snapshot = None
                    if delayed_verification_snapshot and delayed_verification_snapshot.get('queue_delta') and delayed_verification_snapshot.get('member_confirmed'):
                        verification = delayed_verification_snapshot
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
                    'member_confirmation_source': verification.get('member_confirmation_source') or '',
                    'target_member': {
                        'name': target_name,
                        'phone_raw': target_phone_raw,
                        'phone_normalized': target_phone,
                    },
                    'raw_result': {
                        'approval_run_id': approval_run_id,
                        'start_snapshot': start_snapshot,
                        'pending_before': pending_before,
                        'member_count_before': member_before,
                        'pending_after': verification['pending_count'],
                        'member_count_after': verification['member_count'],
                        'verification_snapshot': dict(verification),
                        'all_phones_normalized': verification['all_phones_normalized'],
                        'member_confirmation_source': verification.get('member_confirmation_source') or '',
                        'expected_phone': expected_phone,
                        'expected_name': expected_name,
                        'verification_excerpt': verification['body_excerpt'],
                        'row_text_excerpt': row_text[-800:],
                        'candidate_rows': candidate_rows,
                        'selected_candidate': selected_candidate,
                        'selection_reason': selection_reason,
                        'review_surface': review_surface,
                        'review_surface_recovery_attempted': recovery_attempted,
                        'review_surface_recovery_snapshot': recovery_snapshot,
                        'retry_attempted': retry_attempted,
                        'retry_succeeded': retry_succeeded,
                        'retry_snapshot': retry_snapshot,
                        'delayed_verification_attempted': delayed_verification_attempted,
                        'delayed_verification_snapshot': delayed_verification_snapshot,
                        'stage_timings': dict(stage_marks),
                    },
                }
                return result
            except AmbiguousReviewTargetError as exc:
                finished_at = datetime.now(timezone.utc).isoformat()
                selection = dict(self._last_review_selection or {})
                candidate_rows = list(selection.get('candidate_rows') or candidate_rows or [])
                selected_candidate = dict(selection.get('selected_candidate') or selected_candidate or {})
                selection_reason = str(selection.get('selection_reason') or selection_reason or '')
                return {
                    'status': 'failed',
                    'verified': False,
                    'result_code': 'ambiguous_review_target',
                    'result_reason': str(exc),
                    'finished_at': finished_at,
                    'elapsed_seconds': round(time.perf_counter() - started, 2),
                    'target_member': {
                        'name': target_name,
                        'phone_raw': target_phone_raw,
                        'phone_normalized': target_phone,
                    },
                    'raw_result': {
                        'approval_run_id': approval_run_id,
                        'start_snapshot': start_snapshot,
                        'pending_before': pending_before,
                        'member_count_before': member_before,
                        'candidate_rows': candidate_rows,
                        'selected_candidate': selected_candidate,
                        'selection_reason': selection_reason,
                        'stage_timings': dict(stage_marks),
                    },
                }
            except StaleReviewSurfaceError as exc:
                finished_at = datetime.now(timezone.utc).isoformat()
                selection = dict(self._last_review_selection or {})
                candidate_rows = list(selection.get('candidate_rows') or candidate_rows or [])
                selected_candidate = dict(selection.get('selected_candidate') or selected_candidate or {})
                selection_reason = str(selection.get('selection_reason') or selection_reason or '')
                return {
                    'status': 'failed',
                    'verified': False,
                    'result_code': 'stale_review_surface',
                    'result_reason': str(exc),
                    'finished_at': finished_at,
                    'elapsed_seconds': round(time.perf_counter() - started, 2),
                    'target_member': {
                        'name': target_name,
                        'phone_raw': target_phone_raw,
                        'phone_normalized': target_phone,
                    },
                    'raw_result': {
                        'approval_run_id': approval_run_id,
                        'start_snapshot': start_snapshot,
                        'pending_before': pending_before,
                        'member_count_before': member_before,
                        'candidate_rows': candidate_rows,
                        'selected_candidate': selected_candidate,
                        'selection_reason': selection_reason,
                        'stage_timings': dict(stage_marks),
                    },
                }
            except PlaywrightTimeoutError as exc:
                timeout_snapshot = {}
                verification_snapshot: Dict[str, Any] = {}
                try:
                    timeout_snapshot = self._review_surface_state()
                except Exception:
                    timeout_snapshot = {}
                try:
                    assert self._page is not None
                    timeout_body = self._page.locator('body').inner_text(timeout=1200)
                    pending_after = self._extract_pending_count(timeout_body)
                    member_after = self._extract_member_count(timeout_body)
                    all_phones = self._extract_all_phones(timeout_body)
                    verification_snapshot = {
                        'pending_count': pending_after,
                        'member_count': member_after,
                        'all_phones_normalized': all_phones,
                        'body_excerpt': timeout_body[-2500:],
                        'queue_delta': bool(
                            pending_before is not None
                            and pending_after is not None
                            and int(pending_after) < int(pending_before)
                        ),
                        'member_confirmed': bool(target_phone and target_phone in all_phones),
                        'member_confirmation_source': 'body_phone_match' if bool(target_phone and target_phone in all_phones) else '',
                    }
                    if verification_snapshot['queue_delta'] and not verification_snapshot['member_confirmed'] and self._selected_candidate_exact_phone_match(
                        target_phone=target_phone,
                        target_confirmation_hint=target_confirmation_hint,
                    ):
                        verification_snapshot['member_confirmed'] = True
                        verification_snapshot['member_confirmation_source'] = 'selected_candidate_exact_phone_match'
                except Exception:
                    verification_snapshot = {}
                self._reset_browser(f'playwright_timeout:{exc}')
                finished_at = datetime.now(timezone.utc).isoformat()
                queue_delta = bool(verification_snapshot.get('queue_delta'))
                member_confirmed = bool(verification_snapshot.get('member_confirmed'))
                verified = bool(queue_delta and member_confirmed)
                return {
                    'status': 'success' if verified else 'failed',
                    'verified': verified,
                    'result_code': 'approved' if verified else 'playwright_timeout',
                    'result_reason': 'queue delta and member confirmation verified after timeout salvage' if verified else str(exc),
                    'finished_at': finished_at,
                    'approved_at': finished_at if verified else None,
                    'approved_count': int(context.get('approved_count') or 1),
                    'elapsed_seconds': round(time.perf_counter() - started, 2),
                    'queue_delta': queue_delta,
                    'member_confirmed': member_confirmed,
                    'member_confirmation_source': verification_snapshot.get('member_confirmation_source') or '',
                    'target_member': {
                        'name': target_name,
                        'phone_raw': target_phone_raw,
                        'phone_normalized': target_phone,
                    },
                    'raw_result': {
                        'approval_run_id': approval_run_id,
                        'start_snapshot': start_snapshot,
                        'timeout_snapshot': timeout_snapshot,
                        'pending_before': pending_before,
                        'member_count_before': member_before,
                        'pending_after': verification_snapshot.get('pending_count'),
                        'member_count_after': verification_snapshot.get('member_count'),
                        'verification_snapshot': verification_snapshot,
                        'all_phones_normalized': verification_snapshot.get('all_phones_normalized') or [],
                        'member_confirmation_source': verification_snapshot.get('member_confirmation_source') or '',
                        'verification_excerpt': verification_snapshot.get('body_excerpt') or timeout_snapshot.get('body_excerpt', ''),
                        'row_text_excerpt': row_text[-800:],
                        'candidate_rows': candidate_rows,
                        'selected_candidate': selected_candidate,
                        'selection_reason': selection_reason,
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
                        'approval_run_id': approval_run_id,
                        'start_snapshot': start_snapshot,
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

    def group_state(self, registration_group: str) -> Dict[str, Any]:
        normalized_group = str(registration_group or '').strip()
        if not normalized_group:
            return {
                'group_name': '',
                'pending_count': None,
                'member_count': None,
                'requester_ids': [],
                'status': 'failed',
                'result_code': 'registration_group_missing',
                'result_reason': 'registration_group is required',
            }
        executor = self._get_executor(normalized_group)
        result = executor.group_state(normalized_group)
        if isinstance(result, dict):
            result.setdefault('group_name', normalized_group)
        return result

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

