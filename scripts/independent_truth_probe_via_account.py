#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


def load_internal_token(env_path: Path) -> str:
    text = env_path.read_text(encoding='utf-8')
    match = re.search(r"AUTH_INTERNAL_TOKEN\s*=\s*['\"]?([^'\"\n]+)['\"]?", text)
    if not match:
        raise RuntimeError(f'internal_token_missing:{env_path}')
    return str(match.group(1) or '').strip()


def _request_json(url: str, *, internal_token: str, method: str = 'GET', payload: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            'x-ops-internal-token': internal_token,
            'Content-Type': 'application/json',
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def fetch_truth_probe_payload(
    *,
    api_base_url: str,
    internal_token: str,
    account_key: str,
    registration_group: str,
    timeout: float = 30.0,
    require_login_verified: bool = False,
    fetch_session_first: bool = False,
) -> Dict[str, Any]:
    api_root = api_base_url.rstrip('/')
    runtime_payload = _request_json(
        f'{api_root}/api/ops/whatsapp-approval-accounts/{urllib.parse.quote(account_key, safe="")}/runtime/internal/start',
        internal_token=internal_token,
        method='POST',
        payload={},
        timeout=timeout,
    )
    runtime = dict(runtime_payload.get('runtime') or {})
    base_url = str(runtime.get('base_url') or '').strip()
    if not base_url:
        raise RuntimeError('runtime_base_url_missing')

    if fetch_session_first:
        session_payload = _request_json(
            f'{api_root}/api/ops/whatsapp-approval-accounts/{urllib.parse.quote(account_key, safe="")}/session/internal',
            internal_token=internal_token,
            timeout=timeout,
        )
        session = dict(session_payload.get('session') or {})
        if require_login_verified and not bool(session.get('login_verified')):
            status = str(session.get('login_check_status') or '').strip() or 'session_not_ready'
            if not bool(session.get('qr_available')):
                session_start_payload = _request_json(
                    f'{api_root}/api/ops/whatsapp-approval-accounts/{urllib.parse.quote(account_key, safe="")}/session/internal/start',
                    internal_token=internal_token,
                    method='POST',
                    payload={},
                    timeout=timeout,
                )
                session = dict(session_start_payload.get('session') or session)
                status = str(session.get('login_check_status') or '').strip() or status
            raise RuntimeError(status)

    return _request_json(
        f'{base_url.rstrip("/")}/group-state',
        internal_token=internal_token,
        method='POST',
        payload={'registration_group': registration_group},
        timeout=timeout,
    )


def build_reconcile_result(
    *,
    account_key: str,
    registration_group: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    try:
        observed_pending_count = max(int(payload.get('pending_count') or 0), 0)
    except (TypeError, ValueError):
        observed_pending_count = 0
    return {
        'group_key': str(payload.get('group_id') or registration_group or '').strip() or str(registration_group or '').strip(),
        'account_key': str(account_key or '').strip(),
        'registration_group': str(registration_group or '').strip(),
        'observed_pending_count': observed_pending_count,
        'checked_at': str(payload.get('source_ts') or '').strip() or None,
        'probe_status': 'ok',
        'session_health': str(payload.get('session_health') or 'healthy').strip() or 'healthy',
        'reconcile_result': 'match_zero' if observed_pending_count <= 0 else 'mismatch_positive',
        'authoritative_source': 'group_state',
        'payload': payload,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Independent truth probe via dedicated LocalAuth approval account')
    parser.add_argument('--account-key', required=True)
    parser.add_argument('--registration-group', required=True)
    parser.add_argument('--api-base-url', default=os.getenv('PRODUCTION_OPS_API_BASE_URL', 'http://127.0.0.1:8011'))
    parser.add_argument('--internal-auth-env', default=os.getenv('PRODUCTION_OPS_INTERNAL_AUTH_ENV', str(Path(__file__).resolve().parents[1] / 'data' / 'internal_auth.env')))
    parser.add_argument('--timeout-seconds', type=float, default=float(os.getenv('PROBE_TIMEOUT_SECONDS', '30') or 30))
    args = parser.parse_args()

    internal_token = load_internal_token(Path(args.internal_auth_env).expanduser())
    try:
        payload = fetch_truth_probe_payload(
            api_base_url=args.api_base_url,
            internal_token=internal_token,
            account_key=args.account_key,
            registration_group=args.registration_group,
            timeout=args.timeout_seconds,
            require_login_verified=True,
            fetch_session_first=True,
        )
    except urllib.error.HTTPError as exc:
        raise SystemExit(f'http_error:{exc.code}')
    except Exception as exc:
        raise SystemExit(str(exc))
    print(json.dumps(build_reconcile_result(
        account_key=args.account_key,
        registration_group=args.registration_group,
        payload=payload,
    ), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
