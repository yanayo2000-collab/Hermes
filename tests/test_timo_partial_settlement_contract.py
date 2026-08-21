from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


notifier = _load_module('notify_timo_materialization_partial', 'scripts/notify_timo_materialization.py')
retry_worker = _load_module('timo_incremental_retry_worker_partial', 'scripts/timo_incremental_retry_worker.py')
scope_feed = _load_module('timo_partial_settlement_feed', 'app/timo_partial_settlement.py')


def _status(*, state: str = 'partial') -> dict:
    scope = {
        'status': state,
        'data_date_bj': '2026-08-20',
        'run_id': 'run-0820',
        'guild_count': 3,
        'provisional': False,
        'revenue_contract': 'complete_guild_and_streamer',
        'snapshot_at': '2026-08-21T08:43:43+00:00',
    }
    return {
        'ok': state == 'success',
        'status': state,
        'run_id': 'run-0820',
        'date_from': '2026-08-20',
        'date_to': '2026-08-20',
        'guild_count': 3,
        'errors': [] if state == 'success' else ['Agency MX somente:revenue:timo_revenue_export_not_ready'],
        'scopes': [scope],
    }


def _manifest(storage_name: str, country: str, checksum_char: str, revision: int = 2) -> dict:
    return {
        'guild_name': storage_name,
        'country': country,
        'publication_ready': True,
        'row_count': 10,
        'total_income': '123.500000',
        'checksum': checksum_char * 64,
        'revision_version': revision,
        'last_success_sync_id': f'sync-{country.lower()}-{revision}',
        'last_success_time': '2026-08-21T08:40:00+00:00',
    }


def test_partial_day_publishes_complete_scopes_and_never_encodes_missing_as_zero():
    event = notifier.build_event(
        status=_status(),
        manifests=[
            _manifest('agency of BR somente', 'Brazil', 'b'),
            _manifest('TIMO001', 'Indonesia', 'i'.replace('i', 'a')),
        ],
        failures={
            'Agency MX somente': {
                'errorCode': 'timo_revenue_export_not_ready',
                'error': 'export_url_not_ready_attempt_3',
            },
        },
    )

    assert event['schemaVersion'] == 2
    assert event['eventType'] == 'timo.materialization.partial'
    assert event['dayStatus'] == 'PARTIAL'
    assert event['ready'] is False
    assert event['consumable'] is False
    assert event['scopeSucceeded'] == 2
    assert event['scopeFailed'] == 1
    assert event['failedScopes'] == ['MX']
    mx = next(scope for scope in event['scopes'] if scope['country'] == 'MX')
    assert mx['qualityStatus'] == 'SOURCE_MISSING'
    assert mx['consumable'] is False
    assert mx['rowCount'] is None
    assert mx['totalIncome'] is None
    assert mx['checksum'] is None
    assert mx['revision'] is None
    assert all(
        scope['qualityStatus'] == 'COMPLETE' and scope['consumable'] is True
        for scope in event['scopes']
        if scope['country'] in {'BR', 'ID'}
    )


def test_complete_recovery_upgrades_day_and_revision_is_part_of_scope_lineage():
    manifests = [
        _manifest('Agency MX somente', 'Mexico', 'c', revision=3),
        _manifest('TIMO001', 'Indonesia', 'a', revision=2),
        _manifest('agency of BR somente', 'Brazil', 'b', revision=2),
    ]
    event = notifier.build_event(status=_status(state='success'), manifests=manifests, failures={})

    assert event['eventType'] == 'timo.materialization.completed'
    assert event['dayStatus'] == 'COMPLETE'
    assert event['ready'] is True
    assert event['consumable'] is True
    assert event['scopeSucceeded'] == 3
    assert event['scopeFailed'] == 0
    assert event['failedScopes'] == []
    assert next(scope for scope in event['scopes'] if scope['country'] == 'MX')['revision'] == 3


def test_same_snapshot_has_stable_checksum_and_event_id():
    manifests = [
        _manifest('Agency MX somente', 'Mexico', 'c'),
        _manifest('TIMO001', 'Indonesia', 'a'),
        _manifest('agency of BR somente', 'Brazil', 'b'),
    ]
    first = notifier.build_event(status=_status(state='success'), manifests=manifests, failures={})
    second = notifier.build_event(status=_status(state='success'), manifests=manifests, failures={})
    assert first['checksum'] == second['checksum']
    assert first['eventId'] == second['eventId']
    assert first == second


