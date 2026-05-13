#!/usr/bin/env python3
import argparse
import contextlib
import io
import json
import os
import secrets
import sys
import warnings
from pathlib import Path
from typing import Any, Dict, Optional

warnings.filterwarnings('ignore', message='urllib3 v2 only supports OpenSSL 1.1.1+')

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

with contextlib.redirect_stdout(io.StringIO()):
    from app.main import Database, OpsAuthManager


DEFAULT_DB_PATH = str(ROOT / 'data' / 'automation.db')
DEFAULT_INTERNAL_AUTH_ENV = str(ROOT / 'data' / 'internal_auth.env')


def _build_manager(db_path: str) -> OpsAuthManager:
    db = Database(db_path)
    return OpsAuthManager(db)


def _status_payload(manager: OpsAuthManager) -> Dict[str, Any]:
    users = manager.list_users()
    return {
        'bootstrap_open': not manager.has_users(),
        'user_count': len(users),
        'users': users,
    }


def cmd_status(args: argparse.Namespace) -> int:
    manager = _build_manager(args.db_path)
    print(json.dumps(_status_payload(manager), ensure_ascii=False, indent=2))
    return 0


def cmd_bootstrap_admin(args: argparse.Namespace) -> int:
    manager = _build_manager(args.db_path)
    user = manager.bootstrap_admin(
        username=args.username,
        password=args.password,
        display_name=args.display_name,
    )
    print(json.dumps({'ok': True, 'user': user}, ensure_ascii=False, indent=2))
    return 0


def cmd_create_user(args: argparse.Namespace) -> int:
    manager = _build_manager(args.db_path)
    user = manager.create_user(
        username=args.username,
        password=args.password,
        role=args.role,
        display_name=args.display_name,
        enabled=not args.disabled,
    )
    print(json.dumps({'ok': True, 'user': user}, ensure_ascii=False, indent=2))
    return 0


def cmd_update_user(args: argparse.Namespace) -> int:
    manager = _build_manager(args.db_path)
    enabled: Optional[bool] = None
    if args.enabled:
        enabled = True
    elif args.disabled:
        enabled = False
    user = manager.update_user(
        args.user_id,
        role=args.role,
        display_name=args.display_name,
        enabled=enabled,
        password=args.password,
    )
    print(json.dumps({'ok': True, 'user': user}, ensure_ascii=False, indent=2))
    return 0


def cmd_ensure_internal_token(args: argparse.Namespace) -> int:
    env_path = Path(args.env_path).expanduser()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if env_path.exists() and not args.force:
        text = env_path.read_text(encoding='utf-8')
        if 'AUTH_INTERNAL_TOKEN=' in text:
            print(json.dumps({
                'ok': True,
                'changed': False,
                'path': str(env_path),
                'reason': 'already_present',
            }, ensure_ascii=False, indent=2))
            return 0
    token = str(args.token or '').strip() or secrets.token_urlsafe(32)
    env_path.write_text(f"export AUTH_INTERNAL_TOKEN='{token}'\n", encoding='utf-8')
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass
    print(json.dumps({
        'ok': True,
        'changed': True,
        'path': str(env_path),
        'mode': oct(env_path.stat().st_mode & 0o777),
        'token_preview': f"{token[:4]}...{token[-4:]}",
    }, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Manage MCN ops auth users and internal token bootstrap.')
    parser.add_argument('--db-path', default=os.getenv('DB_PATH', DEFAULT_DB_PATH))
    subparsers = parser.add_subparsers(dest='command', required=True)

    status_parser = subparsers.add_parser('status')
    status_parser.set_defaults(func=cmd_status)

    bootstrap_parser = subparsers.add_parser('bootstrap-admin')
    bootstrap_parser.add_argument('--username', required=True)
    bootstrap_parser.add_argument('--password', required=True)
    bootstrap_parser.add_argument('--display-name', default='')
    bootstrap_parser.set_defaults(func=cmd_bootstrap_admin)

    create_parser = subparsers.add_parser('create-user')
    create_parser.add_argument('--username', required=True)
    create_parser.add_argument('--password', required=True)
    create_parser.add_argument('--role', choices=['super_admin', 'admin', 'operator'], default='operator')
    create_parser.add_argument('--display-name', default='')
    create_parser.add_argument('--disabled', action='store_true')
    create_parser.set_defaults(func=cmd_create_user)

    update_parser = subparsers.add_parser('update-user')
    update_parser.add_argument('--user-id', required=True)
    update_parser.add_argument('--role', choices=['super_admin', 'admin', 'operator'])
    update_parser.add_argument('--display-name')
    update_parser.add_argument('--password')
    enabled_group = update_parser.add_mutually_exclusive_group()
    enabled_group.add_argument('--enabled', action='store_true')
    enabled_group.add_argument('--disabled', action='store_true')
    update_parser.set_defaults(func=cmd_update_user)

    token_parser = subparsers.add_parser('ensure-internal-token')
    token_parser.add_argument('--env-path', default=os.getenv('INTERNAL_AUTH_ENV', DEFAULT_INTERNAL_AUTH_ENV))
    token_parser.add_argument('--token')
    token_parser.add_argument('--force', action='store_true')
    token_parser.set_defaults(func=cmd_ensure_internal_token)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args) or 0)
    except ValueError as exc:
        parser.exit(1, json.dumps({'ok': False, 'detail': str(exc)}, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
