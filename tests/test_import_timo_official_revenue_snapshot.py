from __future__ import annotations

import csv
import hashlib
import importlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import sqlite3
import types

import pytest


CANDIDATE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(CANDIDATE_ROOT))
SPEC = importlib.util.spec_from_file_location(
    'import_timo_official_revenue_snapshot_candidate',
    CANDIDATE_ROOT / 'scripts/import_timo_official_revenue_snapshot.py',
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

if 'app.timo_partial_settlement' not in sys.modules:
    partial_settlement = types.ModuleType('app.timo_partial_settlement')
    partial_settlement.enrich_timo_scope_feed_status = lambda conn, status, **kwargs: status
    sys.modules['app.timo_partial_settlement'] = partial_settlement

NOTIFIER_SPEC = importlib.util.spec_from_file_location(
    'notify_timo_materialization_candidate',
    CANDIDATE_ROOT / 'scripts/notify_timo_materialization.py',
)
NOTIFIER = importlib.util.module_from_spec(NOTIFIER_SPEC)
assert NOTIFIER_SPEC.loader is not None
NOTIFIER_SPEC.loader.exec_module(NOTIFIER)


HEADERS = [
    '主播昵称', '用户id', '公会id', '公会群名称', '主播注册时间', '主播身份',
    '1v1总收益', '本周1v1主播达标收益', '匹配通话收益', '私信消息收益',
    '私信礼物收益', '1v1通话收益', '在线时长(单位：h）', '通话数', '优质主播',
    '优质主播特定场景收益',
]


def _csv_bytes(rows):
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=HEADERS)
    writer.writeheader()
    writer.writerows(rows)
    return ('\ufeff' + output.getvalue()).encode('utf-8')


def _row(*, user_id, total='0', registered='2026-08-23 10:00:00', guild_id='22000408'):
    row = {header: '' for header in HEADERS}
    row.update({
        '主播昵称': f'host-{user_id}',
        '用户id': user_id,
        '公会id': guild_id,
        '公会群名称': 'Royal Latam',
        '主播注册时间': registered,
        '主播身份': '主播',
        '1v1总收益': total,
        '私信消息收益': total,
        '在线时长(单位：h）': '1.5',
        '通话数': '2',
        '优质主播': '否',
    })
    return row


def _parse(content):
    return MODULE.parse_official_snapshot(
        content,
        source_name='1v1主播报表(20260823-20260823).xlsx',
        business_date='2026-08-23',
        guild_id='22000408',
        country='Mexico',
    )


def test_csv_disguised_as_xlsx_is_normalized_and_zero_rows_are_not_consumed():
    result = _parse(_csv_bytes([
        _row(user_id='1001', total='10.25'),
        _row(user_id='1002', total='0', registered='2026-08-25 10:00:00'),
    ]))
    assert result['source_row_count'] == 2
    assert result['source_unique_id_count'] == 2
    assert result['effective_row_count'] == 1
    assert str(result['total_income']) == '10.25'
    assert result['rows'][0]['timo_id'] == '1001'


def test_duplicate_streamer_or_wrong_guild_fails_closed():
    with pytest.raises(ValueError, match='duplicate_streamer_id'):
        _parse(_csv_bytes([_row(user_id='1001', total='1'), _row(user_id='1001', total='2')]))
    with pytest.raises(ValueError, match='guild_mismatch'):
        _parse(_csv_bytes([_row(user_id='1001', total='1', guild_id='22000448')]))


def test_income_after_mexico_business_period_fails_closed():
    with pytest.raises(ValueError, match='post_period_income'):
        _parse(_csv_bytes([
            _row(user_id='1001', total='1', registered='2026-08-24 14:00:00'),
        ]))


def test_true_xlsx_is_not_silently_reinterpreted():
    with pytest.raises(ValueError, match='true_xlsx_not_supported'):
        _parse(b'PK\x03\x04fake')