def test_same_scope_content_keeps_event_id_across_source_retry_run_ids():
    manifests = [
        _manifest('Agency MX somente', 'Mexico', 'c'),
        _manifest('TIMO001', 'Indonesia', 'a'),
        _manifest('agency of BR somente', 'Brazil', 'b'),
    ]
    first_status = _status(state='success')
    second_status = _status(state='success')
    second_status['run_id'] = 'run-0820-retry'
    second_status['scopes'][0]['run_id'] = 'run-0820-retry'
    first = notifier.build_event(status=first_status, manifests=manifests, failures={})
    second = notifier.build_event(status=second_status, manifests=manifests, failures={})

    assert first['checksum'] == second['checksum']
    assert first['eventId'] == second['eventId']
    assert first['sourceGeneration'] != second['sourceGeneration']


def test_non_source_failure_is_anomaly_not_zero():
    event = notifier.build_event(
        status=_status(),
        manifests=[
            _manifest('TIMO001', 'Indonesia', 'a'),
            _manifest('agency of BR somente', 'Brazil', 'b'),
        ],
        failures={'Agency MX somente': {'errorCode': 'fact_checksum_mismatch', 'error': ''}},
    )
    mx = next(scope for scope in event['scopes'] if scope['country'] == 'MX')
    assert mx['qualityStatus'] == 'ANOMALY'
    assert mx['rowCount'] is None
    assert mx['totalIncome'] is None


def test_zero_ready_scope_fails_closed():
    with pytest.raises(ValueError, match='no_publication_ready_scope'):
        notifier.build_event(
            status=_status(),
            manifests=[],
            failures={'Agency MX somente': {'errorCode': 'source_not_ready', 'error': ''}},
        )


def test_retry_envelope_reuses_same_contract():
    nested = _status()['scopes'][0]
    normalized = notifier._materialization_identity({
        'ok': False,
        'status': 'partial',
        'due_dates': ['2026-08-20'],
        'results': [nested],
    })
    assert normalized['data_date_bj'] == '2026-08-20'
    assert normalized['run_id'] == 'run-0820'


def test_retry_worker_persists_atomic_status_for_natural_notifier(tmp_path):
    path = tmp_path / 'retry-status.json'
    result = {'ok': False, 'status': 'partial', 'due_dates': ['2026-08-20'], 'results': []}
    retry_worker._write_status(str(path), result)
    assert json.loads(path.read_text(encoding='utf-8')) == result
    assert not path.with_suffix('.json.tmp').exists()


