import sys
import types
from pathlib import Path


class _BootstrapFakeChromium:
    def launch_persistent_context(self, *args, **kwargs):
        raise AssertionError('test should monkeypatch sync_playwright before launch')


class _BootstrapFakePlaywright:
    def __init__(self):
        self.chromium = _BootstrapFakeChromium()

    def stop(self):
        return None


class _BootstrapSyncPlaywright:
    def start(self):
        return _BootstrapFakePlaywright()


fake_sync_api = types.ModuleType('playwright.sync_api')
fake_sync_api.TimeoutError = RuntimeError
fake_sync_api.sync_playwright = lambda: _BootstrapSyncPlaywright()
fake_playwright = types.ModuleType('playwright')
fake_playwright.sync_api = fake_sync_api
sys.modules.setdefault('playwright', fake_playwright)
sys.modules.setdefault('playwright.sync_api', fake_sync_api)

from app.registration_group_executor import LiveWarmWhatsAppRegistrationGroupApprovalExecutor


class _FakePage:
    def goto(self, *args, **kwargs):
        return None

    def wait_for_timeout(self, *args, **kwargs):
        return None


class _FakeContext:
    def __init__(self):
        self.pages = [_FakePage()]

    def new_page(self):
        return _FakePage()

    def close(self):
        return None


class _FakeChromium:
    def __init__(self):
        self.launch_calls = 0

    def launch_persistent_context(self, *args, **kwargs):
        self.launch_calls += 1
        if self.launch_calls == 1:
            raise RuntimeError(
                'BrowserType.launch_persistent_context: Failed to create a ProcessSingleton for your profile directory. '
                'Failed to create /tmp/chrome-whatsapp-registration-group-approval/SingletonLock: File exists (17)'
            )
        return _FakeContext()


class _FakePlaywright:
    def __init__(self):
        self.chromium = _FakeChromium()

    def stop(self):
        return None


class _FakeSyncPlaywright:
    def __init__(self):
        self.instance = _FakePlaywright()

    def start(self):
        return self.instance


def test_warmup_recovers_from_stale_process_singleton(monkeypatch, tmp_path):
    chrome_root = tmp_path / 'chrome-root'
    profile_dir = chrome_root / 'Profile 25'
    profile_dir.mkdir(parents=True)
    (chrome_root / 'Local State').write_text('{}')
    (profile_dir / 'Preferences').write_text('{}')

    fake_sync = _FakeSyncPlaywright()
    monkeypatch.setattr('app.registration_group_executor.sync_playwright', lambda: fake_sync)

    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(
        chrome_user_data_root=str(chrome_root),
        profile_dir='Profile 25',
        temp_user_data_dir=str(tmp_path / 'temp-profile'),
        initial_wait_ms=0,
        navigation_wait_ms=0,
        post_click_wait_ms=0,
        verify_timeout_ms=300,
        verify_poll_ms=50,
    )

    health = executor.warmup()

    assert health['status'] == 'warm'
    assert health['last_error'] is None
    assert executor._active_temp_user_data_dir is not None
    assert Path(executor._active_temp_user_data_dir).exists()
    assert Path(executor._active_temp_user_data_dir).name.startswith('temp-profile-')


def test_allocate_run_temp_user_data_dir_creates_unique_sibling_dirs(tmp_path):
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(
        temp_user_data_dir=str(tmp_path / 'temp-profile'),
    )

    first = executor._allocate_run_temp_user_data_dir()
    second = executor._allocate_run_temp_user_data_dir()

    assert first != second
    assert first.parent == second.parent == tmp_path
    assert first.name.startswith('temp-profile-')
    assert second.name.startswith('temp-profile-')
    assert first.exists()
    assert second.exists()


class _LoggedOutPage(_FakePage):
    def get_by_text(self, text, exact=False):
        if (text, exact) in {
            ('扫描登录', False),
            ('使用电话号码登录', False),
            ('开始使用', False),
        }:
            return _PollingLocator([1])
        return _PollingLocator([0])

    def locator(self, selector, *args, **kwargs):
        return _PollingLocator([0])


