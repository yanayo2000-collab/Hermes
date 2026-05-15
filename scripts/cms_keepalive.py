#!/usr/bin/env python3
"""Keep Linky CMS authorization sessions warm without logging secrets.

This script performs a harmless CMS search/list action for every configured CMS
account. It is intended to be launched every 6 hours by launchd/cron so CMS
sessions do not expire due to 24h inactivity.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_BASE_URL = 'https://cms.linke.ai'
DEFAULT_STATE_PATH = Path(__file__).resolve().parents[1] / 'data' / 'cms_keepalive_status.json'
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass
class CmsAccount:
    name: str
    authorization: str
    guild_id: Optional[str] = None
    guild_sid: Optional[str] = None
    endpoint: str = '/api/admin/linky/industrial/streamer_detail/page'

    @property
    def safe_name(self) -> str:
        return self.name or self.guild_sid or self.guild_id or 'cms-account'


def _truthy(value: str) -> bool:
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _json_loads(raw: str, *, source: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f'invalid JSON in {source}: {exc}') from exc


def _account_from_dict(item: Dict[str, Any], index: int) -> Optional[CmsAccount]:
    auth = str(item.get('authorization') or item.get('token') or '').strip()
    if not auth:
        return None
    return CmsAccount(
        name=str(item.get('name') or item.get('guild_name') or f'cms-{index}').strip(),
        authorization=auth,
        guild_id=str(item.get('guild_id') or '').strip() or None,
        guild_sid=str(item.get('guild_sid') or item.get('sid') or '').strip() or None,
        endpoint=str(item.get('endpoint') or '/api/admin/linky/industrial/streamer_detail/page').strip(),
    )


def load_accounts() -> List[CmsAccount]:
    accounts: List[CmsAccount] = []

    raw_json = str(os.getenv('LINKE_CMS_ACCOUNTS_JSON') or os.getenv('CMS_KEEPALIVE_ACCOUNTS_JSON') or '').strip()
    if raw_json:
        parsed = _json_loads(raw_json, source='LINKE_CMS_ACCOUNTS_JSON/CMS_KEEPALIVE_ACCOUNTS_JSON')
        if not isinstance(parsed, list):
            raise SystemExit('CMS accounts JSON must be an array')
        for index, item in enumerate(parsed, start=1):
            if isinstance(item, dict):
                account = _account_from_dict(item, index)
                if account:
                    accounts.append(account)

    # Backward-compatible single-token mode.
    single_auth = str(os.getenv('LINKE_CMS_AUTHORIZATION') or '').strip()
    if single_auth:
        accounts.append(
            CmsAccount(
                name=str(os.getenv('LINKE_CMS_ACCOUNT_NAME') or os.getenv('LINKE_CMS_GUILD_NAME') or 'default').strip(),
                authorization=single_auth,
                guild_id=str(os.getenv('LINKE_CMS_GUILD_ID') or '').strip() or None,
                guild_sid=str(os.getenv('LINKE_CMS_GUILD_SID') or '').strip() or None,
            )
        )

    # De-duplicate by authorization token value without exposing it.
    unique: List[CmsAccount] = []
    seen = set()
    for account in accounts:
        if account.authorization in seen:
            continue
        seen.add(account.authorization)
        unique.append(account)
    return unique


def _request_json(base_url: str, account: CmsAccount, body: Dict[str, Any], timeout: float) -> Tuple[int, Any]:
    url = base_url.rstrip('/') + account.endpoint
    data = json.dumps(body, separators=(',', ':')).encode('utf-8')
    request = urllib.request.Request(
        url,
        data=data,
        method='POST',
        headers={
            'authorization': account.authorization,
            'content-type': 'application/json',
            'accept': 'application/json, text/plain, */*',
            'origin': base_url.rstrip('/'),
            'referer': base_url.rstrip('/') + '/anchorDetail',
            'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 Chrome Safari',
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode('utf-8', 'ignore')
    try:
        return response.status, json.loads(text)
    except Exception:
        return response.status, {'raw': text[:200]}


def keepalive_account(base_url: str, account: CmsAccount, timeout: float, retries: int) -> Dict[str, Any]:
    started = int(time.time())
    body = {'page': 1, 'size': 1}
    last_error = ''
    for attempt in range(1, retries + 1):
        try:
            status, payload = _request_json(base_url, account, body, timeout)
            code = payload.get('code') if isinstance(payload, dict) else None
            ok = status == 200 and (code in (1000, None) or str(code) == '1000')
            return {
                'account': account.safe_name,
                'guild_id': account.guild_id,
                'guild_sid': account.guild_sid,
                'ok': ok,
                'http_status': status,
                'cms_code': code,
                'error': None if ok else _safe_message(payload),
                'attempts': attempt,
                'checked_at': started,
            }
        except urllib.error.HTTPError as exc:
            last_error = f'http_error_{exc.code}'
            if exc.code in (401, 403):
                break
        except Exception as exc:  # noqa: BLE001 - CLI tool must classify transient network errors.
            last_error = f'{type(exc).__name__}: {str(exc)[:120]}'
        if attempt < retries:
            time.sleep(min(2 * attempt, 6))
    return {
        'account': account.safe_name,
        'guild_id': account.guild_id,
        'guild_sid': account.guild_sid,
        'ok': False,
        'http_status': None,
        'cms_code': None,
        'error': last_error or 'unknown_error',
        'attempts': retries,
        'checked_at': started,
    }


def _safe_message(payload: Any) -> str:
    if isinstance(payload, dict):
        message = payload.get('message') or payload.get('msg') or payload.get('error') or payload.get('raw') or ''
        return str(message)[:200]
    return str(payload)[:200]


def write_state(path: Path, results: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    state = {
        'updated_at': int(time.time()),
        'total': len(results),
        'ok': sum(1 for result in results if result.get('ok')),
        'failed': sum(1 for result in results if not result.get('ok')),
        'results': results,
    }
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='Keep Linky CMS sessions active every 6 hours.')
    parser.add_argument('--env-path', default=str(Path(__file__).resolve().parents[1] / 'data' / 'cms_keepalive.env'))
    parser.add_argument('--base-url', default=os.getenv('LINKE_CMS_BASE_URL') or DEFAULT_BASE_URL)
    parser.add_argument('--state-path', default=str(DEFAULT_STATE_PATH))
    parser.add_argument('--timeout-seconds', type=float, default=float(os.getenv('CMS_KEEPALIVE_TIMEOUT_SECONDS') or DEFAULT_TIMEOUT_SECONDS))
    parser.add_argument('--retries', type=int, default=int(os.getenv('CMS_KEEPALIVE_RETRIES') or 3))
    parser.add_argument('--fail-on-error', action='store_true')
    args = parser.parse_args(list(argv) if argv is not None else None)

    _load_env_file(Path(args.env_path).expanduser())
    accounts = load_accounts()
    if not accounts:
        result = [{
            'account': 'none',
            'guild_id': None,
            'guild_sid': None,
            'ok': False,
            'http_status': None,
            'cms_code': None,
            'error': 'no CMS accounts configured',
            'attempts': 0,
            'checked_at': int(time.time()),
        }]
        write_state(Path(args.state_path).expanduser(), result)
        print(json.dumps({'ok': False, 'total': 0, 'failed': 1, 'error': 'no CMS accounts configured'}, ensure_ascii=False))
        return 2 if args.fail_on_error else 0

    results = [keepalive_account(args.base_url, account, args.timeout_seconds, args.retries) for account in accounts]
    write_state(Path(args.state_path).expanduser(), results)
    failed = [result for result in results if not result.get('ok')]
    print(json.dumps({
        'ok': not failed,
        'total': len(results),
        'success': len(results) - len(failed),
        'failed': len(failed),
        'failed_accounts': [result.get('account') for result in failed],
    }, ensure_ascii=False))
    return 1 if failed and args.fail_on_error else 0


if __name__ == '__main__':
    raise SystemExit(main())
