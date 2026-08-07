from __future__ import annotations

import json
import importlib.util
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

from app import ad_dashboard_repository, main_shared
from app.schema_migrations import apply_schema_migration_registry
from app.sqlite_write_queue import apply_sqlite_write_job
from app.tugao_funnel_api import (
    DEFAULT_GROUP_BY,
    TugaoFunnelApiError,
    tugao_funnel_api_row_to_fact,
    validate_group_by,
)


def _source_row(**overrides):
    row = {
        'date': '2026-08-07',
        'country': 'Mexico',
        'media_source': 'Meta',
        'campaign_id': 'campaign-1',
        'campaign_name': 'MX campaign',
        'adset_id': 'adset-1',
        'adset_name': 'MX adset',
        'ad_id': 'ad-1',
        'ad_name': 'MX ad',
        'external_app': 'TUGAO',
        'new_registered_users': 100,
        'high_value_l1_female_18_40_users': 80,
        'auto_apply_message_users': 60,
        'im_user_message_ge_3_users': 40,
        'guild_join_success_users': 58,
        'guild_join_success_no_wa_users': 8,
        'guild_join_total_users': 66,
    }
    row.update(overrides)
    return row


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    return conn


def _load_backfill_module():
    script_path = Path(__file__).resolve().parents[1] / 'scripts' / 'backfill_ad_dashboard_fact_rows.py'
    spec = importlib.util.spec_from_file_location('gle_g002b_backfill_contract', script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_producer_keeps_success_separate_from_legacy_total_and_exact_identity():
    fact = tugao_funnel_api_row_to_fact(_source_row())

    assert fact['guild_joins'] == 66
    assert fact['tugao_join_success_users'] == 58
    assert fact['tugao_join_success_no_wa_users'] == 8
    assert fact['campaign_id'] == 'campaign-1'
    assert fact['adset_id'] == 'adset-1'
    assert fact['ad_id'] == 'ad-1'
    assert fact['qualified_join_metric_observed'] is True
    assert fact['qualified_join_exact_attribution'] is True
    assert fact['qualified_join_source_field'] == 'guild_join_success_users'
    assert fact['source_metric_contract'] == 'tugao_funnel_daily_metrics_api_v1'


def test_explicit_zero_never_falls_back_to_total_for_qualified_join():
    fact = tugao_funnel_api_row_to_fact(_source_row(
        guild_join_success_users=0,
        guild_join_success_no_wa_users=0,
        guild_join_total_users=9,
    ))

    assert fact['guild_joins'] == 9
    assert fact['tugao_join_success_users'] == 0
    assert fact['qualified_join_metric_observed'] is True


def test_qualified_count_above_float_precision_fails_closed():
    value = 9_007_199_254_740_993
    with pytest.raises(TugaoFunnelApiError, match='tugao_funnel_invalid_count'):
        tugao_funnel_api_row_to_fact(_source_row(guild_join_success_users=str(value)))


@pytest.mark.parametrize('value', [None, '', 'N/A', -1, 1.5, float('nan'), float('inf'), True])
def test_qualified_count_invalid_or_missing_is_not_coerced_to_zero(value):
    with pytest.raises(TugaoFunnelApiError, match='tugao_funnel_invalid_count'):
        tugao_funnel_api_row_to_fact(_source_row(guild_join_success_users=value))


def test_qualified_group_by_is_exact_and_ordered():
    assert validate_group_by(DEFAULT_GROUP_BY) == list(DEFAULT_GROUP_BY)
    with pytest.raises(ValueError, match='qualified_join_group_by_must_be_exact'):
        validate_group_by(DEFAULT_GROUP_BY[:-1])
    with pytest.raises(ValueError, match='qualified_join_group_by_must_be_exact'):
        validate_group_by(tuple(reversed(DEFAULT_GROUP_BY)))


def test_repository_and_runtime_contract_preserve_metric_and_provenance():
    fact = tugao_funnel_api_row_to_fact(_source_row())
    repository_rows = ad_dashboard_repository._ad_materialize_fact_rows([fact])
    runtime_rows = main_shared._ad_materialize_fact_rows([fact])

    for rows in (repository_rows, runtime_rows):
        assert len(rows) == 1
        row = rows[0]
        assert row['tugao_join_success_users'] == 58
        assert row['tugao_join_success_no_wa_users'] == 8
        assert row['qualified_join_metric_observed'] is True
        assert row['qualified_join_exact_attribution'] is True
        assert row['qualified_join_attribution_status'] == 'exact'
        assert row['qualified_join_source_field'] == 'guild_join_success_users'
        assert row['source_metric_contract'] == 'tugao_funnel_daily_metrics_api_v1'

    assert repository_rows == runtime_rows
    assert ad_dashboard_repository.AD_DASHBOARD_FACT_COLUMNS == main_shared.AD_DASHBOARD_FACT_COLUMNS


def test_same_names_with_different_exact_ids_remain_separate_gate_eligible_rows():
    first = tugao_funnel_api_row_to_fact(_source_row())
    second = tugao_funnel_api_row_to_fact(_source_row(
        campaign_id='campaign-2',
        adset_id='adset-2',
        ad_id='ad-2',
        guild_join_success_users=2,
        guild_join_success_no_wa_users=1,
    ))

    rows = ad_dashboard_repository._ad_materialize_fact_rows([first, second])

    assert len(rows) == 2
    assert sum(row['tugao_join_success_users'] for row in rows) == 60
    assert {row['campaign_id'] for row in rows} == {'campaign-1', 'campaign-2'}
    assert all(row['qualified_join_metric_observed'] is True for row in rows)
    assert all(row['qualified_join_exact_attribution'] is True for row in rows)
    assert all(row['qualified_join_attribution_status'] == 'exact' for row in rows)


@pytest.mark.parametrize('same_identity', [False, True])
def test_observed_tugao_country_is_never_inferred_from_name_peers(same_identity):
    unknown = tugao_funnel_api_row_to_fact(_source_row(country=''))
    mexico_overrides = {'country': 'Mexico', 'guild_join_success_users': 2}
    if not same_identity:
        mexico_overrides.update(campaign_id='campaign-2', adset_id='adset-2', ad_id='ad-2')
    mexico = tugao_funnel_api_row_to_fact(_source_row(**mexico_overrides))

    for materialize in (
        ad_dashboard_repository._ad_materialize_fact_rows,
        main_shared._ad_materialize_fact_rows,
    ):
        rows = materialize([unknown, mexico])
        countries = [row['country'] for row in rows]
        assert 'Unknown' in countries
        assert 'Mexico' in countries
        assert len(rows) == 2


def test_duplicate_exact_tuple_and_mixed_observation_fail_closed():
    observed = tugao_funnel_api_row_to_fact(_source_row())
    duplicate = dict(observed)
    duplicate['tugao_join_success_users'] = 1
    duplicate_rows = ad_dashboard_repository._ad_materialize_fact_rows([observed, duplicate])
    assert duplicate_rows[0]['qualified_join_metric_observed'] is True
    assert duplicate_rows[0]['qualified_join_exact_attribution'] is False
    assert duplicate_rows[0]['qualified_join_attribution_status'] == 'duplicate_exact_tuple'

    legacy = dict(observed)
    legacy.pop('qualified_join_metric_observed')
    mixed_rows = ad_dashboard_repository._ad_materialize_fact_rows([observed, legacy])
    assert len(mixed_rows) == 2
    observed_rows = [row for row in mixed_rows if row.get('qualified_join_metric_observed') is True]
    legacy_rows = [row for row in mixed_rows if 'qualified_join_metric_observed' not in row]
    assert len(observed_rows) == 1
    assert observed_rows[0]['qualified_join_exact_attribution'] is True
    assert len(legacy_rows) == 1


def test_round_trip_distinguishes_observed_zero_from_migrated_unknown():
    conn = _connect()
    try:
        main_shared.ensure_ad_dashboard_fact_tables(conn)
        observed = tugao_funnel_api_row_to_fact(_source_row(
            guild_join_success_users=0,
            guild_join_success_no_wa_users=0,
        ))
        main_shared.upsert_ad_dashboard_fact_rows(conn, [observed])
        stored = main_shared.read_ad_dashboard_fact_rows(
            conn,
            start_date=date(2026, 8, 7),
            end_date=date(2026, 8, 7),
        )
        assert stored[0]['tugao_join_success_users'] == 0
        assert stored[0]['qualified_join_metric_observed'] is True
        assert stored[0]['qualified_join_exact_attribution'] is True

        conn.execute(
            "INSERT INTO ad_dashboard_fact_rows "
            "(row_id,date,data_source,platform,payload_json,updated_at) "
            "VALUES('legacy','2026-08-06','TugaoFunnel','Meta','{}','2026-08-07T00:00:00Z')"
        )
        legacy = conn.execute(
            "SELECT tugao_join_success_users,payload_json FROM ad_dashboard_fact_rows WHERE row_id='legacy'"
        ).fetchone()
        assert legacy['tugao_join_success_users'] == 0
        assert 'qualified_join_metric_observed' not in json.loads(legacy['payload_json'])
    finally:
        conn.close()


def test_schema_registry_and_writer_schema_contract_include_qualified_columns(tmp_path):
    db_path = tmp_path / 'facts.db'
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE ad_dashboard_fact_rows "
            "(row_id TEXT PRIMARY KEY,date TEXT NOT NULL,data_source TEXT NOT NULL,"
            "platform TEXT NOT NULL,payload_json TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL)"
        )
        apply_schema_migration_registry(conn)
        columns = {row[1] for row in conn.execute('PRAGMA table_info(ad_dashboard_fact_rows)')}
        assert {'tugao_join_success_users', 'tugao_join_success_no_wa_users'} <= columns
    finally:
        conn.close()

    writer_db_path = tmp_path / 'writer-schema.db'
    result = apply_sqlite_write_job(
        db_path=str(writer_db_path),
        job={'type': 'ad_dashboard_schema_ensure'},
    )
    assert result['lineage_columns'] == [
        'account_id', 'account_name', 'ad_id', 'adset_id', 'campaign_id',
    ]
    assert result['qualified_join_columns'] == [
        'tugao_join_success_no_wa_users', 'tugao_join_success_users',
    ]


