import errno
import importlib.util
import sys
import types
from pathlib import Path


class _BootstrapAsyncChromium:
    async def launch_persistent_context(self, *args, **kwargs):
        raise AssertionError('test should not launch browser')


class _BootstrapAsyncPlaywrightContextManager:
    async def __aenter__(self):
        return types.SimpleNamespace(chromium=_BootstrapAsyncChromium())

    async def __aexit__(self, exc_type, exc, tb):
        return False


fake_async_api = types.ModuleType('playwright.async_api')
fake_async_api.async_playwright = lambda: _BootstrapAsyncPlaywrightContextManager()
fake_playwright = types.ModuleType('playwright')
fake_playwright.async_api = fake_async_api
sys.modules.setdefault('playwright', fake_playwright)
sys.modules.setdefault('playwright.async_api', fake_async_api)


MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'whatsapp_live_monitor.py'
SPEC = importlib.util.spec_from_file_location('test_whatsapp_live_monitor_module', MODULE_PATH)
whatsapp_live_monitor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(whatsapp_live_monitor)


def test_allocate_run_temp_user_data_dir_creates_unique_sibling_dirs(tmp_path):
    base_dir = tmp_path / 'chrome-whatsapp-live-monitor'

    first = whatsapp_live_monitor._allocate_run_temp_user_data_dir(base_dir)
    second = whatsapp_live_monitor._allocate_run_temp_user_data_dir(base_dir)

    assert first != second
    assert first.parent == base_dir.parent
    assert second.parent == base_dir.parent
    assert first.name.startswith(base_dir.name + '-')
    assert second.name.startswith(base_dir.name + '-')
    assert first.exists()
    assert second.exists()


def test_safe_rmtree_retries_directory_not_empty(monkeypatch, tmp_path):
    target = tmp_path / 'temp-profile'
    target.mkdir()
    calls = []

    def fake_rmtree(path):
        calls.append(Path(path))
        if len(calls) == 1:
            raise OSError(errno.ENOTEMPTY, 'Directory not empty')
        target.rmdir()

    monkeypatch.setattr(whatsapp_live_monitor.shutil, 'rmtree', fake_rmtree)
    monkeypatch.setattr(whatsapp_live_monitor.time, 'sleep', lambda *_args, **_kwargs: None)

    whatsapp_live_monitor._safe_rmtree(target, attempts=2, delay_seconds=0)

    assert len(calls) == 2
    assert not target.exists()
