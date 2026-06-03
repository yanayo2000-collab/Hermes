from __future__ import annotations

import asyncio
import http.client
import io
import json
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any, Optional


class CmsBindFlowError(RuntimeError):
    def __init__(self, result_code: str, result_reason: str) -> None:
        super().__init__(result_reason)
        self.result_code = result_code
        self.result_reason = result_reason


class CmsRequestTimeoutError(TimeoutError):
    def __init__(self, message: str, *, timeout_stage: str, request_trace: dict[str, Any]) -> None:
        super().__init__(message)
        self.timeout_stage = str(timeout_stage or '').strip()
        self.request_trace = dict(request_trace or {})


class _TimedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *, endpoint_host: str, endpoint_port: int, timeout: float, trace: dict[str, Any]) -> None:
        super().__init__(endpoint_host, endpoint_port, timeout=timeout)
        self._trace = trace

    def connect(self) -> None:
        _connect_socket_with_trace(self, endpoint_host=self.host, endpoint_port=int(self.port or 0), trace=self._trace, timeout=float(self.timeout or 0.0))


class _TimedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        *,
        target_host: str,
        target_port: int,
        timeout: float,
        trace: dict[str, Any],
        proxy_url: str = '',
    ) -> None:
        normalized_proxy = str(proxy_url or '').strip()
        self._trace = trace
        self._timed_target_host = target_host
        self._timed_target_port = int(target_port or 443)
        self._timed_proxy_url = normalized_proxy
        self._context = ssl.create_default_context()
        if normalized_proxy:
            parsed_proxy = urllib.parse.urlsplit(normalized_proxy)
            proxy_host = parsed_proxy.hostname or ''
            proxy_port = int(parsed_proxy.port or (443 if parsed_proxy.scheme == 'https' else 80))
            super().__init__(proxy_host, proxy_port, timeout=timeout, context=self._context)
            self.set_tunnel(target_host, self._timed_target_port)
            self._trace['proxy_url'] = normalized_proxy
            self._trace['proxy_host'] = proxy_host
            self._trace['proxy_port'] = proxy_port
        else:
            super().__init__(target_host, self._timed_target_port, timeout=timeout, context=self._context)

    def connect(self) -> None:
        _connect_socket_with_trace(self, endpoint_host=self.host, endpoint_port=int(self.port or 0), trace=self._trace, timeout=float(self.timeout or 0.0))
        tunnel_host = getattr(self, '_tunnel_host', None)
        tunnel_port = getattr(self, '_tunnel_port', None)
        if tunnel_host:
            tunnel_started = time.monotonic()
            try:
                self._tunnel()  # type: ignore[attr-defined]
            except socket.timeout:
                self._trace['timeout_stage'] = 'proxy_tunnel'
                raise
            finally:
                self._trace['proxy_tunnel_duration_ms'] = round((time.monotonic() - tunnel_started) * 1000.0, 3)
            self._trace['tunnel_target_host'] = str(tunnel_host)
            self._trace['tunnel_target_port'] = int(tunnel_port or 0)
        handshake_started = time.monotonic()
        try:
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self._timed_target_host)
        except socket.timeout:
            self._trace['timeout_stage'] = 'tls_handshake'
            raise
        finally:
            self._trace['tls_handshake_duration_ms'] = round((time.monotonic() - handshake_started) * 1000.0, 3)
        peer = None
        try:
            peer = self.sock.getpeername()
        except Exception:
            peer = None
        if isinstance(peer, tuple) and peer:
            self._trace['remote_ip'] = str(peer[0])
            if len(peer) > 1:
                self._trace['remote_port'] = int(peer[1])