def test_dedicated_writer_propagates_tugao_completeness_gate(tmp_path):
    db_path = tmp_path / 'writer.db'
    fact = tugao_funnel_api_row_to_fact(_source_row())
    result = apply_sqlite_write_job(
        db_path=str(db_path),
        job={
            'type': 'ad_dashboard_fact_replace',
            'rows': [fact],
            'start_date': '2026-08-07',
            'end_date': '2026-08-07',
            'appsflyer_required': False,
            'tugao_funnel_required': True,
            'source': 'all',
        },
    )
    assert result['sync_status'] == 'ok'
    assert result['input_observed_qualified_join_rows'] == 1
    assert result['qualified_join_readback'] == {
        'stored_rows': 1,
        'success_users': 58,
        'success_no_wa_users': 8,
        'exact_attribution_rows': 1,
    }

    fact.pop('qualified_join_metric_observed')
    with pytest.raises(ValueError, match='qualified_join_not_observed'):
        apply_sqlite_write_job(
            db_path=str(db_path),
            job={
                'type': 'ad_dashboard_fact_replace',
                'rows': [fact],
                'start_date': '2026-08-07',
                'end_date': '2026-08-07',
                'appsflyer_required': False,
                'tugao_funnel_required': True,
                'source': 'all',
            },
        )


