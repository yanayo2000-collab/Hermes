from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'notify_timo_materialization_complete_only',
    ROOT / 'scripts/notify_timo_materialization.py',
)
assert SPEC and SPEC.loader
NOTIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NOTIFIER)


def _status() -> dict:
    return {
        'ok': False,
        'status': 'partial',
        'run_id': 'run-0823',
        'date_from': '2026-08-23',
        'date_to': '2026-08-23',
        'guild_count': 3,
        'errors': ['Agency MX somente:revenue:source_not_ready'],
        'scopes': [{
            'status': 'partial',
            'data_date_bj': '2026-08-23',
            'run_id': 'run-0823',
            'guild_count': 3,
            'provisional': False,
            'revenue_contract': 'complete_guild_and_streamer',
            'snapshot_at': '2026-08-25T03:00:00+00:00',
        }],
    }


def _manifest(storage_name: str, country: str, checksum_char: str) -> dict:
    return {
        'guild_name': storage_name,
        'country': country,
        'publication_ready': True,
        'row_count': 10,
        'total_income': '123.500000',
        'checksum': checksum_char * 64,
        'revision_version': 2,
        'last_success_sync_id': f'sync-{country.lower()}-2',
        'last_success_time': '2026-08-25T03:00:00+00:00',
    }


def _partial_event() -> dict:
    return NOTIFIER.build_event(
        status=_status(),
        manifests=[
            _manifest('agency of BR somente', 'Brazil', 'b'),
            _manifest('TIMO001', 'Indonesia', 'a'),
        ],
        failures={'Agency MX somente': {'errorCode': 'source_not_ready', 'error': ''}},
    )


def test_partial_is_internal_pending_and_direct_send_fails_closed(monkeypatch):
    event = _partial_event()
    assert NOTIFIER.complete_event_ready_for_downstream(event) is False
    assert NOTIFIER.notification_skip_result(event)['notification_state'] == 'PENDING_REOBSERVE'
    monkeypatch.setattr(NOTIFIER.request, 'urlopen', lambda *args, **kwargs: pytest.fail('network called'))
    with pytest.raises(ValueError, match='downstream_notification_requires_complete'):
        NOTIFIER.send_event(event, url='https://example.invalid', secret='x' * 32)


def test_main_skips_partial_before_secret_http_or_ack(monkeypatch, capsys):
    event = _partial_event()
    monkeypatch.setattr(NOTIFIER, 'load_event', lambda *args, **kwargs: event)
    monkeypatch.setattr(NOTIFIER.Path, 'read_text', lambda *args, **kwargs: pytest.fail('secret read'))
    monkeypatch.setattr(NOTIFIER, 'write_ack', lambda *args, **kwargs: pytest.fail('ack written'))
    monkeypatch.setattr(NOTIFIER.request, 'urlopen', lambda *args, **kwargs: pytest.fail('network called'))
    monkeypatch.setattr(sys, 'argv', ['notify_timo_materialization.py'])
    assert NOTIFIER.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result['notification_state'] == 'PENDING_REOBSERVE'
    assert result['skipped'] == 'downstream_notification_requires_complete'


def test_complete_three_scope_event_remains_publishable():
    status = _status()
    status['ok'] = True
    status['status'] = 'success'
    status['errors'] = []
    status['scopes'][0]['status'] = 'success'
    event = NOTIFIER.build_event(
        status=status,
        manifests=[
            _manifest('Agency MX somente', 'Mexico', 'c'),
            _manifest('agency of BR somente', 'Brazil', 'b'),
            _manifest('TIMO001', 'Indonesia', 'a'),
        ],
        failures={},
    )
    assert NOTIFIER.complete_event_ready_for_downstream(event) is True
