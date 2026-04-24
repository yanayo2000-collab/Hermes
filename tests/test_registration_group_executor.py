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

from app.registration_group_executor import LiveWarmWhatsAppRegistrationGroupApprovalExecutor, ReviewSurfaceRecoveryRequired, AmbiguousReviewTargetError


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


def test_ensure_browser_waits_until_download_gate_clears_before_reporting_warm(monkeypatch, tmp_path):
    chrome_root = tmp_path / 'chrome-root'
    profile_dir = chrome_root / 'Profile 25'
    profile_dir.mkdir(parents=True)
    (chrome_root / 'Local State').write_text('{}')
    (profile_dir / 'Preferences').write_text('{}')

    class _LoadingPage:
        def __init__(self):
            self.waits = []
            self.loading_counts = [1, 1, 0]
            self.chat_list_counts = [0, 1]

        def goto(self, *args, **kwargs):
            return None

        def wait_for_timeout(self, value):
            self.waits.append(value)
            return None

        def get_by_text(self, text, exact=False):
            if text in {'请不要关闭此窗口', '消息正在下载中', '你的消息正在下载中'}:
                count = self.loading_counts.pop(0) if len(self.loading_counts) > 1 else self.loading_counts[0]
                return _PollingLocator([count])
            return _PollingLocator([0])

        def locator(self, selector, *args, **kwargs):
            if selector == '[data-testid="chat-list"]':
                count = self.chat_list_counts.pop(0) if len(self.chat_list_counts) > 1 else self.chat_list_counts[0]
                return _PollingLocator([count])
            return _PollingLocator([0])

    class _LoadingContext:
        def __init__(self):
            self.pages = [_LoadingPage()]

        def new_page(self):
            return self.pages[0]

        def close(self):
            return None

    class _LoadingPlaywright:
        def __init__(self):
            class _Chromium:
                def launch_persistent_context(self, *args, **kwargs):
                    return _LoadingContext()
            self.chromium = _Chromium()

        def stop(self):
            return None

    class _LoadingSyncPlaywright:
        def start(self):
            return _LoadingPlaywright()

    monkeypatch.setattr('app.registration_group_executor.sync_playwright', lambda: _LoadingSyncPlaywright())

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
    assert executor._page.waits[0] == 200
    assert 120 in executor._page.waits[1:]


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


def test_ensure_browser_reuses_warm_context_when_owner_thread_differs(monkeypatch, tmp_path):
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
    executor._context = _FakeContext()
    executor._page = executor._context.pages[0]
    executor._active_temp_user_data_dir = str(tmp_path / 'temp-profile-stale')
    Path(executor._active_temp_user_data_dir).mkdir(parents=True)
    executor._owner_thread_id = -1
    executor._warm = True

    executor._ensure_browser()

    assert executor._owner_thread_id == __import__('threading').get_ident()
    assert executor.health()['status'] == 'warm'
    assert executor._active_temp_user_data_dir == str(tmp_path / 'temp-profile-stale')
    assert fake_sync.instance.chromium.launch_calls == 0


def test_call_on_owner_thread_reuses_same_background_thread():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()

    first = executor._call_on_owner_thread(lambda: __import__('threading').get_ident())
    second = executor._call_on_owner_thread(lambda: __import__('threading').get_ident())

    assert first == second
    assert first != __import__('threading').get_ident()


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
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(navigation_wait_ms=0, initial_wait_ms=0)
    executor._page = _OpenGroupInfoRetryPage()
    executor._enter_groups_tab = lambda: None

    executor._open_group_info()

    assert executor._page.header_clicks == 1
    assert executor._page.subheader_clicks == 1
    assert executor._group_info_ready is True
    assert executor._page.waits[:3] == [200, 120, 120]

class _OpenGroupInfoHeaderRetryPage:
    def __init__(self):
        self.header_clicks = 0
        self.subheader_clicks = 0
        self.waits = []

    def get_by_text(self, text, exact=False):
        if text == '群组信息' and exact:
            return _PollingLocator([1 if self.header_clicks >= 2 else 0])
        if text == '待处理请求' and exact:
            return _PollingLocator([1 if self.header_clicks >= 2 else 0])
        if text == '没有要审核的成员' and exact:
            return _PollingLocator([0])
        if text == '联系人信息' and exact:
            return _PollingLocator([0])
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
        return _Clickable('other')

    def wait_for_timeout(self, value):
        self.waits.append(value)
        return None


def test_open_group_info_retries_header_until_group_info_surface_is_ready():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(navigation_wait_ms=0)
    executor._page = _OpenGroupInfoHeaderRetryPage()
    executor._enter_groups_tab = lambda: None

    executor._open_group_info()

    assert executor._page.header_clicks >= 2
    assert executor._group_info_ready is True


def test_page_ready_for_approval_ignores_membership_request_buttons_on_chat_list():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(navigation_wait_ms=0)
    executor._page = _ReadyPage(locator_counts={'[data-testid="subtype-membership_approval_request"]': 2})

    assert executor._page_ready_for_approval() is False


def test_page_ready_for_approval_does_not_accept_chat_surface_membership_buttons_without_group_info_markers():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(navigation_wait_ms=0)
    executor._page = _ReadyPage(
        counts={('点击以审核', False): 1},
        locator_counts={
            '[data-testid="subtype-membership_approval_request"]': 2,
            '[data-testid="conversation-header"]': 1,
        },
    )

    assert executor._page_ready_for_approval() is False


def test_capture_group_info_body_waits_past_historical_chat_pending_until_group_info_panel_arrives():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(navigation_wait_ms=0)

    class _BodySequencePage:
        def __init__(self):
            self.body_reads = 0
            self.waits = []

        def locator(self, selector, *args, **kwargs):
            if selector != 'body':
                return _PollingLocator([0])
            page = self

            class _Body:
                def inner_text(self, timeout=None):
                    page.body_reads += 1
                    if page.body_reads == 1:
                        return (
                            '聊天历史\n'
                            '待处理请求\n'
                            '通过邀请链接\n'
                            '+86 138 6064 0933\n'
                            '~Eastion\n'
                            '由+86 138 6064 0933添加\n'
                        )
                    return (
                        '群组信息\n'
                        '群组 · 4位成员\n'
                        '待处理请求\n'
                        '2\n'
                        '通过邀请链接\n'
                        '+852 6775 5475\n'
                        '~G2\n'
                        '由+852 6775 5475添加\n'
                        '+62 851-9830-6838\n'
                        '~zhu z\n'
                        '由+62 851-9830-6838添加\n'
                    )
            return _Body()

        def wait_for_timeout(self, value):
            self.waits.append(value)
            return None

    executor._page = _BodySequencePage()

    body = executor._capture_group_info_body(wait_for_pending_seconds=0.4)

    assert '群组信息' in body
    assert '+852 6775 5475' in body
    assert '+86 138 6064 0933' not in body
    assert executor._extract_pending_count(body) == 2
    assert executor._extract_pending_candidates(body)['phones'][0].startswith('+852')
    assert executor._page.body_reads >= 2


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
    assert executor._page.waits == []


def test_open_pending_review_uses_short_wait_after_review_cta_click():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(navigation_wait_ms=0)
    executor._page = _PendingReviewPage(has_review_text=1, approve_count=1, subheader_count=0, review_click_raises=False)

    executor._open_pending_review(1)

    assert executor._page.review_clicks == 1
    assert executor._page.waits == [120]