def test_writer_uses_exact_grain_and_replaces_renamed_tuple_without_duplicates(tmp_path):
    db_path = tmp_path / 'rename.db'
    first = tugao_funnel_api_row_to_fact(_source_row())
    second = tugao_funnel_api_row_to_fact(_source_row(
        campaign_name='Renamed campaign',
        adset_name='Renamed adset',
        ad_name='Renamed ad',
        guild_join_success_users=60,
    ))
    for fact in (first, second):
        apply_sqlite_write_job(
            db_path=str(db_path),
            job={
                'type': 'ad_dashboard_fact_replace',
                'rows': [fact],
                'start_date': '2026-08-07',
                'end_date': '2026-08-07',
                'appsflyer_required': False,
                'tugao_funnel_required': True,
                'source': 'tugao_funnel',
            },
        )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT campaign,ad_group,ad,tugao_join_success_users,payload_json "
            "FROM ad_dashboard_fact_rows WHERE data_source='TugaoFunnel'"
        ).fetchall()
        assert len(rows) == 1
        assert dict(rows[0])['campaign'] == 'Renamed campaign'
        assert dict(rows[0])['tugao_join_success_users'] == 60
        assert json.loads(dict(rows[0])['payload_json'])['qualified_join_exact_attribution'] is True
    finally:
        conn.close()


