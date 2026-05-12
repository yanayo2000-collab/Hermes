#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from urllib import request


def fetch_json(url: str, *, method: str = 'GET', payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = request.Request(url, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    internal_token = str(__import__('os').getenv('AUTH_INTERNAL_TOKEN') or '').strip()
    if internal_token and '/api/ops/' in str(url or ''):
        req.add_header('x-ops-internal-token', internal_token)
    with request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


def main() -> int:
    parser = argparse.ArgumentParser(description='Run an official-group approval smoke test against a configured local service.')
    parser.add_argument('--base-url', default='http://127.0.0.1:8011')
    parser.add_argument('--target-group', default='official-group-a')
    parser.add_argument('--mobile', default='89999999983')
    parser.add_argument('--account-id', default='66778883')
    parser.add_argument('--registration-group', default='Piso-5')
    parser.add_argument('--app-name', default='Linky')
    parser.add_argument('--dept-name', default='Piso')
    args = parser.parse_args()

    lead = fetch_json(f"{args.base_url}/api/leads/upsert", method='POST', payload={
        'trace_id': 'smoke-official-group-1',
        'source_platform': 'meta',
        'source_page_id': 'page-smoke-official-group-1',
        'country': 'Indonesia',
        'area_code': 62,
        'mobile': args.mobile,
        'app_name': args.app_name,
        'dept_name': args.dept_name,
        'pendaftaran_group': args.registration_group,
    })
    submission = fetch_json(f"{args.base_url}/api/account-submissions", method='POST', payload={
        'lead_id': lead['lead_id'],
        'submission_type': 'account_id',
        'account_id': args.account_id,
        'account_id_type': 'platform_uid',
        'source_channel': 'whatsapp',
        'submitted_by': 'official_group_smoke',
        'submitted_at': '2026-04-14T12:15:00Z',
    })
    bind_result = fetch_json(f"{args.base_url}/api/tasks/{submission['task_id']}/bind-check-result", method='POST', payload={
        'status': 'success',
        'result_code': 'bind_ok',
        'result_reason': 'smoke bind success',
        'finished_at': '2026-04-14T12:17:00Z',
        'raw_result': {'guild_code': args.dept_name, 'deptName': args.dept_name, 'deptId': 'dept_1'},
    })
    approval = fetch_json(f"{args.base_url}/api/official-groups/approval-decisions", method='POST', payload={
        'lead_id': lead['lead_id'],
        'target_group': args.target_group,
        'decision': 'approve',
        'decided_at': '2026-04-14T12:18:00Z',
        'decided_by': 'official_group_smoke',
    })
    result = {
        'lead_id': lead['lead_id'],
        'group_join_task_id': bind_result.get('group_join_task_id'),
        'approval': approval,
    }
    if approval.get('decision_result', {}).get('lead_status') == 'group_join_success':
        result['ok'] = True
        result['summary'] = 'OFFICIAL_GROUP_SMOKE_SUCCESS'
    else:
        result['ok'] = False
        result['summary'] = 'OFFICIAL_GROUP_SMOKE_FAILED'
        result['failure_reason_code'] = approval.get('reason_code') or approval.get('decision_result', {}).get('result_code')
        result['failure_next_action'] = approval.get('next_action') or approval.get('follow_up_action')
    summary = fetch_json(f"{args.base_url}/api/ops/official-group-approval-summary")
    result['summary_snapshot'] = {
        'approved_count': summary.get('approved_count'),
        'failed_count': summary.get('failed_count'),
        'retryable_failed_count': summary.get('retryable_failed_count'),
        'manual_required_count': summary.get('manual_required_count'),
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
