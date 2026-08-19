from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import types


noop = types.ModuleType('mcn_streamer_noop_evidence')
noop.DEFAULT_RECEIPT = Path('/nonexistent')
noop.accept_noop_work = lambda *_args, **_kwargs: {}
noop.read_evidence = lambda *_args, **_kwargs: {}
sys.modules.setdefault('mcn_streamer_noop_evidence', noop)
spec = importlib.util.spec_from_file_location(
    'mcn_business_completion_reconcile_under_test',
    Path(__file__).resolve().parents[1] / 'scripts/mcn_business_completion_reconcile.py',
)
assert spec and spec.loader
reconciler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reconciler)


def _control(path: Path, *, source_generation: str, created_at: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE work_items (
              work_id TEXT PRIMARY KEY,kind TEXT,state TEXT,deadline_at_utc TEXT,
              block_reason TEXT,metadata_json TEXT,created_at_utc TEXT,updated_at_utc TEXT,version INTEGER
            );
            CREATE TABLE work_stages (
              stage_id TEXT PRIMARY KEY,work_id TEXT,stage_key TEXT,ordinal INTEGER,state TEXT,result_json TEXT,
              finished_at_utc TEXT,not_before_utc TEXT,lease_owner TEXT,lease_expires_at_utc TEXT,
              updated_at_utc TEXT,version INTEGER
            );
            CREATE TABLE resource_leases(resource_name TEXT,slot INTEGER,stage_id TEXT,owner TEXT,acquired_at_utc TEXT,expires_at_utc TEXT);
            CREATE TABLE work_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,work_id TEXT,stage_id TEXT,event_type TEXT,from_state TEXT,to_state TEXT,detail_json TEXT,created_at_utc TEXT);
            """
        )
        metadata = json.dumps({
            'task_id': 'linky-daily-incremental',
            'target': '2026-08-18',
            'source_generation': source_generation,
        })
        connection.execute(
            'INSERT INTO work_items VALUES(?,?,?,?,?,?,?,?,?)',
            ('work', 'business_batch', 'escalated', '', '', metadata, created_at, created_at, 1),
        )
        connection.execute(
            'INSERT INTO work_stages VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            ('stage', 'work', 'run_service', 10, 'blocked_soft', '{}', '', '', '', '', created_at, 1),
        )


def _evidence() -> list[dict[str, object]]:
    return [{
        'task_id': 'linky-daily-incremental',
        'target': '2026-08-18',
        'evidence_id': 'old-proof',
        'source_run_id': 'old-run',
        'source_updated_at_utc': '2026-08-19T02:16:47+00:00',
        'publication_data_as_of': '2026-08-18',
        'publication_materialized_at_utc': '2026-08-19T02:17:19+00:00',
    }]


def test_incident_work_refuses_completion_evidence_created_before_work(tmp_path: Path) -> None:
    control = tmp_path / 'control.db'
    _control(
        control,
        source_generation='incident-recovery-20260819-br-evian',
        created_at='2026-08-19T05:28:17+00:00',
    )

    result = reconciler.reconcile_control_plane(control, _evidence(), dry_run=True)

    assert result['changed'] == []
    assert result['refused'] == [{
        'work_id': 'work',
        'reason': 'evidence_predates_non_timer_work',
    }]


def test_timer_work_keeps_existing_durable_reconciliation_semantics(tmp_path: Path) -> None:
    control = tmp_path / 'control.db'
    _control(control, source_generation='timer', created_at='2026-08-19T05:28:17+00:00')

    result = reconciler.reconcile_control_plane(
        control,
        _evidence(),
        now=datetime(2026, 8, 19, 5, 30, tzinfo=timezone.utc),
        dry_run=True,
    )

    assert result['refused'] == []
    assert result['changed'][0]['source_run_id'] == 'old-run'