def test_writer_rejects_paid_qualified_row_without_exact_identity(tmp_path):
    fact = tugao_funnel_api_row_to_fact(_source_row(ad_id=''))
    with pytest.raises(ValueError, match='qualified_join_exact_identity_missing'):
        apply_sqlite_write_job(
            db_path=str(tmp_path / 'missing-id.db'),
            job={
                'type': 'ad_dashboard_fact_replace',
                'rows': [fact],
                'start_date': '2026-08-07',
                'end_date': '2026-08-07',
                'appsflyer_required': False,
                'tugao_funnel_required': True,
            },
        )


def test_writer_rejects_materialized_duplicate_before_sync_state(tmp_path):
    fact = tugao_funnel_api_row_to_fact(_source_row())
    duplicate = dict(fact)
    duplicate['tugao_join_success_users'] = 1
    db_path = tmp_path / 'duplicate.db'
    with pytest.raises(ValueError, match='qualified_join_materialized_exact_state_invalid'):
        apply_sqlite_write_job(
            db_path=str(db_path),
            job={
                'type': 'ad_dashboard_fact_replace',
                'rows': [fact, duplicate],
                'start_date': '2026-08-07',
                'end_date': '2026-08-07',
                'appsflyer_required': False,
                'tugao_funnel_required': True,
            },
        )
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('SELECT COUNT(*) FROM ad_dashboard_fact_rows').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM ad_dashboard_sync_state').fetchone()[0] == 0
    finally:
        conn.close()


def test_writer_accepts_observed_organic_without_paid_identity_but_counts_only_paid_exact(tmp_path):
    paid = tugao_funnel_api_row_to_fact(_source_row())
    organic = tugao_funnel_api_row_to_fact(_source_row(
        media_source='organic',
        campaign_id='',
        campaign_name='',
        adset_id='',
        adset_name='',
        ad_id='',
        ad_name='',
        guild_join_success_users=3,
        guild_join_success_no_wa_users=0,
        guild_join_total_users=3,
    ))
    day = date(2026, 8, 7)
    for completeness in (
        ad_dashboard_repository.ad_dashboard_fact_rows_completeness,
        main_shared.ad_dashboard_fact_rows_completeness,
    ):
        result = completeness(
            [paid, organic],
            start_date=day,
            end_date=day,
            appsflyer_required=False,
            tugao_funnel_required=True,
        )
        assert result['complete'] is True

    result = apply_sqlite_write_job(
        db_path=str(tmp_path / 'mixed-paid-organic.db'),
        job={
            'type': 'ad_dashboard_fact_replace',
            'rows': [paid, organic],
            'start_date': '2026-08-07',
            'end_date': '2026-08-07',
            'appsflyer_required': False,
            'tugao_funnel_required': True,
        },
    )
    assert result['sync_status'] == 'ok'
    assert result['qualified_join_readback']['stored_rows'] == 2
    assert result['qualified_join_readback']['exact_attribution_rows'] == 1
    assert result['qualified_join_readback']['success_users'] == 61


def test_pre_metric_bounded_preimage_restore_remains_compatible(tmp_path):
    db_path = tmp_path / 'restore.db'
    apply_sqlite_write_job(db_path=str(db_path), job={'type': 'ad_dashboard_schema_ensure'})
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        main_shared.upsert_ad_dashboard_fact_rows(conn, [tugao_funnel_api_row_to_fact(_source_row())])
        conn.commit()
        preimage = dict(conn.execute('SELECT * FROM ad_dashboard_fact_rows').fetchone())
    finally:
        conn.close()
    preimage.pop('tugao_join_success_users')
    preimage.pop('tugao_join_success_no_wa_users')
    legacy_payload = json.loads(preimage['payload_json'])
    for key in (
        'qualified_join_metric_observed',
        'qualified_join_exact_attribution',
        'qualified_join_attribution_status',
        'qualified_join_source_field',
        'source_metric_contract',
    ):
        legacy_payload.pop(key, None)
    preimage['payload_json'] = json.dumps(legacy_payload, sort_keys=True)

    result = apply_sqlite_write_job(
        db_path=str(db_path),
        job={
            'type': 'ad_dashboard_fact_restore_window',
            'rows': [preimage],
            'start_date': '2026-08-07',
            'end_date': '2026-08-07',
            'expected_rows': 1,
        },
    )
    assert result['restored_rows'] == 1
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        restored = dict(conn.execute('SELECT * FROM ad_dashboard_fact_rows').fetchone())
        assert restored['tugao_join_success_users'] == 0
        assert restored['tugao_join_success_no_wa_users'] == 0
        assert 'qualified_join_metric_observed' not in json.loads(restored['payload_json'])
    finally:
        conn.close()