class _LoggedOutContext:
    def __init__(self):
        self.pages = [_LoggedOutPage()]

    def new_page(self):
        return _LoggedOutPage()

    def close(self):
        return None


class _LoggedOutChromium:
    def launch_persistent_context(self, *args, **kwargs):
        return _LoggedOutContext()


class _LoggedOutPlaywright:
    def __init__(self):
        self.chromium = _LoggedOutChromium()

    def stop(self):
        return None


class _LoggedOutSyncPlaywright:
    def start(self):
        return _LoggedOutPlaywright()


def test_warmup_does_not_report_warm_when_whatsapp_session_is_logged_out(monkeypatch, tmp_path):
    chrome_root = tmp_path / 'chrome-root'
    profile_dir = chrome_root / 'Profile 25'
    profile_dir.mkdir(parents=True)
    (chrome_root / 'Local State').write_text('{}')
    (profile_dir / 'Preferences').write_text('{}')

    monkeypatch.setattr('app.registration_group_executor.sync_playwright', lambda: _LoggedOutSyncPlaywright())

    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(
        chrome_user_data_root=str(chrome_root),
        profile_dir='Profile 25',
        temp_user_data_dir=str(tmp_path / 'temp-profile'),
        initial_wait_ms=0,
        navigation_wait_ms=0,
        post_click_wait_ms=0,
        verify_timeout_ms=300,
        verify_poll_ms=50,
    )

    health = executor.warmup()

    assert health['status'] == 'idle'
    assert 'logged_out' in str(health['last_error'])


def test_ensure_browser_rejects_existing_logged_out_page(monkeypatch, tmp_path):
    chrome_root = tmp_path / 'chrome-root'
    profile_dir = chrome_root / 'Profile 25'
    profile_dir.mkdir(parents=True)
    (chrome_root / 'Local State').write_text('{}')
    (profile_dir / 'Preferences').write_text('{}')

    monkeypatch.setattr('app.registration_group_executor.sync_playwright', lambda: _LoggedOutSyncPlaywright())

    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(
        chrome_user_data_root=str(chrome_root),
        profile_dir='Profile 25',
        temp_user_data_dir=str(tmp_path / 'temp-profile'),
        initial_wait_ms=0,
        navigation_wait_ms=0,
        post_click_wait_ms=0,
        verify_timeout_ms=300,
        verify_poll_ms=50,
    )
    executor._context = _LoggedOutContext()
    executor._page = executor._context.pages[0]
    executor._warm = True

    try:
        executor._ensure_browser()
        raised = None
    except RuntimeError as exc:
        raised = exc

    assert raised is not None
    assert 'logged_out' in str(raised)
    assert executor.health()['status'] == 'idle'


def test_ensure_browser_recreates_context_when_owner_thread_differs(monkeypatch, tmp_path):
    chrome_root = tmp_path / 'chrome-root'
    profile_dir = chrome_root / 'Profile 25'
    profile_dir.mkdir(parents=True)
    (chrome_root / 'Local State').write_text('{}')
    (profile_dir / 'Preferences').write_text('{}')

    fake_sync = _FakeSyncPlaywright()
    monkeypatch.setattr('app.registration_group_executor.sync_playwright', lambda: fake_sync)

    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(
        chrome_user_data_root=str(chrome_root),
        profile_dir='Profile 25',
        temp_user_data_dir=str(tmp_path / 'temp-profile'),
        initial_wait_ms=0,
        navigation_wait_ms=0,
        post_click_wait_ms=0,
        verify_timeout_ms=300,
        verify_poll_ms=50,
    )
    executor._context = _LoggedOutContext()
    executor._page = executor._context.pages[0]
    executor._active_temp_user_data_dir = str(tmp_path / 'temp-profile-stale')
    Path(executor._active_temp_user_data_dir).mkdir(parents=True)
    executor._owner_thread_id = -1
    executor._warm = True

    executor._ensure_browser()

    assert executor._owner_thread_id == __import__('threading').get_ident()
    assert executor.health()['status'] == 'warm'
    assert executor._active_temp_user_data_dir is not None
    assert Path(executor._active_temp_user_data_dir).name.startswith('temp-profile-')