def test_manual_official_verification_is_exact_scope_allowlisted():
    evidence = MODULE.build_official_verification_evidence(
        mode='manual_official_verified',
        business_date='2026-08-19',
        guild_id='22000408',
        source_sha256='2f76c9ac028d97a539d81f9b9dff774a0d1fd0401c58a37da1fef90de5e92fae',
        source_row_count=5271,
        effective_row_count=407,
        total_income='6385485.35',
    )
    assert evidence['observation_policy'] == 'explicit_user_verified_no_reobserve'
    with pytest.raises(ValueError, match='contract_mismatch'):
        MODULE.build_official_verification_evidence(
            mode='manual_official_verified',
            business_date='2026-08-19',
            guild_id='22000408',
            source_sha256='0' * 64,
            source_row_count=5271,
            effective_row_count=407,
            total_income='6385485.35',
        )
    with pytest.raises(ValueError, match='scope_not_authorized'):
        MODULE.build_official_verification_evidence(
            mode='manual_official_verified',
            business_date='2026-08-18',
            guild_id='22000408',
            source_sha256='0' * 64,
            source_row_count=1,
            effective_row_count=1,
            total_income='1',
        )


def test_importer_requires_managed_batch_runtime_and_sqlite_lock():
    source = (CANDIDATE_ROOT / 'scripts/import_timo_official_revenue_snapshot.py').read_text(
        encoding='utf-8'
    )
    assert "assert_managed_batch_runtime('timo_official_revenue_import'" in source
    assert "acquire_sqlite_job_lock('sqlite-etl'" in source
    assert "parser.add_argument('--job-spec', required=True)" in source