class _FastApproveReviewSurfacePage:
    def __init__(self):
        self.body_reads = 0
        self.waits = []

    def locator(self, selector, *args, **kwargs):
        if selector == '[data-testid="row"]':
            return _PollingLocator([1])
        if selector == '[aria-label="批准"]':
            return _PollingLocator([1])
        if selector == '[data-testid="subtype-membership_approval_request"]':
            return _PollingLocator([0])
        if selector == 'body':
            page = self

            class _Body:
                def inner_text(self, timeout=None):
                    page.body_reads += 1
                    return '请求加入。点击以审核。'

            return _Body()
        return _PollingLocator([0])

    def get_by_text(self, text, exact=False):
        return _PollingLocator([0])

    def wait_for_timeout(self, value):
        self.waits.append(value)
        return None


def test_wait_for_review_surface_fast_path_skips_body_read_when_approve_is_visible():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(navigation_wait_ms=0)
    executor._page = _FastApproveReviewSurfacePage()

    state = executor._wait_for_review_surface(timeout_seconds=0.2)

    assert state['approve_count'] == 1
    assert executor._page.body_reads == 0


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


class _GroupInfoMembershipRequestPage(_MembershipRequestButtonPage):
    def locator(self, selector, *args, **kwargs):
        if selector == '[data-testid="subtype-membership_approval_request"]':
            page = self

            class _Locator:
                def count(self):
                    return 2

                def nth(self, index):
                    class _Nth:
                        def click(self, **kwargs):
                            page.membership_clicks.append(index)
                            page.current_open_index = index
                            return None
                    return _Nth()

            return _Locator()
        if selector == '[data-testid="row"]':
            return _PollingLocator([4])
        if selector == '[aria-label="批准"]':
            return _PollingLocator([0])
        if selector == 'body':
            class _Body:
                def inner_text(self, timeout=None):
                    return '群组信息\n群组 · 4位成员\n+852 6775 5475\n~zhu z\n+62 851-9830-6838'

            return _Body()
        return super().locator(selector, *args, **kwargs)


def test_open_pending_review_ignores_group_info_member_rows_without_review_markers():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(navigation_wait_ms=0)
    executor._page = _GroupInfoMembershipRequestPage()

    state = executor._open_pending_review(1)

    assert state['opened_via'] == 'surface_poll_timeout'
    assert state['approve_count'] == 0
    assert state['row_count'] == 4
    assert state['empty_queue_detected'] is False
    assert executor._page.membership_clicks == [1, 0]


class _ContactInfoMembershipRequestPage(_MembershipRequestButtonPage):
    def __init__(self):
        super().__init__()
        self.contact_info_visible = False

    def get_by_text(self, text, exact=False):
        if text == '没有要审核的成员':
            return _PollingLocator([0])
        if text == '联系人信息' and exact:
            return _PollingLocator([1 if self.contact_info_visible else 0])
        return super().get_by_text(text, exact=exact)

    def locator(self, selector, *args, **kwargs):
        if selector == '[data-testid="subtype-membership_approval_request"]':
            page = self

            class _Locator:
                def count(self):
                    return 2

                def nth(self, index):
                    class _Nth:
                        def click(self, **kwargs):
                            page.membership_clicks.append(index)
                            page.current_open_index = index
                            page.contact_info_visible = index == 1
                            return None
                    return _Nth()

            return _Locator()
        if selector == '[data-testid="row"]':
            return _PollingLocator([2 if self.contact_info_visible else 1])
        if selector == '[aria-label="批准"]':
            return _PollingLocator([1 if self.current_open_index == 0 and not self.contact_info_visible else 0])
        if selector == 'body':
            page = self

            class _Body:
                def inner_text(self, timeout=None):
                    if page.contact_info_visible:
                        return '联系人信息\n+852 4456 8277\n影音内容、链接和文档\n加密'
                    return '~Eastion\n请求加入。点击以审核。'

            return _Body()
        return super().locator(selector, *args, **kwargs)


def test_open_pending_review_ignores_contact_info_rows_and_keeps_searching():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(navigation_wait_ms=0)
    executor._page = _ContactInfoMembershipRequestPage()

    state = executor._open_pending_review(1)

    assert state['opened_via'] == 'membership_request_button_0'
    assert state['approve_count'] == 1
    assert state['contact_info_detected'] is False
    assert executor._page.membership_clicks == [1, 0]


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


def test_approve_waits_for_pending_section_before_declaring_no_pending():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(post_click_wait_ms=0)
    executor._warm = True
    executor._context = object()
    executor._group_info_ready = True
    executor._page_ready_for_approval = lambda: True

    class _Page:
        def __init__(self):
            self.body_reads = 0
            self.waits = []

        def locator(self, selector, *args, **kwargs):
            if selector == 'body':
                page = self

                class _Body:
                    def inner_text(self, timeout=None):
                        page.body_reads += 1
                        if page.body_reads == 1:
                            return '群组信息\n群组 · 4位成员\n添加成员\n使用链接邀请加入群组'
                        return '群组信息\n群组 · 4位成员\n待处理请求\n2\n+852 4456 8277\n~G2\n+86 138 6064 0933\n~Eastion'

                return _Body()
            if selector == '[data-testid="pushname"]':
                class _Push:
                    @property
                    def first(self):
                        return self
                    def count(self):
                        return 1
                    def inner_text(self, timeout=None):
                        return '~G2'
                return _Push()
            return _PollingLocator([0])

        def wait_for_timeout(self, value):
            self.waits.append(value)
            return None

    class _Row:
        def inner_text(self, timeout=None):
            return '+852 4456 8277\n~G2\n由+852 4456 8277添加'

        def locator(self, selector, *args, **kwargs):
            return _PollingLocator([0])

    page = _Page()
    executor._page = page
    executor._ensure_browser = lambda: None
    executor._open_group_info = lambda: None
    executor._open_pending_review = lambda pending_before: {'opened_via': 'review_text', 'row_count': 2, 'approve_count': 2, 'empty_queue_detected': False}
    executor._wait_for_review_row = lambda **kwargs: _Row()
    executor._last_review_selection = {
        'candidate_rows': [{'index': 0, 'display_name': '~G2', 'phones': ['+852****8277'], 'actionable': True}],
        'selected_candidate': {'index': 0, 'display_name': '~G2', 'phones': ['+852****8277'], 'actionable': True},
        'selection_reason': 'exact_phone_match',
    }
    executor._click_approve_action = lambda row: None
    executor._same_session_verify = lambda **kwargs: {
        'pending_count': 0,
        'member_count': 6,
        'all_phones_normalized': ['+852****8277', '+861****0933'],
        'body_excerpt': 'queue drained and target member confirmed',
        'queue_delta': True,
        'member_confirmed': True,
    }

    result = executor.approve({
        'registration_group': '8️⃣5️⃣',
        'approved_count': 1,
        'target_name_hint': '~G2',
        'target_phone_hint': '+852 4456 8277',
    })

    assert result['result_code'] == 'approved'
    assert result['raw_result']['start_snapshot']['pending_count'] == 2
    assert page.body_reads >= 2
    assert page.waits


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


def test_extract_pending_count_prefers_last_pending_section_over_historical_pending_section():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    body = (
        '聊天历史\n'
        '待处理请求\n'
        '通过邀请链接\n'
        '+852 6775 5475\n'
        '~G2\n'
        '由+852 6775 5475添加\n'
        '更多历史\n'
        '待处理请求\n'
        '没有要审核的成员\n'
        '请求加入该群组且等待批准的用户将在此显示。\n'
    )

    assert executor._extract_pending_count(body) == 0