class _PollingLocator:
    def __init__(self, counts):
        self.counts = list(counts)
        self.clicks = 0

    @property
    def first(self):
        return self

    def count(self):
        if len(self.counts) > 1:
            return self.counts.pop(0)
        return self.counts[0]

    def click(self, **kwargs):
        self.clicks += 1
        return None


class _EnterGroupsPage:
    def __init__(self, group_locator, text_locator):
        self.group_locator = group_locator
        self.text_locator = text_locator
        self.waits = []

    def get_by_role(self, *args, **kwargs):
        return self.group_locator

    def get_by_text(self, *args, **kwargs):
        return self.text_locator

    def wait_for_timeout(self, value):
        self.waits.append(value)
        return None


def test_enter_groups_tab_polls_until_group_tab_is_ready():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(navigation_wait_ms=0)
    group_locator = _PollingLocator([0, 1])
    text_locator = _PollingLocator([0, 0])
    executor._page = _EnterGroupsPage(group_locator, text_locator)

    executor._enter_groups_tab()

    assert group_locator.clicks == 1
    assert executor._page.waits


class _ReadyPage:
    def __init__(self, counts=None, locator_counts=None):
        self.counts = dict(counts or {})
        self.locator_counts = dict(locator_counts or {})
        self.locator_clicks = 0

    def get_by_text(self, text, exact=False):
        return _PollingLocator([self.counts.get((text, exact), 0)])

    def get_by_role(self, *args, **kwargs):
        return _PollingLocator([0])

    def locator(self, selector, *args, **kwargs):
        if selector in self.locator_counts:
            return _PollingLocator([self.locator_counts[selector]])
        class _Clickable:
            def __init__(self, page):
                self.page = page
            def click(self, **kwargs):
                self.page.locator_clicks += 1
                return None
        return _Clickable(self)

    def wait_for_timeout(self, value):
        return None


def test_open_group_info_skips_navigation_when_cached_page_is_ready():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(navigation_wait_ms=0)
    executor._group_info_ready = True
    executor._page = _ReadyPage({('群组信息', True): 1})

    executor._open_group_info()

    assert executor._page.locator_clicks == 0


class _OpenGroupInfoRetryPage:
    def __init__(self):
        self.header_clicks = 0
        self.subheader_clicks = 0
        self.waits = []

    def get_by_text(self, text, exact=False):
        if text == '群组信息' and exact:
            return _PollingLocator([1 if self.subheader_clicks else 0])
        if text == '待处理请求' and exact:
            return _PollingLocator([1 if self.subheader_clicks else 0])
        if text == '请求加入。点击以审核。':
            return _PollingLocator([1 if self.subheader_clicks else 0])
        if text == '联系人信息' and exact:
            return _PollingLocator([1 if self.header_clicks and not self.subheader_clicks else 0])
        return _PollingLocator([0])

    def get_by_role(self, *args, **kwargs):
        return _PollingLocator([0])

    def locator(self, selector, *args, **kwargs):
        page = self

        class _Clickable:
            def __init__(self, kind):
                self.kind = kind

            @property
            def first(self):
                return self

            def count(self):
                return 1

            def click(self, **kwargs):
                if self.kind == 'header':
                    page.header_clicks += 1
                elif self.kind == 'subheader':
                    page.subheader_clicks += 1
                return None

        if selector == '[data-testid="chat-list"] [data-testid="list-item-0"]':
            return _Clickable('list_item')
        if selector == '[data-testid="conversation-header"]':
            return _Clickable('header')
        if selector == '[data-testid="conversation-subheader"]':
            return _Clickable('subheader')
        if selector == '[data-testid="subtype-membership_approval_request"]':
            return _PollingLocator([0])
        return _Clickable('other')

    def wait_for_timeout(self, value):
        self.waits.append(value)
        return None


