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


class CmsBindFlowError(RuntimeError):
    def __init__(self, result_code: str, result_reason: str) -> None:
        super().__init__(result_reason)
        self.result_code = result_code
        self.result_reason = result_reason


class LiveChromeBindExecutor:
    def __init__(
        self,
        *,
        profile_map: dict[str, str],
        chrome_binary: Optional[str] = None,
        chrome_user_data_root: Optional[str] = None,
        startup_timeout_seconds: float = 20.0,
        post_submit_wait_seconds: float = 8.0,
        cms_postcheck_max_attempts: int = 3,
        cms_postcheck_retry_delay_seconds: float = 0.75,
    ) -> None:
        self.profile_map = {str(k): str(v) for k, v in (profile_map or {}).items()}
        self.chrome_binary = chrome_binary or '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
        self.chrome_user_data_root = str(Path(chrome_user_data_root or '~/Library/Application Support/Google/Chrome').expanduser())
        self.startup_timeout_seconds = max(5.0, float(startup_timeout_seconds or 20.0))
        self.post_submit_wait_seconds = max(3.0, float(post_submit_wait_seconds or 8.0))
        self.cms_postcheck_max_attempts = max(1, int(cms_postcheck_max_attempts or 3))
        self.cms_postcheck_retry_delay_seconds = max(0.0, float(cms_postcheck_retry_delay_seconds or 0.0))

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
            import websockets
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
        proxy_url = str(context.get('executor_proxy_url') or '').strip()
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
        configured_guild_id = str(context.get('executor_cms_guild_id') or '').strip()
        configured_guild_sid = str(context.get('executor_cms_guild_sid') or '').strip()
        if not configured_guild_id or not configured_guild_sid:
            default_locks = {
                'carote': ('3432', '43536425'),
                'permata': ('413', '25400979'),
                'nova': ('1423', '31350499'),
            }
            default_lock = default_locks.get(target_guild.strip().lower())
            if default_lock:
                configured_guild_id = configured_guild_id or default_lock[0]
                configured_guild_sid = configured_guild_sid or default_lock[1]
                raw_result['cms_guild_lock_source'] = 'builtin_default'
            else:
                return {
                    'status': 'failed',
                    'result_code': 'cms_target_guild_lock_missing',
                    'result_reason': 'CMS guild ID/SID lock is required for automatic CMS bind',
                    'raw_result': raw_result,
                }
        try:
            request_timeout_seconds = min(8.0, max(2.0, float(context.get('executor_request_timeout_seconds') or 8.0)))
            guild = self._cms_find_target_guild(
                base_url=base_url,
                authorization=authorization,
                proxy_url=proxy_url,
                target_guild=target_guild,
                configured_guild_id=configured_guild_id,
                configured_guild_sid=configured_guild_sid,
                timeout_seconds=request_timeout_seconds,
            )
            raw_result['cms_guild_id'] = guild.get('id')
            raw_result['cms_guild_name'] = guild.get('guild_name')
            raw_result['cms_guild_sid'] = guild.get('sid')
            before = self._cms_query_sid(base_url=base_url, authorization=authorization, proxy_url=proxy_url, sid=account_id, timeout_seconds=request_timeout_seconds)
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
            if not before:
                raw_result['precheck'] = 'sid_not_found'
                return {
                    'status': 'failed',
                    'result_code': 'cms_sid_not_found',
                    'result_reason': 'SID not found or not available as anchor',
                    'raw_result': raw_result,
                }
            raw_result['precheck'] = 'sid_found_without_guild'
            submit = self._cms_add_anchor(base_url=base_url, authorization=authorization, proxy_url=proxy_url, sid=account_id, guild_id=str(guild.get('id') or ''), timeout_seconds=request_timeout_seconds)
            raw_result['cms_submit_code'] = submit.get('code')
            raw_result['cms_submit_success'] = submit.get('success')
            submit_message = str(submit.get('message') or submit.get('msg') or submit.get('error') or '').strip()
            after: list[dict[str, Any]] = []
            after_match = 'none'
            for attempt in range(1, self.cms_postcheck_max_attempts + 1):
                after = self._cms_query_sid(base_url=base_url, authorization=authorization, proxy_url=proxy_url, sid=account_id, timeout_seconds=request_timeout_seconds)
                after_match = self._cms_match_target_guild(after, guild)
                raw_result['postcheck_attempts'] = attempt
                if after_match == 'target':
                    raw_result['postcheck'] = 'verified_target_guild'
                    return {
                        'status': 'success',
                        'result_code': 'bind_success',
                        'result_reason': 'CMS bind verified',
                        'raw_result': raw_result,
                    }
                if after_match == 'other':
                    raw_result['postcheck'] = 'mismatched_other_guild'
                    break
                if attempt < self.cms_postcheck_max_attempts and self.cms_postcheck_retry_delay_seconds:
                    time.sleep(self.cms_postcheck_retry_delay_seconds)
            message = submit_message or 'CMS bind was not verified'
            lowered_message = message.lower()
            if after_match == 'none':
                raw_result['postcheck'] = 'sid_not_found_or_not_anchor'
                if 'invalid arguments' in lowered_message or submit.get('code') == 1001:
                    result_code = 'cms_add_anchor_invalid_arguments'
                    message = message or 'Invalid or unavailable Linky ID'
                elif submit.get('code') in (1000, '1000') or submit.get('success') is True:
                    result_code = 'cms_postcheck_timeout'
                    message = 'CMS bind submitted but postcheck did not verify target guild'
                else:
                    result_code = 'cms_sid_not_found'
                    message = message or 'SID not found or not available as anchor'
            elif after_match == 'other':
                result_code = 'cms_postcheck_mismatch'
                message = message or 'CMS postcheck found another guild after bind submit'
            else:
                result_code = 'cms_bind_not_verified'
            return {
                'status': 'failed',
                'result_code': result_code,
                'result_reason': message,
                'raw_result': raw_result,
            }
        except CmsBindFlowError as exc:
            return {'status': 'failed', 'result_code': exc.result_code, 'result_reason': exc.result_reason, 'raw_result': raw_result}
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

    def _cms_request_json(self, *, method: str, url: str, authorization: str, body: Optional[dict[str, Any]] = None, proxy_url: str = '', timeout_seconds: float = 8.0) -> Any:
        data = None if body is None else json.dumps(body).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers=self._cms_headers(authorization), method=method)
        opener = urllib.request.build_opener()
        normalized_proxy = str(proxy_url or '').strip()
        if normalized_proxy:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': normalized_proxy, 'https': normalized_proxy}))
        with opener.open(req, timeout=max(2.0, min(float(timeout_seconds or 8.0), 8.0))) as resp:
            text = resp.read().decode('utf-8', errors='replace')
        return json.loads(text) if text else {}

    def _cms_find_target_guild(
        self,
        *,
        base_url: str,
        authorization: str,
        target_guild: str,
        proxy_url: str = '',
        configured_guild_id: str = '',
        configured_guild_sid: str = '',
        timeout_seconds: float = 8.0,
    ) -> dict[str, Any]:
        url = f'{base_url}/api/admin/linky/industrial/industrial/getGuildIdAndName'
        data = self._cms_request_json(method='GET', url=url, authorization=authorization, proxy_url=proxy_url, timeout_seconds=timeout_seconds)
        rows = data.get('data') if isinstance(data, dict) else data
        if not isinstance(rows, list):
            rows = []
        dict_rows = [r for r in rows if isinstance(r, dict)]
        target_norm = target_guild.strip().lower()
        configured_id = str(configured_guild_id or '').strip()
        configured_sid = str(configured_guild_sid or '').strip()
        if configured_id or configured_sid:
            configured_matches = []
            for row in dict_rows:
                row_id = str(row.get('id') or row.get('guild_id') or '').strip()
                row_sid = str(row.get('sid') or row.get('guild_sid') or '').strip()
                id_ok = not configured_id or row_id == configured_id
                sid_ok = not configured_sid or row_sid == configured_sid
                if id_ok and sid_ok:
                    configured_matches.append(row)
            if len(configured_matches) == 1:
                matched = dict(configured_matches[0])
                matched_name = str(matched.get('guild_name') or '').strip().lower()
                if target_norm and matched_name and matched_name != target_norm:
                    raise CmsBindFlowError('cms_target_guild_mismatch', f'Configured CMS guild does not match target guild: {target_guild}')
                return matched
            if len(configured_matches) > 1:
                raise CmsBindFlowError('cms_target_guild_ambiguous', f'Configured CMS guild is ambiguous: {target_guild}')
            raise CmsBindFlowError('cms_target_guild_not_visible', f'Configured CMS guild is not visible for this authorization: {target_guild}')
        exact = [r for r in dict_rows if str(r.get('guild_name') or '').strip().lower() == target_norm]
        if len(exact) == 1:
            return dict(exact[0])
        if len(exact) > 1:
            raise CmsBindFlowError('cms_target_guild_ambiguous', f'CMS target guild is ambiguous: {target_guild}')
        contains = [r for r in dict_rows if target_norm and target_norm in str(r.get('guild_name') or '').strip().lower()]
        if contains:
            raise CmsBindFlowError('cms_target_guild_ambiguous', f'CMS target guild requires exact match: {target_guild}')
        raise CmsBindFlowError('cms_target_guild_not_visible', f'CMS target guild not visible for this authorization: {target_guild}')

    def _cms_extract_rows_from_detail_response(self, data: Any) -> list[dict[str, Any]]:
        if isinstance(data, dict):
            code = data.get('code')
            success = data.get('success')
            if code is not None and str(code) != '1000':
                message = str(data.get('message') or data.get('msg') or data.get('error') or 'CMS detail query did not return success')
                raise CmsBindFlowError('cms_precheck_untrusted', message)
            if success is False:
                message = str(data.get('message') or data.get('msg') or data.get('error') or 'CMS detail query was not successful')
                raise CmsBindFlowError('cms_precheck_untrusted', message)
            rows: Any = data.get('data') or data.get('records') or []
            if isinstance(rows, dict):
                rows = rows.get('records') or rows.get('list') or rows.get('rows') or []
        else:
            rows = data
        if rows in (None, ''):
            return []
        if not isinstance(rows, list):
            raise CmsBindFlowError('cms_precheck_untrusted', 'CMS detail query returned an unsupported data shape')
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise CmsBindFlowError('cms_precheck_untrusted', 'CMS detail query returned an unsupported row shape')
            normalized.append(dict(row))
        return normalized

    def _cms_query_sid(self, *, base_url: str, authorization: str, proxy_url: str = '', sid: str, timeout_seconds: float = 8.0) -> list[dict[str, Any]]:
        url = f'{base_url}/api/admin/linky/industrial/streamer_detail/page'
        candidates = [
            {'page': 1, 'size': 10, 'sid': sid},
            {'page': 1, 'size': 10, 'user_id': sid},
        ]
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        saw_unmatched_rows = False
        for body in candidates:
            data = self._cms_request_json(method='POST', url=url, authorization=authorization, body=body, proxy_url=proxy_url, timeout_seconds=timeout_seconds)
            rows = self._cms_extract_rows_from_detail_response(data)
            for row in rows:
                row_sid = str(row.get('sid') or row.get('user_id') or '').strip()
                if not row_sid:
                    raise CmsBindFlowError('cms_precheck_untrusted', 'CMS detail row has no verifiable SID')
                if row_sid != sid:
                    saw_unmatched_rows = True
                    continue
                key = json.dumps(row, sort_keys=True, ensure_ascii=False)
                if key not in seen:
                    seen.add(key)
                    merged.append(dict(row))
            if merged:
                break
        if not merged and saw_unmatched_rows:
            raise CmsBindFlowError('cms_precheck_untrusted', 'CMS detail query returned rows for a different SID')
        return merged

    def _cms_match_target_guild(self, rows: list[dict[str, Any]], guild: dict[str, Any]) -> str:
        if not rows:
            return 'none'
        target_id = str(guild.get('id') or '').strip()
        target_name = str(guild.get('guild_name') or '').strip().lower()
        found_known_other = False
        def meaningful(value: Any) -> str:
            normalized = str(value or '').strip()
            return '' if normalized.lower() in {'', '0', '0.0', 'none', 'null', 'undefined'} else normalized
        for row in rows:
            join_record = row.get('joinRecord') if isinstance(row.get('joinRecord'), dict) else {}
            primary_guild_id = meaningful(row.get('guild_id') or row.get('industrial_id'))
            primary_guild_name = meaningful(row.get('guild_name') or row.get('industrial_name')).lower()
            join_guild_id = meaningful(join_record.get('guild_id'))
            join_guild_name = meaningful(join_record.get('guild_name')).lower()
            guild_id = join_guild_id or primary_guild_id
            guild_name = join_guild_name or primary_guild_name
            if (target_id and guild_id == target_id) or (target_name and guild_name == target_name):
                return 'target'
            if guild_id or guild_name:
                found_known_other = True
        return 'other' if found_known_other else 'none'

    def _cms_add_anchor(self, *, base_url: str, authorization: str, proxy_url: str = '', sid: str, guild_id: str, timeout_seconds: float = 8.0) -> dict[str, Any]:
        if not guild_id:
            raise RuntimeError('CMS target guild_id is missing')
        url = f'{base_url}/api/admin/linky/industrial/streamer_detail/addAnchor'
        data = self._cms_request_json(method='POST', url=url, authorization=authorization, body={'sids': [int(sid)], 'guild_id': int(guild_id)}, proxy_url=proxy_url, timeout_seconds=timeout_seconds)
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
