import asyncio


def test_enter_groups_tab_falls_back_to_text_when_role_click_times_out():
    from scripts.whatsapp_live_monitor import _enter_groups_tab

    class _AsyncLocator:
        def __init__(self, *, fail_click=False):
            self.fail_click = fail_click
            self.clicks = 0

        async def click(self, **kwargs):
            self.clicks += 1
            if self.fail_click:
                raise RuntimeError('role click timeout')
            return None

    class _AsyncPage:
        def __init__(self):
            self.role_locator = _AsyncLocator(fail_click=True)
            self.text_locator = _AsyncLocator(fail_click=False)
            self.waits = []

        def get_by_role(self, *args, **kwargs):
            return self.role_locator

        def get_by_text(self, *args, **kwargs):
            return self.text_locator

        async def wait_for_timeout(self, value):
            self.waits.append(value)
            return None

    page = _AsyncPage()
    result = asyncio.run(_enter_groups_tab(page, navigation_wait_ms=120))

    assert result == 'text_fallback'
    assert page.role_locator.clicks == 1
    assert page.text_locator.clicks == 1
    assert page.waits


def test_enter_groups_tab_raises_after_both_paths_fail():
    from scripts.whatsapp_live_monitor import _enter_groups_tab

    class _AsyncLocator:
        def __init__(self):
            self.clicks = 0

        async def click(self, **kwargs):
            self.clicks += 1
            raise RuntimeError('still not ready')

    class _AsyncPage:
        def __init__(self):
            self.role_locator = _AsyncLocator()
            self.text_locator = _AsyncLocator()
            self.waits = []

        def get_by_role(self, *args, **kwargs):
            return self.role_locator

        def get_by_text(self, *args, **kwargs):
            return self.text_locator

        async def wait_for_timeout(self, value):
            self.waits.append(value)
            return None

    page = _AsyncPage()
    try:
        asyncio.run(_enter_groups_tab(page, navigation_wait_ms=120, timeout_ms=200))
    except RuntimeError as exc:
        assert 'unable to open groups tab' in str(exc)
    else:
        raise AssertionError('expected _enter_groups_tab to raise when all open paths fail')

    assert page.role_locator.clicks >= 1
    assert page.text_locator.clicks >= 1


def test_assert_home_surface_authenticated_rejects_login_gate():
    from scripts.whatsapp_live_monitor import _assert_home_surface_authenticated

    class _BodyLocator:
        async def inner_text(self):
            return '下载 Mac 版 WhatsApp\n扫描登录\n请改用电话号码关联。'

    class _AsyncPage:
        def locator(self, selector, *args, **kwargs):
            assert selector == 'body'
            return _BodyLocator()

    try:
        asyncio.run(_assert_home_surface_authenticated(_AsyncPage()))
    except RuntimeError as exc:
        assert str(exc) == 'whatsapp_home_not_authenticated_in_copied_profile'
    else:
        raise AssertionError('expected copied-profile login gate to be rejected')


def test_ensure_group_info_uses_subheader_fallback_until_panel_is_ready():
    from scripts.whatsapp_live_monitor import _ensure_group_info

    class _AsyncLocator:
        def __init__(self, page, name):
            self.page = page
            self.name = name
            self.clicks = 0

        async def click(self, **kwargs):
            self.clicks += 1
            if self.name == 'header':
                self.page.header_clicks += 1
            if self.name == 'subheader':
                self.page.subheader_clicks += 1
            return None

        async def count(self):
            if self.name == 'group_info':
                return 1 if self.page.subheader_clicks else 0
            if self.name == 'pending_section':
                return 1 if self.page.subheader_clicks else 0
            if self.name == 'empty_queue':
                return 0
            if self.name == 'contact_info':
                return 0
            if self.name == 'membership_request':
                return 0
            return 0

    class _AsyncPage:
        def __init__(self):
            self.header_clicks = 0
            self.subheader_clicks = 0
            self.waits = []

        def locator(self, selector, *args, **kwargs):
            mapping = {
                '[data-testid="conversation-header"]': _AsyncLocator(self, 'header'),
                '[data-testid="conversation-subheader"]': _AsyncLocator(self, 'subheader'),
                '[data-testid="subtype-membership_approval_request"]': _AsyncLocator(self, 'membership_request'),
            }
            return mapping.get(selector, _AsyncLocator(self, 'unknown'))

        def get_by_text(self, text, exact=False):
            mapping = {
                ('群组信息', True): _AsyncLocator(self, 'group_info'),
                ('待处理请求', True): _AsyncLocator(self, 'pending_section'),
                ('没有要审核的成员', True): _AsyncLocator(self, 'empty_queue'),
                ('联系人信息', True): _AsyncLocator(self, 'contact_info'),
            }
            return mapping.get((text, exact), _AsyncLocator(self, 'unknown'))

        async def wait_for_timeout(self, value):
            self.waits.append(value)
            return None

    page = _AsyncPage()

    opened_via = asyncio.run(_ensure_group_info(page, navigation_wait_ms=120, timeout_ms=500))

    assert opened_via == 'conversation_subheader'
    assert page.header_clicks >= 1
    assert page.subheader_clicks >= 1
    assert page.waits


def test_ensure_group_info_raises_when_group_info_surface_never_appears():
    from scripts.whatsapp_live_monitor import _ensure_group_info

    class _AsyncLocator:
        def __init__(self):
            self.clicks = 0

        async def click(self, **kwargs):
            self.clicks += 1
            return None

        async def count(self):
            return 0

    class _AsyncPage:
        def __init__(self):
            self.header = _AsyncLocator()
            self.subheader = _AsyncLocator()
            self.membership = _AsyncLocator()
            self.waits = []

        def locator(self, selector, *args, **kwargs):
            if selector == '[data-testid="conversation-header"]':
                return self.header
            if selector == '[data-testid="conversation-subheader"]':
                return self.subheader
            if selector == '[data-testid="subtype-membership_approval_request"]':
                return self.membership
            return _AsyncLocator()

        def get_by_text(self, text, exact=False):
            return _AsyncLocator()

        async def wait_for_timeout(self, value):
            self.waits.append(value)
            return None

    page = _AsyncPage()

    try:
        asyncio.run(_ensure_group_info(page, navigation_wait_ms=120, timeout_ms=250))
    except RuntimeError as exc:
        assert 'unable to open group info surface' in str(exc)
    else:
        raise AssertionError('expected _ensure_group_info to raise when the group info surface never appears')


def test_page_ready_for_group_info_ignores_membership_request_markers_on_chat_list():
    from scripts.whatsapp_live_monitor import _page_ready_for_group_info

    class _AsyncLocator:
        def __init__(self, count_value):
            self.count_value = count_value

        async def count(self):
            return self.count_value

    class _AsyncPage:
        def get_by_text(self, text, exact=False):
            return _AsyncLocator(0)

        def locator(self, selector, *args, **kwargs):
            if selector == '[data-testid="subtype-membership_approval_request"]':
                return _AsyncLocator(3)
            return _AsyncLocator(0)

    ready = asyncio.run(_page_ready_for_group_info(_AsyncPage()))

    assert ready is False