def test_extract_pending_count_ignores_historical_chat_rows_when_group_info_panel_is_open():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    body = (
        '聊天历史\n'
        '~Eastion\n请求加入。点击以审核。\n'
        '~Eastion\n请求加入。点击以审核。\n'
        '群组信息\n'
        '群组 · 4位成员\n'
        '添加成员\n'
        '使用链接邀请加入群组\n'
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
        self.waits = []

    def locator(self, selector, *args, **kwargs):
        if selector == '[aria-label="批准"]':
            page = self

            class _Locator:
                @property
                def first(self):
                    return self

                def count(self):
                    return 1 if page.global_approve_count else 0

                def click(self, **kwargs):
                    page.global_approve.clicks += 1
                    page.global_approve_count = 0
                    return None

            return _Locator()
        if selector == '[data-testid="row"]':
            page = self

            class _Rows:
                def count(self):
                    return 0 if page.global_approve_count == 0 else 1

            return _Rows()
        return _PollingLocator([0])

    def wait_for_timeout(self, value):
        self.waits.append(value)
        return None


def test_click_approve_action_falls_back_to_global_approve_button():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    executor._page = _ApproveActionPage(global_approve_count=1)
    row_approve = _ApproveButtonLocator(raises=True)
    row = _ApproveRow(row_approve)

    executor._click_approve_action(row)

    assert row_approve.clicks >= 1
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

    assert row_approve.clicks >= 1
    assert page.global_approve.clicks == 1
    assert page.row_submit_confirmed is True


class _GlobalApproveNeedsSecondClickPage:
    def __init__(self):
        self.waits = []
        self.global_clicks = 0
        self.submission_confirmed = False

    def locator(self, selector, *args, **kwargs):
        if selector == '[aria-label="批准"]':
            page = self

            class _Locator:
                @property
                def first(self):
                    return self

                def count(self):
                    return 0 if page.submission_confirmed else 1

                def click(self, **kwargs):
                    page.global_clicks += 1
                    if page.global_clicks >= 2:
                        page.submission_confirmed = True
                    return None

            return _Locator()
        if selector == '[data-testid="row"]':
            page = self

            class _Rows:
                def count(self):
                    return 0 if page.submission_confirmed else 1

            return _Rows()
        if selector == 'body':
            page = self

            class _Body:
                def inner_text(self, timeout=None):
                    return '待处理请求\n没有要审核的成员' if page.submission_confirmed else '待处理请求\n通过邀请链接\n+852 4456 8277\n~G2'

            return _Body()
        return _PollingLocator([0])

    def get_by_text(self, text, exact=False):
        page = self

        class _Text:
            def count(self):
                if text == '没有要审核的成员' and exact:
                    return 1 if page.submission_confirmed else 0
                if text == '联系人信息' and exact:
                    return 0
                return 0

        return _Text()

    def wait_for_timeout(self, value):
        self.waits.append(value)
        return None


def test_click_approve_action_retries_global_approve_until_submission_is_confirmed():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(post_click_wait_ms=0)
    page = _GlobalApproveNeedsSecondClickPage()
    executor._page = page
    row_approve = _ApproveButtonLocator(raises=True)
    row = _ApproveRow(row_approve)

    executor._click_approve_action(row)

    assert row_approve.clicks >= 1
    assert row.clicks == 0
    assert page.global_clicks == 2
    assert page.submission_confirmed is True


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
                    def inner_text(self, timeout=None):
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
            'all_phones_normalized': ['+861****0933'],
            'body_excerpt': 'still pending',
            'queue_delta': False,
            'member_confirmed': True,
        },
        {
            'pending_count': 0,
            'member_count': None,
            'all_phones_normalized': ['+861****0933'],
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
    executor._wait_for_review_row = lambda **kwargs: row
    executor._click_approve_action = lambda current_row: click_calls.append(current_row)
    executor._same_session_verify = lambda **kwargs: verification_states.pop(0)
    executor._review_surface_state = lambda: actionable_snapshots.pop(0) if actionable_snapshots else {'row_count': 0, 'approve_count': 0, 'empty_queue_detected': True, 'body_excerpt': 'done'}

    result = executor.approve({'registration_group': '8️⃣5️⃣', 'approved_count': 1, 'approval_run_id': 'registration_group_approval_test123'})

    assert result['verified'] is True
    assert result['result_code'] == 'approved'
    assert len(click_calls) == 2
    assert result['raw_result']['approval_run_id'] == 'registration_group_approval_test123'
    assert result['raw_result']['retry_attempted'] is True
    assert result['raw_result']['retry_succeeded'] is True
    assert result['raw_result']['start_snapshot']['pending_count'] == 1
    assert result['raw_result']['verification_snapshot']['pending_count'] == 0


def test_approve_recovers_when_row_click_opens_contact_info_once():
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
                        return '待处理请求\n通过邀请链接\n+852 4456 8277\n由+852 4456 8277添加'
                return _Body()
            if selector == '[data-testid="pushname"]':
                class _Push:
                    @property
                    def first(self):
                        return self
                    def count(self):
                        return 0
                    def inner_text(self):
                        return ''
                return _Push()
            return _PollingLocator([0])

        def wait_for_timeout(self, value):
            self.waits.append(value)
            return None

    class _Row:
        def __init__(self, text):
            self.text = text

        def inner_text(self, timeout=None):
            return self.text

    executor._page = _Page()
    first_row = _Row('+852 4456 8277\n由+852 4456 8277添加')
    second_row = _Row('+852 4456 8277\n由+852 4456 8277添加')
    rows = [first_row, second_row]
    open_group_info_calls = []
    open_pending_review_calls = []
    click_attempts = []

    executor._ensure_browser = lambda: None
    executor._open_group_info = lambda: open_group_info_calls.append('open')
    executor._open_pending_review = lambda pending_before: open_pending_review_calls.append(pending_before) or {'opened_via': 'review_text', 'row_count': 1, 'approve_count': 1, 'empty_queue_detected': False}
    executor._wait_for_review_row = lambda **kwargs: rows.pop(0)

    def _click(current_row):
        click_attempts.append(current_row)
        if len(click_attempts) == 1:
            raise ReviewSurfaceRecoveryRequired('contact info opened after row click; review surface must be reopened')
        return None

    executor._click_approve_action = _click
    executor._same_session_verify = lambda **kwargs: {
        'pending_count': 0,
        'member_count': None,
        'all_phones_normalized': ['+852****8277'],
        'body_excerpt': 'approved',
        'queue_delta': True,
        'member_confirmed': True,
    }
    executor._review_surface_state = lambda: {'row_count': 0, 'approve_count': 0, 'empty_queue_detected': False, 'contact_info_detected': True, 'body_excerpt': '联系人信息'}

    result = executor.approve({'registration_group': '8️⃣5️⃣', 'approved_count': 1})

    assert result['verified'] is True
    assert result['raw_result']['review_surface_recovery_attempted'] is True
    assert result['raw_result']['review_surface_recovery_snapshot']['contact_info_detected'] is True
    assert open_group_info_calls == ['open', 'open']
    assert open_pending_review_calls == [1, 1]
    assert len(click_attempts) == 2


def test_approve_recovers_when_review_row_wait_times_out_on_contact_info_surface():
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
                        return '待处理请求\n通过邀请链接\n+852 4456 8277\n由+852 4456 8277添加'
                return _Body()
            if selector == '[data-testid="pushname"]':
                class _Push:
                    @property
                    def first(self):
                        return self
                    def count(self):
                        return 0
                    def inner_text(self):
                        return ''
                return _Push()
            return _PollingLocator([0])

        def wait_for_timeout(self, value):
            self.waits.append(value)
            return None

    class _Row:
        def inner_text(self, timeout=None):
            return '+852 4456 8277\n由+852 4456 8277添加'

    executor._page = _Page()
    open_group_info_calls = []
    open_pending_review_calls = []
    wait_calls = []

    executor._ensure_browser = lambda: None
    executor._open_group_info = lambda: open_group_info_calls.append('open')
    executor._open_pending_review = lambda pending_before: open_pending_review_calls.append(pending_before) or {'opened_via': 'review_text', 'row_count': 1, 'approve_count': 1, 'empty_queue_detected': False}

    def _wait_for_review_row(**kwargs):
        wait_calls.append('wait')
        if len(wait_calls) == 1:
            raise RuntimeError('review row unavailable after opening pending review; row_count=0 approve_count=0')
        return _Row()

    executor._wait_for_review_row = _wait_for_review_row
    executor._click_approve_action = lambda current_row: None
    executor._same_session_verify = lambda **kwargs: {
        'pending_count': 0,
        'member_count': None,
        'all_phones_normalized': ['+852****8277'],
        'body_excerpt': 'approved',
        'queue_delta': True,
        'member_confirmed': True,
    }
    executor._review_surface_state = lambda: {'row_count': 0, 'approve_count': 0, 'empty_queue_detected': False, 'contact_info_detected': True, 'body_excerpt': '联系人信息'}

    result = executor.approve({'registration_group': '8️⃣5️⃣', 'approved_count': 1})

    assert result['verified'] is True
    assert result['raw_result']['review_surface_recovery_attempted'] is True
    assert open_group_info_calls == ['open', 'open']
    assert open_pending_review_calls == [1, 1]
    assert wait_calls == ['wait', 'wait']


def test_approve_timeout_upgrades_to_verified_when_consumed_queue_and_member_confirmation_are_salvaged():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(post_click_wait_ms=0)
    executor._warm = True
    executor._context = object()
    executor._group_info_ready = True
    executor._page_ready_for_approval = lambda: True

    class _Page:
        def __init__(self):
            self.waits = []
            self.body_calls = 0

        def locator(self, selector, *args, **kwargs):
            if selector == 'body':
                page = self

                class _Body:
                    def inner_text(self, timeout=None):
                        page.body_calls += 1
                        if page.body_calls == 1:
                            return (
                                '群组信息\n群组 · 4位成员\n待处理请求\n2\n+852 6775 5475\n~zhu z\n'
                                '+62 851-9830-6838\n+62 858-1347-8460\n+62 851-9830-6832\n'
                            )
                        return (
                            '联系人信息\n+852 4456 8277\n1个共同群组\n'
                            '+86 138 6064 0933、+62 851-9830-6838、+62 858-1347-8460、+62 851-9830-6832、+852 4456 8277、你\n'
                            '昨天\n你已通过邀请链接加入\n群组 · 6位成员\n没有联系人 · 创建于2026年2月7日\n'
                        )

                return _Body()
            if selector == '[data-testid="pushname"]':
                class _Push:
                    @property
                    def first(self):
                        return self

                    def count(self):
                        return 1

                    def inner_text(self, timeout=None):
                        return '~G2'

                return _Push()
            return _PollingLocator([0])

        def wait_for_timeout(self, value):
            self.waits.append(value)
            return None

    class _Row:
        def inner_text(self, timeout=None):
            return '+852 4456 8277\n~G2\n由+852 4456 8277添加'

    executor._page = _Page()
    executor._ensure_browser = lambda: None
    executor._open_group_info = lambda: None
    executor._open_pending_review = lambda pending_before: {'opened_via': 'review_text', 'row_count': 1, 'approve_count': 1, 'empty_queue_detected': False}
    executor._wait_for_review_row = lambda **kwargs: _Row()
    executor._click_approve_action = lambda row: (_ for _ in ()).throw(RuntimeError('approve action unavailable after review row opened; row_count=0 approve_count=0'))
    executor._review_surface_state = lambda: {
        'row_count': 0,
        'approve_count': 0,
        'empty_queue_detected': False,
        'contact_info_detected': True,
        'review_marker_detected': True,
        'body_excerpt': '联系人信息\n+852 4456 8277\n6位成员',
    }
    executor._reset_browser = lambda reason: None

    result = executor.approve({'registration_group': '8️⃣5️⃣', 'approved_count': 1, 'approval_run_id': 'registration_group_approval_timeout_evidence'})

    assert result['result_code'] == 'approved'
    assert result['status'] == 'success'
    assert result['verified'] is True
    assert result['queue_delta'] is True
    assert result['member_confirmed'] is True
    assert result['target_member']['phone_normalized'].startswith('+852')
    assert result['target_member']['phone_normalized'].endswith('8277')
    assert result['raw_result']['pending_before'] == 2
    assert result['raw_result']['pending_after'] == 0
    assert result['raw_result']['member_count_before'] == 4
    assert result['raw_result']['member_count_after'] == 6
    assert result['raw_result']['verification_snapshot']['queue_delta'] is True
    assert result['raw_result']['verification_snapshot']['member_confirmed'] is True


def test_approve_returns_ambiguous_review_target_when_review_rows_are_not_unique():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(post_click_wait_ms=0)
    executor._warm = True
    executor._context = object()
    executor._group_info_ready = True
    executor._page_ready_for_approval = lambda: True

    class _Page:
        def locator(self, selector, *args, **kwargs):
            if selector == 'body':
                class _Body:
                    def inner_text(self, timeout=None):
                        return (
                            '群组信息\n群组 · 4位成员\n待处理请求\n2\n'
                            '+852 6775 5475\n~zhu z\n+62 851-9830-6838\n+62 858-1347-8460\n+62 851-9830-6832\n'
                        )
                return _Body()
            if selector == '[data-testid="row"]':
                return _SelectableRowLocator([
                    _SelectableRow('+852 4456 8277\n~G2\n通过邀请链接', approve_count=1),
                    _SelectableRow('+852 5566 8899\n~G3\n通过邀请链接', approve_count=1),
                ])
            if selector == '[data-testid="pushname"]':
                class _Push:
                    @property
                    def first(self):
                        return self
                    def count(self):
                        return 0
                    def inner_text(self):
                        return ''
                return _Push()
            if selector == '[aria-label="批准"]':
                return _PollingLocator([0])
            return _PollingLocator([0])

        def get_by_text(self, text, exact=False):
            if text == '联系人信息' and exact:
                return _PollingLocator([0])
            if text == '没有要审核的成员' and exact:
                return _PollingLocator([0])
            return _PollingLocator([0])

        def wait_for_timeout(self, value):
            return None

    executor._page = _Page()
    executor._ensure_browser = lambda: None
    executor._open_group_info = lambda: None
    executor._open_pending_review = lambda pending_before: {'opened_via': 'review_text', 'row_count': 2, 'approve_count': 2, 'empty_queue_detected': False}
    executor._reset_browser = lambda reason: None

    result = executor.approve({'registration_group': '8️⃣5️⃣', 'approved_count': 1, 'approval_run_id': 'registration_group_approval_ambiguous'})

    assert result['result_code'] == 'ambiguous_review_target'
    assert result['verified'] is False
    assert len(result['raw_result']['candidate_rows']) == 2
    assert result['raw_result']['selection_reason'] == ''


def test_approve_uses_context_target_hints_to_break_review_row_ambiguity():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(post_click_wait_ms=0)
    executor._warm = True
    executor._context = object()
    executor._group_info_ready = True
    executor._page_ready_for_approval = lambda: True

    class _Page:
        def locator(self, selector, *args, **kwargs):
            if selector == 'body':
                class _Body:
                    def inner_text(self, timeout=None):
                        return (
                            '群组信息\n群组 · 4位成员\n待处理请求\n2\n'
                            '+852 6775 5475\n~zhu z\n+62 851-9830-6838\n+62 858-1347-8460\n+62 851-9830-6832\n'
                        )
                return _Body()
            if selector == '[data-testid="row"]':
                return _SelectableRowLocator([
                    _SelectableRow('+86 138 6064 0933\n~Eastion\n由+86 138 6064 0933添加', approve_count=1),
                    _SelectableRow('+852 4456 8277\n由+852 4456 8277添加', approve_count=1),
                ])
            if selector == '[data-testid="pushname"]':
                class _Push:
                    @property
                    def first(self):
                        return self
                    def count(self):
                        return 0
                    def inner_text(self):
                        return ''
                return _Push()
            if selector == '[aria-label="批准"]':
                return _PollingLocator([1])
            return _PollingLocator([0])

        def get_by_text(self, text, exact=False):
            if text == '联系人信息' and exact:
                return _PollingLocator([0])
            if text == '没有要审核的成员' and exact:
                return _PollingLocator([0])
            return _PollingLocator([0])

        def wait_for_timeout(self, value):
            return None

    executor._page = _Page()
    executor._ensure_browser = lambda: None
    executor._open_group_info = lambda: None
    executor._open_pending_review = lambda pending_before: {'opened_via': 'review_text', 'row_count': 2, 'approve_count': 2, 'empty_queue_detected': False}
    executor._click_approve_action = lambda row: None
    executor._same_session_verify = lambda **kwargs: {
        'pending_count': 0,
        'member_count': 5,
        'queue_delta': True,
        'member_confirmed': True,
        'all_phones_normalized': ['+861****0933'],
        'body_excerpt': 'queue drained and target member confirmed',
    }
    executor._reset_browser = lambda reason: None

    result = executor.approve({
        'registration_group': '8️⃣5️⃣',
        'approved_count': 1,
        'approval_run_id': 'registration_group_approval_hint_breaks_ambiguity',
        'target_name_hint': '~Eastion',
        'target_phone_hint': '+86 138 6064 0933',
    })

    assert result['result_code'] == 'approved'
    assert result['verified'] is True
    assert result['target_member']['phone_normalized'].startswith('+861')
    assert result['target_member']['phone_normalized'].endswith('0933')
    assert result['raw_result']['expected_name'] == '~Eastion'
    assert result['raw_result']['expected_phone'] == '+8613860640933'
    assert result['raw_result']['selection_reason'] == 'exact_phone_match'
    assert result['raw_result']['selected_candidate']['display_name'] == '~Eastion'


class _SnapshotGroupStatePage:
    def __init__(self, body_text):
        self.body_text = body_text

    def locator(self, selector, *args, **kwargs):
        if selector == 'body':
            page = self

            class _Body:
                def inner_text(self, timeout=None):
                    return page.body_text

            return _Body()
        return _PollingLocator([0])


def test_snapshot_group_state_uses_bounded_body_timeout():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()

    class _Body:
        def __init__(self):
            self.timeouts = []

        def inner_text(self, timeout=None):
            self.timeouts.append(timeout)
            return '群组信息\n待处理请求\n2\n+852 4456 8277'

    class _Page:
        def __init__(self):
            self.body = _Body()

        def locator(self, selector, *args, **kwargs):
            if selector == 'body':
                return self.body
            raise AssertionError(selector)

    page = _Page()
    executor._page = page

    snapshot = executor._snapshot_group_state()

    assert snapshot['pending_count'] == 2
    assert snapshot['all_phones_normalized'][0].startswith('+852')
    assert page.body.timeouts == [1200]


def test_same_session_verify_exits_early_once_queue_delta_is_seen_on_empty_queue_surface():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(verify_timeout_ms=1000, verify_poll_ms=80)
    body = '待处理请求\n没有要审核的成员\n请求加入该群组且等待批准的用户将在此显示。'
    page = _SnapshotGroupStatePage(body)
    waits = []
    page.wait_for_timeout = lambda value: waits.append(value)
    executor._page = page

    snapshot = executor._same_session_verify(target_phone='+85244568277', pending_before=2)

    assert snapshot['queue_delta'] is True
    assert snapshot['member_confirmed'] is False
    assert snapshot['empty_queue_detected'] is True
    assert waits == []


def test_same_session_verify_exits_early_once_queue_delta_is_seen_on_contact_info_surface():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(verify_timeout_ms=1000, verify_poll_ms=80)
    body = '联系人信息\n+852 4456 8277\n影音内容、链接和文档\n加密'
    page = _SnapshotGroupStatePage(body)
    waits = []
    page.wait_for_timeout = lambda value: waits.append(value)
    executor._page = page

    snapshot = executor._same_session_verify(target_phone='+8613860640933', pending_before=2)

    assert snapshot['queue_delta'] is True
    assert snapshot['member_confirmed'] is False
    assert snapshot['contact_info_detected'] is True
    assert waits == []


def test_same_session_verify_confirms_target_once_queue_delta_is_seen_for_exact_phone_match_selection():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(verify_timeout_ms=1000, verify_poll_ms=80)
    body = '待处理请求\n没有要审核的成员\n请求加入该群组且等待批准的用户将在此显示。'
    page = _SnapshotGroupStatePage(body)
    waits = []
    page.wait_for_timeout = lambda value: waits.append(value)
    executor._page = page

    snapshot = executor._same_session_verify(
        target_phone='+852 4456 8277',
        pending_before=2,
        target_confirmation_hint={
            'selection_reason': 'exact_phone_match',
            'selected_candidate': {
                'phones': ['+85244568277'],
                'exact_phone_match': True,
                'display_name': '~G2',
            },
        },
    )

    assert snapshot['queue_delta'] is True
    assert snapshot['member_confirmed'] is True
    assert snapshot['member_confirmation_source'] == 'selected_candidate_exact_phone_match'
    assert snapshot['empty_queue_detected'] is True
    assert waits == []


def test_approve_skips_delayed_verification_when_exact_phone_selected_and_queue_delta_is_already_seen():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(post_click_wait_ms=0)
    executor._warm = True
    executor._context = object()
    executor._group_info_ready = True
    executor._page_ready_for_approval = lambda: True

    class _Page:
        def locator(self, selector, *args, **kwargs):
            if selector == 'body':
                class _Body:
                    def inner_text(self, timeout=None):
                        return '待处理请求\n通过邀请链接\n+852 4456 8277\n~G2\n由+852 4456 8277添加'
                return _Body()
            return _PollingLocator([0])

        def wait_for_timeout(self, value):
            return None

    class _Row:
        def inner_text(self, timeout=None):
            return '+852 4456 8277\n~G2\n由+852 4456 8277添加'

        def locator(self, selector, *args, **kwargs):
            return _PollingLocator([0])

    verify_kwargs = []
    open_group_info_calls = []

    executor._page = _Page()
    executor._ensure_browser = lambda: None
    executor._open_group_info = lambda: open_group_info_calls.append('open')
    executor._open_pending_review = lambda pending_before: {'opened_via': 'review_text', 'row_count': 1, 'approve_count': 1, 'empty_queue_detected': False}
    executor._wait_for_review_row = lambda **kwargs: _Row()
    executor._last_review_selection = {
        'candidate_rows': [{'index': 0, 'display_name': '~G2', 'phones': ['+85244568277'], 'actionable': True, 'exact_phone_match': True}],
        'selected_candidate': {'index': 0, 'display_name': '~G2', 'phones': ['+85244568277'], 'actionable': True, 'exact_phone_match': True},
        'selection_reason': 'exact_phone_match',
    }
    executor._click_approve_action = lambda row: None

    def _verify(**kwargs):
        verify_kwargs.append(kwargs)
        return {
            'pending_count': 0,
            'member_count': 5,
            'all_phones_normalized': [],
            'body_excerpt': 'queue drained on empty queue surface',
            'queue_delta': True,
            'member_confirmed': True,
            'member_confirmation_source': 'selected_candidate_exact_phone_match',
            'empty_queue_detected': True,
            'contact_info_detected': False,
        }

    executor._same_session_verify = _verify

    result = executor.approve({
        'registration_group': '8️⃣5️⃣',
        'approved_count': 1,
        'target_phone_hint': '+852 4456 8277',
        'target_name_hint': '~G2',
    })

    assert result['verified'] is True
    assert result['raw_result']['delayed_verification_attempted'] is False
    assert verify_kwargs[0]['target_confirmation_hint']['selection_reason'] == 'exact_phone_match'
    assert open_group_info_calls == ['open']


def test_approve_prefers_selected_candidate_name_over_global_pushname_noise():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(post_click_wait_ms=0)
    executor._warm = True
    executor._context = object()
    executor._group_info_ready = True
    executor._page_ready_for_approval = lambda: True

    class _Page:
        def locator(self, selector, *args, **kwargs):
            if selector == 'body':
                class _Body:
                    def inner_text(self, timeout=None):
                        return (
                            '群组信息\n群组 · 4位成员\n待处理请求\n2\n'
                            '+852 6775 5475\n~zhu z\n+62 851-9830-6838\n+62 858-1347-8460\n+62 851-9830-6832\n'
                        )
                return _Body()
            if selector == '[data-testid="row"]':
                return _SelectableRowLocator([
                    _SelectableRow('+86 138 6064 0933\n~Eastion\n由+86 138 6064 0933添加', approve_count=1),
                    _SelectableRow('+852 4456 8277\n~G2\n由+852 4456 8277添加', approve_count=1),
                ])
            if selector == '[data-testid="pushname"]':
                class _Push:
                    @property
                    def first(self):
                        return self
                    def count(self):
                        return 1
                    def inner_text(self, timeout=None):
                        return '~Eastion'
                return _Push()
            if selector == '[aria-label="批准"]':
                return _PollingLocator([1])
            return _PollingLocator([0])

        def get_by_text(self, text, exact=False):
            if text == '联系人信息' and exact:
                return _PollingLocator([0])
            if text == '没有要审核的成员' and exact:
                return _PollingLocator([0])
            return _PollingLocator([0])

        def wait_for_timeout(self, value):
            return None

    executor._page = _Page()
    executor._ensure_browser = lambda: None
    executor._open_group_info = lambda: None
    executor._open_pending_review = lambda pending_before: {'opened_via': 'review_text', 'row_count': 2, 'approve_count': 2, 'empty_queue_detected': False}
    executor._click_approve_action = lambda row: None
    executor._same_session_verify = lambda **kwargs: {
        'pending_count': 0,
        'member_count': 5,
        'queue_delta': True,
        'member_confirmed': True,
        'all_phones_normalized': ['+852****8277'],
        'body_excerpt': 'queue drained and target member confirmed',
    }
    executor._reset_browser = lambda reason: None

    result = executor.approve({
        'registration_group': '8️⃣5️⃣',
        'approved_count': 1,
        'approval_run_id': 'registration_group_approval_target_member_consistency',
        'target_name_hint': '~G2',
        'target_phone_hint': '+852 4456 8277',
    })

    assert result['result_code'] == 'approved'
    assert result['verified'] is True
    assert result['target_member']['name'] == '~G2'
    assert result['raw_result']['selected_candidate']['display_name'] == '~G2'
    assert result['target_member']['phone_normalized'].startswith('+852')
    assert result['target_member']['phone_normalized'].endswith('8277')


def test_approve_recovers_with_delayed_second_verification_after_queue_consumed():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(post_click_wait_ms=0)
    executor._warm = True
    executor._context = object()
    executor._group_info_ready = True
    executor._page_ready_for_approval = lambda: True

    class _Page:
        def locator(self, selector, *args, **kwargs):
            if selector == 'body':
                class _Body:
                    def inner_text(self, timeout=None):
                        return '待处理请求\n通过邀请链接\n+852 4456 8277\n~G2\n由+852 4456 8277添加'
                return _Body()
            if selector == '[data-testid="pushname"]':
                class _Push:
                    @property
                    def first(self):
                        return self
                    def count(self):
                        return 1
                    def inner_text(self, timeout=None):
                        return '~G2'
                return _Push()
            return _PollingLocator([0])

        def wait_for_timeout(self, value):
            return None

    class _Row:
        def inner_text(self, timeout=None):
            return '+852 4456 8277\n~G2\n由+852 4456 8277添加'

        def locator(self, selector, *args, **kwargs):
            return _PollingLocator([0])

    verify_results = [
        {
            'pending_count': 0,
            'member_count': 4,
            'all_phones_normalized': [],
            'body_excerpt': 'queue drained but contact surface still open',
            'queue_delta': True,
            'member_confirmed': False,
            'empty_queue_detected': True,
            'contact_info_detected': False,
        },
        {
            'pending_count': 0,
            'member_count': 5,
            'all_phones_normalized': ['+852****8277'],
            'body_excerpt': 'target confirmed after reopening group info',
            'queue_delta': True,
            'member_confirmed': True,
            'empty_queue_detected': True,
            'contact_info_detected': False,
        },
    ]
    open_group_info_calls = []

    executor._page = _Page()
    executor._ensure_browser = lambda: None
    executor._open_group_info = lambda: open_group_info_calls.append('open')
    executor._open_pending_review = lambda pending_before: {'opened_via': 'review_text', 'row_count': 1, 'approve_count': 1, 'empty_queue_detected': False}
    executor._wait_for_review_row = lambda **kwargs: _Row()
    executor._last_review_selection = {
        'candidate_rows': [{'index': 0, 'display_name': '~G2', 'phones': ['+852****8277'], 'actionable': True}],
        'selected_candidate': {'index': 0, 'display_name': '~G2', 'phones': ['+852****8277'], 'actionable': True},
        'selection_reason': 'single_actionable_row',
    }
    executor._click_approve_action = lambda row: None
    executor._same_session_verify = lambda **kwargs: verify_results.pop(0)

    result = executor.approve({'registration_group': '8️⃣5️⃣', 'approved_count': 1})

    assert result['verified'] is True
    assert result['raw_result']['delayed_verification_attempted'] is True
    assert result['raw_result']['delayed_verification_snapshot']['member_confirmed'] is True
    assert open_group_info_calls == ['open']


def test_approve_reopens_group_info_for_delayed_verification_when_contact_info_is_detected():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(post_click_wait_ms=0)
    executor._warm = True
    executor._context = object()
    executor._group_info_ready = True
    executor._page_ready_for_approval = lambda: True

    class _Page:
        def locator(self, selector, *args, **kwargs):
            if selector == 'body':
                class _Body:
                    def inner_text(self, timeout=None):
                        return '待处理请求\n通过邀请链接\n+852 4456 8277\n~G2\n由+852 4456 8277添加'
                return _Body()
            if selector == '[data-testid="pushname"]':
                class _Push:
                    @property
                    def first(self):
                        return self
                    def count(self):
                        return 1
                    def inner_text(self, timeout=None):
                        return '~G2'
                return _Push()
            return _PollingLocator([0])

        def wait_for_timeout(self, value):
            return None

    class _Row:
        def inner_text(self, timeout=None):
            return '+852 4456 8277\n~G2\n由+852 4456 8277添加'

        def locator(self, selector, *args, **kwargs):
            return _PollingLocator([0])

    verify_results = [
        {
            'pending_count': 0,
            'member_count': 4,
            'all_phones_normalized': [],
            'body_excerpt': 'queue drained but contact surface still open',
            'queue_delta': True,
            'member_confirmed': False,
            'empty_queue_detected': False,
            'contact_info_detected': True,
        },
        {
            'pending_count': 0,
            'member_count': 5,
            'all_phones_normalized': ['+852****8277'],
            'body_excerpt': 'target confirmed after reopening group info',
            'queue_delta': True,
            'member_confirmed': True,
            'empty_queue_detected': True,
            'contact_info_detected': False,
        },
    ]
    open_group_info_calls = []

    executor._page = _Page()
    executor._ensure_browser = lambda: None
    executor._open_group_info = lambda: open_group_info_calls.append('open')
    executor._open_pending_review = lambda pending_before: {'opened_via': 'review_text', 'row_count': 1, 'approve_count': 1, 'empty_queue_detected': False}
    executor._wait_for_review_row = lambda **kwargs: _Row()
    executor._last_review_selection = {
        'candidate_rows': [{'index': 0, 'display_name': '~G2', 'phones': ['+852****8277'], 'actionable': True}],
        'selected_candidate': {'index': 0, 'display_name': '~G2', 'phones': ['+852****8277'], 'actionable': True},
        'selection_reason': 'single_actionable_row',
    }
    executor._click_approve_action = lambda row: None
    executor._same_session_verify = lambda **kwargs: verify_results.pop(0)

    result = executor.approve({'registration_group': '8️⃣5️⃣', 'approved_count': 1})

    assert result['verified'] is True
    assert result['raw_result']['delayed_verification_attempted'] is True
    assert open_group_info_calls == ['open', 'open']


class _ApproveActionPollingPage:
    def __init__(self, before_row_click_counts, after_row_click_counts):
        self.before_row_click_counts = list(before_row_click_counts)
        self.after_row_click_counts = list(after_row_click_counts)
        self.row_clicked = False
        self.waits = []
        self.global_approve_clicks = 0
        self.submission_confirmed = False

    def locator(self, selector, *args, **kwargs):
        if selector == '[aria-label="批准"]':
            page = self

            class _Locator:
                @property
                def first(self):
                    return self

                def count(self):
                    if page.submission_confirmed:
                        return 0
                    counts = page.after_row_click_counts if page.row_clicked else page.before_row_click_counts
                    if len(counts) > 1:
                        return counts.pop(0)
                    return counts[0]

                def click(self, **kwargs):
                    page.global_approve_clicks += 1
                    page.submission_confirmed = True
                    return None

            return _Locator()
        if selector == '[data-testid="row"]':
            page = self

            class _Rows:
                def count(self):
                    return 0 if page.submission_confirmed else 1

            return _Rows()
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

    assert row_approve.clicks >= 1
    assert row.clicks == 1
    assert executor._page.waits
    assert executor._page.global_approve_clicks == 1


class _RowApproveBecomesReadyAfterRowClickPage:
    def __init__(self):
        self.row_clicked = False
        self.row_approve_clicks = 0
        self.waits = []

    def locator(self, selector, *args, **kwargs):
        if selector == '[aria-label="批准"]':
            page = self

            class _Locator:
                @property
                def first(self):
                    return self

                def count(self):
                    return 0

                def click(self, **kwargs):
                    raise RuntimeError('global approve still unavailable')

            return _Locator()
        if selector == '[data-testid="row"]':
            page = self

            class _Rows:
                def count(self):
                    return 0 if page.row_approve_clicks else 1

            return _Rows()
        if selector == 'body':
            page = self

            class _Body:
                def inner_text(self, timeout=None):
                    return '待处理请求\n没有要审核的成员' if page.row_approve_clicks else '待处理请求\n通过邀请链接\n+852 4456 8277\n~G2'

            return _Body()
        return _PollingLocator([0])

    def get_by_text(self, text, exact=False):
        page = self

        class _Text:
            def count(self):
                if text == '没有要审核的成员' and exact:
                    return 1 if page.row_approve_clicks else 0
                if text == '联系人信息' and exact:
                    return 0
                return 0

        return _Text()

    def wait_for_timeout(self, value):
        self.waits.append(value)
        return None


class _RowApproveBecomesReadyAfterRowClick(_ApproveRow):
    def __init__(self, page):
        super().__init__(_ApproveButtonLocator(raises=True))
        self.page = page
        self.row_approve_attempts = 0

    def locator(self, selector, *args, **kwargs):
        if selector == '[aria-label="批准"]':
            row = self
            page = self.page

            class _RowApprove:
                def click(self, **kwargs):
                    row.row_approve_attempts += 1
                    if not page.row_clicked:
                        raise RuntimeError('row approve unavailable before row click')
                    page.row_approve_clicks += 1
                    return None

            return _RowApprove()
        return super().locator(selector, *args, **kwargs)

    def click(self, **kwargs):
        self.page.row_clicked = True
        return super().click(**kwargs)


def test_click_approve_action_retries_row_approve_after_row_click_before_global_fallback():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(post_click_wait_ms=0)
    page = _RowApproveBecomesReadyAfterRowClickPage()
    executor._page = page
    row = _RowApproveBecomesReadyAfterRowClick(page)

    executor._click_approve_action(row)

    assert row.clicks == 1
    assert row.row_approve_attempts >= 2
    assert page.row_approve_clicks == 1
    assert page.waits


class _SubmissionConfirmedByJoinedMarkerPage:
    def __init__(self):
        self.waits = []
        self.joined = False
        self.global_clicks = 0

    def locator(self, selector, *args, **kwargs):
        if selector == '[aria-label="批准"]':
            page = self

            class _Locator:
                @property
                def first(self):
                    return self

                def count(self):
                    return 0 if page.joined else 1

                def click(self, **kwargs):
                    page.global_clicks += 1
                    page.joined = True
                    return None

            return _Locator()
        if selector == '[data-testid="row"]':
            page = self

            class _Rows:
                def count(self):
                    return 1

            return _Rows()
        if selector == 'body':
            page = self

            class _Body:
                def inner_text(self, timeout=None):
                    if page.joined:
                        return '你已通过邀请链接加入\n群组 · 6位成员\n输入消息'
                    return '待处理请求\n通过邀请链接\n+852 4456 8277\n~G2\n由+852 4456 8277添加'

            return _Body()
        return _PollingLocator([0])

    def get_by_text(self, text, exact=False):
        page = self

        class _Text:
            def count(self):
                if text == '没有要审核的成员' and exact:
                    return 0
                if text == '联系人信息' and exact:
                    return 0
                return 0

        return _Text()

    def wait_for_timeout(self, value):
        self.waits.append(value)
        return None


class _RowApproveTurnsIntoJoinedMarker(_ApproveRow):
    def __init__(self, page):
        super().__init__(_ApproveButtonLocator(raises=False))
        self.page = page

    def locator(self, selector, *args, **kwargs):
        if selector == '[aria-label="批准"]':
            row = self
            page = self.page

            class _RowApprove:
                def click(self, **kwargs):
                    row.approve_locator.clicks += 1
                    page.joined = True
                    return None

            return _RowApprove()
        return super().locator(selector, *args, **kwargs)


def test_click_approve_action_treats_joined_marker_without_review_controls_as_submitted():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(post_click_wait_ms=0)
    page = _SubmissionConfirmedByJoinedMarkerPage()
    executor._page = page
    row = _RowApproveTurnsIntoJoinedMarker(page)

    executor._click_approve_action(row)

    assert row.approve_locator.clicks >= 1
    assert page.global_clicks == 0
    assert page.joined is True


class _ContactInfoAfterRowClickPage:
    def __init__(self):
        self.row_clicked = False
        self.waits = []

    def locator(self, selector, *args, **kwargs):
        if selector == '[aria-label="批准"]':
            class _Locator:
                @property
                def first(self):
                    return self

                def count(self):
                    return 0

                def click(self, **kwargs):
                    raise RuntimeError('no global approve button')

            return _Locator()
        if selector == '[data-testid="row"]':
            page = self

            class _Rows:
                def count(self):
                    return 0 if page.row_clicked else 1

            return _Rows()
        if selector == 'body':
            page = self

            class _Body:
                def inner_text(self, timeout=None):
                    if page.row_clicked:
                        return '联系人信息\n+852 4456 8277\n待处理请求'
                    return '待处理请求\n通过邀请链接\n+852 4456 8277'

            return _Body()
        return _PollingLocator([0])

    def get_by_text(self, text, exact=False):
        page = self

        class _Text:
            def count(self):
                if text == '联系人信息' and exact:
                    return 1 if page.row_clicked else 0
                if text == '没有要审核的成员' and exact:
                    return 0
                return 0

        return _Text()

    def wait_for_timeout(self, value):
        self.waits.append(value)
        return None


def test_click_approve_action_raises_recovery_when_row_click_opens_contact_info():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor(post_click_wait_ms=0)
    page = _ContactInfoAfterRowClickPage()
    executor._page = page
    row_approve = _ApproveButtonLocator(raises=True)
    row = _ApproveRow(row_approve)
    original_row_click = row.click

    def _row_click(**kwargs):
        page.row_clicked = True
        return original_row_click(**kwargs)

    row.click = _row_click

    try:
        executor._click_approve_action(row)
    except ReviewSurfaceRecoveryRequired as exc:
        assert 'contact info opened after row click' in str(exc)
    else:
        raise AssertionError('expected review surface recovery error')


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


class _SelectableApproveLocator:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class _SelectableRow:
    def __init__(self, text, *, approve_count=0):
        self.text = text
        self.approve_count = approve_count

    def inner_text(self, timeout=None):
        return self.text

    def locator(self, selector, *args, **kwargs):
        if selector == '[aria-label="批准"]':
            return _SelectableApproveLocator(self.approve_count)
        return _PollingLocator([0])


class _SelectableRowLocator:
    def __init__(self, rows):
        self.rows = list(rows)

    def count(self):
        return len(self.rows)

    @property
    def first(self):
        return self.rows[0]

    def nth(self, index):
        return self.rows[index]


class _SelectableRowPage:
    def __init__(self, rows):
        self.rows = rows
        self.waits = []

    def locator(self, selector, *args, **kwargs):
        if selector == '[data-testid="row"]':
            return _SelectableRowLocator(self.rows)
        if selector == 'body':
            class _Body:
                def inner_text(self, timeout=None):
                    return '待处理请求\n通过邀请链接\n+852 6775 5475\n~G2\n由+852 6775 5475添加'
            return _Body()
        if selector == '[aria-label="批准"]':
            return _PollingLocator([0])
        return _PollingLocator([0])

    def get_by_text(self, text, exact=False):
        if text == '联系人信息' and exact:
            return _PollingLocator([0])
        if text == '没有要审核的成员' and exact:
            return _PollingLocator([0])
        return _PollingLocator([0])

    def wait_for_timeout(self, value):
        self.waits.append(value)
        return None


def test_wait_for_review_row_prefers_expected_phone_over_first_unrelated_row():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    unrelated = _SelectableRow('+852 4456 8277\n加密\n1个共同群组')
    target = _SelectableRow('+852 6775 5475\n~G2\n通过邀请链接', approve_count=1)
    page = _SelectableRowPage([unrelated, target])
    executor._page = page

    row = executor._wait_for_review_row(expected_phone='+852 6775 5475')

    assert row is target
    assert executor._last_review_selection['selection_reason'] == 'exact_phone_match'
    assert executor._last_review_selection['selected_candidate']['display_name'] == '~G2'


def test_wait_for_review_row_marks_ambiguous_when_multiple_actionable_rows_remain():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    first = _SelectableRow('+852 4456 8277\n~G2\n通过邀请链接', approve_count=1)
    second = _SelectableRow('+852 5566 8899\n~G3\n通过邀请链接', approve_count=1)
    page = _SelectableRowPage([first, second])
    executor._page = page

    try:
        executor._wait_for_review_row()
    except AmbiguousReviewTargetError as exc:
        assert 'multiple actionable review rows' in str(exc)
    else:
        raise AssertionError('expected ambiguous review rows to be rejected')

    assert len(executor._last_review_selection['candidate_rows']) == 2
    assert executor._last_review_selection['selected_candidate'] == {}


def test_extract_pending_candidates_prefers_pending_section_phone_and_requester():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    body = (
        '聊天历史\n'
        '~Eastion\n请求加入。点击以审核。\n'
        '待处理请求\n'
        '通过邀请链接\n'
        '+852 6775 5475\n'
        '~G2\n'
        '由+852 6775 5475添加\n'
        '输入消息\n'
        '联系人信息\n'
        '+852 4456 8277\n'
    )

    candidates = executor._extract_pending_candidates(body)

    assert candidates['phones'][0].startswith('+852')
    assert candidates['phones'][0].endswith('5475')
    assert candidates['requesters'][0] == '~G2'


def test_extract_pending_candidates_prefers_last_pending_section_over_historical_pending_section():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    body = (
        '聊天历史\n'
        '待处理请求\n'
        '通过邀请链接\n'
        '+852 6775 5475\n'
        '~G2\n'
        '由+852 6775 5475添加\n'
        '更多历史\n'
        '待处理请求\n'
        '没有要审核的成员\n'
        '请求加入该群组且等待批准的用户将在此显示。\n'
        '输入消息\n'
    )

    candidates = executor._extract_pending_candidates(body)

    assert candidates['phones'] == []
    assert candidates['requesters'] == []


def test_extract_pending_candidates_ignores_historical_chat_rows_when_group_info_panel_is_open():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    body = (
        '聊天历史\n'
        '~Eastion\n请求加入。点击以审核。\n'
        '待处理请求\n'
        '通过邀请链接\n'
        '+852 6775 5475\n'
        '~G2\n'
        '由+852 6775 5475添加\n'
        '群组信息\n'
        '群组 · 4位成员\n'
        '添加成员\n'
        '使用链接邀请加入群组\n'
    )

    candidates = executor._extract_pending_candidates(body)

    assert candidates['phones'] == []
    assert candidates['requesters'] == []


class _ContactInfoRowReadyPage:
    def __init__(self):
        self.waits = []
        self.contact_info_visible = True

    def locator(self, selector, *args, **kwargs):
        if selector == '[data-testid="row"]':
            class _RowLocator:
                def count(self):
                    return 2

                @property
                def first(self):
                    class _Row:
                        def inner_text(self, timeout=None):
                            return '影音内容、链接和文档'
                    return _Row()
            return _RowLocator()
        if selector == 'body':
            class _Body:
                def inner_text(self, timeout=None):
                    return '联系人信息\n+852 4456 8277\n影音内容、链接和文档'
            return _Body()
        if selector == '[aria-label="批准"]':
            return _PollingLocator([0])
        return _PollingLocator([0])

    def get_by_text(self, text, exact=False):
        if text == '联系人信息' and exact:
            return _PollingLocator([1 if self.contact_info_visible else 0])
        if text == '没有要审核的成员' and exact:
            return _PollingLocator([0])
        return _PollingLocator([0])

    def wait_for_timeout(self, value):
        self.waits.append(value)
        return None


def test_wait_for_review_row_rejects_contact_info_rows():
    executor = LiveWarmWhatsAppRegistrationGroupApprovalExecutor()
    page = _ContactInfoRowReadyPage()
    executor._page = page

    try:
        executor._wait_for_review_row()
    except RuntimeError as exc:
        assert 'review row unavailable' in str(exc)
    else:
        raise AssertionError('expected contact info rows to be rejected as actionable review rows')