def test_exact_same_official_lineage_can_be_reobserved_without_replacing_facts(tmp_path, monkeypatch):
    source = tmp_path / '1v1-host-report-20260823-20260823-mx.csv'
    # SQLite REAL aggregation is intentionally non-exact here (0.1 + 0.2).
    # Re-observation must compare the frozen six-decimal money contract, not
    # the binary floating-point tail returned by SUM().
    content = _csv_bytes([
        _row(user_id='1001', total='0.1'),
        _row(user_id='1002', total='0.2'),
    ])
    source.write_bytes(content)
    db = tmp_path / 'automation.db'
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE timo_external_revenue_daily(
          guild_executor_key TEXT,stat_date_bj TEXT,total_income REAL,last_sync_id TEXT,revision_version INTEGER
        );
        CREATE TABLE timo_sync_watermark(
          guild_executor_key TEXT,stat_date_bj TEXT,checksum TEXT,last_success_sync_id TEXT,
          row_count INTEGER,total_income REAL,data_status TEXT,revision_version INTEGER
        );
        CREATE TABLE timo_sync_run_log(sync_id TEXT,status TEXT,gate_evidence_json TEXT);
        """
    )
    base = 'timo_manual_official_20260823_22000408_' + hashlib.sha256(content).hexdigest()[:12]
    conn.executemany(
        'INSERT INTO timo_external_revenue_daily VALUES(?,?,?,?,?)',
        [
            ('timo:cms_guild_sid:lvmy210446316420ie3d', '2026-08-23', 0.1, base, 1),
            ('timo:cms_guild_sid:lvmy210446316420ie3d', '2026-08-23', 0.2, base, 1),
        ],
    )
    conn.execute(
        'INSERT INTO timo_sync_watermark VALUES(?,?,?,?,?,?,?,?)',
        ('timo:cms_guild_sid:lvmy210446316420ie3d', '2026-08-23', 'a' * 64, base, 2, 0.3, 'complete', 1),
    )
    conn.execute(
        'INSERT INTO timo_sync_run_log VALUES(?,?,?)',
        (base, 'success', '{"source_provenance":{"raw_response_sha256":"' + hashlib.sha256(content).hexdigest() + '"}}'),
    )
    conn.commit()
    conn.close()
    observed = {}
    monkeypatch.setattr(MODULE, 'archive_raw_bytes', lambda *args, **kwargs: {'raw_object_id': 'raw', 'artifact_path': '/raw'})
    monkeypatch.setattr(MODULE, 'materialize_timo_revenue_snapshot', lambda *args, **kwargs: observed.update(kwargs) or {'status': 'no_op', 'ok': True})
    monkeypatch.setattr(MODULE, 'materialize_streamer_analytics_tables', lambda *args, **kwargs: {'ok': True, 'apps': {'timo': {'status': 'ready'}}})
    result = MODULE.run({
        'source_file': str(source),
        'db_path': str(db),
        'business_date': '2026-08-23',
        'guild_id': '22000408',
        'observation_id': 2,
        'expected_sha256': hashlib.sha256(content).hexdigest(),
        'expected_source_row_count': 2,
        'expected_effective_row_count': 2,
        'expected_total_income': '0.3',
        'preimage_path': str(tmp_path / 'preimage.json'),
    })
    assert result['observation_id'] == 2
    assert observed['sync_id'] == base + '_obs2'
    assert observed['guild_executor_key'] == 'timo:cms_guild_sid:lvmy210446316420ie3d'
    assert observed['idempotency_key'].endswith(':obs2')
    assert (tmp_path / 'preimage-obs2.json').is_file()


def _event(*, complete: bool):
    scopes = []
    for country, guild_id, guild_name in (
        ('MX', '22000408', 'Agency MX somente'),
        ('ID', '11003905', 'TIMO001'),
        ('BR', '22000448', 'agency of BR somente'),
    ):
        consumable = complete or country != 'MX'
        scopes.append({
            'businessDate': '2026-08-23',
            'guildId': guild_id,
            'guildName': guild_name,
            'guildStorageName': guild_name,
            'country': country,
            'qualityStatus': 'COMPLETE' if consumable else 'SOURCE_MISSING',
            'consumable': consumable,
            'rowCount': 1 if consumable else None,
            'totalIncome': '1.000000' if consumable else None,
            'checksum': 'a' * 64 if consumable else None,
            'revision': 1 if consumable else None,
            'sourceGeneration': 'sync-1' if consumable else None,
            'materializedAt': '2026-08-25T03:00:00+00:00' if consumable else None,
        })
    failed = 0 if complete else 1
    return {
        'schemaVersion': 2,
        'eventId': 'timo:2026-08-23:test',
        'checksum': 'b' * 64,
        'businessDate': '2026-08-23',
        'dayStatus': 'COMPLETE' if complete else 'PARTIAL',
        'ready': complete,
        'consumable': complete,
        'expectedScopeCount': 3,
        'scopeTotal': 3,
        'scopeSucceeded': 3 - failed,
        'scopeFailed': failed,
        'failedScopes': [] if complete else ['MX'],
        'scopes': scopes,
    }


def test_partial_event_is_diagnostic_only_and_cannot_be_sent(monkeypatch):
    event = _event(complete=False)
    assert NOTIFIER.complete_event_ready_for_downstream(event) is False
    assert NOTIFIER.notification_skip_result(event)['notification_state'] == 'PENDING_REOBSERVE'
    monkeypatch.setattr(NOTIFIER.request, 'urlopen', lambda *args, **kwargs: pytest.fail('network called'))
    with pytest.raises(ValueError, match='downstream_notification_requires_complete'):
        NOTIFIER.send_event(event, url='https://example.invalid', secret='x' * 32)


def test_complete_event_remains_publishable():
    assert NOTIFIER.complete_event_ready_for_downstream(_event(complete=True)) is True


def test_notifier_main_skips_partial_before_secret_or_network(monkeypatch, capsys):
    monkeypatch.setattr(NOTIFIER, 'load_event', lambda *args, **kwargs: _event(complete=False))
    monkeypatch.setattr(
        NOTIFIER.Path,
        'read_text',
        lambda *args, **kwargs: pytest.fail('secret read'),
    )
    monkeypatch.setattr(NOTIFIER.request, 'urlopen', lambda *args, **kwargs: pytest.fail('network called'))
    monkeypatch.setattr(sys, 'argv', ['notify_timo_materialization.py'])
    assert NOTIFIER.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result['notification_state'] == 'PENDING_REOBSERVE'
    assert result['skipped'] == 'downstream_notification_requires_complete'


def test_official_import_marks_first_observation_pending_without_ack(monkeypatch, tmp_path):
    notifier = importlib.import_module('scripts.notify_timo_materialization')
    monkeypatch.setattr(notifier, 'current_event_for_date', lambda *args, **kwargs: _event(complete=False))
    monkeypatch.setattr(
        notifier.Path,
        'read_text',
        lambda *args, **kwargs: pytest.fail('secret read'),
    )
    monkeypatch.setattr(notifier, 'write_ack', lambda *args, **kwargs: pytest.fail('ack written'))
    db = tmp_path / 'automation.db'
    sqlite3.connect(db).close()
    result = MODULE.notify({'business_date': '2026-08-23', 'db_path': str(db)})
    assert result['notification_state'] == 'PENDING_REOBSERVE'
    assert result['scope_succeeded'] == 2
    assert result['scope_failed'] == 1
