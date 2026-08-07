from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Callable, Dict, Mapping, Optional

from app.production_ops import should_suppress_lark_alert


Runner = Callable[..., subprocess.CompletedProcess]


class LarkCliReplyAdapter:
    """Reply adapter backed by lark-cli's generic OpenAPI command."""

    def __init__(
        self,
        *,
        cli_bin: str = 'lark-cli',
        as_identity: str = 'bot',
        timeout_seconds: float = 15.0,
        env: Optional[Mapping[str, str]] = None,
        runner: Optional[Runner] = None,
    ) -> None:
        self.cli_bin = str(cli_bin or 'lark-cli').strip() or 'lark-cli'
        normalized_identity = str(as_identity or 'bot').strip().lower()
        self.as_identity = normalized_identity if normalized_identity in {'bot', 'user', 'auto'} else 'bot'
        self.timeout_seconds = max(1.0, float(timeout_seconds or 15.0))
        self.env = dict(env or {})
        self._runner = runner or subprocess.run

    def with_env(self, env: Mapping[str, str]) -> 'LarkCliReplyAdapter':
        merged = dict(self.env)
        merged.update({str(k): str(v) for k, v in dict(env or {}).items()})
        return LarkCliReplyAdapter(
            cli_bin=self.cli_bin,
            as_identity=self.as_identity,
            timeout_seconds=self.timeout_seconds,
            env=merged,
            runner=self._runner,
        )

    @staticmethod
    def _normalize_text_markup(text: str) -> str:
        normalized = str(text or '')
        return re.sub(r'\*\*(.+?)\*\*', lambda m: f"<b>{m.group(1)}</b>", normalized)

    @staticmethod
    def _first_env(env: Mapping[str, str], *keys: str) -> str:
        for key in keys:
            value = str(env.get(key) or '').strip()
            if value:
                return value
        return ''

    def _build_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in self.env.items()})

        if not str(env.get('LARKSUITE_CLI_APP_ID') or '').strip():
            app_id = self._first_env(env, 'LARK_CLI_APP_ID', 'LARK_APP_ID', 'FEISHU_APP_ID')
            if app_id:
                env['LARKSUITE_CLI_APP_ID'] = app_id
        if not str(env.get('LARKSUITE_CLI_APP_SECRET') or '').strip():
            app_secret = self._first_env(env, 'LARK_CLI_APP_SECRET', 'LARK_APP_SECRET', 'FEISHU_APP_SECRET')
            if app_secret:
                env['LARKSUITE_CLI_APP_SECRET'] = app_secret
        if not str(env.get('LARKSUITE_CLI_USER_ACCESS_TOKEN') or '').strip():
            uat = self._first_env(env, 'LARK_CLI_USER_ACCESS_TOKEN', 'LARK_USER_ACCESS_TOKEN', 'FEISHU_USER_ACCESS_TOKEN')
            if uat:
                env['LARKSUITE_CLI_USER_ACCESS_TOKEN'] = uat
        if not str(env.get('LARKSUITE_CLI_TENANT_ACCESS_TOKEN') or '').strip():
            tat = self._first_env(env, 'LARK_CLI_TENANT_ACCESS_TOKEN', 'LARK_TENANT_ACCESS_TOKEN', 'FEISHU_TENANT_ACCESS_TOKEN')
            if tat:
                env['LARKSUITE_CLI_TENANT_ACCESS_TOKEN'] = tat
        if not str(env.get('LARKSUITE_CLI_BRAND') or '').strip():
            domain = self._first_env(env, 'LARK_CLI_BRAND', 'LARK_DOMAIN', 'FEISHU_DOMAIN').lower()
            env['LARKSUITE_CLI_BRAND'] = 'feishu' if domain == 'feishu' else 'lark'
        if not str(env.get('LARKSUITE_CLI_DEFAULT_AS') or '').strip():
            env['LARKSUITE_CLI_DEFAULT_AS'] = self.as_identity
        return env

    def _api(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None, data: Optional[Dict[str, Any]] = None) -> dict:
        args = [
            self.cli_bin,
            'api',
            method.upper(),
            path,
            '--as',
            self.as_identity,
            '--format',
            'json',
        ]
        if params is not None:
            args.extend(['--params', json.dumps(params, ensure_ascii=False, separators=(',', ':'))])
        if data is not None:
            args.extend(['--data', json.dumps(data, ensure_ascii=False, separators=(',', ':'))])

        proc = self._runner(
            args,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            env=self._build_env(),
        )
        stdout = str(getattr(proc, 'stdout', '') or '').strip()
        stderr = str(getattr(proc, 'stderr', '') or '').strip()
        if getattr(proc, 'returncode', 0) != 0:
            detail = (stderr or stdout or f'exit {proc.returncode}')[-500:]
            raise RuntimeError(f'lark_cli_failed:{proc.returncode}:{detail}')
        if not stdout:
            return {}
        try:
            body = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f'lark_cli_invalid_json:{stdout[:500]}') from exc
        if isinstance(body, dict) and body.get('code') not in (None, 0):
            raise RuntimeError(f'lark_cli_api_failed:{body}')
        return body if isinstance(body, dict) else {'data': body}

    def reply_text(self, *, message_id: str, text: str) -> dict:
        normalized_text = self._normalize_text_markup(text)
        return self._api(
            'POST',
            f'/open-apis/im/v1/messages/{message_id}/reply',
            data={'msg_type': 'text', 'content': json.dumps({'text': normalized_text}, ensure_ascii=False)},
        )

    def send_text(self, *, chat_id: str, text: str) -> dict:
        if should_suppress_lark_alert(message_text=text):
            return {
                'code': 0,
                'suppressed': True,
                'suppressed_reason': 'invalid_registration_group_invite_404',
            }
        normalized_text = self._normalize_text_markup(text)
        return self._api(
            'POST',
            '/open-apis/im/v1/messages',
            params={'receive_id_type': 'chat_id'},
            data={
                'receive_id': chat_id,
                'msg_type': 'text',
                'content': json.dumps({'text': normalized_text}, ensure_ascii=False),
            },
        )