def test_open_group_info_retries_via_subheader_when_header_opens_contact_panel():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(navigation_wait_ms=0)
    executor._page = _OpenGroupInfoRetryPage()
    executor._enter_groups_tab = lambda: None

    executor._open_group_info()

    assert executor._page.header_clicks == 1
    assert executor._page.subheader_clicks == 1
    assert executor._group_info_ready is True


def test_page_ready_for_approval_accepts_chat_surface_membership_request_buttons():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(navigation_wait_ms=0)
    executor._page = _ReadyPage(locator_counts={'[data-testid="subtype-membership_approval_request"]': 2})

    assert executor._page_ready_for_approval() is True


class _PendingReviewPage:
    def __init__(self, *, has_review_text=0, approve_count=0, subheader_count=0, review_click_raises=False):
        self.has_review_text = has_review_text
        self.approve_count = approve_count
        self.subheader_count = subheader_count
        self.review_click_raises = review_click_raises
        self.review_clicks = 0
        self.subheader_clicks = 0
        self.waits = []

    def get_by_text(self, text, exact=False):
        locator = _PollingLocator([self.has_review_text if text.startswith('审核') else 0])
        original_click = locator.click
        def _click(**kwargs):
            self.review_clicks += 1
            if self.review_click_raises:
                raise RuntimeError('review click failed')
            return original_click(**kwargs)
        locator.click = _click
        return locator

    def locator(self, selector, *args, **kwargs):
        if selector == '[aria-label="批准"]':
            return _PollingLocator([self.approve_count])
        if selector == '[data-testid="conversation-subheader"]':
            locator = _PollingLocator([self.subheader_count])
            original_click = locator.click
            def _click(**kwargs):
                self.subheader_clicks += 1
                return original_click(**kwargs)
            locator.click = _click
            return locator
        return _PollingLocator([0])

    def wait_for_timeout(self, value):
        self.waits.append(value)
        return None


def test_open_pending_review_returns_immediately_when_approve_button_exists():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(navigation_wait_ms=0)
    executor._page = _PendingReviewPage(has_review_text=0, approve_count=1, subheader_count=1, review_click_raises=True)

    executor._open_pending_review(1)

    assert executor._page.review_clicks == 1
    assert executor._page.subheader_clicks == 0


class _MembershipRequestButtonPage:
    def __init__(self):
        self.review_clicks = 0
        self.subheader_clicks = 0
        self.membership_clicks = []
        self.waits = []
        self.empty_queue_visible = False
        self.current_open_index = None

    def get_by_text(self, text, exact=False):
        if text == '没有要审核的成员':
            return _PollingLocator([1 if self.empty_queue_visible else 0])
        locator = _PollingLocator([0])
        original_click = locator.click

        def _click(**kwargs):
            if text.startswith('审核'):
                self.review_clicks += 1
                raise RuntimeError('review click failed')
            return original_click(**kwargs)

        locator.click = _click
        return locator

    def locator(self, selector, *args, **kwargs):
        if selector == '[aria-label="批准"]':
            return _PollingLocator([0])
        if selector == '[data-testid="conversation-subheader"]':
            locator = _PollingLocator([0])
            original_click = locator.click

            def _click(**kwargs):
                self.subheader_clicks += 1
                return original_click(**kwargs)

            locator.click = _click
            return locator
        if selector == '[data-testid="subtype-membership_approval_request"]':
            page = self

            class _Locator:
                def __init__(self, page):
                    self.page = page

                @property
                def first(self):
                    return self.nth(0)

                def count(self):
                    return 2

                def nth(self, index):
                    page = self.page

                    class _Nth:
                        def click(self, **kwargs):
                            page.membership_clicks.append(index)
                            page.current_open_index = index
                            if index == 1:
                                page.empty_queue_visible = True
                            return None

                        def is_visible(self):
                            return True

                    return _Nth()

            return _Locator(page)
        if selector == 'body':
            page = self

            class _Body:
                def inner_text(self, timeout=None):
                    return '待处理请求\n没有要审核的成员\n请求加入该群组且等待批准的用户将在此显示。' if page.empty_queue_visible else '请求加入。点击以审核。'

            return _Body()
        return _PollingLocator([0])

    def wait_for_timeout(self, value):
        self.waits.append(value)
        return None


