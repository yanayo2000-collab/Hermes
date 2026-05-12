#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from urllib import request, error


def fetch_json(url: str) -> dict:
    headers = {}
    internal_token = str(__import__('os').getenv('AUTH_INTERNAL_TOKEN') or '').strip()
    if internal_token and '/api/ops/' in str(url or ''):
        headers['x-ops-internal-token'] = internal_token
    req = request.Request(url, headers=headers)
    with request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))


def main() -> int:
    parser = argparse.ArgumentParser(description='Verify official-group approval bridge readiness from runtime health endpoints.')
    parser.add_argument('--runtime-health-url', default='http://127.0.0.1:8011/api/ops/runtime-health')
    parser.add_argument('--executor-health-url', default='http://127.0.0.1:8011/api/ops/official-group-approval-executor-health')
    parser.add_argument('--summary-url', default='http://127.0.0.1:8011/api/ops/official-group-approval-summary')
    args = parser.parse_args()

    try:
        runtime = fetch_json(args.runtime_health_url)
        executor = fetch_json(args.executor_health_url)
        summary = fetch_json(args.summary_url)
    except error.URLError as exc:
        print(json.dumps({'ok': False, 'summary': 'OFFICIAL_GROUP_BRIDGE_UNREACHABLE', 'error': str(exc)}, ensure_ascii=False))
        return 2

    official = runtime.get('official_group_approval') or {}
    checks = {
        'runtime_has_official_group_section': isinstance(official, dict),
        'crm_enabled': bool((runtime.get('crm') or {}).get('enabled')),
        'crm_status_healthy': str(((runtime.get('crm') or {}).get('status') or '')).lower() == 'healthy',
        'executor_configured': bool(executor.get('configured')),
        'executor_status_healthy': str(executor.get('status') or '').lower() == 'healthy',
        'schema_version_present': bool(executor.get('schema_version') or ((executor.get('details') or {}).get('schema_version'))),
        'summary_available': isinstance(summary, dict) and 'approved_count' in summary,
    }
    ok = all(checks.values())
    result = {
        'ok': ok,
        'summary': 'OFFICIAL_GROUP_BRIDGE_READY' if ok else 'OFFICIAL_GROUP_BRIDGE_NOT_READY',
        'checks': checks,
        'executor': {
            'provider': executor.get('provider'),
            'status': executor.get('status'),
            'schema_version': executor.get('schema_version') or ((executor.get('details') or {}).get('schema_version')),
            'supports': executor.get('supports') or [],
        },
        'summary_snapshot': {
            'pending_count': summary.get('pending_count'),
            'approved_count': summary.get('approved_count'),
            'failed_count': summary.get('failed_count'),
            'retryable_failed_count': summary.get('retryable_failed_count'),
            'manual_required_count': summary.get('manual_required_count'),
        },
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
