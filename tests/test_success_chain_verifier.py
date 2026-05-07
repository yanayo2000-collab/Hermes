import sqlite3
from pathlib import Path

from app.success_chain_verifier import build_success_chain_report


def _setup_min_db(path: Path):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE leads (
            lead_id TEXT PRIMARY KEY,
            mobile TEXT,
            area_code INTEGER,
            yw_id TEXT,
            inviter_id TEXT,
            pendaftaran_group TEXT,
            app_name TEXT,
            dept_name TEXT,
            current_status TEXT,
            matched_customer_id TEXT,
            crm_verified_payload TEXT,
            crm_verified_registration_group TEXT,
            crm_verified_official_group TEXT,
            crm_verified_at TEXT,
            updated_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE automation_tasks (
            task_id TEXT PRIMARY KEY,
            lead_id TEXT,
            task_type TEXT,
            status TEXT,
            result_code TEXT,
            result_reason TEXT,
            created_at TEXT,
            payload TEXT,
            raw_result TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE sync_logs (
            sync_log_id TEXT PRIMARY KEY,
            lead_id TEXT,
            task_id TEXT,
            sync_type TEXT,
            target_system TEXT,
            status TEXT,
            request_snapshot TEXT,
            response_snapshot TEXT,
            created_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE account_submissions (
            submission_id TEXT PRIMARY KEY,
            lead_id TEXT,
            created_at TEXT,
            source_channel TEXT,
            submitted_by TEXT,
            remark TEXT,
            recognition_raw TEXT
        )
        """
    )
    conn.commit()
    return conn


def test_build_success_chain_report_accepts_recovered_success_evidence(tmp_path):
    db_path = tmp_path / 'success.db'
    conn = _setup_min_db(db_path)
    conn.execute(
        "INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            'lead_success', '13434710947', 1, '2', None, '888', 'Linky', 'Permata',
            'group_join_success', 'cust_x', None, '888', '官方测试1', '2026-05-06T09:54:20+00:00', '2026-05-06T09:54:20+00:00'
        ),
    )
    conn.execute(
        "INSERT INTO automation_tasks VALUES (?,?,?,?,?,?,?,?,?)",
        ('task_group_ok', 'lead_success', 'group_join', 'success', 'approved', 'ok', '2026-04-30T10:25:30+00:00', '{}', '{}'),
    )
    conn.commit()
    conn.close()

    report = build_success_chain_report(db_path=str(db_path), lead_id='lead_success', runtime_health_url=None)
    assert report['final_success'] is True
    assert report['stages']['bind']['ok'] is True
    assert report['stages']['crm_create']['ok'] is True
    assert report['stages']['crm_verify']['ok'] is True
    assert report['stages']['group_join']['ok'] is True


def test_build_success_chain_report_keeps_real_crm_500_failure_as_not_confirmed(tmp_path):
    db_path = tmp_path / 'failed.db'
    conn = _setup_min_db(db_path)
    conn.execute(
        "INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            'lead_fail', '89999999983', 62, None, None, 'Piso-5', 'Linky', 'Piso',
            'group_join_failed', 'cust_maybe', None, None, None, None, '2026-05-06T09:31:03+00:00'
        ),
    )
    conn.execute(
        "INSERT INTO automation_tasks VALUES (?,?,?,?,?,?,?,?,?)",
        ('task_bind_ok', 'lead_fail', 'bind_check', 'success', 'bind_ok', 'ok', '2026-05-06T08:30:00+00:00', '{}', '{}'),
    )
    conn.execute(
        "INSERT INTO automation_tasks VALUES (?,?,?,?,?,?,?,?,?)",
        ('task_group_fail', 'lead_fail', 'group_join', 'failed', 'group_join_auto_closed_missing_runtime_requester', 'failed', '2026-05-06T08:31:00+00:00', '{}', '{}'),
    )
    conn.execute(
        "INSERT INTO sync_logs VALUES (?,?,?,?,?,?,?,?,?)",
        (
            'sync_fail', 'lead_fail', 'task_retry', 'customer_upsert', 'crm', 'failed', '{}',
            '{"action":"create","crm_response":{"code":500},"verified_after_write":false}', '2026-05-06T08:33:49+00:00'
        ),
    )
    conn.commit()
    conn.close()

    report = build_success_chain_report(db_path=str(db_path), lead_id='lead_fail', runtime_health_url=None)
    assert report['final_success'] is False
    assert report['stages']['crm_create']['ok'] is False
    assert report['stages']['crm_verify']['ok'] is False
    assert report['stages']['group_join']['ok'] is False
