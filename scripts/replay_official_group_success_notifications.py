#!/usr/bin/env python3
import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.production_ops import FeishuNotifier, build_success_notifications, format_lark_alert
from scripts.production_ops_daemon import _load_notify_profile_env

DB_PATH = ROOT_DIR / 'data' / 'automation.db'


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value: str) -> datetime:
    normalized = str(value or '').strip().replace('Z', '+00:00')
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_notifier(profile_name: str) -> FeishuNotifier:
    env = _load_notify_profile_env(profile_name)
    app_id = str(env.get('FEISHU_APP_ID') or '').strip()
    app_secret = str(env.get('FEISHU_APP_SECRET') or '').strip()
    chat_id = str(env.get('FEISHU_HOME_CHANNEL') or '').strip()
    domain = str(env.get('FEISHU_DOMAIN') or 'lark').strip() or 'lark'
    if not app_id or not app_secret or not chat_id:
        raise RuntimeError(f'notify profile not ready: {profile_name}')
    return FeishuNotifier(app_id=app_id, app_secret=app_secret, chat_id=chat_id, domain=domain)


def load_bindings(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT account_key, account_name, responsible_type, group_links FROM whatsapp_approval_accounts WHERE enabled = 1"
    ).fetchall()
    bindings: List[Dict[str, Any]] = []
    for row in rows:
        if str(row['responsible_type'] or '').strip() != 'official_group':
            continue
        try:
            group_links = json.loads(row['group_links'] or '[]')
        except Exception:
            group_links = []
        if not isinstance(group_links, list):
            continue
        for item in group_links:
            if not isinstance(item, dict):
                continue
            if item.get('enabled') is False:
                continue
            bindings.append({
                'account_key': str(row['account_key'] or '').strip(),
                'account_name': str(row['account_name'] or '').strip(),
                'notify_profile_name': str(item.get('notify_profile_name') or '').strip(),
                'notify_robot_name': str(item.get('notify_robot_name') or '').strip(),
                'group_name': str(item.get('group_name') or '').strip(),
                'registration_group': str(item.get('registration_group') or '').strip(),
                'group_id': str(item.get('group_id') or '').strip(),
                'link': str(item.get('link') or '').strip(),
            })
    return bindings


def match_binding(bindings: List[Dict[str, Any]], target_group: str, group_name: str) -> Optional[Dict[str, Any]]:
    normalized_target = str(target_group or '').strip().lower()
    normalized_group_name = str(group_name or '').strip().lower()
    for binding in bindings:
        candidates = {
            str(binding.get('registration_group') or '').strip().lower(),
            str(binding.get('group_name') or '').strip().lower(),
            str(binding.get('group_id') or '').strip().lower(),
            str(binding.get('link') or '').strip().lower(),
        }
        candidates.discard('')
        if normalized_target and normalized_target in candidates:
            return binding
        if normalized_group_name and normalized_group_name in candidates:
            return binding
    return None


def already_sent(conn: sqlite3.Connection, approval_run_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM operator_audit_log WHERE event_type = 'official_group_success_notification_sent' AND payload LIKE ? LIMIT 1",
        (f'%"approval_run_id": "{approval_run_id}"%',),
    ).fetchone()
    return row is not None