def test_open_pending_review_uses_membership_request_button_and_detects_empty_queue():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(navigation_wait_ms=0)
    executor._page = _MembershipRequestButtonPage()

    state = executor._open_pending_review(2)

    assert state['empty_queue_detected'] is True
    assert state['opened_via'] == 'membership_request_button_1'
    assert executor._page.membership_clicks == [1, 0]


class _MixedMembershipRequestButtonPage(_MembershipRequestButtonPage):
    def locator(self, selector, *args, **kwargs):
        if selector == '[data-testid="subtype-membership_approval_request"]':
            page = self

            class _Locator:
                def count(self):
                    return 3

                def nth(self, index):
                    class _Nth:
                        def click(self, **kwargs):
                            page.membership_clicks.append(index)
                            page.current_open_index = index
                            page.empty_queue_visible = index == 2
                            return None
                    return _Nth()

            return _Locator()
        if selector == '[data-testid="row"]':
            return _PollingLocator([1 if self.current_open_index == 1 else 0])
        if selector == '[aria-label="批准"]':
            return _PollingLocator([1 if self.current_open_index == 1 else 0])
        return super().locator(selector, *args, **kwargs)



def test_open_pending_review_skips_stale_membership_history_buttons_until_actionable_row():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(navigation_wait_ms=0)
    executor._page = _MixedMembershipRequestButtonPage()

    state = executor._open_pending_review(1)

    assert state['opened_via'] == 'membership_request_button_1'
    assert state['row_count'] == 1
    assert state['approve_count'] == 1
    assert executor._page.membership_clicks == [2, 1]


class _ApproveNoPendingPage(_MembershipRequestButtonPage):
    def __init__(self):
        super().__init__()
        self.body_reads = 0

    def locator(self, selector, *args, **kwargs):
        if selector == 'body':
            page = self

            class _Body:
                def inner_text(self, timeout=None):
                    page.body_reads += 1
                    if page.empty_queue_visible:
                        return '待处理请求\n没有要审核的成员\n请求加入该群组且等待批准的用户将在此显示。'
                    return '~Eastion\n请求加入。点击以审核。\n~Eastion\n请求加入。点击以审核。'

            return _Body()
        return super().locator(selector, *args, **kwargs)


def test_approve_returns_no_pending_when_review_surface_is_empty_after_stale_request_history():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(navigation_wait_ms=0)
    executor._page = _ApproveNoPendingPage()
    executor._warm = True
    executor._context = object()
    executor._group_info_ready = True
    executor._page_ready_for_approval = lambda: True

    result = executor.approve({'registration_group': '8️⃣5️⃣', 'approved_count': 1})

    assert result['result_code'] == 'no_pending_request'
    assert result['verified'] is False
    assert result['raw_result']['review_surface']['empty_queue_detected'] is True


class _RowWaitPage:
    def __init__(self, counts):
        class _RowLocator(_PollingLocator):
            def inner_text(self, timeout=None):
                return '+86 138 6064 0933\n~Eastion'

        self.row_locator = _RowLocator(counts)
        self.waits = []

    def locator(self, selector, *args, **kwargs):
        if selector == '[data-testid="row"]':
            return self.row_locator
        return _PollingLocator([0])

    def wait_for_timeout(self, value):
        self.waits.append(value)
        return None


def test_wait_for_review_row_polls_until_row_is_present():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    executor._page = _RowWaitPage([0, 0, 1])

    executor._wait_for_review_row()

    assert executor._page.waits