def test_load_event_reads_failure_evidence_and_watermark_time_without_writing(tmp_path, monkeypatch):
    db_path = tmp_path / 'automation.db'
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE timo_sync_run_log(
                guild_name TEXT, status TEXT, error_code TEXT, error TEXT,
                start_time TEXT, stat_date_bj TEXT
            );
            CREATE TABLE timo_sync_watermark(
                guild_name TEXT, last_success_time TEXT, stat_date_bj TEXT
            );
            """
        )
        conn.execute(
            'INSERT INTO timo_sync_run_log VALUES (?, ?, ?, ?, ?, ?)',
            (
                'Agency MX somente',
                'failed',
                'timo_revenue_export_not_ready',
                'export_url_not_ready_attempt_3',
                '2026-08-21T08:43:00+00:00',
                '2026-08-20',
            ),
        )
        for guild in ('TIMO001', 'agency of BR somente'):
            conn.execute(
                'INSERT INTO timo_sync_watermark VALUES (?, ?, ?)',
                (guild, '2026-08-21T08:40:00+00:00', '2026-08-20'),
            )
    monkeypatch.setattr(notifier, 'timo_external_feed_status', lambda *_args, **_kwargs: {
        'scope_manifests': [
            _manifest('TIMO001', 'Indonesia', 'a'),
            _manifest('agency of BR somente', 'Brazil', 'b'),
        ],
    })
    marker = tmp_path / 'started'
    marker.write_text('', encoding='utf-8')
    marker_ns = marker.stat().st_mtime_ns
    status_path = tmp_path / 'status.json'
    status_path.write_text(json.dumps(_status()), encoding='utf-8')
    os.utime(status_path, ns=(marker_ns + 1_000_000, marker_ns + 1_000_000))

    event = notifier.load_event(status_path, db_path, marker)
    assert event['dayStatus'] == 'PARTIAL'
    assert event['failedScopes'] == ['MX']
    assert next(scope for scope in event['scopes'] if scope['country'] == 'MX')['failureReason'] == (
        'timo_revenue_export_not_ready'
    )


def test_external_feed_v2_returns_all_expected_scopes_and_null_missing_facts(tmp_path):
    db_path = tmp_path / 'automation.db'
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE timo_sync_run_log(
                guild_name TEXT, status TEXT, error_code TEXT, error TEXT,
                start_time TEXT, stat_date_bj TEXT
            );
            CREATE TABLE timo_sync_watermark(
                guild_name TEXT, last_success_time TEXT, stat_date_bj TEXT
            );
            """
        )
        conn.execute(
            'INSERT INTO timo_sync_run_log VALUES (?, ?, ?, ?, ?, ?)',
            (
                'Agency MX somente', 'failed', 'timo_revenue_export_not_ready',
                'export_url_not_ready_attempt_3', '2026-08-21T08:43:00+00:00', '2026-08-20',
            ),
        )
        for guild in ('TIMO001', 'agency of BR somente'):
            conn.execute(
                'INSERT INTO timo_sync_watermark VALUES (?, ?, ?)',
                (guild, '2026-08-21T08:40:00+00:00', '2026-08-20'),
            )
        enriched = scope_feed.enrich_timo_scope_feed_status(
            conn,
            {'scope_manifests': [
                _manifest('TIMO001', 'Indonesia', 'a'),
                _manifest('agency of BR somente', 'Brazil', 'b'),
            ]},
            business_date='2026-08-20',
        )

    assert enriched['integrity_contract_version'] == 'timo_scope_manifest_v2'
    assert enriched['day_status'] == 'PARTIAL'
    assert enriched['publication_ready'] is False
    assert enriched['consumable'] is False
    assert enriched['expected_scope_count'] == 3
    assert enriched['succeeded_scope_count'] == 2
    assert enriched['failed_scope_count'] == 1
    assert enriched['failed_scopes'] == ['MX']
    assert len(enriched['scope_manifests']) == 3
    mx = next(item for item in enriched['scope_manifests'] if item['country_code'] == 'MX')
    assert mx['quality_status'] == 'SOURCE_MISSING'
    assert mx['publication_ready'] is False
    assert mx['consumable'] is False
    for field in ('row_count', 'total_income', 'checksum', 'revision_version', 'last_success_sync_id'):
        assert mx[field] is None


def test_external_feed_country_filter_is_independently_consumable(tmp_path):
    db_path = tmp_path / 'automation.db'
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE timo_sync_run_log(
                guild_name TEXT, status TEXT, error_code TEXT, error TEXT,
                start_time TEXT, stat_date_bj TEXT
            );
            CREATE TABLE timo_sync_watermark(
                guild_name TEXT, last_success_time TEXT, stat_date_bj TEXT
            );
            """
        )
        conn.execute(
            'INSERT INTO timo_sync_watermark VALUES (?, ?, ?)',
            ('agency of BR somente', '2026-08-21T08:40:00+00:00', '2026-08-20'),
        )
        enriched = scope_feed.enrich_timo_scope_feed_status(
            conn,
            {'scope_manifests': [_manifest('agency of BR somente', 'Brazil', 'b')]},
            business_date='2026-08-20',
            country='Brazil',
        )

    assert enriched['day_status'] == 'COMPLETE'
    assert enriched['publication_ready'] is True
    assert enriched['consumable'] is True
    assert enriched['expected_scope_count'] == 1
    assert enriched['succeeded_scope_count'] == 1
    assert enriched['failed_scope_count'] == 0


def test_external_feed_rows_only_expose_consumable_scope_guilds():
    feed_status = {
        'scope_manifests': [
            {'guild_storage_name': 'TIMO001', 'consumable': True},
            {'guild_storage_name': 'agency of BR somente', 'consumable': False},
            {'guild_storage_name': 'Agency MX somente', 'consumable': False},
        ],
    }

    assert scope_feed.consumable_timo_scope_guild_names(feed_status) == ['TIMO001']
    assert scope_feed.consumable_timo_scope_guild_names({'scope_manifests': []}) == []