def record_sent(conn: sqlite3.Connection, *, lead_id: str, approval_run_id: str, notify_profile_name: str, message_text: str) -> None:
    conn.execute(
        "INSERT INTO operator_audit_log (audit_id, lead_id, ingress_event_id, event_type, event_source, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            f'audit_{uuid.uuid4().hex[:12]}',
            lead_id or None,
            None,
            'official_group_success_notification_sent',
            'replay_official_group_success_notifications',
            json.dumps({
                'approval_run_id': approval_run_id,
                'notify_profile_name': notify_profile_name,
                'message_text': message_text,
            }, ensure_ascii=False),
            utc_now_iso(),
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description='Replay missed official-group success notifications.')
    parser.add_argument('--hours', type=float, default=48.0)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    since = (datetime.now(timezone.utc) - timedelta(hours=max(args.hours, 0.0))).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    bindings = load_bindings(conn)
    rows = conn.execute(
        "SELECT lead_id, payload, created_at FROM operator_audit_log WHERE event_type = 'official_group_approval_decision_executed' AND created_at >= ? ORDER BY created_at DESC LIMIT ?",
        (since, max(args.limit * 5, 5)),
    ).fetchall()
    unique_rows = []
    seen_leads = set()
    for row in rows:
        lead_id = str(row['lead_id'] or '').strip()
        if lead_id and lead_id in seen_leads:
            continue
        if lead_id:
            seen_leads.add(lead_id)
        unique_rows.append(row)
        if len(unique_rows) >= max(args.limit, 1):
            break
    rows = list(reversed(unique_rows))

    sent_rows: List[Dict[str, Any]] = []
    skipped_rows: List[Dict[str, Any]] = []
    successful_rows: List[Dict[str, Any]] = []
    ready_group_by_target: Dict[str, Dict[str, Any]] = {}
    approval_run_lead_map: Dict[str, str] = {}

    for row in rows:
        payload = json.loads(row['payload'] or '{}') if row['payload'] else {}
        executor_result = payload.get('executor_result') if isinstance(payload.get('executor_result'), dict) else {}
        raw_result = executor_result.get('raw_result') if isinstance(executor_result.get('raw_result'), dict) else {}
        if str(executor_result.get('status') or '').strip().lower() != 'success':
            skipped_rows.append({'lead_id': row['lead_id'], 'reason': 'not_success'})
            continue
        approval_run_id = str(raw_result.get('approval_run_id') or '').strip()
        if not approval_run_id:
            skipped_rows.append({'lead_id': row['lead_id'], 'reason': 'missing_approval_run_id'})
            continue
        if already_sent(conn, approval_run_id):
            skipped_rows.append({'lead_id': row['lead_id'], 'reason': 'already_sent', 'approval_run_id': approval_run_id})
            continue
        target_group = str(payload.get('target_group') or raw_result.get('target_group') or '').strip()
        group_name = str(raw_result.get('group_name') or '').strip()
        binding = match_binding(bindings, target_group=target_group, group_name=group_name)
        if not binding:
            skipped_rows.append({'lead_id': row['lead_id'], 'reason': 'binding_not_found', 'approval_run_id': approval_run_id})
            continue
        notify_profile_name = str(binding.get('notify_profile_name') or '').strip()
        if not notify_profile_name:
            skipped_rows.append({'lead_id': row['lead_id'], 'reason': 'notify_profile_missing', 'approval_run_id': approval_run_id})
            continue
        approval_run_lead_map[approval_run_id] = str(row['lead_id'] or '').strip()
        ready_key = target_group or group_name or approval_run_id
        ready_group_by_target.setdefault(ready_key, {
            'target_group': target_group or None,
            'group_name': group_name or binding.get('group_name') or None,
            'account_key': binding.get('account_key') or None,
            'notify_profile_name': notify_profile_name,
            'notify_robot_name': str(binding.get('notify_robot_name') or '').strip() or None,
            'registration_group': str((payload.get('eligibility') or {}).get('crm_snapshot', {}).get('pendaftaranGroup') or '').strip() or None,
        })
        successful_rows.append({
            'lead_id': str(payload.get('lead_id') or row['lead_id'] or '').strip() or None,
            'target_group': target_group or None,
            'executed': True,
            'executor_result': {
                'status': 'success',
                'verified': executor_result.get('verified', True),
                'approved_count': executor_result.get('approved_count', 1),
                'raw_result': {
                    **raw_result,
                    'approval_run_id': approval_run_id,
                    'group_name': group_name or binding.get('group_name') or None,
                    'target_group': target_group or None,
                },
            },
        })

    if successful_rows:
        cycle = {
            'checked_at': successful_rows[-1].get('created_at') if isinstance(successful_rows[-1], dict) else utc_now_iso(),
            'registration_group': str(next(iter(ready_group_by_target.values())).get('registration_group') or '888').strip() if ready_group_by_target else '888',
            'official_group_dispatch': {
                'triggered': True,
                'ok': True,
                'ready_groups': list(ready_group_by_target.values()),
                'result': {'results': successful_rows},
            },
        }
        notifications = [
            item
            for item in build_success_notifications(cycle)
            if isinstance(item, dict) and str(item.get('code') or '').strip() == 'official_group_approval_succeeded'
        ]
        for incident in notifications:
            details = incident.get('details') if isinstance(incident.get('details'), dict) else {}
            approval_run_ids = [str(item).strip() for item in list(details.get('approval_run_ids') or []) if str(item).strip()]
            notify_profile_name = str(incident.get('notify_profile_name') or '').strip()
            notify_robot_name = str(incident.get('notify_robot_name') or '').strip() or None
            if not notify_profile_name:
                skipped_rows.append({'reason': 'notify_profile_missing_for_notification', 'approval_run_ids': approval_run_ids})
                continue
            cycle_context = {
                'checked_at': utc_now_iso(),
                'registration_group': str(next(iter(ready_group_by_target.values())).get('registration_group') or '888').strip() if ready_group_by_target else '888',
                'monitor_target': {
                    'notify_profile_name': notify_profile_name,
                    'notify_robot_name': notify_robot_name,
                    'group_name': details.get('group_name'),
                },
            }
            message_text = format_lark_alert('production-ops-daemon', incident, cycle_context)
            if args.dry_run:
                sent_rows.append({
                    'approval_run_ids': approval_run_ids,
                    'notify_profile_name': notify_profile_name,
                    'message_text': message_text,
                    'dry_run': True,
                })
                continue
            notifier = build_notifier(notify_profile_name)
            notifier.send_text(message_text)
            for approval_run_id in approval_run_ids:
                record_sent(
                    conn,
                    lead_id=approval_run_lead_map.get(approval_run_id, ''),
                    approval_run_id=approval_run_id,
                    notify_profile_name=notify_profile_name,
                    message_text=message_text,
                )
            conn.commit()
            sent_rows.append({
                'approval_run_ids': approval_run_ids,
                'notify_profile_name': notify_profile_name,
                'notify_robot_name': notify_robot_name,
            })

    print(json.dumps({'sent': sent_rows, 'skipped': skipped_rows}, ensure_ascii=False, indent=2))
    conn.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