def test_extract_all_phones_from_row_text_returns_normalized_first_phone():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    row_text = '~Eastion\n+86 138 6064 0933\n1个共同群组'
    phones = executor._extract_all_phones(row_text)

    assert len(phones) == 1
    assert phones[0].startswith('+86')


def test_extract_pending_count_falls_back_to_request_join_rows():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    body = '~Eastion\n请求加入。点击以审核。\n~Another\n请求加入。点击以审核。'

    assert executor._extract_pending_count(body) == 2


def test_extract_pending_count_prefers_pending_section_over_historical_request_messages():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    body = (
        '聊天历史\n'
        '~Eastion\n请求加入。点击以审核。\n'
        '~Eastion\n请求加入。点击以审核。\n'
        '待处理请求\n'
        '新成员需要管理员批准才能加入该群组。\n'
        '通过邀请链接\n'
        '+86 138 6064 0933\n'
        '~Eastion\n'
        '由+86 138 6064 0933添加\n'
    )

    assert executor._extract_pending_count(body) == 1


def test_extract_pending_count_ignores_historical_review_cta_when_pending_section_exists():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    body = (
        '聊天历史\n'
        '审核3请求加入\n'
        '~Eastion\n请求加入。点击以审核。\n'
        '~Eastion\n请求加入。点击以审核。\n'
        '~Eastion\n请求加入。点击以审核。\n'
        '待处理请求\n'
        '新成员需要管理员批准才能加入该群组。\n'
        '通过邀请链接\n'
        '+86 138 6064 0933\n'
        '~Eastion\n'
        '由+86 138 6064 0933添加\n'
    )

    assert executor._extract_pending_count(body) == 1


def test_extract_pending_count_ignores_historical_request_rows_when_contact_info_panel_is_open():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    body = (
        '联系人信息\n'
        '+86 138 6064 0933\n'
        '~Eastion\n'
        '聊天历史\n'
        '~Eastion\n请求加入。点击以审核。\n'
        '~Eastion\n请求加入。点击以审核。\n'
    )

    assert executor._extract_pending_count(body) == 0


class _ApproveButtonLocator:
    def __init__(self, *, raises=False):
        self.raises = raises
        self.clicks = 0

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def click(self, **kwargs):
        self.clicks += 1
        if self.raises:
            raise RuntimeError('row approve unavailable')
        return None


class _ApproveRow:
    def __init__(self, approve_locator, *, click_raises=False):
        self.approve_locator = approve_locator
        self.clicks = 0
        self.click_raises = click_raises

    def locator(self, selector, *args, **kwargs):
        if selector == '[aria-label="批准"]':
            return self.approve_locator
        return _PollingLocator([0])

    def click(self, **kwargs):
        self.clicks += 1
        if self.click_raises:
            raise RuntimeError('row click failed')
        return None


class _ApproveActionPage:
    def __init__(self, global_approve_count=1):
        self.global_approve = _ApproveButtonLocator()
        self.global_approve_count = global_approve_count

    def locator(self, selector, *args, **kwargs):
        if selector == '[aria-label="批准"]':
            if self.global_approve_count:
                return self.global_approve
            return _PollingLocator([0])
        return _PollingLocator([0])


def test_click_approve_action_falls_back_to_global_approve_button():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    executor._page = _ApproveActionPage(global_approve_count=1)
    row_approve = _ApproveButtonLocator(raises=True)
    row = _ApproveRow(row_approve)

    executor._click_approve_action(row)

    assert row_approve.clicks == 1
    assert executor._page.global_approve.clicks == 1
    assert row.clicks == 0


