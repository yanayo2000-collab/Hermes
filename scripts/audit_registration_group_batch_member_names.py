#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.main import DEFAULT_DB_PATH, Database, Service


def _load_candidate_rows(service, *, approved_date_start: str, approved_date_end: str, registration_group: str, approval_run_id: str, limit: int, force: bool = False) -> Tuple[List[Dict[str, Any]], int]:
    query = '''
    SELECT member_id, approval_run_id, registration_group, registration_group_name, requester_id, display_name,
           wa_phone_raw, wa_phone_normalized, requested_at, approved_at, batch_index,
           repair_last_attempt_at, repair_last_result, repair_next_attempt_at, created_at, updated_at
    FROM registration_group_approval_batch_members
    ORDER BY approved_at DESC, approval_run_id DESC, batch_index ASC
    LIMIT ?
    '''
    with service.db.connect() as conn:
        rows = [dict(r) for r in conn.execute(query, (max(int(limit or 0), 1),)).fetchall()]
    candidates: List[Dict[str, Any]] = []
    skipped_cooldown = 0
    for row in rows:
        beijing_parts = service._registration_group_batch_member_beijing_parts(str(row.get('approved_at') or ''))
        approved_date_value = str(beijing_parts.get('approved_date_beijing') or '')
        if approved_date_start and approved_date_value < approved_date_start:
            continue
        if approved_date_end and approved_date_value > approved_date_end:
            continue
        if approval_run_id and str(row.get('approval_run_id') or '').strip() != approval_run_id:
            continue
        if registration_group and str(row.get('registration_group') or '').strip() != registration_group:
            continue
        if service._registration_group_batch_member_should_attempt_repair(row, force=force):
            candidates.append(row)
            continue
        if service._registration_group_batch_member_name_needs_repair(row.get('display_name')) and str(row.get('requester_id') or '').strip():
            skipped_cooldown += 1
    return candidates, skipped_cooldown


def main() -> int:
    parser = argparse.ArgumentParser(description='Audit and auto-repair registration-group approval member names')
    parser.add_argument('--db-path', default=DEFAULT_DB_PATH)
    parser.add_argument('--approved-date', default='')
    parser.add_argument('--approved-date-start', default='')
    parser.add_argument('--approved-date-end', default='')
    parser.add_argument('--registration-group', default='')
    parser.add_argument('--approval-run-id', default='')
    parser.add_argument('--limit', type=int, default=500)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()

    db_path = str(Path(args.db_path).expanduser())
    service = Service(Database(db_path))
    if args.approved_date:
        approved_date_start, approved_date_end = service._resolve_registration_group_batch_members_date_range(approved_date=args.approved_date)
    else:
        approved_date_start, approved_date_end = service._resolve_registration_group_batch_members_date_range(
            approved_date_start=args.approved_date_start,
            approved_date_end=args.approved_date_end,
        )
    rows, skipped_cooldown = _load_candidate_rows(
        service,
        approved_date_start=approved_date_start,
        approved_date_end=approved_date_end,
        registration_group=str(args.registration_group or '').strip(),
        approval_run_id=str(args.approval_run_id or '').strip(),
        limit=max(int(args.limit or 0), 1),
        force=bool(args.force),
    )
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get('registration_group') or '').strip(), str(row.get('registration_group_name') or '').strip())].append(row)
    repairs = []
    total_updated = 0
    total_unresolved = 0
    for (registration_group, registration_group_name), group_rows in grouped.items():
        result = service._repair_registration_group_batch_member_rows(
            rows=group_rows,
            registration_group=registration_group,
            registration_group_name=registration_group_name,
            force=bool(args.force),
        )
        repairs.append({
            'registration_group': registration_group,
            'registration_group_name': registration_group_name,
            **result,
        })
        total_updated += int(result.get('updated') or 0)
        total_unresolved += int(result.get('unresolved') or 0)
    print(json.dumps({
        'approved_date_start': approved_date_start,
        'approved_date_end': approved_date_end,
        'candidate_count': len(rows),
        'skipped_cooldown': skipped_cooldown,
        'group_count': len(grouped),
        'updated': total_updated,
        'unresolved': total_unresolved,
        'force': bool(args.force),
        'repairs': repairs,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
