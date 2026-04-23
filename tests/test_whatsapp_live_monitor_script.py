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