class _ApproveSubmitConfirmationPage:
    def __init__(self):
        self.global_approve = _ApproveButtonLocator()
        self.waits = []
        self.row_submit_confirmed = False

    def locator(self, selector, *args, **kwargs):
        if selector == '[aria-label="批准"]':
            page = self

            class _Locator:
                @property
                def first(self):
                    return self

                def count(self):
                    return 0 if page.row_submit_confirmed else 1

                def click(self, **kwargs):
                    page.row_submit_confirmed = True
                    page.global_approve.clicks += 1
                    return None

            return _Locator()
        if selector == '[data-testid="row"]':
            page = self

            class _Rows:
                def count(self):
                    return 0 if page.row_submit_confirmed else 1

            return _Rows()
        if selector == 'body':
            page = self

            class _Body:
                def inner_text(self, timeout=None):
                    return '待处理请求\n没有要审核的成员' if page.row_submit_confirmed else '待处理请求\n通过邀请链接\n+86 138 6064 0933\n~Eastion'

            return _Body()
        return _PollingLocator([0])

    def wait_for_timeout(self, value):
        self.waits.append(value)
        return None


class _ApproveRowNeedsConfirm(_ApproveRow):
    def __init__(self, approve_locator, page):
        super().__init__(approve_locator)
        self.page = page

    def locator(self, selector, *args, **kwargs):
        if selector == '[aria-label="批准"]':
            row = self

            class _RowApprove:
                def click(self, **kwargs):
                    row.approve_locator.clicks += 1
                    return None

            return _RowApprove()
        return super().locator(selector, *args, **kwargs)


def test_click_approve_action_confirms_submission_when_row_click_alone_does_not_change_queue():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(post_click_wait_ms=0)
    page = _ApproveSubmitConfirmationPage()
    executor._page = page
    row_approve = _ApproveButtonLocator(raises=False)
    row = _ApproveRowNeedsConfirm(row_approve, page)

    executor._click_approve_action(row)

    assert row_approve.clicks == 1
    assert page.global_approve.clicks == 1
    assert page.row_submit_confirmed is True


def test_approve_retries_once_when_first_submit_does_not_reduce_queue():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(post_click_wait_ms=0)
    executor._warm = True
    executor._context = object()
    executor._group_info_ready = True
    executor._page_ready_for_approval = lambda: True

    class _Page:
        def __init__(self):
            self.waits = []

        def locator(self, selector, *args, **kwargs):
            if selector == 'body':
                class _Body:
                    def inner_text(self, timeout=None):
                        return '待处理请求\n通过邀请链接\n+86 138 6064 0933\n~Eastion\n由+86 138 6064 0933添加'
                return _Body()
            if selector == '[data-testid="pushname"]':
                class _Push:
                    @property
                    def first(self):
                        return self
                    def count(self):
                        return 1
                    def inner_text(self):
                        return '~Eastion'
                return _Push()
            return _PollingLocator([0])

        def wait_for_timeout(self, value):
            self.waits.append(value)
            return None

    class _Row:
        def inner_text(self, timeout=None):
            return '+86 138 6064 0933\n~Eastion\n由+86 138 6064 0933添加'

    executor._page = _Page()
    row = _Row()
    click_calls = []
    verification_states = [
        {
            'pending_count': 1,
            'member_count': None,
            'all_phones_normalized': ['+8613860640933'],
            'body_excerpt': 'still pending',
            'queue_delta': False,
            'member_confirmed': True,
        },
        {
            'pending_count': 0,
            'member_count': None,
            'all_phones_normalized': ['+8613860640933'],
            'body_excerpt': 'approved',
            'queue_delta': True,
            'member_confirmed': True,
        },
    ]
    actionable_snapshots = [
        {'row_count': 1, 'approve_count': 1, 'empty_queue_detected': False, 'body_excerpt': 'still actionable'},
    ]

    executor._ensure_browser = lambda: None
    executor._open_group_info = lambda: None
    executor._open_pending_review = lambda pending_before: {'opened_via': 'review_text', 'row_count': 1, 'approve_count': 1, 'empty_queue_detected': False}
    executor._wait_for_review_row = lambda: row
    executor._click_approve_action = lambda current_row: click_calls.append(current_row)
    executor._same_session_verify = lambda **kwargs: verification_states.pop(0)
    executor._review_surface_state = lambda: actionable_snapshots.pop(0) if actionable_snapshots else {'row_count': 0, 'approve_count': 0, 'empty_queue_detected': True, 'body_excerpt': 'done'}

    result = executor.approve({'registration_group': '8️⃣5️⃣', 'approved_count': 1})

    assert result['verified'] is True
    assert result['result_code'] == 'approved'
    assert len(click_calls) == 2
    assert result['raw_result']['retry_attempted'] is True
    assert result['raw_result']['retry_succeeded'] is True