def test_restore_rejects_missing_metric_columns_when_observation_marker_remains(tmp_path):
    db_path = tmp_path / 'restore-marker.db'
    apply_sqlite_write_job(db_path=str(db_path), job={'type': 'ad_dashboard_schema_ensure'})
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        main_shared.upsert_ad_dashboard_fact_rows(conn, [tugao_funnel_api_row_to_fact(_source_row())])
        conn.commit()
        preimage = dict(conn.execute('SELECT * FROM ad_dashboard_fact_rows').fetchone())
    finally:
        conn.close()
    preimage.pop('tugao_join_success_users')
    preimage.pop('tugao_join_success_no_wa_users')

    with pytest.raises(Exception, match='ad_dashboard_fact_restore_window_schema_mismatch'):
        apply_sqlite_write_job(
            db_path=str(db_path),
            job={
                'type': 'ad_dashboard_fact_restore_window',
                'rows': [preimage],
                'start_date': '2026-08-07',
                'end_date': '2026-08-07',
                'expected_rows': 1,
            },
        )


def test_tugao_completeness_gate_requires_observed_source_metric():
    day = date(2026, 8, 7)
    fact = tugao_funnel_api_row_to_fact(_source_row())
    complete = ad_dashboard_repository.ad_dashboard_fact_rows_completeness(
        [fact], start_date=day, end_date=day, appsflyer_required=False, tugao_funnel_required=True,
    )
    assert complete['complete'] is True

    missing = dict(fact)
    missing.pop('qualified_join_metric_observed')
    incomplete = ad_dashboard_repository.ad_dashboard_fact_rows_completeness(
        [missing], start_date=day, end_date=day, appsflyer_required=False, tugao_funnel_required=True,
    )
    assert incomplete['complete'] is False
    assert incomplete['invalid_qualified_join'][0]['reason'] == 'qualified_join_not_observed'

    for forged_marker in ('false', '0', 1, {}):
        forged = dict(fact)
        forged['qualified_join_metric_observed'] = forged_marker
        forged_result = ad_dashboard_repository.ad_dashboard_fact_rows_completeness(
            [forged], start_date=day, end_date=day, appsflyer_required=False, tugao_funnel_required=True,
        )
        assert forged_result['complete'] is False
        assert forged_result['invalid_qualified_join'][0]['reason'] == 'qualified_join_observation_marker_invalid'


def test_legacy_metrics_cannot_substitute_for_qualified_success():
    row = tugao_funnel_api_row_to_fact(_source_row(
        guild_join_success_users=0,
        guild_join_total_users=99,
    ))
    row.update({'bind_success_users': 77, 'crm_succeed_users': 88, 'tugao_real_bind_count': 55})
    materialized = ad_dashboard_repository._ad_materialize_fact_rows([row])[0]

    assert materialized['guild_joins'] == 99
    assert materialized['tugao_join_success_users'] == 0


def test_backfill_handoff_failure_happens_before_any_store(monkeypatch, tmp_path):
    module = _load_backfill_module()
    fact = tugao_funnel_api_row_to_fact(_source_row())
    events = []
    monkeypatch.setattr(sys, 'argv', [
        'backfill_ad_dashboard_fact_rows.py',
        '--db-path', str(tmp_path / 'facts.db'),
        '--start-date', '2026-08-07',
        '--end-date', '2026-08-07',
        '--retry-missing-appsflyer', '0',
        '--retry-delay-seconds', '0',
    ])
    monkeypatch.setattr(module, '_load_env_file', lambda _path: None)
    monkeypatch.setattr(module, 'assert_managed_batch_runtime', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, '_build_fact_rows_payload', lambda **_kwargs: {
        'snapshot': {}, 'fact_rows': [fact], 'tugao_api_result': {'status': 'ok'},
        'tugao_api_rows': [fact], 'marketing_result': {}, 'marketing_rows': [], 'tugao_daily_rows': [],
    })
    monkeypatch.setattr(module, '_load_tugao_funnel_api_rows', lambda **_kwargs: {
        'status': 'ok', 'rows': [fact], 'pages': 1, 'raw_row_count': 1,
    })
    monkeypatch.setattr(module, '_fact_completeness', lambda *_args, **_kwargs: {
        'complete': True, 'missing_appsflyer': [], 'error_message': '',
    })

    def fail_handoff(_unit):
        events.append('handoff')
        raise RuntimeError('handoff_failed')

    monkeypatch.setattr(module, 'handoff_network_phase', fail_handoff)
    monkeypatch.setattr(module, '_store_fact_rows', lambda *_args, **_kwargs: events.append('store'))

    with pytest.raises(RuntimeError, match='handoff_failed'):
        module.main()
    assert events == ['handoff']