def _connect_socket_with_trace(conn: http.client.HTTPConnection, *, endpoint_host: str, endpoint_port: int, trace: dict[str, Any], timeout: float) -> None:
    dns_started = time.monotonic()
    try:
        addrinfos = socket.getaddrinfo(endpoint_host, endpoint_port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        trace['dns_duration_ms'] = round((time.monotonic() - dns_started) * 1000.0, 3)
        trace['dns_error'] = str(exc)
        trace['timeout_stage'] = 'dns' if 'timed out' in str(exc).lower() else ''
        raise
    trace['dns_duration_ms'] = round((time.monotonic() - dns_started) * 1000.0, 3)
    trace['dns_results'] = [str(item[4][0]) for item in addrinfos if isinstance(item, tuple) and len(item) >= 5 and isinstance(item[4], tuple) and item[4]]
    last_exc: Exception | None = None
    for family, socktype, proto, _, sockaddr in addrinfos:
        sock = socket.socket(family, socktype, proto)
        connect_started = time.monotonic()
        try:
            sock.settimeout(timeout)
            sock.connect(sockaddr)
            trace['tcp_connect_duration_ms'] = round((time.monotonic() - connect_started) * 1000.0, 3)
            trace['remote_ip'] = str(sockaddr[0])
            if len(sockaddr) > 1:
                trace['remote_port'] = int(sockaddr[1])
            conn.sock = sock
            return
        except socket.timeout as exc:
            trace['tcp_connect_duration_ms'] = round((time.monotonic() - connect_started) * 1000.0, 3)
            trace['timeout_stage'] = 'connect'
            sock.close()
            last_exc = exc
            break
        except OSError as exc:
            trace['tcp_connect_duration_ms'] = round((time.monotonic() - connect_started) * 1000.0, 3)
            trace.setdefault('connect_errors', []).append(str(exc))
            sock.close()
            last_exc = exc
            continue
    if last_exc:
        raise last_exc
    raise OSError(f'Unable to connect to {endpoint_host}:{endpoint_port}')


def _strip_html_tags(text: str) -> str:
    cleaned = re.sub(r'(?is)<(script|style)[^>]*>.*?</\\1>', ' ', str(text or ''))
    cleaned = re.sub(r'(?is)<!--.*?-->', ' ', cleaned)
    cleaned = re.sub(r'(?is)<[^>]+>', ' ', cleaned)
    return re.sub(r'\\s+', ' ', cleaned).strip()


def normalize_bind_upstream_error(status: int, body_text: str) -> str:
    status = int(status or 0)
    body = str(body_text or '')
    lowered = body.lower()
    title_match = re.search(r'(?is)<title[^>]*>(.*?)</title>', body)
    title = _strip_html_tags(title_match.group(1)) if title_match else ''
    plain = _strip_html_tags(body)
    is_html = '<html' in lowered or '<body' in lowered or '<!doctype' in lowered or bool(title_match)
    if is_html:
        if status == 404 or '404 not found' in lowered or '404 not found' in plain.lower():
            return 'Binding upstream returned HTTP 404 Not Found; check executor URL or nginx route.'
        if status in (401, 403):
            return f'Binding upstream returned HTTP {status}; backend session or authorization requires manual recovery.'
        if status >= 500:
            return f'Binding upstream returned HTTP {status}; upstream service is temporarily unavailable.'
        if status >= 400:
            suffix = f' {title}' if title else ''
            return f'Binding upstream returned HTTP {status}{suffix}; check executor route.'
        return plain[:300] or 'Binding upstream returned an HTML response instead of JSON.'
    if status >= 400:
        return f'HTTP {status}: {plain or body[:300]}'
    return plain or body[:300]


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
        self._cms_request_traces: list[dict[str, Any]] = []

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
        authorization = str(context.get('_cms_retry_authorization') or context.get('executor_platform_authorization') or '').strip()
        proxy_url = str(context.get('executor_proxy_url') or '').strip()
        refresh_attempted = bool(context.get('_cms_retry_refresh_attempted'))
        refresh_meta = context.get('_cms_refresh_result') if isinstance(context.get('_cms_refresh_result'), dict) else {}
        self._cms_request_traces = []
        raw_result: dict[str, Any] = {
            'executor_mode': 'cms_id',
            'guild_code': target_guild,
            'deptName': target_guild,
            'sid': account_id,
            'cms_base_url': base_url,
            'cms_proxy_configured': bool(proxy_url),
        }
        if refresh_attempted:
            raw_result['cms_refresh_retry'] = dict(refresh_meta)
        try:
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
            request_timeout_seconds = min(30.0, max(10.0, float(context.get('executor_request_timeout_seconds') or 20.0)))
            raw_result['cms_request_timeout_seconds'] = request_timeout_seconds
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
            raw_result['cms_bind_flow'] = 'ka_addanchor_only'
            try:
                submit = self._cms_add_anchor(base_url=base_url, authorization=authorization, proxy_url=proxy_url, sid=account_id, guild_id=str(guild.get('id') or ''), timeout_seconds=request_timeout_seconds)
            except urllib.error.HTTPError as exc:
                raw_result['cms_submit_http_status'] = exc.code
                raise
            raw_result['cms_submit_code'] = submit.get('code')
            raw_result['cms_submit_success'] = submit.get('success')
            submit_result = self._classify_cms_add_anchor_response(submit)
            raw_result['cms_submit_error_category'] = submit_result.get('category')
            raw_result['cms_submit_success_count'] = submit_result.get('success_count')
            raw_result['cms_submit_fail_count'] = submit_result.get('fail_count')
            raw_result['cms_submit_fail_items'] = submit_result.get('fail_items')
            category = str(submit_result.get('category') or '').strip()
            if category == 'submitted':
                raw_result['postcheck'] = 'ka_addanchor_success_count'
                return {
                    'status': 'success',
                    'result_code': 'bind_success',
                    'result_reason': 'CMS KA-AddAnchor accepted',
                    'raw_result': raw_result,
                }
            return {
                'status': 'failed',
                'result_code': submit_result.get('result_code') or 'cms_add_anchor_unexpected_error',
                'result_reason': submit_result.get('result_reason') or 'CMS KA-AddAnchor returned an unexpected response',
                'raw_result': raw_result,
            }
        except CmsBindFlowError as exc:
            return {'status': 'failed', 'result_code': exc.result_code, 'result_reason': exc.result_reason, 'raw_result': raw_result}
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and not refresh_attempted:
                refresh_token = str(context.get('executor_cms_refresh_token') or '').strip()
                if refresh_token:
                    try:
                        refresh_result = self._cms_refresh_authorization(
                            base_url=base_url,
                            current_authorization=authorization,
                            refresh_token=refresh_token,
                            refresh_token_deadtime=context.get('executor_cms_refresh_token_deadtime'),
                            proxy_url=proxy_url,
                            timeout_seconds=min(30.0, max(10.0, float(raw_result.get('cms_request_timeout_seconds') or context.get('executor_request_timeout_seconds') or 20.0))),
                        )
                        persist_callback = context.get('executor_refresh_persist_callback')
                        if callable(persist_callback):
                            persist_callback(dict(refresh_result))
                        retry_context = dict(context)
                        retry_context['_cms_retry_authorization'] = str(refresh_result.get('authorization') or '').strip()
                        retry_context['_cms_retry_refresh_attempted'] = True
                        retry_context['_cms_refresh_result'] = dict(refresh_result)
                        return self._run_cms_id_bind(retry_context)
                    except Exception as refresh_exc:
                        refresh_failure = {
                            'attempted': True,
                            'ok': False,
                            'error': str(refresh_exc),
                        }
                        raw_result['cms_refresh_retry'] = dict(refresh_failure)
                        persist_callback = context.get('executor_refresh_persist_callback')
                        if callable(persist_callback):
                            try:
                                persist_callback(dict(refresh_failure))
                            except Exception:
                                pass
            if exc.code in (401, 403):
                if raw_result.get('cms_submit_http_status') == 403:
                    code = 'cms_authorization_scope_denied'
                    reason = 'CMS KA-AddAnchor authorization lacks required scope (HTTP 403)'
                else:
                    code = 'cms_authorization_invalid'
                    reason = f'CMS authorization rejected with HTTP {exc.code}'
            else:
                code = 'cms_http_error'
                reason = f'CMS request failed with HTTP {exc.code}'
            return {'status': 'failed', 'result_code': code, 'result_reason': reason, 'raw_result': raw_result}
        except Exception as exc:
            if isinstance(exc, CmsRequestTimeoutError):
                raw_result['cms_timeout_stage'] = exc.timeout_stage
            return {
                'status': 'failed',
                'result_code': 'cms_bind_runtime_error',
                'result_reason': str(exc),
                'raw_result': raw_result,
            }
        finally:
            if self._cms_request_traces:
                raw_result['cms_request_traces'] = [dict(item) for item in self._cms_request_traces]

    def _cms_headers(self, authorization: str) -> dict[str, str]:
        return {
            'authorization': authorization,
            'content-type': 'application/json',
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'zh-CN,zh;q=0.9',
            'origin': 'https://cms.linke.ai',
            'referer': 'https://cms.linke.ai/KA-AddAnchor',
            'cookie': 'locale=zh-cn',
            'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
        }

    @staticmethod
    def _coerce_epoch_seconds(value: Any) -> int | None:
        try:
            if value in (None, ''):
                return None
            number = int(value)
            if number > 10**12:
                return int(number / 1000)
            return number
        except Exception:
            return None

    @staticmethod
    def _normalize_authorization_value(*, current_authorization: str, refreshed_token: str) -> str:
        refreshed = str(refreshed_token or '').strip()
        current = str(current_authorization or '').strip()
        if not refreshed:
            return ''
        if refreshed.lower().startswith('bearer '):
            return refreshed
        if current.lower().startswith('bearer '):
            return f'Bearer {refreshed}'
        return refreshed

    def _record_cms_request_trace(self, trace: dict[str, Any]) -> None:
        self._cms_request_traces.append(dict(trace or {}))

    def _cms_refresh_authorization(
        self,
        *,
        base_url: str,
        current_authorization: str,
        refresh_token: str,
        refresh_token_deadtime: Any,
        proxy_url: str = '',
        timeout_seconds: float = 20.0,
    ) -> dict[str, Any]:
        normalized_refresh_token = str(refresh_token or '').strip()
        if not normalized_refresh_token:
            raise RuntimeError('CMS refresh token is not configured')
        refresh_deadtime = self._coerce_epoch_seconds(refresh_token_deadtime)
        query = urllib.parse.urlencode({
            'refreshToken': normalized_refresh_token,
            'refreshToken_deadtime': refresh_deadtime if refresh_deadtime is not None else '',
        })
        url = f'{base_url}/admin/base/open/refreshToken?{query}'
        payload = self._cms_request_json(
            method='GET',
            url=url,
            authorization=current_authorization,
            proxy_url=proxy_url,
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(payload, dict):
            raise RuntimeError('CMS refreshToken returned an unsupported response shape')
        token = str(payload.get('token') or payload.get('accessToken') or payload.get('authorization') or '').strip()
        authorization = self._normalize_authorization_value(current_authorization=current_authorization, refreshed_token=token)
        if not authorization:
            raise RuntimeError('CMS refreshToken returned an empty access token')
        refresh_value = str(payload.get('refreshToken') or normalized_refresh_token).strip()
        refresh_expire = self._coerce_epoch_seconds(payload.get('refreshExpire') or payload.get('refreshToken_deadtime'))
        access_expire = self._coerce_epoch_seconds(payload.get('expire') or payload.get('access_token_exp') or payload.get('tokenExpire'))
        return {
            'attempted': True,
            'ok': True,
            'authorization': authorization,
            'refresh_token': refresh_value,
            'refresh_token_deadtime': refresh_expire,
            'access_token_exp': access_expire,
        }

    def _make_cms_connection(self, *, parsed_url: urllib.parse.SplitResult, timeout: float, trace: dict[str, Any], proxy_url: str = '') -> http.client.HTTPConnection:
        scheme = (parsed_url.scheme or 'https').lower()
        host = parsed_url.hostname or ''
        if not host:
            raise RuntimeError(f'CMS request host is missing for url={parsed_url.geturl()}')
        port = int(parsed_url.port or (443 if scheme == 'https' else 80))
        if scheme == 'https':
            return _TimedHTTPSConnection(target_host=host, target_port=port, timeout=timeout, trace=trace, proxy_url=proxy_url)
        return _TimedHTTPConnection(endpoint_host=host, endpoint_port=port, timeout=timeout, trace=trace)

    def _cms_request_json(self, *, method: str, url: str, authorization: str, body: Optional[dict[str, Any]] = None, proxy_url: str = '', timeout_seconds: float = 20.0) -> Any:
        data = None if body is None else json.dumps(body, separators=(',', ':')).encode('utf-8')
        timeout = max(10.0, min(float(timeout_seconds or 20.0), 30.0))
        parsed_url = urllib.parse.urlsplit(url)
        path = parsed_url.path or '/'
        if parsed_url.query:
            path = f'{path}?{parsed_url.query}'
        last_exc: Exception | None = None
        last_trace: dict[str, Any] | None = None
        for attempt in range(1, 4):
            trace: dict[str, Any] = {
                'attempt': attempt,
                'method': method,
                'url': url,
                'host': parsed_url.hostname or '',
                'path': path,
                'proxy_url': str(proxy_url or '').strip(),
                'timeout_seconds': timeout,
            }
            conn: http.client.HTTPConnection | None = None
            total_started = time.monotonic()
            try:
                conn = self._make_cms_connection(parsed_url=parsed_url, timeout=timeout, trace=trace, proxy_url=proxy_url)
                request_started = time.monotonic()
                conn.request(method=method, url=path, body=data, headers=self._cms_headers(authorization))
                trace['request_write_duration_ms'] = round((time.monotonic() - request_started) * 1000.0, 3)
                first_byte_started = time.monotonic()
                response = conn.getresponse()
                trace['first_byte_duration_ms'] = round((time.monotonic() - first_byte_started) * 1000.0, 3)
                trace['http_status'] = int(response.status or 0)
                trace['response_reason'] = str(response.reason or '')
                body_started = time.monotonic()
                body_bytes = response.read()
                trace['body_read_duration_ms'] = round((time.monotonic() - body_started) * 1000.0, 3)
                trace['response_bytes'] = len(body_bytes or b'')
                trace['completed'] = True
                text = body_bytes.decode('utf-8', errors='replace')
                if int(response.status or 0) >= 400:
                    raise urllib.error.HTTPError(url, int(response.status or 0), str(response.reason or ''), response.headers, io.BytesIO(body_bytes))
                trace['total_duration_ms'] = round((time.monotonic() - total_started) * 1000.0, 3)
                last_trace = dict(trace)
                self._record_cms_request_trace(trace)
                return json.loads(text) if text else {}
            except urllib.error.HTTPError as exc:
                trace['http_status'] = int(exc.code or 0)
                trace['response_reason'] = str(exc.reason or '')
                trace['error_type'] = type(exc).__name__
                trace['error'] = str(exc)
                trace['total_duration_ms'] = round((time.monotonic() - total_started) * 1000.0, 3)
                last_trace = dict(trace)
                self._record_cms_request_trace(trace)
                raise
            except TimeoutError as exc:
                last_exc = exc
                trace['error_type'] = type(exc).__name__
                trace['error'] = str(exc)
                trace['timeout_stage'] = str(trace.get('timeout_stage') or 'connect').strip()
                trace['total_duration_ms'] = round((time.monotonic() - total_started) * 1000.0, 3)
                last_trace = dict(trace)
                self._record_cms_request_trace(trace)
                if attempt < 3:
                    time.sleep(0.8 * attempt)
                    continue
                raise CmsRequestTimeoutError(f'CMS KA-AddAnchor request timed out after {attempt} attempts', timeout_stage=str(trace.get('timeout_stage') or 'connect'), request_trace=trace) from exc
            except Exception as exc:
                timeout_stage = str(trace.get('timeout_stage') or '').strip()
                message_lower = str(exc).lower()
                if not timeout_stage:
                    if isinstance(exc, ssl.SSLError) and 'timed out' in message_lower:
                        timeout_stage = 'tls_handshake'
                    elif isinstance(exc, urllib.error.URLError) and 'timed out' in message_lower:
                        timeout_stage = 'response_headers'
                    elif isinstance(exc, http.client.RemoteDisconnected):
                        timeout_stage = 'response_headers'
                if timeout_stage:
                    trace['timeout_stage'] = timeout_stage
                trace['error_type'] = type(exc).__name__
                trace['error'] = str(exc)
                trace['total_duration_ms'] = round((time.monotonic() - total_started) * 1000.0, 3)
                last_trace = dict(trace)
                self._record_cms_request_trace(trace)
                if timeout_stage and attempt < 3:
                    last_exc = exc
                    time.sleep(0.8 * attempt)
                    continue
                if timeout_stage:
                    raise CmsRequestTimeoutError(f'CMS KA-AddAnchor request timed out after {attempt} attempts', timeout_stage=timeout_stage, request_trace=trace) from exc
                raise
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        if last_exc:
            raise last_exc
        if last_trace and str(last_trace.get('timeout_stage') or '').strip():
            raise CmsRequestTimeoutError('CMS KA-AddAnchor request timed out', timeout_stage=str(last_trace.get('timeout_stage') or ''), request_trace=last_trace)
        raise TimeoutError('CMS KA-AddAnchor request timed out')

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
        configured_id = str(configured_guild_id or '').strip()
        configured_sid = str(configured_guild_sid or '').strip()
        try:
            data = self._cms_request_json(method='GET', url=url, authorization=authorization, proxy_url=proxy_url, timeout_seconds=timeout_seconds)
        except urllib.error.HTTPError as exc:
            if exc.code == 403 and configured_id and configured_sid:
                return {'id': configured_id, 'guild_id': configured_id, 'guild_name': target_guild.strip(), 'sid': configured_sid, 'guild_sid': configured_sid, 'guild_list_unavailable': True}
            raise
        rows = data.get('data') if isinstance(data, dict) else data
        if not isinstance(rows, list):
            rows = []
        dict_rows = [r for r in rows if isinstance(r, dict)]
        target_norm = target_guild.strip().lower()
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

    def _classify_cms_add_anchor_response(self, response: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(response, dict):
            return {
                'category': 'unexpected_response',
                'result_code': 'cms_add_anchor_unexpected_error',
                'result_reason': 'CMS addAnchor returned an unsupported response shape',
                'success_count': None,
                'fail_count': None,
                'fail_items': [],
            }
        code = response.get('code')
        success = response.get('success')
        message = str(response.get('message') or response.get('msg') or response.get('error') or '').strip()
        data = response.get('data') if isinstance(response.get('data'), dict) else {}
        result_body = data if data else response
        def to_int(value: Any) -> int | None:
            try:
                if value is None or value == '':
                    return None
                return int(value)
            except Exception:
                return None
        success_count = to_int(result_body.get('success_count') or result_body.get('successCount'))
        fail_count = to_int(result_body.get('fail_count') or result_body.get('failCount'))
        fail_items_raw = result_body.get('fail_items') or result_body.get('failItems') or []
        fail_items: list[dict[str, str]] = []
        if isinstance(fail_items_raw, list):
            for item in fail_items_raw:
                if isinstance(item, dict):
                    fail_items.append({
                        'sid': str(item.get('sid') or item.get('user_id') or '').strip(),
                        'reason': str(item.get('reason') or item.get('message') or item.get('msg') or '').strip(),
                    })
                else:
                    fail_items.append({'sid': '', 'reason': str(item)[:200]})
        reasons = '; '.join(item.get('reason', '') for item in fail_items).strip()
        lowered = (message + ' ' + reasons).lower()
        common = {'success_count': success_count, 'fail_count': fail_count, 'fail_items': fail_items}
        if code in (401, 403, '401', '403') or 'token' in lowered or 'authorization' in lowered or 'unauthorized' in lowered or 'forbidden' in lowered or '登录失效' in lowered:
            return {
                **common,
                'category': 'authorization_invalid',
                'result_code': 'cms_authorization_invalid',
                'result_reason': message or 'CMS authorization rejected or expired',
            }
        if code in (1003, '1003') or 'permission denied' in lowered or 'scope mismatch' in lowered or 'no permission' in lowered:
            return {
                **common,
                'category': 'authorization_scope_denied',
                'result_code': 'cms_authorization_scope_denied',
                'result_reason': message or 'CMS authorization does not allow binding this guild',
            }
        if code in (1000, '1000') or success is True:
            if fail_count is not None and fail_count > 0:
                if 'already_joined_another_guild' in lowered or 'another guild' in lowered or 'another agency' in lowered or 'other guild' in lowered:
                    return {
                        **common,
                        'category': 'already_in_other_guild',
                        'result_code': 'already_in_other_guild',
                        'result_reason': 'The streamer was in another agency',
                    }
                if 'already in this guild' in lowered or 'already_joined_this_guild' in lowered or 'already_joined_current_guild' in lowered or 'already in target' in lowered:
                    return {
                        **common,
                        'category': 'already_in_target_guild',
                        'result_code': 'already_in_target_guild',
                        'result_reason': 'Previously registered in this agency',
                    }
                if 'invalid' in lowered or 'not found' in lowered or 'not anchor' in lowered or 'unavailable' in lowered:
                    return {
                        **common,
                        'category': 'invalid_sid',
                        'result_code': 'cms_sid_not_found',
                        'result_reason': 'Invalid or unavailable Linky ID',
                    }
                return {
                    **common,
                    'category': 'business_failed',
                    'result_code': 'cms_add_anchor_business_failed',
                    'result_reason': reasons or message or 'CMS KA-AddAnchor returned failed items',
                }
            if success_count is None or success_count > 0 or success is True:
                return {**common, 'category': 'submitted', 'result_code': '', 'result_reason': ''}
        if code in (1001, '1001') or 'invalid arguments' in lowered:
            return {
                **common,
                'category': 'invalid_arguments_manual_check',
                'result_code': 'cms_add_anchor_invalid_arguments_manual_check',
                'result_reason': 'CMS returned invalid arguments; verify CMS parameters/guild scope manually',
            }
        if 'timeout' in lowered or 'temporarily' in lowered or 'gateway' in lowered or 'unavailable' in lowered:
            return {
                **common,
                'category': 'temporary_error',
                'result_code': 'cms_add_anchor_temporary_error',
                'result_reason': message or 'CMS addAnchor temporary error',
            }
        return {
            **common,
            'category': 'unexpected_error',
            'result_code': 'cms_add_anchor_unexpected_error',
            'result_reason': message or reasons or 'CMS addAnchor returned an unexpected error',
        }

    def _cms_add_anchor(self, *, base_url: str, authorization: str, proxy_url: str = '', sid: str, guild_id: str, timeout_seconds: float = 8.0) -> dict[str, Any]:
        if not guild_id:
            raise RuntimeError('CMS target guild_id is missing')
        url = f'{base_url}/api/admin/linky/industrial/streamer_detail/addAnchor'
        data = self._cms_request_json(method='POST', url=url, authorization=authorization, body={'sids': [str(sid).strip()], 'guild_id': int(guild_id)}, proxy_url=proxy_url, timeout_seconds=timeout_seconds)
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
                    'result_reason': normalize_bind_upstream_error(status, body_text),
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
