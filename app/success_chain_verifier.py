from __future__ import annotations

import json
import sqlite3
import urllib.request
from typing import Any, Dict, Optional


def _json_loads(text: Optional[str], default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _pick_latest(rows: list[sqlite3.Row], predicate) -> Optional[dict[str, Any]]:
    for row in reversed(rows):
        item = dict(row)
        if predicate(item):
            return item
    return None


def _fetch_runtime_health(runtime_health_url: Optional[str]) -> Optional[Dict[str, Any]]:
    if not runtime_health_url:
        return None
    try:
        with urllib.request.urlopen(runtime_health_url, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"fetch_error": str(exc), "url": runtime_health_url}


def _resolve_lead_row(
    conn: sqlite3.Connection,
    *,
    lead_id: Optional[str] = None,
    mobile: Optional[str] = None,
    account_id: Optional[str] = None,
    invite_code: Optional[str] = None,
    registration_group: Optional[str] = None,
) -> Optional[sqlite3.Row]:
    if lead_id:
        return conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()

    clauses = []
    params: list[Any] = []
    if mobile:
        normalized_mobile = ''.join(ch for ch in str(mobile) if ch.isdigit())
        if normalized_mobile.startswith('62') and len(normalized_mobile) > 2:
            normalized_mobile = normalized_mobile[2:]
        clauses.append("mobile = ?")
        params.append(normalized_mobile)
    if account_id:
        clauses.append("yw_id = ?")
        params.append(str(account_id).strip())
    if invite_code:
        clauses.append("inviter_id = ?")
        params.append(str(invite_code).strip().upper())
    if registration_group:
        clauses.append("pendaftaran_group = ?")
        params.append(str(registration_group).strip())
    if not clauses:
        return None
    sql = f"SELECT * FROM leads WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def build_success_chain_report(
    *,
    db_path: str,
    lead_id: Optional[str] = None,
    mobile: Optional[str] = None,
    account_id: Optional[str] = None,
    invite_code: Optional[str] = None,
    registration_group: Optional[str] = None,
    runtime_health_url: Optional[str] = None,
) -> Dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        lead_row = _resolve_lead_row(
            conn,
            lead_id=lead_id,
            mobile=mobile,
            account_id=account_id,
            invite_code=invite_code,
            registration_group=registration_group,
        )
        runtime_health = _fetch_runtime_health(runtime_health_url)
        if not lead_row:
            return {
                "resolved": False,
                "final_success": False,
                "summary": "LEAD_NOT_FOUND",
                "query": {
                    "lead_id": lead_id,
                    "mobile": mobile,
                    "account_id": account_id,
                    "invite_code": invite_code,
                    "registration_group": registration_group,
                },
                "runtime_health": runtime_health,
            }

        lead = dict(lead_row)
        tasks = [dict(r) for r in conn.execute("SELECT * FROM automation_tasks WHERE lead_id = ? ORDER BY created_at ASC", (lead['lead_id'],)).fetchall()]
        sync_logs = [dict(r) for r in conn.execute("SELECT * FROM sync_logs WHERE lead_id = ? ORDER BY created_at ASC", (lead['lead_id'],)).fetchall()]
        submissions = [dict(r) for r in conn.execute("SELECT * FROM account_submissions WHERE lead_id = ? ORDER BY created_at ASC", (lead['lead_id'],)).fetchall()]

        for task in tasks:
            task['payload'] = _json_loads(task.get('payload'), {})
            task['raw_result'] = _json_loads(task.get('raw_result'), {})
        for log in sync_logs:
            log['request_snapshot'] = _json_loads(log.get('request_snapshot'), {})
            log['response_snapshot'] = _json_loads(log.get('response_snapshot'), {})
        for sub in submissions:
            sub['recognition_raw'] = _json_loads(sub.get('recognition_raw'), {})

        latest_submission = submissions[-1] if submissions else None
        latest_bind_task = _pick_latest(tasks, lambda r: str(r.get('task_type') or '') == 'bind_check')
        latest_crm_log = _pick_latest(sync_logs, lambda r: str(r.get('sync_type') or '') == 'customer_upsert')

        requested_mobile = ''.join(ch for ch in str(mobile or '') if ch.isdigit())
        if requested_mobile.startswith('62') and len(requested_mobile) > 2:
            requested_mobile = requested_mobile[2:]
        parse_ok = bool(lead)
        if mobile:
            parse_ok = parse_ok and str(lead.get('mobile') or '') == requested_mobile
        if account_id:
            parse_ok = parse_ok and str(lead.get('yw_id') or '') == str(account_id).strip()
        if invite_code:
            parse_ok = parse_ok and str(lead.get('inviter_id') or '').upper() == str(invite_code).strip().upper()
        if registration_group:
            parse_ok = parse_ok and str(lead.get('pendaftaran_group') or '') == str(registration_group).strip()

        bind_ok = bool(latest_bind_task and str(latest_bind_task.get('status') or '') == 'success')
        crm_response = (latest_crm_log or {}).get('response_snapshot') or {}
        crm_create_ok = bool((crm_response.get('crm_response') or {}).get('code') == 0)
        crm_verify_ok = bool(
            crm_response.get('verified_after_write')
            or lead.get('crm_verified_at')
            or lead.get('crm_verified_payload')
        )
        final_success = bool(parse_ok and bind_ok and crm_create_ok and crm_verify_ok)

        report = {
            "resolved": True,
            "lead": {
                "lead_id": lead.get('lead_id'),
                "current_status": lead.get('current_status'),
                "mobile": lead.get('mobile'),
                "area_code": lead.get('area_code'),
                "account_id": lead.get('yw_id'),
                "invite_code": lead.get('inviter_id'),
                "registration_group": lead.get('pendaftaran_group'),
                "app_name": lead.get('app_name'),
                "dept_name": lead.get('dept_name'),
                "crm_verified_at": lead.get('crm_verified_at'),
            },
            "latest_submission": {
                "submission_id": (latest_submission or {}).get('submission_id'),
                "source_channel": (latest_submission or {}).get('source_channel'),
                "submitted_by": (latest_submission or {}).get('submitted_by'),
                "remark": (latest_submission or {}).get('remark'),
                "created_at": (latest_submission or {}).get('created_at'),
            },
            "stages": {
                "parse": {
                    "ok": parse_ok,
                    "reason": None if parse_ok else "lead fields do not match requested query",
                },
                "bind": {
                    "ok": bind_ok,
                    "task_id": (latest_bind_task or {}).get('task_id'),
                    "status": (latest_bind_task or {}).get('status'),
                    "result_code": (latest_bind_task or {}).get('result_code'),
                    "result_reason": (latest_bind_task or {}).get('result_reason'),
                },
                "crm_create": {
                    "ok": crm_create_ok,
                    "sync_log_id": (latest_crm_log or {}).get('sync_log_id'),
                    "status": (latest_crm_log or {}).get('status'),
                    "crm_response_code": (crm_response.get('crm_response') or {}).get('code'),
                },
                "crm_verify": {
                    "ok": crm_verify_ok,
                    "verified_after_write": crm_response.get('verified_after_write'),
                    "crm_verified_at": lead.get('crm_verified_at'),
                },
            },
            "final_success": final_success,
            "summary": "REAL_SUCCESS_CONFIRMED" if final_success else "REAL_SUCCESS_NOT_CONFIRMED",
            "runtime_health": runtime_health,
        }
        return report
    finally:
        conn.close()
