from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import websockets


class LiveChromeBindExecutor:
    def __init__(
        self,
        *,
        profile_map: dict[str, str],
        chrome_binary: Optional[str] = None,
        chrome_user_data_root: Optional[str] = None,
        startup_timeout_seconds: float = 20.0,
        post_submit_wait_seconds: float = 8.0,
    ) -> None:
        self.profile_map = {str(k): str(v) for k, v in (profile_map or {}).items()}
        self.chrome_binary = chrome_binary or '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
        self.chrome_user_data_root = str(Path(chrome_user_data_root or '~/Library/Application Support/Google/Chrome').expanduser())
        self.startup_timeout_seconds = max(5.0, float(startup_timeout_seconds or 20.0))
        self.post_submit_wait_seconds = max(3.0, float(post_submit_wait_seconds or 8.0))

    def __call__(self, context: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(self._run(context))

    async def _run(self, context: dict[str, Any]) -> dict[str, Any]:
        if str(context.get('bind_route') or '').strip().lower() == 'cms_id':
            return self._run_cms_id_bind(context)

        browser_profile_key = str(context.get('executor_browser_profile_key') or '').strip()
        profile_dir_name = self.profile_map.get(browser_profile_key)
        if not profile_dir_name:
            return {
                'status': 'failed',
                'result_code': 'bind_executor_profile_not_configured',
                'result_reason': f'No Chrome profile mapping configured for browser_profile_key={browser_profile_key}',
                'raw_result': {'browser_profile_key': browser_profile_key},
            }

        backend_url = str(context.get('executor_backend_url') or '').strip().rstrip('/')
        if backend_url == 'https://guild.linke.ai/guild':
            backend_url = backend_url + '/addAnchor'
        invite_code = str(context.get('invite_code') or '').strip().upper()
        if not backend_url or not invite_code:
            return {
                'status': 'failed',
                'result_code': 'bind_executor_missing_context',
                'result_reason': 'Missing backend_url or invite_code for live bind execution',
                'raw_result': {'backend_url': backend_url, 'invite_code': invite_code},
            }

        src_root = Path(self.chrome_user_data_root)
        src_profile_dir = src_root / profile_dir_name
        src_local_state = src_root / 'Local State'
        if not src_profile_dir.exists():
            return {
                'status': 'failed',
                'result_code': 'bind_executor_profile_missing',
                'result_reason': f'Chrome profile directory not found: {src_profile_dir}',
                'raw_result': {'profile_dir': str(src_profile_dir), 'browser_profile_key': browser_profile_key},
            }
        if not src_local_state.exists():
            return {
                'status': 'failed',
                'result_code': 'bind_executor_local_state_missing',
                'result_reason': f'Chrome Local State not found: {src_local_state}',
                'raw_result': {'local_state': str(src_local_state)},
            }

        port = self._pick_free_port()
        temp_root = Path(tempfile.mkdtemp(prefix='mcn-bind-chrome-'))
        proc: Optional[subprocess.Popen[Any]] = None
        try:
            shutil.copy2(src_local_state, temp_root / 'Local State')
            self._prepare_minimal_profile_copy(src_profile_dir=src_profile_dir, temp_root=temp_root, profile_dir_name=profile_dir_name)
            proc = self._launch_chrome(
                port=port,
                user_data_dir=temp_root,
                profile_dir_name=profile_dir_name,
                proxy_url=str(context.get('executor_proxy_url') or '').strip(),
            )
            await self._wait_debugger(port)
            ws_url = self._get_ws_url(port)
            async with websockets.connect(ws_url, max_size=2**24) as conn:
                client = _CdpClient(conn)
                await client.call('Page.enable')
                await client.call('Runtime.enable')
                await client.call('Network.enable')
                await client.call('Page.navigate', {'url': backend_url})
                await asyncio.sleep(4)
                await client.call('Runtime.evaluate', {
                    'expression': _XHR_WRAPPER_JS,
                    'returnByValue': True,
                })
                initial = await client.eval_value(_PAGE_SNAPSHOT_JS)
                guild_name = str((initial or {}).get('guild_name') or '')
                retained_before = str((initial or {}).get('invite_value') or '')
                submit = await client.eval_value(_submit_expression(invite_code))
                retained_after = str((submit or {}).get('invite_value') or '')
                reqs = []
                final_page = await client.eval_value(_PAGE_SNAPSHOT_JS) or {}
                deadline = time.time() + self.post_submit_wait_seconds
                while time.time() < deadline:
                    reqs = await client.eval_value('window.__mcnBindRequests || []') or []
                    final_page = await client.eval_value(_PAGE_SNAPSHOT_JS) or {}
                    if reqs:
                        break
                    page_url = str(final_page.get('url') or '')
                    page_body = str(final_page.get('body') or '')
                    if page_url.startswith('chrome-error://') or 'ERR_' in page_body or 'invalid person code' in page_body:
                        break
                    await asyncio.sleep(0.5)

            result = self._interpret_result(
                context=context,
                invite_code=invite_code,
                guild_name=guild_name,
                retained_before=retained_before,
                retained_after=retained_after,
                requests=reqs,
                final_page=final_page,
            )
            return result
        except Exception as exc:
            return {
                'status': 'failed',
                'result_code': 'bind_executor_runtime_error',
                'result_reason': str(exc),
                'raw_result': {'browser_profile_key': browser_profile_key, 'invite_code': invite_code},
            }
        finally:
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
            shutil.rmtree(temp_root, ignore_errors=True)

    def _run_cms_id_bind(self, context: dict[str, Any]) -> dict[str, Any]:
        account_id = str(context.get('account_id') or '').strip()
        target_guild = str(context.get('dept_name') or '').strip()
        base_url = str(context.get('executor_platform_backend_url') or '').strip().rstrip('/') or 'https://cms.linke.ai'
        authorization = str(context.get('executor_platform_authorization') or '').strip()
        raw_result: dict[str, Any] = {
            'executor_mode': 'cms_id',
            'guild_code': target_guild,
            'deptName': target_guild,
            'sid': account_id,
        }
        if not account_id.isdigit():
            return {
                'status': 'failed',
                'result_code': 'cms_bind_invalid_sid',
                'result_reason': 'Invalid Linky ID / SID for CMS bind',
                'raw_result': raw_result,
            }
        if not authorization:
            return {
                'status': 'failed',
                'result_code': 'cms_authorization_missing',
                'result_reason': 'CMS Authorization is not configured for this guild executor',
                'raw_result': raw_result,
            }
        try:
            guild = self._cms_find_target_guild(base_url=base_url, authorization=authorization, target_guild=target_guild)
            raw_result['cms_guild_id'] = guild.get('id')
            raw_result['cms_guild_name'] = guild.get('guild_name')
            raw_result['cms_guild_sid'] = guild.get('sid')
            before = self._cms_query_sid(base_url=base_url, authorization=authorization, sid=account_id)
            before_match = self._cms_match_target_guild(before, guild)
            if before_match == 'target':
                raw_result['precheck'] = 'already_in_target_guild'
                return {
                    'status': 'success',
                    'result_code': 'bind_success',
                    'result_reason': 'CMS verified SID already in target guild',
                    'raw_result': raw_result,
                }
            if before_match == 'other':
                raw_result['precheck'] = 'already_in_other_guild'
                raw_result['existing_guild_name'] = str((before[0] or {}).get('guild_name') or '') if before else ''
                raw_result['existing_guild_id'] = str((before[0] or {}).get('guild_id') or '') if before else ''
                return {
                    'status': 'failed',
                    'result_code': 'already_in_other_guild',
                    'result_reason': 'The streamer was in another agency',
                    'raw_result': raw_result,
                }
            submit = self._cms_add_anchor(base_url=base_url, authorization=authorization, sid=account_id, guild_id=str(guild.get('id') or ''))
            raw_result['cms_submit_code'] = submit.get('code')
            raw_result['cms_submit_success'] = submit.get('success')
            after = self._cms_query_sid(base_url=base_url, authorization=authorization, sid=account_id)
            after_match = self._cms_match_target_guild(after, guild)
            if after_match == 'target':
                raw_result['postcheck'] = 'verified_target_guild'
                return {
                    'status': 'success',
                    'result_code': 'bind_success',
                    'result_reason': 'CMS bind verified',
                    'raw_result': raw_result,
                }
            message = str(submit.get('message') or submit.get('msg') or submit.get('error') or 'CMS bind was not verified')
            return {
                'status': 'failed',
                'result_code': 'cms_bind_not_verified',
                'result_reason': message,
                'raw_result': raw_result,
            }
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                code = 'cms_authorization_invalid'
                reason = f'CMS authorization rejected with HTTP {exc.code}'
            else:
                code = 'cms_http_error'
                reason = f'CMS request failed with HTTP {exc.code}'
            return {'status': 'failed', 'result_code': code, 'result_reason': reason, 'raw_result': raw_result}
        except Exception as exc:
            return {
                'status': 'failed',
                'result_code': 'cms_bind_runtime_error',
                'result_reason': str(exc),
                'raw_result': raw_result,
            }

    def _cms_headers(self, authorization: str) -> dict[str, str]:
        return {
            'authorization': authorization,
            'content-type': 'application/json',
            'accept': 'application/json, text/plain, */*',
            'origin': 'https://cms.linke.ai',
            'referer': 'https://cms.linke.ai/',
        }

    def _cms_request_json(self, *, method: str, url: str, authorization: str, body: Optional[dict[str, Any]] = None) -> Any:
        data = None if body is None else json.dumps(body).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers=self._cms_headers(authorization), method=method)
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode('utf-8', errors='replace')
        return json.loads(text) if text else {}

    def _cms_find_target_guild(self, *, base_url: str, authorization: str, target_guild: str) -> dict[str, Any]:
        url = f'{base_url}/api/admin/linky/industrial/industrial/getGuildIdAndName'
        data = self._cms_request_json(method='GET', url=url, authorization=authorization)
        rows = data.get('data') if isinstance(data, dict) else data
        if not isinstance(rows, list):
            rows = []
        target_norm = target_guild.strip().lower()
        exact = [r for r in rows if isinstance(r, dict) and str(r.get('guild_name') or '').strip().lower() == target_norm]
        if exact:
            return dict(exact[0])
        contains = [r for r in rows if isinstance(r, dict) and target_norm and target_norm in str(r.get('guild_name') or '').strip().lower()]
        if contains:
            return dict(contains[0])
        raise RuntimeError(f'CMS target guild not visible for this authorization: {target_guild}')

    def _cms_query_sid(self, *, base_url: str, authorization: str, sid: str) -> list[dict[str, Any]]:
        url = f'{base_url}/api/admin/linky/industrial/streamer_detail/page'
        candidates = [
            {'page': 1, 'size': 10, 'sid': sid},
            {'page': 1, 'size': 10, 'user_id': sid},
        ]
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for body in candidates:
            data = self._cms_request_json(method='POST', url=url, authorization=authorization, body=body)
            rows: Any = []
            if isinstance(data, dict):
                rows = data.get('data') or data.get('records') or []
                if isinstance(rows, dict):
                    rows = rows.get('records') or rows.get('list') or rows.get('rows') or []
            if not isinstance(rows, list):
                rows = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_sid = str(row.get('sid') or row.get('user_id') or '')
                if row_sid and row_sid != sid:
                    continue
                key = json.dumps(row, sort_keys=True, ensure_ascii=False)
                if key not in seen:
                    seen.add(key)
                    merged.append(dict(row))
        return merged

    def _cms_match_target_guild(self, rows: list[dict[str, Any]], guild: dict[str, Any]) -> str:
        if not rows:
            return 'none'
        target_id = str(guild.get('id') or '').strip()
        target_name = str(guild.get('guild_name') or '').strip().lower()
        for row in rows:
            guild_id = str(row.get('guild_id') or row.get('industrial_id') or '').strip()
            guild_name = str(row.get('guild_name') or row.get('industrial_name') or '').strip().lower()
            if (target_id and guild_id == target_id) or (target_name and guild_name == target_name):
                return 'target'
        return 'other'

    def _cms_add_anchor(self, *, base_url: str, authorization: str, sid: str, guild_id: str) -> dict[str, Any]:
        if not guild_id:
            raise RuntimeError('CMS target guild_id is missing')
        url = f'{base_url}/api/admin/linky/industrial/streamer_detail/addAnchor'
        data = self._cms_request_json(method='POST', url=url, authorization=authorization, body={'sids': [int(sid)], 'guild_id': int(guild_id)})
        return data if isinstance(data, dict) else {'data': data}

    def _pick_free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('127.0.0.1', 0))
            return int(sock.getsockname()[1])

    def _prepare_minimal_profile_copy(self, *, src_profile_dir: Path, temp_root: Path, profile_dir_name: str) -> None:
        dst_profile_dir = temp_root / profile_dir_name
        dst_profile_dir.mkdir(parents=True, exist_ok=True)
        required_files = [
            'Cookies',
            'Cookies-journal',
            'Preferences',
            'Secure Preferences',
        ]
        for name in required_files:
            src = src_profile_dir / name
            if src.exists():
                shutil.copy2(src, dst_profile_dir / name)

    def _launch_chrome(self, *, port: int, user_data_dir: Path, profile_dir_name: str, proxy_url: str) -> subprocess.Popen[Any]:
        args = [
            self.chrome_binary,
            '--headless=new',
            f'--remote-debugging-port={port}',
            f'--user-data-dir={user_data_dir}',
            f'--profile-directory={profile_dir_name}',
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-background-networking',
            '--disable-breakpad',
            '--disable-component-update',
            '--disable-renderer-backgrounding',
            'about:blank',
        ]
        if proxy_url:
            args.append(f'--proxy-server={proxy_url}')
        return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def _wait_debugger(self, port: int) -> None:
        deadline = time.time() + self.startup_timeout_seconds
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=1)
                return
            except Exception:
                await asyncio.sleep(0.3)
        raise RuntimeError(f'Chrome remote debugger did not start on port {port}')

    def _get_ws_url(self, port: int) -> str:
        tabs = json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=5))
        if not tabs:
            raise RuntimeError('No Chrome tabs exposed by remote debugger')
        return str(tabs[0]['webSocketDebuggerUrl'])

    def _interpret_result(
        self,
        *,
        context: dict[str, Any],
        invite_code: str,
        guild_name: str,
        retained_before: str,
        retained_after: str,
        requests: list[dict[str, Any]],
        final_page: dict[str, Any],
    ) -> dict[str, Any]:
        request = requests[-1] if requests else None
        raw_result = {
            'guild_code': guild_name or str(context.get('dept_name') or ''),
            'deptName': guild_name or str(context.get('dept_name') or ''),
            'invite_code': invite_code,
            'invite_value_before_submit': retained_before,
            'invite_value_after_submit': retained_after,
            'page_title': str(final_page.get('title') or ''),
            'page_url': str(final_page.get('url') or ''),
            'page_body': str(final_page.get('body') or ''),
            'request_count': len(requests),
            'last_request': request or {},
        }
        if retained_after and retained_after != invite_code:
            return {
                'status': 'failed',
                'result_code': 'bind_frontend_input_rejected',
                'result_reason': f'Invite code retained by page as {retained_after}, expected {invite_code}',
                'raw_result': raw_result,
            }
        if request:
            status = int(request.get('status') or 0)
            body_text = str(request.get('body') or '')
            if status == 401:
                return {
                    'status': 'failed',
                    'result_code': 'bind_unauthorized',
                    'result_reason': body_text or 'Backend returned 401 please re-login',
                    'raw_result': raw_result,
                }
            if status >= 400:
                return {
                    'status': 'failed',
                    'result_code': 'bind_backend_http_error',
                    'result_reason': f'HTTP {status}: {body_text}',
                    'raw_result': raw_result,
                }
            parsed = self._try_parse_json(body_text)
            if isinstance(parsed, dict):
                error = parsed.get('error')
                if isinstance(error, dict):
                    return {
                        'status': 'failed',
                        'result_code': 'bind_backend_error',
                        'result_reason': str(error.get('message') or body_text or 'bind backend error'),
                        'raw_result': {**raw_result, 'response_json': parsed},
                    }
                code = parsed.get('code')
                if code not in (None, 0, '0'):
                    return {
                        'status': 'failed',
                        'result_code': 'bind_backend_error',
                        'result_reason': str(parsed.get('message') or parsed.get('msg') or body_text or f'bind backend code={code}'),
                        'raw_result': {**raw_result, 'response_json': parsed},
                    }
                return {
                    'status': 'success',
                    'result_code': 'bind_live_success',
                    'result_reason': str(parsed.get('message') or parsed.get('msg') or 'Live bind request accepted'),
                    'raw_result': {**raw_result, 'response_json': parsed},
                }
            return {
                'status': 'success',
                'result_code': 'bind_live_success',
                'result_reason': body_text or 'Live bind request accepted',
                'raw_result': raw_result,
            }
        if 'anchorManage' in str(final_page.get('url') or ''):
            return {
                'status': 'success',
                'result_code': 'bind_live_success',
                'result_reason': 'Page navigated to anchorManage after submit',
                'raw_result': raw_result,
            }
        return {
            'status': 'failed',
            'result_code': 'bind_no_backend_response',
            'result_reason': 'No backend bind response captured after submit',
            'raw_result': raw_result,
        }

    def _try_parse_json(self, text: str) -> Any:
        text = str(text or '').strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None


