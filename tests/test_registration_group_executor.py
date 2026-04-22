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
    assert Path(executor.temp_user_data_dir).exists()


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
    def __init__(self, counts=None):
        self.counts = dict(counts or {})
        self.locator_clicks = 0

    def get_by_text(self, text, exact=False):
        return _PollingLocator([self.counts.get((text, exact), 0)])

    def get_by_role(self, *args, **kwargs):
        return _PollingLocator([0])

    def locator(self, *args, **kwargs):
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


class _RowWaitPage:
    def __init__(self, counts):
        self.counts = list(counts)
        self.waits = []

    def locator(self, selector, *args, **kwargs):
        if selector == '[data-testid="row"]':
            locator = _PollingLocator(self.counts)
            return locator
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