def test_backfill_watermark_missing_returns_75_after_two_stores(monkeypatch, tmp_path):
    module = _load_backfill_module()
    fact = tugao_funnel_api_row_to_fact(_source_row())
    events = []
    monkeypatch.setattr(sys, 'argv', [
        'backfill_ad_dashboard_fact_rows.py',
        '--db-path', str(tmp_path / 'facts.db'),
        '--start-date', '2026-08-07',
        '--end-date', '2026-08-07',
        '--retry-missing-appsflyer', '0',
        '--retry-delay-seconds', '0',
    ])
    monkeypatch.setattr(module, '_load_env_file', lambda _path: None)
    monkeypatch.setattr(module, 'assert_managed_batch_runtime', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, '_build_fact_rows_payload', lambda **_kwargs: {
        'snapshot': {}, 'fact_rows': [fact], 'tugao_api_result': {'status': 'ok'},
        'tugao_api_rows': [fact], 'marketing_result': {}, 'marketing_rows': [], 'tugao_daily_rows': [],
    })
    monkeypatch.setattr(module, '_load_tugao_funnel_api_rows', lambda **_kwargs: {
        'status': 'ok', 'rows': [fact], 'pages': 1, 'raw_row_count': 1,
    })
    monkeypatch.setattr(module, '_fact_completeness', lambda *_args, **_kwargs: {
        'complete': True, 'missing_appsflyer': [], 'error_message': '',
    })
    monkeypatch.setattr(module, 'handoff_network_phase', lambda _unit: events.append('handoff'))

    def stored(*_args, **_kwargs):
        events.append('store')
        return {'stored_rows': 1, 'qualified_join_readback': {'stored_rows': 1}}

    monkeypatch.setattr(module, '_store_fact_rows', stored)
    monkeypatch.setattr(module, '_completion_watermark', lambda *_args, **_kwargs: {
        'ok': False, 'error_message': 'missing',
    })
    monkeypatch.setattr(module, '_persist_daily_recommendation_report', lambda *_args, **_kwargs: events.append('report'))

    assert module.main() == 75
    assert events == ['handoff', 'store', 'store']


def test_backfill_writer_and_direct_paths_return_qualified_readback(monkeypatch, tmp_path):
    module = _load_backfill_module()
    fact = tugao_funnel_api_row_to_fact(_source_row())
    writer_job = {}
    monkeypatch.setattr(module, 'db_writer_enabled', lambda: True)

    def submit(job, timeout):
        writer_job.update(job)
        return {
            'stored_rows': 1,
            'date_start': '2026-08-07',
            'date_end': '2026-08-07',
            'sync_status': 'ok',
            'qualified_join_readback': {'stored_rows': 1, 'success_users': 58},
        }

    monkeypatch.setattr(module, 'submit_sqlite_write_job', submit)
    writer_result = module._store_fact_rows(tmp_path / 'writer.db', [fact], appsflyer_required=False)
    assert writer_job['tugao_funnel_required'] is True
    assert writer_result['qualified_join_readback']['success_users'] == 58

    monkeypatch.setattr(module, 'db_writer_enabled', lambda: False)
    direct_result = module._store_fact_rows(tmp_path / 'direct.db', [fact], appsflyer_required=False)
    assert direct_result['qualified_join_readback'] == {
        'stored_rows': 1,
        'success_users': 58,
        'success_no_wa_users': 8,
        'exact_attribution_rows': 1,
    }


def test_backfill_production_dependency_missing_fails_closed(monkeypatch):
    module = _load_backfill_module()
    monkeypatch.setattr(module, 'REPO_ROOT', Path('/opt/mcn-ai-automation'))
    with pytest.raises(RuntimeError, match='mcn_phase_resource_handoff_missing_in_production'):
        module.handoff_network_phase('mcn-ad-dashboard-daily-backfill.service')