class _ApproveActionPollingPage:
    def __init__(self, before_row_click_counts, after_row_click_counts):
        self.before_row_click_counts = list(before_row_click_counts)
        self.after_row_click_counts = list(after_row_click_counts)
        self.row_clicked = False
        self.waits = []
        self.global_approve_clicks = 0

    def locator(self, selector, *args, **kwargs):
        if selector == '[aria-label="批准"]':
            page = self

            class _Locator:
                @property
                def first(self):
                    return self

                def count(self):
                    counts = page.after_row_click_counts if page.row_clicked else page.before_row_click_counts
                    if len(counts) > 1:
                        return counts.pop(0)
                    return counts[0]

                def click(self, **kwargs):
                    page.global_approve_clicks += 1
                    return None

            return _Locator()
        return _PollingLocator([0])

    def wait_for_timeout(self, value):
        self.waits.append(value)
        return None


def test_click_approve_action_polls_until_global_approve_button_is_ready():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(post_click_wait_ms=0)
    page = _ApproveActionPollingPage([0, 0, 0], [0, 1])
    executor._page = page
    row_approve = _ApproveButtonLocator(raises=True)
    row = _ApproveRow(row_approve)
    original_row_click = row.click

    def _row_click(**kwargs):
        page.row_clicked = True
        return original_row_click(**kwargs)

    row.click = _row_click

    executor._click_approve_action(row)

    assert row_approve.clicks == 1
    assert row.clicks == 1
    assert executor._page.waits
    assert executor._page.global_approve_clicks == 1


def test_wait_for_review_row_raises_fast_when_row_never_appears():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    executor._page = _RowWaitPage([0, 0, 0])

    try:
        executor._wait_for_review_row()
    except RuntimeError as exc:
        assert 'review row unavailable' in str(exc)
    else:
        raise AssertionError('expected review row wait to fail fast')


class _RowReadyProbe:
    def __init__(self, readiness_sequence):
        self.readiness_sequence = list(readiness_sequence)
        self.inner_text_calls = 0

    def inner_text(self, timeout=None):
        self.inner_text_calls += 1
        if len(self.readiness_sequence) > 1:
            ready = self.readiness_sequence.pop(0)
        else:
            ready = self.readiness_sequence[0]
        if not ready:
            raise RuntimeError('row text not attached yet')
        return '+86 138 6064 0933\n~Eastion'


class _RowReadyLocator:
    def __init__(self, readiness_sequence):
        self.probe = _RowReadyProbe(readiness_sequence)

    @property
    def first(self):
        return self.probe

    def count(self):
        return 1


class _RowReadyPage:
    def __init__(self, readiness_sequence):
        self.row_locator = _RowReadyLocator(readiness_sequence)
        self.waits = []

    def locator(self, selector, *args, **kwargs):
        if selector == '[data-testid="row"]':
            return self.row_locator
        if selector == '[aria-label="批准"]':
            return _PollingLocator([0])
        if selector == 'body':
            class _Body:
                def inner_text(self, timeout=None):
                    return '请求加入。点击以审核。'
            return _Body()
        return _PollingLocator([0])

    def wait_for_timeout(self, value):
        self.waits.append(value)
        return None


def test_wait_for_review_row_waits_until_row_text_is_readable():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    page = _RowReadyPage([False, False, True])
    executor._page = page

    row = executor._wait_for_review_row()

    assert row is page.row_locator.probe
    assert page.row_locator.probe.inner_text_calls >= 3
    assert page.waits