class _CdpClient:
    def __init__(self, conn: websockets.WebSocketClientProtocol):
        self.conn = conn
        self._msg_id = 0

    async def call(self, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        self._msg_id += 1
        msg_id = self._msg_id
        await self.conn.send(json.dumps({'id': msg_id, 'method': method, 'params': params or {}}))
        while True:
            raw = await self.conn.recv()
            message = json.loads(raw)
            if message.get('id') == msg_id:
                return message

    async def eval_value(self, expression: str) -> Any:
        response = await self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
        result = response.get('result', {}).get('result', {})
        return result.get('value')


_XHR_WRAPPER_JS = r"""
(() => {
  window.__mcnBindRequests = [];
  const XO = XMLHttpRequest.prototype.open;
  const XS = XMLHttpRequest.prototype.send;
  if (!window.__mcnBindWrapped) {
    XMLHttpRequest.prototype.open = function(method, url, ...rest) {
      this.__mcnMethod = method;
      this.__mcnUrl = url;
      return XO.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.send = function(body) {
      this.addEventListener('loadend', function() {
        window.__mcnBindRequests.push({
          method: this.__mcnMethod,
          url: this.__mcnUrl,
          status: this.status,
          body: this.responseText || ''
        });
      });
      return XS.call(this, body);
    };
    window.__mcnBindWrapped = true;
  }
  true;
})()
"""

_PAGE_SNAPSHOT_JS = r"""
(() => ({
  title: document.title || '',
  url: location.href || '',
  guild_name: document.querySelector('input[disabled]')?.value || '',
  invite_value: document.querySelector('input:not([disabled])')?.value || '',
  body: (document.body?.innerText || '').slice(0, 1000)
}))()
"""


def _submit_expression(invite_code: str) -> str:
    payload = json.dumps(invite_code)
    return rf"""
(() => {{
  const input = document.querySelector('input:not([disabled])');
  if (!input) return {{error: 'invite_input_missing'}};
  const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  nativeSetter.call(input, {payload});
  input.dispatchEvent(new Event('input', {{ bubbles: true }}));
  input.dispatchEvent(new Event('change', {{ bubbles: true }}));
  const btn = document.querySelector('button[type=submit]') || [...document.querySelectorAll('button')].find(b => (b.textContent || '').includes('Confirm Entry') || (b.textContent || '').includes('确定录入'));
  if (!btn) return {{error: 'submit_button_missing', invite_value: input.value}};
  btn.click();
  return {{invite_value: input.value, guild_name: document.querySelector('input[disabled]')?.value || ''}};
}})()
"""
