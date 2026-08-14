from __future__ import annotations

from app.main_shared import *
from app.newcomer_publication import (
    NewcomerPublicationNotReady,
    list_newcomer_publication,
    reconcile_newcomer_publication,
)


class ExecutorServiceMixin:
    @staticmethod
    def _lighten_production_ops_daemon_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        def light_requesters(value: Any) -> Any:
            if not isinstance(value, list):
                return value
            return [
                {
                    'id': item.get('id') or item.get('requesterId') or item.get('phone') or item.get('displayName'),
                    'requestedAtIso': item.get('requestedAtIso'),
                    'requestedAtUnix': item.get('requestedAtUnix'),
                }
                for item in value[:20]
                if isinstance(item, dict)
            ]

        def light_cycle(cycle: Any) -> Any:
            if not isinstance(cycle, dict):
                return cycle
            result: Dict[str, Any] = {}
            for key in ('approval_scope', 'registration_group', 'target_group', 'checked_at', 'decided_at', 'decision', 'ready', 'reason', 'pending_count', 'oldest_pending_at', 'monitor_target', 'truth_state', 'truth_snapshot'):
                if key in cycle:
                    result[key] = cycle.get(key)
            decision_group_state = cycle.get('decision_group_state') if isinstance(cycle.get('decision_group_state'), dict) else {}
            payload_obj = decision_group_state.get('payload') if isinstance(decision_group_state.get('payload'), dict) else {}
            if decision_group_state:
                result['decision_group_state'] = {
                    'ok': decision_group_state.get('ok'),
                    'source': decision_group_state.get('source'),
                    'checked_at': decision_group_state.get('checked_at'),
                    'payload': {
                        'group_id': payload_obj.get('group_id'),
                        'group_name': payload_obj.get('group_name'),
                        'pending_count': payload_obj.get('pending_count'),
                        'requesters': light_requesters(payload_obj.get('requesters')),
                    },
                }
            return result

        if not isinstance(payload, dict):
            return {}
        light = dict(payload)
        runtime = dict(light.get('runtime') or {})
        status = dict(runtime.get('status') or {})
        state = dict(runtime.get('state') or {})
        for cycles_key in ('registration_group_cycles', 'official_group_cycles'):
            if isinstance(status.get(cycles_key), list):
                status[cycles_key] = [light_cycle(cycle) for cycle in status.get(cycles_key) or []]
        if isinstance(status.get('truth_snapshots'), dict):
            truth_snapshots = dict(status.get('truth_snapshots') or {})
            if isinstance(truth_snapshots.get('registration_group_cycles'), list):
                truth_snapshots['registration_group_cycles'] = [light_cycle(cycle) for cycle in truth_snapshots.get('registration_group_cycles') or []]
            status['truth_snapshots'] = truth_snapshots
        for bulky_key in ('raw_cycles', 'debug', 'debug_events', 'recent_logs', 'notifications'):
            if bulky_key in status and isinstance(status.get(bulky_key), list):
                status[bulky_key] = status.get(bulky_key)[:20]
        if isinstance(state, dict):
            state = {
                key: state.get(key)
                for key in ('running', 'pid', 'started_at', 'updated_at', 'last_heartbeat_at', 'last_error')
                if key in state
            }
        runtime['status'] = status
        runtime['state'] = state
        light['runtime'] = runtime
        light['payload_mode'] = 'light'
        return light

    def get_production_ops_daemon_config_light(self) -> Dict[str, Any]:
        return self._lighten_production_ops_daemon_payload(
            self.get_production_ops_daemon_config(include_truth_snapshots=False)
        )

    def update_production_ops_daemon_config(self, payload: ProductionOpsDaemonConfigUpdateRequest) -> Dict[str, Any]:
        existing = self.get_production_ops_daemon_config()['config']
        registration_group = str(payload.registration_group or '').strip() or str(existing.get('registration_group') or self._default_production_ops_daemon_config().get('registration_group') or '').strip()
        raw_worker_base_url = payload.worker_base_url if payload.worker_base_url is not None else existing.get('worker_base_url')
        worker_base_url = _sanitize_legacy_shared_webjs_worker_base_url(raw_worker_base_url)
        row = {
            'config_name': 'default',
            'enabled': 1 if payload.enabled else 0,
            'registration_group': registration_group,
            'api_base_url': str(payload.api_base_url or existing.get('api_base_url') or 'http://127.0.0.1:8011').strip(),
            'worker_base_url': worker_base_url,
            'interval_seconds': max(5.0, float(payload.interval_seconds or existing.get('interval_seconds') or 20.0)),
            'notify_chat_id': str(payload.notify_chat_id or '').strip(),
            'area': str(payload.area or existing.get('area') or 'Indonesia').strip(),
            'remark': str(payload.remark or existing.get('remark') or 'production auto approval daemon').strip(),
            'approved_count': max(1, int(payload.approved_count or existing.get('approved_count') or 1)),
            'auto_recover_worker': 1 if payload.auto_recover_worker else 0,
            'updated_at': utc_now(),
        }
        if not row['api_base_url']:
            raise HTTPException(status_code=400, detail='api_base_url is required')
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO production_ops_daemon_configs (
                    config_name, enabled, registration_group, api_base_url, worker_base_url, interval_seconds,
                    notify_chat_id, area, remark, approved_count, auto_recover_worker, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_name)
                DO UPDATE SET enabled = excluded.enabled,
                              registration_group = excluded.registration_group,
                              api_base_url = excluded.api_base_url,
                              worker_base_url = excluded.worker_base_url,
                              interval_seconds = excluded.interval_seconds,
                              notify_chat_id = excluded.notify_chat_id,
                              area = excluded.area,
                              remark = excluded.remark,
                              approved_count = excluded.approved_count,
                              auto_recover_worker = excluded.auto_recover_worker,
                              updated_at = excluded.updated_at
                """,
                (
                    row['config_name'], row['enabled'], row['registration_group'], row['api_base_url'], row['worker_base_url'], row['interval_seconds'],
                    row['notify_chat_id'], row['area'], row['remark'], row['approved_count'], row['auto_recover_worker'], row['updated_at'],
                ),
            )
            conn.commit()
        self._persist_production_ops_daemon_env({**row, 'enabled': bool(row['enabled']), 'auto_recover_worker': bool(row['auto_recover_worker'])})
        runtime_sync = self._sync_production_ops_daemon_launch_agent(enabled=bool(row['enabled']))
        return {
            'saved': True,
            'config': {
                **row,
                'enabled': bool(row['enabled']),
                'auto_recover_worker': bool(row['auto_recover_worker']),
            },
            'runtime_sync': runtime_sync,
        }

    def _normalize_cms_executor_keepalive_entry(self, item: Dict[str, Any]) -> Dict[str, Any]:
        entry = dict(item or {})
        checked_at_ts: Optional[int] = None
        raw_checked = entry.get('checked_at')
        if isinstance(raw_checked, (int, float)):
            checked_at_ts = int(raw_checked)
        else:
            raw_iso = str(entry.get('checked_at_iso') or raw_checked or '').strip()
            if raw_iso:
                try:
                    checked_at_ts = int(parse_iso_datetime(raw_iso).timestamp())
                except Exception:
                    checked_at_ts = None
        stale_after_seconds = _coerce_positive_int(entry.get('stale_after_seconds'), CMS_EXECUTOR_KEEPALIVE_STALE_UNKNOWN_SECONDS)
        freshness_ts = checked_at_ts
        raw_refreshed_at = str(entry.get('refreshed_at') or '').strip()
        if raw_refreshed_at:
            try:
                freshness_ts = int(parse_iso_datetime(raw_refreshed_at).timestamp())
            except Exception:
                freshness_ts = checked_at_ts
        is_stale = freshness_ts is None or (time.time() - freshness_ts) > stale_after_seconds
        error_category = str(entry.get('error_category') or '').strip().lower()
        ok = bool(entry.get('ok'))
        if is_stale:
            live_status = 'unknown'
        elif ok:
            live_status = 'active'
        elif error_category in {'auth_invalid', 'scope_denied', 'target_not_visible', 'not_configured'}:
            live_status = 'inactive'
        else:
            live_status = 'unknown'
        reason_map = {
            'auth_invalid': 'CMS 登录态失效，请重新授权',
            'scope_denied': 'CMS 权限不足，需处理',
            'target_not_visible': 'CMS 目标公会不可见',
            'transient_timeout': 'CMS 探活超时，待校验',
            'transient_network': 'CMS 网络波动，待校验',
            'invalid_response': 'CMS 返回异常，待校验',
            'http_error': 'CMS 校验异常，待校验',
            'not_configured': '未配置 CMS 凭证',
            'unknown': 'CMS 状态待校验',
        }
        normalized_reason = 'CMS 状态待校验' if is_stale else (reason_map.get(error_category) or ('' if ok else str(entry.get('error') or 'CMS 状态待校验')))
        entry['checked_at'] = checked_at_ts
        entry['checked_at_iso'] = str(entry.get('checked_at_iso') or (datetime.fromtimestamp(checked_at_ts, tz=timezone.utc).isoformat().replace('+00:00', 'Z') if checked_at_ts else ''))
        entry['stale_after_seconds'] = stale_after_seconds
        entry['is_stale'] = is_stale
        entry['live_status'] = live_status
        entry['normalized_reason'] = normalized_reason
        entry['probe_endpoint'] = str(entry.get('probe_endpoint') or '')
        entry['error_category'] = error_category
        return entry

    def _cms_executor_keepalive_status_by_guild(self) -> Dict[str, Dict[str, Any]]:
        try:
            if not CMS_EXECUTOR_KEEPALIVE_STATUS_PATH.exists():
                return {}
            payload = json.loads(CMS_EXECUTOR_KEEPALIVE_STATUS_PATH.read_text(encoding='utf-8'))
        except Exception:
            return {}
        rows = payload.get('results') if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            return {}
        result: Dict[str, Dict[str, Any]] = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            normalized = self._normalize_cms_executor_keepalive_entry(item)
            guild_name = str(normalized.get('guild_name') or normalized.get('account') or '').strip()
            if guild_name:
                result[guild_name] = normalized
        return result

    @staticmethod
    def _cms_executor_keepalive_script_path() -> Optional[Path]:
        for candidate in CMS_EXECUTOR_KEEPALIVE_SCRIPT_CANDIDATES:
            if candidate.exists():
                return candidate
        return None

    def _build_cms_keepalive_account_payload(self, guild_name: str) -> Optional[Dict[str, Any]]:
        executor = self.resolve_guild_executor(guild_name)
        if not executor or not bool(executor.get('enabled')):
            return None
        authorization = str(executor.get('platform_authorization') or '').strip()
        refresh_token = str(executor.get('cms_refresh_token') or '').strip()
        if not authorization and not refresh_token:
            return None
        return {
            'guild_name': str(executor.get('guild_name') or guild_name or '').strip(),
            'platform_authorization': authorization,
            'cms_refresh_token': refresh_token,
            'cms_refresh_token_deadtime': executor.get('cms_refresh_token_deadtime'),
            'cms_guild_id': str(executor.get('cms_guild_id') or '').strip(),
            'cms_guild_sid': str(executor.get('cms_guild_sid') or '').strip(),
            'proxy_url': self._resolve_executor_proxy_url(executor),
            'request_timeout_seconds': int(executor.get('request_timeout_seconds') or 30),
        }

    def _run_cms_executor_keepalive_accounts(self, accounts: List[Dict[str, Any]]) -> Dict[str, Any]:
        clean_accounts = [dict(item) for item in (accounts or []) if isinstance(item, dict) and str(item.get('guild_name') or '').strip()]
        if not clean_accounts:
            return {'results': [], 'summary': {'total': 0, 'ok': 0, 'failed': 0}}
        script_path = self._cms_executor_keepalive_script_path()
        if script_path is None:
            raise RuntimeError('cms keepalive script not found')
        with tempfile.TemporaryDirectory(prefix='cms-keepalive-refresh-') as tmpdir:
            state_path = Path(tmpdir) / 'cms_executor_keepalive_status.json'
            env = os.environ.copy()
            env['CMS_KEEPALIVE_ACCOUNTS_JSON'] = json.dumps(clean_accounts, ensure_ascii=False)
            completed = subprocess.run(
                [sys.executable, str(script_path), '--state-path', str(state_path), '--db-path', str(self.db.db_path)],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=max(30, 45 * len(clean_accounts)),
                check=False,
                env=env,
            )
            if completed.returncode != 0:
                detail = str(completed.stderr or completed.stdout or f'exit code {completed.returncode}').strip()
                raise RuntimeError(f'cms keepalive refresh failed: {detail}')
            try:
                payload = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
            except Exception as exc:
                raise RuntimeError(f'cms keepalive refresh output unreadable: {exc}') from exc
        results = payload.get('results') if isinstance(payload, dict) else []
        if not isinstance(results, list):
            results = []
        return {
            'results': results,
            'summary': payload.get('summary') if isinstance(payload, dict) else {},
        }

    def _merge_cms_executor_keepalive_results(self, results: List[Dict[str, Any]]) -> None:
        current_payload: Dict[str, Any] = {}
        current_rows: List[Dict[str, Any]] = []
        try:
            if CMS_EXECUTOR_KEEPALIVE_STATUS_PATH.exists():
                current_payload = json.loads(CMS_EXECUTOR_KEEPALIVE_STATUS_PATH.read_text(encoding='utf-8'))
                loaded_rows = current_payload.get('results') if isinstance(current_payload, dict) else []
                if isinstance(loaded_rows, list):
                    current_rows = [dict(item) for item in loaded_rows if isinstance(item, dict)]
        except Exception:
            current_payload = {}
            current_rows = []
        merged_by_guild: Dict[str, Dict[str, Any]] = {}
        ordered_guilds: List[str] = []

        def _put(item: Dict[str, Any]) -> None:
            guild = str(item.get('guild_name') or item.get('account') or '').strip()
            if not guild:
                return
            if guild not in merged_by_guild:
                ordered_guilds.append(guild)
            merged_by_guild[guild] = dict(item)

        for item in current_rows:
            _put(item)
        refreshed_at = utc_now()
        for item in results or []:
            if isinstance(item, dict):
                _put({**item, 'refreshed_at': str(item.get('refreshed_at') or refreshed_at)})

        merged_rows = [merged_by_guild[guild] for guild in ordered_guilds]
        ok_count = sum(1 for item in merged_rows if bool(item.get('ok')))
        failed_count = max(0, len(merged_rows) - ok_count)
        payload = {
            **(current_payload if isinstance(current_payload, dict) else {}),
            'generated_at': utc_now(),
            'generated_by': 'ops_intake_manual_refresh',
            'results': merged_rows,
            'summary': {
                'total': len(merged_rows),
                'ok': ok_count,
                'failed': failed_count,
            },
        }
        save_json_state(CMS_EXECUTOR_KEEPALIVE_STATUS_PATH, payload)

    def refresh_ops_intake_guild_health(
        self,
        *,
        user: Optional[Dict[str, Any]],
        guild_names: Optional[List[str]] = None,
        only_if_unknown_or_stale: bool = True,
    ) -> Dict[str, Any]:
        visible_guilds = self._ops_intake_visible_guild_names(user=user)
        visible_set = set(visible_guilds)
        requested: List[str] = []
        for value in guild_names or []:
            guild = str(value or '').strip()
            if guild and guild not in requested:
                requested.append(guild)
        candidate_names = requested or visible_guilds
        health_by_guild = {str(row.get('guild_name') or '').strip(): row for row in self.guild_executor_health().get('rows', [])}
        target_names: List[str] = []
        skipped: List[Dict[str, Any]] = []
        for guild_name in candidate_names:
            if guild_name not in visible_set:
                skipped.append({'guild_name': guild_name, 'reason': 'forbidden'})
                continue
            health = health_by_guild.get(guild_name, {})
            cms_configured = bool(health.get('cms_token_configured') or health.get('cms_refresh_token_configured'))
            if not cms_configured:
                skipped.append({'guild_name': guild_name, 'reason': 'cms_not_configured'})
                continue
            if only_if_unknown_or_stale:
                current_status = str(health.get('cms_live_status') or '').strip().lower()
                if current_status and current_status != 'unknown' and not bool(health.get('cms_live_is_stale')):
                    skipped.append({'guild_name': guild_name, 'reason': 'already_fresh'})
                    continue
            target_names.append(guild_name)
        accounts: List[Dict[str, Any]] = []
        for guild_name in target_names:
            payload = self._build_cms_keepalive_account_payload(guild_name)
            if payload:
                accounts.append(payload)
            else:
                skipped.append({'guild_name': guild_name, 'reason': 'executor_not_probeable'})
        run_result = self._run_cms_executor_keepalive_accounts(accounts) if accounts else {'results': [], 'summary': {'total': 0, 'ok': 0, 'failed': 0}}
        result_rows = [dict(item) for item in (run_result.get('results') or []) if isinstance(item, dict)]
        if result_rows:
            self._merge_cms_executor_keepalive_results(result_rows)
        rows = self.list_ops_intake_guilds(user=user).get('rows', [])
        refreshed_names = [str(item.get('guild_name') or item.get('account') or '').strip() for item in result_rows if str(item.get('guild_name') or item.get('account') or '').strip()]
        return {
            'refreshed_guild_names': refreshed_names,
            'refreshed_count': len(refreshed_names),
            'skipped': skipped,
            'summary': run_result.get('summary') or {'total': len(refreshed_names), 'ok': 0, 'failed': 0},
            'results': result_rows,
            'rows': rows,
        }

    def guild_executor_health(self) -> Dict[str, Any]:
        executors = self.list_guild_executors()['rows']
        cms_keepalive_by_guild = self._cms_executor_keepalive_status_by_guild()
        human_actions = self._pending_bind_human_actions(limit=100)
        human_by_guild = {}
        for item in human_actions:
            guild_name = str(item.get('guild_name') or '').strip()
            if guild_name and guild_name not in human_by_guild:
                human_by_guild[guild_name] = item
        with self.db.connect() as conn:
            latest_bind_rows = [dict(r) for r in conn.execute(
                """
                SELECT x.guild_name, x.task_id, x.status, x.result_code, x.result_reason, x.created_at, x.started_at, x.finished_at
                FROM (
                    SELECT COALESCE(l.dept_name, '') AS guild_name,
                           t.task_id,
                           t.status,
                           t.result_code,
                           t.result_reason,
                           t.created_at,
                           t.started_at,
                           t.finished_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(l.dept_name, '')
                               ORDER BY COALESCE(t.finished_at, t.started_at, t.created_at) DESC
                           ) AS rn
                    FROM automation_tasks t
                    LEFT JOIN leads l ON l.lead_id = t.lead_id
                    WHERE t.task_type = 'bind_check'
                ) x
                WHERE x.rn = 1
                """
            ).fetchall()]
            processing_rows = [dict(r) for r in conn.execute(
                """
                SELECT COALESCE(l.dept_name, '') AS guild_name, COUNT(*) AS processing_count
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check' AND t.status = 'processing'
                GROUP BY COALESCE(l.dept_name, '')
                """
            ).fetchall()]
        latest_by_guild = {str(r.get('guild_name') or '').strip(): r for r in latest_bind_rows}
        processing_by_guild = {str(r.get('guild_name') or '').strip(): int(r.get('processing_count') or 0) for r in processing_rows}
        rows = []
        for executor in executors:
            guild_name = str(executor.get('guild_name') or '').strip()
            latest = latest_by_guild.get(guild_name, {})
            human = human_by_guild.get(guild_name, {})
            bind_concurrency = int(executor.get('bind_concurrency') or 1)
            processing_count = int(processing_by_guild.get(guild_name) or 0)
            human_visible = bool(human)
            if human_visible and latest:
                latest_task_id = str(latest.get('task_id') or '').strip()
                human_task_id = str(human.get('task_id') or '').strip()
                latest_code = str(latest.get('result_code') or '').strip().lower()
                latest_reason = str(latest.get('result_reason') or '').strip().lower()
                latest_still_requires_human = latest_code in {'bind_unauthorized', 'auth_required', 'session_expired', 'bind_session_expired', 'captcha_required', 'bind_captcha_required', 'manual_continue_required', 'bind_manual_continue_required'} or 're-login' in latest_reason or 'status code 401' in latest_reason or 'unauthorized' in latest_reason or 'forbidden' in latest_reason or 'captcha' in latest_reason
                if latest_task_id and human_task_id and latest_task_id != human_task_id and not latest_still_requires_human:
                    human_visible = False
            cms_keepalive = cms_keepalive_by_guild.get(guild_name, {})
            cms_token_configured = bool(executor.get('platform_authorization_configured'))
            cms_refresh_configured = bool(executor.get('cms_refresh_token_configured'))
            cms_live_status = str(cms_keepalive.get('live_status') or ('not_configured' if not (cms_token_configured or cms_refresh_configured) else 'unknown')).strip() or 'unknown'
            cms_reason = str(cms_keepalive.get('normalized_reason') or '').strip()
            if not bool(executor.get('enabled')):
                effective_status = 'disabled'
                effective_reason = '执行器已停用'
            elif cms_token_configured:
                if cms_live_status == 'active':
                    effective_status = 'active'
                    effective_reason = 'CMS 保活正常'
                elif not cms_refresh_configured:
                    effective_status = 'inactive'
                    effective_reason = '缺 CMS Refresh Token'
                elif cms_live_status == 'inactive':
                    effective_status = 'inactive'
                    effective_reason = cms_reason or 'CMS 验证失败'
                else:
                    effective_status = 'unknown'
                    effective_reason = cms_reason or 'CMS 状态待校验'
            elif bool(executor.get('oauth_configured')):
                effective_status = 'active'
                effective_reason = '个人 Code 绑定链路已配置'
            else:
                effective_status = 'inactive'
                effective_reason = '缺绑定凭证'
            cms_channel_status = 'not_configured'
            if cms_token_configured or cms_refresh_configured:
                cms_channel_status = 'valid' if cms_live_status == 'active' else ('invalid' if cms_live_status == 'inactive' else 'unknown')
            rows.append({
                'guild_name': guild_name,
                'enabled': bool(executor.get('enabled')),
                'effective_status': effective_status,
                'effective_reason': effective_reason,
                'cms_live_status': cms_live_status,
                'cms_live_checked_at': cms_keepalive.get('checked_at_iso') or cms_keepalive.get('checked_at'),
                'cms_live_error': cms_keepalive.get('error'),
                'cms_live_error_category': cms_keepalive.get('error_category'),
                'cms_live_probe_endpoint': cms_keepalive.get('probe_endpoint'),
                'cms_live_is_stale': bool(cms_keepalive.get('is_stale')),
                'cms_live_seconds_to_expiry': cms_keepalive.get('seconds_to_expiry'),
                'cms_token_configured': cms_token_configured,
                'cms_refresh_token_configured': cms_refresh_configured,
                'browser_profile_key': executor.get('browser_profile_key') or '',
                'proxy_region': executor.get('proxy_region') or '',
                'bind_concurrency': bind_concurrency,
                'processing_count': processing_count,
                'available_slots': max(0, max(1, bind_concurrency) - processing_count),
                'last_bind_task_id': latest.get('task_id'),
                'last_bind_status': latest.get('status'),
                'last_bind_result_code': latest.get('result_code'),
                'last_bind_result_reason': latest.get('result_reason'),
                'last_bind_created_at': latest.get('created_at'),
                'last_bind_started_at': latest.get('started_at'),
                'last_bind_finished_at': latest.get('finished_at'),
                'requires_human_action': human_visible,
                'human_action_type': human.get('human_action_type') if human_visible else None,
                'human_action_task_id': human.get('task_id') if human_visible else None,
                'cms_channel_status': cms_channel_status,
                'code_channel_status': 'valid' if bool(executor.get('oauth_configured')) else 'not_configured',
            })
        return {'rows': rows}

    @staticmethod
    def _guild_anchor_country_header(country: Any) -> str:
        text = str(country or '').strip().lower()
        if text in {'indonesia', 'indonesia/id', 'id', '印尼'}:
            return 'ID'
        if text in {'brazil', 'br', '巴西'}:
            return 'BR'
        if text in {'mexico', 'mx', '墨西哥'}:
            return 'MX'
        return 'US'

    @staticmethod
    def _parse_anchor_stat_date(value: Any, *, fallback: datetime.date) -> datetime.date:
        text = str(value or '').strip()
        if not text:
            return fallback
        try:
            return datetime.strptime(text[:10], '%Y-%m-%d').date()
        except Exception:
            return fallback

    @staticmethod
    def _guild_anchor_executor_key(executor: Dict[str, Any]) -> str:
        app_name = str(executor.get('app_name') or 'linky').strip().lower() or 'linky'
        for key in ('cms_guild_sid', 'cms_guild_id', 'guild_executor_id', 'rowid'):
            value = str(executor.get(key) or '').strip()
            if value:
                return f'{app_name}:{key}:{value}'
        guild_name = str(executor.get('guild_name') or '').strip()
        digest = hashlib.sha1(guild_name.encode('utf-8')).hexdigest()[:16] if guild_name else 'unknown'
        return f'{app_name}:guild_name_sha1:{digest}'

    @staticmethod
    def _guild_anchor_anchor_id(item: Dict[str, Any]) -> str:
        for key in ('user_id', 'userId', 'timoId', 'timo_id', 'sid', 'character_id', 'id', 'anchor_id'):
            value = str(item.get(key) or '').strip()
            if value:
                return f'{key}:{value}'
        raw = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
        return 'hash:' + hashlib.sha1(raw.encode('utf-8')).hexdigest()

    @staticmethod
    def _guild_anchor_streamer_sid(item: Dict[str, Any]) -> str:
        sid = str(item.get('sid') or '').strip()
        character_id = str(item.get('character_id') or item.get('characterId') or '').strip()
        return sid or character_id

    @staticmethod
    def _guild_anchor_streamer_sid_source_contract(item: Dict[str, Any]) -> str:
        if str(item.get('sid') or '').strip():
            return 'linky_authoritative_sid_v1'
        if str(item.get('character_id') or item.get('characterId') or '').strip():
            return 'linky_character_id_fallback_v1'
        return ''

    @staticmethod
    def _guild_anchor_created_epoch(item: Dict[str, Any]) -> int:
        for key in ('created_at', 'joinTime', 'join_time', 'joined_at', 'createdAt'):
            raw_value = item.get(key)
            if raw_value in (None, ''):
                continue
            if isinstance(raw_value, (int, float)):
                try:
                    value = int(float(raw_value))
                    if value > 10_000_000_000:
                        value = value // 1000
                    if value > 0:
                        return value
                except Exception:
                    continue
            text = str(raw_value or '').strip()
            if not text:
                continue
            if re.fullmatch(r'\d+(?:\.\d+)?', text):
                try:
                    value = int(float(text))
                    if value > 10_000_000_000:
                        value = value // 1000
                    if value > 0:
                        return value
                except Exception:
                    continue
            for fmt in ('%Y.%m.%d %H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S', '%Y%m%d%H%M%S', '%Y%m%d'):
                try:
                    parsed = datetime.strptime(text[:len(datetime.now().strftime(fmt))], fmt)
                    return int(parsed.replace(tzinfo=ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc).timestamp())
                except Exception:
                    continue
        return 0

    @staticmethod
    def _guild_anchor_is_real_person(item: Dict[str, Any]) -> int:
        raw_value = item.get('isRealPerson', item.get('is_real_person', item.get('realPerson', item.get('verified'))))
        if isinstance(raw_value, bool):
            return 1 if raw_value else 0
        text = str(raw_value or '').strip().lower()
        if text in {'1', 'true', 'yes', 'y', '是', '已认证', '認證', 'verified'}:
            return 1
        return 0

    def _list_enabled_linky_guild_anchor_executors(self) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                """
                SELECT rowid AS guild_executor_id, guild_name, COALESCE(app_name, 'linky') AS app_name,
                       oauth_token, oauth_token_secret, cms_guild_id, cms_guild_sid,
                       country, proxy_url, proxy_region, request_timeout_seconds, enabled
                FROM guild_executors
                WHERE enabled = 1
                  AND LOWER(COALESCE(app_name, 'linky')) = 'linky'
                ORDER BY guild_name ASC
                """
            ).fetchall()]

    def _list_enabled_timo_guild_anchor_executors(self) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                """
                SELECT rowid AS guild_executor_id, guild_name, COALESCE(app_name, 'linky') AS app_name,
                       platform_backend_url, platform_authorization, cms_guild_id, cms_guild_sid,
                       country, proxy_url, proxy_region, request_timeout_seconds, enabled
                FROM guild_executors
                WHERE enabled = 1
                  AND LOWER(COALESCE(app_name, 'linky')) = 'timo'
                ORDER BY guild_name ASC
                """
            ).fetchall()]

    def _list_enabled_guild_anchor_executors(self) -> List[Dict[str, Any]]:
        return self._list_enabled_linky_guild_anchor_executors() + self._list_enabled_timo_guild_anchor_executors()

    def _record_guild_anchor_seen_rows(
        self,
        *,
        executor: Dict[str, Any],
        items: List[Dict[str, Any]],
        total_anchors: Optional[int],
        page: int,
        page_size: int,
    ) -> int:
        if not items:
            return 0
        executor_key = self._guild_anchor_executor_key(executor)
        guild_name = str(executor.get('guild_name') or '').strip()
        now_iso = utc_now()
        rows = []
        for item in items:
            if not isinstance(item, dict):
                continue
            created_epoch = self._guild_anchor_created_epoch(item)
            if created_epoch <= 0:
                continue
            created_utc = datetime.fromtimestamp(created_epoch, tz=timezone.utc)
            created_bj = datetime.fromtimestamp(created_epoch, tz=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
            anchor_id = self._guild_anchor_anchor_id(item)
            streamer_sid = (
                self._guild_anchor_streamer_sid(item)
                if str(executor.get('app_name') or 'linky').strip().lower() == 'linky'
                else ''
            )
            streamer_sid_source_contract = (
                self._guild_anchor_streamer_sid_source_contract(item)
                if streamer_sid else ''
            )
            anchor_name = str(
                item.get('nickName')
                or item.get('nickname')
                or item.get('nick_name')
                or item.get('name')
                or item.get('userName')
                or ''
            ).strip()
            is_real_person = self._guild_anchor_is_real_person(item)
            raw_for_hash = {
                'user_id': item.get('user_id'),
                'userId': item.get('userId'),
                'timoId': item.get('timoId'),
                'sid': item.get('sid'),
                'character_id': item.get('character_id'),
                'anchor_name': anchor_name,
                'created_at': created_epoch,
                'is_real_person': is_real_person,
            }
            raw_hash = hashlib.sha1(json.dumps(raw_for_hash, sort_keys=True, ensure_ascii=False, default=str).encode('utf-8')).hexdigest()
            rows.append((
                executor_key,
                guild_name,
                anchor_id,
                streamer_sid,
                streamer_sid_source_contract,
                anchor_name,
                created_epoch,
                created_utc.date().isoformat(),
                created_bj.date().isoformat(),
                is_real_person,
                now_iso,
                now_iso,
                total_anchors,
                page,
                page_size,
                raw_hash,
            ))
        if not rows:
            return 0
        with self.db.connect() as conn:
            page_anchor_ids = sorted({str(row[2]) for row in rows if str(row[2])})
            existing_anchor_ids: set[str] = set()
            if page_anchor_ids:
                placeholders = ','.join('?' for _ in page_anchor_ids)
                existing_anchor_ids = {
                    str(row['anchor_id'])
                    for row in conn.execute(
                        f"SELECT anchor_id FROM guild_anchor_seen "
                        f"WHERE guild_executor_key = ? AND anchor_id IN ({placeholders})",
                        (executor_key, *page_anchor_ids),
                    ).fetchall()
                }
            page_sid_by_anchor: Dict[str, str] = {}
            page_anchor_by_sid: Dict[str, str] = {}
            for row in rows:
                anchor_id = str(row[2])
                streamer_sid = str(row[3])
                if not streamer_sid:
                    continue
                previous_sid = page_sid_by_anchor.get(anchor_id)
                if previous_sid is not None and previous_sid != streamer_sid:
                    raise RuntimeError('linky_anchor_sid_one_to_many')
                previous_anchor = page_anchor_by_sid.get(streamer_sid)
                if previous_anchor is not None and previous_anchor != anchor_id:
                    raise RuntimeError('linky_anchor_sid_many_to_one')
                page_sid_by_anchor[anchor_id] = streamer_sid
                page_anchor_by_sid[streamer_sid] = anchor_id
            for anchor_id, streamer_sid in page_sid_by_anchor.items():
                existing = conn.execute(
                    """
                    SELECT guild_executor_key,anchor_id,streamer_sid
                    FROM guild_anchor_seen
                    WHERE (guild_executor_key=? AND anchor_id=?) OR streamer_sid=?
                    """,
                    (executor_key, anchor_id, streamer_sid),
                ).fetchall()
                for current in existing:
                    current_sid = str(current['streamer_sid'] or '').strip()
                    if (
                        str(current['guild_executor_key']) != executor_key
                        or str(current['anchor_id']) != anchor_id
                    ):
                        raise RuntimeError('linky_anchor_sid_cross_guild_conflict')
                    if current_sid and current_sid != streamer_sid:
                        raise RuntimeError('linky_anchor_sid_one_to_many')
            conn.executemany(
                """
                INSERT INTO guild_anchor_seen (
                    guild_executor_key, guild_name, anchor_id, streamer_sid, streamer_sid_source_contract, anchor_name, created_at, created_date_utc, created_date_bj,
                    is_real_person, first_seen_at, last_seen_at, source_total_anchors, source_page, source_page_size, raw_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_executor_key, anchor_id) DO UPDATE SET
                    guild_name = excluded.guild_name,
                    streamer_sid = CASE
                        WHEN excluded.streamer_sid != '' THEN excluded.streamer_sid
                        ELSE guild_anchor_seen.streamer_sid
                    END,
                    streamer_sid_source_contract = CASE
                        WHEN excluded.streamer_sid != '' THEN excluded.streamer_sid_source_contract
                        ELSE guild_anchor_seen.streamer_sid_source_contract
                    END,
                    anchor_name = CASE WHEN excluded.anchor_name != '' THEN excluded.anchor_name ELSE guild_anchor_seen.anchor_name END,
                    created_at = excluded.created_at,
                    created_date_utc = excluded.created_date_utc,
                    created_date_bj = excluded.created_date_bj,
                    is_real_person = excluded.is_real_person,
                    last_seen_at = excluded.last_seen_at,
                    source_total_anchors = excluded.source_total_anchors,
                    source_page = excluded.source_page,
                    source_page_size = excluded.source_page_size,
                    raw_hash = excluded.raw_hash
                """,
                rows,
            )
            conn.commit()
        return len(set(page_anchor_ids) - existing_anchor_ids)

    def _count_seen_anchor_date(self, *, executor_key: str, stat_date: str) -> int:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM guild_anchor_seen WHERE guild_executor_key = ? AND created_date_utc = ?",
                (executor_key, stat_date),
            ).fetchone()
        return int(row['n'] or 0) if row else 0

    def _count_seen_real_person_anchor_date(self, *, executor_key: str, stat_date: str) -> int:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM guild_anchor_seen WHERE guild_executor_key = ? AND created_date_utc = ? AND COALESCE(is_real_person, 0) = 1",
                (executor_key, stat_date),
            ).fetchone()
        return int(row['n'] or 0) if row else 0

    def _count_seen_anchor_date_bj(self, *, executor_key: str, stat_date: str) -> int:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM guild_anchor_seen WHERE guild_executor_key = ? AND created_date_bj = ?",
                (executor_key, stat_date),
            ).fetchone()
        return int(row['n'] or 0) if row else 0

    def _count_seen_real_person_anchor_date_bj(self, *, executor_key: str, stat_date: str) -> int:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM guild_anchor_seen WHERE guild_executor_key = ? AND created_date_bj = ? AND COALESCE(is_real_person, 0) = 1",
                (executor_key, stat_date),
            ).fetchone()
        return int(row['n'] or 0) if row else 0

    def _count_seen_anchor_total(self, *, executor_key: str) -> int:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM guild_anchor_seen WHERE guild_executor_key = ?",
                (executor_key,),
            ).fetchone()
        return int(row['n'] or 0) if row else 0

    def _freeze_linky_newcomer_identity_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        executor_key: str,
        guild_name: str,
        stat_date: str,
        refreshed_at: str,
    ) -> int:
        """Freeze the first successful per-anchor cohort for one Linky guild-day."""
        existing = conn.execute(
            """
            SELECT member_count
            FROM guild_anchor_newcomer_snapshot_runs
            WHERE guild_executor_key = ? AND stat_date = ?
            """,
            (executor_key, stat_date),
        ).fetchone()
        if existing is not None:
            member_count = int(existing['member_count'] or 0)
            integrity = conn.execute(
                """
                SELECT COUNT(*) AS n,
                       COUNT(DISTINCT NULLIF(streamer_sid, '')) AS sid_n,
                       SUM(CASE WHEN NULLIF(streamer_sid, '') IS NULL THEN 1 ELSE 0 END) AS missing_n
                FROM guild_anchor_newcomer_identity_snapshots
                WHERE guild_executor_key = ? AND stat_date = ?
                """,
                (executor_key, stat_date),
            ).fetchone()
            if (
                int(integrity['n'] or 0) != member_count
                or int(integrity['sid_n'] or 0) != member_count
                or int(integrity['missing_n'] or 0) != 0
            ):
                raise RuntimeError('linky_newcomer_snapshot_frozen_sid_incomplete')
            return member_count
        members = conn.execute(
            """
            SELECT anchor_id,streamer_sid,streamer_sid_source_contract,
                   anchor_name,created_at,first_seen_at,is_real_person
            FROM guild_anchor_seen
            WHERE guild_executor_key = ? AND created_date_utc = ?
            ORDER BY anchor_id
            """,
            (executor_key, stat_date),
        ).fetchall()
        frozen_rows = []
        frozen_sids: set[str] = set()
        run_source_contract = 'first_successful_linky_refresh_frozen_sid_v1'
        for member in members:
            anchor_id = str(member['anchor_id'] or '').strip()
            sid = str(member['streamer_sid'] or '').strip()
            if not sid:
                raise RuntimeError('linky_newcomer_snapshot_sid_unresolved')
            sid_source_contract = str(
                member['streamer_sid_source_contract'] or ''
            ).strip()
            if sid_source_contract not in {
                'linky_authoritative_sid_v1',
                'linky_character_id_fallback_v1',
            }:
                raise RuntimeError('linky_newcomer_snapshot_sid_source_contract_invalid')
            if sid_source_contract == 'linky_character_id_fallback_v1':
                run_source_contract = (
                    'first_successful_linky_refresh_character_id_fallback_v1'
                )
            if sid in frozen_sids:
                raise RuntimeError('linky_newcomer_snapshot_sid_many_to_one')
            frozen_sids.add(sid)
            frozen_rows.append((
                executor_key, guild_name, stat_date, anchor_id, sid,
                str(member['anchor_name'] or ''), int(member['created_at'] or 0),
                str(member['first_seen_at'] or ''),
                int(member['is_real_person'] or 0), refreshed_at, refreshed_at,
            ))
        for row in frozen_rows:
            historical_sid = conn.execute(
                """
                SELECT streamer_sid
                FROM guild_anchor_newcomer_identity_snapshots
                WHERE anchor_id = ? AND stat_date <> ?
                LIMIT 1
                """,
                (row[3], stat_date),
            ).fetchone()
            if historical_sid is not None and str(historical_sid['streamer_sid']) != row[4]:
                raise RuntimeError('linky_newcomer_snapshot_sid_one_to_many')
            historical_anchor = conn.execute(
                """
                SELECT anchor_id
                FROM guild_anchor_newcomer_identity_snapshots
                WHERE streamer_sid = ? AND stat_date <> ?
                LIMIT 1
                """,
                (row[4], stat_date),
            ).fetchone()
            if historical_anchor is not None and str(historical_anchor['anchor_id']) != row[3]:
                raise RuntimeError('linky_newcomer_snapshot_sid_many_to_one')
        conn.executemany(
            """
            INSERT INTO guild_anchor_newcomer_identity_snapshots (
                guild_executor_key, guild_name, stat_date, anchor_id,
                streamer_sid, anchor_name, source_created_at,
                source_first_seen_at, is_real_person, snapshot_refreshed_at,
                recorded_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            frozen_rows,
        )
        member_count = len(frozen_rows)
        conn.execute(
            """
            INSERT INTO guild_anchor_newcomer_snapshot_runs (
                guild_executor_key, guild_name, stat_date, member_count,
                source_contract, snapshot_refreshed_at, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                executor_key, guild_name, stat_date, member_count,
                run_source_contract,
                refreshed_at, refreshed_at,
            ),
        )
        return member_count

    def _upsert_guild_anchor_marker(
        self,
        *,
        executor: Dict[str, Any],
        total_anchors: Optional[int],
        marker_items: List[Dict[str, Any]],
        marker_page: int,
        marker_page_size: int,
        full_scan: bool,
        sort_confidence: str,
    ) -> None:
        executor_key = self._guild_anchor_executor_key(executor)
        guild_name = str(executor.get('guild_name') or '').strip()
        marker_ids = [self._guild_anchor_anchor_id(item) for item in marker_items if isinstance(item, dict)]
        marker_ids = [item for item in marker_ids if item][:100]
        now_iso = utc_now()
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT last_full_scan_at FROM guild_anchor_scan_markers WHERE guild_executor_key = ?",
                (executor_key,),
            ).fetchone()
            last_full_scan_at = now_iso if full_scan else (str(existing['last_full_scan_at'] or '') if existing else '')
            conn.execute(
                """
                INSERT INTO guild_anchor_scan_markers (
                    guild_executor_key, guild_name, last_total_anchors, last_full_scan_at, last_incremental_scan_at,
                    marker_anchor_ids_json, marker_page, marker_page_size, sort_confidence, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_executor_key) DO UPDATE SET
                    guild_name = excluded.guild_name,
                    last_total_anchors = excluded.last_total_anchors,
                    last_full_scan_at = excluded.last_full_scan_at,
                    last_incremental_scan_at = excluded.last_incremental_scan_at,
                    marker_anchor_ids_json = excluded.marker_anchor_ids_json,
                    marker_page = excluded.marker_page,
                    marker_page_size = excluded.marker_page_size,
                    sort_confidence = excluded.sort_confidence,
                    updated_at = excluded.updated_at
                """,
                (
                    executor_key,
                    guild_name,
                    int(total_anchors or 0),
                    last_full_scan_at,
                    now_iso,
                    json.dumps(marker_ids, ensure_ascii=False),
                    int(marker_page or 0),
                    int(marker_page_size or 0),
                    str(sort_confidence or ''),
                    now_iso,
                ),
            )
            conn.commit()

    def _linky_guild_api_signed_get(
        self,
        *,
        executor: Dict[str, Any],
        path: str,
        params: Dict[str, Any],
        timeout_seconds: float,
    ) -> Dict[str, Any]:
        oauth_token = str(executor.get('oauth_token') or '').strip()
        oauth_secret = str(executor.get('oauth_token_secret') or '').strip()
        if not oauth_token or not oauth_secret:
            raise RuntimeError('oauth_token_or_secret_missing')
        ordered_params = [(str(k), v) for k, v in dict(params or {}).items() if v is not None and str(v) != '']
        query = '?' + '&'.join(f"{quote(k, safe='')}={quote(str(v), safe='')}" for k, v in ordered_params) if ordered_params else ''
        timestamp_ms = str(int(time.time() * 1000))
        signature_base = f'{path}{query}&{timestamp_ms}' if query else f'{path}&{timestamp_ms}'
        signature = base64.b64encode(
            hmac.new(oauth_secret.encode('utf-8'), signature_base.encode('utf-8'), hashlib.sha1).digest()
        ).decode('ascii')
        country = self._guild_anchor_country_header(executor.get('country'))
        headers = {
            'X-Auth-Token': oauth_token,
            'X-Auth-Timestamp': timestamp_ms,
            'X-Auth-Signature': signature,
            'X-App-Language': 'en',
            'Country': country,
            'Accept': 'application/json, text/plain, */*',
            'Origin': 'https://guild.linke.ai',
            'Referer': 'https://guild.linke.ai/guild/anchorManage',
            'User-Agent': f'Mozilla/5.0 MCN-Automation GuildDashboard/1.0 Language/en Country/{country}',
        }
        proxy_url = self._resolve_executor_proxy_url(executor)
        proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None
        response = requests.get(
            f'https://api.linke.ai{path}',
            params=dict(ordered_params),
            headers=headers,
            proxies=proxies,
            timeout=max(5.0, min(float(timeout_seconds or 30), 60.0)),
        )
        try:
            payload = response.json()
        except Exception:
            payload = {'raw': response.text[:300]}
        if response.status_code >= 400:
            message = ''
            if isinstance(payload, dict):
                error = payload.get('error')
                if isinstance(error, dict):
                    message = str(error.get('message') or error.get('code') or '').strip()
                message = message or str(payload.get('message') or '').strip()
            raise RuntimeError(f'guild_api_http_{response.status_code}: {message or response.reason}')
        if isinstance(payload, dict) and payload.get('error'):
            error = payload.get('error')
            if isinstance(error, dict):
                raise RuntimeError(str(error.get('message') or error.get('code') or 'guild_api_error'))
            raise RuntimeError(str(error))
        if not isinstance(payload, dict):
            raise RuntimeError('guild_api_response_not_json_object')
        return payload

    def _timo_guild_api_post(
        self,
        *,
        executor: Dict[str, Any],
        path: str,
        payload: Dict[str, Any],
        timeout_seconds: float,
    ) -> Dict[str, Any]:
        ticket = str(executor.get('platform_authorization') or '').strip()
        if not ticket:
            raise RuntimeError('timo_ticket_missing')
        base_url = str(executor.get('platform_backend_url') or TIMO_DEFAULT_API_BASE_URL).strip().rstrip('/') or TIMO_DEFAULT_API_BASE_URL
        url = f'{base_url}/{str(path or "").strip().lstrip("/")}'
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'ticket': ticket,
            'lang': 'zh_TW',
            'Origin': 'https://www.timo.club',
            'Referer': 'https://www.timo.club/#/guildManagement/member-list',
            'User-Agent': 'Mozilla/5.0 MCN-Automation TimoGuildStats/1.0',
        }
        proxy_url = self._resolve_executor_proxy_url(executor)
        proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None
        response = requests.post(
            url,
            params={'distinctRequestId': uuid.uuid4().hex[:20]},
            json=dict(payload or {}),
            headers=headers,
            proxies=proxies,
            timeout=max(5.0, min(float(timeout_seconds or 30), 60.0)),
        )
        try:
            body = response.json()
        except Exception:
            body = {'raw': response.text[:300]}
        if response.status_code >= 400:
            raise RuntimeError(f'timo_guild_api_http_{response.status_code}')
        if isinstance(body, dict) and body.get('success') is False:
            code = str(body.get('code') or '').strip()
            message = str(body.get('msg') or body.get('message') or code or 'timo_api_rejected').strip()
            if code == 'ticketExpire':
                raise RuntimeError('timo_ticket_expired')
            raise RuntimeError(message)
        if not isinstance(body, dict):
            raise RuntimeError('timo_guild_api_response_not_json_object')
        return body

    def _sogo_guild_api_get(
        self,
        *,
        executor: Dict[str, Any],
        path: str,
        params: Dict[str, Any],
        timeout_seconds: float,
    ) -> Dict[str, Any]:
        authorization = str(executor.get('platform_authorization') or '').strip()
        if not authorization:
            raise RuntimeError('sogo_access_token_missing')
        auth_header = authorization if authorization.lower().startswith('bearer ') else f'Bearer {authorization}'
        base_url = str(executor.get('platform_backend_url') or SUGO_DEFAULT_API_BASE_URL).strip().rstrip('/') or SUGO_DEFAULT_API_BASE_URL
        url = f'{base_url}/{str(path or "").strip().lstrip("/")}'
        headers = {
            'Accept': 'application/json',
            'Authorization': auth_header,
            'Origin': 'https://union.sugo.com',
            'Referer': 'https://union.sugo.com/',
            'User-Agent': 'Mozilla/5.0 MCN-Automation SugoIntake/1.0',
        }
        proxy_url = self._resolve_executor_proxy_url(executor)
        proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None
        response = requests.get(
            url,
            params={k: v for k, v in dict(params or {}).items() if v not in (None, '')},
            headers=headers,
            proxies=proxies,
            timeout=max(5.0, min(float(timeout_seconds or 30), 60.0)),
        )
        try:
            body = response.json()
        except Exception:
            body = {'raw': response.text[:300]}
        if response.status_code >= 400:
            raise RuntimeError(f'sogo_guild_api_http_{response.status_code}')
        if isinstance(body, dict) and body.get('code') not in (None, 200, '200'):
            raise RuntimeError(str(body.get('msg') or body.get('message') or body.get('code') or 'sogo_api_rejected'))
        if not isinstance(body, dict):
            raise RuntimeError('sogo_guild_api_response_not_json_object')
        return body

    def _fetch_linky_guild_anchor_daily_count(
        self,
        *,
        executor: Dict[str, Any],
        stat_date: datetime.date,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        start_bj = datetime.combine(stat_date, datetime.min.time(), tzinfo=ZoneInfo('Asia/Shanghai'))
        end_bj = start_bj + timedelta(days=1)
        joined_count = 0
        scanned_count = 0
        page_count = 0
        total_anchors: Optional[int] = None
        timeout_seconds = float(executor.get('request_timeout_seconds') or 30)
        page_size = self.guild_anchor_daily_stats_page_size
        max_pages = self.guild_anchor_daily_stats_max_pages
        sort_direction = 'unknown'
        sort_confidence = 'low'
        last_page_max: Optional[datetime] = None
        sort_violations = 0
        def fetch_page(page: int) -> Dict[str, Any]:
            nonlocal page_count, total_anchors
            payload = self._linky_guild_api_signed_get(
                executor=executor,
                path='/api/guild/search_anchors',
                params={'page': page, 'page_size': page_size},
                timeout_seconds=timeout_seconds,
            )
            page_count += 1
            if total_anchors is None:
                try:
                    raw_total = payload.get('total_anchors')
                    if raw_total not in (None, ''):
                        total_anchors = max(0, int(raw_total))
                except Exception:
                    total_anchors = None
            return payload

        def page_created_times(payload: Dict[str, Any]) -> List[datetime]:
            times: List[datetime] = []
            items = payload.get('items') if isinstance(payload.get('items'), list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    created_epoch = int(float(item.get('created_at') or 0))
                except Exception:
                    continue
                if created_epoch <= 0:
                    continue
                times.append(datetime.fromtimestamp(created_epoch, tz=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')))
            return times

        first_payload = fetch_page(1)
        first_items = first_payload.get('items') if isinstance(first_payload.get('items'), list) else []
        if not first_items:
            return {
                'joined_count': 0,
                'total_anchors': total_anchors,
                'scanned_count': 0,
                'page_count': page_count,
                'sort_direction': sort_direction,
                'sort_confidence': sort_confidence,
                'status': 'success',
                'error': '',
            }
        effective_page_size = max(1, len(first_items))
        total_pages = max(1, math.ceil((total_anchors or len(first_items)) / effective_page_size))
        total_pages = min(total_pages, max_pages)
        first_times = page_created_times(first_payload)
        counted_pages: set[int] = set()
        last_items: List[Dict[str, Any]] = []
        for page in range(1, total_pages + 1):
            payload = first_payload if page == 1 else fetch_page(page)
            items = payload.get('items') if isinstance(payload.get('items'), list) else []
            if not items:
                break
            last_items = [item for item in items if isinstance(item, dict)]
            times = page_created_times(payload)
            self._record_guild_anchor_seen_rows(
                executor=executor,
                items=items,
                total_anchors=total_anchors,
                page=page,
                page_size=page_size,
            )
            if not times:
                scanned_count += len(items)
                counted_pages.add(page)
                continue
            page_max = max(times)
            if last_page_max is not None and page_max < last_page_max:
                sort_violations += 1
            last_page_max = page_max
            scanned_count += len(items)
            counted_pages.add(page)
            for created_bj in times:
                if start_bj <= created_bj < end_bj:
                    joined_count += 1
            if total_anchors is not None and scanned_count >= total_anchors:
                break
            if job_id:
                with self.db.connect() as conn:
                    progress_iso = utc_now()
                    renewed_lease_until = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=self.guild_anchor_daily_stats_job_lease_seconds)
                    ).isoformat()
                    conn.execute(
                        """
                        UPDATE guild_anchor_daily_stat_jobs
                        SET scanned_count = ?, page_count = ?, total_anchors = ?,
                            lease_until = ?, updated_at = ?
                        WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                        """,
                        (
                            scanned_count,
                            len(counted_pages),
                            total_anchors,
                            renewed_lease_until,
                            progress_iso,
                            job_id,
                            self._worker_id,
                        ),
                    )
                    conn.commit()
        if total_pages > 1 and first_times and last_page_max:
            sort_direction = 'ascending' if last_page_max >= max(first_times) else 'descending'
            sort_confidence = 'medium' if sort_violations == 0 else 'low'
        executor_key = self._guild_anchor_executor_key(executor)
        joined_count = self._count_seen_anchor_date(executor_key=executor_key, stat_date=stat_date.isoformat())
        real_person_count = self._count_seen_real_person_anchor_date(executor_key=executor_key, stat_date=stat_date.isoformat())
        self._upsert_guild_anchor_marker(
            executor=executor,
            total_anchors=total_anchors,
            marker_items=last_items,
            marker_page=len(counted_pages),
            marker_page_size=page_size,
            full_scan=True,
            sort_confidence=sort_confidence,
        )
        return {
            'joined_count': joined_count,
            'real_person_count': real_person_count,
            'total_anchors': total_anchors,
            'scanned_count': scanned_count,
            'page_count': len(counted_pages),
            'sort_direction': sort_direction,
            'sort_confidence': sort_confidence,
            'status': 'success',
            'error': '',
        }

    def _fetch_linky_guild_anchor_incremental_count(
        self,
        *,
        executor: Dict[str, Any],
        stat_date: datetime.date,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        executor_key = self._guild_anchor_executor_key(executor)
        timeout_seconds = float(executor.get('request_timeout_seconds') or 30)
        page_size = self.guild_anchor_daily_stats_page_size
        max_pages = self.guild_anchor_daily_stats_max_pages
        with self.db.connect() as conn:
            marker = conn.execute(
                "SELECT * FROM guild_anchor_scan_markers WHERE guild_executor_key = ?",
                (executor_key,),
            ).fetchone()
        if not marker or int(marker['last_total_anchors'] or 0) <= 0:
            return {'status': 'needs_full_scan', 'error': 'marker_missing'}

        page_count = 0
        scanned_count = 0
        total_anchors: Optional[int] = None

        def fetch_page(page: int) -> Dict[str, Any]:
            nonlocal page_count, total_anchors
            payload = self._linky_guild_api_signed_get(
                executor=executor,
                path='/api/guild/search_anchors',
                params={'page': page, 'page_size': page_size},
                timeout_seconds=timeout_seconds,
            )
            page_count += 1
            if total_anchors is None:
                try:
                    raw_total = payload.get('total_anchors')
                    if raw_total not in (None, ''):
                        total_anchors = max(0, int(raw_total))
                except Exception:
                    total_anchors = None
            return payload

        first_payload = fetch_page(1)
        first_items = first_payload.get('items') if isinstance(first_payload.get('items'), list) else []
        if not first_items:
            return {'status': 'success', 'joined_count': 0, 'total_anchors': total_anchors, 'scanned_count': 0, 'page_count': page_count, 'sort_confidence': 'marker_empty', 'sort_direction': 'marker'}
        previous_total = int(marker['last_total_anchors'] or 0)
        if total_anchors is not None and total_anchors < previous_total:
            return {'status': 'needs_full_scan', 'error': 'total_anchors_decreased', 'total_anchors': total_anchors, 'scanned_count': 0, 'page_count': page_count}
        if total_anchors is not None and total_anchors == previous_total:
            return {
                'joined_count': self._count_seen_anchor_date(executor_key=executor_key, stat_date=stat_date.isoformat()),
                'real_person_count': self._count_seen_real_person_anchor_date(executor_key=executor_key, stat_date=stat_date.isoformat()),
                'total_anchors': total_anchors,
                'scanned_count': len(first_items),
                'page_count': page_count,
                'sort_direction': 'marker',
                'sort_confidence': 'high',
                'status': 'success',
                'error': '',
            }
        effective_page_size = max(1, len(first_items))
        total_pages = max(1, math.ceil((total_anchors or len(first_items)) / effective_page_size))
        total_pages = min(total_pages, max_pages)
        new_anchor_count = 0
        last_items: List[Dict[str, Any]] = []
        expected_delta = max(0, int(total_anchors or previous_total) - previous_total)
        try:
            marker_ids = {
                str(value or '').strip()
                for value in json.loads(str(marker['marker_anchor_ids_json'] or '[]'))
                if str(value or '').strip()
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            marker_ids = set()
        observed_anchor_ids: set[str] = set()
        guard_pages = max(1, self.guild_anchor_daily_stats_guard_pages)
        try:
            marker_page = int(marker['marker_page'] or 0)
        except Exception:
            marker_page = 0
        previous_last_page = max(1, marker_page, math.ceil(previous_total / effective_page_size))
        delta_pages = max(1, math.ceil(expected_delta / effective_page_size)) if expected_delta > 0 else 1
        start_page = max(1, min(total_pages, previous_last_page) - guard_pages + 1)
        end_page = min(total_pages, max(start_page, previous_last_page + delta_pages + guard_pages))
        pages_to_scan = list(range(start_page, end_page + 1))

        def scan_tail_page(page: int) -> bool:
            nonlocal scanned_count, new_anchor_count, last_items
            payload = first_payload if page == 1 else fetch_page(page)
            items = payload.get('items') if isinstance(payload.get('items'), list) else []
            if not items:
                return False
            last_items = [item for item in items if isinstance(item, dict)]
            observed_anchor_ids.update(
                self._guild_anchor_anchor_id(item)
                for item in last_items
            )
            scanned_count += len(items)
            inserted_count = self._record_guild_anchor_seen_rows(
                executor=executor,
                items=items,
                total_anchors=total_anchors,
                page=page,
                page_size=page_size,
            )
            new_anchor_count += max(0, int(inserted_count or 0))
            if job_id:
                with self.db.connect() as progress_conn:
                    progress_iso = utc_now()
                    renewed_lease_until = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=self.guild_anchor_daily_stats_job_lease_seconds)
                    ).isoformat()
                    progress_conn.execute(
                        """
                        UPDATE guild_anchor_daily_stat_jobs
                        SET scanned_count = ?, page_count = ?, total_anchors = ?,
                            lease_until = ?, updated_at = ?
                        WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                        """,
                        (
                            scanned_count,
                            page_count,
                            total_anchors,
                            renewed_lease_until,
                            progress_iso,
                            job_id,
                            self._worker_id,
                        ),
                    )
                    progress_conn.commit()
            return True

        scanned_pages: set[int] = set()
        for page in pages_to_scan:
            if page_count >= max_pages:
                break
            if not scan_tail_page(page):
                break
            scanned_pages.add(page)

        marker_overlap = bool(marker_ids and marker_ids.intersection(observed_anchor_ids))
        if expected_delta > 0 and new_anchor_count < expected_delta and not marker_overlap:
            return {
                'status': 'needs_full_scan',
                'error': 'marker_tail_boundary_unverified',
                'total_anchors': total_anchors,
                'scanned_count': scanned_count,
                'page_count': page_count,
            }
        joined_count = self._count_seen_anchor_date(executor_key=executor_key, stat_date=stat_date.isoformat())
        real_person_count = self._count_seen_real_person_anchor_date(executor_key=executor_key, stat_date=stat_date.isoformat())
        self._upsert_guild_anchor_marker(
            executor=executor,
            total_anchors=total_anchors,
            marker_items=last_items,
            marker_page=total_pages,
            marker_page_size=page_size,
            full_scan=False,
            sort_confidence='marker_tail_overlap' if marker_overlap else 'marker_tail',
        )
        return {
            'joined_count': joined_count,
            'real_person_count': real_person_count,
            'total_anchors': total_anchors,
            'scanned_count': scanned_count,
            'page_count': page_count,
            'sort_direction': 'ascending',
            'sort_confidence': 'marker_tail_overlap' if marker_overlap else 'marker_tail',
            'status': 'success',
            'error': '',
        }

    def _fetch_linky_guild_anchor_bounded_date_count(
        self,
        *,
        executor: Dict[str, Any],
        stat_date: datetime.date,
        job_id: Optional[str] = None,
        fallback_reason: str = '',
    ) -> Dict[str, Any]:
        """Read only the date-bearing edge of Linky's ordered anchor pages.

        Scheduled jobs use this when an incremental marker cannot be trusted.
        It deliberately never turns a daily job into an implicit historical
        full scan. An operator must use source=full_scan for that.
        """
        start_bj = datetime.combine(stat_date, datetime.min.time(), tzinfo=ZoneInfo('Asia/Shanghai'))
        end_bj = start_bj + timedelta(days=1)
        executor_key = self._guild_anchor_executor_key(executor)
        timeout_seconds = float(executor.get('request_timeout_seconds') or 30)
        page_size = self.guild_anchor_daily_stats_page_size
        max_pages = self.guild_anchor_daily_stats_max_pages
        page_count = 0
        scanned_count = 0
        total_anchors: Optional[int] = None

        def fetch_page(page: int) -> Dict[str, Any]:
            nonlocal page_count, total_anchors
            payload = self._linky_guild_api_signed_get(
                executor=executor,
                path='/api/guild/search_anchors',
                params={'page': page, 'page_size': page_size},
                timeout_seconds=timeout_seconds,
            )
            page_count += 1
            if total_anchors is None:
                try:
                    raw_total = payload.get('total_anchors')
                    if raw_total not in (None, ''):
                        total_anchors = max(0, int(raw_total))
                except Exception:
                    total_anchors = None
            return payload

        def payload_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
            raw_items = payload.get('items') if isinstance(payload.get('items'), list) else []
            return [item for item in raw_items if isinstance(item, dict)]

        def item_time(item: Dict[str, Any]) -> Optional[datetime]:
            try:
                created_epoch = int(float(item.get('created_at') or 0))
            except Exception:
                return None
            if created_epoch <= 0:
                return None
            return datetime.fromtimestamp(created_epoch, tz=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))

        first_payload = fetch_page(1)
        first_items = payload_items(first_payload)
        if not first_items:
            return {
                'joined_count': 0,
                'real_person_count': 0,
                'total_anchors': total_anchors,
                'scanned_count': 0,
                'page_count': page_count,
                'sort_direction': 'date_edge',
                'sort_confidence': f'date_edge_empty:{str(fallback_reason or "marker_unavailable")[:80]}',
                'status': 'success',
                'error': '',
            }
        effective_page_size = max(1, len(first_items))
        total_pages = max(1, math.ceil((total_anchors or len(first_items)) / effective_page_size))
        if total_pages > max_pages:
            return {
                'status': 'needs_explicit_full_scan',
                'error': 'date_edge_page_budget_exceeded',
                'total_anchors': total_anchors,
                'scanned_count': 0,
                'page_count': page_count,
            }
        last_payload = first_payload if total_pages == 1 else fetch_page(total_pages)
        last_items = payload_items(last_payload)
        first_times = [value for value in (item_time(item) for item in first_items) if value is not None]
        last_times = [value for value in (item_time(item) for item in last_items) if value is not None]
        if not first_times or not last_times:
            return {
                'status': 'needs_explicit_full_scan',
                'error': 'date_edge_timestamp_missing',
                'total_anchors': total_anchors,
                'scanned_count': 0,
                'page_count': page_count,
            }
        if max(last_times) >= max(first_times):
            sort_direction = 'ascending'
            scan_pages = range(total_pages, 0, -1)
        else:
            sort_direction = 'descending'
            scan_pages = range(1, total_pages + 1)

        payload_cache = {1: first_payload, total_pages: last_payload}
        boundary_verified = False
        marker_items = last_items if sort_direction == 'ascending' else first_items
        for page in scan_pages:
            if page_count >= max_pages and page not in payload_cache:
                break
            payload = payload_cache.get(page) or fetch_page(page)
            items = payload_items(payload)
            if not items:
                break
            scanned_count += len(items)
            self._record_guild_anchor_seen_rows(
                executor=executor,
                items=items,
                total_anchors=total_anchors,
                page=page,
                page_size=page_size,
            )
            times = [value for value in (item_time(item) for item in items) if value is not None]
            if not times:
                return {
                    'status': 'needs_explicit_full_scan',
                    'error': 'date_edge_timestamp_missing',
                    'total_anchors': total_anchors,
                    'scanned_count': scanned_count,
                    'page_count': page_count,
                }
            page_min = min(times)
            page_max = max(times)
            if sort_direction == 'ascending' and page_max < start_bj:
                boundary_verified = True
                break
            if sort_direction == 'descending' and page_min >= end_bj:
                continue
            if sort_direction == 'descending' and page_max < start_bj:
                boundary_verified = True
                break
            if job_id:
                with self.db.connect() as progress_conn:
                    progress_iso = utc_now()
                    renewed_lease_until = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=self.guild_anchor_daily_stats_job_lease_seconds)
                    ).isoformat()
                    progress_conn.execute(
                        """
                        UPDATE guild_anchor_daily_stat_jobs
                        SET scanned_count = ?, page_count = ?, total_anchors = ?,
                            lease_until = ?, updated_at = ?
                        WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                        """,
                        (
                            scanned_count,
                            page_count,
                            total_anchors,
                            renewed_lease_until,
                            progress_iso,
                            job_id,
                            self._worker_id,
                        ),
                    )
                    progress_conn.commit()
        if not boundary_verified:
            return {
                'status': 'needs_explicit_full_scan',
                'error': 'date_edge_boundary_unverified',
                'total_anchors': total_anchors,
                'scanned_count': scanned_count,
                'page_count': page_count,
            }
        joined_count = self._count_seen_anchor_date(
            executor_key=executor_key,
            stat_date=stat_date.isoformat(),
        )
        real_person_count = self._count_seen_real_person_anchor_date(
            executor_key=executor_key,
            stat_date=stat_date.isoformat(),
        )
        self._upsert_guild_anchor_marker(
            executor=executor,
            total_anchors=total_anchors,
            marker_items=marker_items,
            marker_page=total_pages if sort_direction == 'ascending' else 1,
            marker_page_size=page_size,
            full_scan=False,
            sort_confidence='date_edge_bounded',
        )
        return {
            'joined_count': joined_count,
            'real_person_count': real_person_count,
            'total_anchors': total_anchors,
            'scanned_count': scanned_count,
            'page_count': page_count,
            'sort_direction': sort_direction,
            'sort_confidence': f'date_edge_bounded:{str(fallback_reason or "marker_unavailable")[:80]}',
            'status': 'success',
            'error': '',
        }

    def _fetch_timo_guild_host_page(
        self,
        *,
        executor: Dict[str, Any],
        page: int,
        page_size: int,
        timeout_seconds: float,
    ) -> Dict[str, Any]:
        payload = {
            'uuid': str(executor.get('cms_guild_sid') or executor.get('cms_guild_id') or '').strip(),
            'pageNum': max(1, int(page or 1)),
            'pageSize': max(1, int(page_size or 100)),
            'userId': '',
            'status': '',
            'queryRole': '',
            'gender': '',
            'startTime': '',
            'endTime': '',
            'isRealPerson': '',
        }
        body = self._timo_guild_api_post(
            executor=executor,
            path='website-frontend/v1/officalWebGuild/getHostList',
            payload=payload,
            timeout_seconds=timeout_seconds,
        )
        data = body.get('data') if isinstance(body.get('data'), list) else []
        total = None
        for candidate in (body, body.get('info') if isinstance(body.get('info'), dict) else None):
            if not isinstance(candidate, dict):
                continue
            for key in ('total', 'totalCount', 'totalNum', 'count'):
                try:
                    raw_total = candidate.get(key)
                    if raw_total not in (None, ''):
                        total = max(0, int(raw_total))
                        break
                except Exception:
                    continue
            if total is not None:
                break
        if total is None and isinstance(body.get('info'), dict):
            nested_data = body['info'].get('data')
            if isinstance(nested_data, list):
                data = nested_data
        return {'items': [item for item in data if isinstance(item, dict)], 'total_anchors': total}

    def _fetch_timo_guild_anchor_daily_count(
        self,
        *,
        executor: Dict[str, Any],
        stat_date: datetime.date,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        timeout_seconds = float(executor.get('request_timeout_seconds') or 30)
        page_size = self.guild_anchor_daily_stats_page_size
        max_pages = self.guild_anchor_daily_stats_max_pages
        scanned_count = 0
        page_count = 0
        total_anchors: Optional[int] = None
        last_items: List[Dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            payload = self._fetch_timo_guild_host_page(executor=executor, page=page, page_size=page_size, timeout_seconds=timeout_seconds)
            items = payload.get('items') if isinstance(payload.get('items'), list) else []
            if total_anchors is None:
                total_anchors = payload.get('total_anchors')
            if not items:
                break
            last_items = [item for item in items if isinstance(item, dict)]
            scanned_count += len(items)
            page_count += 1
            self._record_guild_anchor_seen_rows(
                executor=executor,
                items=items,
                total_anchors=total_anchors,
                page=page,
                page_size=page_size,
            )
            if job_id:
                with self.db.connect() as conn:
                    progress_iso = utc_now()
                    renewed_lease_until = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=self.guild_anchor_daily_stats_job_lease_seconds)
                    ).isoformat()
                    conn.execute(
                        """
                        UPDATE guild_anchor_daily_stat_jobs
                        SET scanned_count = ?, page_count = ?, total_anchors = ?,
                            lease_until = ?, updated_at = ?
                        WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                        """,
                        (
                            scanned_count,
                            page_count,
                            total_anchors,
                            renewed_lease_until,
                            progress_iso,
                            job_id,
                            self._worker_id,
                        ),
                    )
                    conn.commit()
            if total_anchors is not None and scanned_count >= total_anchors:
                break
            if len(items) < page_size:
                break
        executor_key = self._guild_anchor_executor_key(executor)
        joined_count = self._count_seen_anchor_date_bj(executor_key=executor_key, stat_date=stat_date.isoformat())
        real_person_count = self._count_seen_real_person_anchor_date_bj(executor_key=executor_key, stat_date=stat_date.isoformat())
        effective_total_anchors = total_anchors if total_anchors is not None else scanned_count
        self._upsert_guild_anchor_marker(
            executor=executor,
            total_anchors=effective_total_anchors,
            marker_items=last_items,
            marker_page=page_count,
            marker_page_size=page_size,
            full_scan=True,
            sort_confidence='timo_full_scan',
        )
        return {
            'joined_count': joined_count,
            'real_person_count': real_person_count,
            'total_anchors': effective_total_anchors,
            'scanned_count': scanned_count,
            'page_count': page_count,
            'sort_direction': 'timo_api',
            'sort_confidence': 'timo_full_scan',
            'status': 'success',
            'error': '',
        }

    def _fetch_timo_guild_anchor_incremental_count(
        self,
        *,
        executor: Dict[str, Any],
        stat_date: datetime.date,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        executor_key = self._guild_anchor_executor_key(executor)
        timeout_seconds = float(executor.get('request_timeout_seconds') or 30)
        page_size = self.guild_anchor_daily_stats_page_size
        max_pages = self.guild_anchor_daily_stats_max_pages
        with self.db.connect() as conn:
            marker = conn.execute(
                "SELECT * FROM guild_anchor_scan_markers WHERE guild_executor_key = ?",
                (executor_key,),
            ).fetchone()
        if not marker or int(marker['last_total_anchors'] or 0) <= 0:
            return {'status': 'needs_full_scan', 'error': 'marker_missing'}
        first_payload = self._fetch_timo_guild_host_page(executor=executor, page=1, page_size=page_size, timeout_seconds=timeout_seconds)
        total_anchors = first_payload.get('total_anchors')
        first_items = first_payload.get('items') if isinstance(first_payload.get('items'), list) else []
        previous_total = int(marker['last_total_anchors'] or 0)
        if total_anchors is not None and total_anchors < previous_total:
            return {'status': 'needs_full_scan', 'error': 'total_anchors_decreased', 'total_anchors': total_anchors, 'scanned_count': 0, 'page_count': 1}
        if total_anchors is not None and total_anchors == previous_total:
            return {
                'joined_count': self._count_seen_anchor_date_bj(executor_key=executor_key, stat_date=stat_date.isoformat()),
                'real_person_count': self._count_seen_real_person_anchor_date_bj(executor_key=executor_key, stat_date=stat_date.isoformat()),
                'total_anchors': total_anchors,
                'scanned_count': len(first_items),
                'page_count': 1,
                'sort_direction': 'timo_api',
                'sort_confidence': 'marker',
                'status': 'success',
                'error': '',
            }
        expected_delta = max(0, int(total_anchors or previous_total) - previous_total)
        pages_to_scan = max(1, min(max_pages, math.ceil(max(1, expected_delta) / max(1, len(first_items) or page_size)) + self.guild_anchor_daily_stats_guard_pages))
        scanned_count = 0
        page_count = 0
        last_items: List[Dict[str, Any]] = []
        for page in range(1, pages_to_scan + 1):
            payload = first_payload if page == 1 else self._fetch_timo_guild_host_page(executor=executor, page=page, page_size=page_size, timeout_seconds=timeout_seconds)
            items = payload.get('items') if isinstance(payload.get('items'), list) else []
            if not items:
                break
            last_items = [item for item in items if isinstance(item, dict)]
            scanned_count += len(items)
            page_count += 1
            self._record_guild_anchor_seen_rows(
                executor=executor,
                items=items,
                total_anchors=total_anchors,
                page=page,
                page_size=page_size,
            )
            if job_id:
                with self.db.connect() as conn:
                    progress_iso = utc_now()
                    renewed_lease_until = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=self.guild_anchor_daily_stats_job_lease_seconds)
                    ).isoformat()
                    conn.execute(
                        """
                        UPDATE guild_anchor_daily_stat_jobs
                        SET scanned_count = ?, page_count = ?, total_anchors = ?,
                            lease_until = ?, updated_at = ?
                        WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                        """,
                        (
                            scanned_count,
                            page_count,
                            total_anchors,
                            renewed_lease_until,
                            progress_iso,
                            job_id,
                            self._worker_id,
                        ),
                    )
                    conn.commit()
        joined_count = self._count_seen_anchor_date_bj(executor_key=executor_key, stat_date=stat_date.isoformat())
        real_person_count = self._count_seen_real_person_anchor_date_bj(executor_key=executor_key, stat_date=stat_date.isoformat())
        effective_total_anchors = total_anchors if total_anchors is not None else max(previous_total, self._count_seen_anchor_total(executor_key=executor_key))
        self._upsert_guild_anchor_marker(
            executor=executor,
            total_anchors=effective_total_anchors,
            marker_items=last_items,
            marker_page=page_count,
            marker_page_size=page_size,
            full_scan=False,
            sort_confidence='timo_marker_tail',
        )
        return {
            'joined_count': joined_count,
            'real_person_count': real_person_count,
            'total_anchors': effective_total_anchors,
            'scanned_count': scanned_count,
            'page_count': page_count,
            'sort_direction': 'timo_api',
            'sort_confidence': 'timo_marker_tail',
            'status': 'success',
            'error': '',
        }

    def _backfill_timo_guild_anchor_daily_stats_from_seen(
        self,
        *,
        executor: Dict[str, Any],
        date_to: Optional[datetime.date] = None,
    ) -> Dict[str, Any]:
        executor_key = self._guild_anchor_executor_key(executor)
        guild_name = str(executor.get('guild_name') or '').strip()
        if not executor_key or not guild_name:
            return {'ok': False, 'reason': 'executor_missing'}
        with self.db.connect() as conn:
            aggregate_rows = [dict(row) for row in conn.execute(
                """
                SELECT created_date_bj AS stat_date,
                       COUNT(*) AS joined_count,
                       SUM(CASE WHEN COALESCE(is_real_person, 0) = 1 THEN 1 ELSE 0 END) AS real_person_count
                FROM guild_anchor_seen
                WHERE guild_executor_key = ?
                  AND COALESCE(created_date_bj, '') != ''
                GROUP BY created_date_bj
                ORDER BY created_date_bj ASC
                """,
                (executor_key,),
            ).fetchall()]
            marker = conn.execute(
                "SELECT last_total_anchors, marker_page FROM guild_anchor_scan_markers WHERE guild_executor_key = ?",
                (executor_key,),
            ).fetchone()
            total_seen = conn.execute(
                "SELECT COUNT(*) AS count FROM guild_anchor_seen WHERE guild_executor_key = ?",
                (executor_key,),
            ).fetchone()
        if not aggregate_rows:
            return {'ok': True, 'guild_name': guild_name, 'backfilled_count': 0, 'reason': 'no_seen_rows'}
        first_day = datetime.strptime(str(aggregate_rows[0]['stat_date']), '%Y-%m-%d').date()
        end_day = date_to or self._timo_revenue_latest_complete_day_bj()
        if end_day < first_day:
            end_day = first_day
        counts_by_date = {
            str(row.get('stat_date') or ''): {
                'joined_count': int(row.get('joined_count') or 0),
                'real_person_count': int(row.get('real_person_count') or 0),
            }
            for row in aggregate_rows
        }
        marker_total = int(marker['last_total_anchors'] or 0) if marker else 0
        marker_page = int(marker['marker_page'] or 0) if marker else 0
        seen_total = int(total_seen['count'] or 0) if total_seen else 0
        total_anchors = int(marker_total or seen_total or 0)
        page_count = int(marker_page or 0)
        scanned_count = int(seen_total or 0)
        stale_after = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        refreshed_at = utc_now()
        cursor = first_day
        rows_to_write: List[Tuple[Any, ...]] = []
        while cursor <= end_day:
            stat_date = cursor.isoformat()
            counts = counts_by_date.get(stat_date) or {'joined_count': 0, 'real_person_count': 0}
            rows_to_write.append((
                executor_key,
                guild_name,
                guild_name,
                stat_date,
                int(counts.get('joined_count') or 0),
                int(counts.get('real_person_count') or 0),
                total_anchors,
                scanned_count,
                page_count,
                'timo_api',
                'timo_seen_backfill',
                stale_after,
                'success',
                '',
                refreshed_at,
            ))
            cursor += timedelta(days=1)
        with self.db.connect() as conn:
            conn.executemany(
                """
                INSERT INTO guild_anchor_daily_stats (
                    guild_executor_key, guild_name, guild_display_name, stat_date, joined_count, real_person_count, total_anchors,
                    scanned_count, page_count, sort_direction, sort_confidence, stale_after, status, error, refreshed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_name, stat_date) DO UPDATE SET
                    guild_executor_key = excluded.guild_executor_key,
                    guild_display_name = excluded.guild_display_name,
                    joined_count = excluded.joined_count,
                    real_person_count = excluded.real_person_count,
                    total_anchors = excluded.total_anchors,
                    scanned_count = excluded.scanned_count,
                    page_count = excluded.page_count,
                    sort_direction = excluded.sort_direction,
                    sort_confidence = excluded.sort_confidence,
                    stale_after = excluded.stale_after,
                    status = excluded.status,
                    error = excluded.error,
                    refreshed_at = excluded.refreshed_at
                """,
                rows_to_write,
            )
            conn.commit()
        return {
            'ok': True,
            'guild_name': guild_name,
            'date_from': first_day.isoformat(),
            'date_to': end_day.isoformat(),
            'backfilled_count': len(rows_to_write),
            'total_anchors': total_anchors,
            'scanned_count': scanned_count,
        }

    def _bootstrap_timo_guild_executor_global_data(self, executor: Dict[str, Any]) -> Dict[str, Any]:
        today_bj = datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date()
        anchor_stats_day = today_bj - timedelta(days=1)
        export_cache_day = self._timo_revenue_latest_complete_day_bj()
        scan_result = self._fetch_timo_guild_anchor_daily_count(executor=executor, stat_date=anchor_stats_day)
        stats_result = self._backfill_timo_guild_anchor_daily_stats_from_seen(executor=executor, date_to=anchor_stats_day)
        cache_results: Dict[str, Any] = {}
        guild_name = str(executor.get('guild_name') or '').strip()
        try:
            cache_results['real_person'] = self.materialize_timo_real_person_ids_cache(
                guild_name=guild_name,
                user={'role': 'admin'},
                as_of_date_bj=export_cache_day.isoformat(),
                refresh_anchor_cache=False,
            )
        except Exception as exc:
            cache_results['real_person'] = {'ok': False, 'error': str(exc)[:240]}
        if str(executor.get('cms_guild_id') or '').strip() and str(executor.get('cms_guild_sid') or '').strip():
            for period in ('yesterday', 'last_week'):
                try:
                    cache_results[f'revenue_{period}'] = self.materialize_timo_revenue_export_cache(
                        guild_name=guild_name,
                        user={'role': 'admin'},
                        period=period,
                    )
                except Exception as exc:
                    cache_results[f'revenue_{period}'] = {'ok': False, 'error': str(exc)[:240]}
            try:
                cache_results['first_20k_diamonds'] = self.materialize_timo_first_20k_diamonds_cache(
                    guild_name=guild_name,
                    user={'role': 'admin'},
                    as_of_date_bj=export_cache_day.isoformat(),
                    refresh_anchor_cache=False,
                )
            except Exception as exc:
                cache_results['first_20k_diamonds'] = {'ok': False, 'error': str(exc)[:240]}
        else:
            cache_results['revenue_exports'] = {'ok': False, 'error': 'timo_guild_lock_required'}
        return {
            'ok': True,
            'guild_name': guild_name,
            'anchor_stats_day': anchor_stats_day.isoformat(),
            'export_cache_day': export_cache_day.isoformat(),
            'scan': scan_result,
            'stats': stats_result,
            'cache': cache_results,
        }

    def _start_timo_guild_executor_global_bootstrap(self, executor: Dict[str, Any]) -> Dict[str, Any]:
        if self.db.db_path == ':memory:':
            return {'ok': True, 'accepted': False, 'reason': 'memory_db_skip'}
        executor_key = self._guild_anchor_executor_key(executor)
        if not executor_key:
            return {'ok': False, 'accepted': False, 'reason': 'executor_key_missing'}
        script_path = PROJECT_ROOT / 'scripts' / 'timo_guild_bootstrap_events.py'
        if not script_path.exists():
            return {'ok': False, 'accepted': False, 'reason': 'bootstrap_script_missing'}
        log_path = PROJECT_ROOT / 'logs' / 'timo_guild_bootstrap.log'
        log_handle = None
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_path.open('a', encoding='utf-8')
            process = subprocess.Popen(
                [sys.executable, str(script_path), 'enqueue', '--guild-name', str(executor.get('guild_name') or '')],
                cwd=str(PROJECT_ROOT),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            log_handle.close()
            return {
                'ok': True,
                'accepted': True,
                'already_running': False,
                'supervisor': 'p2_event_enqueue',
                'pid': process.pid,
            }
        except Exception as exc:
            if log_handle is not None:
                try:
                    log_handle.close()
                except Exception:
                    pass
            return {'ok': False, 'accepted': False, 'reason': str(exc)[:240]}

    def enqueue_guild_anchor_daily_stat_jobs(
        self,
        *,
        stat_dates: List[str],
        source: str = 'schedule',
        force: bool = False,
        guild_names: Optional[List[str]] = None,
        app_names: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        beijing_tz = ZoneInfo('Asia/Shanghai')
        today_bj = datetime.now(timezone.utc).astimezone(beijing_tz).date()
        normalized_dates = []
        for value in stat_dates:
            parsed = self._parse_anchor_stat_date(value, fallback=today_bj - timedelta(days=1))
            if parsed >= today_bj and not force:
                raise HTTPException(status_code=400, detail='只能刷新北京时间今天以前的日期')
            if parsed.isoformat() not in normalized_dates:
                normalized_dates.append(parsed.isoformat())
        executor_rows = self._list_enabled_guild_anchor_executors()
        requested_app_names = {
            str(name or '').strip().lower()
            for name in (app_names or [])
            if str(name or '').strip()
        }
        if requested_app_names:
            executor_rows = [
                executor for executor in executor_rows
                if str(executor.get('app_name') or 'linky').strip().lower() in requested_app_names
            ]
        requested_guild_names = {
            str(name or '').strip()
            for name in (guild_names or [])
            if str(name or '').strip()
        }
        if requested_guild_names:
            executor_rows = [
                executor for executor in executor_rows
                if str(executor.get('guild_name') or '').strip() in requested_guild_names
            ]
        now_iso = utc_now()
        enqueued = 0
        with self.db.connect() as conn:
            for executor in executor_rows:
                guild_name = str(executor.get('guild_name') or '').strip()
                if not guild_name:
                    continue
                executor_key = self._guild_anchor_executor_key(executor)
                for stat_date in normalized_dates:
                    job_id = hashlib.sha1(f'{executor_key}:{stat_date}'.encode('utf-8')).hexdigest()
                    existing_job = conn.execute(
                        "SELECT source FROM guild_anchor_daily_stat_jobs WHERE guild_executor_key = ? AND stat_date = ?",
                        (executor_key, stat_date),
                    ).fetchone()
                    scheduled_timo_refresh = (
                        source == 'schedule'
                        and str(executor.get('app_name') or '').strip().lower() == 'timo'
                        and existing_job is not None
                        and str(existing_job['source'] or '').strip() != 'schedule'
                    )
                    reset_existing = bool(force or scheduled_timo_refresh)
                    conn.execute(
                        """
                        INSERT INTO guild_anchor_daily_stat_jobs (
                            job_id, guild_executor_key, guild_name, stat_date, status, attempt_count, max_attempts,
                            next_retry_at, lease_owner, lease_until, started_at, finished_at, scanned_count, page_count,
                            total_anchors, joined_count, sort_direction, sort_confidence, error, source, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'pending', 0, ?, '', '', '', '', '', 0, 0, NULL, NULL, '', '', '', ?, ?, ?)
                        ON CONFLICT(guild_executor_key, stat_date) DO UPDATE SET
                            guild_name = excluded.guild_name,
                            status = CASE
                                WHEN ? THEN 'pending'
                                ELSE guild_anchor_daily_stat_jobs.status
                            END,
                            attempt_count = CASE WHEN ? THEN 0 ELSE guild_anchor_daily_stat_jobs.attempt_count END,
                            next_retry_at = CASE WHEN ? THEN '' ELSE guild_anchor_daily_stat_jobs.next_retry_at END,
                            lease_owner = CASE WHEN ? THEN '' ELSE guild_anchor_daily_stat_jobs.lease_owner END,
                            lease_until = CASE WHEN ? THEN '' ELSE guild_anchor_daily_stat_jobs.lease_until END,
                            started_at = CASE WHEN ? THEN '' ELSE guild_anchor_daily_stat_jobs.started_at END,
                            finished_at = CASE WHEN ? THEN '' ELSE guild_anchor_daily_stat_jobs.finished_at END,
                            error = CASE WHEN ? THEN '' ELSE guild_anchor_daily_stat_jobs.error END,
                            last_error = CASE
                                WHEN ? THEN guild_anchor_daily_stat_jobs.error
                                ELSE guild_anchor_daily_stat_jobs.last_error
                            END,
                            recovery_count = CASE WHEN ? THEN 0 ELSE guild_anchor_daily_stat_jobs.recovery_count END,
                            last_recovered_at = CASE WHEN ? THEN '' ELSE guild_anchor_daily_stat_jobs.last_recovered_at END,
                            source = CASE WHEN ? THEN excluded.source ELSE guild_anchor_daily_stat_jobs.source END,
                            updated_at = CASE WHEN ? THEN excluded.updated_at ELSE guild_anchor_daily_stat_jobs.updated_at END
                        """,
                        (
                            job_id,
                            executor_key,
                            guild_name,
                            stat_date,
                            self.guild_anchor_daily_stats_job_max_attempts,
                            source,
                            now_iso,
                            now_iso,
                            1 if reset_existing else 0,
                            1 if reset_existing else 0,
                            1 if reset_existing else 0,
                            1 if reset_existing else 0,
                            1 if reset_existing else 0,
                            1 if reset_existing else 0,
                            1 if reset_existing else 0,
                            1 if reset_existing else 0,
                            1 if reset_existing else 0,
                            1 if reset_existing else 0,
                            1 if reset_existing else 0,
                            1 if reset_existing else 0,
                            1 if reset_existing else 0,
                        ),
                    )
                    enqueued += 1
            conn.commit()
        return {'ok': True, 'dates': normalized_dates, 'executor_count': len(executor_rows), 'job_count': enqueued}

    def _resolve_guild_anchor_executor_for_job(self, job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        target_key = str(job.get('guild_executor_key') or '').strip()
        target_name = str(job.get('guild_name') or '').strip()
        for executor in self._list_enabled_guild_anchor_executors():
            if self._guild_anchor_executor_key(executor) == target_key:
                return executor
        for executor in self._list_enabled_guild_anchor_executors():
            if str(executor.get('guild_name') or '').strip() == target_name:
                return executor
        return None

    def _guild_anchor_daily_stats_worker_pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    def _guild_anchor_daily_stats_dead_lease_owner(self, lease_owner: Any) -> bool:
        owner = str(lease_owner or '').strip()
        match = re.match(r'^worker-(\d+)-', owner)
        if not match:
            return False
        try:
            pid = int(match.group(1))
        except ValueError:
            return False
        if pid <= 0 or pid == os.getpid():
            return False
        return not self._guild_anchor_daily_stats_worker_pid_alive(pid)

    def _guild_anchor_daily_stats_max_recovery_cycles(self) -> int:
        try:
            configured = int(os.getenv('GUILD_ANCHOR_DAILY_STATS_MAX_RECOVERY_CYCLES') or 2)
        except (TypeError, ValueError):
            configured = 2
        return max(1, min(5, configured))

    def _acquire_guild_anchor_daily_stats_deploy_guard(self):
        lock_path = Path(os.getenv('MCN_DEPLOY_LOCK_PATH') or '/var/lock/mcn-deploy.lock')
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open('a+')
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            return handle
        except (BlockingIOError, OSError):
            try:
                handle.close()
            except Exception:
                pass
            return None

    @staticmethod
    def _release_guild_anchor_daily_stats_deploy_guard(handle: Any) -> None:
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def reclaim_guild_anchor_daily_stat_jobs_from_dead_workers(
        self, *, limit: int = 50, app_name: str = '',
    ) -> Dict[str, Any]:
        scan_limit = max(1, min(200, int(limit or 50)))
        normalized_app = str(app_name or '').strip().lower()
        app_filter = ''
        query_params: List[Any] = []
        if normalized_app:
            app_filter = ' AND guild_executor_key LIKE ?'
            query_params.append(f'{normalized_app}:%')
        query_params.append(scan_limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT job_id, lease_owner, attempt_count, max_attempts, recovery_count, error
                FROM guild_anchor_daily_stat_jobs
                WHERE status = 'running' AND lease_owner != ''
                  {app_filter}
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                tuple(query_params),
            ).fetchall()
        reclaim_job_ids = [
            str(row['job_id'])
            for row in rows
            if self._guild_anchor_daily_stats_dead_lease_owner(row['lease_owner'])
        ]
        if not reclaim_job_ids:
            return {'ok': True, 'reclaimed_count': 0, 'job_ids': []}
        now_iso = utc_now()
        max_recovery_cycles = self._guild_anchor_daily_stats_max_recovery_cycles()
        reclaimed_rows = [
            row for row in rows
            if str(row['job_id']) in set(reclaim_job_ids)
        ]
        with self.db.connect() as conn:
            for row in reclaimed_rows:
                attempt_count = int(row['attempt_count'] or 0)
                max_attempts = int(row['max_attempts'] or self.guild_anchor_daily_stats_job_max_attempts)
                recovery_count = int(row['recovery_count'] or 0)
                exhausted = attempt_count >= max_attempts
                can_recover = not exhausted or recovery_count < max_recovery_cycles
                next_status = 'pending' if can_recover else 'dead'
                next_attempt_count = 0 if exhausted and can_recover else attempt_count
                next_recovery_count = recovery_count + 1 if exhausted and can_recover else recovery_count
                diagnostic = (
                    f'worker_process_lost:{row["lease_owner"]};'
                    f'attempt={attempt_count}/{max_attempts};'
                    f'recovery_cycle={next_recovery_count}/{max_recovery_cycles}'
                )
                conn.execute(
                    """
                    UPDATE guild_anchor_daily_stat_jobs
                    SET status = ?,
                        attempt_count = ?,
                        recovery_count = ?,
                        last_recovered_at = CASE WHEN ? THEN ? ELSE last_recovered_at END,
                        lease_owner = '',
                        lease_until = '',
                        started_at = '',
                        finished_at = '',
                        next_retry_at = '',
                        scanned_count = 0,
                        page_count = 0,
                        total_anchors = NULL,
                        joined_count = NULL,
                        real_person_count = NULL,
                        sort_direction = '',
                        sort_confidence = '',
                        last_error = CASE WHEN error != '' THEN error ELSE ? END,
                        error = ?,
                        updated_at = ?
                    WHERE status = 'running' AND job_id = ?
                    """,
                    (
                        next_status,
                        next_attempt_count,
                        next_recovery_count,
                        1 if exhausted and can_recover else 0,
                        now_iso,
                        diagnostic,
                        diagnostic,
                        now_iso,
                        row['job_id'],
                    ),
                )
            conn.commit()
        return {'ok': True, 'reclaimed_count': len(reclaim_job_ids), 'job_ids': reclaim_job_ids}

    def auto_recover_guild_anchor_daily_stat_jobs(
        self, *, limit: int = 50, app_name: str = '',
    ) -> Dict[str, Any]:
        recovery_limit = max(1, min(200, int(limit or 50)))
        max_recovery_cycles = self._guild_anchor_daily_stats_max_recovery_cycles()
        now_iso = utc_now()
        normalized_app = str(app_name or '').strip().lower()
        app_filter = ''
        query_params: List[Any] = [max_recovery_cycles]
        if normalized_app:
            app_filter = ' AND jobs.guild_executor_key LIKE ?'
            query_params.append(f'{normalized_app}:%')
        query_params.append(recovery_limit)
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute(
                f"""
                SELECT job_id, status, attempt_count, max_attempts, recovery_count, error
                FROM guild_anchor_daily_stat_jobs AS jobs
                WHERE (
                    jobs.status = 'dead'
                    OR (jobs.status IN ('pending', 'retry_waiting') AND jobs.attempt_count >= jobs.max_attempts)
                )
                  AND jobs.recovery_count < ?
                  {app_filter}
                  AND NOT EXISTS (
                      SELECT 1
                      FROM guild_anchor_daily_stats AS stats
                      WHERE stats.guild_name = jobs.guild_name
                        AND stats.stat_date = jobs.stat_date
                        AND stats.status = 'success'
                  )
                ORDER BY jobs.updated_at ASC
                LIMIT ?
                """,
                tuple(query_params),
            ).fetchall()
            recovered_job_ids: List[str] = []
            for row in rows:
                recovery_count = int(row['recovery_count'] or 0) + 1
                diagnostic = (
                    f'automatic_recovery:previous_status={row["status"]};'
                    f'attempt={int(row["attempt_count"] or 0)}/{int(row["max_attempts"] or 0)};'
                    f'recovery_cycle={recovery_count}/{max_recovery_cycles}'
                )
                conn.execute(
                    """
                    UPDATE guild_anchor_daily_stat_jobs
                    SET status = 'pending',
                        attempt_count = 0,
                        recovery_count = ?,
                        last_recovered_at = ?,
                        next_retry_at = '',
                        lease_owner = '',
                        lease_until = '',
                        started_at = '',
                        finished_at = '',
                        last_error = CASE WHEN error != '' THEN error ELSE last_error END,
                        error = ?,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (recovery_count, now_iso, diagnostic, now_iso, row['job_id']),
                )
                recovered_job_ids.append(str(row['job_id']))
            conn.commit()
        return {
            'ok': True,
            'recovered_count': len(recovered_job_ids),
            'job_ids': recovered_job_ids,
            'max_recovery_cycles': max_recovery_cycles,
        }

    def _claim_next_guild_anchor_daily_stat_job(self, *, app_name: str = '') -> Optional[Dict[str, Any]]:
        now_iso = utc_now()
        lease_until = (datetime.now(timezone.utc) + timedelta(seconds=self.guild_anchor_daily_stats_job_lease_seconds)).isoformat()
        normalized_app = str(app_name or '').strip().lower()
        app_filter = ''
        query_params: List[Any] = [now_iso]
        if normalized_app:
            app_filter = ' AND guild_executor_key LIKE ?'
            query_params.append(f'{normalized_app}:%')
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                f"""
                SELECT *
                FROM guild_anchor_daily_stat_jobs
                WHERE (
                    status = 'pending'
                    OR (status = 'retry_waiting' AND (next_retry_at = '' OR next_retry_at <= ?))
                )
                  AND attempt_count < max_attempts
                  {app_filter}
                ORDER BY stat_date DESC, updated_at ASC
                LIMIT 1
                """,
                tuple(query_params),
            ).fetchone()
            if not row:
                conn.commit()
                return None
            job = dict(row)
            conn.execute(
                """
                UPDATE guild_anchor_daily_stat_jobs
                SET status = 'running',
                    attempt_count = attempt_count + 1,
                    lease_owner = ?,
                    lease_until = ?,
                    started_at = ?,
                    finished_at = '',
                    error = '',
                    updated_at = ?
                WHERE job_id = ?
                """,
                (self._worker_id, lease_until, now_iso, now_iso, job['job_id']),
            )
            conn.commit()
            job['attempt_count'] = int(job.get('attempt_count') or 0) + 1
            return job

    def _finish_guild_anchor_daily_stat_job(self, job: Dict[str, Any], result: Dict[str, Any], *, executor: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        finished_iso = utc_now()
        stat_date = str(job.get('stat_date') or '').strip()
        guild_name = str((executor or {}).get('guild_name') or job.get('guild_name') or '').strip()
        executor_key = str((self._guild_anchor_executor_key(executor) if executor else job.get('guild_executor_key')) or '').strip()
        stale_after = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        status = str(result.get('status') or 'success')
        error = str(result.get('error') or '')
        joined_count = int(result.get('joined_count') or 0)
        deploy_guard = self._acquire_guild_anchor_daily_stats_deploy_guard()
        if deploy_guard is None:
            raise RuntimeError('deploy_in_progress_before_guild_anchor_commit')
        try:
            with self.db.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current_job = conn.execute(
                """
                SELECT status, lease_owner
                FROM guild_anchor_daily_stat_jobs
                WHERE job_id = ?
                """,
                (job.get('job_id'),),
            ).fetchone()
                if (
                    not current_job
                    or str(current_job['status'] or '') != 'running'
                    or str(current_job['lease_owner'] or '') != self._worker_id
                ):
                    conn.rollback()
                    raise RuntimeError('guild_anchor_daily_stat_job_lease_lost')
                executor_app = str((executor or {}).get('app_name') or 'linky').strip().lower()
                if status == 'success' and executor_app == 'linky':
                    joined_count = self._freeze_linky_newcomer_identity_snapshot(
                    conn,
                    executor_key=executor_key,
                    guild_name=guild_name,
                    stat_date=stat_date,
                    refreshed_at=finished_iso,
                )
                conn.execute(
                """
                INSERT INTO guild_anchor_daily_stats (
                    guild_executor_key, guild_name, guild_display_name, stat_date, joined_count, real_person_count, total_anchors,
                    scanned_count, page_count, sort_direction, sort_confidence, stale_after, status, error, refreshed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_name, stat_date) DO UPDATE SET
                    guild_executor_key = excluded.guild_executor_key,
                    guild_display_name = excluded.guild_display_name,
                    joined_count = excluded.joined_count,
                    real_person_count = excluded.real_person_count,
                    total_anchors = excluded.total_anchors,
                    scanned_count = excluded.scanned_count,
                    page_count = excluded.page_count,
                    sort_direction = excluded.sort_direction,
                    sort_confidence = excluded.sort_confidence,
                    stale_after = excluded.stale_after,
                    status = excluded.status,
                    error = excluded.error,
                    refreshed_at = excluded.refreshed_at
                """,
                (
                    executor_key,
                    guild_name,
                    guild_name,
                    stat_date,
                    joined_count,
                    int(result.get('real_person_count') or 0),
                    result.get('total_anchors'),
                    int(result.get('scanned_count') or 0),
                    int(result.get('page_count') or 0),
                    str(result.get('sort_direction') or ''),
                    str(result.get('sort_confidence') or ''),
                    stale_after,
                    status,
                    error,
                    finished_iso,
                ),
            )
                conn.execute(
                """
                UPDATE guild_anchor_daily_stat_jobs
                SET status = ?,
                    lease_owner = '',
                    lease_until = '',
                    finished_at = ?,
                    scanned_count = ?,
                    page_count = ?,
                    total_anchors = ?,
                    joined_count = ?,
                    real_person_count = ?,
                    sort_direction = ?,
                    sort_confidence = ?,
                    error = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    status,
                    finished_iso,
                    int(result.get('scanned_count') or 0),
                    int(result.get('page_count') or 0),
                    result.get('total_anchors'),
                    joined_count,
                    int(result.get('real_person_count') or 0),
                    str(result.get('sort_direction') or ''),
                    str(result.get('sort_confidence') or ''),
                    error,
                    finished_iso,
                    job.get('job_id'),
                ),
            )
                publication = reconcile_newcomer_publication(
                    conn,
                    platform=executor_app,
                    business_date=stat_date,
                    created_at=finished_iso,
                )
                conn.commit()
        finally:
            self._release_guild_anchor_daily_stats_deploy_guard(deploy_guard)
        return {
            'guild_name': guild_name,
            'guild_executor_key': executor_key,
            'stat_date': stat_date,
            'joined_count': joined_count,
            'real_person_count': int(result.get('real_person_count') or 0),
            'total_anchors': result.get('total_anchors'),
            'scanned_count': int(result.get('scanned_count') or 0),
            'page_count': int(result.get('page_count') or 0),
            'status': status,
            'error': error,
            'refreshed_at': finished_iso,
            'newcomer_publication': publication,
        }

    def _fail_guild_anchor_daily_stat_job(self, job: Dict[str, Any], error: str) -> None:
        now_iso = utc_now()
        attempt_count = int(job.get('attempt_count') or 0)
        max_attempts = int(job.get('max_attempts') or self.guild_anchor_daily_stats_job_max_attempts)
        delays = [300, 900, 3600, 7200]
        if attempt_count >= max_attempts:
            status = 'dead'
            next_retry_at = ''
        else:
            status = 'retry_waiting'
            delay = delays[min(max(0, attempt_count - 1), len(delays) - 1)]
            next_retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            updated = conn.execute(
                """
                UPDATE guild_anchor_daily_stat_jobs
                SET status = ?, lease_owner = '', lease_until = '', finished_at = ?, next_retry_at = ?,
                    error = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_owner = ?
                """,
                (
                    status,
                    now_iso,
                    next_retry_at,
                    str(error)[:500],
                    now_iso,
                    job.get('job_id'),
                    self._worker_id,
                ),
            )
            if updated.rowcount:
                platform = str(job.get('guild_executor_key') or '').split(':', 1)[0].lower()
                if platform in {'linky', 'timo'}:
                    reconcile_newcomer_publication(
                        conn,
                        platform=platform,
                        business_date=str(job.get('stat_date') or ''),
                        created_at=now_iso,
                    )
            conn.commit()

    def list_newcomer_daily_publication(
        self,
        *,
        platform: str,
        business_date: str,
        revision: int = 0,
        limit: int = 500,
        offset: int = 0,
    ) -> Dict[str, Any]:
        try:
            with self.db.connect() as conn:
                return list_newcomer_publication(
                    conn,
                    platform=platform,
                    business_date=business_date,
                    revision=revision,
                    limit=limit,
                    offset=offset,
                )
        except NewcomerPublicationNotReady as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def run_due_guild_anchor_daily_stat_jobs(self, *, limit: int = 1, app_name: str = '') -> Dict[str, Any]:
        try:
            reclaimed = self.reclaim_guild_anchor_daily_stat_jobs_from_dead_workers(app_name=app_name)
            if int(reclaimed.get('reclaimed_count') or 0):
                print(f"Guild anchor daily stats reclaimed dead worker jobs: {reclaimed}")
        except Exception as exc:
            print(f'Guild anchor daily stats dead lease reclaim degraded: {exc}')
        try:
            recovered = self.auto_recover_guild_anchor_daily_stat_jobs(app_name=app_name)
            if int(recovered.get('recovered_count') or 0):
                print(f"Guild anchor daily stats automatically recovered jobs: {recovered}")
        except Exception as exc:
            print(f'Guild anchor daily stats automatic recovery degraded: {exc}')
        rows = []
        for _ in range(max(1, limit)):
            job = self._claim_next_guild_anchor_daily_stat_job(app_name=app_name)
            if not job:
                break
            executor = self._resolve_guild_anchor_executor_for_job(job)
            try:
                if not executor:
                    raise RuntimeError('executor_not_found')
                app_name = str(executor.get('app_name') or 'linky').strip().lower() or 'linky'
                if app_name == 'timo':
                    if not str(executor.get('platform_authorization') or '').strip():
                        raise RuntimeError('timo_ticket_missing')
                elif not (str(executor.get('oauth_token') or '').strip() and str(executor.get('oauth_token_secret') or '').strip()):
                    raise RuntimeError('oauth_not_configured')
                stat_date = self._parse_anchor_stat_date(job.get('stat_date'), fallback=datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date() - timedelta(days=1))
                if app_name == 'timo':
                    if str(job.get('source') or '').strip() == 'full_scan':
                        result = self._fetch_timo_guild_anchor_daily_count(executor=executor, stat_date=stat_date, job_id=str(job.get('job_id') or ''))
                    else:
                        result = self._fetch_timo_guild_anchor_incremental_count(executor=executor, stat_date=stat_date, job_id=str(job.get('job_id') or ''))
                        if str(result.get('status') or '') == 'needs_full_scan':
                            result = self._fetch_timo_guild_anchor_daily_count(executor=executor, stat_date=stat_date, job_id=str(job.get('job_id') or ''))
                elif str(job.get('source') or '').strip() == 'full_scan':
                    result = self._fetch_linky_guild_anchor_daily_count(executor=executor, stat_date=stat_date, job_id=str(job.get('job_id') or ''))
                else:
                    result = self._fetch_linky_guild_anchor_incremental_count(executor=executor, stat_date=stat_date, job_id=str(job.get('job_id') or ''))
                    if str(result.get('status') or '') == 'needs_full_scan':
                        fallback_reason = str(result.get('error') or 'incremental_boundary_unverified')
                        result = self._fetch_linky_guild_anchor_bounded_date_count(
                            executor=executor,
                            stat_date=stat_date,
                            job_id=str(job.get('job_id') or ''),
                            fallback_reason=fallback_reason,
                        )
                    if str(result.get('status') or '') != 'success':
                        raise RuntimeError(
                            'linky_incremental_scan_requires_explicit_full_scan:'
                            + str(result.get('error') or result.get('status') or 'unknown')
                        )
                rows.append(self._finish_guild_anchor_daily_stat_job(job, result, executor=executor))
            except Exception as exc:
                self._fail_guild_anchor_daily_stat_job(job, str(exc))
                rows.append({
                    'guild_name': job.get('guild_name'),
                    'stat_date': job.get('stat_date'),
                    'status': 'failed',
                    'error': str(exc)[:500],
                })
        return {'ok': True, 'processed_count': len(rows), 'rows': rows}

    def refresh_guild_anchor_daily_stats(self, *, stat_date: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        beijing_tz = ZoneInfo('Asia/Shanghai')
        today_bj = datetime.now(timezone.utc).astimezone(beijing_tz).date()
        target_date = self._parse_anchor_stat_date(stat_date, fallback=today_bj - timedelta(days=1))
        enqueue = self.enqueue_guild_anchor_daily_stat_jobs(stat_dates=[target_date.isoformat()], source='full_scan' if force else 'manual', force=force)
        processed_rows = []
        expected = int(enqueue.get('job_count') or 0)
        for _ in range(max(1, expected)):
            result = self.run_due_guild_anchor_daily_stat_jobs(limit=1)
            if not result.get('rows'):
                break
            processed_rows.extend(result.get('rows') or [])
        return {
            'ok': True,
            'stat_date': target_date.isoformat(),
            'timezone': 'Asia/Shanghai',
            'guild_count': len(processed_rows),
            'rows': processed_rows,
            'refreshed_at': utc_now(),
        }

    def refresh_guild_anchor_daily_stats_dates(self, *, stat_dates: List[str], force: bool = False, source: str = 'manual') -> Dict[str, Any]:
        enqueue = self.enqueue_guild_anchor_daily_stat_jobs(stat_dates=stat_dates, source=source, force=force)
        processed_rows = []
        expected = int(enqueue.get('job_count') or 0)
        for _ in range(max(1, expected)):
            result = self.run_due_guild_anchor_daily_stat_jobs(limit=1)
            if not result.get('rows'):
                break
            processed_rows.extend(result.get('rows') or [])
        return {
            'ok': True,
            'dates': enqueue.get('dates') or [],
            'timezone': 'Asia/Shanghai',
            'guild_count': len(processed_rows),
            'rows': processed_rows,
            'refreshed_at': utc_now(),
        }

    def guild_anchor_daily_stats_refresh_state(self) -> Dict[str, Any]:
        with self._guild_anchor_daily_stats_lock:
            return dict(self._guild_anchor_daily_stats_refresh_state)

    def start_guild_anchor_daily_stats_refresh(
        self,
        *,
        stat_date: Optional[str] = None,
        force: bool = False,
        source: str = 'manual',
    ) -> Dict[str, Any]:
        beijing_tz = ZoneInfo('Asia/Shanghai')
        today_bj = datetime.now(timezone.utc).astimezone(beijing_tz).date()
        target_date = self._parse_anchor_stat_date(stat_date, fallback=today_bj - timedelta(days=1))
        if target_date >= today_bj and not force:
            raise HTTPException(status_code=400, detail='只能刷新北京时间今天以前的日期')
        with self._guild_anchor_daily_stats_lock:
            if self._guild_anchor_daily_stats_refresh_thread and self._guild_anchor_daily_stats_refresh_thread.is_alive():
                state = dict(self._guild_anchor_daily_stats_refresh_state)
                state.update({'ok': True, 'accepted': True, 'already_running': True})
                return state
            started_at = utc_now()
            self._guild_anchor_daily_stats_refresh_state = {
                'running': True,
                'stat_date': target_date.isoformat(),
                'source': source,
                'started_at': started_at,
                'finished_at': '',
                'error': '',
            }
            thread = threading.Thread(
                target=self._run_guild_anchor_daily_stats_refresh,
                args=(target_date.isoformat(), force, source),
                name=f'guild-anchor-daily-stats-refresh-{target_date.isoformat()}',
                daemon=True,
            )
            self._guild_anchor_daily_stats_refresh_thread = thread
            thread.start()
            return {
                'ok': True,
                'accepted': True,
                'already_running': False,
                'running': True,
                'stat_date': target_date.isoformat(),
                'timezone': 'Asia/Shanghai',
                'started_at': started_at,
            }

    def _run_guild_anchor_daily_stats_refresh(self, stat_date: str, force: bool, source: str) -> None:
        try:
            result = self.refresh_guild_anchor_daily_stats(stat_date=stat_date, force=force)
            with self._guild_anchor_daily_stats_lock:
                self._guild_anchor_daily_stats_refresh_state = {
                    'running': False,
                    'stat_date': stat_date,
                    'source': source,
                    'started_at': self._guild_anchor_daily_stats_refresh_state.get('started_at') or '',
                    'finished_at': utc_now(),
                    'error': '',
                    'guild_count': int(result.get('guild_count') or 0),
                }
        except Exception as exc:
            with self._guild_anchor_daily_stats_lock:
                self._guild_anchor_daily_stats_refresh_state = {
                    'running': False,
                    'stat_date': stat_date,
                    'source': source,
                    'started_at': self._guild_anchor_daily_stats_refresh_state.get('started_at') or '',
                    'finished_at': utc_now(),
                    'error': str(exc)[:500],
                }

    @staticmethod
    def _timo_anchor_stats_latest_complete_day_bj(now: Optional[datetime] = None) -> datetime.date:
        beijing_tz = ZoneInfo('Asia/Shanghai')
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current_bj = current.astimezone(beijing_tz)
        if current_bj.hour >= 9:
            return current_bj.date() - timedelta(days=1)
        return current_bj.date() - timedelta(days=2)

    def list_guild_anchor_daily_stats(self, *, date_from: Optional[str] = None, date_to: Optional[str] = None, app_name: Optional[str] = None) -> Dict[str, Any]:
        latest_complete = self._timo_anchor_stats_latest_complete_day_bj()
        default_to = latest_complete
        requested_end = self._parse_anchor_stat_date(date_to, fallback=default_to)
        end_date = min(requested_end, latest_complete)
        start_date = self._parse_anchor_stat_date(date_from, fallback=end_date - timedelta(days=6))
        if start_date > end_date:
            start_date = end_date
        if (end_date - start_date).days > 6:
            start_date = end_date - timedelta(days=6)
        normalized_app = str(app_name or '').strip().lower()
        if normalized_app not in {'linky', 'timo'}:
            normalized_app = ''
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                """
                SELECT s.guild_executor_key, s.guild_name, s.guild_display_name, s.stat_date,
                       s.joined_count, s.real_person_count, s.total_anchors, s.scanned_count,
                       s.page_count, s.sort_direction, s.sort_confidence, s.stale_after,
                       s.status, s.error, s.refreshed_at
                FROM guild_anchor_daily_stats s
                WHERE s.stat_date BETWEEN ? AND ?
                ORDER BY s.stat_date DESC, s.guild_name ASC
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()]
            executor_rows = [dict(r) for r in conn.execute(
                """
                SELECT rowid AS guild_executor_id, guild_name, COALESCE(app_name, 'linky') AS app_name,
                       cms_guild_id, cms_guild_sid,
                       CASE WHEN COALESCE(oauth_token, '') != '' AND COALESCE(oauth_token_secret, '') != '' THEN 1 ELSE 0 END AS oauth_configured,
                       CASE WHEN COALESCE(platform_authorization, '') != '' THEN 1 ELSE 0 END AS timo_ticket_configured,
                       enabled
                FROM guild_executors
                WHERE LOWER(COALESCE(app_name, 'linky')) IN ('linky', 'timo')
                ORDER BY guild_name ASC
                """
            ).fetchall()]
            job_rows = [dict(r) for r in conn.execute(
                """
                SELECT guild_executor_key, guild_name, stat_date, status, attempt_count, max_attempts,
                       next_retry_at, lease_owner, lease_until, started_at, finished_at,
                       scanned_count, page_count, total_anchors, joined_count, real_person_count, error, source, updated_at
                FROM guild_anchor_daily_stat_jobs
                WHERE stat_date BETWEEN ? AND ?
                ORDER BY stat_date DESC, guild_name ASC
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()]
            seen_cache_rows = [dict(r) for r in conn.execute(
                """
                SELECT guild_name, created_date_bj AS stat_date,
                       COUNT(*) AS joined_count,
                       SUM(CASE WHEN COALESCE(is_real_person, 0) = 1 THEN 1 ELSE 0 END) AS real_person_count,
                       MAX(last_seen_at) AS last_seen_at
                FROM guild_anchor_seen
                WHERE created_date_bj BETWEEN ? AND ?
                GROUP BY guild_name, created_date_bj
                """,
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()]
        if normalized_app:
            executor_rows = [row for row in executor_rows if str(row.get('app_name') or 'linky').strip().lower() == normalized_app]
        executor_name_set = {str(row.get('guild_name') or '').strip() for row in executor_rows if str(row.get('guild_name') or '').strip()}
        executor_keys_by_name = {str(r.get('guild_name') or '').strip(): self._guild_anchor_executor_key(r) for r in executor_rows if str(r.get('guild_name') or '').strip()}
        guild_names = [str(r.get('guild_name') or '').strip() for r in executor_rows if str(r.get('guild_name') or '').strip()]
        dates = []
        cursor = start_date
        while cursor <= end_date:
            dates.append(cursor.isoformat())
            cursor += timedelta(days=1)
        by_guild: Dict[str, Dict[str, Any]] = {}
        by_date: Dict[str, int] = {d: 0 for d in dates}
        for guild_name in guild_names:
            by_guild[guild_name] = {
                'guild_executor_key': executor_keys_by_name.get(guild_name, ''),
                'guild_name': guild_name,
                'total': 0,
                'real_person_total': 0,
                'dates': {d: None for d in dates},
                'latest_status': 'missing',
                'latest_refreshed_at': '',
                'app_name': str(next((r for r in executor_rows if str(r.get('guild_name') or '').strip() == guild_name), {}).get('app_name') or 'linky').strip().lower() or 'linky',
                'oauth_configured': bool(next((r for r in executor_rows if str(r.get('guild_name') or '').strip() == guild_name), {}).get('oauth_configured')),
                'timo_ticket_configured': bool(next((r for r in executor_rows if str(r.get('guild_name') or '').strip() == guild_name), {}).get('timo_ticket_configured')),
                'enabled': bool(next((r for r in executor_rows if str(r.get('guild_name') or '').strip() == guild_name), {}).get('enabled')),
            }
        for row in rows:
            guild_name = str(row.get('guild_name') or '').strip()
            if normalized_app and guild_name not in executor_name_set:
                continue
            stat_date = str(row.get('stat_date') or '').strip()
            if guild_name not in by_guild:
                by_guild[guild_name] = {'guild_executor_key': row.get('guild_executor_key') or '', 'guild_name': guild_name, 'total': 0, 'real_person_total': 0, 'dates': {d: None for d in dates}, 'latest_status': 'missing', 'latest_refreshed_at': '', 'app_name': 'unknown', 'oauth_configured': False, 'timo_ticket_configured': False}
            count = int(row.get('joined_count') or 0)
            real_person_count = int(row.get('real_person_count') or 0)
            status = row.get('status') or 'unknown'
            stale_after = str(row.get('stale_after') or '').strip()
            if status == 'success' and stale_after and stale_after < utc_now():
                status = 'stale'
            by_guild[guild_name]['dates'][stat_date] = {
                'joined_count': count,
                'real_person_count': real_person_count,
                'status': status,
                'error': row.get('error') or '',
                'refreshed_at': row.get('refreshed_at') or '',
                'total_anchors': row.get('total_anchors'),
                'scanned_count': row.get('scanned_count'),
                'page_count': row.get('page_count'),
                'sort_direction': row.get('sort_direction') or '',
                'sort_confidence': row.get('sort_confidence') or '',
                'stale_after': stale_after,
            }
            by_guild[guild_name]['total'] += count
            by_guild[guild_name]['real_person_total'] += real_person_count
            by_guild[guild_name]['latest_status'] = status or by_guild[guild_name]['latest_status']
            by_guild[guild_name]['latest_refreshed_at'] = row.get('refreshed_at') or by_guild[guild_name]['latest_refreshed_at']
            if stat_date in by_date:
                by_date[stat_date] += count
        processing_statuses = {'pending', 'running', 'retry_waiting'}
        failed_statuses = {'dead', 'failed'}
        processing_job_count = 0
        running_job_count = 0
        failed_job_count = 0
        for row in job_rows:
            guild_name = str(row.get('guild_name') or '').strip()
            if normalized_app and guild_name not in executor_name_set:
                continue
            stat_date = str(row.get('stat_date') or '').strip()
            if not guild_name or stat_date not in dates:
                continue
            if guild_name not in by_guild:
                by_guild[guild_name] = {'guild_executor_key': row.get('guild_executor_key') or '', 'guild_name': guild_name, 'total': 0, 'real_person_total': 0, 'dates': {d: None for d in dates}, 'latest_status': 'missing', 'latest_refreshed_at': '', 'app_name': 'unknown', 'oauth_configured': False, 'timo_ticket_configured': False}
            if by_guild[guild_name]['dates'].get(stat_date) is not None:
                continue
            job_status = str(row.get('status') or '').strip()
            if job_status in processing_statuses:
                attempts = int(row.get('attempt_count') or 0)
                max_attempts = int(row.get('max_attempts') or self.guild_anchor_daily_stats_job_max_attempts)
                if attempts >= max_attempts:
                    status = 'failed'
                    failed_job_count += 1
                else:
                    status = 'processing'
                    processing_job_count += 1
                    if job_status == 'running':
                        running_job_count += 1
            elif job_status in failed_statuses:
                status = 'failed'
                failed_job_count += 1
            else:
                continue
            by_guild[guild_name]['dates'][stat_date] = {
                'joined_count': int(row.get('joined_count') or 0),
                'real_person_count': int(row.get('real_person_count') or 0),
                'status': status,
                'job_status': job_status,
                'error': row.get('error') or '',
                'refreshed_at': row.get('finished_at') or row.get('updated_at') or '',
                'total_anchors': row.get('total_anchors'),
                'scanned_count': row.get('scanned_count'),
                'page_count': row.get('page_count'),
                'attempt_count': row.get('attempt_count'),
                'max_attempts': row.get('max_attempts'),
                'next_retry_at': row.get('next_retry_at') or '',
                'lease_until': row.get('lease_until') or '',
                'source': row.get('source') or '',
            }
            by_guild[guild_name]['latest_status'] = status
        seen_cache_by_key = {
            (str(row.get('guild_name') or '').strip(), str(row.get('stat_date') or '').strip()): row
            for row in seen_cache_rows
        }
        # Seen rows are diagnostic cache only.  A failed or incomplete source
        # read must never be presented as a completed daily count.
        fallback_count = 0
        # Per-guild results are staging facts until the whole app/date batch is
        # complete. Never expose or total a partially refreshed day.
        expected_guild_names = {
            str(guild.get('guild_name') or '').strip()
            for guild in by_guild.values()
            if bool(guild.get('enabled')) and str(guild.get('guild_name') or '').strip()
        }
        jobs_by_date: Dict[str, Dict[str, Dict[str, Any]]] = {stat_date: {} for stat_date in dates}
        for job in job_rows:
            guild_name = str(job.get('guild_name') or '').strip()
            stat_date = str(job.get('stat_date') or '').strip()
            if stat_date not in jobs_by_date or guild_name not in expected_guild_names:
                continue
            jobs_by_date[stat_date][guild_name] = job
        date_states: Dict[str, Dict[str, Any]] = {}
        unpublished_dates = set()
        for stat_date in dates:
            date_jobs = jobs_by_date.get(stat_date) or {}
            if not date_jobs:
                date_states[stat_date] = {'published': True, 'status': 'published'}
                continue
            incomplete_guilds = []
            has_processing = False
            has_failed = False
            for guild_name in expected_guild_names:
                guild = by_guild.get(guild_name) or {}
                cell = (guild.get('dates') or {}).get(stat_date)
                cell_status = str((cell or {}).get('status') or '').strip()
                job_status = str((date_jobs.get(guild_name) or {}).get('status') or '').strip()
                complete = job_status == 'success' and cell_status not in {'', 'processing', 'failed'}
                if complete:
                    continue
                incomplete_guilds.append(guild_name)
                has_processing = has_processing or job_status in processing_statuses or not job_status
                has_failed = has_failed or job_status in failed_statuses
            if not incomplete_guilds:
                date_states[stat_date] = {'published': True, 'status': 'published'}
                continue
            unpublished_dates.add(stat_date)
            state_status = 'processing' if has_processing else ('failed' if has_failed else 'processing')
            date_states[stat_date] = {
                'published': False,
                'status': state_status,
                'incomplete_guild_count': len(incomplete_guilds),
            }
        if unpublished_dates:
            for guild in by_guild.values():
                for stat_date in unpublished_dates:
                    date_state = date_states[stat_date]
                    guild_name = str(guild.get('guild_name') or '').strip()
                    prior_cell = (guild.get('dates') or {}).get(stat_date) or {}
                    job_status = str((jobs_by_date.get(stat_date, {}).get(guild_name) or {}).get('status') or prior_cell.get('job_status') or '').strip()
                    guild['dates'].pop(stat_date, None)
                guild['total'] = sum(
                    int(((guild.get('dates') or {}).get(stat_date) or {}).get('joined_count') or 0)
                    for stat_date in dates
                    if stat_date not in unpublished_dates
                )
                guild['real_person_total'] = sum(
                    int(((guild.get('dates') or {}).get(stat_date) or {}).get('real_person_count') or 0)
                    for stat_date in dates
                    if stat_date not in unpublished_dates
                )
            by_date = {stat_date: value for stat_date, value in by_date.items() if stat_date not in unpublished_dates}
            rows = [row for row in rows if str(row.get('stat_date') or '').strip() not in unpublished_dates]
        visible_dates = [stat_date for stat_date in dates if stat_date not in unpublished_dates]
        refresh_state = self.guild_anchor_daily_stats_refresh_state()
        return {
            'ok': True,
            'timezone': 'Asia/Shanghai',
            'date_from': start_date.isoformat(),
            'date_to': end_date.isoformat(),
            'app_name': normalized_app or 'all',
            'dates': list(reversed(visible_dates)),
            'guilds': list(by_guild.values()),
            'rows': rows,
            'date_states': {k: date_states[k] for k in reversed(dates)},
            'summary': {
                'guild_count': len(by_guild),
                'date_count': len(visible_dates),
                'total_joined': sum(int(g.get('total') or 0) for g in by_guild.values()),
                'total_real_person': sum(int(g.get('real_person_total') or 0) for g in by_guild.values()),
                'by_date': {k: by_date[k] for k in reversed(visible_dates)},
            },
            'schedule': {
                'enabled': self.guild_anchor_daily_stats_enabled,
                'hour_bj': self.guild_anchor_daily_stats_hour_bj,
                'minute_bj': self.guild_anchor_daily_stats_minute_bj,
                'target': 'yesterday',
            },
            'refresh_state': {
                **refresh_state,
                'running': bool(refresh_state.get('running') or processing_job_count),
                'processing_job_count': processing_job_count,
                'running_job_count': running_job_count,
                'failed_job_count': failed_job_count,
                'fallback_count': fallback_count,
            },
        }

    def resolve_guild_executor(self, guild_name: Optional[str], *, app_name: str = 'linky') -> Optional[Dict[str, Any]]:
        normalized_guild_name = str(guild_name or '').strip()
        if not normalized_guild_name:
            return None
        normalized_app = str(app_name or 'linky').strip().lower() or 'linky'
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM guild_executors WHERE guild_name = ? AND LOWER(COALESCE(app_name, 'linky')) = ?",
                (normalized_guild_name, normalized_app),
            ).fetchone()
        if not row:
            return None
        resolved = dict(row)
        resolved.update(guild_country_contract(resolved))
        resolved['country'] = resolved.get('guild_country') or resolved.get('country') or ''
        resolved['enabled'] = bool(resolved.get('enabled'))
        resolved['password_configured'] = bool(str(resolved.get('password_secret_ref') or '').strip())
        resolved['oauth_configured'] = bool(str(resolved.get('oauth_token') or '').strip() and str(resolved.get('oauth_token_secret') or '').strip())
        resolved['guild_backend_token_configured'] = bool(str(resolved.get('guild_backend_token') or '').strip())
        resolved['platform_authorization_configured'] = bool(str(resolved.get('platform_authorization') or '').strip())
        with self.db.connect() as conn:
            token_row = conn.execute(
                "SELECT refresh_token, refresh_token_deadtime, access_token_exp FROM cms_executor_tokens WHERE guild_name = ?",
                (normalized_guild_name,),
            ).fetchone()
        resolved['cms_refresh_token'] = str(token_row['refresh_token'] or '').strip() if token_row else ''
        resolved['cms_refresh_token_deadtime'] = token_row['refresh_token_deadtime'] if token_row else None
        resolved['cms_access_token_exp'] = token_row['access_token_exp'] if token_row else None
        resolved['cms_refresh_token_configured'] = bool(resolved['cms_refresh_token'])
        return resolved

    def resolve_timo_guild_executor(self, guild_name: Optional[str]) -> Optional[Dict[str, Any]]:
        from app.timo_guild_identity import timo_guild_storage_name
        return self.resolve_guild_executor(timo_guild_storage_name(guild_name), app_name='timo')

    def persist_cms_executor_refresh_result(self, guild_name: Optional[str], refresh_result: Any) -> None:
        normalized_guild_name = str(guild_name or '').strip()
        if not normalized_guild_name or not isinstance(refresh_result, dict):
            return
        updated_at = utc_now()
        last_refresh_at = int(time.time())
        ok = bool(refresh_result.get('ok'))
        authorization = str(refresh_result.get('authorization') or '').strip()
        refresh_token = str(refresh_result.get('refresh_token') or '').strip()
        refresh_token_deadtime = refresh_result.get('refresh_token_deadtime')
        access_token_exp = refresh_result.get('access_token_exp')
        last_refresh_error = None if ok else str(refresh_result.get('error') or 'CMS refresh failed').strip()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO cms_executor_tokens (
                    guild_name, refresh_token, refresh_token_deadtime, access_token_exp, last_refresh_at, last_refresh_error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_name) DO UPDATE SET
                    refresh_token = CASE WHEN excluded.refresh_token != '' THEN excluded.refresh_token ELSE cms_executor_tokens.refresh_token END,
                    refresh_token_deadtime = COALESCE(excluded.refresh_token_deadtime, cms_executor_tokens.refresh_token_deadtime),
                    access_token_exp = COALESCE(excluded.access_token_exp, cms_executor_tokens.access_token_exp),
                    last_refresh_at = excluded.last_refresh_at,
                    last_refresh_error = excluded.last_refresh_error,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_guild_name,
                    refresh_token,
                    refresh_token_deadtime,
                    access_token_exp,
                    last_refresh_at,
                    last_refresh_error,
                    updated_at,
                ),
            )
            if ok and authorization:
                conn.execute(
                    "UPDATE guild_executors SET platform_authorization = ?, updated_at = ? WHERE guild_name = ?",
                    (authorization, updated_at, normalized_guild_name),
                )
            conn.commit()

    @staticmethod
    def _cms_proxy_dict(proxy_url: Optional[str]) -> Optional[Dict[str, str]]:
        normalized = str(proxy_url or '').strip()
        if not normalized:
            return None
        return {'http': normalized, 'https': normalized}

    def sync_guild_executor_cms_lock(
        self,
        guild_name: Optional[str],
        *,
        authorization: Optional[str],
        proxy_url: Optional[str] = '',
        timeout_seconds: float = 15.0,
    ) -> Dict[str, Any]:
        normalized_guild_name = str(guild_name or '').strip()
        normalized_authorization = str(authorization or '').strip()
        normalized_proxy_url = str(proxy_url or '').strip()
        if not normalized_guild_name:
            return {'synced': False, 'status': 'skipped', 'reason': 'missing_guild_name'}
        if not normalized_authorization:
            return {'synced': False, 'status': 'skipped', 'reason': 'missing_authorization'}
        url = f'{PLATFORM_BACKEND_BASE_URL.rstrip("/")}/api/admin/linky/industrial/industrial/getGuildIdAndName'
        try:
            response = requests.get(
                url,
                headers={
                    'Authorization': normalized_authorization,
                    'Accept': 'application/json, text/plain, */*',
                },
                proxies=self._cms_proxy_dict(normalized_proxy_url),
                timeout=max(5.0, float(timeout_seconds or 15.0)),
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get('data') if isinstance(payload, dict) else payload
            if not isinstance(rows, list):
                raise ValueError('CMS guild list returned an unsupported data shape')
            exact = [
                dict(row)
                for row in rows
                if isinstance(row, dict)
                and str(row.get('guild_name') or '').strip().lower() == normalized_guild_name.lower()
            ]
            if len(exact) != 1:
                if len(exact) > 1:
                    return {
                        'synced': False,
                        'status': 'failed',
                        'reason': 'ambiguous_guild_name',
                        'guild_name': normalized_guild_name,
                        'match_count': len(exact),
                    }
                contains = [
                    dict(row)
                    for row in rows
                    if isinstance(row, dict)
                    and normalized_guild_name.lower() in str(row.get('guild_name') or '').strip().lower()
                ]
                if contains:
                    return {
                        'synced': False,
                        'status': 'failed',
                        'reason': 'requires_exact_match',
                        'guild_name': normalized_guild_name,
                        'match_count': len(contains),
                    }
                return {
                    'synced': False,
                    'status': 'failed',
                    'reason': 'target_guild_not_visible',
                    'guild_name': normalized_guild_name,
                }
            matched = exact[0]
            cms_guild_id = str(matched.get('id') or matched.get('guild_id') or '').strip()
            cms_guild_sid = str(matched.get('sid') or matched.get('guild_sid') or '').strip()
            if not cms_guild_id or not cms_guild_sid:
                return {
                    'synced': False,
                    'status': 'failed',
                    'reason': 'cms_guild_lock_missing_in_response',
                    'guild_name': normalized_guild_name,
                }
            updated_at = utc_now()
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE guild_executors SET cms_guild_id = ?, cms_guild_sid = ?, updated_at = ? WHERE guild_name = ?",
                    (cms_guild_id, cms_guild_sid, updated_at, normalized_guild_name),
                )
                conn.commit()
            return {
                'synced': True,
                'status': 'synced',
                'guild_name': normalized_guild_name,
                'cms_guild_id': cms_guild_id,
                'cms_guild_sid': cms_guild_sid,
                'cms_guild_name': str(matched.get('guild_name') or '').strip(),
            }
        except Exception as exc:
            return {
                'synced': False,
                'status': 'failed',
                'reason': 'cms_lock_sync_failed',
                'guild_name': normalized_guild_name,
                'error': str(exc),
            }

    def sync_timo_guild_executor_cms_lock(
        self,
        guild_name: Optional[str],
        *,
        authorization: Optional[str],
        platform_backend_url: Optional[str] = '',
        timeout_seconds: float = 15.0,
    ) -> Dict[str, Any]:
        normalized_guild_name = str(guild_name or '').strip()
        normalized_authorization = str(authorization or '').strip()
        if not normalized_guild_name:
            return {'synced': False, 'status': 'skipped', 'reason': 'missing_guild_name'}
        if not normalized_authorization:
            return {'synced': False, 'status': 'skipped', 'reason': 'missing_authorization'}
        executor = {
            'guild_name': normalized_guild_name,
            'platform_authorization': normalized_authorization,
            'platform_backend_url': str(platform_backend_url or TIMO_DEFAULT_API_BASE_URL).strip() or TIMO_DEFAULT_API_BASE_URL,
            'request_timeout_seconds': timeout_seconds,
        }
        try:
            body = self._timo_guild_api_post(
                executor=executor,
                path='website-frontend/v1/officalWebGuild/getMyGuildInfo',
                payload={},
                timeout_seconds=timeout_seconds,
            )
            data = body.get('data') if isinstance(body.get('data'), dict) else body
            if not isinstance(data, dict):
                raise ValueError('Timo guild info returned an unsupported data shape')
            cms_guild_name = str(data.get('guildName') or data.get('guild_name') or '').strip()
            from app.timo_guild_identity import timo_guild_storage_name
            if cms_guild_name and timo_guild_storage_name(cms_guild_name).lower() != timo_guild_storage_name(normalized_guild_name).lower():
                return {
                    'synced': False,
                    'status': 'failed',
                    'reason': 'target_guild_not_visible',
                    'guild_name': normalized_guild_name,
                    'cms_guild_name': cms_guild_name,
                }
            cms_guild_id = str(data.get('guildId') or data.get('id') or data.get('guild_id') or '').strip()
            cms_guild_sid = str(data.get('uuid') or data.get('guildUuid') or data.get('guild_uuid') or data.get('sid') or data.get('guild_sid') or '').strip()
            cms_guild_sid = cms_guild_sid or cms_guild_id
            if not cms_guild_id:
                return {
                    'synced': False,
                    'status': 'failed',
                    'reason': 'cms_guild_id_missing_in_response',
                    'guild_name': normalized_guild_name,
                    'cms_guild_name': cms_guild_name,
                }
            updated_at = utc_now()
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE guild_executors SET cms_guild_id = ?, cms_guild_sid = ?, updated_at = ? WHERE guild_name = ? AND LOWER(COALESCE(app_name, '')) = 'timo'",
                    (cms_guild_id, cms_guild_sid, updated_at, normalized_guild_name),
                )
                conn.commit()
            return {
                'synced': True,
                'status': 'synced',
                'guild_name': normalized_guild_name,
                'cms_guild_id': cms_guild_id,
                'cms_guild_sid': cms_guild_sid,
                'cms_guild_name': cms_guild_name,
            }
        except Exception as exc:
            return {
                'synced': False,
                'status': 'failed',
                'reason': 'timo_cms_lock_sync_failed',
                'guild_name': normalized_guild_name,
                'error': str(exc),
            }

    def _guild_executor_country_guard(self, guild_name: Optional[str], user_country: Any) -> Dict[str, Any]:
        executor = self.resolve_guild_executor(guild_name)
        contract = guild_country_contract(executor)
        guild_country = contract['guild_country']
        eligible_user_countries = contract['eligible_user_countries']
        normalized_user_country = normalize_country_label(user_country)
        allowed = countries_match(normalized_user_country, eligible_user_countries)
        cross_country_fallback = bool(allowed and normalized_user_country and guild_country and normalized_user_country.casefold() != guild_country.casefold())
        return {
            'allowed': allowed,
            'guild_name': str(guild_name or '').strip(),
            'user_country': normalized_user_country,
            'guild_country': guild_country,
            'eligible_user_countries': eligible_user_countries,
            'routing_region': contract['routing_region'],
            'cross_country_fallback': cross_country_fallback,
            'cross_country_fallback_reason': 'eligible_country_compatibility' if cross_country_fallback else '',
        }

    def guild_executor_has_platform_cms_route(self, guild_name: Optional[str]) -> bool:
        executor = self.resolve_guild_executor(guild_name)
        if not executor or not executor.get('enabled'):
            return False
        return bool(
            str(executor.get('platform_backend_url') or '').strip()
            and str(executor.get('platform_authorization') or '').strip()
        )

    def get_guild_executor(self, guild_name: str) -> Dict[str, Any]:
        resolved = self.resolve_guild_executor(guild_name)
        if not resolved:
            raise HTTPException(status_code=404, detail='guild executor not found')
        return {
            'guild_name': resolved['guild_name'],
            'app_name': resolved.get('app_name') or 'linky',
            'backend_url': resolved['backend_url'],
            'login_username': resolved['login_username'],
            'platform_backend_url': resolved.get('platform_backend_url') or '',
            'cms_guild_id': resolved.get('cms_guild_id') or '',
            'cms_guild_sid': resolved.get('cms_guild_sid') or '',
            'country': resolved.get('country') or '',
            'guild_country': resolved.get('guild_country') or resolved.get('country') or '',
            'eligible_user_countries': resolved.get('eligible_user_countries') or [],
            'routing_region': resolved.get('routing_region') or '',
            'proxy_url': resolved.get('proxy_url') or '',
            'proxy_region': resolved.get('proxy_region') or '',
            'proxy_type': resolved.get('proxy_type') or '',
            'enabled': bool(resolved.get('enabled')),
            'browser_profile_key': resolved.get('browser_profile_key') or '',
            'bind_concurrency': int(resolved.get('bind_concurrency') or 1),
            'request_timeout_seconds': int(resolved.get('request_timeout_seconds') or 30),
            'notes': resolved.get('notes') or '',
            'password_configured': bool(resolved.get('password_configured')),
            'oauth_configured': bool(resolved.get('oauth_configured')),
            'guild_backend_token_configured': bool(resolved.get('guild_backend_token_configured')),
            'platform_authorization_configured': bool(resolved.get('platform_authorization_configured')),
            'cms_refresh_token_configured': bool(resolved.get('cms_refresh_token_configured')),
            'updated_at': resolved.get('updated_at'),
        }

    def get_timo_guild_executor(self, guild_name: str) -> Dict[str, Any]:
        resolved = self.resolve_timo_guild_executor(guild_name)
        if not resolved:
            raise HTTPException(status_code=404, detail='timo guild executor not found')
        return self._public_timo_guild_executor(resolved)

    def _public_timo_guild_executor(self, row: Dict[str, Any]) -> Dict[str, Any]:
        from app.timo_guild_identity import timo_guild_display_name
        public = dict(row or {})
        public['app_name'] = 'timo'
        public['guild_display_name'] = timo_guild_display_name(
            public.get('guild_name'),
            guild_id=public.get('cms_guild_id'),
            guild_sid=public.get('cms_guild_sid'),
        )
        public['platform_backend_url'] = str(public.get('platform_backend_url') or TIMO_DEFAULT_API_BASE_URL).strip() or TIMO_DEFAULT_API_BASE_URL
        public['cms_guild_sid'] = str(public.get('cms_guild_sid') or public.get('cms_guild_id') or '').strip()
        public['enabled'] = bool(public.get('enabled'))
        public['bind_concurrency'] = max(1, int(public.get('bind_concurrency') or 3))
        public['request_timeout_seconds'] = max(3, int(public.get('request_timeout_seconds') or 15))
        public['platform_authorization_configured'] = bool(str(public.get('platform_authorization') or '').strip()) or bool(public.get('platform_authorization_configured'))
        public['cms_token_configured'] = bool(public['platform_authorization_configured'])
        public['cms_refresh_token_configured'] = False
        public['oauth_configured'] = False
        public['assignees'] = self._ops_intake_assignees_for_guild(str(public.get('guild_name') or ''))
        public['reward_tracks'] = self._build_timo_reward_tracks(
            str(public.get('guild_name') or ''),
            country=str(public.get('country') or ''),
        )
        guild_name = str(public.get('guild_name') or '')
        public['auth_station_status'] = self._timo_auth_station_status_by_guild().get(guild_name.strip().lower()) or self._empty_timo_auth_station_status(guild_name)
        ticket_status = self._timo_executor_keepalive_status_by_guild().get(guild_name.strip().lower()) or {}
        timo_live_status = str(ticket_status.get('live_status') or ('not_configured' if not public['platform_authorization_configured'] else 'unknown')).strip() or 'unknown'
        public['timo_live_status'] = timo_live_status
        public['timo_live_checked_at'] = ticket_status.get('checked_at_iso') or ticket_status.get('checked_at')
        public['timo_live_error'] = ticket_status.get('error')
        public['timo_live_error_category'] = ticket_status.get('error_category')
        public['timo_live_reason'] = ticket_status.get('normalized_reason') or ''
        public['timo_live_capability'] = ticket_status.get('capability') or ''
        public['timo_live_is_stale'] = bool(ticket_status.get('is_stale'))
        public['timo_account_diamond_balance'] = ticket_status.get('account_diamond_balance')
        public['timo_account_balance_checked_at'] = ticket_status.get('checked_at_iso') or ticket_status.get('checked_at')
        public['cms_channel_status'] = 'valid' if timo_live_status == 'active' else ('invalid' if timo_live_status == 'inactive' else ('not_configured' if not public['platform_authorization_configured'] else 'unknown'))
        public.pop('platform_authorization', None)
        public.pop('oauth_token', None)
        public.pop('oauth_token_secret', None)
        return public

    def get_sogo_guild_executor(self, guild_name: str) -> Dict[str, Any]:
        resolved = self.resolve_guild_executor(guild_name, app_name=SUGO_APP_NAME) or self.resolve_guild_executor(guild_name, app_name=SUGO_LEGACY_APP_NAME)
        if not resolved:
            raise HTTPException(status_code=404, detail='sugo guild executor not found')
        return self._public_sogo_guild_executor(resolved)

    def _public_sogo_guild_executor(self, row: Dict[str, Any]) -> Dict[str, Any]:
        public = dict(row or {})
        public['app_name'] = SUGO_APP_NAME
        public['platform_backend_url'] = str(public.get('platform_backend_url') or SUGO_DEFAULT_API_BASE_URL).strip() or SUGO_DEFAULT_API_BASE_URL
        public['enabled'] = bool(public.get('enabled'))
        public['bind_concurrency'] = max(1, int(public.get('bind_concurrency') or 1))
        public['request_timeout_seconds'] = max(5, int(public.get('request_timeout_seconds') or 30))
        public['platform_authorization_configured'] = bool(str(public.get('platform_authorization') or '').strip()) or bool(public.get('platform_authorization_configured'))
        public['cms_token_configured'] = bool(public['platform_authorization_configured'])
        public['cms_refresh_token_configured'] = bool(public.get('cms_refresh_token_configured'))
        public['oauth_configured'] = False
        public['assignees'] = self._ops_intake_assignees_for_guild(str(public.get('guild_name') or ''))
        public.pop('platform_authorization', None)
        public.pop('cms_refresh_token', None)
        public.pop('password_secret_ref', None)
        public.pop('guild_backend_token', None)
        public.pop('oauth_token', None)
        public.pop('oauth_token_secret', None)
        return public

    def update_timo_guild_executor(self, guild_name: str, payload: TimoGuildExecutorUpdateRequest) -> Dict[str, Any]:
        from app.timo_guild_identity import timo_guild_display_name, timo_guild_storage_name
        normalized_guild_name = timo_guild_storage_name(guild_name)
        if not normalized_guild_name:
            raise HTTPException(status_code=400, detail='guild_name is required')
        existing_executor = self.resolve_timo_guild_executor(normalized_guild_name)
        requested_platform_backend_url = str(payload.platform_backend_url or '').strip()
        existing_platform_backend_url = str((existing_executor or {}).get('platform_backend_url') or '').strip()
        platform_backend_url = requested_platform_backend_url or existing_platform_backend_url or TIMO_DEFAULT_API_BASE_URL
        platform_authorization = str(payload.platform_authorization or '').strip()
        existing_authorization = str((existing_executor or {}).get('platform_authorization') or '').strip()
        cms_guild_id = str(payload.cms_guild_id or '').strip() or str((existing_executor or {}).get('cms_guild_id') or '').strip()
        cms_guild_sid = str(payload.cms_guild_sid or '').strip() or str((existing_executor or {}).get('cms_guild_sid') or '').strip()
        requested_contract = guild_country_contract({
            'country': payload.country,
            'guild_country': payload.guild_country or payload.country or (existing_executor or {}).get('guild_country') or (existing_executor or {}).get('country'),
            'eligible_user_countries': payload.eligible_user_countries or (existing_executor or {}).get('eligible_user_countries'),
            'routing_region': payload.routing_region if payload.routing_region is not None else (existing_executor or {}).get('routing_region'),
        })
        country = (
            requested_contract['guild_country']
            or normalize_country_label((existing_executor or {}).get('guild_country'))
            or normalize_country_label((existing_executor or {}).get('country'))
            or infer_country_context(normalized_guild_name)
        )
        if country == 'Colombia' and not payload.eligible_user_countries and not (existing_executor or {}).get('eligible_user_countries'):
            requested_contract['eligible_user_countries'] = list(SPANISH_LATAM_COMPAT_COUNTRIES)
        requested_contract = guild_country_contract({**requested_contract, 'guild_country': country})
        timo_allowed_countries = {'Mexico', 'Brazil', 'Indonesia', 'Philippines', 'Turkey', 'Chile', 'Colombia', 'Venezuela', 'Peru', 'Argentina'}
        if not country or country not in timo_allowed_countries:
            raise HTTPException(status_code=400, detail='country is required for timo guild executor')
        bind_concurrency = max(1, int(payload.bind_concurrency if payload.bind_concurrency is not None else ((existing_executor or {}).get('bind_concurrency') or 3)))
        request_timeout_seconds = max(3, int(payload.request_timeout_seconds if payload.request_timeout_seconds is not None else ((existing_executor or {}).get('request_timeout_seconds') or 15)))
        now = utc_now()
        slug = re.sub(r'[^a-z0-9]+', '-', normalized_guild_name.lower()).strip('-') or 'default'
        crm_dept_sync = {'synced': False, 'status': 'not_checked'}
        with self.db.connect() as conn:
            collision = conn.execute(
                "SELECT app_name FROM guild_executors WHERE guild_name = ? AND LOWER(COALESCE(app_name, 'linky')) != 'timo' LIMIT 1",
                (normalized_guild_name,),
            ).fetchone()
            if collision:
                raise HTTPException(status_code=400, detail='guild_name_already_used_by_linky_executor')
            crm_dept_sync = self._ensure_crm_dept_for_guild_executor(
                timo_guild_display_name(normalized_guild_name)
            )
            conn.execute(
                """
                INSERT INTO guild_executors (
                    guild_name, app_name, backend_url, login_username, password_secret_ref, guild_backend_token,
                    oauth_token, oauth_token_secret, platform_backend_url, platform_authorization,
                    cms_guild_id, cms_guild_sid, country, guild_country, eligible_user_countries, routing_region,
                    proxy_url, proxy_region, proxy_type,
                    enabled, browser_profile_key, bind_concurrency, request_timeout_seconds, notes, updated_at
                ) VALUES (?, 'timo', ?, '', '', '', '', '', ?, ?, ?, ?, ?, ?, ?, ?, '', '', 'http', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_name)
                DO UPDATE SET app_name = 'timo',
                              backend_url = excluded.backend_url,
                              platform_backend_url = excluded.platform_backend_url,
                              platform_authorization = CASE WHEN excluded.platform_authorization != '' THEN excluded.platform_authorization ELSE guild_executors.platform_authorization END,
                              cms_guild_id = CASE WHEN excluded.cms_guild_id != '' THEN excluded.cms_guild_id ELSE guild_executors.cms_guild_id END,
                              cms_guild_sid = CASE WHEN excluded.cms_guild_sid != '' THEN excluded.cms_guild_sid ELSE guild_executors.cms_guild_sid END,
                              country = excluded.country,
                              guild_country = excluded.guild_country,
                              eligible_user_countries = excluded.eligible_user_countries,
                              routing_region = excluded.routing_region,
                              enabled = excluded.enabled,
                              browser_profile_key = excluded.browser_profile_key,
                              bind_concurrency = excluded.bind_concurrency,
                              request_timeout_seconds = excluded.request_timeout_seconds,
                              notes = excluded.notes,
                              updated_at = excluded.updated_at
                """,
                (
                    normalized_guild_name,
                    TIMO_DEFAULT_API_BASE_URL,
                    platform_backend_url,
                    platform_authorization or existing_authorization,
                    cms_guild_id,
                    cms_guild_sid,
                    country,
                    country,
                    json.dumps(requested_contract['eligible_user_countries'], ensure_ascii=False),
                    requested_contract['routing_region'],
                    1 if payload.enabled else 0,
                    f'timo-{slug}',
                    bind_concurrency,
                    request_timeout_seconds,
                    str(payload.notes or '').strip(),
                    now,
                ),
            )
            conn.commit()
        persisted_executor = self.resolve_timo_guild_executor(normalized_guild_name) or {}
        timo_lock_sync: Dict[str, Any] = {'synced': False, 'status': 'skipped', 'reason': 'not_needed'}
        if self.db.db_path != ':memory:' and str(persisted_executor.get('platform_authorization') or '').strip() and (
            not str(persisted_executor.get('cms_guild_id') or '').strip()
            or not str(persisted_executor.get('cms_guild_sid') or '').strip()
            or bool(platform_authorization)
        ):
            timo_lock_sync = self.sync_timo_guild_executor_cms_lock(
                normalized_guild_name,
                authorization=str(persisted_executor.get('platform_authorization') or '').strip(),
                platform_backend_url=str(persisted_executor.get('platform_backend_url') or platform_backend_url or '').strip(),
                timeout_seconds=float(persisted_executor.get('request_timeout_seconds') or request_timeout_seconds or 15),
            )
            if timo_lock_sync.get('synced'):
                persisted_executor = self.resolve_timo_guild_executor(normalized_guild_name) or persisted_executor
        persisted = self.get_timo_guild_executor(normalized_guild_name)
        persisted['saved'] = True
        persisted['timo_cms_lock_sync'] = timo_lock_sync
        persisted['crm_guild_sync'] = crm_dept_sync
        persisted['mafubo_guild_access'] = self._auto_assign_default_mafubo_for_guild(
            normalized_guild_name,
            assigned_by='system:timo_guild_executor',
        )
        existing_key = self._guild_anchor_executor_key(existing_executor or {}) if existing_executor else ''
        persisted_key = self._guild_anchor_executor_key(persisted_executor)
        auth_was_added = bool(platform_authorization) and not bool(existing_authorization)
        identity_changed = bool(existing_executor) and bool(persisted_key) and persisted_key != existing_key
        has_seen_stats = False
        if persisted_key:
            with self.db.connect() as conn:
                has_seen_stats = bool(conn.execute(
                    "SELECT 1 FROM guild_anchor_daily_stats WHERE guild_executor_key = ? LIMIT 1",
                    (persisted_key,),
                ).fetchone())
        should_bootstrap = (
            bool(payload.enabled)
            and bool(str(persisted_executor.get('platform_authorization') or '').strip())
            and (not existing_executor or auth_was_added or identity_changed or not has_seen_stats)
        )
        if should_bootstrap:
            persisted['global_data_bootstrap'] = self._start_timo_guild_executor_global_bootstrap(persisted_executor)
        if platform_authorization:
            persisted['export_cache_catchup'] = self._trigger_timo_export_cache_after_ticket_update(persisted_executor)
            persisted['ticket_recovery_query_catchup'] = self.replay_timo_ticket_expired_intake_items(
                guild_name=normalized_guild_name,
            )
        return persisted

    def delete_timo_guild_executor(self, guild_name: str) -> Dict[str, Any]:
        from app.timo_guild_identity import timo_guild_storage_name
        return self.delete_guild_executor(timo_guild_storage_name(guild_name), app_name='timo')

    def update_sogo_guild_executor(self, guild_name: str, payload: SugoGuildExecutorUpdateRequest) -> Dict[str, Any]:
        normalized_guild_name = str(guild_name or '').strip()
        if not normalized_guild_name:
            raise HTTPException(status_code=400, detail='guild_name is required')
        existing_executor = self.resolve_guild_executor(normalized_guild_name, app_name=SUGO_APP_NAME) or self.resolve_guild_executor(normalized_guild_name, app_name=SUGO_LEGACY_APP_NAME)
        requested_platform_backend_url = str(payload.platform_backend_url or '').strip()
        existing_platform_backend_url = str((existing_executor or {}).get('platform_backend_url') or '').strip()
        platform_backend_url = requested_platform_backend_url or existing_platform_backend_url or SUGO_DEFAULT_API_BASE_URL
        platform_authorization = str(payload.platform_authorization or '').strip()
        existing_authorization = str((existing_executor or {}).get('platform_authorization') or '').strip()
        cms_guild_id = str(payload.cms_guild_id or '').strip() or str((existing_executor or {}).get('cms_guild_id') or '').strip()
        cms_guild_sid = str(payload.cms_guild_sid or '').strip() or str((existing_executor or {}).get('cms_guild_sid') or '').strip()
        country = normalize_country_label(payload.country) or str((existing_executor or {}).get('country') or '').strip()
        bind_concurrency = max(1, int(payload.bind_concurrency if payload.bind_concurrency is not None else ((existing_executor or {}).get('bind_concurrency') or 1)))
        request_timeout_seconds = max(5, int(payload.request_timeout_seconds if payload.request_timeout_seconds is not None else ((existing_executor or {}).get('request_timeout_seconds') or 30)))
        login_username = str(payload.login_username or '').strip() or str((existing_executor or {}).get('login_username') or '').strip()
        now = utc_now()
        slug = re.sub(r'[^a-z0-9]+', '-', normalized_guild_name.lower()).strip('-') or 'default'
        with self.db.connect() as conn:
            collision = conn.execute(
                "SELECT app_name FROM guild_executors WHERE guild_name = ? AND LOWER(COALESCE(app_name, 'linky')) NOT IN (?, ?) LIMIT 1",
                (normalized_guild_name, SUGO_APP_NAME, SUGO_LEGACY_APP_NAME),
            ).fetchone()
            if collision:
                raise HTTPException(status_code=400, detail='guild_name_already_used_by_other_app_executor')
            conn.execute(
                """
                INSERT INTO guild_executors (
                    guild_name, app_name, backend_url, login_username, password_secret_ref, guild_backend_token,
                    oauth_token, oauth_token_secret, platform_backend_url, platform_authorization,
                    cms_guild_id, cms_guild_sid, country, proxy_url, proxy_region, proxy_type,
                    enabled, browser_profile_key, bind_concurrency, request_timeout_seconds, notes, updated_at
                ) VALUES (?, 'sugo', ?, ?, '', '', '', '', ?, ?, ?, ?, ?, '', '', 'http', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_name)
                DO UPDATE SET app_name = 'sugo',
                              backend_url = excluded.backend_url,
                              login_username = excluded.login_username,
                              platform_backend_url = excluded.platform_backend_url,
                              platform_authorization = CASE WHEN excluded.platform_authorization != '' THEN excluded.platform_authorization ELSE guild_executors.platform_authorization END,
                              cms_guild_id = CASE WHEN excluded.cms_guild_id != '' THEN excluded.cms_guild_id ELSE guild_executors.cms_guild_id END,
                              cms_guild_sid = CASE WHEN excluded.cms_guild_sid != '' THEN excluded.cms_guild_sid ELSE guild_executors.cms_guild_sid END,
                              country = excluded.country,
                              enabled = excluded.enabled,
                              browser_profile_key = excluded.browser_profile_key,
                              bind_concurrency = excluded.bind_concurrency,
                              request_timeout_seconds = excluded.request_timeout_seconds,
                              notes = excluded.notes,
                              updated_at = excluded.updated_at
                """,
                (
                    normalized_guild_name,
                    SUGO_DEFAULT_API_BASE_URL,
                    login_username,
                    platform_backend_url,
                    platform_authorization or existing_authorization,
                    cms_guild_id,
                    cms_guild_sid,
                    country,
                    1 if payload.enabled else 0,
                    f'sugo-{slug}',
                    bind_concurrency,
                    request_timeout_seconds,
                    str(payload.notes or '').strip(),
                    now,
                ),
            )
            cms_refresh_token = str(payload.cms_refresh_token or payload.refresh_token or '').strip()
            if cms_refresh_token:
                conn.execute(
                    """
                    INSERT INTO cms_executor_tokens(guild_name, refresh_token, updated_at)
                    VALUES(?, ?, datetime('now'))
                    ON CONFLICT(guild_name) DO UPDATE SET
                      refresh_token = excluded.refresh_token,
                      updated_at = datetime('now'),
                      last_refresh_error = NULL
                    """,
                    (normalized_guild_name, cms_refresh_token),
                )
            conn.commit()
        persisted = self.get_sogo_guild_executor(normalized_guild_name)
        persisted['saved'] = True
        return persisted

    def delete_sogo_guild_executor(self, guild_name: str) -> Dict[str, Any]:
        normalized_guild_name = str(guild_name or '').strip()
        if not normalized_guild_name:
            raise HTTPException(status_code=400, detail='guild_name is required')
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT guild_name FROM guild_executors WHERE guild_name = ? AND LOWER(COALESCE(app_name, 'linky')) IN (?, ?) LIMIT 1",
                (normalized_guild_name, SUGO_APP_NAME, SUGO_LEGACY_APP_NAME),
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail='guild executor not found')
            conn.execute(
                "DELETE FROM guild_executors WHERE guild_name = ? AND LOWER(COALESCE(app_name, 'linky')) IN (?, ?)",
                (normalized_guild_name, SUGO_APP_NAME, SUGO_LEGACY_APP_NAME),
            )
            conn.commit()
        return {'deleted': True, 'guild_name': normalized_guild_name}

    def retry_bind_submission(self, submission_id: str) -> Dict[str, Any]:
        normalized_submission_id = str(submission_id or '').strip()
        if not normalized_submission_id:
            raise HTTPException(status_code=400, detail='submission_id is required')
        preserved_payload_keys = (
            'source_channel',
            'expected_guild',
            'route_snapshot',
            'source_bot_app_id',
            'source_message_id',
            'source_chat_id',
            'executor_slot_key',
            'executor_slot_index',
            'executor_slot_count',
            'executor_slot_hidden',
        )
        with self.db.connect() as conn:
            submission = conn.execute("SELECT * FROM account_submissions WHERE submission_id = ?", (normalized_submission_id,)).fetchone()
            if not submission:
                raise HTTPException(status_code=404, detail='submission not found')
            submission_dict = dict(submission)
            lead = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (submission_dict['lead_id'],)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail='lead not found')
            account_id = str(submission_dict.get('recognized_account_id') or submission_dict.get('account_id') or '').strip()
            if not account_id:
                raise HTTPException(status_code=400, detail='submission has no account_id for bind retry')
            latest_bind_rows = conn.execute(
                "SELECT task_id, payload FROM automation_tasks WHERE lead_id = ? AND task_type = 'bind_check' ORDER BY created_at DESC LIMIT 10",
                (submission_dict['lead_id'],),
            ).fetchall()
            retry_task_id = create_id('task')
            retry_payload: Dict[str, Any] = {
                'submission_id': normalized_submission_id,
                'lead_id': submission_dict['lead_id'],
                'account_id': account_id,
            }
            for latest_row in latest_bind_rows:
                try:
                    latest_payload = json.loads((latest_row['payload'] if latest_row else '{}') or '{}')
                except Exception:
                    latest_payload = {}
                for key in preserved_payload_keys:
                    if key not in retry_payload and latest_payload.get(key):
                        retry_payload[key] = latest_payload.get(key)
                if all(key in retry_payload for key in preserved_payload_keys if key not in {'source_chat_id'}):
                    break
            created_at = utc_now()
            conn.execute(
                """
                INSERT INTO automation_tasks (
                    task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    retry_task_id,
                    submission_dict['lead_id'],
                    'bind_check',
                    'P0',
                    json.dumps(retry_payload, ensure_ascii=False),
                    f"bind_retry:{normalized_submission_id}:{retry_task_id}",
                    'system:retry_bind',
                    created_at,
                    'pending',
                ),
            )
            conn.execute(
                "UPDATE leads SET current_status = ?, updated_at = ? WHERE lead_id = ?",
                ('bind_check_pending', created_at, submission_dict['lead_id']),
            )
            self._record_status_history(
                conn,
                lead_id=submission_dict['lead_id'],
                from_status=str((lead['current_status'] if lead else '') or ''),
                to_status='bind_check_pending',
                trigger_type='technical_retry_bind',
                trigger_source='ops_retry_bind',
                trigger_task_id=retry_task_id,
                operator_name='system:retry_bind',
                remark=f'retry original submission {normalized_submission_id}',
            )
            conn.commit()
        return {
            'accepted': True,
            'retry_type': 'bind',
            'submission_id': normalized_submission_id,
            'task_id': retry_task_id,
            'next_action': 'queue_bind_check',
            'created_new_submission': False,
        }

    def retry_crm_submission(self, submission_id: str) -> Dict[str, Any]:
        normalized_submission_id = str(submission_id or '').strip()
        if not normalized_submission_id:
            raise HTTPException(status_code=400, detail='submission_id is required')
        if self.crm_adapter is None:
            raise HTTPException(status_code=400, detail='crm adapter not configured')
        with self.db.connect() as conn:
            submission = conn.execute("SELECT * FROM account_submissions WHERE submission_id = ?", (normalized_submission_id,)).fetchone()
            if not submission:
                raise HTTPException(status_code=404, detail='submission not found')
            submission_dict = dict(submission)
            lead = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (submission_dict['lead_id'],)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail='lead not found')
            latest_successful_bind = conn.execute(
                "SELECT task_id, result_reason, raw_result FROM automation_tasks WHERE lead_id = ? AND task_type = 'bind_check' AND status = 'success' ORDER BY COALESCE(finished_at, created_at) DESC LIMIT 1",
                (submission_dict['lead_id'],),
            ).fetchone()
            if not latest_successful_bind:
                raise HTTPException(status_code=400, detail='no successful bind context available for crm retry')
            retry_task_id = create_id('task')
            raw_result = json.loads(str(latest_successful_bind['raw_result'] or '{}')) if latest_successful_bind['raw_result'] else {}
            conn.execute(
                """
                INSERT INTO automation_tasks (
                    task_id, lead_id, task_type, priority, payload, dedupe_key, created_by, created_at, status, result_code, result_reason, finished_at, raw_result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    retry_task_id,
                    submission_dict['lead_id'],
                    'crm_sync_retry',
                    'P0',
                    json.dumps({'submission_id': normalized_submission_id, 'lead_id': submission_dict['lead_id'], 'account_id': str(submission_dict.get('recognized_account_id') or submission_dict.get('account_id') or '')}, ensure_ascii=False),
                    f"crm_retry:{normalized_submission_id}:{retry_task_id}",
                    'system:retry_crm',
                    utc_now(),
                    'success',
                    'crm_retry_started',
                    'crm retry started from latest successful bind context',
                    utc_now(),
                    json.dumps(raw_result, ensure_ascii=False),
                ),
            )
            crm_sync = self._sync_crm_after_bind_success(
                conn,
                lead_id=submission_dict['lead_id'],
                account_id=str(submission_dict.get('recognized_account_id') or submission_dict.get('account_id') or ''),
                task_id=retry_task_id,
                bind_result_reason=str(latest_successful_bind['result_reason'] or ''),
                bind_raw_result=raw_result,
            )
            created_group_join = None
            if not crm_sync['crm_sync_failed']:
                created_group_join = self._queue_group_join_after_verified_crm(
                    conn,
                    lead_id=submission_dict['lead_id'],
                    submission_id=normalized_submission_id,
                    account_id=str(submission_dict.get('recognized_account_id') or submission_dict.get('account_id') or ''),
                    created_at=utc_now(),
                )
            conn.commit()
        return {
            'accepted': crm_sync['crm_sync_failed'] is None,
            'retry_type': 'crm',
            'submission_id': normalized_submission_id,
            'task_id': retry_task_id,
            'next_action': 'queue_group_join' if created_group_join else 'retry_crm_sync',
            'result_reason': crm_sync['crm_sync_failed'],
            'crm_verified': crm_sync['crm_verified'],
            'created_new_submission': False,
            'group_join_task_id': (created_group_join or {}).get('group_join_task_id'),
        }

    def resubmit_corrected_submission(self, submission_id: str, payload: SubmissionResubmitRequest) -> Dict[str, Any]:
        normalized_submission_id = str(submission_id or '').strip()
        if not normalized_submission_id:
            raise HTTPException(status_code=400, detail='submission_id is required')
        with self.db.connect() as conn:
            submission = conn.execute("SELECT * FROM account_submissions WHERE submission_id = ?", (normalized_submission_id,)).fetchone()
            if not submission:
                raise HTTPException(status_code=404, detail='submission not found')
            submission_dict = dict(submission)
            lead = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (submission_dict['lead_id'],)).fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail='lead not found')
            lead_dict = dict(lead)
        final_mobile = str(payload.mobile or '').strip() or format_display_phone(lead_dict.get('mobile'), area_code=lead_dict.get('area_code'))
        final_group = str(payload.registration_group or '').strip() or str(lead_dict.get('pendaftaran_group') or '').strip()
        final_code = str(payload.invite_code or '').strip().upper() or str(lead_dict.get('inviter_id') or '').strip().upper()
        final_account_id = str(payload.account_id or '').strip() or str(submission_dict.get('recognized_account_id') or submission_dict.get('account_id') or lead_dict.get('yw_id') or '').strip()
        resubmit = self._submit_manual_cs_sync(
            ManualCsSubmissionRequest(
                mobile=final_mobile,
                registration_group=final_group,
                app_name=str(lead_dict.get('app_name') or '').strip(),
                dept_name=str(lead_dict.get('dept_name') or '').strip(),
                invite_code=final_code,
                submission_type='account_id',
                account_id=final_account_id,
                submitted_by=str(payload.corrected_by or '').strip(),
                source_channel=str(submission_dict.get('source_channel') or 'manual_cs_lark'),
                remark=(str(payload.remark or '').strip() or str(submission_dict.get('remark') or '').strip() or None),
                submitted_at=payload.submitted_at,
            )
        )
        updated_lead_id = str(resubmit.get('lead_id') or submission_dict['lead_id'])
        corrections = {
            'mobile': (format_display_phone(lead_dict.get('mobile'), area_code=lead_dict.get('area_code')), final_mobile),
            'registration_group': (str(lead_dict.get('pendaftaran_group') or '').strip(), final_group),
            'invite_code': (str(lead_dict.get('inviter_id') or '').strip().upper(), final_code),
            'account_id': (str(lead_dict.get('yw_id') or '').strip(), final_account_id),
        }
        now = utc_now()
        with self.db.connect() as conn:
            correction_count = 0
            for field_name, (old_value, new_value) in corrections.items():
                if str(old_value or '') == str(new_value or ''):
                    continue
                correction_count += 1
                conn.execute(
                    """
                    INSERT INTO lead_corrections (
                        correction_id, lead_id, field_name, old_value, new_value, corrected_by, review_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (create_id('corr'), updated_lead_id, field_name, old_value, new_value, payload.corrected_by, normalized_submission_id, now),
                )
            if correction_count:
                conn.execute(
                    "UPDATE leads SET correction_count = COALESCE(correction_count, 0) + ?, updated_at = ? WHERE lead_id = ?",
                    (correction_count, now, updated_lead_id),
                )
            conn.commit()
        resubmit['original_submission_id'] = normalized_submission_id
        resubmit['created_new_submission'] = True
        resubmit['resubmit_type'] = 'manual_corrected_submission'
        return resubmit

    def exception_queue(self) -> Dict[str, Any]:
        rows: list[Dict[str, Any]] = []
        with self.db.connect() as conn:
            bind_rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.lead_id, COALESCE(l.mobile, '') AS mobile, COALESCE(l.yw_id, '') AS account_id,
                       COALESCE(l.pendaftaran_group, '') AS registration_group, COALESCE(l.dept_name, '') AS guild_name,
                       t.result_code, t.result_reason, COALESCE(t.finished_at, t.created_at) AS created_at
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'bind_check' AND t.status = 'failed'
                ORDER BY COALESCE(t.finished_at, t.created_at) DESC
                LIMIT 50
                """
            ).fetchall()]
            for row in bind_rows:
                human = self._classify_bind_human_action(result_code=row.get('result_code'), result_reason=row.get('result_reason'), raw_result={})
                rows.append({
                    'lead_id': row['lead_id'],
                    'submission_id': None,
                    'task_id': row['task_id'],
                    'current_status': 'bind_failed',
                    'exception_type': human['human_action_type'] or 'bind_failure',
                    'reason': row['result_reason'],
                    'latest_action': 'retry_bind' if not human['requires_human_action'] else 'manual_reauth',
                    'guild_name': row['guild_name'],
                    'mobile': row['mobile'],
                    'account_id': row['account_id'],
                    'registration_group': row['registration_group'],
                    'created_at': row['created_at'],
                })
            crm_rows = [dict(r) for r in conn.execute(
                """
                SELECT n.notification_id, n.lead_id, n.mobile, n.yw_id, n.reason, n.created_at,
                       COALESCE(l.current_status, '') AS current_status, COALESCE(l.dept_name, '') AS guild_name,
                       COALESCE(l.pendaftaran_group, '') AS registration_group
                FROM operator_notifications n
                LEFT JOIN leads l ON l.lead_id = n.lead_id
                WHERE n.notification_type = 'crm_record_failed' AND n.is_read = 0
                ORDER BY n.created_at DESC
                LIMIT 50
                """
            ).fetchall()]
            for row in crm_rows:
                rows.append({
                    'lead_id': row['lead_id'],
                    'submission_id': None,
                    'task_id': row['notification_id'],
                    'current_status': row['current_status'],
                    'exception_type': 'crm_failure',
                    'reason': row['reason'],
                    'latest_action': 'retry_crm',
                    'guild_name': row['guild_name'],
                    'mobile': row['mobile'],
                    'account_id': row['yw_id'],
                    'registration_group': row['registration_group'],
                    'created_at': row['created_at'],
                })
            group_rows = [dict(r) for r in conn.execute(
                """
                SELECT t.task_id, t.lead_id, t.result_reason, t.raw_result, COALESCE(t.finished_at, t.created_at) AS created_at,
                       COALESCE(l.mobile, '') AS mobile, COALESCE(l.yw_id, '') AS account_id,
                       COALESCE(l.pendaftaran_group, '') AS registration_group, COALESCE(l.dept_name, '') AS guild_name,
                       COALESCE(l.current_status, '') AS current_status
                FROM automation_tasks t
                LEFT JOIN leads l ON l.lead_id = t.lead_id
                WHERE t.task_type = 'group_join' AND t.status = 'failed'
                ORDER BY COALESCE(t.finished_at, t.created_at) DESC
                LIMIT 50
                """
            ).fetchall()]
            for row in group_rows:
                latest_action = 'retry_group_join'
                try:
                    raw_result = json.loads(row.get('raw_result') or '{}')
                except Exception:
                    raw_result = {}
                disposition = str(raw_result.get('execution_disposition') or '').strip().lower()
                if disposition == 'retryable_failed':
                    latest_action = 'retry_official_group_approval'
                elif disposition == 'manual_required':
                    latest_action = 'manual_continue_official_group_approval'
                rows.append({
                    'lead_id': row['lead_id'],
                    'submission_id': None,
                    'task_id': row['task_id'],
                    'current_status': row['current_status'],
                    'exception_type': 'group_join_failure',
                    'reason': row['result_reason'],
                    'latest_action': latest_action,
                    'guild_name': row['guild_name'],
                    'mobile': row['mobile'],
                    'account_id': row['account_id'],
                    'registration_group': row['registration_group'],
                    'created_at': row['created_at'],
                })
            timeout_rows = [dict(r) for r in conn.execute(
                """
                SELECT s.submission_id, s.lead_id, s.created_at, COALESCE(l.current_status, '') AS current_status,
                       COALESCE(l.mobile, '') AS mobile, COALESCE(l.yw_id, '') AS account_id,
                       COALESCE(l.pendaftaran_group, '') AS registration_group, COALESCE(l.dept_name, '') AS guild_name
                FROM account_submissions s
                LEFT JOIN leads l ON l.lead_id = s.lead_id
                WHERE l.current_status IN ('account_submitted','bind_check_pending','bind_success','group_join_pending')
                ORDER BY s.created_at DESC
                LIMIT 100
                """
            ).fetchall()]
        now_dt = parse_iso_datetime(utc_now())
        for row in timeout_rows:
            created_dt = parse_iso_datetime(str(row['created_at']))
            if (now_dt - created_dt).total_seconds() < 300:
                continue
            rows.append({
                'lead_id': row['lead_id'],
                'submission_id': row['submission_id'],
                'task_id': None,
                'current_status': row['current_status'],
                'exception_type': 'submission_timeout',
                'reason': 'submission has not reached a terminal state within 5 minutes',
                'latest_action': 'inspect_timeline',
                'guild_name': row['guild_name'],
                'mobile': row['mobile'],
                'account_id': row['account_id'],
                'registration_group': row['registration_group'],
                'created_at': row['created_at'],
            })
        rows.sort(key=lambda item: str(item.get('created_at') or ''), reverse=True)
        return {'rows': rows[:100]}

    def sla_summary(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            intake_rows = [dict(r) for r in conn.execute(
                """
                SELECT item_id, guild_name, system_status, feedback_status, result_code, created_at, processed_at, feedback_done_at
                FROM ops_intake_items
                WHERE COALESCE(feedback_status, '') != 'cleared'
                ORDER BY created_at DESC
                LIMIT 500
                """
            ).fetchall()]
            failure_rows = [dict(r) for r in conn.execute(
                """
                SELECT notification_type, COALESCE(reason, '') AS reason, COUNT(*) AS cnt
                FROM operator_notifications
                WHERE write_result = 'failed'
                GROUP BY notification_type, COALESCE(reason, '')
                ORDER BY cnt DESC, notification_type ASC
                LIMIT 10
                """
            ).fetchall()]
        now_dt = parse_iso_datetime(utc_now())
        total = len(intake_rows)
        success_count = 0
        failed_count = 0
        pending_count = 0
        timeout_count = 0
        by_guild: Dict[str, Dict[str, Any]] = {}
        success_statuses = {'fully_success'}
        failure_statuses = {'partial_success_crm_failed', 'bind_failed', 'validation_failed', 'manual_required', 'route_mismatch'}
        for row in intake_rows:
            status = str(row.get('system_status') or '').strip()
            feedback_status = str(row.get('feedback_status') or '').strip()
            result_code = str(row.get('result_code') or '').strip().lower()
            guild = str(row.get('guild_name') or '').strip() or '-'
            bucket = by_guild.setdefault(guild, {'guild_name': guild, 'submission_count': 0, 'success_count': 0, 'failed_count': 0, 'pending_count': 0})
            bucket['submission_count'] += 1
            is_success = status in success_statuses or result_code == 'bind_success'
            is_failed = status in failure_statuses or result_code in {'bind_failed', 'validation_failed', 'route_mismatch'}
            is_feedback_done = feedback_status == 'feedback_done'
            if is_success and is_feedback_done:
                success_count += 1
                bucket['success_count'] += 1
            elif is_failed and is_feedback_done:
                failed_count += 1
                bucket['failed_count'] += 1
            else:
                pending_count += 1
                bucket['pending_count'] += 1
                created_at = str(row.get('created_at') or '')
                if created_at:
                    try:
                        if (now_dt - parse_iso_datetime(created_at)).total_seconds() >= 300:
                            timeout_count += 1
                    except Exception:
                        pass
        top_failure_reasons = [
            {
                'notification_type': row['notification_type'],
                'reason': row['reason'],
                'count': int(row['cnt'] or 0),
            }
            for row in failure_rows
        ]
        return {
            'scope': 'ops_intake_items_current',
            'submission_total': total,
            'success_count': success_count,
            'failed_count': failed_count,
            'pending_count': pending_count,
            'timeout_over_5m_count': timeout_count,
            'top_failure_reasons': top_failure_reasons,
            'by_guild': sorted(by_guild.values(), key=lambda item: item['guild_name']),
        }

    def delete_guild_executor(self, guild_name: str, *, app_name: str = 'linky') -> Dict[str, Any]:
        normalized_guild_name = str(guild_name or '').strip()
        if not normalized_guild_name:
            raise HTTPException(status_code=400, detail='guild_name is required')
        normalized_app = str(app_name or 'linky').strip().lower() or 'linky'
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT guild_name FROM guild_executors WHERE guild_name = ? AND LOWER(COALESCE(app_name, 'linky')) = ?",
                (normalized_guild_name, normalized_app),
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail='guild executor not found')
            conn.execute("DELETE FROM guild_executors WHERE guild_name = ? AND LOWER(COALESCE(app_name, 'linky')) = ?", (normalized_guild_name, normalized_app))
            conn.commit()
        return {'deleted': True, 'guild_name': normalized_guild_name}

    def _find_crm_dept_row_by_name(self, rows: Any, guild_name: str) -> Optional[Dict[str, Any]]:
        normalized = str(guild_name or '').strip().lower()
        if not normalized:
            return None
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            for key in ('deptName', 'name', 'label', 'value'):
                if str(row.get(key) or '').strip().lower() == normalized:
                    return dict(row)
        return None

    def _ensure_crm_dept_for_guild_executor(self, guild_name: str) -> Dict[str, Any]:
        normalized_guild_name = str(guild_name or '').strip()
        if not normalized_guild_name:
            return {'synced': False, 'status': 'skipped', 'reason': 'empty_guild_name'}
        if self.crm_adapter is None or not hasattr(self.crm_adapter, 'get_depts'):
            return {'synced': False, 'status': 'skipped', 'reason': 'crm_adapter_not_configured'}
        try:
            rows = self.crm_adapter.get_depts()
            self._cache_crm_option_rows(option_type='guild', rows=rows, candidate_keys=['deptName', 'name', 'label', 'value'])
            existing = self._find_crm_dept_row_by_name(rows, normalized_guild_name)
            if existing:
                return {
                    'synced': True,
                    'status': 'existing',
                    'deptName': str(existing.get('deptName') or existing.get('name') or normalized_guild_name).strip(),
                    'deptId': str(existing.get('deptId') or existing.get('id') or existing.get('value') or '').strip(),
                }
            if not hasattr(self.crm_adapter, 'create_dept'):
                return {'synced': False, 'status': 'skipped', 'reason': 'crm_create_dept_not_supported'}
            body = self.crm_adapter.create_dept(name=normalized_guild_name, pid=0, sort=0)
            if isinstance(body, dict) and body.get('code') not in (None, 0):
                msg = str(body.get('msg') or '').strip()
                if '已存在' not in msg and 'already exists' not in msg.lower() and 'duplicate' not in msg.lower():
                    raise RuntimeError(msg or str(body))
            refreshed_rows = self.crm_adapter.get_depts()
            self._cache_crm_option_rows(option_type='guild', rows=refreshed_rows, candidate_keys=['deptName', 'name', 'label', 'value'])
            created = self._find_crm_dept_row_by_name(refreshed_rows, normalized_guild_name) or {}
            return {
                'synced': True,
                'status': 'created' if created else 'created_unverified',
                'deptName': str(created.get('deptName') or created.get('name') or normalized_guild_name).strip(),
                'deptId': str(created.get('deptId') or created.get('id') or created.get('value') or '').strip(),
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f'crm_dept_sync_failed: {exc}')

    def update_guild_executor(self, guild_name: str, payload: GuildExecutorUpdateRequest) -> Dict[str, Any]:
        normalized_guild_name = str(guild_name or '').strip()
        if not normalized_guild_name:
            raise HTTPException(status_code=400, detail='guild_name is required')
        existing_executor = self.resolve_guild_executor(normalized_guild_name, app_name='linky')
        requested_platform_backend_url = str(payload.platform_backend_url or '').strip()
        existing_platform_backend_url = str((existing_executor or {}).get('platform_backend_url') or '').strip()
        platform_backend_url_candidate = requested_platform_backend_url or existing_platform_backend_url
        platform_backend_url_lower = platform_backend_url_candidate.lower()
        platform_backend_url = platform_backend_url_candidate if ('touchchat' in platform_backend_url_lower or 'timo' in platform_backend_url_lower) else PLATFORM_BACKEND_BASE_URL
        requested_contract = guild_country_contract({
            'country': payload.country,
            'guild_country': payload.guild_country or payload.country or (existing_executor or {}).get('guild_country') or (existing_executor or {}).get('country'),
            'eligible_user_countries': payload.eligible_user_countries or (existing_executor or {}).get('eligible_user_countries'),
            'routing_region': payload.routing_region if payload.routing_region is not None else (existing_executor or {}).get('routing_region'),
        })
        row = {
            'guild_name': normalized_guild_name,
            'app_name': 'linky',
            'backend_url': GUILD_BACKEND_BASE_URL,
            'login_username': str(payload.login_username or '').strip(),
            'password_secret_ref': str(payload.password_secret_ref or '').strip(),
            'guild_backend_token': str(payload.guild_backend_token or '').strip(),
            'oauth_token': str(payload.oauth_token or '').strip(),
            'oauth_token_secret': str(payload.oauth_token_secret or '').strip(),
            'platform_backend_url': platform_backend_url,
            'platform_authorization': str(payload.platform_authorization or payload.platform_backend_token or '').strip(),
            'cms_guild_id': str(payload.cms_guild_id or '').strip(),
            'cms_guild_sid': str(payload.cms_guild_sid or '').strip(),
            'country': requested_contract['guild_country'],
            'guild_country': requested_contract['guild_country'],
            'eligible_user_countries': requested_contract['eligible_user_countries'],
            'routing_region': requested_contract['routing_region'],
            'proxy_url': str(payload.proxy_url or '').strip(),
            'proxy_region': str(payload.proxy_region or '').strip(),
            'proxy_type': str(payload.proxy_type or '').strip() or 'http',
            'enabled': 1 if payload.enabled else 0,
            'browser_profile_key': str(payload.browser_profile_key or '').strip(),
            'bind_concurrency': max(1, int(payload.bind_concurrency or 1)),
            'request_timeout_seconds': max(5, int(payload.request_timeout_seconds or 30)),
            'notes': str(payload.notes or '').strip(),
            'updated_at': utc_now(),
        }
        if not row['browser_profile_key']:
            slug = re.sub(r'[^a-z0-9]+', '-', normalized_guild_name.lower()).strip('-') or 'default'
            row['browser_profile_key'] = f'guild-{slug}'
        if not (
            (row['oauth_token'] and row['oauth_token_secret'])
            or (existing_executor and existing_executor.get('oauth_configured') and not row['oauth_token'] and not row['oauth_token_secret'])
        ):
            raise HTTPException(status_code=400, detail='oauth_token and oauth_token_secret are required')
        if row['proxy_region'] and row['proxy_region'] not in GUILD_EXECUTOR_PROXY_REGION_VALUES:
            raise HTTPException(status_code=400, detail='proxy_region must be one of the configured city options')
        crm_dept_sync = {'synced': False, 'status': 'not_checked'}
        effective_platform_authorization = row['platform_authorization'] or str((existing_executor or {}).get('platform_authorization') or '').strip()
        with self.db.connect() as conn:
            collision = conn.execute(
                "SELECT app_name FROM guild_executors WHERE guild_name = ? AND LOWER(COALESCE(app_name, 'linky')) != 'linky' LIMIT 1",
                (row['guild_name'],),
            ).fetchone()
            if collision:
                raise HTTPException(status_code=400, detail='guild_name_already_used_by_timo_executor')
            if row['proxy_region']:
                existing_region_owner = conn.execute(
                    "SELECT guild_name FROM guild_executors WHERE proxy_region = ? AND guild_name != ? LIMIT 1",
                    (row['proxy_region'], row['guild_name']),
                ).fetchone()
                if existing_region_owner:
                    raise HTTPException(status_code=400, detail=f"proxy_region is already assigned to guild {existing_region_owner['guild_name']}")
            crm_dept_sync = self._ensure_crm_dept_for_guild_executor(normalized_guild_name)
            conn.execute(
                """
                INSERT INTO guild_executors (
                    guild_name, app_name, backend_url, login_username, password_secret_ref, guild_backend_token, oauth_token, oauth_token_secret, platform_backend_url, platform_authorization, cms_guild_id, cms_guild_sid, country, guild_country, eligible_user_countries, routing_region, proxy_url, proxy_region,
                    proxy_type, enabled, browser_profile_key, bind_concurrency, request_timeout_seconds,
                    notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_name)
                DO UPDATE SET app_name = excluded.app_name,
                              backend_url = excluded.backend_url,
                              login_username = excluded.login_username,
                              password_secret_ref = CASE WHEN excluded.password_secret_ref != '' THEN excluded.password_secret_ref ELSE guild_executors.password_secret_ref END,
                              guild_backend_token = CASE WHEN excluded.guild_backend_token != '' THEN excluded.guild_backend_token ELSE guild_executors.guild_backend_token END,
                              oauth_token = CASE WHEN excluded.oauth_token != '' THEN excluded.oauth_token ELSE guild_executors.oauth_token END,
                              oauth_token_secret = CASE WHEN excluded.oauth_token_secret != '' THEN excluded.oauth_token_secret ELSE guild_executors.oauth_token_secret END,
                              platform_backend_url = excluded.platform_backend_url,
                              platform_authorization = CASE WHEN excluded.platform_authorization != '' THEN excluded.platform_authorization ELSE guild_executors.platform_authorization END,
                              cms_guild_id = CASE WHEN excluded.cms_guild_id != '' THEN excluded.cms_guild_id ELSE guild_executors.cms_guild_id END,
                              cms_guild_sid = CASE WHEN excluded.cms_guild_sid != '' THEN excluded.cms_guild_sid ELSE guild_executors.cms_guild_sid END,
                              country = excluded.country,
                              guild_country = excluded.guild_country,
                              eligible_user_countries = excluded.eligible_user_countries,
                              routing_region = excluded.routing_region,
                              proxy_url = excluded.proxy_url,
                              proxy_region = excluded.proxy_region,
                              proxy_type = excluded.proxy_type,
                              enabled = excluded.enabled,
                              browser_profile_key = excluded.browser_profile_key,
                              bind_concurrency = excluded.bind_concurrency,
                              request_timeout_seconds = excluded.request_timeout_seconds,
                              notes = excluded.notes,
                              updated_at = excluded.updated_at
                """,
                (
                    row['guild_name'], row['app_name'], row['backend_url'], row['login_username'], row['password_secret_ref'], row['guild_backend_token'], row['oauth_token'], row['oauth_token_secret'], row['platform_backend_url'], row['platform_authorization'], row['cms_guild_id'], row['cms_guild_sid'], row['country'], row['guild_country'], json.dumps(row['eligible_user_countries'], ensure_ascii=False), row['routing_region'], row['proxy_url'], row['proxy_region'],
                    row['proxy_type'], row['enabled'], row['browser_profile_key'], row['bind_concurrency'], row['request_timeout_seconds'],
                    row['notes'], row['updated_at'],
                ),
            )
            cms_refresh_token = str(payload.cms_refresh_token or payload.refresh_token or '').strip()
            if cms_refresh_token:
                conn.execute(
                    """
                    INSERT INTO cms_executor_tokens(guild_name, refresh_token, updated_at)
                    VALUES(?, ?, datetime('now'))
                    ON CONFLICT(guild_name) DO UPDATE SET
                      refresh_token = excluded.refresh_token,
                      updated_at = datetime('now'),
                      last_refresh_error = NULL
                    """,
                    (normalized_guild_name, cms_refresh_token),
                )
            conn.commit()
        resolved_executor = self.resolve_guild_executor(normalized_guild_name) or row
        cms_lock_sync = self.sync_guild_executor_cms_lock(
            normalized_guild_name,
            authorization=effective_platform_authorization,
            proxy_url=self._resolve_executor_proxy_url(resolved_executor),
            timeout_seconds=float(row['request_timeout_seconds']),
        )
        persisted = self.get_guild_executor(normalized_guild_name)
        persisted['saved'] = True
        persisted['crm_guild_sync'] = crm_dept_sync
        persisted['cms_lock_sync'] = cms_lock_sync
        return persisted

    def ensure_current_intake_preset(self) -> Dict[str, Any]:
        rows = self._fetch_intake_bot_preset_rows()
        existing = next((row for row in rows if row.get('profile_name') == 'current'), None)
        if existing:
            self.lark_default_app_name = str(existing.get('default_app') or '').strip() or self.lark_default_app_name
            self.lark_default_dept_name = str(existing.get('default_guild') or '').strip() or self.lark_default_dept_name
            if existing.get('app_id'):
                self.current_lark_app_id = existing.get('app_id')
            if self.lark_default_app_name:
                self._resolve_crm_app_mapping(self.lark_default_app_name)
            if self.lark_default_dept_name:
                self._resolve_crm_dept_mapping(self.lark_default_dept_name)
            return existing
        return self._upsert_intake_bot_preset_row(
            profile_name='current',
            app_id=self.current_lark_app_id,
            robot_name='current',
            default_app=self.lark_default_app_name or '',
            default_guild=self.lark_default_dept_name or '',
            enabled=1,
        )

    def resolve_intake_bot_preset(self, *, app_id: Optional[str] = None, profile_name: Optional[str] = None) -> Dict[str, Any]:
        current_row = self.ensure_current_intake_preset()
        rows = self._fetch_intake_bot_preset_rows()
        normalized_profile = str(profile_name or '').strip()
        normalized_app_id = str(app_id or '').strip()
        if normalized_profile:
            matched = next((row for row in rows if str(row.get('profile_name') or '').strip() == normalized_profile), None)
            if matched:
                return {**matched, 'matched_by': 'profile_name'}
        if normalized_app_id:
            matched = next((row for row in rows if str(row.get('app_id') or '').strip() == normalized_app_id), None)
            if matched:
                return {**matched, 'matched_by': 'app_id'}
        return {**current_row, 'matched_by': 'fallback_current'}

    def _normalize_crm_dropdown_options(self, rows: Any, *, candidate_keys: list[str]) -> list[dict[str, str]]:
        seen: set[str] = set()
        options: list[dict[str, str]] = []
        for row in rows or []:
            if isinstance(row, dict):
                raw_value = next((row.get(key) for key in candidate_keys if row.get(key)), None)
            else:
                raw_value = next((getattr(row, key, None) for key in candidate_keys if getattr(row, key, None)), None)
            value = str(raw_value or '').strip()
            if not value or value in seen:
                continue
            seen.add(value)
            options.append({'label': value, 'value': value})
        options.sort(key=lambda item: item['label'].lower())
        return options

    def _list_cached_crm_dropdown_options(self, *, option_type: str, candidate_keys: list[str]) -> list[dict[str, str]]:
        rows = list((self._crm_option_cache.get(option_type) or {}).values())
        return self._normalize_crm_dropdown_options(rows, candidate_keys=candidate_keys)

    def _crm_dropdown_candidate_keys(self, option_type: str) -> list[str]:
        if option_type == 'app':
            return ['name', 'ywName', 'appName', 'label', 'value']
        return ['deptName', 'name', 'label', 'value']

    def _list_crm_dropdown_options(self, *, option_type: str) -> Dict[str, Any]:
        candidate_keys = self._crm_dropdown_candidate_keys(option_type)
        if self.crm_adapter is not None:
            try:
                if option_type == 'app' and hasattr(self.crm_adapter, 'get_apps'):
                    rows = self.crm_adapter.get_apps()
                elif option_type == 'guild' and hasattr(self.crm_adapter, 'get_depts'):
                    rows = self.crm_adapter.get_depts()
                else:
                    rows = []
                self._cache_crm_option_rows(option_type=option_type, rows=rows, candidate_keys=candidate_keys)
                options = self._normalize_crm_dropdown_options(rows, candidate_keys=candidate_keys)
                return {'options': options, 'source': 'live' if options else 'unavailable'}
            except Exception as exc:
                print(f'Failed to load CRM dropdown options for {option_type}: {exc}')
        cached_options = self._list_cached_crm_dropdown_options(option_type=option_type, candidate_keys=candidate_keys)
        if cached_options:
            return {'options': cached_options, 'source': 'cache'}
        return {'options': [], 'source': 'unavailable'}

    def _cache_crm_option_rows(self, *, option_type: str, rows: Any, candidate_keys: list[str], persist: bool = True) -> None:
        bucket = self._crm_option_cache.setdefault(option_type, {})
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            persisted = False
            for key in candidate_keys:
                raw_value = str(row.get(key) or '').strip()
                if raw_value:
                    bucket[raw_value.lower()] = dict(row)
                    if persist and not persisted:
                        self._persist_crm_option_row(option_type=option_type, display_name=raw_value, row=dict(row))
                        persisted = True

    def _get_cached_crm_option_row(self, *, option_type: str, display_name: Optional[str]) -> Optional[Dict[str, Any]]:
        normalized_name = str(display_name or '').strip().lower()
        if not normalized_name:
            return None
        cached = self._crm_option_cache.get(option_type, {}).get(normalized_name)
        if not cached:
            return None
        row = dict(cached)
        row['_mapping_source'] = 'cache'
        return row

    def _resolve_crm_option_row(self, *, option_type: str, display_name: Optional[str]) -> Optional[Dict[str, Any]]:
        normalized_name = str(display_name or '').strip()
        if not normalized_name:
            return None
        cached = self._get_cached_crm_option_row(option_type=option_type, display_name=display_name)
        if cached:
            return cached
        if self.crm_adapter is None:
            return None
        try:
            if option_type == 'app' and hasattr(self.crm_adapter, 'get_apps'):
                rows = self.crm_adapter.get_apps()
                candidate_keys = ['name', 'ywName', 'appName', 'label', 'value']
            elif option_type == 'guild' and hasattr(self.crm_adapter, 'get_depts'):
                rows = self.crm_adapter.get_depts()
                candidate_keys = ['deptName', 'name', 'label', 'value']
            else:
                return self._get_cached_crm_option_row(option_type=option_type, display_name=display_name)
        except Exception as exc:
            cached = self._get_cached_crm_option_row(option_type=option_type, display_name=display_name)
            if cached:
                return cached
            print(f'Failed to resolve CRM option row for {option_type}: {exc}')
            return {
                '_mapping_error': str(exc),
                '_mapping_source': 'unavailable',
            }

        # This resolver is used inside binding/CRM result paths that may already
        # hold a SQLite write transaction. Persisting option-cache rows through a
        # second connection here can block on SQLite busy timeout and add minutes
        # of latency before CRM sync. Keep the in-memory cache warm, but reserve
        # durable cache writes for explicit dropdown/list refresh paths.
        self._cache_crm_option_rows(option_type=option_type, rows=rows, candidate_keys=candidate_keys, persist=False)
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            for key in candidate_keys:
                raw_value = row.get(key)
                if str(raw_value or '').strip().lower() == normalized_name.lower():
                    live_row = dict(row)
                    live_row['_mapping_source'] = 'live'
                    return live_row
        return self._get_cached_crm_option_row(option_type=option_type, display_name=display_name)

    def _resolve_crm_app_mapping(self, app_name: Optional[str]) -> Dict[str, str]:
        row = self._resolve_crm_option_row(option_type='app', display_name=app_name)
        resolved_name = str(app_name or '').strip()
        if not row:
            return {'appName': resolved_name, 'appId': '', 'mapping_source': 'missing'}
        if row.get('_mapping_error'):
            return {
                'appName': resolved_name,
                'appId': '',
                'mapping_source': str(row.get('_mapping_source') or 'unavailable'),
                'mapping_error': str(row.get('_mapping_error') or ''),
            }
        return {
            'appName': str(
                row.get('name')
                or row.get('ywName')
                or row.get('appName')
                or resolved_name
            ).strip(),
            'appId': str(row.get('id') or row.get('appId') or row.get('value') or '').strip(),
            'mapping_source': str(row.get('_mapping_source') or 'live'),
        }

    def _resolve_crm_dept_mapping(self, dept_name: Optional[str], dept_id: Optional[str] = None) -> Dict[str, str]:
        resolved_dept_id = str(dept_id or '').strip()
        resolved_name = str(dept_name or '').strip()
        if resolved_dept_id:
            return {'deptName': resolved_name, 'deptId': resolved_dept_id, 'mapping_source': 'provided'}
        row = self._resolve_crm_option_row(option_type='guild', display_name=dept_name)
        if not row:
            return {'deptName': resolved_name, 'deptId': '', 'mapping_source': 'missing'}
        if row.get('_mapping_error'):
            return {
                'deptName': resolved_name,
                'deptId': '',
                'mapping_source': str(row.get('_mapping_source') or 'unavailable'),
                'mapping_error': str(row.get('_mapping_error') or ''),
            }
        return {
            'deptName': str(row.get('deptName') or row.get('name') or resolved_name).strip(),
            'deptId': str(row.get('deptId') or row.get('id') or row.get('value') or '').strip(),
            'mapping_source': str(row.get('_mapping_source') or 'live'),
        }

    def _precheck_crm_mapping_failure(self, *, resolved_app: Dict[str, Any], resolved_dept: Dict[str, Any]) -> Optional[str]:
        app_name = str(resolved_app.get('appName') or '').strip()
        app_id = str(resolved_app.get('appId') or '').strip()
        app_mapping_error = str(resolved_app.get('mapping_error') or '').strip()
        app_mapping_source = str(resolved_app.get('mapping_source') or '').strip()
        if app_name and not app_id:
            if app_mapping_source == 'unavailable' or 'get_apps' in app_mapping_error or 'non-json' in app_mapping_error.lower() or '502' in app_mapping_error:
                return 'Please retry once.'
            return 'CRM app mapping is missing. Please contact the administrator.'
        return None

    def _crm_response_looks_like_duplicate(self, crm_response: Dict[str, Any]) -> bool:
        code = crm_response.get('code')
        msg = str(crm_response.get('msg') or '')
        if code == 10002:
            return True
        lowered = msg.lower()
        return ('已存在' in msg) or ('duplicate' in lowered) or ('already exists' in lowered)

    def _extract_crm_duplicate_hints(self, crm_response: Dict[str, Any]) -> Dict[str, str]:
        msg = str(crm_response.get('msg') or '')
        hints: Dict[str, str] = {}
        id_match = re.search(r'用户\s*ID\s*(\d{6,12})', msg, flags=re.IGNORECASE)
        mobile_match = re.search(r'手机号码\s*(\d{6,15})', msg)
        if id_match:
            hints['yw_id'] = id_match.group(1)
        if mobile_match:
            hints['mobile'] = mobile_match.group(1)
        return hints

    def _crm_mobile_matches_expected(self, *, expected_mobile: Optional[str], actual_mobile: Optional[str]) -> bool:
        expected = str(expected_mobile or '').strip()
        actual = str(actual_mobile or '').strip()
        if not expected:
            return True
        if not actual:
            return False
        if expected == actual:
            return True
        expected_keys = self._official_group_phone_match_keys(phone=expected)
        actual_keys = self._official_group_phone_match_keys(phone=actual)
        if expected_keys.intersection(actual_keys):
            return True
        expected_digits = ''.join(ch for ch in expected if ch.isdigit())
        actual_digits = ''.join(ch for ch in actual if ch.isdigit())
        if expected_digits and actual_digits and expected_digits == actual_digits:
            return True
        for prefix in sorted(PHONE_PREFIX_COUNTRY_MAP.keys(), key=len, reverse=True):
            if expected_digits.startswith(prefix) and expected_digits[len(prefix):] == actual_digits:
                return True
            if actual_digits.startswith(prefix) and actual_digits[len(prefix):] == expected_digits:
                return True
        return False

    def _crm_row_matches_expected(
        self,
        row: Dict[str, Any],
        *,
        yw_id: Optional[str] = None,
        mobile: Optional[str] = None,
        app_name: Optional[str] = None,
        dept_name: Optional[str] = None,
        registration_group: Optional[str] = None,
        official_group: Optional[str] = None,
        allow_empty_mobile_match: bool = False,
    ) -> bool:
        if not row:
            return False
        expected_pairs = [
            (str(yw_id or '').strip(), str(row.get('ywId') or '').strip()),
            (str(app_name or '').strip(), str(row.get('appName') or '').strip()),
            (str(dept_name or '').strip(), str(row.get('deptName') or '').strip()),
            (str(registration_group or '').strip(), str(row.get('pendaftaranGroup') or '').strip()),
            (str(official_group or '').strip(), str(row.get('wa') or '').strip()),
        ]
        for expected, actual in expected_pairs:
            if expected and expected != actual:
                return False
        if allow_empty_mobile_match and mobile and not str(row.get('mobile') or '').strip():
            return True
        return self._crm_mobile_matches_expected(expected_mobile=mobile, actual_mobile=row.get('mobile'))

    def _find_existing_customer_with_fallback(self, *, yw_id: Optional[str], mobile: Optional[str], crm_response: Optional[Dict[str, Any]] = None, app_name: Optional[str] = None, dept_name: Optional[str] = None, registration_group: Optional[str] = None, official_group: Optional[str] = None, allow_empty_mobile_match: bool = False) -> Optional[Dict[str, Any]]:
        if self.crm_adapter is None:
            return None
        if hasattr(self.crm_adapter, 'verify_customer'):
            verify_payload = {
                'ywId': yw_id,
                'mobile': mobile,
                'appName': app_name,
                'deptName': dept_name,
                'pendaftaranGroup': registration_group,
                'wa': official_group,
            }
            verify_payload = {k: v for k, v in verify_payload.items() if v not in (None, '')}
            try:
                verify_response = self.crm_adapter.verify_customer(verify_payload)
            except Exception:
                verify_response = None
            if verify_response and verify_response.get('code') == 0:
                data = verify_response.get('data') or {}
                return {
                    'id': data.get('customerId'),
                    'ywId': data.get('ywId') or yw_id,
                    'mobile': mobile,
                    'appName': app_name,
                    'deptName': dept_name,
                    'pendaftaranGroup': registration_group,
                    'wa': official_group,
                    '_source': 'automation_verify',
                    '_verify_response': data,
                }
        attempts: list[Dict[str, Optional[str]]] = []
        seen: set[tuple[str, str]] = set()
        for candidate in [
            {'yw_id': yw_id, 'mobile': mobile},
            {'yw_id': yw_id, 'mobile': None},
            {'yw_id': None, 'mobile': mobile},
        ]:
            key = (str(candidate.get('yw_id') or ''), str(candidate.get('mobile') or ''))
            if key not in seen:
                seen.add(key)
                attempts.append(candidate)
        for candidate in [self._extract_crm_duplicate_hints(crm_response or {})] if crm_response else []:
            if candidate:
                key = (str(candidate.get('yw_id') or ''), str(candidate.get('mobile') or ''))
                if key not in seen:
                    seen.add(key)
                    attempts.append(candidate)
                for narrowed in [
                    {'yw_id': candidate.get('yw_id'), 'mobile': None},
                    {'yw_id': None, 'mobile': candidate.get('mobile')},
                ]:
                    key = (str(narrowed.get('yw_id') or ''), str(narrowed.get('mobile') or ''))
                    if key not in seen:
                        seen.add(key)
                        attempts.append(narrowed)
        for candidate in attempts:
            if not candidate.get('yw_id') and not candidate.get('mobile'):
                continue
            try:
                row = self.crm_adapter.find_customer(yw_id=candidate.get('yw_id'), mobile=candidate.get('mobile'))
            except Exception:
                row = None
            if row and self._crm_row_matches_expected(
                row,
                yw_id=yw_id,
                mobile=mobile,
                app_name=app_name,
                dept_name=dept_name,
                registration_group=registration_group,
                official_group=official_group,
                allow_empty_mobile_match=allow_empty_mobile_match,
            ):
                return row
        return None

    def _normalize_crm_failure_reason(self, crm_response: Dict[str, Any], *, fallback_found: bool) -> str:
        if self._crm_response_looks_like_duplicate(crm_response):
            if fallback_found:
                return 'Data duplication.'
            return 'Data duplication.'
        return 'CRM write was rejected.'

    def _validate_intake_preset_dropdown_value(self, *, field_name: str, option_type: str, value: str) -> str:
        normalized_value = str(value or '').strip()
        if not normalized_value:
            raise HTTPException(status_code=400, detail=f'{field_name} is required.')
        dropdown_state = self._list_crm_dropdown_options(option_type=option_type)
        options = dropdown_state.get('options') or []
        option_values = {str((item or {}).get('value') or '').strip() for item in options}
        option_values.discard('')
        if not option_values:
            raise HTTPException(status_code=400, detail=f'{field_name} CRM dropdown options are unavailable. Please restore CRM options first.')
        if normalized_value not in option_values:
            raise HTTPException(status_code=400, detail=f'{field_name} must be selected from CRM dropdown options.')
        return normalized_value

    def list_intake_bot_presets(self) -> Dict[str, Any]:
        self.ensure_current_intake_preset()
        all_rows = self._fetch_intake_bot_preset_rows()
        rows = [row for row in all_rows if str(row.get('profile_name') or '').strip() != 'current']
        if not rows:
            rows = all_rows
        app_dropdown = self._list_crm_dropdown_options(option_type='app')
        guild_dropdown = self._list_crm_dropdown_options(option_type='guild')
        return {
            'rows': rows,
            'app_options': app_dropdown.get('options') or [],
            'guild_options': guild_dropdown.get('options') or [],
            'app_options_source': str(app_dropdown.get('source') or 'unavailable'),
            'guild_options_source': str(guild_dropdown.get('source') or 'unavailable'),
        }

    def _hermes_profile_root(self) -> Path:
        return Path(os.getenv('HERMES_HOME') or (Path.home() / '.hermes')).expanduser()

    def _read_dotenv_lines(self, path: Path) -> List[str]:
        if not path.exists():
            return []
        return path.read_text(encoding='utf-8', errors='ignore').splitlines()

    def _write_dotenv_values(self, env_path: Path, updates: Dict[str, str], *, remove_keys: Optional[List[str]] = None) -> None:
        remove = set(remove_keys or [])
        keys = set(updates) | remove
        lines = []
        seen = set()
        for line in self._read_dotenv_lines(env_path):
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or '=' not in stripped:
                lines.append(line)
                continue
            key = stripped.split('=', 1)[0].strip().lstrip('export ').strip()
            if key in remove:
                seen.add(key)
                continue
            if key in updates:
                value = str(updates[key])
                escaped = value.replace("'", "'\\''")
                lines.append(f"{key}='{escaped}'")
                seen.add(key)
            else:
                lines.append(line)
        for key, value in updates.items():
            if key not in seen:
                escaped = str(value).replace("'", "'\\''")
                lines.append(f"{key}='{escaped}'")
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text('\n'.join(lines).rstrip() + '\n', encoding='utf-8')
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass

    def _validate_lark_app_credentials(self, *, app_id: str, app_secret: str) -> Dict[str, Any]:
        normalized_app_id = str(app_id or '').strip()
        normalized_secret = str(app_secret or '').strip()
        if not normalized_app_id or not normalized_secret:
            return {'ok': False, 'code': 'missing_credentials', 'msg': 'missing app_id or app_secret'}
        try:
            response = requests.post(
                'https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal',
                json={'app_id': normalized_app_id, 'app_secret': normalized_secret},
                timeout=15,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            return {'ok': False, 'code': 'request_failed', 'msg': str(exc)[:180]}
        return {'ok': body.get('code') == 0, 'code': body.get('code'), 'msg': str(body.get('msg') or '')[:180]}

    def _configure_intake_bot_profile(self, *, profile_name: str, app_id: str, app_secret: str) -> Dict[str, Any]:
        normalized_profile = str(profile_name or '').strip()
        normalized_app_id = str(app_id or '').strip()
        normalized_secret = str(app_secret or '').strip()
        if not normalized_profile or not normalized_app_id or not normalized_secret:
            return {'configured': False, 'reason': 'missing_profile_or_credentials'}
        hermes_home = self._hermes_profile_root()
        profile_dir = hermes_home / 'profiles' / normalized_profile
        created = False
        commands: List[Dict[str, Any]] = []
        if not profile_dir.exists():
            hermes_cli = shutil.which('hermes')
            source_profile_dir = hermes_home / 'profiles' / 'intake'
            if hermes_cli:
                cmd = [hermes_cli, 'profile', 'create', normalized_profile, '--clone-from', 'intake']
                proc = subprocess.run(cmd, cwd=str(Path.cwd()), capture_output=True, text=True, timeout=90)
                commands.append({'action': 'profile_create', 'returncode': proc.returncode, 'stderr': (proc.stderr or '')[-300:]})
                if proc.returncode != 0:
                    raise HTTPException(status_code=500, detail=f'profile_create_failed:{proc.stderr[-200:] if proc.stderr else proc.returncode}')
                created = True
            elif source_profile_dir.exists():
                def _ignore_profile_runtime(src: str, names: List[str]) -> set:
                    return {name for name in names if name in {'logs', 'sessions', 'tmp', '.cache'} or name.endswith('.log')}
                shutil.copytree(source_profile_dir, profile_dir, ignore=_ignore_profile_runtime)
                commands.append({'action': 'profile_clone_without_hermes_cli', 'returncode': 0, 'stderr': 'hermes_cli_not_found; cloned profile directory only'})
                created = True
            else:
                profile_dir.mkdir(parents=True, exist_ok=True)
                commands.append({'action': 'profile_create_without_hermes_cli', 'returncode': 0, 'stderr': 'hermes_cli_not_found; created profile directory only'})
                created = True
        env_path = profile_dir / '.env'
        updates = {
            'FEISHU_APP_ID': normalized_app_id,
            'FEISHU_APP_SECRET': normalized_secret,
            'FEISHU_DOMAIN': 'lark',
            'FEISHU_CONNECTION_MODE': 'websocket',
            'FEISHU_ALLOW_ALL_USERS': 'true',
            'GATEWAY_ALLOW_ALL_USERS': 'true',
            'HERMES_DETERMINISTIC_INTAKE_ENABLED': 'true',
            'HERMES_DETERMINISTIC_INTAKE_URL': 'http://127.0.0.1:8011/api/intake/lark/events',
            'HERMES_DETERMINISTIC_INTAKE_TIMEOUT_SECONDS': '12',
            'HERMES_DETERMINISTIC_INTAKE_FALLBACK_TO_AGENT': 'false',
        }
        self._write_dotenv_values(env_path, updates, remove_keys=['FEISHU_ALLOWED_USERS'])
        default_auth = hermes_home / 'auth.json'
        profile_auth = profile_dir / 'auth.json'
        if default_auth.exists() and not profile_auth.exists():
            shutil.copy2(default_auth, profile_auth)
            try:
                os.chmod(profile_auth, 0o600)
            except OSError:
                pass
        hermes_cli_for_gateway = shutil.which('hermes')
        for action, cmd in [
            ('gateway_install', [hermes_cli_for_gateway, '-p', normalized_profile, 'gateway', 'install'] if hermes_cli_for_gateway else []),
            ('gateway_restart', [hermes_cli_for_gateway, '-p', normalized_profile, 'gateway', 'restart'] if hermes_cli_for_gateway else []),
        ]:
            if not cmd:
                commands.append({'action': action, 'returncode': -1, 'stderr': 'hermes_cli_not_found'})
                continue
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
                commands.append({'action': action, 'returncode': proc.returncode, 'stderr': (proc.stderr or '')[-300:]})
            except Exception as exc:
                commands.append({'action': action, 'returncode': -1, 'stderr': str(exc)[-300:]})
        return {'configured': True, 'profile_name': normalized_profile, 'created': created, 'env_path': str(env_path), 'commands': commands}

    def activate_local_intake_bot_gateway(self, profile_name: str, payload: LocalIntakeBotGatewayActivationRequest) -> Dict[str, Any]:
        normalized_profile_name = str(profile_name or '').strip()
        if not normalized_profile_name or normalized_profile_name == 'current':
            raise HTTPException(status_code=400, detail='profile_name is required')
        if not str(payload.app_secret or '').strip():
            raise HTTPException(status_code=400, detail='app_secret is required for gateway activation')
        saved = self.update_intake_bot_preset(normalized_profile_name, payload)
        setup = saved.get('profile_setup') or {}
        commands = setup.get('commands') or []
        failed_commands = [cmd for cmd in commands if cmd.get('action') in {'gateway_install', 'gateway_restart'} and int(cmd.get('returncode') if cmd.get('returncode') is not None else -1) != 0]
        if failed_commands:
            return {
                'ok': False,
                'profile_name': normalized_profile_name,
                'saved': True,
                'reason': 'gateway_command_failed',
                'profile_setup': setup,
                'failed_commands': failed_commands,
            }
        app_id = str(payload.app_id or '').strip()
        resolved = self.resolve_intake_bot_preset(app_id=app_id or None, profile_name=normalized_profile_name)
        logs_dir = self._hermes_profile_root() / 'profiles' / normalized_profile_name / 'logs'
        agent_log = logs_dir / 'agent.log'
        gateway_log = logs_dir / 'gateway.log'
        log_tail = ''
        connected = False
        for _ in range(20):
            log_tail = ''
            for log_path in (agent_log, gateway_log):
                if log_path.exists():
                    try:
                        log_tail += '\n'.join(log_path.read_text(errors='ignore').splitlines()[-100:]) + '\n'
                    except Exception:
                        pass
            connected = ('Connected in websocket mode' in log_tail) and ('connected to wss://msg-frontier' in log_tail)
            if connected:
                break
            time.sleep(1)
        return {
            'ok': bool(connected),
            'profile_name': normalized_profile_name,
            'saved': True,
            'resolved': resolved,
            'profile_setup': setup,
            'gateway_connected': bool(connected),
            'reason': None if connected else 'gateway_not_connected_yet',
        }

    def update_intake_bot_preset(self, profile_name: str, payload: IntakeBotPresetUpdateRequest) -> Dict[str, Any]:
        self.ensure_current_intake_preset()
        normalized_profile_name = str(profile_name or '').strip()
        if not normalized_profile_name:
            raise HTTPException(status_code=400, detail='profile_name is required')
        normalized_app = self._validate_intake_preset_dropdown_value(
            field_name='default_app',
            option_type='app',
            value=payload.default_app,
        )
        normalized_guild = self._validate_intake_preset_dropdown_value(
            field_name='default_guild',
            option_type='guild',
            value=payload.default_guild,
        )
        existing = next((row for row in self._fetch_intake_bot_preset_rows() if str(row.get('profile_name') or '').strip() == normalized_profile_name), None)
        normalized_app_id = str(payload.app_id or (existing or {}).get('app_id') or '').strip()
        if normalized_profile_name == 'current' and not normalized_app_id:
            normalized_app_id = str(self.current_lark_app_id or '').strip()
        if normalized_profile_name != 'current' and not normalized_app_id:
            raise HTTPException(status_code=400, detail='app_id is required when creating a new bot preset.')
        duplicate_app = next((row for row in self._fetch_intake_bot_preset_rows()
                              if str(row.get('profile_name') or '').strip() != normalized_profile_name
                              and str(row.get('app_id') or '').strip()
                              and str(row.get('app_id') or '').strip() == normalized_app_id), None)
        if normalized_profile_name != 'current' and duplicate_app:
            raise HTTPException(status_code=400, detail=f'app_id_already_used_by_profile:{duplicate_app.get("profile_name")}')
        normalized_robot_name = str(payload.robot_name or (existing or {}).get('robot_name') or normalized_profile_name).strip() or normalized_profile_name
        profile_setup: Dict[str, Any] = {'configured': False, 'reason': 'app_secret_not_provided'}
        if str(payload.app_secret or '').strip() and normalized_profile_name != 'current':
            credential_check = self._validate_lark_app_credentials(app_id=normalized_app_id, app_secret=str(payload.app_secret or '').strip())
            if not credential_check.get('ok'):
                raise HTTPException(status_code=400, detail=f'lark_app_credentials_invalid:{credential_check.get("code")}:{credential_check.get("msg")}')
            profile_setup = self._configure_intake_bot_profile(
                profile_name=normalized_profile_name,
                app_id=normalized_app_id,
                app_secret=str(payload.app_secret or '').strip(),
            )
        saved_row = self._upsert_intake_bot_preset_row(
            profile_name=normalized_profile_name,
            app_id=normalized_app_id,
            robot_name=normalized_robot_name,
            default_app=normalized_app,
            default_guild=normalized_guild,
            enabled=int((existing or {}).get('enabled') or 1),
        )
        if normalized_profile_name == 'current':
            self.lark_default_app_name = normalized_app
            self.lark_default_dept_name = normalized_guild
            if normalized_app_id:
                self.current_lark_app_id = normalized_app_id
            # Best-effort prewarm so the write path can survive later CRM dropdown flakiness.
            self._resolve_crm_app_mapping(normalized_app)
            self._resolve_crm_dept_mapping(normalized_guild)
        return {
            'saved': True,
            **saved_row,
            'profile_setup': profile_setup,
        }

    def delete_intake_bot_preset(self, profile_name: str) -> Dict[str, Any]:
        self.ensure_current_intake_preset()
        normalized_profile_name = str(profile_name or '').strip()
        if not normalized_profile_name:
            raise HTTPException(status_code=400, detail='profile_name is required')
        if normalized_profile_name == 'current':
            raise HTTPException(status_code=400, detail='cannot_delete_current_preset')
        with self.db.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM intake_bot_presets WHERE profile_name = ?",
                (normalized_profile_name,),
            )
            conn.commit()
        return {'ok': True, 'deleted': bool(cursor.rowcount)}

    def daily_summary(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            lead_count = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            engaged_count = conn.execute("SELECT COUNT(*) FROM lead_events WHERE event_type IN ('contact_clicked', 'wa_redirected', 'account_id_submitted')").fetchone()[0]
            account_submitted_count = conn.execute("SELECT COUNT(*) FROM lead_events WHERE event_type = 'account_id_submitted'").fetchone()[0]
            success_count = conn.execute("SELECT COUNT(*) FROM automation_tasks WHERE status = 'success'").fetchone()[0]
            failed_count = conn.execute("SELECT COUNT(*) FROM automation_tasks WHERE status = 'failed'").fetchone()[0]
            pending_count = conn.execute("SELECT COUNT(*) FROM automation_tasks WHERE status IN ('pending', 'running', 'retry_waiting')").fetchone()[0]
            task_count = conn.execute("SELECT COUNT(*) FROM automation_tasks").fetchone()[0]
        return {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "lead_count": lead_count,
            "engaged_count": engaged_count,
            "account_submitted_count": account_submitted_count,
            "task_count": task_count,
            "completed_task_count": success_count,
            "success_count": success_count,
            "failed_count": failed_count,
            "pending_count": pending_count,
            "top_fail_reasons": [],
            "group_breakdown": [],
            "operator_breakdown": [],
        }


__all__ = ['ExecutorServiceMixin']
