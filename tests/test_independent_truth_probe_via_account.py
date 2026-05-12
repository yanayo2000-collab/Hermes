from __future__ import annotations

import json
from pathlib import Path
import importlib.util

import pytest

MODULE_PATH = Path('/Users/chauncey/work/mcn-ai-automation/scripts/independent_truth_probe_via_account.py')
spec = importlib.util.spec_from_file_location('independent_truth_probe_via_account', MODULE_PATH)
probe = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(probe)


class DummyResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_load_internal_token_reads_exported_shell_file(tmp_path: Path):
    env_path = tmp_path / 'internal_auth.env'
    env_path.write_text("export AUTH_INTERNAL_TOKEN='abc123'\n", encoding='utf-8')

    assert probe.load_internal_token(env_path) == 'abc123'


def test_fetch_truth_probe_payload_uses_runtime_start_and_group_state(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=30):
        calls.append((request.full_url, request.get_method(), json.loads(request.data.decode('utf-8')) if request.data else None))
        if request.full_url.endswith('/runtime/internal/start'):
            return DummyResponse({'runtime': {'base_url': 'http://127.0.0.1:53563'}})
        if request.full_url == 'http://127.0.0.1:53563/group-state':
            return DummyResponse({'group_name': '注册测试1', 'pending_count': 2, 'review_surface_ready': True})
        raise AssertionError(request.full_url)

    monkeypatch.setattr(probe.urllib.request, 'urlopen', fake_urlopen)

    payload = probe.fetch_truth_probe_payload(
        api_base_url='http://127.0.0.1:8011',
        internal_token='tok',
        account_key='truth-probe-regtest1',
        registration_group='注册测试1',
    )

    assert payload['pending_count'] == 2
    assert calls[0][0].endswith('/api/ops/whatsapp-approval-accounts/truth-probe-regtest1/runtime/internal/start')
    assert calls[1][0] == 'http://127.0.0.1:53563/group-state'
    assert calls[1][2] == {'registration_group': '注册测试1'}


def test_build_reconcile_result_wraps_payload_into_async_reconcile_contract():
    result = probe.build_reconcile_result(
        account_key='truth-probe-regtest1',
        registration_group='注册测试1',
        payload={
            'group_id': '120363422719530134@g.us',
            'pending_count': 0,
            'source_ts': '2026-05-09T12:00:00Z',
            'session_health': 'healthy',
        },
    )

    assert result['group_key'] == '120363422719530134@g.us'
    assert result['observed_pending_count'] == 0
    assert result['probe_status'] == 'ok'
    assert result['reconcile_result'] == 'match_zero'
    assert result['authoritative_source'] == 'group_state'


def test_fetch_truth_probe_payload_raises_when_runtime_not_logged_in(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=30):
        calls.append(request.full_url)
        if request.full_url.endswith('/runtime/internal/start'):
            return DummyResponse({'runtime': {'base_url': 'http://127.0.0.1:53563'}})
        if request.full_url.endswith('/session/internal'):
            return DummyResponse({'session': {'login_verified': False, 'qr_available': False, 'login_check_status': 'pending_runtime'}})
        if request.full_url.endswith('/session/internal/start'):
            return DummyResponse({'session': {'login_verified': False, 'qr_available': True, 'login_check_status': 'waiting_for_scan'}})
        raise AssertionError(request.full_url)

    monkeypatch.setattr(probe.urllib.request, 'urlopen', fake_urlopen)

    with pytest.raises(RuntimeError, match='waiting_for_scan'):
        probe.fetch_truth_probe_payload(
            api_base_url='http://127.0.0.1:8011',
            internal_token='tok',
            account_key='truth-probe-regtest1',
            registration_group='注册测试1',
            require_login_verified=True,
            fetch_session_first=True,
        )

    assert calls[-1].endswith('/session/internal/start')
